#!/usr/bin/env python3
"""Автоприём заявок. Python 3.9+, без сторонних библиотек.
Запуск: python3 join_bot.py
Токен: переменная BOT_TOKEN или скрытый ввод при запуске.
Принимает заявки во всех каналах, куда добавлен администратором.
Не отправляет сообщения. Для круглосуточной работы нужен сервер.
"""
import getpass
import json
import os
from pathlib import Path
import sqlite3
import time
import urllib.error
import urllib.request


class APIError(Exception):
    def __init__(self, data):
        self.code = data.get('error_code', 0)
        self.delay = data.get('parameters', {}).get('retry_after', 10)
        super().__init__(data.get('description', 'Ошибка Telegram'))


def api(token, method, **params):
    request = urllib.request.Request(
        'https://api.telegram.org/bot' + token + '/' + method,
        data=json.dumps(params).encode(),
        headers={'Content-Type': 'application/json'},
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = json.load(response)
    except urllib.error.HTTPError as error:
        try:
            data = json.loads(error.read())
        except (ValueError, UnicodeError):
            raise APIError({'error_code': error.code, 'description': 'HTTP ' + str(error.code)}) from None
    if not data.get('ok'):
        raise APIError(data)
    return data['result']


def main():
    token = os.environ.get('BOT_TOKEN', '').strip() or getpass.getpass('Вставь токен BotFather (ввод скрыт): ').strip()
    if not token:
        raise SystemExit('Токен не указан.')
    me = api(token, 'getMe')
    if api(token, 'getWebhookInfo').get('url'):
        raise SystemExit('У бота уже подключён webhook другого сервиса. Сначала отключи его в том сервисе.')
    db = sqlite3.connect(Path(__file__).resolve().with_name('join_bot_' + str(me['id']) + '.sqlite3'))
    db.execute('CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, offset INTEGER)')
    db.execute('INSERT OR IGNORE INTO state VALUES (1, 0)')
    db.execute('CREATE TABLE IF NOT EXISTS queue (chat INTEGER, user INTEGER, retry REAL DEFAULT 0, PRIMARY KEY(chat,user))')
    db.commit()
    print('@' + me['username'] + ': автоприём включён. Остановка: Ctrl+C.', flush=True)
    while True:
        try:
            offset = db.execute('SELECT offset FROM state WHERE id=1').fetchone()[0]
            pending = db.execute('SELECT COUNT(*) FROM queue').fetchone()[0]
            updates = api(token, 'getUpdates', offset=offset, timeout=1 if pending else 25, allowed_updates=['chat_join_request'])
            with db:
                for update in updates:
                    req = update.get('chat_join_request')
                    if req:
                        db.execute('INSERT INTO queue(chat,user,retry) VALUES(?,?,0) ON CONFLICT(chat,user) DO UPDATE SET retry=0', (req['chat']['id'], req['from']['id']))
                    db.execute('UPDATE state SET offset=? WHERE id=1', (update['update_id'] + 1,))
            for chat, user in db.execute('SELECT chat,user FROM queue WHERE retry<=? LIMIT 50', (time.time(),)).fetchall():
                try:
                    api(token, 'approveChatJoinRequest', chat_id=chat, user_id=user)
                    print('Заявка принята: канал', chat, 'пользователь', user, flush=True)
                except APIError as error:
                    if error.code in (401, 409):
                        raise
                    if not any(reason in str(error) for reason in ('USER_ALREADY_PARTICIPANT', 'HIDE_REQUESTER_MISSING')):
                        print('Не удалось принять заявку:', str(error).replace(token, '[TOKEN]'), flush=True)
                        with db:
                            db.execute('UPDATE queue SET retry=? WHERE chat=? AND user=?', (time.time() + max(error.delay, 30), chat, user))
                        if error.code == 429:
                            time.sleep(max(error.delay, 1))
                            break
                        continue
                with db:
                    db.execute('DELETE FROM queue WHERE chat=? AND user=?', (chat, user))
        except APIError as error:
            if error.code == 401:
                raise SystemExit('Токен недействителен. Проверь его в BotFather.') from None
            if error.code == 409:
                raise SystemExit('Конфликт: останови другую копию бота или отключи webhook.') from None
            print('Ошибка Telegram:', str(error).replace(token, '[TOKEN]'), flush=True)
            time.sleep(max(error.delay, 5))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            print('Нет ответа Telegram. Повтор через 5 секунд.', flush=True)
            time.sleep(5)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nБот остановлен.')
    except APIError as error:
        print('Не удалось подключиться к Telegram. Код ошибки:', error.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        print('Ошибка сети или доступа к файлу. Проверь подключение и права на папку.')
