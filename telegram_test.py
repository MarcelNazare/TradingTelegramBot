import os
import asyncio
from dotenv import load_dotenv
from telegram import Bot

load_dotenv()

async def main():
    bot = Bot(token=os.getenv("TELEGRAM_TOKEN"))
    await bot.send_message(
        chat_id=os.getenv("TELEGRAM_CHAT_ID"),
        text="Test from forex_bot",
    )
    print("Sent — check Telegram")

if __name__ == "__main__":
    asyncio.run(main())