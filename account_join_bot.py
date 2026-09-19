#!/usr/bin/env python3
"""Telegram join manager. Install: pip install Telethon==1.45.0
Run: python account_join_bot.py. Credentials and sessions stay outside the repo.
"""
import asyncio
import contextlib
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import time

from telethon import TelegramClient, events, Button, functions, types, utils, errors


def save_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    tmp.chmod(0o600)
    tmp.replace(path)


class Manager:
    def __init__(self, user, bot, owner, state_path):
        self.user, self.bot, self.owner = user, bot, owner
        self.state_path = state_path
        self.auto = set(json.loads(state_path.read_text()) if state_path.exists() else [])
        self.channels = {}
        self.confirmations = {}
        self.lock = asyncio.Lock()
        self.pause_until = 0
        self.last_error = {}

    def save(self):
        save_json(self.state_path, sorted(self.auto))

    async def refresh(self):
        channels = {}
        async for dialog in self.user.iter_dialogs():
            entity = dialog.entity
            rights = getattr(entity, 'admin_rights', None)
            if isinstance(entity, types.Channel) and (entity.creator or (rights and rights.invite_users)):
                channels[utils.get_peer_id(entity)] = entity
        self.channels = channels

    async def checked(self, key):
        if key not in self.channels:
            raise ValueError('Канал не найден. Обнови список каналов.')
        entity = await self.user.get_entity(self.channels[key])
        rights = getattr(entity, 'admin_rights', None)
        if not (entity.creator or (rights and rights.invite_users)):
            raise ValueError('У аккаунта больше нет права принимать заявки в этом канале.')
        return entity

    async def count(self, entity):
        result = await self.user(functions.messages.GetChatInviteImportersRequest(
            peer=entity, requested=True, offset_date=0,
            offset_user=types.InputUserEmpty(), limit=1))
        return result.count

    @staticmethod
    def btn(text, data):
        return Button.inline(text, data.encode())

    async def show(self, event, text, buttons):
        if isinstance(event, events.CallbackQuery.Event):
            try:
                return await event.edit(text, buttons=buttons, parse_mode=None)
            except errors.MessageNotModifiedError:
                return
        return await event.respond(text, buttons=buttons, parse_mode=None)

    async def home(self, event, page=0, refresh=False):
        if refresh or not self.channels:
            await self.refresh()
        entries = sorted(self.channels.items(), key=lambda x: x[1].title)
        page = max(0, min(page, max(0, (len(entries)-1)//8)))
        buttons = [[self.btn(('🟢 ' if key in self.auto else '📣 ') + entity.title[:45], f'view:{key}')]
                   for key, entity in entries[page*8:page*8+8]]
        nav = []
        if page:
            nav.append(self.btn('←', f'page:{page-1}'))
        if (page+1)*8 < len(entries):
            nav.append(self.btn('→', f'page:{page+1}'))
        if nav:
            buttons.append(nav)
        buttons.append([self.btn('🔄 Обновить список', 'refresh')])
        await self.show(event, '📋 Заявки на вступление\n\nВыбери канал или группу. Здесь видны каналы твоего аккаунта, где ты можешь приглашать пользователей.\n\nСтарые заявки доступны. 🟢 — включён автоприём.', buttons)

    async def panel(self, event, key, notice=''):
        entity = await self.checked(key)
        count = await self.count(entity)
        buttons = [[self.btn('✅ Принять все заявки', f'ask:{key}')],
                   [self.btn('🔴 Выключить автоприём' if key in self.auto else '🟢 Включить автоприём', f'off:{key}' if key in self.auto else f'autoask:{key}')],
                   [self.btn('🔄 Обновить', f'view:{key}'), self.btn('← Каналы', 'page:0')]]
        text = f'📣 {entity.title}\nID: {key}\n\nОжидают принятия: {count}\nАвтоприём: {"включён" if key in self.auto else "выключен"}'
        if self.last_error.get(key):
            text += '\n\nПоследняя ошибка: ' + self.last_error[key]
        if notice:
            text += '\n\n' + notice
        await self.show(event, text, buttons)

    async def approve(self, key):
        if time.time() < self.pause_until:
            raise ValueError(f'Telegram ограничил частоту действий. Подожди {int(self.pause_until-time.time())+1} сек.')
        entity = await self.checked(key)
        await self.user(functions.messages.HideAllChatJoinRequestsRequest(peer=entity, approved=True))
        self.last_error.pop(key, None)

    async def handle(self, event):
        # Every action is restricted to the logged-in account in a private chat.
        if event.sender_id != self.owner or not event.is_private:
            if isinstance(event, events.CallbackQuery.Event):
                await event.answer('Доступ только владельцу.', alert=True)
            return
        if isinstance(event, events.CallbackQuery.Event):
            await event.answer()
        async with self.lock:
            try:
                await self.dispatch(event)
            except errors.FloodWaitError as error:
                self.pause_until = time.time() + error.seconds
                await self.show(event, f'Telegram просит подождать {error.seconds} сек. Затем повтори действие.', [[self.btn('← Каналы', 'page:0')]])
            except (errors.RPCError, ValueError) as error:
                message = str(error) if isinstance(error, ValueError) else type(error).__name__
                await self.show(event, 'Не удалось выполнить действие: ' + message + '\nПроверь права аккаунта и обнови список.', [[self.btn('← Каналы', 'refresh')]])
            except (OSError, asyncio.TimeoutError):
                await self.show(event, 'Нет ответа Telegram. Результат операции неизвестен: обнови количество заявок перед повтором.', [[self.btn('← Каналы', 'refresh')]])

    async def dispatch(self, event):
        if not isinstance(event, events.CallbackQuery.Event):
            return await self.home(event, refresh=True)
        parts = event.data.decode().split(':')
        action = parts[0]
        if action == 'refresh':
            return await self.home(event, refresh=True)
        if action == 'page':
            return await self.home(event, int(parts[1]))
        if action == 'confirm':
            item = self.confirmations.pop(parts[1], None)
            if not item or time.time() - item[2] > 300:
                raise ValueError('Кнопка устарела. Открой канал и повтори действие.')
            key, mode, _ = item
            await self.checked(key)
            if mode == 'auto':
                self.auto.add(key)
                self.save()
                return await self.panel(event, key, 'Автоприём включён: старые и новые заявки будут приниматься при проверках примерно раз в 30 секунд.')
            await self.approve(key)
            return await self.panel(event, key, '✅ Telegram выполнил команду принятия всех заявок. Выше — актуальный остаток.')
        key = int(parts[1])
        if action in ('ask', 'autoask'):
            entity = await self.checked(key)
            count = await self.count(entity)
            self.confirmations = {k:v for k,v in self.confirmations.items() if time.time()-v[2] <= 300}
            nonce = secrets.token_hex(8)
            self.confirmations[nonce] = (key, 'auto' if action == 'autoask' else 'once', time.time())
            text = f'📣 {entity.title}\nID: {key}\nСейчас заявок: {count}\n\n'
            text += ('Включить постоянный автоприём всех старых и новых заявок?' if action == 'autoask' else 'Принять все заявки этого канала? Будут приняты все, ожидающие на момент выполнения команды.')
            return await self.show(event, text, [[self.btn('✅ Подтвердить', f'confirm:{nonce}')], [self.btn('← Назад', f'view:{key}')]])
        if action == 'off':
            self.auto.discard(key)
            self.save()
        await self.panel(event, key)

    async def worker(self):
        while True:
            await asyncio.sleep(30)
            for key in list(self.auto):
                async with self.lock:
                    if key not in self.auto or time.time() < self.pause_until:
                        continue
                    try:
                        entity = await self.checked(key)
                        if await self.count(entity):
                            await self.approve(key)
                    except errors.FloodWaitError as error:
                        self.pause_until = time.time()+error.seconds
                        self.last_error[key] = f'Лимит Telegram: ожидание {error.seconds} сек.'
                    except (errors.RPCError, ValueError, OSError, asyncio.TimeoutError) as error:
                        self.last_error[key] = type(error).__name__
                        if isinstance(error, (ValueError, errors.ChatAdminRequiredError, errors.ChannelPrivateError)):
                            self.auto.discard(key)
                            self.save()


async def main():
    os.umask(0o077)
    root = Path.home() / '.local/share/dine-join-manager'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    # Linux lock prevents two processes from sharing the same account session.
    import fcntl
    lock_file = (root / 'process.lock').open('w')
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Эта программа уже запущена. Сначала останови её вторую копию.')
    config_path = root / 'config.json'
    config = json.loads(config_path.read_text()) if config_path.exists() else {}
    if not config:
        api_id = int(input('API ID с my.telegram.org: ').strip())
        api_hash = getpass.getpass('API HASH (ввод скрыт): ').strip()
        token = getpass.getpass('Токен бота BotFather (ввод скрыт): ').strip()
        if not re.fullmatch(r'[a-fA-F0-9]{32}', api_hash) or not re.fullmatch(r'\d+:[\w-]+', token):
            raise SystemExit('Неверный формат API HASH или токена. Запусти снова.')
        config = dict(api_id=api_id, api_hash=api_hash, token=token)
    user = TelegramClient(str(root/'account'), config['api_id'], config['api_hash'], flood_sleep_threshold=0)
    bot = TelegramClient(str(root/'bot'), config['api_id'], config['api_hash'], flood_sleep_threshold=0)
    task = None
    try:
        await user.start(phone=lambda: input('Твой номер телефона с +кодом страны: ').strip(),
                         code_callback=lambda: getpass.getpass('Код входа из Telegram (ввод скрыт): ').strip(),
                         password=lambda: getpass.getpass('Пароль двухэтапной защиты: '))
        me = await user.get_me()
        if me.bot:
            raise SystemExit('Нужен личный аккаунт, а не бот.')
        await bot.start(bot_token=config['token'])
        bot_me = await bot.get_me()
        if not bot_me.bot or bot_me.id != int(config['token'].split(':')[0]):
            raise SystemExit('Сессия бота не соответствует токену.')
        save_json(config_path, config)
        manager = Manager(user, bot, me.id, root/f'auto_{me.id}.json')
        await manager.refresh()
        bot.add_event_handler(manager.handle, events.NewMessage(incoming=True))
        bot.add_event_handler(manager.handle, events.CallbackQuery())
        task = asyncio.create_task(manager.worker())
        print(f'Готово! Открой @{bot_me.username} со своего аккаунта и отправь /start.', flush=True)
        await bot.run_until_disconnected()
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await bot.disconnect()
        await user.disconnect()
        lock_file.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('\nОстановлено.')
    except errors.RPCError as error:
        print('Ошибка Telegram:', type(error).__name__, 'Проверь данные входа. Если указан FloodWait — подожди перед повтором.')
    except (ValueError, OSError) as error:
        print('Ошибка настройки или подключения:', type(error).__name__)
