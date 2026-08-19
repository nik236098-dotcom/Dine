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

    async def check_penalty_by_uin(self, uin):
        try:
            if not self.page or not self.logged_in:
                return False, "Вы не авторизованы. Введите /login."

            # БАГ БЫЛ ЗДЕСЬ: раньше переход выполнялся на "https://gosuslugi.ru" (главная),
            # а не на страницу поиска квитанций — на главной нет поля ввода УИН,
            # поэтому wait_for_selector ниже всегда падал по таймауту.
            logger.info("Выполняю переход на %s", QUITTANCE_URL)
            await self.page.goto(QUITTANCE_URL, wait_until="load")

            # Если сессия истекла или сайт заподозрил автоматизацию — нас могут
            # редиректнуть обратно на страницу логина. Проверяем это явно,
            # вместо того чтобы падать в неясный Timeout.
            await asyncio.sleep(2)
            if "login" in self.page.url or "esia" in self.page.url:
                self.logged_in = False
                return False, "⚠️ Сессия слетела — сайт вернул вас на страницу входа. Авторизуйтесь заново через /login."

            uin_selector = (
                "input[name*='uin' i], input[id*='uin' i], "
                "input[placeholder*='УИН'], input[placeholder*='уин'], "
                "input[type='text']"
            )
            await self.page.wait_for_selector(uin_selector, state="visible", timeout=25000)
            uin_input = self.page.locator(uin_selector).first

            await uin_input.click()
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Delete")
            await uin_input.fill(uin)
            await asyncio.sleep(1)

            button = self.page.locator("button[type='submit'], button:has-text('Найти'), button:has-text('Проверить')").first
            await button.click()

            # Снимок сразу после клика — чтобы видеть, что произошло на странице,
            # даже если поиск в итоге зависнет и упадёт по таймауту ниже.
            try:
                await asyncio.sleep(1)
                await self.page.screenshot(path=os.path.join(BASE_DATA_DIR, "after_search_click.png"))
            except Exception:
                pass

            # Даём странице время подгрузить результат поиска (это AJAX/React,
            # а не обычная навигация) прежде чем искать сумму.
            try:
                await self.page.wait_for_load_state("networkidle", timeout=8000)
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

            logger.info("Всего фреймов на странице: %d", len(self.page.frames))

            found = False
            for frame in self.page.frames:
                try:
                    await frame.wait_for_selector(result_selector, state="visible", timeout=30000)
                    found = True
                    break
                except PlaywrightTimeoutError:
                    continue

            if not found:
                # Точная диагностика: что реально видел Playwright в момент неудачи.
                try:
                    body_text = await self.page.inner_text("body")
                    logger.info("Длина текста body: %d символов", len(body_text))
                    logger.info("Вхождений 'Найдено' в тексте: %d", body_text.count("Найдено"))
                    logger.info("Первые 500 символов текста страницы: %s", body_text[:500])
                except Exception as diag_err:
                    logger.warning("Не удалось прочитать текст страницы для диагностики: %s", diag_err)

                if await self.page.locator(not_found_selector).count() > 0:
                    return False, "ℹ️ По этому УИН ничего не найдено — возможно, штраф уже оплачен или УИН введён неверно."
                raise PlaywrightTimeoutError("сумма штрафа не появилась ни на странице, ни во фреймах")

            # Достаём сумму штрафа для вывода в сообщении пользователю.
            # Внимание: символ рубля в тексте страницы — это буква "Р", не "₽".
            amount_str = None
            try:
                body_text = await self.page.inner_text("body")
                amount_match = re.search(r"(\d[\d \s]*\d)\s*Р(?![а-яёА-ЯЁ])", body_text)
                if amount_match:
                    amount_str = amount_match.group(1).replace(" ", " ").strip()
            except Exception:
                pass

            pay_button = self.page.locator("button:has-text('Оплатить'), button:has-text('Перейти к оплате')").first
            await pay_button.wait_for(state="visible", timeout=15000)

            # На Госуслугах оплата часто открывается в НОВОЙ вкладке (popup),
            # а не на текущей странице. Если это так — переключаемся на неё,
            # иначе следующий шаг (ввод карты) будет искать поля не там.
            try:
                async with self.context.expect_page(timeout=6000) as new_page_info:
                    await pay_button.click()
                new_page = await new_page_info.value
                await new_page.wait_for_load_state("domcontentloaded")
                self.page = new_page
                logger.info("Оплата открылась в новой вкладке: %s", new_page.url)
            except PlaywrightTimeoutError:
                # Новая вкладка не появилась — форма оплаты, скорее всего, на этой же странице
                await asyncio.sleep(4)

            amount_line = f" К оплате: {amount_str} ₽." if amount_str else ""
            return True, f"✅ Штраф найден!{amount_line} Введите данные карты в формате: номер|дата|cvv"
        except Exception as e:
            try:
                if self.page:
                    await self.page.screenshot(path=os.path.join(BASE_DATA_DIR, "error_check_penalty.png"))
            except Exception:
                pass
            current_url = self.page.url if self.page else "?"
            return False, f"Ошибка поиска: {str(e)} (страница: {current_url})"

    async def pay_by_card_auto(self, card_num, expiry, cvv):
        try:
            if not self.page: return False, "Браузер не активен."

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
            for frame in self.page.frames:
                try:
                    await frame.wait_for_selector(card_selector, state="visible", timeout=8000)
                    target = frame
                    break
                except PlaywrightTimeoutError:
                    continue

            if target is None:
                # Диагностика: логируем реальные атрибуты всех input на странице
                # и во всех фреймах, чтобы не гадать с селекторами вслепую.
                for frame in self.page.frames:
                    try:
                        inputs_info = await frame.eval_on_selector_all(
                            "input",
                            "els => els.map(e => ({name:e.name, id:e.id, placeholder:e.placeholder, "
                            "type:e.type, autocomplete:e.autocomplete}))"
                        )
                        logger.info("Фрейм %s — input'ы: %s", frame.url, inputs_info)
                    except Exception as diag_err:
                        logger.warning("Не удалось прочитать input'ы фрейма %s: %s", frame.url, diag_err)
                raise PlaywrightTimeoutError("поле номера карты не найдено ни на странице, ни во фреймах")

            expiry_selector = (
                "input[autocomplete*='cc-exp' i], input[placeholder*='ММ' i], "
                "input[placeholder*='MM' i], input[placeholder*='срок' i], input[name*='exp' i]"
            )
            cvv_selector = (
                "input[autocomplete*='cc-csc' i], input[placeholder*='CVV' i], "
                "input[placeholder*='CVC' i], input[name*='cvv' i], input[name*='cvc' i]"
            )

            await target.fill(card_selector, card_num)
            await target.fill(expiry_selector, expiry)
            await target.fill(cvv_selector, cvv)
            await target.click("button:has-text('Оплатить'), button[type='submit']")
            return True, "⏳ Данные карты успешно введены! Завершите платеж по СМС в окне на вашем ПК."
        except Exception as e:
            try:
                if self.page:
                    await self.page.screenshot(path=os.path.join(BASE_DATA_DIR, "error_pay_by_card.png"))
            except Exception:
                pass
            current_url = self.page.url if self.page else "?"
            return False, f"Не удалось автоматически заполнить карту: {str(e)} (страница: {current_url})"

    async def close(self):
        self.logged_in = False
        if self.context: await self.context.close()
        if self.playwright: await self.playwright.stop()
        # Иначе _launch_browser() решит, что браузер всё ещё поднят
        # (self.page будет ссылаться на уже закрытую страницу), и не запустит его заново.
        self.page = None
        self.context = None
        self.playwright = None


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer("🚗 **Бот для Госуслуг**\n\nКоманды:\n/login - Авторизоваться\n/pay - Проверить штраф по УИН")


@dp.message(Command("login"))
async def login_cmd(message: Message):
    chat_id = message.chat.id
    user_state[chat_id] = "waiting_username"
    if chat_id in user_data and "client" in user_data[chat_id]:
        await user_data[chat_id]["client"].close()
    user_data[chat_id] = {"client": GosuslugiBrowserClient()}
    await message.answer("Введите ваш логин от Госуслуг (телефон, почта или СНИЛС):")


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
    await message.answer("Пожалуйста, введите УИН штрафа (20 или 25 цифр):")


@dp.message(lambda message: message.text and not message.text.startswith("/"))
async def process_steps(message: Message):
    chat_id = message.chat.id
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
        if not message.text.strip().isdigit() or len(message.text.strip()) not in (20, 25):
            await message.answer("❌ Неверный формат УИН. Попробуйте еще раз:")
            return
        await message.answer("⏳ Выполняю переход по прямой ссылке на страницу квитанций...")
        success, res_msg = await client.check_penalty_by_uin(message.text.strip())
        await message.answer(res_msg)
        if success:
            user_state[chat_id] = "waiting_card_info"
        else:
            user_state[chat_id] = "ready_for_pay" if client and client.logged_in else "waiting_username"
    elif state == "waiting_card_info":
        parts = message.text.split("|")
        if len(parts) < 3:
            await message.answer("❌ Неверный формат. Введите строго через вертикальную черту:\nномер|дата|cvv")
            return
        await message.answer("⏳ Автоматически заполняю реквизиты карты на сайте...")
        success, res_msg = await client.pay_by_card_auto(parts[0].strip(), parts[1].strip(), parts[2].strip())
        await message.answer(res_msg)
        user_state[chat_id] = "ready_for_pay"


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit, asyncio.exceptions.CancelledError):
        pass
