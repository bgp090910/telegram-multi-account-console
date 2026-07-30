import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()


async def main() -> None:
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    phone = os.getenv("TG_PHONE", "").strip() or input("手機號碼（含國碼）：").strip()
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.start(phone=phone)
    print("\n請將下方 Session String 貼到控制台新增帳號欄位：\n")
    print(client.session.save())
    await client.disconnect()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
