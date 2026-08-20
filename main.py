import asyncio
import logging
import os
import re
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = "8839226959:AAEnAfN1Hs3OOqmCPui0sSaz6MjCNsrR6Rc"

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()

user_state = {}
user_data = {}
active_tasks = {}  # chat_id -> asyncio.Task текущего process_steps, чтобы /stop мог его отменить

BASE_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bot_data")
os.makedirs(BASE_DATA_DIR, exist_ok=True)
USER_PROFILE_DIR = os.path.join(BASE_DATA_DIR, "gosuslugi_profile")

# Страница прямого поиска и оплаты штрафа/квитанции по УИН.
QUITTANCE_URL = "https://www.gosuslugi.ru/pay/quittance"


class GosuslugiBrowserClient:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.logged_in = False

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

    async def ensure_logged_in(self):
        """Проверяет, действует ли уже сохранённая сессия Госуслуг, не запрашивая
        логин/пароль заново. Используется в /pay, чтобы не гонять пользователя
        через /login при каждом перезапуске бота, если сессия ещё жива."""
        try:
            await self._launch_browser()
            await self.page.goto("https://gosuslugi.ru", wait_until="domcontentloaded")
            await asyncio.sleep(2)

            if "login" not in self.page.url and await self.page.locator("input#login").count() == 0:
                self.logged_in = True
                return "already_logged_in", "✨ Сессия активна! Можно проверять штраф."

            self.logged_in = False
            return False, "❌ Сохранённая сессия не найдена или истекла."
        except Exception as e:
            await self.close()
            return False, f"Ошибка проверки сессии: {str(e)}"

    async def start_auth(self, username, password):
        try:
            await self._launch_browser()

            await self.page.goto("https://gosuslugi.ru", wait_until="domcontentloaded")
            await asyncio.sleep(2)

            if "login" not in self.page.url and await self.page.locator("input#login").count() == 0:
                self.logged_in = True
                return "already_logged_in", "✨ Сессия активна! Можно сразу проверять штраф через /pay."

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
            return True, "Данные заполнены! Введите СМС-код из телефона в чат бота:"
        except Exception as e:
            await self.close()
            return False, f"Ошибка при вводе данных: {str(e)}"

    async def enter_sms_code(self, code):
        try:
            if not self.page: return False, "Сессия не найдена. Начните сначала через /login."
            await self.page.wait_for_selector("input[type='tel']", timeout=15000)
            await self.page.fill("input[type='tel']", code)

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
                return False, "Вы не авторизованы. Введите /login.", page, False

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
                return False, "⚠️ Сессия слетела — сайт вернул вас на страницу входа. Авторизуйтесь заново через /login.", page, False

            uin_selector = (
                "input[name*='uin' i], input[id*='uin' i], "
                "input[placeholder*='УИН'], input[placeholder*='уин'], "
                "input[type='text']"
            )
            await page.wait_for_selector(uin_selector, state="visible", timeout=25000)
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
                    return False, "ℹ️ По этому УИН ничего не найдено — возможно, штраф уже оплачен или УИН введён неверно.", page, False
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
            # ФССП не распознавался. Явно ждём появления ссылки до 5 сек, а не
            # проверяем мгновенно.
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
                return True, (
                    f"✅ ФССП найдено!{amount_line} Доступна частичная оплата.\n"
                    "Введите сумму к оплате (числом, в рублях):"
                ), page, True

            amount_line = f" К оплате: {amount_str} ₽." if amount_str else ""
            return True, (
                f"✅ Штраф найден!{amount_line} Введите данные карты в формате: номер|дата|cvv\n"
                "Можно несколько карт — каждую с новой строки, бот будет пробовать их по очереди, "
                "пока платёж не пройдёт."
            ), page, False
        except Exception as e:
            try:
                if page:
                    await page.screenshot(path=os.path.join(BASE_DATA_DIR, "error_check_penalty.png"))
            except Exception:
                pass
            current_url = page.url if page else "?"
            return False, f"Ошибка поиска: {str(e)} (страница: {current_url})", page, False

    async def set_partial_payment_amount(self, page, amount_str):
        """На странице ФССП жмёт 'Оплатить частично', вводит сумму в открывшейся
        модалке и жмёт 'Сохранить'. Возвращает (успех, сообщение)."""
        try:
            partial_link_selector = "a:has-text('Оплатить частично'), button:has-text('Оплатить частично')"
            partial_link_count = await page.locator(partial_link_selector).count()
            logger.info("ФССП: ссылок 'Оплатить частично' найдено: %d", partial_link_count)
            if partial_link_count == 0:
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
                    await send_fn(f"{self._mask_card(card_num)}: ⏳ банк {attempt}/{max_attempts}", True)
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
            "*:has-text('Платеж не прошел')"
        )
        # "Платёж в обработке" — банк принял платёж, но ещё не подтвердил.
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
            # СНАЧАЛА проверяем однозначные текстовые статусы на самой странице
            # Госуслуг (обработка/отказ/успех) — они надёжнее эвристики "домен
            # похож на банк" ниже. Та эвристика однажды ложно сработала на уже
            # неактуальном/скрытом iframe от прошлого шага 3DS, из-за чего бот
            # пытался отменить платёж, который на самом деле просто "в обработке".
            try:
                if await page.locator(processing_selector).count() > 0:
                    outcome = "processing"
                elif await page.locator(declined_selector).count() > 0:
                    outcome = "declined"
                elif await page.locator(success_selector).count() > 0:
                    outcome = "success"
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
                              total_cards=None, set_progress_fn=None):
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
        обновить заголовок статус-сообщения ("💳 Оплата 2/3")."""
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

        payment_url = page.url
        attempt = 0
        if total_cards is None:
            total_cards = len(card_pool)

        while True:
            if not card_pool:
                await send_fn("🚫 карты закончились")
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

            if attempt > 1:
                # После неудачной попытки страница могла остаться в непонятном
                # состоянии (экран отказа, отменённый 3DS и т.п.) — перед
                # следующей картой возвращаемся на чистую страницу оплаты.
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

            await send_fn(f"{masked}: ⏳")
            status, message = await self._submit_single_card(card_num, expiry, cvv, page, send_fn=send_fn)
            logger.info("Карта %s — статус %s: %s", masked, status, message)
            await send_fn(f"{masked}: {SHORT_STATUS.get(status, '❓')}", True)

            if status in ("success", "processing"):
                # "В обработке" — банк уже принял платёж, следующую карту
                # пробовать не нужно (это не отказ).
                return True

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


class StatusMessage:
    """Один пуш на весь процесс оплаты, который дальше только редактируется —
    вместо отдельного сообщения на каждую попытку/карту (было слишком много
    спама: одно сообщение на старт карты, другое на результат)."""

    def __init__(self, title):
        self._title = title
        self._lines = []
        self._message = None

    async def start(self, source_message):
        self._message = await source_message.answer(self._title)

    async def _render_and_edit(self):
        body = self._title + ("\n" + "\n".join(self._lines) if self._lines else "")
        try:
            await self._message.edit_text(body)
        except Exception:
            pass  # текст не изменился / временная ошибка редактирования — не критично

    async def push(self, text, replace_last=False):
        if replace_last and self._lines:
            self._lines[-1] = text
        else:
            self._lines.append(text)
        await self._render_and_edit()

    async def set_title(self, title):
        self._title = title
        await self._render_and_edit()


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "🚗 **Бот для Госуслуг**\n\n"
        "Команды:\n"
        "/login - Авторизоваться\n"
        "/pay - Проверить штраф по УИН\n"
        "/stop - Остановить текущий процесс (браузер остаётся открытым)"
    )


@dp.message(Command("login"))
async def login_cmd(message: Message):
    chat_id = message.chat.id
    user_state[chat_id] = "waiting_username"
    if chat_id in user_data and "client" in user_data[chat_id]:
        await user_data[chat_id]["client"].close()
    user_data[chat_id] = {"client": GosuslugiBrowserClient()}
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

    client = user_data.get(chat_id, {}).get("client")
    user_state[chat_id] = "ready_for_pay" if client and client.logged_in else None

    if was_running:
        await message.answer("🛑 Процесс остановлен. Браузер и сессия входа не тронуты — можно продолжать через /pay.")
    else:
        await message.answer("🛑 Активных процессов не было.")


@dp.message(Command("pay"))
async def pay_cmd(message: Message):
    chat_id = message.chat.id
    client = user_data.get(chat_id, {}).get("client")

    if not client:
        # После перезапуска бота клиента в памяти нет, но браузерный профиль
        # на диске (USER_PROFILE_DIR) мог сохранить рабочую сессию Госуслуг —
        # проверяем её, вместо того чтобы сразу гнать пользователя на /login.
        client = GosuslugiBrowserClient()
        user_data[chat_id] = {"client": client}

    if not client.logged_in:
        await message.answer("⏳ Проверяю сохранённую сессию Госуслуг...")
        status, res_msg = await client.ensure_logged_in()
        if status != "already_logged_in":
            await message.answer(res_msg + "\n\nИспользуйте команду /login")
            return

    user_state[chat_id] = "waiting_uin"
    await message.answer(
        "Пожалуйста, введите УИН штрафа (20 или 25 цифр).\n"
        "Можно сразу два УИН, каждый с новой строки — бот проверит и оплатит "
        "их параллельно, в двух вкладках одного браузера."
    )


@dp.message(lambda message: message.text and not message.text.startswith("/"))
async def process_steps(message: Message):
    chat_id = message.chat.id
    # Регистрируем текущую задачу, чтобы /stop мог её отменить, даже если
    # это долгий поиск/оплата на нескольких вкладках.
    active_tasks[chat_id] = asyncio.current_task()
    state = user_state.get(chat_id)
    client = user_data.get(chat_id, {}).get("client")

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
        if status == "already_logged_in":
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
        # Можно ввести один или два УИН, каждый с новой строки — второй штраф
        # обрабатывается на отдельной вкладке того же браузера (сессия входа
        # общая на весь контекст), параллельно с первым.
        uins = [line.strip() for line in message.text.splitlines() if line.strip()]
        if not uins or any(not u.isdigit() or len(u) not in (20, 25) for u in uins):
            await message.answer(
                "❌ Неверный формат. Каждый УИН — 20 или 25 цифр, можно одну или две строки:"
            )
            return
        if len(uins) > 2:
            await message.answer("❌ Пока можно проверить не больше 2 штрафов одновременно.")
            return

        await message.answer(f"⏳ Проверяю {len(uins)} штраф(ов){' параллельно' if len(uins) > 1 else ''}...")

        # Первый УИН — на уже открытой вкладке клиента, для второго открываем
        # новую вкладку в том же контексте (куки/сессия входа общие).
        pages = [client.page]
        for _ in range(len(uins) - 1):
            new_tab = await client.context.new_page()
            await new_tab.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            new_tab.set_default_timeout(40000)
            pages.append(new_tab)

        async def check_one(index, uin, page):
            success, res_msg, result_page, is_fssp = await client.check_penalty_by_uin(uin, page)
            prefix = f"Штраф {index}/{len(uins)} (УИН {uin}): " if len(uins) > 1 else ""
            await message.answer(f"{prefix}{res_msg}")
            return success, result_page, uin, is_fssp

        results = await asyncio.gather(*[
            check_one(i + 1, uin, pages[i]) for i, uin in enumerate(uins)
        ])

        # Обычные штрафы идут сразу к вводу карты, а ФССП — сначала нужно
        # спросить сумму частичной оплаты и задать её на сайте.
        ready_fines = [(page, uin, False, None) for success, page, uin, is_fssp in results if success and not is_fssp]
        fssp_queue = [(page, uin) for success, page, uin, is_fssp in results if success and is_fssp]

        if not ready_fines and not fssp_queue:
            user_state[chat_id] = "ready_for_pay" if client and client.logged_in else "waiting_username"
            return

        user_data[chat_id]["pending_fines"] = ready_fines
        user_data[chat_id]["fssp_queue"] = fssp_queue
        user_state[chat_id] = "waiting_fssp_amount" if fssp_queue else "waiting_card_info"
    elif state == "waiting_fssp_amount":
        cleaned = re.sub(r"[^\d.]", "", message.text.strip().replace(",", "."))
        if not cleaned:
            await message.answer("❌ Введите сумму числом, например: 2250")
            return

        fssp_queue = user_data[chat_id].get("fssp_queue") or []
        if not fssp_queue:
            user_state[chat_id] = "waiting_card_info"
            return

        page, uin = fssp_queue.pop(0)
        await message.answer(f"⏳ Задаю сумму {cleaned} ₽ для ФССП (УИН {uin})...")
        ok, res_msg = await client.set_partial_payment_amount(page, cleaned)
        await message.answer(res_msg if ok else f"❌ {res_msg}")

        if ok:
            user_data[chat_id].setdefault("pending_fines", []).append((page, uin, True, cleaned))

        if fssp_queue:
            next_uin = fssp_queue[0][1]
            await message.answer(f"Введите сумму к оплате для следующего ФССП (УИН {next_uin}):")
        else:
            user_data[chat_id].pop("fssp_queue", None)
            if user_data[chat_id].get("pending_fines"):
                user_state[chat_id] = "waiting_card_info"
            else:
                user_state[chat_id] = "ready_for_pay" if client and client.logged_in else "waiting_username"
    elif state == "waiting_card_info":
        # Можно ввести несколько карт — каждую с новой строки в формате номер|дата|cvv.
        # Бот пробует их по очереди и останавливается на первой успешной оплате.
        lines = [line.strip() for line in message.text.splitlines() if line.strip()]
        cards = []
        for line in lines:
            parts = line.split("|")
            if len(parts) < 3:
                await message.answer(
                    f"❌ Неверный формат в строке «{line}». Нужно: номер|дата|cvv "
                    "(можно несколько строк, по одной карте на строку)."
                )
                return
            cards.append((parts[0].strip(), parts[1].strip(), parts[2].strip()))

        if not cards:
            await message.answer("❌ Не нашёл ни одной карты. Введите: номер|дата|cvv")
            return

        pending_fines = user_data[chat_id].get("pending_fines") or [(client.page, None, False, None)]
        multi = len(pending_fines) > 1
        total_cards_count = len(cards)  # фиксируем ДО того, как пул начнут разбирать

        async def pay_one(index, page, uin, is_fssp, amount_str):
            base_title = (
                f"💳 Штраф {index}/{len(pending_fines)}" + (f" (УИН {uin})" if uin else "")
                if multi else "💳 Оплата"
            )
            status_msg = StatusMessage(base_title)
            await status_msg.start(message)

            async def send(text, replace_last=False):
                await status_msg.push(text, replace_last)

            async def set_progress(current, total):
                await status_msg.set_title(f"{base_title} {current}/{total}")

            await client.pay_with_cards(
                cards, send, page, is_fssp=is_fssp, amount_str=amount_str,
                total_cards=total_cards_count, set_progress_fn=set_progress,
            )

        await asyncio.gather(*[
            pay_one(i + 1, page, uin, is_fssp, amount_str)
            for i, (page, uin, is_fssp, amount_str) in enumerate(pending_fines)
        ])

        user_data[chat_id].pop("pending_fines", None)
        user_state[chat_id] = "ready_for_pay"


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit, asyncio.exceptions.CancelledError):
        pass
