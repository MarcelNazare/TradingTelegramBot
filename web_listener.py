import os
import sys
import asyncio
import threading
import traceback
from pathlib import Path

from flask import Flask, request, abort, jsonify, render_template
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from telegram import Update
from telegram_commands import build_application
import main as bot_main

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
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SCAN_SECRET    = os.getenv("SCAN_SECRET", "")
WEBHOOK_URL    = os.getenv("WEBHOOK_URL", "").rstrip("/")

telegram_app = build_application()

# ============================================================
# BACKGROUND EVENT LOOP
# ============================================================
_loop = asyncio.new_event_loop()

def _run_background_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

_loop_thread = threading.Thread(target=_run_background_loop, args=(_loop,), daemon=True, name="TelegramAsyncLoop")
_loop_thread.start()

async def _init_telegram_app():
    await telegram_app.initialize()
    await telegram_app.start()

asyncio.run_coroutine_threadsafe(_init_telegram_app(), _loop).result(timeout=15)
print("[BOOT] Telegram application initialized in background loop.")

def _register_webhook():
    if not WEBHOOK_URL:
        print("[WEBHOOK] WEBHOOK_URL not set — skipping registration.")
        return
    full_url = f"{WEBHOOK_URL}/webhook/{TELEGRAM_TOKEN}"
    async def _set():
        await telegram_app.bot.set_webhook(url=full_url, allowed_updates=None, drop_pending_updates=True)
    try:
        future = asyncio.run_coroutine_threadsafe(_set(), _loop)
        future.result(timeout=10)
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
    try:
        return render_template("index.html")
    except Exception:
        return "Forex Signal Bot is running.", 200

@app.route("/health")
def health():
    return "ok", 200

@app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        update = Update.de_json(data, telegram_app.bot)

        if telegram_app.update_queue is not None:
            future = asyncio.run_coroutine_threadsafe(telegram_app.update_queue.put(update), _loop)
            future.result(timeout=5)
        else:
            future = asyncio.run_coroutine_threadsafe(telegram_app.process_update(update), _loop)
            future.result(timeout=20)

        return "ok", 200
    except Exception as e:
        print(f"[WEBHOOK ERROR] {type(e).__name__}: {e}")
        traceback.print_exc()
        return "error", 500

@app.route("/run-scan", methods=["GET"])
def run_scan():
    # 1. Security Check
    if SCAN_SECRET and request.args.get("k") != SCAN_SECRET:
        abort(403)
    
    # 2. Run the scan
    try:
        result = bot_main.run_scan()
        
        # 3. Send Telegram Heartbeat (Success)
        msg = (
            f"🤖 *Cron Job Triggered*\n"
            f"━━━━━━━━━━━━━━\n"
            f"Pairs Scanned: `{result.get('pairs_scanned', 0)}`\n"
            f"Signals Sent: `{result.get('signals_sent', 0)}`\n"
            f"Skipped: `{result.get('signals_skipped', 0)}`\n"
            f"Errors: `{result.get('errors', 0)}`\n"
            f"Run ID: `{result.get('run_id', 0)}`"
        )
        
        async def _send_msg():
            await telegram_app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=msg,
                parse_mode="Markdown"
            )
        
        # Send asynchronously so the HTTP response isn't delayed
        asyncio.run_coroutine_threadsafe(_send_msg(), _loop)
        
        return jsonify({"status": "ok", "result": result}), 200
        
    except Exception as e:
        traceback.print_exc()
        
        # 4. Send Telegram Heartbeat (Failure)
        err_msg = f"❌ *Scan Failed!*\n\n`{type(e).__name__}: {e}`"
        async def _send_err():
            await telegram_app.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=err_msg,
                parse_mode="Markdown"
            )
        asyncio.run_coroutine_threadsafe(_send_err(), _loop)
        
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/dashboard")
def dashboard():
    try:
        import psycopg2
        import psycopg2.extras
        
        conn = psycopg2.connect(os.getenv("DATABASE_URL"), options="-c search_path=public")
        conn.autocommit = True
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT created_at, symbol, direction, entry, stop_loss, take_profit, lot_size, outcome, pnl_cents FROM public.signals ORDER BY id DESC LIMIT 50")
            signals = cur.fetchall()
            
            cur.execute("""
                SELECT COUNT(*) AS total,
                    SUM(CASE WHEN outcome = 'open' THEN 1 ELSE 0 END) AS open_n,
                    SUM(CASE WHEN outcome = 'win' THEN 1 ELSE 0 END) AS wins,
                    SUM(CASE WHEN outcome = 'loss' THEN 1 ELSE 0 END) AS losses,
                    COALESCE(SUM(pnl_cents), 0) AS pnl
                FROM public.signals
            """)
            totals = cur.fetchone()
        conn.close()
        
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"[BOOT] Flask listening on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)