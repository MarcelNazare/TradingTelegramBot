"""
Forex Supply/Demand + Liquidity Trading Bot
- Weltrade Micro (Cent) account edition
- 1:3 RR, ATR-adaptive stops, dynamic 5%/10% risk
- Daily timeframe, majors + selective crosses
- SQLite persistence (signals, runs, pair_diagnostics)

Changes in this version
  1. Zones are now taken from the MOST RECENT bars (was: oldest bars).
  2. Zones are invalidated once price closes through them. Touched-but-not-
     broken zones stay valid (FRESH_ZONES_ONLY=False).
  3. Real de-duplication: one signal per (symbol, direction, candle date),
     plus no new signal while one is still open for that symbol+direction.
  4. The still-forming daily candle is dropped before analysis.
  5. SPREAD_BUFFER_PIPS is now added to the stop distance.
  6. Open signals are checked against later candles (win/loss/pnl filled in).
  7. Tuning: ZONE_BODY_MULT=1.2, RETEST_BUFFER_PIPS=5, SR_TOLERANCE_PIPS=10.
  8. Warning suppression now also targets yfinance's own logger, since
     Pandas4Warning is routed through logging, not the warnings module.
"""

# ============================================================
# WARNING SUPPRESSION — MUST BE FIRST EXECUTABLE CODE
# ============================================================
import warnings
warnings.filterwarnings("ignore")

import logging
logging.disable(logging.WARNING)

# yfinance routes some messages through its own logger — silence it by name
for _logger_name in ("yfinance", "peewee", "urllib3", "requests", "charset_normalizer"):
    _lg = logging.getLogger(_logger_name)
    _lg.setLevel(logging.CRITICAL)
    _lg.propagate = False
    _lg.handlers.clear()

try:
    from pandas.errors import Pandas4Warning as _P4W
    warnings.simplefilter("ignore", _P4W)
    warnings.filterwarnings("ignore", category=_P4W)
except Exception:
    pass

import os
import asyncio
import sqlite3
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv
from telegram import Bot

# ============================================================
# ENVIRONMENT
# ============================================================
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set.")

# ============================================================
# CONFIG
# ============================================================
ACCOUNT_BALANCE    = 300.0
RISK_PERCENT       = 5.0
MAX_RISK_PERCENT   = 10.0
BROKER_MIN_LOT     = 0.01
PIP_VALUE_STANDARD = 0.10
SPREAD_BUFFER_PIPS = 2.0

ATR_PERIOD        = 14
ATR_BUFFER_MULT   = 0.3
MIN_STOP_ATR_MULT = 1.0
MAX_STOP_ATR_MULT = 3.0

# --- Tuning knobs -------------------------------------------------
RETEST_BUFFER_PIPS = 5.0     # was 3.0 — more retest tolerance
SR_TOLERANCE_PIPS  = 10.0    # was 5 (hardcoded) — more S/R confluence
ZONE_BODY_MULT     = 1.2     # was 1.5 (hardcoded) — more zones

ZONE_LOOKBACK_BARS = 50
FRESH_ZONES_ONLY   = False   # was True — keep touched-but-not-broken zones
# ------------------------------------------------------------------

DAILY_CLOSE_UTC_HOUR = 22

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

PAIRS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X",
    "AUDUSD=X", "USDCAD=X", "NZDUSD=X",
    "EURGBP=X", "EURJPY=X", "GBPJPY=X", "AUDJPY=X",
]

DB_PATH = Path(__file__).parent / "signals.db"

# ============================================================
# DATABASE
# ============================================================
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def _column_exists(conn, table, column) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)

def db_init():
    conn = db_connect()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at      TEXT NOT NULL,
            signal_date     TEXT,
            symbol          TEXT NOT NULL,
            direction       TEXT NOT NULL,
            entry           REAL NOT NULL,
            stop_loss       REAL NOT NULL,
            take_profit     REAL NOT NULL,
            risk_pips       REAL NOT NULL,
            atr_pips        REAL NOT NULL,
            atr_multiple    REAL NOT NULL,
            lot_size        REAL NOT NULL,
            risk_cents      REAL NOT NULL,
            sizing_source   TEXT NOT NULL,
            zone_type       TEXT NOT NULL,
            zone_top        REAL NOT NULL,
            zone_bottom     REAL NOT NULL,
            outcome         TEXT DEFAULT 'open',
            exit_price      REAL,
            closed_at       TEXT,
            pnl_cents       REAL
        );
        CREATE INDEX IF NOT EXISTS idx_signals_created
            ON signals(created_at);
        CREATE INDEX IF NOT EXISTS idx_signals_symbol
            ON signals(symbol, created_at);

        CREATE TABLE IF NOT EXISTS runs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            pairs_scanned   INTEGER DEFAULT 0,
            signals_sent    INTEGER DEFAULT 0,
            signals_skipped INTEGER DEFAULT 0,
            errors          INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS pair_diagnostics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id      INTEGER NOT NULL,
            symbol      TEXT NOT NULL,
            zones       INTEGER,
            sr_levels   INTEGER,
            pools       INTEGER,
            reasons     TEXT,
            FOREIGN KEY(run_id) REFERENCES runs(id)
        );
        CREATE INDEX IF NOT EXISTS idx_diag_run
            ON pair_diagnostics(run_id);
        """)

        if not _column_exists(conn, "signals", "signal_date"):
            conn.execute("ALTER TABLE signals ADD COLUMN signal_date TEXT")

        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_unique
            ON signals(symbol, direction, signal_date)
        """)
        conn.commit()
    finally:
        conn.close()

def db_insert_run_start(started_at: str) -> int:
    conn = db_connect()
    try:
        cur = conn.execute(
            "INSERT INTO runs (started_at) VALUES (?)", (started_at,)
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()

def db_finish_run(run_id, pairs_scanned, signals_sent, signals_skipped, errors):
    conn = db_connect()
    try:
        conn.execute("""
            UPDATE runs SET
                finished_at = ?, pairs_scanned = ?, signals_sent = ?,
                signals_skipped = ?, errors = ?
            WHERE id = ?
        """, (datetime.now(timezone.utc).isoformat(),
              pairs_scanned, signals_sent, signals_skipped, errors, run_id))
        conn.commit()
    finally:
        conn.close()

def db_insert_diagnostic(run_id, symbol, diag):
    conn = db_connect()
    try:
        conn.execute("""
            INSERT INTO pair_diagnostics
                (run_id, symbol, zones, sr_levels, pools, reasons)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (run_id, symbol, diag["zones"], diag["sr"], diag["pools"],
              ",".join(diag["reasons"])))
        conn.commit()
    finally:
        conn.close()

def db_insert_signal(signal, lot_size, risk_cents, source) -> bool:
    conn = db_connect()
    try:
        cur = conn.execute("""
            INSERT OR IGNORE INTO signals
                (created_at, signal_date, symbol, direction, entry, stop_loss,
                 take_profit, risk_pips, atr_pips, atr_multiple,
                 lot_size, risk_cents, sizing_source, zone_type,
                 zone_top, zone_bottom)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            signal["signal_date"],
            signal["symbol"], signal["direction"],
            signal["entry"], signal["stop"], signal["target"],
            signal["risk_pips"], signal["atr_pips"], signal["atr_multiple"],
            lot_size, risk_cents, source,
            signal["zone"]["type"],
            signal["zone"]["top"], signal["zone"]["bottom"],
        ))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()

def db_has_open_signal(symbol, direction) -> bool:
    conn = db_connect()
    try:
        row = conn.execute("""
            SELECT 1 FROM signals
            WHERE symbol = ? AND direction = ? AND outcome = 'open'
            LIMIT 1
        """, (symbol, direction)).fetchone()
        return row is not None
    finally:
        conn.close()

# ============================================================
# DATA + INDICATORS
# ============================================================
def fetch_data(symbol, interval="1d", period="6mo", drop_forming=True):
    df = yf.download(symbol, interval=interval, period=period,
                     progress=False, auto_adjust=False)
    if df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.columns = ["open", "high", "low", "close", "volume"]

    if drop_forming and interval == "1d" and not df.empty:
        now = datetime.now(timezone.utc)
        last_date = pd.Timestamp(df.index[-1]).date()
        if last_date >= now.date() and now.hour < DAILY_CLOSE_UTC_HOUR:
            df = df.iloc[:-1]
    return df

def calculate_atr(df, period=ATR_PERIOD):
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def detect_swing_points(df, window=5):
    highs, lows = [], []
    for i in range(window, len(df) - window):
        if df["high"].iloc[i] == df["high"].iloc[i-window:i+window+1].max():
            highs.append(i)
        if df["low"].iloc[i] == df["low"].iloc[i-window:i+window+1].min():
            lows.append(i)
    return highs, lows

def _zone_still_valid(df, kind, top, bottom, impulse_idx):
    after = df.iloc[impulse_idx + 1:]
    if after.empty:
        return True
    prior = after.iloc[:-1]

    if kind == "demand":
        if (after["close"] < bottom).any():
            return False
        if FRESH_ZONES_ONLY and not prior.empty and (prior["low"] <= top).any():
            return False
    else:
        if (after["close"] > top).any():
            return False
        if FRESH_ZONES_ONLY and not prior.empty and (prior["high"] >= bottom).any():
            return False
    return True

def find_supply_demand_zones(df, lookback=ZONE_LOOKBACK_BARS):
    avg_body = (df["close"] - df["open"]).abs().rolling(20).mean().iloc[-1]
    if pd.isna(avg_body) or avg_body <= 0:
        return []

    n = len(df)
    start = max(2, n - 1 - lookback)
    zones = []

    for i in range(start, n - 1):
        body = abs(df["close"].iloc[i] - df["open"].iloc[i])
        if body < ZONE_BODY_MULT * avg_body:
            continue
        prev = i - 1

        if df["close"].iloc[i] > df["open"].iloc[i]:
            if df["close"].iloc[prev] < df["open"].iloc[prev]:
                top    = df["high"].iloc[prev]
                bottom = df["low"].iloc[prev]
                if _zone_still_valid(df, "demand", top, bottom, i):
                    zones.append({"type": "demand", "top": top,
                                  "bottom": bottom, "index": prev})
        elif df["close"].iloc[i] < df["open"].iloc[i]:
            if df["close"].iloc[prev] > df["open"].iloc[prev]:
                top    = df["high"].iloc[prev]
                bottom = df["low"].iloc[prev]
                if _zone_still_valid(df, "supply", top, bottom, i):
                    zones.append({"type": "supply", "top": top,
                                  "bottom": bottom, "index": prev})
    return zones

def find_liquidity_pools(df, tolerance_pips=3.0,
                         swing_window=5, min_bars_apart=10, max_age_bars=90):
    pips = 0.0001 if df["close"].iloc[-1] < 10 else 0.01
    tol  = tolerance_pips * pips
    n = len(df)
    cutoff = max(n - max_age_bars, 0)

    highs_idx, lows_idx = [], []
    for i in range(swing_window, n - swing_window):
        if i < cutoff:
            continue
        wh = df["high"].iloc[i - swing_window:i + swing_window + 1]
        wl = df["low"].iloc[i - swing_window:i + swing_window + 1]
        if df["high"].iloc[i] == wh.max():
            highs_idx.append(i)
        if df["low"].iloc[i] == wl.min():
            lows_idx.append(i)

    pools = []
    for idx_list, kind in ((highs_idx, "equal_highs"), (lows_idx, "equal_lows")):
        for a in range(len(idx_list)):
            for b in range(a + 1, len(idx_list)):
                i, j = idx_list[a], idx_list[b]
                if j - i < min_bars_apart:
                    continue
                if kind == "equal_highs":
                    lvl_i, lvl_j = df["high"].iloc[i], df["high"].iloc[j]
                else:
                    lvl_i, lvl_j = df["low"].iloc[i], df["low"].iloc[j]
                if abs(lvl_i - lvl_j) <= tol:
                    pools.append({"type": kind,
                                  "level": (lvl_i + lvl_j) / 2,
                                  "recent_bar": j})
    return pools

def find_support_resistance(df, window=10):
    highs, lows = detect_swing_points(df, window)
    pips = 0.0001 if df["close"].iloc[-1] < 10 else 0.01
    tol = SR_TOLERANCE_PIPS * pips
    sr = []
    for idx in highs:
        level = df["high"].iloc[idx]
        if sum(1 for h in df["high"] if abs(h - level) <= tol) >= 2:
            sr.append({"type": "resistance", "level": level})
    for idx in lows:
        level = df["low"].iloc[idx]
        if sum(1 for l in df["low"] if abs(l - level) <= tol) >= 2:
            sr.append({"type": "support", "level": level})
    return sr

# ============================================================
# RETEST LOGIC
# ============================================================
def touched_demand(zone, last_low, last_close, buffer_pips, pip):
    zlow  = zone["bottom"]
    zhigh = zone["top"]
    buf   = buffer_pips * pip

    if last_close < zlow:
        return False
    if zlow <= last_low <= zhigh:
        return True
    if last_low < zlow <= last_close:
        return True
    if zlow <= last_close <= zhigh:
        return True
    if zlow - buf <= last_low < zlow:
        return True
    return False

def touched_supply(zone, last_high, last_close, buffer_pips, pip):
    zlow  = zone["bottom"]
    zhigh = zone["top"]
    buf   = buffer_pips * pip

    if last_close > zhigh:
        return False
    if zlow <= last_high <= zhigh:
        return True
    if last_high > zhigh >= last_close:
        return True
    if zlow <= last_close <= zhigh:
        return True
    if zhigh < last_high <= zhigh + buf:
        return True
    return False

# ============================================================
# SIGNAL GENERATION
# ============================================================
def generate_signal(df, symbol):
    diag = {"zones": 0, "sr": 0, "pools": 0, "reasons": []}

    if len(df) < 60:
        diag["reasons"].append("insufficient_data")
        return None, diag
    atr = calculate_atr(df).iloc[-1]
    if pd.isna(atr) or atr <= 0:
        diag["reasons"].append("no_atr")
        return None, diag

    zones = find_supply_demand_zones(df)
    pools = find_liquidity_pools(df)
    sr    = find_support_resistance(df)

    diag["zones"] = len(zones)
    diag["sr"]    = len(sr)
    diag["pools"] = len(pools)

    if not zones:
        diag["reasons"].append("no_zones")
        return None, diag

    last_close = df["close"].iloc[-1]
    last_low   = df["low"].iloc[-1]
    last_high  = df["high"].iloc[-1]
    pip = 0.0001 if last_close < 10 else 0.01
    spread = SPREAD_BUFFER_PIPS * pip
    signal_date = pd.Timestamp(df.index[-1]).date().isoformat()

    # ---------------- LONG ----------------
    for zone in zones:
        if zone["type"] != "demand":
            continue
        if not touched_demand(zone, last_low, last_close,
                              RETEST_BUFFER_PIPS, pip):
            diag["reasons"].append("long_no_retest")
            continue
        if not any(abs(s["level"] - zone["bottom"]) <= SR_TOLERANCE_PIPS * pip
                   for s in sr if s["type"] == "support"):
            diag["reasons"].append("long_no_support")
            continue
        if any(p["type"] == "equal_lows"
               and zone["bottom"] - 10 * pip <= p["level"] <= zone["bottom"]
               for p in pools):
            diag["reasons"].append("long_liquidity_trap")
            continue

        entry     = last_close
        zone_dist = max(entry - zone["bottom"], 0.0)
        raw_dist  = zone_dist + ATR_BUFFER_MULT * atr
        stop_dist = max(raw_dist, MIN_STOP_ATR_MULT * atr) + spread
        if stop_dist > MAX_STOP_ATR_MULT * atr:
            diag["reasons"].append("long_stop_too_wide")
            continue

        return {
            "symbol": symbol.replace("=X", ""), "direction": "LONG",
            "signal_date": signal_date,
            "entry": round(entry, 5),
            "stop": round(entry - stop_dist, 5),
            "target": round(entry + 3 * stop_dist, 5),
            "risk_pips": round(stop_dist / pip, 1),
            "atr_pips": round(atr / pip, 1),
            "atr_multiple": round(stop_dist / atr, 2),
            "zone": zone,
        }, diag

    # ---------------- SHORT ----------------
    for zone in zones:
        if zone["type"] != "supply":
            continue
        if not touched_supply(zone, last_high, last_close,
                              RETEST_BUFFER_PIPS, pip):
            diag["reasons"].append("short_no_retest")
            continue
        if not any(abs(s["level"] - zone["top"]) <= SR_TOLERANCE_PIPS * pip
                   for s in sr if s["type"] == "resistance"):
            diag["reasons"].append("short_no_resistance")
            continue
        if any(p["type"] == "equal_highs"
               and zone["top"] <= p["level"] <= zone["top"] + 10 * pip
               for p in pools):
            diag["reasons"].append("short_liquidity_trap")
            continue

        entry     = last_close
        zone_dist = max(zone["top"] - entry, 0.0)
        raw_dist  = zone_dist + ATR_BUFFER_MULT * atr
        stop_dist = max(raw_dist, MIN_STOP_ATR_MULT * atr) + spread
        if stop_dist > MAX_STOP_ATR_MULT * atr:
            diag["reasons"].append("short_stop_too_wide")
            continue

        return {
            "symbol": symbol.replace("=X", ""), "direction": "SHORT",
            "signal_date": signal_date,
            "entry": round(entry, 5),
            "stop": round(entry + stop_dist, 5),
            "target": round(entry - 3 * stop_dist, 5),
            "risk_pips": round(stop_dist / pip, 1),
            "atr_pips": round(atr / pip, 1),
            "atr_multiple": round(stop_dist / atr, 2),
            "zone": zone,
        }, diag

    if not diag["reasons"]:
        diag["reasons"].append("no_matching_direction")
    return None, diag

# ============================================================
# POSITION SIZING
# ============================================================
def calculate_lot_size(stop_pips):
    if stop_pips <= 0:
        return 0.0, 0.0, "invalid_stop"
    target = ACCOUNT_BALANCE * (RISK_PERCENT / 100)
    lots = target / (stop_pips * PIP_VALUE_STANDARD)
    if lots < BROKER_MIN_LOT:
        min_risk = BROKER_MIN_LOT * stop_pips * PIP_VALUE_STANDARD
        if min_risk > ACCOUNT_BALANCE * (MAX_RISK_PERCENT / 100):
            return 0.0, min_risk, "min_lot_exceeds_max_risk"
        lots = BROKER_MIN_LOT
        risk = min_risk
        src  = "min_lot_floor"
    else:
        lots = round(lots, 2)
        risk = lots * stop_pips * PIP_VALUE_STANDARD
        src  = "target"
    if (risk / ACCOUNT_BALANCE) * 100 > MAX_RISK_PERCENT:
        return 0.0, risk, "rounded_exceeds_max_risk"
    return lots, risk, src

# ============================================================
# OUTCOME TRACKING
# ============================================================
def update_open_signals() -> int:
    conn = db_connect()
    try:
        rows = conn.execute("""
            SELECT id, symbol, direction, stop_loss, take_profit,
                   risk_cents, signal_date, created_at
            FROM signals WHERE outcome = 'open'
        """).fetchall()
    finally:
        conn.close()

    if not rows:
        return 0

    cache = {}
    updates = []

    for r in rows:
        sym = r["symbol"]
        if sym not in cache:
            try:
                cache[sym] = fetch_data(sym + "=X")
            except Exception:
                cache[sym] = pd.DataFrame()
        df = cache[sym]
        if df.empty:
            continue

        start_str = r["signal_date"] or r["created_at"][:10]
        start_date = datetime.fromisoformat(start_str[:10]).date()
        sl, tp = r["stop_loss"], r["take_profit"]
        is_long = r["direction"] == "LONG"

        for ts, bar in df.iterrows():
            bar_date = pd.Timestamp(ts).date()
            if bar_date <= start_date:
                continue

            if is_long:
                hit_sl = bar["low"] <= sl
                hit_tp = bar["high"] >= tp
            else:
                hit_sl = bar["high"] >= sl
                hit_tp = bar["low"] <= tp

            if hit_sl:
                updates.append((r["id"], "loss", sl, bar_date.isoformat(),
                                -r["risk_cents"]))
                break
            if hit_tp:
                updates.append((r["id"], "win", tp, bar_date.isoformat(),
                                3 * r["risk_cents"]))
                break

    if not updates:
        return 0

    conn = db_connect()
    try:
        for sid, outcome, exit_price, closed_at, pnl in updates:
            conn.execute("""
                UPDATE signals
                SET outcome = ?, exit_price = ?, closed_at = ?, pnl_cents = ?
                WHERE id = ?
            """, (outcome, exit_price, closed_at, pnl, sid))
        conn.commit()
    finally:
        conn.close()
    return len(updates)

# ============================================================
# TELEGRAM
# ============================================================
async def send_telegram(signal, lot_size, risk_cents, source):
    bot = Bot(token=TELEGRAM_TOKEN)
    risk_pct = (risk_cents / ACCOUNT_BALANCE) * 100
    msg = (
        f"🚨 *{signal['symbol']} {signal['direction']}*\n"
        f"━━━━━━━━━━━━━━\n"
        f"Entry: `{signal['entry']}`\n"
        f"Stop Loss: `{signal['stop']}`\n"
        f"Take Profit: `{signal['target']}`\n"
        f"Risk: {signal['risk_pips']} pips (1:3 RR)\n"
        f"ATR({ATR_PERIOD}): {signal['atr_pips']} pips | Stop = {signal['atr_multiple']}× ATR\n"
        f"Lot Size: `{lot_size}` ({source})\n"
        f"Risk ≈ {risk_cents:.1f}¢ ({risk_pct:.1f}%)\n"
        f"Zone: {signal['zone']['type'].capitalize()} "
        f"({signal['zone']['bottom']:.5f} – {signal['zone']['top']:.5f})\n"
        f"Candle: {signal['signal_date']}\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")

# ============================================================
# MAIN
# ============================================================
def main():
    db_init()

    try:
        closed = update_open_signals()
        if closed:
            print(f"[OUTCOMES] {closed} open signal(s) closed")
    except Exception as e:
        print(f"[ERROR] outcome update: {type(e).__name__}: {e}")

    started_at = datetime.now(timezone.utc).isoformat()
    run_id = db_insert_run_start(started_at)

    print(f"[START] {started_at} | Balance={ACCOUNT_BALANCE}¢ | "
          f"TargetRisk={RISK_PERCENT}% | MaxRisk={MAX_RISK_PERCENT}% | "
          f"PipValue={PIP_VALUE_STANDARD} | DB={DB_PATH.name} | run_id={run_id}")
    print(f"[CFG]   ZONE_BODY_MULT={ZONE_BODY_MULT} | "
          f"RETEST_BUFFER_PIPS={RETEST_BUFFER_PIPS} | "
          f"SR_TOLERANCE_PIPS={SR_TOLERANCE_PIPS} | "
          f"FRESH_ZONES_ONLY={FRESH_ZONES_ONLY}")

    pairs_scanned = 0
    signals_sent = 0
    signals_skipped = 0
    errors = 0

    for pair in PAIRS:
        try:
            df = fetch_data(pair, interval="1d", period="6mo")
            if df.empty:
                print(f"[SKIP] {pair}: no data")
                db_insert_diagnostic(run_id, pair,
                                     {"zones": 0, "sr": 0, "pools": 0,
                                      "reasons": ["no_data"]})
                continue

            pairs_scanned += 1
            signal, diag = generate_signal(df, pair)
            db_insert_diagnostic(run_id, pair, diag)

            if signal is None:
                reasons = ",".join(diag["reasons"]) or "unknown"
                print(f"[NONE] {pair}: zones={diag['zones']} sr={diag['sr']} "
                      f"pools={diag['pools']} | {reasons}")
                continue

            if db_has_open_signal(signal["symbol"], signal["direction"]):
                print(f"[OPEN] {pair} {signal['direction']}: signal already open, skipping")
                continue

            lots, risk_cents, source = calculate_lot_size(signal["risk_pips"])

            if lots == 0.0:
                signals_skipped += 1
                print(f"[SKIP] {pair} {signal['direction']}: {source} "
                      f"(stop={signal['risk_pips']} pips)")
                continue

            inserted = db_insert_signal(signal, lots, risk_cents, source)
            if not inserted:
                print(f"[DUP] {pair} {signal['direction']}: already sent for "
                      f"candle {signal['signal_date']}, skipping Telegram")
                continue

            print(f"[SIGNAL] {pair} {signal['direction']} | lots={lots} | "
                  f"risk={risk_cents:.1f}¢ ({(risk_cents/ACCOUNT_BALANCE)*100:.1f}%) | "
                  f"source={source} | zones={diag['zones']} sr={diag['sr']} pools={diag['pools']}")

            asyncio.run(send_telegram(signal, lots, risk_cents, source))
            signals_sent += 1

        except Exception as e:
            errors += 1
            print(f"[ERROR] {pair}: {type(e).__name__}: {e}")

    db_finish_run(run_id, pairs_scanned, signals_sent, signals_skipped, errors)
    print(f"[DONE] {signals_sent} signal(s) sent | "
          f"{signals_skipped} skipped | {errors} errors | run_id={run_id}")

if __name__ == "__main__":
    main()