"""
Run the Telegram command listener.

Usage:
    python run_commands.py

Keep this process running while you want the bot to respond to commands.
It is separate from the daily scan — the scan writes to signals.db and sends
alerts; this process answers on-demand commands.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

from telegram_commands import build_application

load_dotenv(Path(__file__).parent / ".env")


def main():
    if not os.getenv("TELEGRAM_TOKEN"):
        raise RuntimeError("TELEGRAM_TOKEN not set in .env")
    if not os.getenv("TELEGRAM_CHAT_ID"):
        raise RuntimeError("TELEGRAM_CHAT_ID not set in .env")

    print("[LISTENER] Starting Telegram command listener…")
    app = build_application()
    print("[LISTENER] Polling. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=None, drop_pending_updates=True)


if __name__ == "__main__":
    main()