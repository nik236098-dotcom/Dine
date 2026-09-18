import asyncio
import json
import logging
import os
import re
import pyotp  # pip install pyotp — генерация TOTP-кодов из секретного ключа
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = "8839226959:AAEnAfN1Hs3OOqmCPui0sSaz6MjCNsrR6Rc"

# На российских серверах/датацентрах Telegram часто режут — если нужно,
# задайте прокси именно для соединения с Telegram (не для Госуслуг/браузера)
# через переменную окружения, например:
#   export TELEGRAM_PROXY=socks5://127.0.0.1:40000
# (порт — тот, что настроен в Cloudflare WARP в режиме proxy). Локально на
# ПК, где Telegram не блокируется, переменную просто не задавайте.
TELEGRAM_PROXY = os.environ.get("TELEGRAM_PROXY")
bot_session = AiohttpSession(proxy=TELEGRAM_PROXY) if TELEGRAM_PROXY else None

bot = Bot(token=TELEGRAM_BOT_TOKEN, session=bot_session)
dp = Dispatcher()

user_state = {}
user_data = {}
active_tasks = {}  # chat_id -> asyncio.Task текущего process_steps, чтобы /stop мог его отменить

BASE_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot_data")
os.makedirs(BASE_DATA_DIR, exist_ok=True)
USER_PROFILE_DIR = os.path.join(BASE_DATA_DIR, "gosuslugi_profile")

# Страница прямого поиска и оплаты штрафа/квитанции по УИН.
QUITTANCE_URL = "https://www.gosuslugi.ru/pay/quittance"

# Страница входа (esia) — отдельный сайт. Нужна, чтобы залогиниться напрямую,
# если защищённая страница квитанций не отрисовалась и сама не унесла на вход.
ESIA_LOGIN_URL = "https://esia.gosuslugi.ru/login/"

# "История платежей" — надёжный источник правды об итоге оплаты: текст на
# самой форме оплаты угадывался и то и дело подводил (дисклеймеры путались
# с отказом, результат рисовался в iframe и вообще не находился). Здесь же
# у Госуслуг каждый платёж — отдельная карточка с УИН, суммой и статусом
# ("Принят" и т.п.), который можно свериться с тем, что было задано.
PAYMENT_HISTORY_URL = "https://www.gosuslugi.ru/pay/paymentHistory"

# Секретные ключи TOTP хранятся отдельным локальным файлом (папка .bot_data
# в .gitignore, наружу не уходит) — чтобы не спрашивать /totp заново после
# каждого перезапуска бота.
TOTP_SECRETS_FILE = os.path.join(BASE_DATA_DIR, "totp_secrets.json")


def load_totp_secrets():
    try:
        with open(TOTP_SECRETS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_totp_secret(chat_id, secret):
    secrets = load_totp_secrets()
    secrets[str(chat_id)] = secret
    with open(TOTP_SECRETS_FILE, "w", encoding="utf-8") as f:
        json.dump(secrets, f)


totp_secrets = load_totp_secrets()  # chat_id (str) -> секрет, в памяти на весь процесс


class GosuslugiBrowserClient:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.logged_in = False
        self.totp_secret = None  # если задан — код на шаге входа вводится автоматически
        self.tab_pool = []  # свободные вкладки — переиспользуются вместо открытия новых

    async def _launch_browser(self):
        """Поднимает постоянный (persisted) браузерный профиль, если он ещё не запущен.
        Профиль в USER_PROFILE_DIR хранит куки между перезапусками бота, поэтому
        повторный запуск может унаследовать уже действующую сессию Госуслуг."""
        if self.page:
            return
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=USER_PROFILE_DIR, headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--blink-settings=imagesEnabled=true",
                "--ignore-certificate-errors"
            ],
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            locale="ru-RU", timezone_id="Europe/Moscow"
        )
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await self.page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self.page.set_default_timeout(40000)

    # Что видно на защищённой странице (см. _detect_session_state). Отдельно
    # от селектора поиска: там в конце есть общий input[type='text'] как
    # запасной вариант — для ПОЛОЖИТЕЛЬНОГО признака "мы внутри" он не годится
    # (поле логина на esia — тоже input[type='text']).
    UIN_FIELD_SELECTOR = (
        "input[name*='uin' i], input[id*='uin' i], "
        "input[placeholder*='УИН'], input[placeholder*='уин']"
    )
    LOGIN_FORM_SELECTOR = "input#login, input#password, input[type='password']"

    async def _detect_session_state(self, page, max_wait=45):
        """Смотрит, что РЕАЛЬНО показала защищённая страница, и возвращает:
        'logged_in'  — видно поле УИН (мы внутри, сессия живая);
        'logged_out' — унесло на esia/login или видна форма входа;
        'blank'      — за max_wait сек ни того, ни другого: страница пустая,
                       не грузится или заблокирована.
        Раньше признаком живой сессии было "не унесло на логин" — и пустая,
        так и не загрузившаяся страница сходила за "Сессия активна", хотя
        сессии не было вовсе. Теперь сессия считается живой только по
        положительному признаку."""
        waited = 0
        while waited < max_wait:
            url = page.url
            if "login" in url or "esia" in url:
                return "logged_out"
            try:
                if await page.locator(self.LOGIN_FORM_SELECTOR).first.is_visible():
                    return "logged_out"
                if await page.locator(self.UIN_FIELD_SELECTOR).first.is_visible():
                    return "logged_in"
                # На странице поиска квитанций слово "УИН" есть в подписи к
                # полю — тоже годится как признак, что страница наша и
                # отрисовалась, даже если атрибуты самого поля другие.
                body_text = await page.inner_text("body")
                if "УИН" in body_text and len(body_text.strip()) >= 50:
                    return "logged_in"
            except Exception:
                pass
            await asyncio.sleep(1)
            waited += 1

        # Диагностика пустой страницы — чтобы понять, что именно сайт показал
        # (ничего / капчу / "подождите" / баннер), а не гадать.
        try:
            body_text = await page.inner_text("body")
        except Exception:
            body_text = ""
        logger.warning(
            "Защищённая страница не отрисовалась за %d сек: url=%s, текст (%d симв.): %s",
            max_wait, page.url, len(body_text), body_text[:300],
        )
        await self._log_clickables(page)
        try:
            await page.screenshot(path=os.path.join(BASE_DATA_DIR, "blank_page.png"))
        except Exception:
            pass
        return "blank"

    async def _log_clickables(self, page):
        """Пишет в лог видимые кнопки/ссылки на странице и во фреймах — чтобы
        по логу было видно, какая кнопка на баннере, без VNC."""
        for frame in page.frames:
            try:
                items = await frame.eval_on_selector_all(
                    "button, a, [role='button']",
                    "els => els.filter(e => e.offsetParent !== null).map(e => (e.innerText || e.getAttribute('aria-label') || '').trim()).filter(t => t).slice(0, 30)",
                )
                logger.info("Фрейм %s — кнопки/ссылки: %s", frame.url[:80], items)
            except Exception as e:
                logger.info("Фрейм %s — не удалось прочитать кнопки: %s", frame.url[:80], e)

    BLANK_PAGE_MSG = (
        "⚠️ Страница Госуслуг не загрузилась (пустая {secs} сек) — на логин не "
        "перекинуло, но и содержимого нет. Возможно, сайт не отвечает или блокирует "
        "этот сервер. Скриншот: .bot_data/blank_page.png. Загляни через VNC, что на экране."
    )

    async def ensure_logged_in(self):
        """Проверяет, действует ли уже сохранённая сессия Госуслуг, не запрашивая
        логин/пароль заново. Используется в /pay, чтобы не гонять пользователя
        через /login при каждом перезапуске бота, если сессия ещё жива."""
        try:
            await self._launch_browser()
            # Проверяем на защищённой странице (той же, куда падает /pay):
            # публичная главная gosuslugi.ru не требует входа и не редиректит,
            # поэтому по ней сессия всегда выглядела бы живой.
            await self.page.goto(QUITTANCE_URL, wait_until="load")
            state = await self._detect_session_state(self.page)

            if state == "logged_in":
                self.logged_in = True
                return "already_logged_in", "✨ Сессия активна! Можно проверять штраф."

            self.logged_in = False
            if state == "blank":
                return False, self.BLANK_PAGE_MSG.format(secs=45)
            return False, "❌ Сохранённая сессия не найдена или истекла."
        except Exception as e:
            await self.close()
            return False, f"Ошибка проверки сессии: {str(e)}"

    # "Зал ожидания" на входе esia: "Скоро вы сможете войти… Здесь появится
    # ссылка для входа… Осталось 03:00". Ждём до 6 минут — с запасом к их трём.
    LOGIN_QUEUE_MAX_WAIT = 360
    LOGIN_QUEUE_MARKERS = ("Скоро вы сможете войти", "появится ссылка для входа", "Осталось")
    LOGIN_LINK_SELECTOR = (
        "a:has-text('Войти'), button:has-text('Войти'), a:has-text('войти'), "
        "a:has-text('Перейти'), button:has-text('Перейти'), a:has-text('Продолжить'), "
        "button:has-text('Продолжить')"
    )

    async def _wait_through_login_queue(self, page):
        """Госуслуги перед формой входа стали показывать таймер (~3 мин), после
        которого появляется ссылка для входа — её нужно нажать. Ждём конца
        таймера, жмём ссылку, ждём форму. Возвращает 'form' (форма входа на
        экране), 'already_logged_in' (esia увела с /login в кабинет — сессия
        жива) или 'timeout'."""
        form_selector = "input#login, input[type='text'], input[type='password']"
        waited = 0
        clicked_link = False
        while waited < self.LOGIN_QUEUE_MAX_WAIT:
            try:
                if await page.locator(form_selector).first.is_visible():
                    return "form"
            except Exception:
                pass
            if "login" not in page.url and "esia" not in page.url:
                return "already_logged_in"

            try:
                body_text = await page.inner_text("body")
            except Exception:
                body_text = ""
            in_queue = any(marker in body_text for marker in self.LOGIN_QUEUE_MARKERS)

            if in_queue:
                if waited % 30 == 0:
                    remaining = re.search(r"\d{1,2}\s*:\s*\d{2}", body_text)
                    logger.info("Таймер на входе Госуслуг, осталось %s (жду уже %d сек)",
                                remaining.group(0) if remaining else "?", waited)
            elif not clicked_link:
                # Таймер закончился — ищем появившуюся ссылку для входа. Сначала
                # по ожидаемому тексту, иначе — любую видимую ссылку/кнопку,
                # кроме переключателя языка.
                try:
                    link = page.locator(self.LOGIN_LINK_SELECTOR).first
                    if not (await link.count() > 0 and await link.is_visible()):
                        link = page.locator("a, button").filter(has_not_text="Русский").first
                    if await link.count() > 0 and await link.is_visible():
                        text = (await link.inner_text()).strip()[:40]
                        await link.click()
                        clicked_link = True
                        logger.info("Таймер на входе прошёл — нажал ссылку для входа: «%s»", text)
                        await asyncio.sleep(3)
                except Exception as e:
                    logger.info("Не удалось нажать ссылку для входа: %s", e)

            await asyncio.sleep(1)
            waited += 1

        await self._log_clickables(page)
        return "timeout"

    async def start_auth(self, username, password):
        try:
            await self._launch_browser()

            # /login идёт СРАЗУ на страницу входа (esia), а не через страницу
            # квитанций: та может быть накрыта баннером-заставкой Госуслуг и
            # не грузиться вовсе, а esia — отдельный сайт, ему это не мешает.
            await self.page.goto(ESIA_LOGIN_URL, wait_until="load")
            outcome = await self._wait_through_login_queue(self.page)
            if outcome == "already_logged_in":
                self.logged_in = True
                return "already_logged_in", "✨ Сессия активна! Можно сразу проверять штраф через /pay."
            if outcome != "form":
                await self._dump_error_state("login_blank")
                await self.close()
                return False, (
                    f"⚠️ Форма входа так и не появилась за {self.LOGIN_QUEUE_MAX_WAIT // 60} мин. "
                    "Скриншот: .bot_data/error_login_blank.png"
                )

            # Если в этом браузерном профиле уже кто-то входил раньше, esia
            # сразу показывает запомненного пользователя (только поле пароля,
            # без поля логина) — тогда сначала жмём "Другой пользователь",
            # чтобы получить обычную форму с полем логина.
            try:
                await self.page.get_by_text("Другой пользователь").first.click(timeout=3000)
                await asyncio.sleep(1)
                logger.info("На странице входа был запомненный аккаунт — нажал 'Другой пользователь'")
            except PlaywrightTimeoutError:
                pass  # поле логина, видимо, и так на странице — идём дальше как обычно

            await self.page.wait_for_selector("input#login, input[type='text']", state="visible")
            login_field = self.page.locator("input#login, input[type='text']").first
            await login_field.click()
            await login_field.fill(username)

            await self.page.click("button[type='submit'], button.plain-button")
            await asyncio.sleep(2.5)

            await self.page.wait_for_selector("input#password, input[type='password']", state="visible")
            password_field = self.page.locator("input#password, input[type='password']").first
            await password_field.click()
            await password_field.fill(password)

            await self.page.click("button[type='submit'], button.plain-button")

            if self.totp_secret:
                # Ключ TOTP задан — вводим код из приложения автоматически,
                # не спрашивая пользователя в Telegram вообще.
                try:
                    await asyncio.sleep(2)
                    # Код генерируется внутри, когда поле реально появится —
                    # до него теперь можно ждать минуты (таймер Госуслуг).
                    success, msg = await self.enter_sms_code(totp_secret=self.totp_secret)
                    if success:
                        return "auto_logged_in", "✅ Авторизация успешна (TOTP-код введён автоматически)."
                    return False, f"❌ Автоматический ввод TOTP не сработал: {msg}"
                except Exception as e:
                    await self.close()
                    return False, f"❌ Ошибка автоматического TOTP: {e}"

            return True, "Данные заполнены! Введите СМС-код из телефона в чат бота:"
        except Exception as e:
            await self._dump_error_state("login")
            await self.close()
            return False, f"Ошибка при вводе данных: {str(e)}"

    async def _dump_error_state(self, tag):
        """Сохраняет скриншот и адрес страницы в момент ошибки в .bot_data —
        чтобы разбирать сбои на сервере (без визуального доступа под рукой)
        не вслепую, а по факту того, что реально показывал Госуслуги."""
        try:
            if self.page:
                path = os.path.join(BASE_DATA_DIR, f"error_{tag}.png")
                await self.page.screenshot(path=path)
                logger.warning("Скриншот ошибки сохранён: %s (url=%s)", path, self.page.url)
        except Exception as e:
            logger.warning("Не удалось сохранить скриншот ошибки (%s): %s", tag, e)

    # Сколько ждать поле для кода после ввода пароля. Госуслуги стали ставить
    # таймер (~3 минуты) перед тем, как пустить дальше — 15 секунд, как было,
    # уже не хватает.
    CODE_FIELD_MAX_WAIT = 240

    async def enter_sms_code(self, code=None, totp_secret=None):
        """Вводит код подтверждения. Либо готовый code (СМС, который прислал
        пользователь), либо totp_secret — тогда код генерируется ТОЛЬКО когда
        поле для него реально появилось: TOTP живёт ~30 секунд, а до поля
        теперь можно ждать несколько минут из-за таймера Госуслуг, и код,
        сгенерированный заранее, к тому моменту протух бы."""
        try:
            if not self.page: return False, "Сессия не найдена. Начните сначала через /login."
            # Поле ввода кода может быть и под СМС, и под TOTP из приложения —
            # набор атрибутов на всякий случай пошире, чем просто type='tel'.
            code_selector = (
                "input[type='tel'], input[inputmode='numeric'], "
                "input[autocomplete='one-time-code'], input[name*='otp' i], input[name*='code' i]"
            )
            waited = 0
            while True:
                try:
                    if await self.page.locator(code_selector).first.is_visible():
                        break
                except Exception:
                    pass
                if waited >= self.CODE_FIELD_MAX_WAIT:
                    await self._dump_error_state("code_field")
                    return False, (
                        f"⏱ Поле для кода так и не появилось за {self.CODE_FIELD_MAX_WAIT // 60} мин. "
                        "Скриншот: .bot_data/error_code_field.png"
                    )
                # Раз в полминуты — в лог, что сейчас на странице (таймер и т.п.),
                # чтобы по логу было видно, чего ждём.
                if waited % 30 == 0:
                    try:
                        snippet = (await self.page.inner_text("body")).strip().replace("\n", " | ")[:200]
                    except Exception:
                        snippet = "?"
                    logger.info("Жду поле для кода (%d сек), на странице: %s", waited, snippet)
                await asyncio.sleep(1)
                waited += 1

            if totp_secret:
                code = pyotp.TOTP(totp_secret).now()
            await self.page.fill(code_selector, code)

            # СТАРЫЙ БАГ: страница входа сама находится на *.gosuslugi.ru
            # (обычно esia.gosuslugi.ru), поэтому wait_for_url с таким широким
            # паттерном совпадал с текущим URL почти сразу и НИЧЕГО не проверял —
            # бот считал вход успешным даже при заведомо неверном коде/пароле.
            await asyncio.sleep(3)

            error_selector = (
                "*:has-text('Неверный код'), *:has-text('неверный код'), "
                "*:has-text('Код неверен'), *:has-text('истёк'), *:has-text('истек'), "
                "*:has-text('Неверный пароль'), *:has-text('неверный логин')"
            )
            if await self.page.locator(error_selector).count() > 0:
                self.logged_in = False
                return False, "❌ Госуслуги отклонили код или данные входа. Проверьте их и попробуйте /login заново."

            try:
                await self.page.wait_for_function(
                    "() => !location.href.includes('esia') && !location.href.includes('login')",
                    timeout=20000
                )
            except PlaywrightTimeoutError:
                pass

            await self.page.wait_for_load_state("domcontentloaded")
            await asyncio.sleep(2)

            # Настоящая проверка успеха: мы реально ушли со страницы входа,
            # и на странице не осталось полей логина/пароля/смс-кода.
            still_on_login = (
                "esia" in self.page.url or "login" in self.page.url
                or await self.page.locator("input[type='tel'], input#login, input#password").count() > 0
            )
            if still_on_login:
                self.logged_in = False
                return False, ("❌ Авторизация не завершена — сайт всё ещё показывает форму входа. "
                                "Проверьте логин/пароль/код и попробуйте /login заново.")

            self.logged_in = True
            return True, "✅ Авторизация успешна! Введите /pay для проверки УИН."
        except Exception as e:
            await self.close()
            return False, f"Ошибка при вводе СМС: {str(e)}"

    async def check_penalty_by_uin(self, uin, page):
        """Ищет штраф по УИН на переданной странице (page). Страница передаётся
        явно, а не берётся из self.page, чтобы можно было запускать несколько
        таких проверок параллельно на разных вкладках одного браузера."""
        try:
            if not page or not self.logged_in:
                return False, "Вы не авторизованы. Введите /login.", page, False, None

            # БАГ БЫЛ ЗДЕСЬ: раньше переход выполнялся на "https://gosuslugi.ru" (главная),
            # а не на страницу поиска квитанций — на главной нет поля ввода УИН,
            # поэтому wait_for_selector ниже всегда падал по таймауту.
            logger.info("Выполняю переход на %s", QUITTANCE_URL)
            await page.goto(QUITTANCE_URL, wait_until="load")

            # Если сессия истекла или сайт заподозрил автоматизацию — нас могут
            # редиректнуть обратно на страницу логина. Проверяем это явно,
            # вместо того чтобы падать в неясный Timeout.
            await asyncio.sleep(2)
            if "login" in page.url or "esia" in page.url:
                self.logged_in = False
                return False, "⚠️ Сессия слетела — сайт вернул вас на страницу входа. Авторизуйтесь заново через /login.", page, False, None

            uin_selector = (
                "input[name*='uin' i], input[id*='uin' i], "
                "input[placeholder*='УИН'], input[placeholder*='уин'], "
                "input[type='text']"
            )
            try:
                await page.wait_for_selector(uin_selector, state="visible", timeout=25000)
            except PlaywrightTimeoutError:
                # В лог — что за кнопки видны на странице (баннер/заставка?),
                # чтобы разбирать по логу, а не вслепую.
                await self._log_clickables(page)
                raise
            uin_input = page.locator(uin_selector).first

            await uin_input.click()
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Delete")
            await uin_input.fill(uin)
            await asyncio.sleep(1)

            button = page.locator("button[type='submit'], button:has-text('Найти'), button:has-text('Проверить')").first
            await button.click()

            # Снимок сразу после клика — чтобы видеть, что произошло на странице,
            # даже если поиск в итоге зависнет и упадёт по таймауту ниже.
            try:
                await asyncio.sleep(1)
                await page.screenshot(path=os.path.join(BASE_DATA_DIR, "after_search_click.png"))
            except Exception:
                pass

            # Даём странице время подгрузить результат поиска (это AJAX/React,
            # а не обычная навигация) прежде чем искать сумму.
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except PlaywrightTimeoutError:
                pass
            await asyncio.sleep(2)

            # ВАЖНО: рубль на этой странице визуально похож на "₽", но фактически
            # это кириллическая буква "Р" в специальном шрифте — поэтому поиск по
            # символу "₽" никогда не срабатывал. Ищем по однозначному тексту:
            # заголовку "Найдено ..." и кнопке "Оплатить", которые реально есть в DOM.
            result_selector = (
                "*:has-text('Найдено'), button:has-text('Оплатить'), "
                "button:has-text('Перейти к оплате')"
            )
            not_found_selector = (
                "*:has-text('не найдено'), *:has-text('не найден'), "
                "*:has-text('ничего не найдено'), *:has-text('отсутствует')"
            )

            logger.info("Всего фреймов на странице: %d", len(page.frames))

            found = False
            for frame in page.frames:
                try:
                    await frame.wait_for_selector(result_selector, state="visible", timeout=30000)
                    found = True
                    break
                except PlaywrightTimeoutError:
                    continue

            if not found:
                # Точная диагностика: что реально видел Playwright в момент неудачи.
                try:
                    body_text = await page.inner_text("body")
                    logger.info("Длина текста body: %d символов", len(body_text))
                    logger.info("Вхождений 'Найдено' в тексте: %d", body_text.count("Найдено"))
                    logger.info("Первые 500 символов текста страницы: %s", body_text[:500])
                except Exception as diag_err:
                    logger.warning("Не удалось прочитать текст страницы для диагностики: %s", diag_err)

                if await page.locator(not_found_selector).count() > 0:
                    return False, "ℹ️ По этому УИН ничего не найдено — возможно, штраф уже оплачен или УИН введён неверно.", page, False, None
                raise PlaywrightTimeoutError("сумма штрафа не появилась ни на странице, ни во фреймах")

            # Достаём сумму штрафа для вывода в сообщении пользователю.
            # Внимание: символ рубля в тексте страницы — это буква "Р", не "₽".
            amount_str = None
            try:
                body_text = await page.inner_text("body")
                amount_match = re.search(r"(\d[\d \s]*\d)\s*[Р₽](?![а-яёА-ЯЁ])", body_text)
                if amount_match:
                    amount_str = amount_match.group(1).replace(" ", " ").strip()
            except Exception:
                pass

            pay_button = page.locator("button:has-text('Оплатить'), button:has-text('Перейти к оплате')").first
            await pay_button.wait_for(state="visible", timeout=15000)

            # На Госуслугах оплата часто открывается в НОВОЙ вкладке (popup),
            # а не на текущей странице. Если это так — переключаемся на неё,
            # иначе следующий шаг (ввод карты) будет искать поля не там.
            try:
                async with self.context.expect_page(timeout=6000) as new_page_info:
                    await pay_button.click()
                new_page = await new_page_info.value
                await new_page.wait_for_load_state("domcontentloaded")
                page = new_page
                logger.info("Оплата открылась в новой вкладке: %s", new_page.url)
            except PlaywrightTimeoutError:
                # Новая вкладка не появилась — форма оплаты, скорее всего, на этой же странице
                await asyncio.sleep(4)

            # ФССП (в отличие от обычного штрафа ГИБДД) позволяет оплатить долг
            # частями — на странице оплаты появляется ссылка "Оплатить частично".
            # Её наличие и есть признак того, что это ФССП, а не обычный штраф.
            # ВАЖНО: если оплата открылась в НОВОЙ вкладке, страница ещё могла не
            # дорисоваться (domcontentloaded — это только разбор HTML, React
            # дорисовывает позже) — раньше проверка срабатывала слишком рано и
            # ФССП не распознавался. Сначала ждём хоть какого-то контента (не
            # пустую загрузку/спиннер), потом уже проверяем саму ссылку.
            await self._wait_for_page_content(page)

            partial_pay_selector = "a:has-text('Оплатить частично'), button:has-text('Оплатить частично')"
            is_fssp = False
            try:
                await page.wait_for_selector(partial_pay_selector, state="visible", timeout=5000)
                is_fssp = True
            except PlaywrightTimeoutError:
                is_fssp = False
            except Exception:
                pass
            logger.info("Определение ФССП: is_fssp=%s, страница=%s", is_fssp, page.url)

            if is_fssp:
                amount_line = f" Сумма долга: {amount_str} ₽." if amount_str else ""
                # Про ввод суммы — отдельным сообщением с кнопкой "Оставить
                # текущую сумму" (см. prompt_fssp_amount), а не здесь.
                return True, f"✅ ФССП найдено!{amount_line} Доступна частичная оплата.", page, True, amount_str

            amount_line = f" К оплате: {amount_str} ₽." if amount_str else ""
            return True, (
                f"✅ Штраф найден!{amount_line} Введите данные карты в формате: номер|дата|cvv\n"
                "Можно несколько карт — каждую с новой строки, бот будет пробовать их по очереди, "
                "пока платёж не пройдёт."
            ), page, False, amount_str
        except Exception as e:
            try:
                if page:
                    await page.screenshot(path=os.path.join(BASE_DATA_DIR, "error_check_penalty.png"))
            except Exception:
                pass
            current_url = page.url if page else "?"
            # Сессия иногда слетает не сразу, а прямо во время ожидания поля —
            # сайт редиректит на esia.gosuslugi.ru/login с задержкой, и тогда
            # ошибка выглядит как обычный Timeout, хотя причина понятна по URL.
            if "login" in current_url or "esia" in current_url:
                self.logged_in = False
                return False, "⚠️ Сессия слетела прямо во время поиска. Авторизуйтесь заново через /login.", page, False, None
            return False, f"Ошибка поиска: {str(e)} (страница: {current_url})", page, False, None

    async def _wait_for_page_content(self, page, min_length=50, max_wait=60):
        """Ждёт, пока на странице появится хоть какой-то текст, вместо того
        чтобы сдаваться по фиксированному таймауту, пока идёт пустая загрузка/
        крутится спиннер. Как только текст есть — можно уже нормально искать
        конкретные элементы с обычным (коротким) таймаутом."""
        waited = 0
        step = 1
        while waited < max_wait:
            try:
                text = await page.inner_text("body")
            except Exception:
                text = ""
            if len(text.strip()) >= min_length:
                return True
            await asyncio.sleep(step)
            waited += step
        logger.warning("Страница так и не показала контент за %d сек (%s)", max_wait, page.url)
        return False

    async def set_partial_payment_amount(self, page, amount_str):
        """На странице ФССП жмёт 'Оплатить частично', вводит сумму в открывшейся
        модалке и жмёт 'Сохранить'. Возвращает (успех, сообщение)."""
        try:
            # Сначала ждём, пока страница вообще покажет какой-то текст (не
            # пустая загрузка/спиннер) — без этого короткий таймаут ниже мог
            # истечь, пока страница ещё честно грузится.
            await self._wait_for_page_content(page)

            partial_link_selector = "a:has-text('Оплатить частично'), button:has-text('Оплатить частично')"
            try:
                await page.wait_for_selector(partial_link_selector, state="visible", timeout=10000)
            except PlaywrightTimeoutError:
                # Значит либо это уже не та страница (не перезагрузилась как ожидалось),
                # либо ссылка после первого использования называется иначе.
                await page.screenshot(path=os.path.join(BASE_DATA_DIR, "fssp_no_partial_link.png"))
                body_text = await page.inner_text("body")
                logger.info("ФССП: текст страницы (первые 500 симв.): %s", body_text[:500])
                return False, "не нашёл ссылку «Оплатить частично» на странице"

            partial_link = page.locator(partial_link_selector).first
            await partial_link.click()
            await asyncio.sleep(1)

            # Модалка дорисовывается в конец DOM — берём ПОСЛЕДНЕЕ подходящее
            # поле на странице (а не первое, чтобы не задеть поля карты выше).
            amount_input_selector = (
                "input[type='text'], input[type='number'], input[inputmode='decimal'], input:not([type])"
            )
            await page.wait_for_selector(amount_input_selector, state="visible", timeout=8000)
            inputs_count = await page.locator(amount_input_selector).count()
            amount_input = page.locator(amount_input_selector).last
            await amount_input.click()
            await amount_input.fill(str(amount_str))
            await asyncio.sleep(0.5)

            # Проверяем, что реально осталось в поле после fill() — на некоторых
            # React-формах программный fill() не всегда "приживается".
            actual_value = await amount_input.input_value()
            logger.info(
                "ФССП: подходящих input на странице %d, взяли последний, "
                "хотели вписать %s, реально в поле: %s",
                inputs_count, amount_str, actual_value
            )
            try:
                await page.screenshot(path=os.path.join(BASE_DATA_DIR, "fssp_amount_before_save.png"))
            except Exception:
                pass

            save_btn = page.locator("button:has-text('Сохранить')").last
            await save_btn.click()
            await asyncio.sleep(1.5)

            # Финальная проверка: сумма на странице после сохранения должна
            # совпасть с тем, что мы вводили — иначе сохранение не подействовало.
            try:
                body_text_after = await page.inner_text("body")
                logger.info(
                    "ФССП: сумма %s встречается в тексте после сохранения: %s раз",
                    amount_str, body_text_after.count(str(amount_str))
                )
            except Exception:
                pass

            return True, f"Сумма {amount_str} ₽ сохранена (в поле было: {actual_value})."
        except Exception as e:
            try:
                await page.screenshot(path=os.path.join(BASE_DATA_DIR, "error_fssp_amount.png"))
            except Exception:
                pass
            return False, f"Не удалось задать сумму частичной оплаты: {e}"

    async def _wait_for_gazprombank(self, target, card_selector, card_num, send_fn=None, max_attempts=60):
        """Госуслуги выбирают банк-эквайер для платежа заново при каждом новом
        вводе номера карты; какой банк выбран — видно после клика по ссылке
        'Без комиссии банка'. Газпромбанк проводит этот платёж без комиссии,
        остальные — нет. Банк выпадает случайно — иногда с первого раза,
        иногда нет, поэтому лимит попыток большой (по ~3 сек на попытку,
        60 попыток — это до ~3 минут). Если банк не тот, стираем номер карты
        и вводим его заново, пока не выпадет Газпромбанк или не кончатся
        попытки. Возвращает True, если Газпромбанк подтверждён."""
        # Кликабельно именно слово "банка" внутри фразы "Без комиссии банка".
        # Клик открывает модалку "Информация о платеже" с текстом вида
        # 'Перевод пройдёт через «НАЗВАНИЕ БАНКА» ...' и кнопкой "Закрыть".
        bank_link_selector = (
            "a:text-is('банка'), button:text-is('банка'), span:text-is('банка'), "
            "u:text-is('банка'), *:text-is('банка'), "
            "a:has-text('Без комиссии банка'), button:has-text('Без комиссии банка')"
        )
        # ".has-text" матчит ВСЕХ предков, где угодно в поддереве которых есть
        # фраза — от всей модалки до самого заголовка внутри неё. Нужен именно
        # самый ВНЕШНИЙ контейнер (там же лежит и текст "Перевод пройдёт
        # через..."), а не самый глубокий — поэтому .first, а не .last.
        modal_selector = "*:has-text('Информация о платеже')"
        close_button_selector = "button:has-text('Закрыть'), button:text-is('Закрыть')"
        target_bank_names = ("газпромбанк", "гпб", "gazprombank")

        for attempt in range(1, max_attempts + 1):
            await asyncio.sleep(2)  # не быстро — как раз то время, чтобы сайт показал банк

            modal_text = ""
            try:
                bank_link = target.locator(bank_link_selector).first
                if await bank_link.count() > 0:
                    await bank_link.click()
                    await asyncio.sleep(1)
                    modal = target.locator(modal_selector).first
                    if await modal.count() > 0:
                        modal_text = (await modal.inner_text()).lower()
            except Exception as e:
                logger.warning("Не удалось прочитать банк-эквайер (попытка %d): %s", attempt, e)

            # Название банка ищем именно рядом с фразой "Перевод пройдёт через" —
            # так не словим случайное упоминание банка где-то ещё в модалке.
            bank_match = re.search(r"перевод пройдёт через[^«]*«([^»]+)»", modal_text)
            detected_bank = bank_match.group(1) if bank_match else None

            if attempt <= 5 or detected_bank:
                logger.info(
                    "Банк, попытка %d — распознано: %s | текст модалки: %s",
                    attempt, detected_bank, modal_text[:300]
                )

            matched = detected_bank is not None and any(name in detected_bank for name in target_bank_names)

            # Модалку обязательно закрываем в любом случае — иначе она
            # перекрывает поле карты и следующая попытка ничего не сможет ввести.
            try:
                close_btn = target.locator(close_button_selector).first
                if await close_btn.count() > 0:
                    await close_btn.click()
                    await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning("Не удалось закрыть модалку с банком (попытка %d): %s", attempt, e)

            if matched:
                logger.info("Газпромбанк подтверждён с попытки %d", attempt)
                return True

            logger.info(
                "Попытка %d/%d: банк не Газпромбанк (или не определился) — ввожу карту заново",
                attempt, max_attempts
            )

            # Раз в 10 попыток обновляем ТУ ЖЕ строку в статус-сообщении (не
            # добавляем новую) — чтобы было видно, что бот жив, без спама.
            if send_fn and attempt % 10 == 0:
                try:
                    masked = self._mask_card(card_num)
                    await send_fn(f"{masked}: ⏳ банк {attempt}/{max_attempts}", masked)
                except Exception:
                    pass

            if attempt < max_attempts:
                try:
                    card_input = target.locator(card_selector).first
                    await card_input.click()
                    await card_input.fill("")
                    await asyncio.sleep(0.5)
                    await card_input.fill(card_num)
                except Exception as e:
                    logger.warning("Не удалось перевести карту заново (попытка %d): %s", attempt, e)
                    break

        logger.warning("Газпромбанк не подтвердился за %d попыток — продолжаю с текущим банком.", max_attempts)
        return False

    async def _submit_single_card(self, card_num, expiry, cvv, page, send_fn=None):
        """Заполняет форму оплаты одной картой и возвращает (статус, сообщение).
        Статус — одно из: 'success', 'declined', '3ds', 'unknown', 'error'.
        Используется как внутренний шаг pay_with_cards() при переборе карт."""
        try:
            if not page: return "error", "Браузер не активен."

            # СТАРЫЙ БАГ: URL страницы оплаты — payment.gosuslugi.ru, поэтому
            # подстрока "pay" всегда находится в URL ГЛАВНОГО фрейма и код
            # ошибочно останавливался на нём, даже если карта вводится в
            # отдельном iframe. Теперь ищем реальный фрейм по наличию в нём
            # видимого поля номера карты, а не по угаданной подстроке в URL.
            card_selector = (
                "input[autocomplete*='cc-number' i], input[inputmode='numeric'], "
                "input[placeholder*='номер' i], input[name*='card' i], "
                "input[name*='pan' i], input[data-testid*='card' i], input[type='tel']"
            )

            target = None
            for frame in page.frames:
                try:
                    await frame.wait_for_selector(card_selector, state="visible", timeout=8000)
                    target = frame
                    break
                except PlaywrightTimeoutError:
                    continue

            if target is None:
                # Диагностика: логируем реальные атрибуты всех input на странице
                # и во всех фреймах, чтобы не гадать с селекторами вслепую.
                for frame in page.frames:
                    try:
                        inputs_info = await frame.eval_on_selector_all(
                            "input",
                            "els => els.map(e => ({name:e.name, id:e.id, placeholder:e.placeholder, "
                            "type:e.type, autocomplete:e.autocomplete}))"
                        )
                        logger.info("Фрейм %s — input'ы: %s", frame.url, inputs_info)
                    except Exception as diag_err:
                        logger.warning("Не удалось прочитать input'ы фрейма %s: %s", frame.url, diag_err)
                try:
                    await page.screenshot(path=os.path.join(BASE_DATA_DIR, "error_pay_by_card.png"))
                except Exception:
                    pass
                return "error", "поле номера карты не найдено ни на странице, ни во фреймах (см. лог)."

            expiry_selector = (
                "input[autocomplete*='cc-exp' i], input[placeholder*='ММ' i], "
                "input[placeholder*='MM' i], input[placeholder*='срок' i], input[name*='exp' i]"
            )
            cvv_selector = (
                "input[autocomplete*='cc-csc' i], input[placeholder*='CVV' i], "
                "input[placeholder*='CVC' i], input[name*='cvv' i], input[name*='cvc' i]"
            )

            await target.fill(card_selector, card_num)

            got_gazprombank = await self._wait_for_gazprombank(target, card_selector, card_num, send_fn=send_fn)
            bank_note = "" if got_gazprombank else " ⚠️ Газпромбанк не подтвердился — возможна комиссия."

            await target.fill(expiry_selector, expiry)
            await target.fill(cvv_selector, cvv)
            await target.click("button:has-text('Оплатить'), button[type='submit']")

            status, message = await self._await_payment_outcome(page)
            return status, message + bank_note
        except Exception as e:
            try:
                if page:
                    await page.screenshot(path=os.path.join(BASE_DATA_DIR, "error_pay_by_card.png"))
            except Exception:
                pass
            current_url = page.url if page else "?"
            return "error", f"Не удалось автоматически заполнить карту: {str(e)} (страница: {current_url})"

    async def _find_history_blocks(self, history_page, uin):
        """Открывает 'Историю платежей' и возвращает список текстов карточек,
        относящихся к этому УИН (обычно одна, но их может быть несколько,
        если штраф уже оплачивался раньше). Ищем по видимому тексту, а не по
        угаданным CSS-классам — карточку определяем как самого верхнего
        предка, который ещё содержит "УИН: <uin>" ровно один раз (выше —
        уже соседняя карточка/список)."""
        try:
            await history_page.goto(PAYMENT_HISTORY_URL, wait_until="load")
        except Exception as e:
            logger.warning("Не удалось открыть историю платежей: %s", e)
            return []
        await self._wait_for_page_content(history_page)
        try:
            return await history_page.evaluate(
                """(uin) => {
                    const marker = 'УИН: ' + uin;
                    const escaped = marker.replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&');
                    const re = new RegExp(escaped, 'g');
                    const results = [];
                    const all = document.querySelectorAll('body *');
                    for (const el of all) {
                        if (el.children.length > 0) continue;
                        const text = el.textContent || '';
                        if (!text.includes(marker)) continue;
                        let card = el;
                        let node = el;
                        for (let i = 0; i < 10 && node.parentElement; i++) {
                            const parent = node.parentElement;
                            const count = (parent.textContent.match(re) || []).length;
                            if (count > 1) break;
                            card = parent;
                            node = parent;
                        }
                        results.push(card.textContent.trim());
                    }
                    return results;
                }""",
                uin,
            )
        except Exception as e:
            logger.warning("Не удалось прочитать историю платежей: %s", e)
            return []

    async def check_payment_in_history(self, history_page, uin, amount_str, exclude_timestamps=None):
        """Ищет в 'Истории платежей' карточку с этим УИН и суммой (сравнение —
        по цифрам, без учёта пробелов и валютного знака). exclude_timestamps —
        метки времени, уже виденные ДО текущей попытки оплаты (см. вызов в
        pay_with_cards), чтобы не спутать старый (прошлый) платёж с только
        что прошедшим новым. Возвращает (найдено, метка_времени, текст_карточки)."""
        if not uin or not amount_str:
            return False, None, None
        target_digits = re.sub(r"\D", "", amount_str)
        if not target_digits:
            return False, None, None
        exclude_timestamps = exclude_timestamps or set()

        blocks = await self._find_history_blocks(history_page, uin)
        for block in blocks:
            amount_match = re.search(r"(\d[\d \s]*\d)\s*[Р₽](?![а-яёА-ЯЁ])", block)
            if not amount_match or re.sub(r"\D", "", amount_match.group(1)) != target_digits:
                continue
            ts_match = re.search(r"\d{2}\.\d{2}\.\d{4}\s*в\s*\d{2}:\d{2}", block)
            # Если формат времени вдруг окажется другим и регэксп не совпадёт —
            # используем кусок текста самой карточки как запасной ключ, чтобы
            # такую запись всё равно можно было отличить/исключить как уже
            # виденную (иначе она бы всегда казалась "новой").
            timestamp = ts_match.group(0) if ts_match else block.strip()[:80]
            if timestamp in exclude_timestamps:
                continue
            return True, timestamp, block
        return False, None, None

    async def known_history_timestamps(self, history_page, uin, amount_str):
        """Снимок 'ДО' — метки времени уже существующих в истории карточек с
        этим УИН и суммой, снятый ПЕРЕД первой попыткой ввода карты. Дальше
        check_payment_in_history(..., exclude_timestamps=этот_снимок)
        распознаёт только НОВУЮ карточку, а не штраф, оплаченный раньше."""
        found, timestamp, _ = await self.check_payment_in_history(history_page, uin, amount_str)
        timestamps = set()
        if found and timestamp:
            timestamps.add(timestamp)
        # check_payment_in_history возвращает только первое совпадение — если
        # таких платежей в истории уже несколько, находим и остальные, гоняя
        # его же с растущим списком исключений (карточек обычно единицы).
        while found and timestamp:
            found, timestamp, _ = await self.check_payment_in_history(
                history_page, uin, amount_str, exclude_timestamps=timestamps
            )
            if found and timestamp:
                timestamps.add(timestamp)
        return timestamps

    async def _await_payment_outcome(self, page):
        """После клика 'Оплатить' смотрим, что реально ответил сайт: успех,
        отказ или запрос 3DS-подтверждения. Точные тексты угаданы (реального
        лога результата платежа ещё не было) — если ни один вариант не
        совпадёт за отведённое время, в консоль пишется диагностика вместо
        того чтобы просто соврать про успех, как было раньше."""
        success_selector = (
            "*:has-text('Платёж успешно'), *:has-text('Платеж успешно'), "
            "*:has-text('успешно проведён'), *:has-text('успешно проведен'), "
            "*:has-text('Оплата прошла'), *:has-text('Оплачено')"
        )
        declined_selector = (
            "*:has-text('отклонен'), *:has-text('отклонён'), "
            "*:has-text('Отказано'), *:has-text('не удалось провести'), "
            "*:has-text('Ошибка оплаты'), *:has-text('Платёж не прошёл'), "
            "*:has-text('Платеж не прошел'), "
            "*:has-text('Оплата временно не доступна'), *:has-text('Оплата временно недоступна'), "
            "*:has-text('временно недоступна')"
        )
        # "Платёж в обработке" — на практике это и есть финальный успешный
        # статус на Госуслугах (банк принял платёж), а не промежуточный экран
        # загрузки — ничего "более окончательного" после него не появляется.
        # Это не отказ и не 3DS — при таком статусе новую карту пробовать не нужно.
        processing_selector = (
            "*:has-text('в обработке'), *:has-text('Платёж в обработке'), "
            "*:has-text('обрабатывается')"
        )
        threeds_selector = (
            "*:has-text('3-D Secure'), *:has-text('3DS'), "
            "*:has-text('Подтверждение платежа'), *:has-text('Подтверждение операции'), "
            "*:has-text('код из смс'), *:has-text('код из СМС'), "
            "*:has-text('Сбербанк'), *:has-text('SberPay'), *:has-text('Идентификация'), "
            "input[name*='otp' i]"
        )
        # Кнопка "Отмена"/"Отменить" — самый надёжный признак банковской
        # 3DS-страницы (у самих Госуслуг такой кнопки на исходе платежа нет),
        # поэтому её наличие проверяем раньше угаданного текста.
        cancel_selector = (
            "button:has-text('Отмена'), button:has-text('Отменить'), button:has-text('Cancel'), "
            "a:has-text('Отмена'), a:has-text('Отменить'), "
            "input[type='button'][value*='Отмена' i], input[type='submit'][value*='Отмена' i], "
            "*[role='button']:has-text('Отмена')"
        )
        bank_domain_hints = ("sberbank", "sber", "3ds", "acs", "securepay")
        # После клика "Отмена" банк иногда переспрашивает: "Уверены, что хотите
        # отменить покупку?" с кнопками "Вернуться к подтверждению" / "Да, отменить".
        confirm_cancel_selector = (
            "button:has-text('Да, отменить'), a:has-text('Да, отменить'), "
            "*[role='button']:has-text('Да, отменить')"
        )

        async def confirm_cancel_if_asked():
            await asyncio.sleep(1.5)
            for frame in page.frames:
                try:
                    confirm_btn = frame.locator(confirm_cancel_selector).first
                    if await confirm_btn.count() > 0:
                        await confirm_btn.click()
                        logger.info("Подтвердил повторный запрос отмены 3DS ('Да, отменить').")
                        return True
                except Exception:
                    continue
            return False

        outcome = None
        for _ in range(8):  # опрашиваем ~40 секунд — банк может отвечать не сразу
            # СНАЧАЛА проверяем однозначные текстовые статусы (отказ/успех/
            # обработка) — они надёжнее эвристики "домен похож на банк" ниже.
            # ВАЖНО: ищем по ВСЕМ фреймам страницы, а не только на самой
            # верхней — форма оплаты (и, судя по всему, итоговый статус тоже)
            # может рисоваться внутри iframe, куда вводилась карта, а не
            # всплывать в главный документ. Раньше искали только на page,
            # из-за чего реальный успех/отказ внутри iframe не находился
            # вообще, и бот 40 секунд впустую ждал текста, которого никогда
            # не увидит на верхнем уровне.
            try:
                for frame in page.frames:
                    if await frame.locator(declined_selector).count() > 0:
                        outcome = "declined"
                    elif await frame.locator(success_selector).count() > 0:
                        outcome = "success"
                    elif await frame.locator(processing_selector).count() > 0:
                        outcome = "processing"
                    if outcome:
                        break
            except Exception:
                pass

            if not outcome:
                for frame in page.frames:
                    try:
                        if await frame.locator(cancel_selector).count() > 0:
                            outcome = "3ds"
                        elif any(hint in frame.url.lower() for hint in bank_domain_hints):
                            outcome = "3ds"
                        elif await frame.locator(threeds_selector).count() > 0:
                            outcome = "3ds"
                        if outcome:
                            break
                    except Exception:
                        continue

            if outcome:
                break
            await asyncio.sleep(5)

        # Диагностика: скриншот того, что реально видел бот в момент решения
        # (или после 40с, если так и не понял) — чтобы не гадать вслепую,
        # если статус снова определится неверно.
        try:
            await page.screenshot(path=os.path.join(BASE_DATA_DIR, f"last_outcome_{outcome or 'unknown'}.png"))
        except Exception:
            pass

        if outcome == "processing":
            return "processing", "⏳ Платёж в обработке."

        if outcome == "success":
            return "success", "✅ Платёж успешно проведён!"

        if outcome == "declined":
            return "declined", "❌ Платёж отклонён банком."

        if outcome == "3ds":
            # Домен банка мог сработать раньше, чем страница дорисовала кнопку —
            # даём ей до 10 сек показаться, прежде чем сдаваться.
            clicked = False
            for frame in page.frames:
                try:
                    await frame.wait_for_selector(cancel_selector, state="visible", timeout=10000)
                    btn = frame.locator(cancel_selector).first
                    await btn.click()
                    clicked = True
                    break
                except PlaywrightTimeoutError:
                    continue
                except Exception:
                    continue

            if clicked:
                await confirm_cancel_if_asked()
                return "3ds", "🚫 3DS платёж отменён"

            # Кнопку отмены не нашли — это ровно та ситуация, которая раньше
            # приводила к зависанию/некорректному состоянию платежа. Логируем
            # разметку страницы, чтобы точно подставить селектор кнопки.
            for frame in page.frames:
                try:
                    text = await frame.inner_text("body")
                    logger.info("3DS без кнопки отмены — фрейм %s: %s", frame.url, text[:400])
                except Exception:
                    logger.info("3DS без кнопки отмены — фрейм %s: не удалось прочитать текст", frame.url)
            try:
                await page.screenshot(path=os.path.join(BASE_DATA_DIR, "3ds_no_cancel_button.png"))
            except Exception:
                pass
            logger.warning("3DS обнаружен, но кнопку отмены найти не удалось — платёж остался незавершённым.")
            return "3ds", "⚠️ Обнаружена 3DS-страница банка, но кнопку «Отмена» найти не удалось — проверьте окно браузера вручную, платёж может остаться незавершённым."

        # Ни один из ожидаемых исходов не найден. На всякий случай проверяем,
        # не осталась ли где-то незакрытая кнопка "Отмена" (мало ли угадали
        # не весь текст 3DS, а только её) — лучше закрыть платёж чисто, чем
        # оставить Госуслуги в подвешенном состоянии.
        for frame in page.frames:
            try:
                btn = frame.locator(cancel_selector).first
                if await btn.count() > 0:
                    await btn.click()
                    await confirm_cancel_if_asked()
                    return "3ds", "🚫 3DS платёж отменён"
            except Exception:
                continue

        # Логируем реальную картину, чтобы точно подставить правильные
        # селекторы, а не гадать заново.
        try:
            logger.info("Итог платежа не распознан. Фреймов: %d", len(page.frames))
            for frame in page.frames:
                try:
                    text = await frame.inner_text("body")
                    logger.info("Фрейм %s — первые 400 символов: %s", frame.url, text[:400])
                except Exception:
                    logger.info("Фрейм %s — не удалось прочитать текст", frame.url)
        except Exception as diag_err:
            logger.warning("Не удалось собрать диагностику по итогу платежа: %s", diag_err)

        try:
            await page.screenshot(path=os.path.join(BASE_DATA_DIR, "payment_outcome_unknown.png"))
        except Exception:
            pass

        return "unknown", "⏳ Не удалось точно распознать результат платежа. Проверьте окно браузера — там видно, что произошло."

    @staticmethod
    def _mask_card(card_num):
        digits = re.sub(r"\D", "", card_num)
        return f"•••• {digits[-4:]}" if len(digits) >= 4 else "••••"

    async def pay_with_cards(self, card_pool, send_fn, page, is_fssp=False, amount_str=None,
                              total_cards=None, set_progress_fn=None, payment_url=None, resume=False,
                              uin=None, chat_id=None, history_page=None, known_history_ts=None,
                              history_lock=None, ambiguous_attribution=False, stop_event=None):
        """Тянет карты из ОБЩЕГО пула card_pool (список [(номер, срок, cvv), ...]),
        пока платёж не пройдёт успешно, или пул не опустеет. card_pool может быть
        одним и тем же списком, переданным нескольким параллельным вызовам этого
        метода (для одновременной оплаты нескольких штрафов одними картами) —
        каждая карта забирается из пула ОДИН раз (через .pop(0)), поэтому две
        параллельные оплаты никогда не возьмут одну и ту же карту одновременно.
        asyncio однопоточный, а между проверкой пула и .pop(0) нет await,
        поэтому гонки за карту здесь в принципе не возникает.
        send_fn — это send_fn(text, replace_last=False): по умолчанию добавляет
        новую короткую строку в статус-сообщение, а с replace_last=True
        обновляет последнюю (для промежуточного прогресса вроде подбора банка) —
        так весь ход оплаты умещается в одно редактируемое сообщение, без спама.
        Итоговый статус по карте — максимально короткий (эмодзи), без описания
        причины (она всё равно есть в логах консоли).
        page передаётся явно, чтобы разные вызовы работали на разных вкладках.
        total_cards — исходное количество карт в пуле (до всех .pop), нужно
        только для счётчика "N/M". set_progress_fn(current, total), если
        передан, вызывается перед каждой попыткой — им можно, например,
        обновить заголовок статус-сообщения ("💳 Оплата 2/3").
        payment_url — адрес чистой формы оплаты (с уже введённой суммой для
        ФССП), на которую нужно возвращаться перед каждой попыткой; если не
        передан, берётся текущий page.url в момент вызова. resume=True — этот
        вызов продолжает уже когда-то начатую оплату (после кнопки "Добавить
        ещё карты"), поэтому страница почти наверняка осталась на экране
        результата ПРЕДЫДУЩЕЙ попытки — значит, на payment_url нужно вернуться
        даже перед самой первой картой этого вызова, а не только со второй.
        uin — если передан (вместе с amount_str), при отказе/неясном статусе
        по тексту на форме оплаты бот дополнительно сверяется с 'Историей
        платежей' Госуслуг — она надёжнее угадывания текста на самой форме
        (см. check_payment_in_history). chat_id — если передан вместе с uin,
        при таком обнаружении бот шлёт отдельным НОВЫМ сообщением в чат (не
        строкой в статус-сообщение), что платёж найден в истории.
        history_page/known_history_ts/history_lock — если этот штраф проверяют
        НЕСКОЛЬКО вкладок параллельно (несколько карт на один штраф), их нужно
        передать ОБЩИМИ на все вызовы (готовит run_fine_payment) — иначе каждая
        вкладка по отдельности увидела бы один и тот же новый платёж (от той
        карты, что реально прошла) и обе засчитали бы его себе как успех,
        хотя платёж был один. Если не переданы — метод сам заводит вкладку и
        снимок "до" (для одиночного вызова этого достаточно).
        ambiguous_attribution=True — этот вызов один из НЕСКОЛЬКИХ параллельных
        для одного штрафа: 'История платежей' Госуслуг не показывает номер
        карты, только УИН/сумму/статус, поэтому при подтверждении через
        историю нельзя быть уверенным, что успех именно у ЭТОЙ карты, а не у
        другой, которая пробовалась параллельно — короткий статус и итоговое
        сообщение в этом случае формулируются с оговоркой, а не как точный
        факт.
        stop_event — общий на все параллельные вкладки одного штрафа
        asyncio.Event: как только ЛЮБАЯ из них подтверждает успех (по тексту
        формы или по истории), событие взводится, и остальные вкладки — как
        только освободятся между попытками — сразу останавливаются, не тратя
        оставшиеся карты на уже оплаченный штраф."""
        SHORT_STATUS = {
            "success": "✅",
            "processing": "✅",  # банк принял платёж — считаем успехом, не часиками
            "declined": "❌",
            "3ds": "❌",
            "unknown": "❓",
            "error": "⚠️",
        }

        if not page:
            await send_fn("⚠️ браузер не активен")
            return False

        payment_url = payment_url or page.url
        attempt = 0
        if total_cards is None:
            total_cards = len(card_pool)

        # "История платежей" — независимая от формы оплаты проверка (см.
        # докстринг). Если вызывающий код (несколько параллельных вкладок
        # одного штрафа) уже передал общую вкладку/снимок/лок — используем их;
        # иначе (одиночный вызов) заводим свои: открываем вкладку один раз и
        # запоминаем, что там уже было ДО первой попытки — иначе штраф,
        # оплаченный когда-то раньше с той же суммой, спутался бы с только
        # что прошедшим платежом.
        owns_history_tab = False
        if known_history_ts is None:
            known_history_ts = set()
        if history_lock is None:
            history_lock = asyncio.Lock()
        if uin and amount_str and history_page is None:
            try:
                history_page = await acquire_tab(self)
                known_history_ts = await self.known_history_timestamps(history_page, uin, amount_str)
                owns_history_tab = True
            except Exception as e:
                logger.warning("Не удалось подготовить проверку истории платежей: %s", e)
                history_page = None

        try:
            while True:
                # Соседняя вкладка этого же штрафа уже подтвердила успех, пока
                # эта вкладка обрабатывала свою предыдущую карту — штраф уже
                # оплачен, дальше пробовать нечего.
                if stop_event and stop_event.is_set():
                    return True

                if not card_pool:
                    await send_fn("🚫 карты закончились (жмите «Добавить ещё карты» выше)")
                    return False

                card_num, expiry, cvv = card_pool.pop(0)
                attempt += 1
                masked = self._mask_card(card_num)

                # card_pool общий на несколько параллельных оплат, поэтому текущий
                # номер попытки — это сколько карт всего уже разобрано из пула
                # (кем угодно), а не локальный счётчик именно этого вызова.
                if set_progress_fn:
                    try:
                        await set_progress_fn(total_cards - len(card_pool), total_cards)
                    except Exception:
                        pass

                if attempt > 1 or resume:
                    # После неудачной попытки страница могла остаться в непонятном
                    # состоянии (экран отказа, отменённый 3DS и т.п.) — перед
                    # следующей картой возвращаемся на чистую страницу оплаты.
                    # resume=True — тот же случай и для самой первой карты этого
                    # вызова: значит, это продолжение после "карты закончились",
                    # а страница всё ещё показывает результат предыдущей попытки.
                    try:
                        await page.goto(payment_url, wait_until="load")
                        await asyncio.sleep(2)
                    except Exception as e:
                        logger.warning("Не удалось перезагрузить страницу оплаты перед картой %s: %s", masked, e)

                    # У ФССП перезагрузка страницы сбрасывает частичную сумму обратно
                    # на полную — задаём её заново перед каждой новой картой, а не
                    # только один раз в самом начале.
                    if is_fssp and amount_str:
                        ok, fssp_msg = await self.set_partial_payment_amount(page, amount_str)
                        if ok:
                            logger.info("ФССП: сумма переустановлена перед картой %s: %s", masked, fssp_msg)
                        else:
                            logger.warning(
                                "Не удалось повторно задать сумму ФССП перед картой %s: %s", masked, fssp_msg
                            )
                            await send_fn(f"⚠️ не удалось выставить сумму {amount_str} ₽ повторно ({fssp_msg})")

                # key=masked — чтобы при нескольких вкладках одного штрафа, пишущих
                # в одно статус-сообщение параллельно, обновление статуса именно
                # ЭТОЙ карты не попало по ошибке в чужую (последнюю на тот момент)
                # строку другой карты, которую обрабатывает соседняя вкладка.
                await send_fn(f"{masked}: ⏳", masked)
                status, message = await self._submit_single_card(card_num, expiry, cvv, page, send_fn=send_fn)
                logger.info("Карта %s — статус %s: %s", masked, status, message)

                # Текст на самой форме оплаты не раз подводил (то дисклеймер
                # путался с отказом, то итог рисовался в iframe и вообще не
                # находился) — если по нему выходит отказ/неясно, на всякий
                # случай сверяемся с "Историей платежей" прежде чем сдаваться.
                found_by_history = False
                if history_page and status not in ("success", "processing"):
                    try:
                        # Лок — на случай нескольких вкладок ОДНОГО штрафа:
                        # без него обе могли бы независимо прочитать одну и ту
                        # же новую запись в истории и обе засчитать её себе.
                        # "Забираем" найденную запись (добавляем в общий
                        # known_history_ts) СРАЗУ под тем же локом — до await
                        # между проверкой и "захватом" нет, поэтому соседняя
                        # вкладка, дождавшись лока следующей, эту запись уже
                        # не увидит и не припишет себе повторно.
                        async with history_lock:
                            found, found_ts, _ = await self.check_payment_in_history(
                                history_page, uin, amount_str, exclude_timestamps=known_history_ts
                            )
                            if found:
                                known_history_ts.add(found_ts)
                    except Exception as e:
                        logger.warning("Ошибка проверки истории платежей: %s", e)
                        found = False
                    if found:
                        status = "success"
                        found_by_history = True
                        logger.info("Карта %s — отказ по форме, но найден новый платёж в истории (%s)", masked, found_ts)
                        if chat_id:
                            # При параллельных картах одного штрафа история не
                            # говорит, какая именно из них прошла — честно
                            # предупреждаем об этом, а не приписываем успех
                            # конкретной карте наугад.
                            caveat = (
                                "\n\n⚠️ Пробовалось несколько карт одновременно — по истории не видно, "
                                "какая именно, но штраф в любом случае оплачен."
                                if ambiguous_attribution else ""
                            )
                            try:
                                await bot.send_message(
                                    chat_id,
                                    "✅ Платёж найден!\n\n"
                                    f"УИН: {uin}\n"
                                    f"Сумма: {amount_str} ₽\n\n"
                                    f"Подтверждено по истории платежей Госуслуг.{caveat}",
                                )
                            except Exception as e:
                                logger.warning("Не удалось отправить сообщение о находке в истории: %s", e)

                short_label = SHORT_STATUS.get(status, "❓")
                if status == "success" and found_by_history and ambiguous_attribution:
                    short_label = "✅?"  # успех штрафа подтверждён, но не факт, что именно эта карта
                await send_fn(f"{masked}: {short_label}", masked)

                if status in ("success", "processing"):
                    # "В обработке" — банк уже принял платёж, следующую карту
                    # пробовать не нужно (это не отказ). Взводим общий
                    # stop_event — остальные параллельные вкладки этого же
                    # штрафа увидят его на следующей проверке в начале цикла
                    # и тоже остановятся, не тратя оставшиеся карты впустую.
                    if stop_event:
                        stop_event.set()
                    return True
        finally:
            # Общую (переданную извне) вкладку истории не закрываем сами —
            # ею распоряжается run_fine_payment, который её и открыл на весь
            # штраф целиком, а не на один этот вызов.
            if history_page and owns_history_tab:
                release_tab(self, history_page)

    async def close(self):
        self.logged_in = False
        # try/except на каждый шаг: /stop может вызвать close() ровно в момент,
        # когда где-то ещё идёт операция с этим же context/page — тогда Playwright
        # кинет ошибку о закрытом соединении. Это ожидаемо и не должно мешать
        # /stop гарантированно завершиться и освободить браузер.
        try:
            if self.context:
                await self.context.close()
        except Exception as e:
            logger.warning("Ошибка при закрытии context (не критично): %s", e)
        try:
            if self.playwright:
                await self.playwright.stop()
        except Exception as e:
            logger.warning("Ошибка при остановке playwright (не критично): %s", e)
        # Иначе _launch_browser() решит, что браузер всё ещё поднят
        # (self.page будет ссылаться на уже закрытую страницу), и не запустит его заново.
        self.page = None
        self.context = None
        self.playwright = None
        self.tab_pool = []  # старые вкладки закрылись вместе с context — пул больше не действителен


# Один общий браузер/логин на всех, кто пишет боту — на Госуслугах всё равно
# только один аккаунт и один профиль (USER_PROFILE_DIR общий на всех), поэтому
# отдельный GosuslugiBrowserClient на каждый chat_id только вредил: второй
# chat_id (или повторный /pay из того же чата) пытался поднять ещё один
# launch_persistent_context на уже занятом профиле и падал, открывая пустую
# вкладку. Теперь клиент один, а каждый /pay просто открывает СВОЮ отдельную
# вкладку в этом общем браузере — вкладки не пересекаются между вызовами.
shared_client = GosuslugiBrowserClient()


async def open_new_tab(client):
    """Открывает отдельную вкладку в уже поднятом общем браузере — используется
    вместо client.page, чтобы параллельные/повторные /pay не дрались за одну
    и ту же вкладку."""
    page = await client.context.new_page()
    await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    page.set_default_timeout(40000)
    return page


async def acquire_tab(client):
    """Берёт свободную вкладку из пула (оставшуюся от прошлого штрафа), а
    новую открывает только если свободных нет — чтобы вкладки не копились
    без переиспользования от /pay к /pay."""
    while client.tab_pool:
        page = client.tab_pool.pop()
        if not page.is_closed():
            return page
    return await open_new_tab(client)


def release_tab(client, page):
    """Возвращает вкладку в пул, чтобы следующий штраф использовал её вместо
    открытия новой. Вызывается, когда вкладка больше не нужна: штраф не
    найден, оплата прошла, или партия оплаты брошена ради нового /pay."""
    if page and not page.is_closed() and page not in client.tab_pool:
        client.tab_pool.append(page)


class StatusMessage:
    """Один пуш на весь процесс оплаты, который дальше только редактируется —
    вместо отдельного сообщения на каждую попытку/карту (было слишком много
    спама: одно сообщение на старт карты, другое на результат)."""

    def __init__(self, title, reply_markup=None):
        self._title = title
        self._lines = {}  # key -> текст строки; dict сохраняет порядок вставки
        self._auto_key = 0
        self._message = None
        self._reply_markup = reply_markup  # сохраняем, чтобы кнопка не пропадала при edit_text
        # Несколько вкладок ОДНОГО штрафа пишут в одно статус-сообщение
        # параллельно — без лока конкурентные edit_text могли завершиться не
        # в том порядке, в каком были вызваны, и на экране повисал устаревший
        # текст. Лок гарантирует, что каждый edit_text читает self._lines
        # заново, уже после всех более ранних вызовов.
        self._render_lock = asyncio.Lock()

    async def start(self, source_message):
        self._message = await source_message.answer(self._title, reply_markup=self._reply_markup)

    async def _render_and_edit(self):
        async with self._render_lock:
            body = self._title + ("\n" + "\n".join(self._lines.values()) if self._lines else "")
            try:
                await self._message.edit_text(body, reply_markup=self._reply_markup)
            except Exception:
                pass  # текст не изменился / временная ошибка редактирования — не критично

    async def push(self, text, key=None):
        """Добавляет строку. Если передан key и такой ключ уже был — строка с
        этим ключом обновляется НА МЕСТЕ (не в конце сообщения), иначе
        добавляется новая. key обязателен для прогресса ПО КОНКРЕТНОЙ карте
        (например, её маскированный номер) — иначе при нескольких вкладках
        одного штрафа, пишущих в одно сообщение параллельно, "обновление
        последней строки" могло попасть не в свою строку, а в чужую, которая
        случайно оказалась последней в этот момент."""
        if key is None:
            key = f"_auto{self._auto_key}"
            self._auto_key += 1
        self._lines[key] = text
        await self._render_and_edit()

    async def set_title(self, title):
        self._title = title
        await self._render_and_edit()


ADD_CARDS_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="➕ Добавить ещё карты", callback_data="addcards")]
])

FSSP_AMOUNT_KEYBOARD = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Оставить текущую сумму", callback_data="keepamount")]
])


async def prompt_fssp_amount(target_message, chat_id):
    """Спрашивает сумму к оплате для ТЕКУЩЕГО (первого в очереди) ФССП —
    с кнопкой 'Оставить текущую сумму', если сумма долга уже известна
    (найдена при поиске штрафа): для многих ФССП сумма долга и так совпадает
    с той, что нужно ввести (например, 750 ₽), и заново вручную вбивать её —
    лишнее действие."""
    fssp_queue = user_data.get(chat_id, {}).get("fssp_queue") or []
    if not fssp_queue:
        return
    _, uin, amount = fssp_queue[0]
    amount_line = f" (сумма долга: {amount} ₽)" if amount else ""
    await target_message.answer(
        f"Введите сумму к оплате для ФССП (УИН {uin}){amount_line}:",
        reply_markup=FSSP_AMOUNT_KEYBOARD if amount else None,
    )


async def apply_fssp_amount(target_message, chat_id, client, cleaned):
    """Общая логика после того, как сумма для текущего ФССП в очереди
    определена — введена вручную или взята по кнопке 'Оставить текущую
    сумму' — выставляет её на сайте и либо спрашивает сумму для следующего
    ФССП в очереди, либо переходит к вводу карт."""
    fssp_queue = user_data[chat_id].get("fssp_queue") or []
    if not fssp_queue:
        user_state[chat_id] = "waiting_card_info"
        return

    page, uin, _full_amount = fssp_queue.pop(0)
    await target_message.answer(f"⏳ Задаю сумму {cleaned} ₽ для ФССП (УИН {uin})...")
    ok, res_msg = await client.set_partial_payment_amount(page, cleaned)
    await target_message.answer(res_msg if ok else f"❌ {res_msg}")

    if ok:
        user_data[chat_id].setdefault("pending_fines", []).append((page, uin, True, cleaned))

    if fssp_queue:
        await prompt_fssp_amount(target_message, chat_id)
    else:
        user_data[chat_id].pop("fssp_queue", None)
        if user_data[chat_id].get("pending_fines"):
            user_state[chat_id] = "waiting_card_info"
        else:
            user_state[chat_id] = "ready_for_pay" if client.logged_in else "waiting_username"


def parse_cards(text):
    """Разбирает текст вида 'номер|дата|cvv' (можно несколько строк) в список
    карт. Возвращает (cards, error) — при ошибке формата cards is None и в
    error лежит готовое сообщение для пользователя."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    cards = []
    for line in lines:
        parts = line.split("|")
        if len(parts) < 3:
            return None, (
                f"❌ Неверный формат в строке «{line}». Нужно: номер|дата|cvv "
                "(можно несколько строк, по одной карте на строку)."
            )
        cards.append((parts[0].strip(), parts[1].strip(), parts[2].strip()))
    if not cards:
        return None, "❌ Не нашёл ни одной карты. Введите: номер|дата|cvv"
    return cards, None


async def run_fine_payment(client, batch, fine, chat_id):
    """Прогоняет карты из общего пула batch['cards'] на одном конкретном
    штрафе (fine) — вынесено из process_steps, чтобы им можно было запустить
    как исходную проверку, так и повторный проход после кнопки "Добавить ещё
    карты" уже после того, как первая партия карт закончилась.
    У штрафа может быть НЕСКОЛЬКО вкладок (fine['pages']) — по одной карте на
    вкладку одновременно, чтобы весь пул карт разбирался параллельно, а не по
    очереди в одной вкладке."""
    if fine["running"] or fine["resolved"]:
        return
    fine["running"] = True
    resume = fine["attempted"]  # это уже не первый заход на этот штраф —
    fine["attempted"] = True    # страница осталась на экране предыдущего результата

    # Проверка "Истории платежей" — ОДНА общая на весь штраф (вкладка, снимок
    # "до" и лок), а не своя у каждой карточной вкладки. У штрафа один и тот
    # же УИН+сумма на всех параллельных вкладках — если бы каждая вкладка
    # сама открывала историю и сверялась независимо, они обе увидели бы ОДИН
    # и тот же новый платёж (от той карты, которая реально прошла) и обе
    # засчитали бы его себе как успех, хотя платёж был один. known_history_ts
    # общий и мутируется под locком: как только одна вкладка "забрала" себе
    # найденную запись, другая её уже не увидит и не припишет себе повторно.
    history_page = None
    known_history_ts = set()
    history_lock = asyncio.Lock()
    if fine["uin"] and fine["amount_str"]:
        try:
            history_page = await acquire_tab(client)
            known_history_ts = await client.known_history_timestamps(history_page, fine["uin"], fine["amount_str"])
        except Exception as e:
            logger.warning("Не удалось подготовить проверку истории платежей для штрафа: %s", e)
            history_page = None

    try:
        async def set_progress(current, total):
            await fine["status_msg"].set_title(f"{fine['base_title']} {current}/{total}")

        # Больше одной вкладки на штраф — это несколько карт, пробуемых
        # параллельно ("турбо"-режим). История платежей не показывает номер
        # карты, поэтому в этом случае успех через историю нельзя точно
        # приписать конкретной карте — pay_with_cards формулирует это с
        # оговоркой, а не как точный факт (см. ambiguous_attribution).
        ambiguous_attribution = len(fine["pages"]) > 1
        # Общий на все вкладки этого штрафа — как только ОДНА из них
        # подтвердит успех, остальные увидят взведённый флаг и перестанут
        # пробовать оставшиеся карты из очереди (штраф уже оплачен).
        stop_event = asyncio.Event()

        async def run_on_page(page, payment_url):
            return await client.pay_with_cards(
                batch["cards"], fine["status_msg"].push, page,
                is_fssp=fine["is_fssp"], amount_str=fine["amount_str"],
                total_cards=batch["total_cards"], set_progress_fn=set_progress,
                payment_url=payment_url, resume=resume, uin=fine["uin"],
                chat_id=chat_id, history_page=history_page,
                known_history_ts=known_history_ts, history_lock=history_lock,
                ambiguous_attribution=ambiguous_attribution, stop_event=stop_event,
            )

        results = await asyncio.gather(
            *[run_on_page(p, u) for p, u in zip(fine["pages"], fine["payment_urls"])]
        )
        if any(results):
            fine["resolved"] = True
            # Вкладки этого штрафа больше не нужны — возвращаем в общий пул,
            # чтобы следующий штраф их переиспользовал вместо новых.
            for p in fine["pages"]:
                release_tab(client, p)
    finally:
        if history_page:
            release_tab(client, history_page)
        fine["running"] = False


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🚗 **Бот для Госуслуг**\n\n"
        "Команды:\n"
        "/login - Авторизоваться\n"
        "/pay - Проверить штраф по УИН\n"
        "/stop - Остановить текущий процесс (браузер остаётся открытым)\n"
        "/close - Закрыть все вкладки и сам браузер\n"
        "/totp <ключ> - Сохранить TOTP-ключ, чтобы /login сам вводил код из приложения"
    )


@dp.message(Command("totp"))
async def totp_cmd(message: Message):
    chat_id = message.chat.id
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Использование: /totp <секретный ключ>\n"
            "Это тот же ключ (base32), который вы бы вставили в Google Authenticator "
            "при включении входа по приложению на Госуслугах.\n"
            "После сохранения /login будет вводить код из приложения сам, без ручного ввода."
        )
        return

    secret = parts[1].strip().replace(" ", "")
    try:
        # Простая проверка, что это вообще валидный TOTP-секрет — сразу
        # пробуем сгенерировать код, а не ждём первого реального /login.
        pyotp.TOTP(secret).now()
    except Exception as e:
        await message.answer(f"❌ Похоже, ключ невалидный: {e}")
        return

    totp_secrets[str(chat_id)] = secret
    save_totp_secret(chat_id, secret)
    shared_client.totp_secret = secret

    await message.answer("✅ TOTP-ключ сохранён. Дальше /login будет вводить код из приложения автоматически.")


@dp.message(Command("login"))
async def login_cmd(message: Message):
    chat_id = message.chat.id
    user_state[chat_id] = "waiting_username"
    if shared_client.page:
        await shared_client.close()
    shared_client.totp_secret = totp_secrets.get(str(chat_id))
    user_data[chat_id] = {}
    await message.answer("Введите ваш логин от Госуслуг (телефон, почта или СНИЛС):")


@dp.message(Command("stop"))
async def stop_cmd(message: Message):
    chat_id = message.chat.id

    # Отменяем текущую задачу (поиск/оплата) прямо на месте — CancelledError
    # прервёт её на ближайшем await внутри Playwright-вызова. Браузер и сессию
    # входа НЕ трогаем — только останавливаем процесс, чтобы можно было сразу
    # начать заново без повторного /login.
    task = active_tasks.get(chat_id)
    was_running = bool(task and not task.done())
    if was_running:
        task.cancel()
    active_tasks.pop(chat_id, None)

    if chat_id in user_data:
        user_data[chat_id].pop("pending_fines", None)

    user_state[chat_id] = "ready_for_pay" if shared_client.logged_in else None

    if was_running:
        await message.answer("🛑 Процесс остановлен. Браузер и сессия входа не тронуты — можно продолжать через /pay.")
    else:
        await message.answer("🛑 Активных процессов не было.")


@dp.message(Command("close"))
async def close_cmd(message: Message):
    chat_id = message.chat.id

    # В отличие от /stop — закрывает сам браузер целиком со всеми вкладками
    # (и сбрасывает сессию входа), а не только останавливает текущий процесс.
    for task in list(active_tasks.values()):
        if task and not task.done():
            task.cancel()
    active_tasks.clear()

    await shared_client.close()

    for cid in list(user_data.keys()):
        user_data[cid].pop("pending_fines", None)
        user_data[cid].pop("fssp_queue", None)
        user_data[cid].pop("card_batch", None)
    for cid in list(user_state.keys()):
        user_state[cid] = None

    await message.answer("🧹 Браузер и все вкладки закрыты. Для продолжения — /login или /pay.")


@dp.message(Command("pay"))
async def pay_cmd(message: Message):
    chat_id = message.chat.id
    client = shared_client

    if not client.logged_in:
        # После перезапуска бота клиента в памяти нет, но браузерный профиль
        # на диске (USER_PROFILE_DIR) мог сохранить рабочую сессию Госуслуг —
        # проверяем её, вместо того чтобы сразу гнать пользователя на /login.
        await message.answer("⏳ Проверяю сохранённую сессию Госуслуг...")
        status, res_msg = await client.ensure_logged_in()
        if status != "already_logged_in":
            await message.answer(res_msg + "\n\nИспользуйте команду /login")
            return

    # Если от прошлого /pay остались недооплаченные штрафы (например, карты
    # кончились и добавить больше не прислали) — их вкладки больше никому не
    # принадлежат, возвращаем их в пул, а не бросаем висеть открытыми зря.
    old_batch = user_data.get(chat_id, {}).get("card_batch")
    if old_batch:
        for fine in old_batch["fines"]:
            if not fine["running"]:
                for p in fine["pages"]:
                    release_tab(client, p)

    user_data[chat_id] = {}
    user_state[chat_id] = "waiting_uin"
    await message.answer(
        "Пожалуйста, введите УИН штрафа (20 или 25 цифр).\n"
        "Можно сразу несколько УИН, каждый с новой строки (до 5 штрафов) — "
        "бот проверит и оплатит их параллельно, в отдельных вкладках одного браузера."
    )


@dp.callback_query(F.data == "addcards")
async def addcards_cb(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    batch = user_data.get(chat_id, {}).get("card_batch")
    if not batch or all(f["resolved"] for f in batch["fines"]):
        await callback.answer("Нечего продолжать — оплата уже завершена или ещё не запускалась.", show_alert=True)
        return
    user_state[chat_id] = "waiting_more_cards"
    await callback.answer()
    await callback.message.answer(
        "Пришлите ещё карты (можно несколько строк, номер|дата|cvv) — добавлю их в очередь."
    )


@dp.callback_query(F.data == "keepamount")
async def keepamount_cb(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    fssp_queue = user_data.get(chat_id, {}).get("fssp_queue") or []
    if not fssp_queue:
        await callback.answer("Нет активного запроса суммы ФССП.", show_alert=True)
        return
    _, uin, amount = fssp_queue[0]
    if not amount:
        await callback.answer("Сумма долга не определена — введите вручную.", show_alert=True)
        return
    await callback.answer()
    # Как и в process_steps — регистрируем задачу для /stop, не перезаписывая
    # уже отслеживаемую незавершённую (см. комментарий там же).
    existing_task = active_tasks.get(chat_id)
    if not existing_task or existing_task.done():
        active_tasks[chat_id] = asyncio.current_task()
    await apply_fssp_amount(callback.message, chat_id, shared_client, amount)


@dp.message(lambda message: message.text and not message.text.startswith("/"))
async def process_steps(message: Message):
    chat_id = message.chat.id
    # Регистрируем текущую задачу, чтобы /stop мог её отменить, даже если
    # это долгий поиск/оплата на нескольких вкладках. НЕ перезаписываем, если
    # для этого чата уже отслеживается ещё не завершённая задача — иначе
    # случайное лишнее сообщение (пришло не туда, задублировали карты и т.п.)
    # подменит собой отслеживаемую задачу на своё, которое почти сразу
    # завершится — и /stop после этого перестанет видеть реально идущую
    # оплату, решив, что активных процессов нет.
    existing_task = active_tasks.get(chat_id)
    if not existing_task or existing_task.done():
        active_tasks[chat_id] = asyncio.current_task()
    state = user_state.get(chat_id)
    client = shared_client

    if state == "waiting_username":
        user_data[chat_id]["username"] = message.text
        user_state[chat_id] = "waiting_password"
        await message.answer("Введите ваш пароль от Госуслуг:")
    elif state == "waiting_password":
        user_data[chat_id]["password"] = message.text
        user_state[chat_id] = "waiting_code"
        await message.answer("⏳ Запускаю автоматический браузер на ПК и ввожу ваши данные...")
        status, res_msg = await client.start_auth(user_data[chat_id]["username"], user_data[chat_id]["password"])
        await message.answer(res_msg)
        if status in ("already_logged_in", "auto_logged_in"):
            user_state[chat_id] = "ready_for_pay"
        elif status is False:
            user_state[chat_id] = None
    elif state == "waiting_code":
        await message.answer("⏳ Передаю код авторизации в открытое окно...")
        success, res_msg = await client.enter_sms_code(message.text.strip())
        await message.answer(res_msg)
        if success:
            user_state[chat_id] = "ready_for_pay"
        else:
            user_state[chat_id] = None
    elif state == "waiting_uin":
        # Можно ввести от одного до пяти УИН, каждый с новой строки — каждый
        # штраф обрабатывается на отдельной вкладке того же браузера (сессия
        # входа общая на весь контекст), параллельно с остальными.
        uins = [line.strip() for line in message.text.splitlines() if line.strip()]
        if not uins or any(not u.isdigit() or len(u) not in (20, 25) for u in uins):
            await message.answer(
                "❌ Неверный формат. Каждый УИН — 20 или 25 цифр, можно от одной до пяти строк:"
            )
            return
        if len(uins) > 5:
            await message.answer("❌ Пока можно проверить не больше 5 штрафов одновременно.")
            return

        await message.answer(f"⏳ Проверяю {len(uins)} штраф(ов){' параллельно' if len(uins) > 1 else ''}...")

        # Каждый УИН — на своей вкладке общего браузера (а не на client.page):
        # так повторный/параллельный /pay из этого же или другого чата не
        # пересекается с вкладками, уже занятыми другим вызовом. Вкладки берём
        # из общего пула (acquire_tab) — переиспользуем то, что осталось от
        # прошлых штрафов, вместо того чтобы плодить новые.
        pages = [await acquire_tab(client) for _ in uins]

        async def check_one(index, uin, page):
            success, res_msg, result_page, is_fssp, amount_str = await client.check_penalty_by_uin(uin, page)
            prefix = f"Штраф {index}/{len(uins)} (УИН {uin}): " if len(uins) > 1 else ""
            await message.answer(f"{prefix}{res_msg}")
            if not success:
                release_tab(client, result_page or page)
            return success, result_page, uin, is_fssp, amount_str

        results = await asyncio.gather(*[
            check_one(i + 1, uin, pages[i]) for i, uin in enumerate(uins)
        ])

        # Обычные штрафы идут сразу к вводу карты (сумма уже известна — нужна
        # позже для сверки с "Историей платежей"), а ФССП — сначала нужно
        # спросить сумму частичной оплаты и задать её на сайте.
        ready_fines = [
            (page, uin, False, amount_str)
            for success, page, uin, is_fssp, amount_str in results if success and not is_fssp
        ]
        fssp_queue = [
            (page, uin, amount_str)
            for success, page, uin, is_fssp, amount_str in results if success and is_fssp
        ]

        if not ready_fines and not fssp_queue:
            user_state[chat_id] = "ready_for_pay" if client and client.logged_in else "waiting_username"
            return

        user_data[chat_id]["pending_fines"] = ready_fines
        user_data[chat_id]["fssp_queue"] = fssp_queue
        user_state[chat_id] = "waiting_fssp_amount" if fssp_queue else "waiting_card_info"
        if fssp_queue:
            await prompt_fssp_amount(message, chat_id)
    elif state == "waiting_fssp_amount":
        cleaned = re.sub(r"[^\d.]", "", message.text.strip().replace(",", "."))
        if not cleaned:
            await message.answer("❌ Введите сумму числом, например: 2250")
            return
        await apply_fssp_amount(message, chat_id, client, cleaned)
    elif state == "waiting_card_info":
        # Можно ввести несколько карт — каждую с новой строки в формате номер|дата|cvv.
        # Бот пробует их по очереди и останавливается на первой успешной оплате.
        cards, error = parse_cards(message.text)
        if error:
            await message.answer(error)
            return

        pending_fines = user_data[chat_id].get("pending_fines")
        if not pending_fines:
            pending_fines = [(await acquire_tab(client), None, False, None)]
        multi = len(pending_fines) > 1

        async def prepare_replica_page(uin, is_fssp, amount_str):
            """Открывает ЕЩЁ ОДНУ вкладку на тот же самый штраф (заново ищет
            его по УИН) — используется, чтобы несколько карт одного штрафа
            пробовались параллельно, а не по очереди в одной вкладке."""
            tab = await acquire_tab(client)
            success, _, result_page, _, _ = await client.check_penalty_by_uin(uin, tab)
            if not success:
                release_tab(client, result_page or tab)
                return None
            if is_fssp and amount_str:
                ok_amt, _ = await client.set_partial_payment_amount(result_page, amount_str)
                if not ok_amt:
                    release_tab(client, result_page)
                    return None
            return result_page

        # "Общая очередь карт" — этот же список карт разбирают все штрафы
        # партии, и кнопка "Добавить ещё карты" дозаписывает сюда же новые
        # карты, даже если сама проверка к тому моменту уже закончилась.
        batch = {"cards": cards, "fines": [], "total_cards": len(cards)}
        for i, (page, uin, is_fssp, amount_str) in enumerate(pending_fines):
            base_title = (
                f"💳 Штраф {i + 1}/{len(pending_fines)}" + (f" (УИН {uin})" if uin else "")
                if multi else "💳 Оплата"
            )
            pages = [page]
            # Если штраф один и карт несколько — открываем этому же штрафу ещё
            # вкладок (до 5 всего), по числу карт, чтобы они пробовались
            # параллельно и оплата шла быстрее. При нескольких штрафах сразу
            # это не делаем — иначе вкладок стало бы слишком много разом.
            if not multi and uin and len(cards) > 1:
                replicas = await asyncio.gather(*[
                    prepare_replica_page(uin, is_fssp, amount_str)
                    for _ in range(min(len(cards), 5) - 1)
                ])
                pages.extend(p for p in replicas if p)

            status_msg = StatusMessage(base_title, reply_markup=ADD_CARDS_KEYBOARD)
            await status_msg.start(message)
            if len(pages) > 1:
                await status_msg.push(f"⚡ {len(pages)} вкладки параллельно")
            batch["fines"].append({
                "pages": pages, "uin": uin, "is_fssp": is_fssp, "amount_str": amount_str,
                "status_msg": status_msg, "base_title": base_title,
                # payment_urls — чистая форма оплаты ПРЯМО СЕЙЧАС (сумма ФССП,
                # если есть, уже выставлена), по одной на каждую вкладку — на
                # неё будем возвращаться перед каждой попыткой, включая самую
                # первую при повторном заходе через "Добавить ещё карты"
                # (см. resume в run_fine_payment).
                "payment_urls": [p.url for p in pages], "attempted": False,
                "running": False, "resolved": False,
            })
        user_data[chat_id]["card_batch"] = batch

        await asyncio.gather(*[run_fine_payment(client, batch, fine, chat_id) for fine in batch["fines"]])

        user_data[chat_id].pop("pending_fines", None)
        user_state[chat_id] = "ready_for_pay"
    elif state == "waiting_more_cards":
        # Пользователь прислал карты в ответ на кнопку "Добавить ещё карты" —
        # дописываем их в общий пул уже запущенной (или уже закончившейся)
        # партии оплаты и, если есть неоплаченные штрафы, пробуем снова.
        cards, error = parse_cards(message.text)
        if error:
            await message.answer(error)
            return

        batch = user_data.get(chat_id, {}).get("card_batch")
        if not batch:
            await message.answer("❌ Нет активной оплаты, к которой можно добавить карты.")
            user_state[chat_id] = "ready_for_pay" if client.logged_in else None
            return

        batch["cards"].extend(cards)
        batch["total_cards"] += len(cards)
        await message.answer(f"➕ Добавил {len(cards)} карт(у/ы) в очередь.")

        unresolved = [f for f in batch["fines"] if not f["resolved"] and not f["running"]]
        if unresolved:
            await asyncio.gather(*[run_fine_payment(client, batch, fine, chat_id) for fine in unresolved])

        user_state[chat_id] = "ready_for_pay" if client.logged_in else None


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit, asyncio.exceptions.CancelledError):
        pass
