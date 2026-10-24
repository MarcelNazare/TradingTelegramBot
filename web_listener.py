"""
Render deployment: Telegram webhook listener + scan trigger endpoint.

Two responsibilities:
  1. Receive Telegram updates at /webhook/<TOKEN> and dispatch them
     to the command handlers from telegram_commands.py
  2. Expose /run-scan for an external cron service (cron-job.org)
     to trigger the daily scan

Endpoints:
    POST /webhook/<TELEGRAM_TOKEN>   -> Telegram updates
    GET  /run-scan?k=<SCAN_SECRET>   -> trigger a full scan
    GET  /health                     -> health check for Render

Requires env vars:
    TELEGRAM_TOKEN
    TELEGRAM_CHAT_ID
    DATABASE_URL  (Neon Postgres connection string)
    SCAN_SECRET   (any random string; protects /run-scan)
    WEBHOOK_URL   (optional; e.g. https://your-bot.onrender.com)
                  If set, the app registers the webhook with Telegram on start.
"""

import os
import sys
import asyncio
import traceback
from pathlib import Path

from flask import Flask, request, abort, jsonify
from dotenv import load_dotenv

# Load .env before importing anything that reads env vars
load_dotenv(Path(__file__).parent / ".env")

from telegram import Update
from telegram_commands import build_application
import main as bot_main

# ============================================================
# STARTUP VALIDATION
# ============================================================
def _validate_env():
    missing = []
    for var in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "DATABASE_URL"):
        if not os.getenv(var):
            missing.append(var)
    if missing:
        print(f"[FATAL] Missing required env vars: {', '.join(missing)}")
        sys.exit(1)

_validate_env()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
SCAN_SECRET    = os.getenv("SCAN_SECRET", "")   # empty = no protection (dev only)
WEBHOOK_URL    = os.getenv("WEBHOOK_URL", "").rstrip("/")

# ============================================================
# TELEGRAM APPLICATION
# ============================================================
# Build the application once. In webhook mode, this is not polling —
# we hand Telegram a URL and it POSTs updates to us.
telegram_app = build_application()

# ============================================================
# FLASK APP
# ============================================================
app = Flask(__name__)


# ---------- Health ----------
@app.route("/health")
def health():
    return "ok", 200


# ---------- Webhook receiver ----------
@app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    """
    Telegram POSTs updates here. We feed them to the dispatcher queue
    and let python-telegram-bot process them asynchronously.
    """
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, telegram_app.bot)

        # The update_queue is created lazily by PTB when the app starts.
        # Put the update on the queue for the dispatcher to process.
        if telegram_app.update_queue is not None:
            # In webhook mode, we need to run the coroutine to put the update
            asyncio.get_event_loop().run_until_complete(
                telegram_app.update_queue.put(update)
            )
        else:
            # Fallback: process synchronously (rarely needed)
            asyncio.get_event_loop().run_until_complete(
                telegram_app.process_update(update)
            )
        return "ok", 200
    except Exception as e:
        traceback.print_exc()
        return f"error: {e}", 500


# ---------- Scan trigger ----------
@app.route("/run-scan", methods=["GET"])
def run_scan():
    """
    Triggered by cron-job.org (or any external scheduler) once per day.
    Protected by ?k=<SCAN_SECRET>.
    """
    if SCAN_SECRET and request.args.get("k") != SCAN_SECRET:
        abort(403)

    try:
        result = bot_main.run_scan()
        return jsonify({"status": "ok", "result": result}), 200
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


# ============================================================
# WEBHOOK REGISTRATION (run once at startup if WEBHOOK_URL is set)
# ============================================================
def _register_webhook():
    """
    If WEBHOOK_URL is set, tell Telegram to send updates to our endpoint.
    This replaces polling — Telegram pushes each update to us instead of
    us asking Telegram for new messages.
    """
    if not WEBHOOK_URL:
        print("[WEBHOOK] WEBHOOK_URL not set — skipping registration.")
        print("[WEBHOOK] Set WEBHOOK_URL=https://your-bot.onrender.com to enable.")
        print("[WEBHOOK] Until then, commands will NOT be received.")
        return

    full_url = f"{WEBHOOK_URL}/webhook/{TELEGRAM_TOKEN}"

    async def _set():
        await telegram_app.bot.set_webhook(
            url=full_url,
            allowed_updates=None,
            drop_pending_updates=True,
        )

    try:
        asyncio.get_event_loop().run_until_complete(_set())
        print(f"[WEBHOOK] Registered: {full_url}")
    except Exception as e:
        print(f"[WEBHOOK] Registration failed: {type(e).__name__}: {e}")


# Register on import — Render runs this file as a WSGI/ASGI entrypoint
_register_webhook()


# ============================================================
# ENTRYPOINT
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"[BOOT] Listening on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)