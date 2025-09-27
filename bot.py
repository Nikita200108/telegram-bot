#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot.py — Telegram crypto bot (single-file)
Improvements requested:
 - Bulk/faster price fetching from MEXC (fetch_tickers)
 - Fair price attempt (MEXC endpoints) and fallback to last price
 - Matplotlib backend set to 'Agg' for headless servers (Railway)
 - Fix alert save errors and ensure DB commits are safe
 - Candlestick chart generation robust with fallbacks
 - Autosignals run continuously (RSI/EMA) and do not rely on chart generation
 - All interactions via inline buttons (main menu always available)
 - TELEGRAM_TOKEN resolution from ENV or token.txt (non-blocking for Railway)
 - Extensive logging (Railway logs friendly) and DB logging
 - Graceful error handling: exceptions are logged and don't crash bot
 - Uses ccxt.mexc for exchange connectivity
"""

import os
import sys
import time
import json
import math
import logging
import sqlite3
import threading
import traceback
from datetime import datetime, timedelta
from collections import defaultdict, deque

# ---- Matplotlib safe backend for headless envs (Railway/Gunicorn) ----
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    HAS_MATPLOTLIB = True
except Exception:
    HAS_MATPLOTLIB = False

# ---- Optional libs for indicators and charting ----
try:
    import pandas as pd
    import numpy as np
    HAS_PANDAS = True
except Exception:
    HAS_PANDAS = False

# mplfinance optional for candlesticks
try:
    import mplfinance as mpf
    HAS_MPLFINANCE = True
except Exception:
    HAS_MPLFINANCE = False

# ---- Core network libs ----
try:
    import requests
    import ccxt
    from flask import Flask, request
except Exception as e:
    print("ERROR: missing required packages. Install ccxt, flask, requests. Details:", e)
    raise

# ---- Logging config ----
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("crypto_bot")

# ---------------- TELEGRAM TOKEN RESOLUTION ----------------
def resolve_token():
    """
    Resolve TELEGRAM_TOKEN in order:
      1) ENV TELEGRAM_TOKEN
      2) token.txt (if exists)
      3) interactive prompt (only if tty) and save to token.txt
      4) else raise RuntimeError (expected for Railway/Gunicorn if env missing)
    """
    token = os.getenv("TELEGRAM_TOKEN")
    if token:
        logger.info("TELEGRAM_TOKEN loaded from environment.")
        return token.strip()

    token_path = "token.txt"
    if os.path.exists(token_path):
        try:
            with open(token_path, "r", encoding="utf-8") as f:
                token = f.read().strip()
                if token:
                    logger.info("TELEGRAM_TOKEN loaded from token.txt.")
                    return token
        except Exception:
            logger.exception("Failed to read token.txt")

    # Interactive prompt only if running in TTY
    try:
        if sys.stdin and sys.stdin.isatty():
            token = input("Введите TELEGRAM_TOKEN (BotFather): ").strip()
            if token:
                try:
                    with open(token_path, "w", encoding="utf-8") as f:
                        f.write(token)
                        logger.info("TELEGRAM_TOKEN saved to token.txt")
                except Exception:
                    logger.exception("Failed to save token.txt")
                return token
    except Exception:
        logger.exception("Prompt for token failed")

    logger.error("TELEGRAM_TOKEN not found. Set TELEGRAM_TOKEN env var (Railway: Project > Variables) or create token.txt.")
    raise RuntimeError("TELEGRAM_TOKEN not set")

try:
    TELEGRAM_TOKEN = resolve_token()
except Exception as e:
    logger.exception("Token resolution failed: %s", e)
    # In non-interactive env we must stop; raising so caller sees it.
    raise

BOT_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SEND_MESSAGE = BOT_API + "/sendMessage"
EDIT_MESSAGE = BOT_API + "/editMessageText"
SEND_PHOTO = BOT_API + "/sendPhoto"
ANSWER_CB = BOT_API + "/answerCallbackQuery"
PORT = int(os.getenv("PORT", "8000"))

# ---------------- Configuration ----------------
DB_PATH = os.getenv("DB_PATH", "bot_data.sqlite")
DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "NEAR/USDT"]
PRICE_POLL_INTERVAL = int(os.getenv("PRICE_POLL_INTERVAL", "10"))  # seconds
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "1000"))  # how many price points to keep
AUTOSIGNAL_MIN_INTERVAL = int(os.getenv("AUTOSIGNAL_MIN_INTERVAL", "900"))  # secs between autosignal notifications per sub

PRICE_STEPS = [-10000, -5000, -1000, -100, -10, -1, 1, 10, 100, 1000, 5000, 10000]
TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

# ---------------- Exchange (MEXC) ----------------
exchange = ccxt.mexc({"enableRateLimit": True, "timeout": 10000})

# ---------------- Database init ----------------
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id TEXT PRIMARY KEY,
        created_at DATETIME
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_symbols (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        symbol TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        symbol TEXT,
        is_percent INTEGER DEFAULT 0,
        value REAL,
        is_recurring INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS autosignals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        symbol TEXT,
        timeframe TEXT,
        enabled INTEGER DEFAULT 1,
        last_notified DATETIME
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT,
        price REAL,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        level TEXT,
        message TEXT,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_settings (
        chat_id TEXT PRIMARY KEY,
        signals_enabled INTEGER DEFAULT 1
    )""")
    conn.commit()
    logger.info("DB initialized at %s", DB_PATH)
    return conn, cur

conn, cur = init_db()
db_lock = threading.Lock()

def db_commit():
    try:
        with db_lock:
            conn.commit()
    except Exception:
        logger.exception("DB commit error")

def log_db(level, message):
    try:
        with db_lock:
            cur.execute("INSERT INTO logs (level, message) VALUES (?, ?)", (level.upper(), message))
            conn.commit()
    except Exception:
        logger.exception("log_db write failed")
    getattr(logger, level.lower())(message)

# ---------------- In-memory runtime ----------------
pending_alerts = {}  # temporary flows keyed by chat_id (string)
last_prices = {}     # symbol -> last price
history_cache = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))  # symbol -> deque of (ts, price)

# ---------------- Helper: price fetching (fast) ----------------
def fetch_tickers_bulk(symbols):
    """
    Fetch tickers in bulk using ccxt.fetch_tickers if supported.
    Returns dict symbol->last_price (float or None).
    """
    prices = {}
    try:
        # ccxt expects list of market symbols; ensure they exist in exchange markets
        # fetch_tickers can accept list or no args (all tickers) depending on exchange impl
        tickers = {}
        try:
            tickers = exchange.fetch_tickers(symbols)
        except Exception:
            # fallback: fetch tickers without argument and filter
            all_t = exchange.fetch_tickers()
            tickers = {k: v for k, v in all_t.items() if k in symbols}
        for s in symbols:
            t = tickers.get(s)
            if t:
                last = t.get("last") or t.get("close")
                try:
                    prices[s] = float(last) if last is not None else None
                except Exception:
                    prices[s] = None
            else:
                prices[s] = None
    except Exception:
        logger.exception("fetch_tickers_bulk error")
        # Try per-symbol fallback
        for s in symbols:
            try:
                t = exchange.fetch_ticker(s)
                prices[s] = float(t.get("last") or t.get("close") or 0.0)
            except Exception:
                prices[s] = None
    return prices

def fetch_price_for(symbol):
    """
    Fetch price for single symbol and record history/cache.
    """
    try:
        ticker = exchange.fetch_ticker(symbol)
        price = float(ticker.get("last") or ticker.get("close") or 0.0)
        now_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        last_prices[symbol] = price
        try:
            with db_lock:
                cur.execute("INSERT INTO history (symbol, price, ts) VALUES (?, ?, ?)", (symbol, price, now_ts))
                conn.commit()
        except Exception:
            logger.exception("history save failed")
        history_cache[symbol].append((now_ts, price))
        return price
    except Exception:
        logger.exception("fetch_price_for failed for %s", symbol)
        return None

def fetch_all_user_symbols():
    try:
        with db_lock:
            cur.execute("SELECT DISTINCT symbol FROM user_symbols")
            rows = cur.fetchall()
            syms = set(DEFAULT_SYMBOLS)
            syms.update([r[0] for r in rows])
            return list(syms)
    except Exception:
        logger.exception("fetch_all_user_symbols error")
        return DEFAULT_SYMBOLS.copy()

# ---------------- Helper: MEXC Fair Price (best-effort) ----------------
def fetch_mexc_fair_price(symbol_pair):
    """
    Try to fetch 'fair' or 'mark' price from MEXC using public endpoints.
    Best-effort — may return None if endpoint isn't available.
    """
    try:
        # Normalize symbol: BTC/USDT -> BTCUSDT
        s = symbol_pair.replace("/", "").upper()
        # Candidate endpoints:
        candidates = [
            f"https://contract.mexc.com/api/v1/contract/fair_price/{s}",
            f"https://contract.mexc.com/api/v1/contract/premiumIndex?symbol={s}",
            f"https://contract.mexc.com/api/v1/contract/market/depth?symbol={s}",  # less likely
            f"https://www.mexc.com/open/api/v2/market/ticker?symbol={s}"  # generic
        ]
        for url in candidates:
            try:
                r = requests.get(url, timeout=5)
                if r.status_code != 200:
                    continue
                j = r.json()
                # common patterns
                if isinstance(j, dict):
                    data = j.get("data")
                    if isinstance(data, dict):
                        # fairPrice
                        if "fairPrice" in data:
                            return float(data["fairPrice"])
                        if "markPrice" in data:
                            return float(data["markPrice"])
                        if "lastPrice" in data:
                            return float(data["lastPrice"])
                    # list data
                    if isinstance(data, list) and len(data) > 0:
                        item = data[0]
                        for key in ("fairPrice", "last", "lastPrice", "price"):
                            if key in item:
                                return float(item[key])
                    # some endpoints return ticker inside 'ticker' key
                    if "ticker" in j and isinstance(j["ticker"], dict) and "last" in j["ticker"]:
                        return float(j["ticker"]["last"])
                    # fallback for simple {code:0, data:{...}} patterns
                # else ignore
            except Exception:
                continue
    except Exception:
        logger.exception("fetch_mexc_fair_price top-level exception")
    return None

# ---------------- DB helpers ----------------
def add_user(chat_id):
    try:
        with db_lock:
            cur.execute("INSERT OR IGNORE INTO users (chat_id, created_at) VALUES (?, ?)", (str(chat_id), datetime.utcnow()))
            conn.commit()
    except Exception:
        logger.exception("add_user error")

def get_user_symbols(chat_id):
    try:
        with db_lock:
            cur.execute("SELECT symbol FROM user_symbols WHERE chat_id=?", (str(chat_id),))
            rows = cur.fetchall()
            if rows:
                return [r[0] for r in rows]
    except Exception:
        logger.exception("get_user_symbols error")
    return DEFAULT_SYMBOLS.copy()

def add_user_symbol(chat_id, symbol):
    try:
        with db_lock:
            cur.execute("INSERT INTO user_symbols (chat_id, symbol) VALUES (?, ?)", (str(chat_id), symbol))
            conn.commit()
            return True
    except Exception:
        logger.exception("add_user_symbol error")
        return False

def save_alert(chat_id, symbol, is_percent, value, is_recurring=0):
    """
    Save alert ensuring float conversion and commit; returns inserted id or None.
    """
    try:
        val = float(value)
    except Exception:
        logger.error("save_alert invalid value: %s", value)
        return None
    try:
        with db_lock:
            cur.execute("INSERT INTO alerts (chat_id, symbol, is_percent, value, is_recurring, active) VALUES (?, ?, ?, ?, ?, ?)",
                        (str(chat_id), symbol, int(is_percent), val, int(is_recurring), 1))
            conn.commit()
            aid = cur.lastrowid
            logger.info("Saved alert id=%s for %s %s", aid, chat_id, symbol)
            return aid
    except Exception:
        logger.exception("save_alert error")
        return None

def list_alerts(chat_id):
    try:
        with db_lock:
            cur.execute("SELECT id, symbol, is_percent, value, is_recurring, active FROM alerts WHERE chat_id=? ORDER BY id DESC", (str(chat_id),))
            return cur.fetchall()
    except Exception:
        logger.exception("list_alerts error")
        return []

def delete_alert(alert_id, chat_id=None):
    try:
        with db_lock:
            if chat_id:
                cur.execute("DELETE FROM alerts WHERE id=? AND chat_id=?", (int(alert_id), str(chat_id)))
            else:
                cur.execute("DELETE FROM alerts WHERE id=?", (int(alert_id),))
            conn.commit()
    except Exception:
        logger.exception("delete_alert error")

def upsert_autosignal(chat_id, symbol, timeframe, enabled=1):
    try:
        with db_lock:
            cur.execute("SELECT id FROM autosignals WHERE chat_id=? AND symbol=? AND timeframe=?", (str(chat_id), symbol, timeframe))
            r = cur.fetchone()
            if r:
                cur.execute("UPDATE autosignals SET enabled=? WHERE id=?", (int(enabled), r[0]))
            else:
                cur.execute("INSERT INTO autosignals (chat_id, symbol, timeframe, enabled) VALUES (?, ?, ?, ?)",
                            (str(chat_id), symbol, timeframe, int(enabled)))
            conn.commit()
    except Exception:
        logger.exception("upsert_autosignal error")

def list_autosignals(chat_id):
    try:
        with db_lock:
            cur.execute("SELECT id, symbol, timeframe, enabled FROM autosignals WHERE chat_id=?", (str(chat_id),))
            return cur.fetchall()
    except Exception:
        logger.exception("list_autosignals error")
        return []

def delete_autosignal(aid):
    try:
        with db_lock:
            cur.execute("DELETE FROM autosignals WHERE id=?", (int(aid),))
            conn.commit()
    except Exception:
        logger.exception("delete_autosignal error")

# ---------------- Telegram helpers ----------------
def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": str(chat_id), "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(SEND_MESSAGE, data=payload, timeout=10)
        if not r.ok:
            logger.warning("send_message failed: %s %s", r.status_code, r.text)
        return r.json()
    except Exception:
        logger.exception("send_message exception")
        return {}

def edit_message(chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {"chat_id": str(chat_id), "message_id": int(message_id), "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(EDIT_MESSAGE, data=payload, timeout=10)
        if not r.ok:
            logger.warning("edit_message failed: %s %s", r.status_code, r.text)
        return r.json()
    except Exception:
        logger.exception("edit_message exception")
        return {}

def answer_callback(callback_query_id, text=None):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        requests.post(ANSWER_CB, data=payload, timeout=5)
    except Exception:
        pass

def send_photo_bytes(chat_id, buf, caption=None):
    try:
        # buf: io.BytesIO or bytes
        if hasattr(buf, "getvalue"):
            files = {"photo": ("chart.png", buf.getvalue())}
        else:
            files = {"photo": ("chart.png", buf)}
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
            data["parse_mode"] = "HTML"
        r = requests.post(SEND_PHOTO, files=files, data=data, timeout=30)
        if not r.ok:
            logger.warning("send_photo failed: %s %s", r.status_code, r.text)
        return r.json()
    except Exception:
        logger.exception("send_photo exception")
        return {}

# ---------------- Keyboards ----------------
def kb_main_menu():
    return {
        "inline_keyboard": [
            [{"text": "💰 Цена (MEXC)", "callback_data": "price_all"}, {"text": "📊 График", "callback_data": "chart_menu"}],
            [{"text": "🔔 Алерты", "callback_data": "alerts_menu"}, {"text": "🤖 Автосигналы", "callback_data": "autosignals_menu"}],
            [{"text": "⚙️ Настройки", "callback_data": "settings_menu"}]
        ]
    }

def kb_chart_symbols(chat_id):
    syms = get_user_symbols(chat_id)
    rows = []
    row = []
    for i, s in enumerate(syms):
        row.append({"text": s.split("/")[0], "callback_data": f"chart_symbol_{s}"})
        if (i+1) % 3 == 0:
            rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([{"text":"🏠 Главное меню","callback_data":"main"}])
    return {"inline_keyboard": rows}

def kb_timeframe_select(symbol):
    rows = []
    tfs = [["1m","5m","15m"], ["30m","1h","4h"], ["1d"]]
    for row in tfs:
        rows.append([{"text": tf, "callback_data": f"chart_tf_{symbol}_{tf}"} for tf in row])
    rows.append([{"text":"🏠 Главное меню","callback_data":"main"}])
    return {"inline_keyboard": rows}

def kb_alerts_menu():
    return {
        "inline_keyboard": [
            [{"text":"➕ Добавить алерт","callback_data":"add_alert"}, {"text":"📋 Мои алерты","callback_data":"list_alerts"}],
            [{"text":"🏠 Главное меню","callback_data":"main"}]
        ]
    }

def kb_alert_symbol_select(chat_id):
    syms = get_user_symbols(chat_id)
    rows = []
    row = []
    for i, s in enumerate(syms):
        row.append({"text": s.split("/")[0], "callback_data": f"alert_symbol_{s}"})
        if (i+1) % 3 == 0:
            rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([{"text":"🏠 Главное меню","callback_data":"main"}])
    return {"inline_keyboard": rows}

def kb_alert_type():
    return {"inline_keyboard":[
        [{"text":"%","callback_data":"alert_type_percent"}, {"text":"$","callback_data":"alert_type_usd"}],
        [{"text":"🏠 Главное меню","callback_data":"main"}]
    ]}

def kb_alerts_list(alerts):
    rows = []
    for a in alerts:
        aid, sym, is_pct, val, rec, active = a
        label = f"{sym} {'%' if is_pct else '$'}{val} {'🔁' if rec else ''}"
        rows.append([{"text": label, "callback_data": f"alert_item_{aid}"}, {"text":"❌","callback_data":f"alert_del_{aid}"}])
    rows.append([{"text":"⬅️ Назад","callback_data":"alerts_menu"}, {"text":"🏠 Главное меню","callback_data":"main"}])
    return {"inline_keyboard": rows}

def kb_autosignals_menu(chat_id):
    syms = get_user_symbols(chat_id)
    rows = []
    row = []
    for i, s in enumerate(syms):
        row.append({"text": s.split("/")[0], "callback_data": f"autosignal_select_{s}"})
        if (i+1) % 3 == 0:
            rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([{"text":"📋 Мои автосигналы","callback_data":"list_autosignals"}])
    rows.append([{"text":"🏠 Главное меню","callback_data":"main"}])
    return {"inline_keyboard": rows}

def kb_timeframe_for_autosignal(symbol):
    rows = []
    tfs = [["1m","5m","15m"], ["30m","1h","4h"], ["1d"]]
    for row in tfs:
        rows.append([{"text": tf, "callback_data": f"autosignal_tf_{symbol}_{tf}"} for tf in row])
    rows.append([{"text":"🏠 Главное меню","callback_data":"main"}])
    return {"inline_keyboard": rows}

def kb_autosignals_list(rows_data):
    kb = {"inline_keyboard":[]}
    for r in rows_data:
        aid, sym, tf, enabled = r
        kb["inline_keyboard"].append([{"text": f"{sym} {tf} {'✅' if enabled else '❌'}", "callback_data": f"autosignal_item_{aid}"}, {"text":"❌","callback_data":f"autosignal_del_{aid}"}])
    kb["inline_keyboard"].append([{"text":"⬅️ Назад","callback_data":"autosignals_menu"}, {"text":"🏠 Главное меню","callback_data":"main"}])
    return kb

# ---------------- Alerts & Autosignals logic ----------------
def check_alerts():
    """
    Evaluate active alerts against last_prices/history_cache.
    alerts may be percent-based (relative to previous price) or absolute (value crossing).
    """
    try:
        with db_lock:
            cur.execute("SELECT id, chat_id, symbol, is_percent, value, is_recurring, active FROM alerts WHERE active=1")
            rows = cur.fetchall()
    except Exception:
        logger.exception("check_alerts db read failed")
        rows = []

    for r in rows:
        aid, chat_id, symbol, is_pct, value, is_rec, active = r
        cur_price = last_prices.get(symbol)
        if cur_price is None:
            continue
        triggered = False
        hist = list(history_cache.get(symbol, []))
        prev_price = hist[-2][1] if len(hist) >= 2 else None

        if is_pct:
            # percent change from previous candle/point
            if prev_price:
                try:
                    change_pct = (cur_price - prev_price) / prev_price * 100 if prev_price != 0 else 0
                    if abs(change_pct) >= abs(value):
                        triggered = True
                except Exception:
                    logger.exception("percent change calc failed")
        else:
            # absolute crossing
            try:
                v = float(value)
                if prev_price is not None:
                    if (prev_price < v and cur_price >= v) or (prev_price > v and cur_price <= v):
                        triggered = True
                else:
                    if cur_price >= v:
                        triggered = True
            except Exception:
                logger.exception("absolute alert value parse failed for %s", value)

        if triggered:
            try:
                send_message(chat_id, f"🔔 Alert сработал: {symbol} {'%' if is_pct else '$'}{value} — текущая {cur_price}$")
                log_db("info", f"Alert fired for {chat_id} {symbol} {value}{'%' if is_pct else '$'}")
            except Exception:
                logger.exception("send alert message failed")
            if not is_rec:
                try:
                    with db_lock:
                        cur.execute("UPDATE alerts SET active=0 WHERE id=?", (aid,))
                        conn.commit()
                except Exception:
                    logger.exception("deactivate alert failed")

# ---------------- Autosignals (RSI/EMA) ----------------
def calculate_rsi(prices, period=14):
    if not HAS_PANDAS:
        return []
    s = pd.Series(prices)
    delta = s.diff().dropna()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ma_up = up.rolling(window=period).mean()
    ma_down = down.rolling(window=period).mean()
    rs = ma_up / ma_down
    rsi = 100 - 100/(1+rs)
    return rsi.fillna(50).tolist()

def calculate_ema(prices, span):
    if not HAS_PANDAS:
        return []
    s = pd.Series(prices)
    return s.ewm(span=span, adjust=False).mean().tolist()

def fetch_ohlcv_safe(symbol, timeframe="1m", limit=200):
    try:
        return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception:
        logger.exception("fetch_ohlcv failed for %s %s", symbol, timeframe)
        return []

def check_autosignals():
    """
    Evaluate autosignals subscriptions: for each subscription, fetch OHLCV and compute RSI/EMA.
    Send notification if condition met and last_notified older than threshold.
    """
    try:
        with db_lock:
            cur.execute("SELECT id, chat_id, symbol, timeframe, enabled, last_notified FROM autosignals WHERE enabled=1")
            rows = cur.fetchall()
    except Exception:
        logger.exception("list_autosignals error")
        rows = []

    for r in rows:
        aid, chat_id, symbol, timeframe, enabled, last_notified = r
        ohlcv = fetch_ohlcv_safe(symbol, timeframe=timeframe, limit=200)
        if not ohlcv:
            continue
        closes = [c[4] for c in ohlcv]
        if len(closes) < 20:
            continue
        signal_msg = None
        try:
            if HAS_PANDAS:
                rsi = calculate_rsi(closes, 14)[-1]
                ema5 = calculate_ema(closes, 5)[-1]
                ema20 = calculate_ema(closes, 20)[-1]
                # RSI signals
                if rsi > 70:
                    signal_msg = f"🔻 {symbol} {timeframe}: RSI {rsi:.1f} (overbought) — consider SHORT"
                elif rsi < 30:
                    signal_msg = f"🔺 {symbol} {timeframe}: RSI {rsi:.1f} (oversold) — consider LONG"
                # EMA cross detection (use last two)
                ema5_series = calculate_ema(closes, 5)
                ema20_series = calculate_ema(closes, 20)
                if len(ema5_series) >= 2 and len(ema20_series) >= 2:
                    if ema5_series[-2] <= ema20_series[-2] and ema5_series[-1] > ema20_series[-1]:
                        signal_msg = f"🔺 {symbol} {timeframe}: EMA5 crossed above EMA20 (LONG)"
                    elif ema5_series[-2] >= ema20_series[-2] and ema5_series[-1] < ema20_series[-1]:
                        signal_msg = f"🔻 {symbol} {timeframe}: EMA5 crossed below EMA20 (SHORT)"
        except Exception:
            logger.exception("indicator calc failed")

        should_notify = True
        if last_notified:
            try:
                last_dt = datetime.strptime(last_notified, "%Y-%m-%d %H:%M:%S")
                if datetime.utcnow() - last_dt < timedelta(seconds=AUTOSIGNAL_MIN_INTERVAL):
                    should_notify = False
            except Exception:
                should_notify = True

        if signal_msg and should_notify:
            try:
                send_message(chat_id, f"🤖 Автосигнал: {signal_msg}")
                with db_lock:
                    cur.execute("UPDATE autosignals SET last_notified=? WHERE id=?", (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), aid))
                    conn.commit()
                log_db("info", f"Autosignal sent to {chat_id}: {signal_msg}")
            except Exception:
                logger.exception("notify autosignal failed")

# ---------------- Chart building ----------------
def build_candlestick_chart(symbol, timeframe="1h", limit=200):
    """
    Build candlestick chart as bytes (BytesIO) and return (buf, None) or (None, error_message)
    Uses mplfinance if available; otherwise manual drawing with matplotlib.
    """
    try:
        if not HAS_PANDAS or not HAS_MATPLOTLIB:
            return None, "pandas or matplotlib not installed on server"

        ohlcv = fetch_ohlcv_safe(symbol, timeframe=timeframe, limit=limit)
        if not ohlcv:
            return None, "No OHLCV data from exchange"

        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
        df['date'] = pd.to_datetime(df['ts'], unit='ms')
        df.set_index('date', inplace=True)
        df = df[["open","high","low","close","volume"]]

        import io
        buf = io.BytesIO()
        if HAS_MPLFINANCE:
            # mplfinance can save to BytesIO by passing savefig=dict
            try:
                mpf.plot(df, type='candle', style='charles', volume=True, savefig=dict(fname=buf, dpi=150, bbox_inches="tight"))
                buf.seek(0)
                return buf, None
            except Exception:
                logger.exception("mplfinance plot failed; falling back")
        # Fallback manual:
        fig, ax = plt.subplots(figsize=(10,6))
        df_reset = df.reset_index()
        dates = mdates.date2num(df_reset['date'].to_pydatetime())
        width = max(0.0005, (dates[1] - dates[0]) * 0.6) if len(dates) > 1 else 0.0005
        for idx, row in df_reset.iterrows():
            color = 'green' if row['close'] >= row['open'] else 'red'
            ax.vlines(dates[idx], row['low'], row['high'], color='black', linewidth=0.5)
            ax.add_patch(plt.Rectangle((dates[idx]-width/2, min(row['open'], row['close'])), width, abs(row['open']-row['close']), color=color))
        ax.xaxis_date()
        ax.set_title(f"{symbol} {timeframe}")
        ax.grid(True)
        plt.tight_layout()
        fig.savefig(buf, format='png', dpi=150)
        plt.close(fig)
        buf.seek(0)
        return buf, None
    except Exception:
        logger.exception("build_candlestick_chart error")
        return None, "chart generation failed"

# ---------------- Background workers ----------------
def price_poll_worker():
    logger.info("Price poll worker started (interval %s s)", PRICE_POLL_INTERVAL)
    while True:
        try:
            syms = fetch_all_user_symbols()
            # bulk fetch
            prices = fetch_tickers_bulk(syms)
            now_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            for s, p in prices.items():
                if p is None:
                    # try single fetch fallback
                    try:
                        p = fetch_price_for(s)
                    except Exception:
                        p = None
                if p is not None:
                    last_prices[s] = p
                    try:
                        with db_lock:
                            cur.execute("INSERT INTO history (symbol, price, ts) VALUES (?, ?, ?)", (s, float(p), now_ts))
                            conn.commit()
                    except Exception:
                        logger.exception("history insert failed")
                    history_cache[s].append((now_ts, float(p)))
            # Now process alerts based on updated last_prices/history_cache
            check_alerts()
        except Exception:
            logger.exception("price_poll_worker exception")
        time.sleep(PRICE_POLL_INTERVAL)

def autosignal_worker():
    logger.info("Autosignal worker started")
    while True:
        try:
            check_autosignals()
        except Exception:
            logger.exception("autosignal_worker exception")
        time.sleep(30)

def safe_thread(target, name):
    def wrapper():
        logger.info("Thread %s launched", name)
        while True:
            try:
                target()
            except Exception:
                logger.exception("Thread %s crashed; restarting in 5s", name)
                time.sleep(5)
    t = threading.Thread(target=wrapper, daemon=True, name=name)
    t.start()
    return t

# start background threads
safe_thread(price_poll_worker, "price_poll_worker")
safe_thread(autosignal_worker, "autosignal_worker")

# ---------------- Flask webhook ----------------
app = Flask(__name__)

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        data = request.get_json() or {}
        # Callback query (button)
        if "callback_query" in data:
            cb = data["callback_query"]
            cb_id = cb.get("id")
            user = cb.get("from", {})
            user_id = user.get("id")
            message = cb.get("message", {})
            chat = message.get("chat", {}) or {}
            chat_id = chat.get("id") or user_id
            msg_id = message.get("message_id")
            action = cb.get("data", "")

            add_user(chat_id)
            log_db("info", f"callback {action} from {chat_id}")

            # Navigation
            if action == "main":
                edit_message(chat_id, msg_id, "🏠 Главное меню", kb_main_menu())
                answer_callback(cb_id)
                return {"ok": True}

            # Price: send all prices (fast)
            if action == "price_all":
                syms = fetch_all_user_symbols()
                prices = fetch_tickers_bulk(syms)
                lines = []
                for s in syms:
                    last = prices.get(s) or last_prices.get(s) or fetch_price_for(s)
                    fair = fetch_mexc_fair_price(s)
                    lines.append(f"{s}: last {last if last is not None else 'N/A'}$, fair {fair if fair is not None else 'N/A'}$")
                send_message(chat_id, "💰 Цены (источник: MEXC):\n" + "\n".join(lines), kb_main_menu())
                answer_callback(cb_id)
                return {"ok": True}

            # Chart menu
            if action == "chart_menu":
                edit_message(chat_id, msg_id, "📊 Выберите монету для графика:", kb_chart_symbols(chat_id))
                answer_callback(cb_id)
                return {"ok": True}

            if action.startswith("chart_symbol_"):
                symbol = action.split("chart_symbol_",1)[1]
                edit_message(chat_id, msg_id, f"📊 {symbol} — выберите таймфрейм:", kb_timeframe_select(symbol))
                answer_callback(cb_id)
                return {"ok": True}

            if action.startswith("chart_tf_"):
                try:
                    _, rest = action.split("chart_tf_",1)
                    symbol, tf = rest.rsplit("_",1)
                except Exception:
                    answer_callback(cb_id, "Ошибка парсинга")
                    return {"ok": True}
                send_message(chat_id, f"📈 Строю график {symbol} {tf} ...")
                buf, err = build_candlestick_chart(symbol, timeframe=tf, limit=200)
                if buf:
                    send_photo_bytes(chat_id, buf, caption=f"{symbol} {tf} свечи (MEXC)")
                else:
                    send_message(chat_id, f"Не удалось построить график: {err}")
                answer_callback(cb_id)
                return {"ok": True}

            # Alerts menu
            if action == "alerts_menu":
                edit_message(chat_id, msg_id, "🔔 Меню алертов", kb_alerts_menu())
                answer_callback(cb_id)
                return {"ok": True}

            if action == "add_alert":
                pending_alerts[str(chat_id)] = {"state":"awaiting_alert_type"}
                edit_message(chat_id, msg_id, "➕ Выберите тип алерта", kb_alert_type())
                answer_callback(cb_id)
                return {"ok": True}

            if action == "alert_type_percent":
                pending_alerts[str(chat_id)].update({"is_percent":1, "state":"awaiting_alert_value"})
                send_message(chat_id, "Введите значение в процентах (например: 2.5) и затем выберите монету кнопкой ниже.", kb_alert_symbol_select(chat_id))
                answer_callback(cb_id)
                return {"ok": True}

            if action == "alert_type_usd":
                pending_alerts[str(chat_id)].update({"is_percent":0, "state":"awaiting_alert_value"})
                send_message(chat_id, "Введите значение в $ (например: 50) и затем выберите монету кнопкой ниже.", kb_alert_symbol_select(chat_id))
                answer_callback(cb_id)
                return {"ok": True}

            if action.startswith("alert_symbol_"):
                symbol = action.split("alert_symbol_",1)[1]
                key = str(chat_id)
                if key in pending_alerts and pending_alerts[key].get("state") == "awaiting_alert_value":
                    val = pending_alerts[key].get("value")
                    is_pct = pending_alerts[key].get("is_percent", 0)
                    if val is None:
                        send_message(chat_id, f"Вы ещё не ввели значение. Введите число ({'%' if is_pct else '$'}) в текстовом сообщении, затем нажмите монету.")
                        answer_callback(cb_id)
                        return {"ok": True}
                    alert_id = save_alert(chat_id, symbol, is_pct, val, is_recurring=0)
                    if alert_id:
                        send_message(chat_id, f"✅ Алерт сохранён: {symbol} {'%' if is_pct else '$'}{val}", kb_main_menu())
                        del pending_alerts[key]
                    else:
                        send_message(chat_id, "Ошибка при сохранении алерта. Проверьте вводимое число.")
                else:
                    pending_alerts[str(chat_id)] = {"state":"awaiting_value_for_symbol", "symbol": symbol}
                    send_message(chat_id, f"Вы выбрали {symbol}. Теперь введите значение (например: 50 или 2.5) в текстовом сообщении.")
                answer_callback(cb_id)
                return {"ok": True}

            if action == "list_alerts":
                rows = list_alerts(chat_id)
                if not rows:
                    edit_message(chat_id, msg_id, "У вас нет алертов.", kb_alerts_menu())
                else:
                    edit_message(chat_id, msg_id, "📋 Ваши алерты:", kb_alerts_list(rows))
                answer_callback(cb_id)
                return {"ok": True}

            if action.startswith("alert_item_"):
                aid = int(action.split("_")[-1])
                with db_lock:
                    cur.execute("SELECT id, symbol, is_percent, value, is_recurring FROM alerts WHERE id=?", (aid,))
                    row = cur.fetchone()
                if row:
                    _id, sym, is_pct, val, rec = row
                    txt = f"Alert #{_id}\n{sym}\nТип: {'%' if is_pct else '$'}\nЗначение: {val}\nПостоянный: {'Да' if rec else 'Нет'}"
                    kb = {"inline_keyboard":[[{"text":"✏ Изменить","callback_data":f"alert_edit_{_id}"}, {"text":"❌ Удалить","callback_data":f"alert_del_{_id}"}],
                                               [{"text":"⬅️ Назад","callback_data":"list_alerts"}, {"text":"🏠 Главное меню","callback_data":"main"}]]}
                    edit_message(chat_id, msg_id, txt, kb)
                else:
                    edit_message(chat_id, msg_id, "Алерт не найден.", kb_alerts_menu())
                answer_callback(cb_id)
                return {"ok": True}

            if action.startswith("alert_del_"):
                aid = int(action.split("_")[-1])
                delete_alert(aid, chat_id)
                edit_message(chat_id, msg_id, "✅ Алерт удалён.", kb_alerts_menu())
                answer_callback(cb_id)
                return {"ok": True}

            if action.startswith("alert_edit_"):
                aid = int(action.split("_")[-1])
                pending_alerts[str(chat_id)] = {"state":"awaiting_edit_value", "edit_aid": aid}
                send_message(chat_id, "Введите новое значение для алерта (число):", {"inline_keyboard":[[{"text":"🏠 Главное меню","callback_data":"main"}]]})
                answer_callback(cb_id)
                return {"ok": True}

            # Autosignals
            if action == "autosignals_menu":
                edit_message(chat_id, msg_id, "🤖 Автосигналы: выберите монету", kb_autosignals_menu(chat_id))
                answer_callback(cb_id)
                return {"ok": True}

            if action.startswith("autosignal_select_"):
                symbol = action.split("autosignal_select_",1)[1]
                edit_message(chat_id, msg_id, f"🤖 {symbol} — выберите таймфрейм для автосигнала:", kb_timeframe_for_autosignal(symbol))
                answer_callback(cb_id)
                return {"ok": True}

            if action.startswith("autosignal_tf_"):
                try:
                    _, rest = action.split("autosignal_tf_",1)
                    symbol, tf = rest.rsplit("_",1)
                except Exception:
                    answer_callback(cb_id, "Ошибка выбора")
                    return {"ok": True}
                upsert_autosignal(chat_id, symbol, tf, enabled=1)
                send_message(chat_id, f"✅ Подписка автосигналов: {symbol} {tf}", kb_main_menu())
                answer_callback(cb_id)
                return {"ok": True}

            if action == "list_autosignals":
                rows = list_autosignals(chat_id)
                if not rows:
                    edit_message(chat_id, msg_id, "У вас нет автосигналов.", kb_autosignals_menu(chat_id))
                else:
                    edit_message(chat_id, msg_id, "Ваши автосигналы:", kb_autosignals_list(rows))
                answer_callback(cb_id)
                return {"ok": True}

            if action.startswith("autosignal_item_"):
                aid = int(action.split("_")[-1])
                with db_lock:
                    cur.execute("SELECT enabled, symbol, timeframe FROM autosignals WHERE id=?", (aid,))
                    r = cur.fetchone()
                    if r:
                        enabled, symbol, tf = r[0], r[1], r[2]
                        newv = 0 if enabled else 1
                        cur.execute("UPDATE autosignals SET enabled=? WHERE id=?", (newv, aid))
                        conn.commit()
                        send_message(chat_id, f"Автосигнал {symbol} {tf} {'включен' if newv else 'выключен'}", kb_main_menu())
                answer_callback(cb_id)
                return {"ok": True}

            if action.startswith("autosignal_del_"):
                aid = int(action.split("_")[-1])
                delete_autosignal(aid)
                edit_message(chat_id, msg_id, "✅ Автосигнал удалён.", kb_autosignals_menu(chat_id))
                answer_callback(cb_id)
                return {"ok": True}

            # Settings
            if action == "settings_menu":
                edit_message(chat_id, msg_id, "⚙️ Настройки (пока пусто)", {"inline_keyboard":[[{"text":"🏠 Главное меню","callback_data":"main"}]]})
                answer_callback(cb_id)
                return {"ok": True}

            # Unknown callback
            answer_callback(cb_id, "Неизвестная кнопка")
            logger.warning("Unknown callback: %s", action)
            return {"ok": True}

        # ---- Message handling (text) ----
        msg = data.get("message") or data.get("edited_message")
        if not msg:
            return {"ok": True}
        chat_id = msg.get("chat",{}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id:
            return {"ok": True}
        add_user(chat_id)
        log_db("info", f"msg from {chat_id}: {text}")

        # Pending flow handling
        key = str(chat_id)
        if key in pending_alerts:
            st = pending_alerts[key].get("state")
            if st == "awaiting_alert_value":
                try:
                    val = float(text.replace(",","."))
                    pending_alerts[key]["value"] = val
                    send_message(chat_id, f"Значение {val} сохранено. Теперь выберите монету кнопкой ниже.", kb_alert_symbol_select(chat_id))
                except Exception:
                    send_message(chat_id, "Неверное число. Введите число (например: 50 или 2.5).")
                return {"ok": True}
            if st == "awaiting_value_for_symbol":
                try:
                    val = float(text.replace(",","."))
                    symbol = pending_alerts[key].get("symbol")
                    is_pct = pending_alerts[key].get("is_percent", 0)
                    aid = save_alert(chat_id, symbol, is_pct, val, is_recurring=0)
                    if aid:
                        send_message(chat_id, f"✅ Алерт сохранён: {symbol} {'%' if is_pct else '$'}{val}", kb_main_menu())
                        del pending_alerts[key]
                    else:
                        send_message(chat_id, "Ошибка при сохранении алерта. Проверьте введённое число.")
                except Exception:
                    send_message(chat_id, "Неверное число. Введите число снова.")
                return {"ok": True}
            if st == "awaiting_edit_value":
                try:
                    newval = float(text.replace(",","."))
                    aid = pending_alerts[key].get("edit_aid")
                    with db_lock:
                        cur.execute("UPDATE alerts SET value=? WHERE id=?", (newval, aid))
                        conn.commit()
                    send_message(chat_id, f"✅ Алерт #{aid} обновлён на {newval}", kb_main_menu())
                    del pending_alerts[key]
                except Exception:
                    logger.exception("edit alert value failed")
                    send_message(chat_id, "Ошибка при обновлении алерта.")
                return {"ok": True}

        # Commands fallback
        if text.startswith("/start"):
            send_message(chat_id, "👋 Привет! Я крипто-бот (цены и сигналы от MEXC). Управление через кнопки.", kb_main_menu())
            return {"ok": True}

        if text.startswith("/price"):
            parts = text.split()
            if len(parts) == 2:
                s = parts[1].upper()
                if "/" not in s:
                    s = s + "/USDT"
                # fast attempt via cache or single fetch
                last = last_prices.get(s) or fetch_price_for(s)
                fair = fetch_mexc_fair_price(s)
                send_message(chat_id, f"{s}: last {last if last is not None else 'N/A'}$, fair {fair if fair is not None else 'N/A'}$", kb_main_menu())
            else:
                send_message(chat_id, "Использование: /price SYMBOL (например /price BTC)", kb_main_menu())
            return {"ok": True}

        if text.startswith("/chart"):
            parts = text.split()
            if len(parts) >= 2:
                sym = parts[1].upper()
                if "/" not in sym:
                    sym = sym + "/USDT"
                tf = parts[2] if len(parts) >= 3 else "1h"
                send_message(chat_id, f"Строю график {sym} {tf} ...")
                buf, err = build_candlestick_chart(sym, timeframe=tf, limit=200)
                if buf:
                    send_photo_bytes(chat_id, buf, caption=f"{sym} {tf} свечи")
                else:
                    send_message(chat_id, f"Не удалось: {err}")
            else:
                send_message(chat_id, "Использование: /chart SYMBOL [timeframe]\nНапример: /chart BTC 1h")
            return {"ok": True}

        if text.startswith("/alerts"):
            rows = list_alerts(chat_id)
            if not rows:
                send_message(chat_id, "У вас нет алертов.", kb_alerts_menu())
            else:
                lines = []
                for r in rows:
                    aid, sym, is_pct, val, rec, active = r
                    lines.append(f"{aid}: {sym} {'%' if is_pct else '$'}{val} {'(on)' if active else '(off)'}")
                send_message(chat_id, "📋 Мои алерты:\n" + "\n".join(lines), kb_alerts_menu())
            return {"ok": True}

        # Show main menu by default
        send_message(chat_id, "Выберите действие:", kb_main_menu())
        return {"ok": True}

    except Exception:
        logger.exception("telegram_webhook exception")
        return {"ok": True}

# ---------------- Run Flask app ----------------
if __name__ == "__main__":
    logger.info("Bot started. Webhook endpoint: /%s", TELEGRAM_TOKEN)
    logger.info("If running on Railway, set TELEGRAM_TOKEN variable and set webhook to https://<your-app>/%s", TELEGRAM_TOKEN)
    app.run(host="0.0.0.0", port=PORT)
