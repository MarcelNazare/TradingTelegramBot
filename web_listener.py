"""
Render deployment: webhook listener + scan trigger endpoint.
"""
import os
import threading
from pathlib import Path
from flask import Flask, request, abort
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from telegram import Update
from telegram.ext import Application
from telegram_commands import build_application
import main as bot_main

app = Flask(__name__)

# Build the Telegram application once at startup
telegram_app = build_application()

# Webhook route: Telegram sends updates here
@app.route(f"/webhook/{os.getenv('TELEGRAM_TOKEN')}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), telegram_app.bot)
    telegram_app.update_queue.put(update)
    return "ok", 200

# Cron route: cron-job.org hits this daily to run the scan
@app.route("/run-scan")
def run_scan():
    # Optional: add a secret query param check
    secret = os.getenv("SCAN_SECRET", "")
    if secret and request.args.get("k") != secret:
        abort(403)
    try:
        result = bot_main.run_scan()
        return {"status": "ok", "result": result}, 200
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500

@app.route("/health")
def health():
    return "ok", 200

def start_telegram_polling():
    """Fallback polling loop if webhook isn't set."""
    telegram_app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    # Start Telegram polling in a background thread (webhook mode is preferred,
    # but polling is more reliable on free tier without a public URL)
    threading.Thread(target=start_telegram_polling, daemon=True).start()
    app.run(host="0.0.0.0", port=port)