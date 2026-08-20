import asyncio
import aiohttp
from aiohttp_socks import ProxyConnector

PROXY_URL = "socks5://YUVLV1:037MkK@196.18.14.206:8000"
BOT_TOKEN = "8839226959:AAEnAfN1Hs3OOqmCPui0sSaz6MjCNsrR6Rc"


async def main():
    print("Подключаюсь через прокси...")
    connector = ProxyConnector.from_url(PROXY_URL)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getMe"
        async with session.post(url) as resp:
            print("Статус:", resp.status)
            print("Ответ:", await resp.text())


asyncio.run(main())
