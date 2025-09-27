#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Полный Telegram crypto bot (single-file).
Ключевые функции:
 - Webhook (Flask) — быстрый ответ, не блокирует
 - MEXC via ccxt (bulk fetch / per-symbol fallback)
 - Частое кэширование цен (по умолчанию 2s — осторожно с rate limits!)
 - SQLite persistence (users, user_symbols, alerts, autosignals, history, logs)
 - Inline buttons (главное меню, графики, алерты, автосигналы)
 - Автосигналы: RSI/EMA + EMA cross — непрерывно
 - Candlestick chart (mplfinance or manual matplotlib fallback)
 - Logging to console + DB; ошибки не падают
 - TELEGRAM_TOKEN: берётся из ENV TELEGRAM_TOKEN или token.txt (no input() for Railway)
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

# matplotlib backend for headless
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    HAS_MATPLOTLIB = True
except Exception:
    HAS_MATPLOTLIB = False

# pandas & numpy optional but recommended
try:
    import pandas as pd
    import numpy as np
    HAS_PANDAS = True
except Exception:
    HAS_PANDAS = False

# mplfinance optional for candlestick plotting
try:
    import mplfinance as mpf
    HAS_MPLFINANCE = True
except Exception:
    HAS_MPLFINANCE = False

# network & exchange
try:
    import requests
    import ccxt
    from flask import Flask, request
except Exception as e:
    print("Missing critical package. Install ccxt, flask, requests. Exception:", e)
    raise

# ---------------- Config & logging ----------------
# Настройки: можно переопределить через переменные окружения
PRICE_POLL_INTERVAL = float(os.getenv("PRICE_POLL_INTERVAL", "2.0"))  # seconds (DEFAULT 2s — осторожно)
AUTOSIGNAL_INTERVAL = float(os.getenv("AUTOSIGNAL_INTERVAL", "10.0"))  # seconds for autosignal checks
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "1000"))
PRICE_CACHE_SYMBOLS_BASE = os.getenv("PRICE_CACHE_SYMBOLS_BASE", "BTC/USDT,ETH/USDT,SOL/USDT,NEAR/USDT")
DEFAULT_SYMBOLS = [s.strip() for s in PRICE_CACHE_SYMBOLS_BASE.split(",") if s.strip()]
TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

# Logging to stdout (Railway will capture these)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("cryptobot")

# ---------------- Token resolution (ENV or token.txt) ----------------
def resolve_token():
    # 1) ENV
    token = os.getenv("TELEGRAM_TOKEN")
    if token:
        logger.info("TELEGRAM_TOKEN loaded from environment.")
        return token.strip()
    # 2) token.txt
    token_path = "token.txt"
    if os.path.exists(token_path):
        try:
            with open(token_path, "r", encoding="utf-8") as f:
                t = f.read().strip()
                if t:
                    logger.info("TELEGRAM_TOKEN loaded from token.txt.")
                    return t
        except Exception:
            logger.exception("Failed to read token.txt")
    # 3) no interactive prompt (Railway cannot provide input)
    logger.error("TELEGRAM_TOKEN not set. Please set TELEGRAM_TOKEN environment variable (Railway: Project -> Variables).")
    raise RuntimeError("TELEGRAM_TOKEN not set")

TELEGRAM_TOKEN = resolve_token()
BOT_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SEND_MESSAGE = BOT_API + "/sendMessage"
EDIT_MESSAGE = BOT_API + "/editMessageText"
SEND_PHOTO = BOT_API + "/sendPhoto"
ANSWER_CB = BOT_API + "/answerCallbackQuery"
PORT = int(os.getenv("PORT", "8000"))
DB_PATH = os.getenv("DB_PATH", "bot_data.sqlite")

# ---------------- Exchange init ----------------
exchange = ccxt.mexc({"enableRateLimit": True, "timeout": 10000})

# ---------------- Database init ----------------
def init_db(path=DB_PATH):
    conn = sqlite3.connect(path, check_same_thread=False)
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
    logger.info("DB initialized at %s", path)
    return conn, cur

conn, cur = init_db(DB_PATH)
db_lock = threading.Lock()

def db_commit():
    try:
        with db_lock:
            conn.commit()
    except Exception:
        logger.exception("DB commit failed")

def log_db(level, message):
    try:
        with db_lock:
            cur.execute("INSERT INTO logs (level, message) VALUES (?, ?)", (level.upper(), message))
            conn.commit()
    except Exception:
        logger.exception("write log_db failed")
    getattr(logger, level.lower())(message)

# ---------------- In-memory caches & state ----------------
price_cache = {}               # symbol -> last price
history_cache = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))  # symbol -> deque of (ts, price)
pending_flows = {}             # chat_id -> flow state (for multi-step flows)
watch_symbols = set(DEFAULT_SYMBOLS)  # symbols to bulk fetch (updated when users add symbols)
watch_lock = threading.Lock()

# ---------------- Utilities: exchange price fetching ----------------
def fetch_tickers_bulk(symbols):
    """
    Try fetch_tickers for a symbol list. Returns dict symbol->price (float or None).
    Uses fetch_tickers if possible; fallback per-symbol fetch.
    """
    out = {}
    try:
        # ccxt fetch_tickers may accept list (some exchanges), but mexc supports fetch_tickers() full
        tickers = {}
        try:
            tickers = exchange.fetch_tickers()  # fetch all, then filter (may be heavy)
            # filter:
            for s in symbols:
                t = tickers.get(s)
                if t:
                    last = t.get("last") or t.get("close")
                    out[s] = float(last) if last is not None else None
                else:
                    out[s] = None
        except Exception:
            # fallback: fetch per symbol
            for s in symbols:
                try:
                    t = exchange.fetch_ticker(s)
                    last = t.get("last") or t.get("close")
                    out[s] = float(last) if last is not None else None
                except Exception:
                    out[s] = None
    except Exception:
        logger.exception("fetch_tickers_bulk top-level error")
        for s in symbols:
            out[s] = None
    return out

def fetch_price_single(symbol):
    try:
        t = exchange.fetch_ticker(symbol)
        p = float(t.get("last") or t.get("close") or 0.0)
        now_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        price_cache[symbol] = p
        history_cache[symbol].append((now_ts, p))
        # store minimal history to DB (best-effort)
        try:
            with db_lock:
                cur.execute("INSERT INTO history (symbol, price, ts) VALUES (?, ?, ?)", (symbol, p, now_ts))
                conn.commit()
        except Exception:
            logger.exception("Failed to write history")
        return p
    except Exception:
        logger.exception("fetch_price_single failed for %s", symbol)
        return None

# ---------------- Optional: fair price from MEXC public endpoints (best-effort) ----------------
def fetch_mexc_fair_price(symbol):
    """
    Try various MEXC public endpoints to get fair/mark price. Best-effort; may return None.
    Non-blocking for webhook: used from background tasks ideally.
    """
    try:
        s = symbol.replace("/", "").upper()
        endpoints = [
            f"https://contract.mexc.com/api/v1/contract/fair_price/{s}",
            f"https://contract.mexc.com/api/v1/contract/premiumIndex?symbol={s}",
            f"https://www.mexc.com/open/api/v2/market/ticker?symbol={s}"
        ]
        for url in endpoints:
            try:
                r = requests.get(url, timeout=3)
                if r.status_code != 200:
                    continue
                j = r.json()
                # Try to find numeric keys
                if isinstance(j, dict):
                    data = j.get("data") or j.get("ticker") or j
                    if isinstance(data, dict):
                        for key in ("fairPrice","markPrice","lastPrice","last","price"):
                            if key in data:
                                try:
                                    return float(data[key])
                                except Exception:
                                    pass
                    if isinstance(data, list) and len(data) > 0:
                        item = data[0]
                        for key in ("lastPrice","last","price"):
                            if key in item:
                                try:
                                    return float(item[key])
                                except Exception:
                                    pass
            except Exception:
                continue
    except Exception:
        logger.exception("fetch_mexc_fair_price top-level")
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
    return list(DEFAULT_SYMBOLS)

def add_user_symbol(chat_id, symbol):
    try:
        with db_lock:
            cur.execute("INSERT INTO user_symbols (chat_id, symbol) VALUES (?, ?)", (str(chat_id), symbol))
            conn.commit()
        with watch_lock:
            watch_symbols.add(symbol)
        return True
    except Exception:
        logger.exception("add_user_symbol error")
        return False

def save_alert(chat_id, symbol, is_percent, value, is_recurring=0):
    try:
        v = float(value)
    except Exception:
        logger.error("save_alert invalid value: %s", value)
        return None
    try:
        with db_lock:
            cur.execute("INSERT INTO alerts (chat_id, symbol, is_percent, value, is_recurring, active) VALUES (?, ?, ?, ?, ?, ?)",
                        (str(chat_id), symbol, int(is_percent), v, int(is_recurring), 1))
            conn.commit()
            aid = cur.lastrowid
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
        r = requests.post(SEND_MESSAGE, data=payload, timeout=6)
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
        r = requests.post(EDIT_MESSAGE, data=payload, timeout=6)
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
        requests.post(ANSWER_CB, data=payload, timeout=4)
    except Exception:
        pass

def send_photo_bytes(chat_id, buf, caption=None):
    try:
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
def kb_main():
    return {
        "inline_keyboard": [
            [{"text":"💰 Цена (все мои монеты)","callback_data":"price_all"}, {"text":"📊 График","callback_data":"chart_menu"}],
            [{"text":"🔔 Алерт","callback_data":"alerts_menu"}, {"text":"🤖 Автосигналы","callback_data":"autosignals_menu"}],
            [{"text":"⚙️ Настройки","callback_data":"settings_menu"}]
        ]
    }

def kb_symbols_grid(chat_id, callback_prefix):
    syms = get_user_symbols(chat_id)
    rows = []
    row = []
    for i, s in enumerate(syms):
        label = s.split("/")[0]
        row.append({"text": label, "callback_data": f"{callback_prefix}_{s}"})
        if (i+1) % 3 == 0:
            rows.append(row); row=[]
    if row: rows.append(row)
    rows.append([{"text":"🏠 Главное меню","callback_data":"main"}])
    return {"inline_keyboard": rows}

def kb_timeframes_for(symbol, prefix):
    rows = []
    groups = [["1m","5m","15m"], ["30m","1h","4h"], ["1d"]]
    for g in groups:
        rows.append([{"text":tf, "callback_data":f"{prefix}_{symbol}_{tf}"} for tf in g])
    rows.append([{"text":"🏠 Главное меню","callback_data":"main"}])
    return {"inline_keyboard": rows}

def kb_alerts_menu():
    return {"inline_keyboard":[[{"text":"➕ Добавить","callback_data":"add_alert"}, {"text":"📋 Мои алерты","callback_data":"list_alerts"}],
                                [{"text":"🏠 Главное меню","callback_data":"main"}]]}

def kb_alert_type():
    return {"inline_keyboard":[[{"text":"%","callback_data":"alert_type_pct"}, {"text":"$","callback_data":"alert_type_usd"}],
                                [{"text":"🏠 Главное меню","callback_data":"main"}]]}

def kb_alerts_list(alerts):
    kb = {"inline_keyboard": []}
    for a in alerts:
        aid, sym, is_pct, val, rec, active = a
        label = f"{sym} {'%' if is_pct else '$'}{val} {'🔁' if rec else ''}"
        kb["inline_keyboard"].append([{"text":label, "callback_data":f"alert_item_{aid}"}, {"text":"❌","callback_data":f"alert_del_{aid}"}])
    kb["inline_keyboard"].append([{"text":"⬅️ Назад","callback_data":"alerts_menu"}, {"text":"🏠 Главное меню","callback_data":"main"}])
    return kb

def kb_autosignals_menu(chat_id):
    return kb_symbols_grid(chat_id, "autosel")

def kb_autosignals_list(rows):
    kb = {"inline_keyboard":[]}
    for r in rows:
        aid, sym, tf, enabled = r
        kb["inline_keyboard"].append([{"text":f"{sym} {tf} {'✅' if enabled else '❌'}", "callback_data":f"autosig_item_{aid}"}, {"text":"❌","callback_data":f"autosig_del_{aid}"}])
    kb["inline_keyboard"].append([{"text":"⬅️ Назад","callback_data":"autosignals_menu"}, {"text":"🏠 Главное меню","callback_data":"main"}])
    return kb

# ---------------- Indicator functions ----------------
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

# ---------------- Chart building ----------------
def fetch_ohlcv_safe(symbol, timeframe="1h", limit=200):
    try:
        return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception:
        logger.exception("fetch_ohlcv_safe failed for %s %s", symbol, timeframe)
        return []

def build_candlestick(symbol, timeframe="1h", limit=200):
    """
    Возвращает (buf, None) или (None, error_msg)
    """
    if not HAS_MATPLOTLIB or not HAS_PANDAS:
        return None, "matplotlib or pandas not installed"
    ohlcv = fetch_ohlcv_safe(symbol, timeframe=timeframe, limit=limit)
    if not ohlcv:
        return None, "No OHLCV data"
    df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","vol"])
    df['date'] = pd.to_datetime(df['ts'], unit='ms')
    df.set_index('date', inplace=True)
    df = df[["open","high","low","close","vol"]]
    import io
    buf = io.BytesIO()
    try:
        if HAS_MPLFINANCE:
            mpf.plot(df, type='candle', style='charles', volume=True, savefig=dict(fname=buf, dpi=150, bbox_inches="tight"))
            buf.seek(0)
            return buf, None
    except Exception:
        logger.exception("mplfinance failed, fallback to manual")
    # Manual drawing fallback
    try:
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
        logger.exception("manual candlestick failed")
        return None, "chart generation failed"

# ---------------- Alerts processing ----------------
def evaluate_alerts():
    """
    Для всех активных алертов: если условие выполнено — отправляем и деактивируем, если одноразовый.
    Алгоритм:
     - % алерт: сравнение с предыдущим значением (процент изменения)
     - $ алерт: пересечение уровня (используем prev price и current price)
    """
    try:
        with db_lock:
            cur.execute("SELECT id, chat_id, symbol, is_percent, value, is_recurring, active FROM alerts WHERE active=1")
            rows = cur.fetchall()
    except Exception:
        logger.exception("read alerts failed")
        rows = []
    for row in rows:
        aid, chat_id, symbol, is_pct, value, is_rec, active = row
        cur_price = price_cache.get(symbol)
        if cur_price is None:
            continue
        hist = history_cache.get(symbol, [])
        prev_price = hist[-2][1] if len(hist) >= 2 else None
        triggered = False
        if is_pct:
            if prev_price is not None and prev_price != 0:
                pct = (cur_price - prev_price) / prev_price * 100.0
                if abs(pct) >= abs(value):
                    triggered = True
        else:
            try:
                v = float(value)
                if prev_price is not None:
                    if (prev_price < v and cur_price >= v) or (prev_price > v and cur_price <= v):
                        triggered = True
                else:
                    # if no prev, trigger if current crosses above or equals
                    if cur_price >= v:
                        triggered = True
            except Exception:
                logger.exception("parse alert value")
        if triggered:
            try:
                send_message(chat_id, f"🔔 Alert сработал: {symbol} {'%' if is_pct else '$'}{value} — now {cur_price}$")
                log_db("info", f"Alert fired for {chat_id} {symbol} {value}")
            except Exception:
                logger.exception("alert notify failed")
            if not is_rec:
                try:
                    with db_lock:
                        cur.execute("UPDATE alerts SET active=0 WHERE id=?", (aid,))
                        conn.commit()
                except Exception:
                    logger.exception("deactivate alert failed")

# ---------------- Autosignals processing ----------------
AUTOSIGNAL_NOTIFY_SPACING = 60  # seconds minimum between autosignal notifications per subscription (to avoid spam)
def evaluate_autosignals():
    try:
        with db_lock:
            cur.execute("SELECT id, chat_id, symbol, timeframe, enabled, last_notified FROM autosignals WHERE enabled=1")
            rows = cur.fetchall()
    except Exception:
        logger.exception("read autosignals failed")
        rows = []
    for aid, chat_id, symbol, timeframe, enabled, last_notified in rows:
        ohlcv = fetch_ohlcv_safe(symbol, timeframe=timeframe, limit=200)
        if not ohlcv or not HAS_PANDAS:
            continue
        closes = [c[4] for c in ohlcv]
        if len(closes) < 20:
            continue
        try:
            rsi_series = calculate_rsi(closes, 14)
            rsi_now = rsi_series[-1] if rsi_series else None
            ema5 = calculate_ema(closes, 5)
            ema20 = calculate_ema(closes, 20)
            msg = None
            # RSI conditions
            if rsi_now is not None:
                if rsi_now > 70:
                    msg = f"🔻 {symbol} {timeframe}: RSI {rsi_now:.1f} (overbought) — possible SHORT"
                elif rsi_now < 30:
                    msg = f"🔺 {symbol} {timeframe}: RSI {rsi_now:.1f} (oversold) — possible LONG"
            # EMA cross detection
            if len(ema5) >= 2 and len(ema20) >= 2:
                if ema5[-2] <= ema20[-2] and ema5[-1] > ema20[-1]:
                    msg = f"🔺 {symbol} {timeframe}: EMA5 crossed above EMA20 (LONG)"
                elif ema5[-2] >= ema20[-2] and ema5[-1] < ema20[-1]:
                    msg = f"🔻 {symbol} {timeframe}: EMA5 crossed below EMA20 (SHORT)"
            if msg:
                should_send = True
                if last_notified:
                    try:
                        last_dt = datetime.strptime(last_notified, "%Y-%m-%d %H:%M:%S")
                        if datetime.utcnow() - last_dt < timedelta(seconds=AUTOSIGNAL_NOTIFY_SPACING):
                            should_send = False
                    except Exception:
                        should_send = True
                if should_send:
                    try:
                        send_message(chat_id, f"🤖 Автосигнал: {msg}")
                        with db_lock:
                            cur.execute("UPDATE autosignals SET last_notified=? WHERE id=?", (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), aid))
                            conn.commit()
                    except Exception:
                        logger.exception("notify autosignal failed")
        except Exception:
            logger.exception("autosignal calc failed for %s %s", symbol, timeframe)

# ---------------- Background workers ----------------
def price_worker_loop():
    logger.info("Price worker started, interval=%s sec", PRICE_POLL_INTERVAL)
    while True:
        try:
            with watch_lock:
                syms = list(watch_symbols)
            # Bulk fetch
            prices = fetch_tickers_bulk(syms)
            now_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            for sym in syms:
                p = prices.get(sym)
                if p is None:
                    # fallback single
                    p = fetch_price_single(sym)
                else:
                    price_cache[sym] = p
                    history_cache[sym].append((now_ts, p))
                    # write to DB best-effort
                    try:
                        with db_lock:
                            cur.execute("INSERT INTO history (symbol, price, ts) VALUES (?, ?, ?)", (sym, float(p), now_ts))
                            conn.commit()
                    except Exception:
                        logger.exception("write history failed")
            # evaluate alerts with new prices
            try:
                evaluate_alerts()
            except Exception:
                logger.exception("evaluate_alerts failed")
        except Exception:
            logger.exception("price_worker_loop exception")
        time.sleep(PRICE_POLL_INTERVAL)

def autosignal_loop():
    logger.info("Autosignal worker started, interval=%s sec", AUTOSIGNAL_INTERVAL)
    while True:
        try:
            evaluate_autosignals()
        except Exception:
            logger.exception("autosignal_loop exception")
        time.sleep(AUTOSIGNAL_INTERVAL)

# Safe thread starter
def start_daemon_thread(target, name):
    t = threading.Thread(target=target, daemon=True, name=name)
    t.start()
    logger.info("Started thread %s", name)
    return t

start_daemon_thread(price_worker_loop, "price_worker")
start_daemon_thread(autosignal_loop, "autosignal_worker")

# ---------------- Flask webhook ----------------
app = Flask(__name__)

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    """
    Быстрый обработчик: парсит update, отвечает 200 как можно быстрее.
    Все тяжёлые операции (fetching, charts) выполняются в background или берутся из cache.
    """
    try:
        data = request.get_json() or {}
        # CALLBACK queries (inline buttons)
        if "callback_query" in data:
            cb = data["callback_query"]
            cb_id = cb.get("id")
            user = cb.get("from", {})
            uid = user.get("id")
            msg = cb.get("message", {}) or {}
            chat = msg.get("chat", {}) or {}
            chat_id = chat.get("id") or uid
            msg_id = msg.get("message_id")
            action = cb.get("data", "")
            add_user(chat_id)
            log_db("info", f"callback {action} from {chat_id}")

            # Navigation
            if action == "main":
                edit_message(chat_id, msg_id, "🏠 Главное меню", kb_main())
                answer_callback(cb_id)
                return {"ok": True}

            # Price all -> returns cached prices quickly
            if action == "price_all":
                syms = get_user_symbols(chat_id)
                lines = []
                # use cached price, fair price attempt in background not to block
                for s in syms:
                    last = price_cache.get(s)
                    fair = None
                    # try fair price but non-blocking: run in background and reply with cached quick
                    lines.append(f"{s}: last {last if last is not None else 'N/A'}$")
                send_message(chat_id, "💰 Цены (источник: MEXC):\n" + "\n".join(lines), kb_main())
                answer_callback(cb_id)
                return {"ok": True}

            # Chart menu -> choose symbol
            if action == "chart_menu":
                edit_message(chat_id, msg_id, "📊 Выберите монету", kb_symbols_grid(chat_id, "chart_sel"))
                answer_callback(cb_id)
                return {"ok": True}

            # symbol selected for chart -> ask timeframe
            if action.startswith("chart_sel_"):
                symbol = action.split("chart_sel_",1)[1]
                edit_message(chat_id, msg_id, f"📊 {symbol} — выберите таймфрейм", kb_timeframes_for(symbol, "chart_tf"))
                answer_callback(cb_id)
                return {"ok": True}

            # timeframe chosen -> build chart asynchronously to avoid long blocking
            if action.startswith("chart_tf_"):
                try:
                    _, rest = action.split("chart_tf_",1)
                    # rest like "BTC/USDT_1h"
                    parts = rest.rsplit("_", 1)
                    symbol = parts[0]
                    tf = parts[1]
                except Exception:
                    answer_callback(cb_id, "Ошибка парсинга")
                    return {"ok": True}
                # Acknowledge immediately
                answer_callback(cb_id, "Запрос принят. Строю график в фоне...")
                # spawn thread to build chart and send
                def build_and_send():
                    try:
                        buf, err = build_candlestick(symbol, timeframe=tf, limit=200)
                        if buf:
                            send_photo_bytes(chat_id, buf, caption=f"{symbol} {tf} свечи (MEXC)")
                        else:
                            send_message(chat_id, f"Не удалось построить график: {err}")
                    except Exception:
                        logger.exception("build_and_send failed")
                threading.Thread(target=build_and_send, daemon=True).start()
                return {"ok": True}

            # Alerts menu
            if action == "alerts_menu":
                edit_message(chat_id, msg_id, "🔔 Меню алертов", kb_alerts_menu())
                answer_callback(cb_id)
                return {"ok": True}

            # add alert -> choose type (%/$)
            if action == "add_alert":
                pending_flows[str(chat_id)] = {"state":"awaiting_alert_type"}
                edit_message(chat_id, msg_id, "➕ Выберите тип алерта", kb_alert_type())
                answer_callback(cb_id)
                return {"ok": True}

            # alert type chosen
            if action == "alert_type_pct" or action == "alert_type_usd":
                key = str(chat_id)
                typ = 1 if action == "alert_type_pct" else 0
                pending_flows[key] = {"state":"awaiting_alert_value", "is_percent": typ}
                send_message(chat_id, "Введите значение (например: 2.5 для 2.5% или 50 для $50). После ввода — выберите монету кнопкой.", kb_symbols_grid(chat_id, "alert_symbol"))
                answer_callback(cb_id)
                return {"ok": True}

            # user selected symbol to attach alert to
            if action.startswith("alert_symbol_"):
                symbol = action.split("alert_symbol_",1)[1]
                key = str(chat_id)
                flow = pending_flows.get(key)
                if not flow or flow.get("state") != "awaiting_alert_value":
                    # if not in flow, prompt value first
                    pending_flows[key] = {"state":"awaiting_value_for_symbol", "symbol":symbol}
                    send_message(chat_id, f"Вы выбрали {symbol}. Введите значение (например: 50 или 2.5) текстом.")
                    answer_callback(cb_id)
                    return {"ok": True}
                # If value already provided in flow
                val = flow.get("value")
                is_pct = flow.get("is_percent",0)
                if val is None:
                    send_message(chat_id, "Вы ещё не ввели значение. Введите число в сообщении, затем нажмите монету.", kb_symbols_grid(chat_id, "alert_symbol"))
                    answer_callback(cb_id)
                    return {"ok": True}
                aid = save_alert(chat_id, symbol, is_pct, val, is_recurring=0)
                if aid:
                    send_message(chat_id, f"✅ Алерт добавлен для {symbol}: {'%' if is_pct else '$'}{val}", kb_main())
                    pending_flows.pop(key, None)
                else:
                    send_message(chat_id, "Ошибка при сохранении алерта. Проверьте введённое число.")
                answer_callback(cb_id)
                return {"ok": True}

            # show list of alerts
            if action == "list_alerts":
                rows = list_alerts(chat_id)
                if not rows:
                    edit_message(chat_id, msg_id, "У вас нет алертов.", kb_alerts_menu())
                else:
                    edit_message(chat_id, msg_id, "📋 Ваши алерты:", kb_alerts_list(rows))
                answer_callback(cb_id)
                return {"ok": True}

            # alert item selected (to view/edit/delete)
            if action.startswith("alert_item_"):
                aid = int(action.split("_")[-1])
                with db_lock:
                    cur.execute("SELECT id, symbol, is_percent, value, is_recurring FROM alerts WHERE id=?", (aid,))
                    r = cur.fetchone()
                if r:
                    _id, sym, is_pct, val, rec = r
                    text = f"Alert #{_id}\n{sym}\nТип: {'%' if is_pct else '$'}\nЗначение: {val}\nПостоянный: {'Да' if rec else 'Нет'}"
                    kb = {"inline_keyboard":[[{"text":"✏ Изменить","callback_data":f"alert_edit_{_id}"},{"text":"❌ Удалить","callback_data":f"alert_del_{_id}"}],
                                              [{"text":"⬅️ Назад","callback_data":"list_alerts"},{"text":"🏠 Главное меню","callback_data":"main"}]]}
                    edit_message(chat_id, msg_id, text, kb)
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
                pending_flows[str(chat_id)] = {"state":"awaiting_edit_value", "edit_aid":aid}
                send_message(chat_id, "Введите новое значение (число):", {"inline_keyboard":[[{"text":"🏠 Главное меню","callback_data":"main"}]]})
                answer_callback(cb_id)
                return {"ok": True}

            # Autosignals menu
            if action == "autosignals_menu":
                edit_message(chat_id, msg_id, "🤖 Автосигналы — выберите монету", kb_autosignals_menu(chat_id))
                answer_callback(cb_id)
                return {"ok": True}

            if action.startswith("autosel_"):
                symbol = action.split("autosel_",1)[1]
                edit_message(chat_id, msg_id, f"🤖 {symbol} — выберите таймфрейм", kb_timeframes_for(symbol, "autosel_tf"))
                answer_callback(cb_id)
                return {"ok": True}

            if action.startswith("autosel_tf_"):
                try:
                    _, rest = action.split("autosel_tf_",1)
                    symbol, tf = rest.rsplit("_",1)
                except Exception:
                    answer_callback(cb_id, "Ошибка парсинга")
                    return {"ok": True}
                upsert_autosignal(chat_id, symbol, tf, enabled=1)
                send_message(chat_id, f"✅ Автосигнал подписан: {symbol} {tf}", kb_main())
                answer_callback(cb_id)
                return {"ok": True}

            if action == "list_autosignals":
                rows = list_autosignals(chat_id)
                if not rows:
                    edit_message(chat_id, msg_id, "Нет автосигналов.", kb_autosignals_menu(chat_id))
                else:
                    edit_message(chat_id, msg_id, "Мои автосигналы:", kb_autosignals_list(rows))
                answer_callback(cb_id)
                return {"ok": True}

            if action.startswith("autosig_item_"):
                aid = int(action.split("_")[-1])
                with db_lock:
                    cur.execute("SELECT id, symbol, timeframe, enabled FROM autosignals WHERE id=?", (aid,))
                    r = cur.fetchone()
                if r:
                    aid, sym, tf, enabled = r
                    newv = 0 if enabled else 1
                    with db_lock:
                        cur.execute("UPDATE autosignals SET enabled=? WHERE id=?", (newv, aid))
                        conn.commit()
                    send_message(chat_id, f"Автосигнал {sym} {tf} {'включен' if newv else 'выключен'}", kb_main())
                answer_callback(cb_id)
                return {"ok": True}

            if action.startswith("autosig_del_"):
                aid = int(action.split("_")[-1])
                delete_autosignal(aid)
                edit_message(chat_id, msg_id, "✅ Автосигнал удалён.", kb_autosignals_menu(chat_id))
                answer_callback(cb_id)
                return {"ok": True}

            # Settings
            if action == "settings_menu":
                edit_message(chat_id, msg_id, "⚙️ Настройки (в разработке)", {"inline_keyboard":[[{"text":"🏠 Главное меню","callback_data":"main"}]]})
                answer_callback(cb_id)
                return {"ok": True}

            answer_callback(cb_id, "Неизвестная кнопка")
            logger.warning("Unknown callback action: %s", action)
            return {"ok": True}

        # MESSAGE handling (text)
        msg = data.get("message") or data.get("edited_message")
        if not msg:
            return {"ok": True}
        chat_id = msg.get("chat",{}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id:
            return {"ok": True}
        add_user(chat_id)
        log_db("info", f"message from {chat_id}: {text}")

        # pending flows by text
        key = str(chat_id)
        flow = pending_flows.get(key)
        if flow:
            state = flow.get("state")
            if state == "awaiting_alert_value":
                try:
                    val = float(text.replace(",","."))
                    flow["value"] = val
                    pending_flows[key] = flow
                    send_message(chat_id, f"Значение {val} сохранено. Теперь выберите монету кнопкой.", kb_symbols_grid(chat_id, "alert_symbol"))
                except Exception:
                    send_message(chat_id, "Неверный формат числа. Введите только число (например: 2.5 или 50).")
                return {"ok": True}
            if state == "awaiting_value_for_symbol":
                try:
                    v = float(text.replace(",","."))  # user typed value after selecting symbol
                    symbol = flow.get("symbol")
                    is_pct = flow.get("is_percent", 0)
                    aid = save_alert(chat_id, symbol, is_pct, v, is_recurring=0)
                    if aid:
                        send_message(chat_id, f"✅ Алерт добавлен: {symbol} {'%' if is_pct else '$'}{v}", kb_main())
                        pending_flows.pop(key, None)
                    else:
                        send_message(chat_id, "Ошибка при сохранении алерта.")
                except Exception:
                    send_message(chat_id, "Неверный формат числа.")
                return {"ok": True}
            if state == "awaiting_edit_value":
                try:
                    newv = float(text.replace(",",".")) 
                    aid = flow.get("edit_aid")
                    with db_lock:
                        cur.execute("UPDATE alerts SET value=? WHERE id=?", (newv, aid))
                        conn.commit()
                    send_message(chat_id, f"✅ Алерт #{aid} обновлён на {newv}", kb_main())
                    pending_flows.pop(key, None)
                except Exception:
                    logger.exception("edit alert failed")
                    send_message(chat_id, "Ошибка при обновлении алерта.")
                return {"ok": True}

        # Commands quick handlers
        if text.startswith("/start"):
            send_message(chat_id, "👋 Привет! Я крипто-бот. Управление через кнопки (ниже).", kb_main())
            return {"ok": True}

        if text.startswith("/price"):
            parts = text.split()
            if len(parts) == 2:
                s = parts[1].upper()
                if "/" not in s:
                    s = s + "/USDT"
                last = price_cache.get(s) or fetch_price_single(s)
                fair = fetch_mexc_fair_price(s)  # best-effort (may be slow)
                send_message(chat_id, f"{s}: last {last if last is not None else 'N/A'}$, fair {fair if fair is not None else 'N/A'}", kb_main())
            else:
                send_message(chat_id, "Использование: /price SYMBOL (например /price BTC)", kb_main())
            return {"ok": True}

        if text.startswith("/chart"):
            parts = text.split()
            if len(parts) >= 2:
                s = parts[1].upper()
                if "/" not in s:
                    s = s + "/USDT"
                tf = parts[2] if len(parts) >= 3 else "1h"
                send_message(chat_id, f"Строю график {s} {tf} ...")
                # build async
                def build_and_send_chart():
                    buf, err = build_candlestick(s, timeframe=tf, limit=200)
                    if buf:
                        send_photo_bytes(chat_id, buf, caption=f"{s} {tf} свечи")
                    else:
                        send_message(chat_id, f"Не удалось: {err}")
                threading.Thread(target=build_and_send_chart, daemon=True).start()
            else:
                send_message(chat_id, "Использование: /chart SYMBOL [tf]\nПример: /chart BTC 1h")
            return {"ok": True}

        # default: show main menu
        send_message(chat_id, "Выберите действие:", kb_main())
        return {"ok": True}
    except Exception:
        logger.exception("telegram_webhook exception")
        return {"ok": True}

# ---------------- Run Flask ----------------
if __name__ == "__main__":
    logger.info("Bot starting. Webhook endpoint: /%s", TELEGRAM_TOKEN)
    logger.info("Be sure to set Telegram webhook to https://<your-app>/%s", TELEGRAM_TOKEN)
    # Start Flask (for local dev). On Railway / production you typically use gunicorn: web: gunicorn bot:app --workers 2 --timeout 120
    app.run(host="0.0.0.0", port=PORT)
