"""
Render deployment: Telegram webhook listener + scan trigger endpoint.

This version uses a dedicated background event loop for the Telegram
Application. This is strictly required for python-telegram-bot v20+ to
work alongside Flask, and ensures that asyncio.to_thread (used in the
database commands) does not block or crash.

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
"""

import os
import sys
import asyncio
import threading
import traceback
from pathlib import Path

from flask import Flask, request, abort, jsonify, render_template
from dotenv import load_dotenv

# Load .env before importing anything that reads env vars
load_dotenv(Path(__file__).parent / ".env")

from telegram import Update
from telegram_commands import build_application
import main as bot_main


# Get the absolute path to the directory containing web_listener.py
BASE_DIR = Path(__file__).resolve().parent

# Tell Flask to look exactly in BASE_DIR/templates
app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))

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
telegram_app = build_application()

# ============================================================
# BACKGROUND EVENT LOOP (The Fallback)
# ============================================================
# Flask runs synchronously. PTB requires an active asyncio loop.
# We create a loop, run it forever in a daemon thread, and route all
# Telegram async work through it.

_loop = asyncio.new_event_loop()

def _run_background_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

_loop_thread = threading.Thread(
    target=_run_background_loop,
    args=(_loop,),
    daemon=True,
    name="TelegramAsyncLoop"
)
_loop_thread.start()

# Initialize the Telegram app inside the background loop
async def _init_telegram_app():
    await telegram_app.initialize()
    await telegram_app.start()

# Block until the app is fully initialized
asyncio.run_coroutine_threadsafe(_init_telegram_app(), _loop).result(timeout=15)
print("[BOOT] Telegram application initialized in background loop.")

# ============================================================
# WEBHOOK REGISTRATION
# ============================================================
def _register_webhook():
    if not WEBHOOK_URL:
        print("[WEBHOOK] WEBHOOK_URL not set — skipping registration.")
        print("[WEBHOOK] Set WEBHOOK_URL=https://tradingtelegrambot.onrender.com/ to enable.")
        return

    full_url = f"{WEBHOOK_URL}/webhook/{TELEGRAM_TOKEN}"

    async def _set():
        await telegram_app.bot.set_webhook(
            url=full_url,
            allowed_updates=None,
            drop_pending_updates=True,
        )

    try:
        # Run the set_webhook coroutine on our background loop
        future = asyncio.run_coroutine_threadsafe(_set(), _loop)
        future.result(timeout=10)  # Wait up to 10s
        print(f"[WEBHOOK] Registered: {full_url}")
    except Exception as e:
        print(f"[WEBHOOK] Registration failed: {type(e).__name__}: {e}")

_register_webhook()

# ============================================================
# FLASK APP
# ============================================================
app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/dashboard")
def dashboard():
    try:
        import psycopg2
        import psycopg2.extras
        
        # Connect to Neon
        conn = psycopg2.connect(os.getenv("DATABASE_URL"))
        conn.autocommit = True
        
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # 1. Get recent signals
            cur.execute("""
                SELECT created_at, symbol, direction, entry, stop_loss, 
                       take_profit, lot_size, outcome, pnl_cents 
                FROM signals 
                ORDER BY id DESC LIMIT 50
            """)
            signals = cur.fetchall()
            
            # 2. Get aggregate stats
            cur.execute("""
                SELECT 
                    COUNT(*) AS total,
                    SUM(CASE WHEN outcome = 'open' THEN 1 ELSE 0 END) AS open_n,
                    SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) AS losses,
                    COALESCE(SUM(pnl_cents), 0) AS pnl
                FROM signals
            """)
            totals = cur.fetchone()
            
        conn.close()
        
        # Calculate Win Rate
        closed = (totals["wins"] or 0) + (totals["losses"] or 0)
        win_rate = round(((totals["wins"] or 0) / closed * 100), 1) if closed else 0.0
        
        stats = {
            "total": totals["total"],
            "open": totals["open_n"] or 0,
            "win_rate": win_rate,
            "pnl": round(totals["pnl"], 1)
        }
        
        return render_template("dashboard.html", signals=signals, stats=stats)
        
    except Exception as e:
        return f"Dashboard Error: {e}", 500



@app.route("/health")
def health():
    return "ok", 200


@app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    """
    Telegram POSTs updates here. We feed them to the dispatcher queue
    on the background loop.
    """
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, telegram_app.bot)

        if telegram_app.update_queue is not None:
            # Put update on the queue. The background loop handles processing.
            future = asyncio.run_coroutine_threadsafe(
                telegram_app.update_queue.put(update),
                _loop
            )
            future.result(timeout=5)
        else:
            # Fallback: process update directly on the loop
            future = asyncio.run_coroutine_threadsafe(
                telegram_app.process_update(update),
                _loop
            )
            future.result(timeout=20)  # Some commands (like /scan) take time

        return "ok", 200
    except Exception as e:
        print(f"[WEBHOOK ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()
        return "error", 500


@app.route("/run-scan", methods=["GET"])
def run_scan():
    """
    Triggered by cron-job.org. Protected by ?k=<SCAN_SECRET>.
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
# ENTRYPOINT
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"[BOOT] Flask listening on 0.0.0.0:{port}")
    # threaded=True is crucial so webhook and health requests don't block each other
    app.run(host="0.0.0.0", port=port, threaded=True)