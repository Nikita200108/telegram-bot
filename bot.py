#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Полный bot.py
Requirements (пример для requirements.txt):
ccxt
flask
requests
pandas
matplotlib
numpy
mplfinance

Сохраните TELEGRAM_TOKEN в переменной окружения TELEGRAM_TOKEN на Railway,
или поместите токен в файл token.txt (он автоматически будет создан, если вы введёте токен локально).
"""

import os
import sys
import time
import json
import math
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from collections import defaultdict, deque

# Optional imports for charts/indicators
HAS_PANDAS = True
HAS_MATPLOTLIB = True
HAS_MPLFINANCE = True
try:
    import pandas as pd
except Exception:
    HAS_PANDAS = False

try:
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
except Exception:
    HAS_MATPLOTLIB = False

try:
    import mplfinance as mpf
except Exception:
    HAS_MPLFINANCE = False

# Core libs
try:
    import requests
    import ccxt
    from flask import Flask, request
except Exception as e:
    # If required libs missing, log and raise (user must pip install)
    print("ERROR: missing required packages. Install requirements (ccxt, flask, requests, pandas, matplotlib, numpy, mplfinance).")
    raise

# ---------------- Logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("crypto_bot")

# ---------------- Token resolution & persistence ----------------
def resolve_token():
    """
    Resolve TELEGRAM_TOKEN in this order:
    1) ENV TELEGRAM_TOKEN
    2) token.txt file (if exists)
    3) if interactive terminal -> prompt user and save to token.txt
    4) else -> exit with informative message (on Railway use ENV instead)
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
            logger.exception("Не удалось прочитать token.txt")

    # If interactive, ask
    if sys.stdin and sys.stdin.isatty():
        try:
            token = input("Введите TELEGRAM_TOKEN (BotFather): ").strip()
            if token:
                try:
                    with open(token_path, "w", encoding="utf-8") as f:
                        f.write(token)
                        logger.info("TELEGRAM_TOKEN сохранён в token.txt")
                except Exception:
                    logger.exception("Не удалось сохранить token.txt")
                return token
        except Exception as e:
            logger.error("Token not set: %s", str(e))

    # Not interactive and no token found -> fail early
    logger.error("TELEGRAM_TOKEN не найден. На Railway установите переменную окружения TELEGRAM_TOKEN.")
    raise RuntimeError("TELEGRAM_TOKEN not set in ENV and cannot prompt (non-interactive).")

try:
    TELEGRAM_TOKEN = resolve_token()
except Exception as e:
    # Fail fast with clear message
    logger.exception("Token resolution failed: %s", e)
    # Exit with non-zero so Gunicorn will fail to boot (so you notice)
    sys.exit(1)

BOT_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SEND_MESSAGE = BOT_API + "/sendMessage"
EDIT_MESSAGE = BOT_API + "/editMessageText"
SEND_PHOTO = BOT_API + "/sendPhoto"
ANSWER_CB = BOT_API + "/answerCallbackQuery"
GET_UPDATES = BOT_API + "/getUpdates"

# ---------------- Config & Defaults ----------------
PORT = int(os.getenv("PORT", "8000"))
DB_PATH = os.getenv("DB_PATH", "bot_data.sqlite")
DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "NEAR/USDT"]
PRICE_POLL_INTERVAL = int(os.getenv("PRICE_POLL_INTERVAL", "10"))  # seconds
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "1000"))

# Price steps for UI (kept from original)
PRICE_STEPS = [-10000, -5000, -1000, -100, -10, -1, 1, 10, 100, 1000, 5000, 10000]

TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

# ---------------- Exchange ----------------
exchange = ccxt.mexc({"enableRateLimit": True})

# ---------------- Database init ----------------
# We'll reuse your tables and add a few: user_symbols, alerts (levels), autosignals, history, logs, user_settings
def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
    # users
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id TEXT PRIMARY KEY,
        created_at DATETIME
    )
    """)
    # user_symbols
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_symbols (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        symbol TEXT
    )
    """)
    # alerts (levels)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        symbol TEXT,
        is_percent INTEGER DEFAULT 0,   -- 1 => percent, 0 => absolute $
        value REAL,
        is_recurring INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # autosignals subscriptions
    cur.execute("""
    CREATE TABLE IF NOT EXISTS autosignals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        symbol TEXT,
        timeframe TEXT,
        enabled INTEGER DEFAULT 1,
        last_notified DATETIME
    )
    """)
    # history: simple price history for charting
    cur.execute("""
    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT,
        price REAL,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # logs DB table (optional)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        level TEXT,
        message TEXT,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    # user settings
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_settings (
        chat_id TEXT PRIMARY KEY,
        signals_enabled INTEGER DEFAULT 1
    )
    """)
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
        logger.exception("DB commit failed")

def log_db(level, message):
    try:
        with db_lock:
            cur.execute("INSERT INTO logs (level, message) VALUES (?, ?)", (level.upper(), message))
            conn.commit()
    except Exception:
        logger.exception("log_db failed")
    getattr(logger, level.lower())(message)

# ---------------- In-memory structures ----------------
pending_alerts = {}  # chat_id -> pending dict when user configuring an alert
last_prices = {}     # symbol -> last price
history_cache = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))

# ---------------- Helper DB functions ----------------
def add_user(chat_id):
    try:
        with db_lock:
            cur.execute("INSERT OR IGNORE INTO users (chat_id, created_at) VALUES (?, ?)", (str(chat_id), datetime.utcnow()))
            conn.commit()
    except Exception:
        logger.exception("add_user failed")

def get_user_symbols(chat_id):
    try:
        with db_lock:
            cur.execute("SELECT symbol FROM user_symbols WHERE chat_id=?", (str(chat_id),))
            rows = cur.fetchall()
            if rows:
                return [r[0] for r in rows]
    except Exception:
        logger.exception("get_user_symbols failed")
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
    try:
        with db_lock:
            cur.execute("INSERT INTO alerts (chat_id, symbol, is_percent, value, is_recurring) VALUES (?, ?, ?, ?, ?)",
                        (str(chat_id), symbol, int(is_percent), float(value), int(is_recurring)))
            conn.commit()
            return cur.lastrowid
    except Exception:
        logger.exception("save_alert error")
        return None

def list_alerts(chat_id):
    try:
        with db_lock:
            cur.execute("SELECT id, symbol, is_percent, value, is_recurring, active FROM alerts WHERE chat_id=? ORDER BY id DESC", (str(chat_id),))
            rows = cur.fetchall()
            return rows
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
        if r.status_code != 200:
            logger.warning("send_message failed %s %s", r.status_code, r.text)
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
        if r.status_code != 200:
            logger.warning("edit_message failed %s %s", r.status_code, r.text)
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
        files = {"photo": ("chart.png", buf.getvalue() if hasattr(buf, "getvalue") else buf)}
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
            data["parse_mode"] = "HTML"
        r = requests.post(SEND_PHOTO, files=files, data=data, timeout=30)
        if r.status_code != 200:
            logger.warning("send_photo failed %s %s", r.status_code, r.text)
        return r.json()
    except Exception:
        logger.exception("send_photo exception")
        return {}

# ---------------- UI Keyboards ----------------
def kb_main_menu():
    return {
        "inline_keyboard": [
            [{"text": "💰 Цена", "callback_data": "price_menu"}, {"text": "📊 График", "callback_data": "chart_menu"}],
            [{"text": "🔔 Алерты", "callback_data": "alerts_menu"}, {"text": "🤖 Автосигналы", "callback_data": "autosignals_menu"}],
            [{"text": "⚙️ Настройки", "callback_data": "settings_menu"}]
        ]
    }

def kb_price_menu():
    return {
        "inline_keyboard": [
            [{"text": "💰 Все цены", "callback_data": "price_all"}],
            [{"text": "🏠 Главное меню", "callback_data": "main"}]
        ]
    }

def kb_alerts_menu():
    return {
        "inline_keyboard": [
            [{"text": "➕ Добавить алерт", "callback_data": "add_alert"}],
            [{"text": "📋 Мои алерты", "callback_data": "list_alerts"}],
            [{"text": "🏠 Главное меню", "callback_data": "main"}]
        ]
    }

def kb_alert_type():
    return {
        "inline_keyboard": [
            [{"text": "%", "callback_data": "alert_type_percent"}, {"text": "$", "callback_data": "alert_type_usd"}],
            [{"text": "⬅️ Назад", "callback_data": "alerts_menu"}, {"text": "🏠 Главное меню", "callback_data": "main"}]
        ]
    }

def kb_confirm_cancel():
    return {"inline_keyboard": [
        [{"text": "✅ Сохранить", "callback_data": "confirm_save"}, {"text": "❌ Отменить", "callback_data": "cancel_save"}],
        [{"text": "🏠 Главное меню", "callback_data": "main"}]
    ]}

def kb_autosignals_menu(symbols=None):
    if symbols is None:
        symbols = DEFAULT_SYMBOLS
    rows = []
    for s in symbols:
        rows.append([{"text": s.split("/")[0], "callback_data": f"autosignal_select_{s}"}])
    rows.append([{"text": "📋 Мои автосигналы", "callback_data": "list_autosignals"}])
    rows.append([{"text": "🏠 Главное меню", "callback_data": "main"}])
    return {"inline_keyboard": rows}

def kb_timeframe_select(symbol):
    # timeframe selection for autosignals
    rows = []
    tfs = [["1m","5m","15m"], ["30m","1h","4h"], ["1d"]]
    for row in tfs:
        rows.append([{"text": tf, "callback_data": f"autosignal_tf_{symbol}_{tf}"} for tf in row])
    rows.append([{"text": "⬅️ Назад", "callback_data": "autosignals_menu"}, {"text": "🏠 Главное меню", "callback_data": "main"}])
    return {"inline_keyboard": rows}

def kb_alerts_list(alerts):
    rows = []
    for a in alerts:
        aid, sym, is_pct, val, rec, active = a
        label = f"{sym} {'%' if is_pct else '$'}{val}"
        rows.append([{"text": label, "callback_data": f"alert_item_{aid}"}, {"text": "❌", "callback_data": f"alert_del_{aid}"}])
    rows.append([{"text":"⬅️ Назад", "callback_data":"alerts_menu"}, {"text": "🏠 Главное меню", "callback_data": "main"}])
    return {"inline_keyboard": rows}

def kb_autosignals_list(rows):
    kb = {"inline_keyboard": []}
    for r in rows:
        aid, sym, tf, enabled = r
        kb["inline_keyboard"].append([{"text": f"{sym} {tf} {'✅' if enabled else '❌'}", "callback_data": f"autosignal_item_{aid}"},
                                     {"text": "❌", "callback_data": f"autosignal_del_{aid}"}])
    kb["inline_keyboard"].append([{"text": "⬅️ Назад", "callback_data": "autosignals_menu"}, {"text": "🏠 Главное меню", "callback_data": "main"}])
    return kb

# ---------------- Price fetching & history ----------------
def fetch_price_for(symbol):
    try:
        ticker = exchange.fetch_ticker(symbol)
        price = float(ticker.get("last") or ticker.get("close") or 0.0)
        now_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        last_prices[symbol] = price
        # save history
        try:
            with db_lock:
                cur.execute("INSERT INTO history (symbol, price, ts) VALUES (?, ?, ?)", (symbol, price, now_ts))
                conn.commit()
        except Exception:
            logger.exception("save history failed")
        # in-memory cache
        history_cache[symbol].append((now_ts, price))
        return price
    except Exception as e:
        logger.exception("fetch_price_for error for %s: %s", symbol, str(e))
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

# Background worker to poll prices and check alerts/autosignals
def price_poll_worker():
    logger.info("Price poll worker started")
    while True:
        try:
            symbols = fetch_all_user_symbols()
            for sym in symbols:
                try:
                    fetch_price_for(sym)
                except Exception:
                    logger.exception("price fetch failed for %s", sym)
            # check alerts and autosignals
            try:
                check_alerts()
                check_autosignals()
            except Exception:
                logger.exception("alert/autosignal check failed")
        except Exception:
            logger.exception("price_poll_worker outer exception")
        time.sleep(PRICE_POLL_INTERVAL)

# ---------------- Alerts checking ----------------
def check_alerts():
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
        if is_pct:
            # percent: relative to previous price in history or to last saved price
            hist = list(history_cache.get(symbol, []))
            if len(hist) >= 2:
                prev = hist[-2][1]
                change_pct = (cur_price - prev) / prev * 100 if prev != 0 else 0
                if abs(change_pct) >= abs(value):
                    triggered = True
            else:
                # fallback: compare to price stored when alert was created? skip for clarity
                pass
        else:
            # absolute value
            if cur_price >= value and r[5] == 0:  # for one-time maybe we want specific direction; keep simple: notify on crossing up
                triggered = True
            # More robust logic: for cross detection we'd need previous price; implement for both directions:
            hist = list(history_cache.get(symbol, []))
            if len(hist) >= 2:
                prev = hist[-2][1]
                if (prev < value and cur_price >= value) or (prev > value and cur_price <= value):
                    triggered = True

        if triggered:
            try:
                send_message(chat_id, f"🔔 ALARM: {symbol} reached {value}{'%' if is_pct else '$'} — current {cur_price}")
                log_db("info", f"Alert fired for {chat_id} {symbol} {value}{'%' if is_pct else '$'}")
            except Exception:
                logger.exception("failed to notify alert")
            # if one-time => deactivate/delete
            if not is_rec:
                try:
                    with db_lock:
                        cur.execute("UPDATE alerts SET active=0 WHERE id=?", (aid,))
                        conn.commit()
                except Exception:
                    logger.exception("deactivate alert failed")

# ---------------- Autosignal checking (RSI/EMA) ----------------
def calculate_rsi_from_series(prices, period=14):
    if not HAS_PANDAS:
        return []
    s = pd.Series(prices)
    delta = s.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ma_up = up.rolling(window=period).mean()
    ma_down = down.rolling(window=period).mean()
    rs = ma_up / ma_down
    rsi = 100 - 100/(1+rs)
    return rsi.fillna(50).tolist()

def calculate_ema_series(prices, span):
    if not HAS_PANDAS:
        return []
    s = pd.Series(prices)
    return s.ewm(span=span, adjust=False).mean().tolist()

def fetch_ohlcv_safe(symbol, timeframe="1m", limit=200):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        # returns list of [ts, open, high, low, close, volume]
        return ohlcv
    except Exception:
        logger.exception("fetch_ohlcv failed for %s %s", symbol, timeframe)
        return []

def check_autosignals():
    try:
        with db_lock:
            cur.execute("SELECT id, chat_id, symbol, timeframe, enabled, last_notified FROM autosignals WHERE enabled=1")
            rows = cur.fetchall()
    except Exception:
        logger.exception("list autosignals db error")
        rows = []
    for r in rows:
        aid, chat_id, symbol, timeframe, enabled, last_notified = r
        # fetch recent closes
        ohlcv = fetch_ohlcv_safe(symbol, timeframe=timeframe, limit=100)
        if not ohlcv:
            continue
        closes = [c[4] for c in ohlcv]
        if len(closes) < 20:
            continue
        # simple strategy: RSI(14) > 70 -> overbought -> send "consider short", RSI <30 -> consider long
        if HAS_PANDAS:
            rsi = calculate_rsi_from_series(closes, 14)[-1]
            # EMA cross simple: EMA5 cross EMA20
            ema5 = calculate_ema_series(closes, 5)
            ema20 = calculate_ema_series(closes, 20)
            ema5v = ema5[-1] if ema5 else None
            ema20v = ema20[-1] if ema20 else None
            signal = None
            if rsi is not None:
                if rsi > 70:
                    signal = f"🔻 {symbol} ({timeframe}) — RSI {rsi:.1f} -> overbought (consider SHORT)"
                elif rsi < 30:
                    signal = f"🔺 {symbol} ({timeframe}) — RSI {rsi:.1f} -> oversold (consider LONG)"
            # EMA cross stronger confirmation
            if ema5v and ema20v:
                if ema5v > ema20v and closes[-2] and closes[-1] and ema5[-2] <= ema20[-2]:
                    # upward cross
                    signal = f"🔺 {symbol} ({timeframe}) — EMA5 crossed above EMA20 (LONG)"
                elif ema5v < ema20v and ema5[-2] >= ema20[-2]:
                    signal = f"🔻 {symbol} ({timeframe}) — EMA5 crossed below EMA20 (SHORT)"

            # throttle notifications: don't spam. We'll use last_notified field.
            should_notify = True
            if last_notified:
                try:
                    last_dt = datetime.strptime(last_notified, "%Y-%m-%d %H:%M:%S")
                    if datetime.utcnow() - last_dt < timedelta(minutes=30):
                        should_notify = False
                except Exception:
                    should_notify = True

            if signal and should_notify:
                try:
                    send_message(chat_id, f"🤖 Автосигнал: {signal}")
                    with db_lock:
                        cur.execute("UPDATE autosignals SET last_notified=? WHERE id=?", (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), aid))
                        conn.commit()
                    log_db("info", f"Autosignal sent to {chat_id}: {signal}")
                except Exception:
                    logger.exception("notify autosignal failed")

# ---------------- Chart building ----------------
def build_candlestick_chart(symbol, timeframe="1h", limit=200):
    """
    Returns BytesIO buffer with chart PNG (candlesticks).
    Uses mplfinance if available; otherwise constructs basic candlesticks with matplotlib.
    """
    if not HAS_PANDAS or not HAS_MATPLOTLIB:
        logger.warning("pandas or matplotlib not available; cannot build chart.")
        return None, "Required packages pandas and matplotlib are not installed."

    ohlcv = fetch_ohlcv_safe(symbol, timeframe=timeframe, limit=limit)
    if not ohlcv:
        return None, "No OHLCV data"

    # prepare dataframe
    df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
    df['date'] = pd.to_datetime(df['ts'], unit='ms')
    df.set_index('date', inplace=True)
    df = df[["open","high","low","close","volume"]]

    import io
    buf = io.BytesIO()
    try:
        if HAS_MPLFINANCE:
            mpf.plot(df, type='candle', style='charles', volume=True, savefig=buf, figsize=(10,6))
            buf.seek(0)
            return buf, None
        else:
            # fallback simple candlestick plotting
            fig, ax = plt.subplots(figsize=(10,6))
            width = 0.0005 * (df.index[-1] - df.index[0]).total_seconds() / len(df)  # approximate width
            dates = df.index
            for idx, row in df.iterrows():
                color = 'green' if row['close'] >= row['open'] else 'red'
                # candle body
                ax.add_patch(Rectangle((mdates.date2num(idx)-0.0005, min(row['open'], row['close'])),
                                        0.001, abs(row['open']-row['close']), color=color))
                # wick
                ax.vlines(mdates.date2num(idx), row['low'], row['high'], color='black', linewidth=0.5)
            ax.plot(df['close'], label='Close')
            ax.set_title(f"{symbol} {timeframe}")
            ax.grid(True)
            plt.tight_layout()
            fig.savefig(buf, format='png', dpi=150)
            plt.close(fig)
            buf.seek(0)
            return buf, None
    except Exception:
        logger.exception("build_candlestick_chart error")
        return None, "Chart build error"

# ---------------- Webhook & Bot logic (Flask) ----------------
app = Flask(__name__)

# since TELEGRAM_TOKEN already resolved, define route with token
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        data = request.get_json() or {}
        # callback queries (button presses)
        if "callback_query" in data:
            cb = data["callback_query"]
            cb_id = cb.get("id")
            from_user = cb.get("from", {})
            user_id = from_user.get("id")
            message = cb.get("message", {})
            chat_id = message.get("chat", {}).get("id") or user_id
            message_id = message.get("message_id")
            data_cb = cb.get("data", "")

            add_user(chat_id)
            log_db("info", f"callback {data_cb} from {chat_id}")

            # Navigation / main
            if data_cb == "main":
                edit_message(chat_id, message_id, "🏠 Главное меню", kb_main_menu())
                answer_callback(cb_id)
                return {"ok": True}

            if data_cb == "price_menu":
                edit_message(chat_id, message_id, "💰 Меню цен", kb_price_menu())
                answer_callback(cb_id)
                return {"ok": True}

            if data_cb == "price_all":
                # send all user symbols current price
                syms = get_user_symbols(chat_id)
                texts = []
                for s in syms:
                    p = last_prices.get(s) or fetch_price_for(s)
                    texts.append(f"{s}: {p if p is not None else 'N/A'}$")
                send_message(chat_id, "💰 Текущие цены:\n" + "\n".join(texts), kb_main_menu())
                answer_callback(cb_id)
                return {"ok": True}

            if data_cb == "chart_menu":
                edit_message(chat_id, message_id, "📊 Графики (используй /chart SYMBOL TF или выбери в меню):", kb_main_menu())
                answer_callback(cb_id)
                return {"ok": True}

            # Alerts menu
            if data_cb == "alerts_menu":
                edit_message(chat_id, message_id, "🔔 Меню алертов", kb_alerts_menu())
                answer_callback(cb_id)
                return {"ok": True}

            if data_cb == "add_alert":
                # start alert creation flow
                # store pending state
                pending_alerts[str(chat_id)] = {"state": "awaiting_type"}
                edit_message(chat_id, message_id, "➕ Какой тип алерта выбрать?", kb_alert_type())
                answer_callback(cb_id)
                return {"ok": True}

            if data_cb == "alert_type_percent":
                # awaiting percent value
                pending_alerts[str(chat_id)] = {"state": "awaiting_value", "is_percent": 1}
                send_message(chat_id, "Введите значение в процентах (например: 5 для 5%):", {"inline_keyboard":[[{"text":"🏠 Главное меню","callback_data":"main"}]]})
                answer_callback(cb_id)
                return {"ok": True}

            if data_cb == "alert_type_usd":
                pending_alerts[str(chat_id)] = {"state": "awaiting_value", "is_percent": 0}
                send_message(chat_id, "Введите значение в долларах (например: 50):", {"inline_keyboard":[[{"text":"🏠 Главное меню","callback_data":"main"}]]})
                answer_callback(cb_id)
                return {"ok": True}

            if data_cb == "list_alerts":
                rows = list_alerts(chat_id)
                if not rows:
                    edit_message(chat_id, message_id, "📋 У вас нет алертов.", kb_alerts_menu())
                else:
                    # rows: id, symbol, is_percent, value, is_recurring, active
                    edit_message(chat_id, message_id, "📋 Ваши алерты:", kb_alerts_list(rows))
                answer_callback(cb_id)
                return {"ok": True}

            if data_cb.startswith("alert_item_"):
                aid = int(data_cb.split("_")[-1])
                # show details and offer edit/delete
                with db_lock:
                    cur.execute("SELECT id, symbol, is_percent, value, is_recurring FROM alerts WHERE id=?", (aid,))
                    row = cur.fetchone()
                if row:
                    _id, sym, is_pct, val, rec = row
                    txt = f"Alert #{_id}\n{sym}\nТип: {'%' if is_pct else '$'}\nЗначение: {val}\nПостоянный: {'Да' if rec else 'Нет'}"
                    kb = {"inline_keyboard":[
                        [{"text":"✏ Изменить", "callback_data":f"alert_edit_{_id}"}, {"text":"❌ Удалить", "callback_data":f"alert_del_{_id}"}],
                        [{"text":"⬅️ Назад", "callback_data":"list_alerts"}, {"text":"🏠 Главное меню", "callback_data":"main"}]
                    ]}
                    edit_message(chat_id, message_id, txt, kb)
                else:
                    edit_message(chat_id, message_id, "Не найден алерт.", kb_alerts_menu())
                answer_callback(cb_id)
                return {"ok": True}

            if data_cb.startswith("alert_del_"):
                aid = int(data_cb.split("_")[-1])
                delete_alert(aid, chat_id)
                edit_message(chat_id, message_id, "✅ Алерт удалён.", kb_alerts_menu())
                answer_callback(cb_id)
                return {"ok": True}

            if data_cb.startswith("alert_edit_"):
                aid = int(data_cb.split("_")[-1])
                # ask for new value
                pending_alerts[str(chat_id)] = {"state":"awaiting_edit_value", "edit_aid":aid}
                send_message(chat_id, "Введите новое значение (число):", {"inline_keyboard":[[{"text":"🏠 Главное меню","callback_data":"main"}]]})
                answer_callback(cb_id)
                return {"ok": True}

            # Autosignals
            if data_cb == "autosignals_menu":
                edit_message(chat_id, message_id, "🤖 Автосигналы: выберите монету", kb_autosignals_menu(get_user_symbols(chat_id)))
                answer_callback(cb_id)
                return {"ok": True}

            if data_cb.startswith("autosignal_select_"):
                symbol = data_cb.split("_",2)[2]
                edit_message(chat_id, message_id, f"Вы выбрали {symbol}. Выберите таймфрейм:", kb_timeframe_select(symbol))
                answer_callback(cb_id)
                return {"ok": True}

            if data_cb.startswith("autosignal_tf_"):
                # format autosignal_tf_<symbol>_<tf>
                parts = data_cb.split("_",3)
                if len(parts) >= 3:
                    _, _, sym_tf = parts[0], parts[1], parts[2]
                # safer parse:
                try:
                    # data_cb = autosignal_tf_<symbol>_<tf>
                    _, _, rest = data_cb.split("_",2)
                    # rest e.g. "BTC/USDT_1m" or "BTC/USDT_1h"
                    if "_" in rest:
                        sym, tf = rest.rsplit("_",1)
                    else:
                        # fallback
                        sym = rest
                        tf = "1m"
                except Exception:
                    logger.exception("parse autosignal_tf failed")
                    answer_callback(cb_id, "Ошибка выбора таймфрейма")
                    return {"ok": True}
                upsert_autosignal(chat_id, sym, tf, enabled=1)
                send_message(chat_id, f"✅ Подписка автосигналов для {sym} {tf} активирована.", kb_main_menu())
                answer_callback(cb_id)
                return {"ok": True}

            if data_cb == "list_autosignals":
                rows = list_autosignals(chat_id)
                if not rows:
                    edit_message(chat_id, message_id, "У вас нет автосигналов.", kb_autosignals_menu(get_user_symbols(chat_id)))
                else:
                    edit_message(chat_id, message_id, "Ваши автосигналы:", kb_autosignals_list(rows))
                answer_callback(cb_id)
                return {"ok": True}

            if data_cb.startswith("autosignal_del_"):
                aid = int(data_cb.split("_")[-1])
                delete_autosignal(aid)
                edit_message(chat_id, message_id, "✅ Автосигнал удалён.", kb_autosignals_menu(get_user_symbols(chat_id)))
                answer_callback(cb_id)
                return {"ok": True}

            if data_cb == "settings_menu":
                edit_message(chat_id, message_id, "⚙️ Настройки", {"inline_keyboard":[[{"text":"🏠 Главное меню","callback_data":"main"}]]})
                answer_callback(cb_id)
                return {"ok": True}

            # Unknown callback
            answer_callback(cb_id, "Неизвестная кнопка")
            logger.warning("Unknown callback: %s", data_cb)
            return {"ok": True}

        # Message handling (text)
        msg = data.get("message") or data.get("edited_message")
        if not msg:
            return {"ok": True}
        chat_id = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id:
            return {"ok": True}
        add_user(chat_id)
        log_db("info", f"msg from {chat_id}: {text}")

        # If user is in pending flow:
        key = str(chat_id)
        if key in pending_alerts:
            state = pending_alerts[key].get("state")
            if state == "awaiting_value":
                # expecting numeric value and also symbol selection -> for simplicity ask symbol as well
                try:
                    val = float(text.replace(",", "."))
                except Exception:
                    send_message(chat_id, "Неверное число. Введите просто число (например: 5.5):", {"inline_keyboard":[[{"text":"🏠 Главное меню","callback_data":"main"}]]})
                    return {"ok": True}
                is_pct = bool(pending_alerts[key].get("is_percent"))
                # ask for symbol
                pending_alerts[key].update({"value": val, "is_percent": is_pct, "state":"awaiting_symbol_for_alert"})
                syms = get_user_symbols(chat_id)
                # build keyboard of symbols
                rows = []
                row = []
                for i, s in enumerate(syms):
                    row.append({"text": s.split("/")[0], "callback_data": f"alert_symbol_{s}"})
                    if (i+1) % 3 == 0:
                        rows.append(row); row=[]
                if row: rows.append(row)
                rows.append([{"text":"🏠 Главное меню", "callback_data":"main"}])
                send_message(chat_id, f"Выбрано {'%' if is_pct else '$'}{val}. Выберите монету для алерта:", {"inline_keyboard": rows})
                return {"ok": True}

            if pending_alerts[key].get("state") == "awaiting_symbol_for_alert":
                # maybe user typed symbol directly
                symbol = text.upper().strip()
                if "/" not in symbol:
                    symbol = f"{symbol}/USDT"
                # validate market
                try:
                    markets = exchange.load_markets()
                    if symbol in markets:
                        a = pending_alerts[key]
                        save_alert(chat_id, symbol, int(a["is_percent"]), a["value"], is_recurring=0)
                        send_message(chat_id, f"✅ Алерт сохранён: {symbol} {'%' if a['is_percent'] else '$'}{a['value']}", kb_main_menu())
                        del pending_alerts[key]
                        return {"ok": True}
                    else:
                        send_message(chat_id, f"Пара {symbol} не найдена на MEXC. Введите существующую пару (например BTC/USDT).")
                        return {"ok": True}
                except Exception:
                    logger.exception("validate symbol failed")
                    send_message(chat_id, "Ошибка проверки символа.")
                    return {"ok": True}

            if pending_alerts[key].get("state") == "awaiting_edit_value":
                try:
                    new_val = float(text.replace(",","."))
                    aid = pending_alerts[key].get("edit_aid")
                    with db_lock:
                        cur.execute("UPDATE alerts SET value=? WHERE id=?", (new_val, aid))
                        conn.commit()
                    send_message(chat_id, f"✅ Алерт #{aid} обновлён на {new_val}", kb_main_menu())
                    del pending_alerts[key]
                except Exception:
                    logger.exception("edit alert value failed")
                    send_message(chat_id, "Ошибка при обновлении алерта.")
                return {"ok": True}

        # normal text commands and shortcuts
        if text.startswith("/start"):
            send_message(chat_id, ("👋 Привет! Я твой CryptoBot.\n"
                                    "Управление через кнопки внизу."), kb_main_menu())
            return {"ok": True}

        if text.startswith("/price"):
            parts = text.split()
            if len(parts) == 2:
                s = parts[1].upper()
                if "/" not in s: s = s + "/USDT"
                p = last_prices.get(s) or fetch_price_for(s)
                send_message(chat_id, f"💰 {s}: {p if p is not None else 'N/A'}$", kb_main_menu())
                return {"ok": True}
            else:
                send_message(chat_id, "Использование: /price SYMBOL (например /price BTC)", kb_main_menu())
                return {"ok": True}

        if text.startswith("/chart"):
            # /chart SYMBOL [TF]
            parts = text.split()
            if len(parts) >= 2:
                sym = parts[1].upper()
                if "/" not in sym: sym = sym + "/USDT"
                tf = parts[2] if len(parts) >= 3 else "1h"
                buf, err = build_candlestick_chart(sym, timeframe=tf, limit=200)
                if buf:
                    send_photo_bytes(chat_id, buf, caption=f"{sym} {tf} свечи")
                else:
                    send_message(chat_id, f"Не удалось построить график: {err}", kb_main_menu())
            else:
                send_message(chat_id, "Использование: /chart SYMBOL [timeframe]\nНапример: /chart BTC 1h", kb_main_menu())
            return {"ok": True}

        if text.startswith("/alerts"):
            rows = list_alerts(chat_id)
            if not rows:
                send_message(chat_id, "У вас нет алертов.", kb_alerts_menu())
            else:
                # show list
                lines = []
                for r in rows:
                    aid, sym, is_pct, val, rec, active = r
                    lines.append(f"{aid}: {sym} {'%' if is_pct else '$'}{val} {'(активен)' if active else '(неактивен)'}")
                send_message(chat_id, "📋 Мои алерты:\n" + "\n".join(lines), kb_alerts_menu())
            return {"ok": True}

        # fallback show main menu
        send_message(chat_id, "Выберите действие:", kb_main_menu())
        return {"ok": True}

    except Exception:
        logger.exception("telegram_webhook exception")
        return {"ok": True}

# ---------------- Start background threads ----------------
def safe_thread(target, name):
    def wrapper():
        logger.info("Thread %s started", name)
        while True:
            try:
                target()
                break
            except Exception:
                logger.exception("Thread %s crashed, restarting in 5s", name)
                time.sleep(5)
    t = threading.Thread(target=wrapper, daemon=True, name=name)
    t.start()
    return t

# Start price polling thread
safe_thread(price_poll_worker, "price_poll_worker")

# Start periodic autosignal checker thread (separate cadence)
def autosignal_worker():
    logger.info("Autosignal worker started")
    while True:
        try:
            check_autosignals()
        except Exception:
            logger.exception("autosignal_worker exception")
        time.sleep(30)  # check every 30s (adjustable)

safe_thread(autosignal_worker, "autosignal_worker")

# ---------------- Run Flask app ----------------
if __name__ == "__main__":
    logger.info("Starting Flask webhook on port %s", PORT)
    # If running locally, you can set webhook manually using setWebhook to http://<yourhost>/{TELEGRAM_TOKEN}
    app.run(host="0.0.0.0", port=PORT)
