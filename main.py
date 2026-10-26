"""
Forex Supply/Demand + Liquidity Trading Bot
- SURVIVAL SCALPING EDITION (15-Minute)
"""

import warnings
warnings.filterwarnings("ignore")
import logging
logging.disable(logging.WARNING)

for _logger_name in ("yfinance", "peewee", "urllib3", "requests", "charset_normalizer"):
    _lg = logging.getLogger(_logger_name)
    _lg.setLevel(logging.CRITICAL)
    _lg.propagate = False
    _lg.handlers.clear()

import os
import sys
import time
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import numpy as np
import yfinance as yf
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from telegram import Bot

# ============================================================
# ENVIRONMENT
# ============================================================
env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DATABASE_URL     = os.getenv("DATABASE_URL")

if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL must be set.")

# ============================================================
# CONFIG — SURVIVAL SCALPING MODE (15-Minute Timeframe)
# ============================================================
ACCOUNT_BALANCE    = 300.0    
RISK_PERCENT       = 1.0      
MAX_RISK_PERCENT   = 1.5      
BROKER_MIN_LOT     = 0.01
PIP_VALUE_STANDARD = 0.10     
SPREAD_BUFFER_PIPS = 1.5      

ATR_PERIOD        = 14
ATR_BUFFER_MULT   = 0.2       
MIN_STOP_ATR_MULT = 0.5       
MAX_STOP_ATR_MULT = 1.5       

RR_RATIO           = 2.0      
RETEST_BUFFER_PIPS = 3.0      # Checks last 3 candles
SR_TOLERANCE_PIPS  = 3.0      
ZONE_BODY_MULT     = 1.5      

ZONE_LOOKBACK_BARS = 100      
FRESH_ZONES_ONLY   = False
MAX_DAILY_TRADES   = 3        

INTERVAL = "15m"              
PERIOD   = "30d"              

PAIRS = [
    "EURUSD=X", "GBPUSD=X", "USDJPY=X", "USDCHF=X",
    "AUDUSD=X", "USDCAD=X", "NZDUSD=X"
]

# ============================================================
# DATABASE
# ============================================================
def db_connect():
    for attempt in range(3):
        try:
            conn = psycopg2.connect(DATABASE_URL)
            conn.autocommit = False
            return conn
        except psycopg2.OperationalError:
            if attempt == 2:
                raise
            time.sleep(2)

def db_init():
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id              SERIAL PRIMARY KEY,
                created_at      TEXT NOT NULL,
                signal_date     TEXT,
                symbol          TEXT NOT NULL,
                direction       TEXT NOT NULL,
                entry           DOUBLE PRECISION NOT NULL,
                stop_loss       DOUBLE PRECISION NOT NULL,
                take_profit     DOUBLE PRECISION NOT NULL,
                risk_pips       DOUBLE PRECISION NOT NULL,
                atr_pips        DOUBLE PRECISION NOT NULL,
                atr_multiple    DOUBLE PRECISION NOT NULL,
                lot_size        DOUBLE PRECISION NOT NULL,
                risk_cents      DOUBLE PRECISION NOT NULL,
                sizing_source   TEXT NOT NULL,
                zone_type       TEXT NOT NULL,
                zone_top        DOUBLE PRECISION NOT NULL,
                zone_bottom     DOUBLE PRECISION NOT NULL,
                outcome         TEXT DEFAULT 'open',
                exit_price      DOUBLE PRECISION,
                closed_at       TEXT,
                pnl_cents       DOUBLE PRECISION
            );
            CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
            CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol, created_at);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_unique ON signals(symbol, direction, signal_date);

            CREATE TABLE IF NOT EXISTS runs (
                id              SERIAL PRIMARY KEY,
                started_at      TEXT NOT NULL,
                finished_at     TEXT,
                pairs_scanned   INTEGER DEFAULT 0,
                signals_sent    INTEGER DEFAULT 0,
                signals_skipped INTEGER DEFAULT 0,
                errors          INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS pair_diagnostics (
                id          SERIAL PRIMARY KEY,
                run_id      INTEGER NOT NULL REFERENCES runs(id),
                symbol      TEXT NOT NULL,
                zones       INTEGER,
                sr_levels   INTEGER,
                pools       INTEGER,
                reasons     TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_diag_run ON pair_diagnostics(run_id);
            """)
        conn.commit()
    finally:
        conn.close()

def db_insert_run_start(started_at):
    conn = db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO runs (started_at) VALUES (%s) RETURNING id", (started_at,))
            run_id = cur.fetchone()[0]
        conn.commit()
        return run_id
    finally:
        conn.close()

def db_finish_run(run_id, pairs_scanned, signals_sent, signals_skipped, errors):
    try:
        conn = db_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE runs SET finished_at=%s, pairs_scanned=%s,
                                    signals_sent=%s, signals_skipped=%s, errors=%s
                    WHERE id=%s
                """, (datetime.now(timezone.utc).isoformat(),
                      pairs_scanned, signals_sent, signals_skipped, errors, run_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[DB ERROR] db_finish_run: {e}")

def db_insert_diagnostic(run_id, symbol, diag):
    try:
        conn = db_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO pair_diagnostics
                        (run_id, symbol, zones, sr_levels, pools, reasons)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (run_id, symbol, diag["zones"], diag["sr"], diag["pools"],
                      ",".join(sorted(set(diag["reasons"])))))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[DB ERROR] db_insert_diagnostic ({symbol}): {e}")

def db_insert_signal(signal, lot_size, risk_cents, source):
    try:
        conn = db_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO signals
                        (created_at, signal_date, symbol, direction, entry, stop_loss,
                         take_profit, risk_pips, atr_pips, atr_multiple,
                         lot_size, risk_cents, sizing_source, zone_type,
                         zone_top, zone_bottom)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (symbol, direction, signal_date) DO NOTHING
                    RETURNING id
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
                inserted = cur.fetchone() is not None
            conn.commit()
            return inserted
        finally:
            conn.close()
    except Exception as e:
        print(f"[DB ERROR] db_insert_signal: {e}")
        return False

def db_has_open_signal(symbol, direction):
    try:
        conn = db_connect()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM signals
                    WHERE symbol=%s AND direction=%s AND outcome='open'
                    LIMIT 1
                """, (symbol, direction))
                return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception as e:
        print(f"[DB ERROR] db_has_open_signal: {e}")
        return False

def db_get_todays_signal_count():
    try:
        conn = db_connect()
        try:
            with conn.cursor() as cur:
                today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                cur.execute("""
                    SELECT COUNT(*) FROM signals 
                    WHERE created_at LIKE %s
                """, (f"{today_str}%",))
                return cur.fetchone()[0]
        finally:
            conn.close()
    except Exception as e:
        print(f"[DB ERROR] db_get_todays_signal_count: {e}")
        return 0

# ============================================================
# DATA & INDICATORS
# ============================================================
def fetch_data(symbol, interval=INTERVAL, period=PERIOD):
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval, auto_adjust=False)
        
        if df.empty:
            return pd.DataFrame()
            
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.columns = ["open", "high", "low", "close", "volume"]
        df = df.astype(float)
        
        if not df.empty:
            df = df.iloc[:-1]
        return df
    except Exception as e:
        print(f"[DATA ERROR] {symbol}: {e}")
        return pd.DataFrame()

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
                top, bottom = df["high"].iloc[prev], df["low"].iloc[prev]
                if _zone_still_valid(df, "demand", top, bottom, i):
                    zones.append({"type": "demand", "top": top,
                                  "bottom": bottom, "index": prev})
        elif df["close"].iloc[i] < df["open"].iloc[i]:
            if df["close"].iloc[prev] > df["open"].iloc[prev]:
                top, bottom = df["high"].iloc[prev], df["low"].iloc[prev]
                if _zone_still_valid(df, "supply", top, bottom, i):
                    zones.append({"type": "supply", "top": top,
                                  "bottom": bottom, "index": prev})
    return zones

def find_liquidity_pools(df, tolerance_pips=3.0,
                         swing_window=5, min_bars_apart=10, max_age_bars=90):
    pips = 0.0001 if df["close"].iloc[-1] < 10 else 0.01
    tol = tolerance_pips * pips
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

def touched_demand(zone, last_low, last_close, buffer_pips, pip):
    zlow, zhigh = zone["bottom"], zone["top"]
    buf = buffer_pips * pip
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
    zlow, zhigh = zone["bottom"], zone["top"]
    buf = buffer_pips * pip
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

    try:
        if len(df) < 60:
            diag["reasons"].append("insufficient_data")
            return None, diag
        
        atr = calculate_atr(df).iloc[-1]
        if pd.isna(atr) or atr <= 0:
            diag["reasons"].append("no_atr")
            return None, diag

        zones = find_supply_demand_zones(df)
        pools = find_liquidity_pools(df)
        sr = find_support_resistance(df)

        diag["zones"] = len(zones)
        diag["sr"] = len(sr)
        diag["pools"] = len(pools)

        if not zones:
            diag["reasons"].append("no_zones")
            return None, diag

        last_close = df["close"].iloc[-1]
        pip = 0.0001 if last_close < 10 else 0.01
        spread = SPREAD_BUFFER_PIPS * pip
        
        signal_date = pd.Timestamp(df.index[-1]).isoformat() 

        # ---------------- LONG ----------------
        for zone in zones:
            if zone["type"] != "demand":
                continue
                
            # Check last 3 candles for retest
            retest_hit = False
            for i in range(1, 4):
                if touched_demand(zone, df["low"].iloc[-i], df["close"].iloc[-i], RETEST_BUFFER_PIPS, pip):
                    retest_hit = True
                    break
            
            if not retest_hit:
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

            entry = last_close
            zone_dist = max(entry - zone["bottom"], 0.0)
            raw_dist = zone_dist + ATR_BUFFER_MULT * atr
            stop_dist = max(raw_dist, MIN_STOP_ATR_MULT * atr) + spread
            if stop_dist > MAX_STOP_ATR_MULT * atr:
                diag["reasons"].append("long_stop_too_wide")
                continue

            return {
                "symbol": symbol.replace("=X", ""), "direction": "LONG",
                "signal_date": signal_date,
                "entry": round(entry, 5),
                "stop": round(entry - stop_dist, 5),
                "target": round(entry + (RR_RATIO * stop_dist), 5),
                "risk_pips": round(stop_dist / pip, 1),
                "atr_pips": round(atr / pip, 1),
                "atr_multiple": round(stop_dist / atr, 2),
                "zone": zone,
            }, diag

        # ---------------- SHORT ----------------
        for zone in zones:
            if zone["type"] != "supply":
                continue
                
            # Check last 3 candles for retest
            retest_hit = False
            for i in range(1, 4):
                if touched_supply(zone, df["high"].iloc[-i], df["close"].iloc[-i], RETEST_BUFFER_PIPS, pip):
                    retest_hit = True
                    break
                    
            if not retest_hit:
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

            entry = last_close
            zone_dist = max(zone["top"] - entry, 0.0)
            raw_dist = zone_dist + ATR_BUFFER_MULT * atr
            stop_dist = max(raw_dist, MIN_STOP_ATR_MULT * atr) + spread
            if stop_dist > MAX_STOP_ATR_MULT * atr:
                diag["reasons"].append("short_stop_too_wide")
                continue

            return {
                "symbol": symbol.replace("=X", ""), "direction": "SHORT",
                "signal_date": signal_date,
                "entry": round(entry, 5),
                "stop": round(entry + stop_dist, 5),
                "target": round(entry - (RR_RATIO * stop_dist), 5),
                "risk_pips": round(stop_dist / pip, 1),
                "atr_pips": round(atr / pip, 1),
                "atr_multiple": round(stop_dist / atr, 2),
                "zone": zone,
            }, diag

        if not diag["reasons"]:
            diag["reasons"].append("no_matching_direction")
        return None, diag

    except ValueError:
        diag["reasons"].append("error ValueError")
        return None, diag
    except Exception as e:
        diag["reasons"].append(f"error {type(e).__name__}")
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
        src = "min_lot_floor"
    else:
        lots = round(lots, 2)
        risk = lots * stop_pips * PIP_VALUE_STANDARD
        src = "target"
    if (risk / ACCOUNT_BALANCE) * 100 > MAX_RISK_PERCENT:
        return 0.0, risk, "rounded_exceeds_max_risk"
    return lots, risk, src

# ============================================================
# OUTCOME TRACKING
# ============================================================
def update_open_signals():
    try:
        conn = db_connect()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, symbol, direction, stop_loss, take_profit,
                           risk_cents, signal_date, created_at
                    FROM signals WHERE outcome = 'open'
                """)
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        print(f"[DB ERROR] update_open_signals: {e}")
        return 0

    if not rows:
        return 0

    cache, updates = {}, []
    for r in rows:
        sym = r["symbol"]
        if sym not in cache:
            try:
                cache[sym] = fetch_data(sym + "=X", interval=INTERVAL, period="5d")
            except Exception:
                cache[sym] = pd.DataFrame()
        df = cache[sym]
        if df.empty:
            continue

        start_str = r["signal_date"] or r["created_at"]
        try:
            start_date = datetime.fromisoformat(start_str)
        except ValueError:
            continue

        sl, tp = r["stop_loss"], r["take_profit"]
        is_long = r["direction"] == "LONG"

        for ts, bar in df.iterrows():
            bar_date = pd.Timestamp(ts).to_pydatetime()
            if bar_date.tzinfo is None:
                bar_date = bar_date.replace(tzinfo=timezone.utc)
                
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
                                (RR_RATIO * r["risk_cents"])))
                break

    if not updates:
        return 0

    try:
        conn = db_connect()
        try:
            with conn.cursor() as cur:
                for sid, outcome, exit_price, closed_at, pnl in updates:
                    cur.execute("""
                        UPDATE signals
                        SET outcome=%s, exit_price=%s, closed_at=%s, pnl_cents=%s
                        WHERE id=%s
                    """, (outcome, exit_price, closed_at, pnl, sid))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[DB ERROR] update_outcomes: {e}")
        return 0
        
    return len(updates)

# ============================================================
# TELEGRAM NOTIFICATION
# ============================================================
async def send_telegram(signal, lot_size, risk_cents, source):
    bot = Bot(token=TELEGRAM_TOKEN)
    risk_pct = (risk_cents / ACCOUNT_BALANCE) * 100
    reward_cents = risk_cents * RR_RATIO
    
    msg = (
        f"⚡️ *{signal['symbol']} {signal['direction']}* (Scalp)\n"
        f"━━━━━━━━━━━━━━\n"
        f"Entry: `{signal['entry']}`\n"
        f"Stop Loss: `{signal['stop']}`\n"
        f"Take Profit: `{signal['target']}`\n"
        f"Risk: {signal['risk_pips']} pips (1:{int(RR_RATIO)} RR)\n"
        f"ATR({ATR_PERIOD}): {signal['atr_pips']} pips\n"
        f"Lot Size: `{lot_size}` ({source})\n"
        f"Risk ≈ {risk_cents:.1f}¢ ({risk_pct:.1f}%)\n"
        f"Max Win ≈ {reward_cents:.1f}¢\n"
        f"Zone: {signal['zone']['type'].capitalize()} "
        f"({signal['zone']['bottom']:.5f} – {signal['zone']['top']:.5f})\n"
        f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=msg, parse_mode="Markdown")

# ============================================================
# SCAN
# ============================================================
def run_scan():
    db_init()

    try:
        closed = update_open_signals()
        if closed:
            print(f"[OUTCOMES] {closed} open signal(s) closed")
    except Exception as e:
        print(f"[ERROR] outcome update: {type(e).__name__}: {e}")

    try:
        todays_trades = db_get_todays_signal_count()
        if todays_trades >= MAX_DAILY_TRADES:
            print(f"[LIMIT] Already sent {todays_trades} signals today. Max is {MAX_DAILY_TRADES}. Stopping scan.")
            return {"run_id": 0, "pairs_scanned": 0, "signals_sent": 0, "signals_skipped": 0, "errors": 0, "per_pair": []}
    except Exception as e:
        print(f"[ERROR] circuit breaker check: {e}")

    started_at = datetime.now(timezone.utc).isoformat()
    run_id = db_insert_run_start(started_at)
    print(f"[START] {started_at} | Balance={ACCOUNT_BALANCE}¢ | run_id={run_id}")

    pairs_scanned = signals_sent = signals_skipped = errors = 0
    per_pair = []

    for pair in PAIRS:
        signal = None
        diag = {"zones": 0, "sr": 0, "pools": 0, "reasons": []}
        
        try:
            df = fetch_data(pair)
            if df.empty:
                print(f"[SKIP] {pair}: no data")
                diag["reasons"].append("no_data")
            else:
                pairs_scanned += 1
                signal, diag = generate_signal(df, pair)
                db_insert_diagnostic(run_id, pair, diag)

                if signal is None:
                    unique_reasons = ",".join(sorted(set(diag["reasons"]))) or "unknown"
                    print(f"[NONE] {pair}: zones={diag['zones']} sr={diag['sr']} pools={diag['pools']} | {unique_reasons}")
                else:
                    if db_has_open_signal(signal["symbol"], signal["direction"]):
                        print(f"[OPEN] {pair} {signal['direction']}: already open, skipping")
                        signal = None
                    else:
                        lots, risk_cents, source = calculate_lot_size(signal["risk_pips"])
                        if lots == 0.0:
                            signals_skipped += 1
                            print(f"[SKIP] {pair} {signal['direction']}: {source} (stop={signal['risk_pips']} pips)")
                            signal = None
                        else:
                            inserted = db_insert_signal(signal, lots, risk_cents, source)
                            if not inserted:
                                print(f"[DUP] {pair} {signal['direction']}: already sent or DB error")
                                signal = None
                            else:
                                print(f"[SIGNAL] {pair} {signal['direction']} | lots={lots} | risk={risk_cents:.1f}¢")
                                asyncio.run(send_telegram(signal, lots, risk_cents, source))
                                signals_sent += 1

        except Exception as e:
            errors += 1
            print(f"[ERROR] {pair}: {type(e).__name__}: {e}")
            diag["reasons"].append(f"error {type(e).__name__}")
            signal = None
        
        finally:
            per_pair.append((pair, signal, diag))

    db_finish_run(run_id, pairs_scanned, signals_sent, signals_skipped, errors)
    print(f"[DONE] {signals_sent} sent | {signals_skipped} skipped | {errors} errors | run_id={run_id}")

    return {
        "run_id": run_id,
        "pairs_scanned": pairs_scanned,
        "signals_sent": signals_sent,
        "signals_skipped": signals_skipped,
        "errors": errors,
        "per_pair": [
            {
                "pair": p.replace("=X", ""),
                "signal": (
                    {"direction": s["direction"], "entry": s["entry"],
                     "stop": s["stop"], "target": s["target"]}
                    if s else None
                ),
                "reasons": list(set(d.get("reasons", []))), 
                "zones": d.get("zones", 0),
            }
            for p, s, d in per_pair
        ],
    }

if __name__ == "__main__":
    run_scan()