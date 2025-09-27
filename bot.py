
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Полный bot.py — Telegram crypto bot
Функции:
- Webhook (Flask) на /<TELEGRAM_TOKEN>
- MEXC via ccxt для цен и OHLCV
- SQLite persistence (users, user_symbols, alerts, history, logs, autosignals)
- Inline-button UI (меню, графики, автосигналы, алерты)
- Автосигналы: RSI, EMA crossover, Combo
- Сохраняет историю цен, строит свечной график + EMA + RSI
- Логирование (Railway-friendly). Ошибки пишутся в лог и не падают процесс.
"""

import os
import io
import json
import time
import math
import logging
import threading
import sqlite3
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque

# external libraries (make sure installed)
try:
    import requests
    import ccxt
    import pandas as pd
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
except Exception as e:
    print("Missing dependency:", e)
    raise

from flask import Flask, request

# ---------------- Configuration ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN not found in environment. Set TELEGRAM_TOKEN in Railway project variables.")

BOT_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SEND_MESSAGE_URL = BOT_API + "/sendMessage"
EDIT_MESSAGE_URL = BOT_API + "/editMessageText"
SEND_PHOTO_URL = BOT_API + "/sendPhoto"
ANSWER_CB_URL = BOT_API + "/answerCallbackQuery"
GET_WEBHOOK_INFO_URL = BOT_API + "/getWebhookInfo"

PORT = int(os.getenv("PORT", "8000"))
DB_PATH = os.getenv("DB_PATH", "bot_data.sqlite")
PRICE_POLL_INTERVAL = int(os.getenv("PRICE_POLL_INTERVAL", "10"))  # seconds between basic price polls
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "1000"))

DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "NEAR/USDT"]
TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("crypto_bot")

# ---------------- Exchange ----------------
exchange = ccxt.mexc({"enableRateLimit": True})

# ---------------- DB init ----------------
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()

def init_db():
    try:
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
        CREATE TABLE IF NOT EXISTS level_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            symbol TEXT,
            target REAL,
            alert_type TEXT DEFAULT 'cross', -- 'above','below','cross'
            is_recurring INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS change_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            symbol TEXT,
            change_percent REAL DEFAULT NULL,
            change_amount REAL DEFAULT NULL,
            period_seconds INTEGER DEFAULT 300,
            is_recurring INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS autosignals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            symbol TEXT,
            timeframe TEXT,
            strategy TEXT, -- 'RSI','EMA','COMBO'
            params TEXT,   -- json for extras (e.g. ema_fast, ema_slow, rsi_period, threshold)
            check_interval INTEGER DEFAULT 60,
            active INTEGER DEFAULT 1,
            last_fired DATETIME DEFAULT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts_sent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            symbol TEXT,
            msg TEXT,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP
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
        conn.commit()
        logger.info("DB initialized at %s", DB_PATH)
    except Exception:
        logger.exception("init_db error")

init_db()

# ---------------- In-memory runtime ----------------
last_prices = {}  # symbol -> last price
history_cache = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))  # symbol -> deque((ts, price))
pending_states = {}  # for interactive flows: chat_id -> dict
autosignal_threads = {}  # id -> thread handle (not persisted)

# ---------------- DB helpers ----------------
def db_commit():
    try:
        conn.commit()
    except Exception:
        logger.exception("db commit error")

def log_db(level, message):
    try:
        cur.execute("INSERT INTO logs (level, message) VALUES (?, ?)", (level.upper(), str(message)))
        db_commit()
    except Exception:
        logger.exception("log_db insert error")
    getattr(logger, level.lower())(message)

def add_user(chat_id):
    try:
        cur.execute("INSERT OR IGNORE INTO users (chat_id, created_at) VALUES (?, ?)", (str(chat_id), datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
        db_commit()
    except Exception:
        logger.exception("add_user error")

def get_user_symbols(chat_id):
    try:
        cur.execute("SELECT symbol FROM user_symbols WHERE chat_id=?", (str(chat_id),))
        rows = cur.fetchall()
        if rows:
            return [r[0] for r in rows]
        else:
            # default fill for new users
            for s in DEFAULT_SYMBOLS:
                add_user_symbol(chat_id, s)
            return DEFAULT_SYMBOLS.copy()
    except Exception:
        logger.exception("get_user_symbols")
        return DEFAULT_SYMBOLS.copy()

def add_user_symbol(chat_id, symbol):
    try:
        cur.execute("INSERT INTO user_symbols (chat_id, symbol) VALUES (?, ?)", (str(chat_id), symbol))
        db_commit()
        return True
    except Exception:
        logger.exception("add_user_symbol error")
        return False

def list_level_alerts(chat_id):
    try:
        cur.execute("SELECT id, symbol, target, alert_type, is_recurring FROM level_alerts WHERE chat_id=? ORDER BY id DESC", (str(chat_id),))
        return cur.fetchall()
    except Exception:
        logger.exception("list_level_alerts")
        return []

def save_level_alert(chat_id, symbol, target, alert_type='cross', is_recurring=0):
    try:
        cur.execute("INSERT INTO level_alerts (chat_id, symbol, target, alert_type, is_recurring) VALUES (?, ?, ?, ?, ?)",
                    (str(chat_id), symbol, float(target), alert_type, int(is_recurring)))
        db_commit()
        return True
    except Exception:
        logger.exception("save_level_alert")
        return False

def save_change_alert(chat_id, symbol, change_percent=None, change_amount=None, period_seconds=300, is_recurring=1):
    try:
        cur.execute("INSERT INTO change_alerts (chat_id, symbol, change_percent, change_amount, period_seconds, is_recurring) VALUES (?, ?, ?, ?, ?, ?)",
                    (str(chat_id), symbol, change_percent, change_amount, int(period_seconds), int(is_recurring)))
        db_commit()
        return True
    except Exception:
        logger.exception("save_change_alert")
        return False

def save_autosignal(chat_id, symbol, timeframe, strategy, params, check_interval=60):
    try:
        cur.execute("INSERT INTO autosignals (chat_id, symbol, timeframe, strategy, params, check_interval, active) VALUES (?, ?, ?, ?, ?, ?, 1)",
                    (str(chat_id), symbol, timeframe, strategy, json.dumps(params), int(check_interval)))
        db_commit()
        return True
    except Exception:
        logger.exception("save_autosignal")
        return False

def list_autosignals(chat_id):
    try:
        cur.execute("SELECT id, symbol, timeframe, strategy, params, check_interval, active FROM autosignals WHERE chat_id=? ORDER BY id DESC", (str(chat_id),))
        return cur.fetchall()
    except Exception:
        logger.exception("list_autosignals")
        return []

def deactivate_autosignal(signal_id, chat_id):
    try:
        cur.execute("UPDATE autosignals SET active=0 WHERE id=? AND chat_id=?", (int(signal_id), str(chat_id)))
        db_commit()
        return True
    except Exception:
        logger.exception("deactivate_autosignal")
        return False

# ---------------- Telegram helpers ----------------
def send_message(chat_id, text, reply_markup=None, parse_mode="HTML"):
    try:
        payload = {"chat_id": str(chat_id), "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            # telegram wants JSON for reply_markup
            if isinstance(reply_markup, dict):
                payload["reply_markup"] = json.dumps(reply_markup)
            else:
                payload["reply_markup"] = reply_markup
        r = requests.post(SEND_MESSAGE_URL, data=payload, timeout=10)
        try:
            return r.json()
        except Exception:
            return {"ok": False, "status_code": r.status_code, "text": r.text}
    except Exception:
        logger.exception("send_message exception")
        return {}

def edit_message(chat_id, message_id, text, reply_markup=None):
    try:
        payload = {"chat_id": str(chat_id), "message_id": int(message_id), "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        r = requests.post(EDIT_MESSAGE_URL, data=payload, timeout=10)
        try:
            return r.json()
        except Exception:
            return {"ok": False, "status_code": r.status_code, "text": r.text}
    except Exception:
        logger.exception("edit_message exception")
        return {}

def answer_callback(callback_query_id, text=None, show_alert=False):
    try:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        if show_alert:
            payload["show_alert"] = True
        requests.post(ANSWER_CB_URL, data=payload, timeout=5)
    except Exception:
        logger.exception("answer_callback exception")

def send_photo_bytes(chat_id, buf: io.BytesIO, caption=None):
    try:
        buf.seek(0)
        files = {"photo": ("chart.png", buf.read())}
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
            data["parse_mode"] = "HTML"
        r = requests.post(SEND_PHOTO_URL, files=files, data=data, timeout=30)
        try:
            return r.json()
        except Exception:
            return {"ok": False, "status_code": r.status_code, "text": r.text}
    except Exception:
        logger.exception("send_photo exception")
        return {}

# ---------------- Keyboards ----------------
def main_menu_kb():
    return {"inline_keyboard": [
        [{"text": "💰 Цена (все монеты)", "callback_data": "menu_price_all"}],
        [{"text": "📈 График", "callback_data": "menu_chart"}],
        [{"text": "🔔 Алерты (уровень)", "callback_data": "menu_level_alert"}],
        [{"text": "⚡ Изменение (%, $)", "callback_data": "menu_change_alert"}],
        [{"text": "🤖 Автосигналы", "callback_data": "menu_autosignals"}],
        [{"text": "⚙️ Мои монеты", "callback_data": "menu_my_symbols"}],
    ]}

def symbols_kb(prefix, chat_id):
    syms = get_user_symbols(chat_id)
    rows = []
    row = []
    for i, s in enumerate(syms, 1):
        row.append({"text": s, "callback_data": f"{prefix}|{s}"})
        if i % 2 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text":"➕ Добавить вручную","callback_data":"add_symbol_text"},{"text":"⬅️ Назад","callback_data":"back_main"}])
    return {"inline_keyboard": rows}

def timeframe_kb(symbol):
    rows = []
    row = []
    for i, tf in enumerate(TIMEFRAMES, 1):
        row.append({"text": tf, "callback_data": f"tf|{symbol}|{tf}"})
        if i % 3 == 0:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([{"text":"⬅️ Назад","callback_data":"menu_chart"}])
    return {"inline_keyboard": rows}

def chart_type_source_kb(symbol, tf):
    return {"inline_keyboard":[
        [{"text":"🕯 Свечи (биржа)","callback_data":f"chart|{symbol}|{tf}|exchange|candles"},
         {"text":"📉 Линия (биржа)","callback_data":f"chart|{symbol}|{tf}|exchange|line"}],
        [{"text":"🕯 Свечи (история)","callback_data":f"chart|{symbol}|{tf}|history|candles"},
         {"text":"📉 Линия (история)","callback_data":f"chart|{symbol}|{tf}|history|line"}],
        [{"text":"⬅️ Назад","callback_data":"menu_chart"}]
    ]}

def autosignal_menu_kb(chat_id):
    return {"inline_keyboard":[
        [{"text":"➕ Добавить автосигнал","callback_data":"autosignal_add"}],
        [{"text":"📋 Мои автосигналы","callback_data":"autosignal_list"}],
        [{"text":"⬅️ Назад","callback_data":"back_main"}]
    ]}

# ---------------- Price polling (history) ----------------
def fetch_and_save_prices_loop():
    while True:
        try:
            # symbols to fetch: union of default and user symbols
            cur.execute("SELECT DISTINCT symbol FROM user_symbols")
            rows = cur.fetchall()
            symbols = set(DEFAULT_SYMBOLS)
            symbols.update([r[0] for r in rows])
            for sym in list(symbols):
                try:
                    ticker = exchange.fetch_ticker(sym)
                    price = float(ticker.get("last") or ticker.get("close") or 0.0)
                    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                    last_prices[sym] = price
                    # save to DB
                    cur.execute("INSERT INTO history (symbol, price, ts) VALUES (?, ?, ?)", (sym, price, ts))
                    db_commit()
                    history_cache[sym].append((ts, price))
                except Exception as e:
                    logger.debug("fetch price error for %s: %s", sym, str(e))
            # check change and level alerts periodically
            check_level_alerts()
            check_change_alerts()
        except Exception:
            logger.exception("price polling loop error")
        time.sleep(max(1, PRICE_POLL_INTERVAL))

# ---------------- Alerts checking ----------------
def check_level_alerts():
    try:
        cur.execute("SELECT id, chat_id, symbol, target, alert_type, is_recurring FROM level_alerts")
        rows = cur.fetchall()
        for row in rows:
            aid, chat_id, symbol, target, alert_type, is_recurring = row
            price = last_prices.get(symbol)
            if price is None:
                continue
            triggered = False
            if alert_type == 'above' and price >= target:
                triggered = True
            elif alert_type == 'below' and price <= target:
                triggered = True
            elif alert_type == 'cross':
                hist = list(history_cache.get(symbol, []))
                prev = hist[-2][1] if len(hist) >= 2 else None
                if prev is not None and ((prev < target and price >= target) or (prev > target and price <= target)):
                    triggered = True
            if triggered:
                try:
                    msg = f"🔔 ALARM {symbol}: reached {target}$ (now {price}$)"
                    send_message(chat_id, msg)
                    log_db("info", f"level alert fired: {chat_id} {symbol} {target}")
                    cur.execute("INSERT INTO alerts_sent (chat_id, symbol, msg) VALUES (?, ?, ?)", (chat_id, symbol, msg))
                    db_commit()
                except Exception:
                    logger.exception("sending level alert")
                if not is_recurring:
                    cur.execute("DELETE FROM level_alerts WHERE id=?", (aid,))
                    db_commit()
    except Exception:
        logger.exception("check_level_alerts error")

def check_change_alerts():
    try:
        cur.execute("SELECT id, chat_id, symbol, change_percent, change_amount, period_seconds, is_recurring FROM change_alerts")
        rows = cur.fetchall()
        for row in rows:
            aid, chat_id, symbol, pct, amt, period_s, is_rec = row
            hist = list(history_cache.get(symbol, []))
            if not hist:
                continue
            now_price = hist[-1][1]
            # find price ~ period_s seconds ago; since we store snapshots at PRICE_POLL_INTERVAL,
            # approximate by looking back N points
            lookback_points = max(1, int(period_s / max(1, PRICE_POLL_INTERVAL)))
            if len(hist) >= lookback_points + 1:
                past_price = hist[-1 - lookback_points][1]
            else:
                past_price = hist[0][1]
            # compute changes
            change_amount = abs(now_price - past_price)
            change_pct = (abs(now_price - past_price) / past_price) * 100 if past_price != 0 else 0
            triggered = False
            if pct is not None and pct > 0 and change_pct >= pct:
                triggered = True
            if amt is not None and amt > 0 and change_amount >= amt:
                triggered = True
            if triggered:
                try:
                    msg = f"⚡ {symbol} moved by {change_amount:.2f}$ ({change_pct:.2f}%) in last {period_s}s — now {now_price}$"
                    send_message(chat_id, msg)
                    cur.execute("INSERT INTO alerts_sent (chat_id, symbol, msg) VALUES (?, ?, ?)", (chat_id, symbol, msg))
                    db_commit()
                    log_db("info", f"change alert fired: {chat_id} {symbol}")
                except Exception:
                    logger.exception("sending change alert")
                if not is_rec:
                    cur.execute("DELETE FROM change_alerts WHERE id=?", (aid,))
                    db_commit()
    except Exception:
        logger.exception("check_change_alerts error")

# ---------------- Charting ----------------
def fetch_ohlcv_exchange(symbol, timeframe, limit=200):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        df.set_index("ts", inplace=True)
        return df
    except Exception:
        logger.exception("fetch_ohlcv_exchange")
        return pd.DataFrame()

def fetch_ohlcv_history(symbol, timeframe, limit=200):
    try:
        cur.execute("SELECT ts, price FROM history WHERE symbol=? ORDER BY id DESC LIMIT ?", (symbol, limit))
        rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
        rows = rows[::-1]
        times = [datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S") for r in rows]
        prices = [r[1] for r in rows]
        df = pd.DataFrame({"close": prices}, index=pd.to_datetime(times))
        # approximate open/high/low
        df["open"] = df["close"].shift(1).fillna(df["close"])
        df["high"] = df["close"].rolling(3, min_periods=1).max()
        df["low"] = df["close"].rolling(3, min_periods=1).min()
        df["volume"] = 0
        df = df[["open","high","low","close","volume"]]
        return df

    except Exception:
        logger.exception("fetch_ohlcv_history")
        return pd.DataFrame()

def plot_candles_with_indicators(df, symbol, timeframe, mark_index=None, ema_list=None, show_rsi=True):
    try:
        fig = plt.figure(figsize=(10,6))
        gs = fig.add_gridspec(3, 1, height_ratios=[3, 1, 0.2])  # price, rsi, small
        ax_price = fig.add_subplot(gs[0,0])
        ax_rsi = fig.add_subplot(gs[1,0], sharex=ax_price)
        # convert index to matplotlib dates
        dates = mdates.date2num(df.index.to_pydatetime())
        width = (dates[1]-dates[0]) * 0.6 if len(dates) > 1 else 0.0005
        # draw candles
        for i in range(len(df)):
            o = df["open"].iat[i]
            c = df["close"].iat[i]
            h = df["high"].iat[i]
            l = df["low"].iat[i]
            t = dates[i]
            color = "green" if c >= o else "red"
            ax_price.plot([t, t], [l, h], color="k", linewidth=0.6)
            rect_bottom = min(o, c)
            rect_height = max(abs(c - o), 1e-8)
            ax_price.add_patch(plt.Rectangle((t - width/2, rect_bottom), width, rect_height, color=color, alpha=0.9))
        # ema overlays
        if ema_list:
            for e in ema_list:
                df[f"EMA{e}"] = df["close"].ewm(span=e, adjust=False).mean()
                ax_price.plot(df.index, df[f"EMA{e}"], label=f"EMA{e}")
            ax_price.legend()
        ax_price.grid(True, linestyle="--", alpha=0.3)
        ax_price.set_title(f"{symbol} — {timeframe}")
        # RSI
        if show_rsi:
            period = 14
            delta = df['close'].diff()
            up = delta.clip(lower=0)
            down = -delta.clip(upper=0)
            ma_up = up.rolling(window=period).mean()
            ma_down = down.rolling(window=period).mean()
            rs = ma_up / ma_down
            rsi = 100 - 100/(1+rs)
            ax_rsi.plot(df.index, rsi, color="purple")
            ax_rsi.axhline(70, color="red", linestyle="--")
            ax_rsi.axhline(30, color="green", linestyle="--")
            ax_rsi.set_ylim(0,100)
            ax_rsi.set_title("RSI(14)")
        # mark entry
        if mark_index is not None and 0 <= mark_index < len(df):
            t = dates[mark_index]
            price = df["close"].iat[mark_index]
            ax_price.annotate("ENTRY", xy=(t, price), xytext=(t, price*1.01),
                              arrowprops=dict(facecolor='blue', shrink=0.05), fontsize=10, color='blue')
        fig.autofmt_xdate()
        buf = io.BytesIO()
        plt.tight_layout()
        fig.savefig(buf, format="png", dpi=150)
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception:
        logger.exception("plot_candles_with_indicators error")
        try:
            plt.close('all')
        except Exception:
            pass
        return None

# ---------------- Strategy helpers ----------------
def calculate_rsi_series(series, period=14):
    s = pd.Series(series)
    delta = s.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ma_up = up.rolling(window=period).mean()
    ma_down = down.rolling(window=period).mean()
    rs = ma_up / ma_down
    rsi = 100 - 100/(1+rs)
    return rsi

def evaluate_strategy_on_df(df, strategy, params):
    # df must have columns open/high/low/close
    res = {"signal": None, "reason": "", "index": None}
    try:
        if df.empty or len(df) < 3:
            return res
        if strategy == "RSI":
            period = params.get("rsi_period", 14)
            threshold_low = params.get("rsi_long", 30)
            threshold_high = params.get("rsi_short", 70)
            rsi = calculate_rsi_series(df["close"], period)
            last_rsi = rsi.iat[-1]
            if last_rsi < threshold_low:
                res["signal"] = "LONG"
                res["reason"] = f"RSI={last_rsi:.2f} < {threshold_low}"
                res["index"] = len(df)-1
            elif last_rsi > threshold_high:
                res["signal"] = "SHORT"
                res["reason"] = f"RSI={last_rsi:.2f} > {threshold_high}"
                res["index"] = len(df)-1
            return res
        elif strategy == "EMA":
            fast = params.get("ema_fast", 9)
            slow = params.get("ema_slow", 21)
            df[f"EMA_fast"] = df["close"].ewm(span=fast, adjust=False).mean()
            df[f"EMA_slow"] = df["close"].ewm(span=slow, adjust=False).mean()
            # detect cross at last candle: previous fast < slow and now fast > slow => bullish cross
            if len(df) < 2:
                return res
            prev_fast = df[f"EMA_fast"].iat[-2]
            prev_slow = df[f"EMA_slow"].iat[-2]
            cur_fast = df[f"EMA_fast"].iat[-1]
            cur_slow = df[f"EMA_slow"].iat[-1]
            if prev_fast < prev_slow and cur_fast > cur_slow:
                res["signal"] = "LONG"
                res["reason"] = f"EMA{fast} crossed above EMA{slow}"
                res["index"] = len(df)-1
            elif prev_fast > prev_slow and cur_fast < cur_slow:
                res["signal"] = "SHORT"
                res["reason"] = f"EMA{fast} crossed below EMA{slow}"
                res["index"] = len(df)-1
            return res
        elif strategy == "COMBO":
            # require both RSI and EMA agree
            rsi_part = evaluate_strategy_on_df(df, "RSI", params)
            ema_part = evaluate_strategy_on_df(df, "EMA", params)
            if rsi_part["signal"] and ema_part["signal"] and rsi_part["signal"] == ema_part["signal"]:
                res["signal"] = rsi_part["signal"]
                res["reason"] = f"Combo: {rsi_part['reason']} & {ema_part['reason']}"
                res["index"] = rsi_part.get("index", ema_part.get("index"))
            return res
        else:
            return res
    except Exception:
        logger.exception("evaluate_strategy_on_df error")
        return res

# ---------------- Autosignal worker ----------------
def autosignal_worker_thread(signal_row):
    """
    signal_row: (id, chat_id, symbol, timeframe, strategy, params_json, check_interval, active)
    runs in a loop while active=1
    """
    try:
        sid, chat_id, symbol, timeframe, strategy, params_json, check_interval, active = signal_row
        params = json.loads(params_json) if params_json else {}
    except Exception:
        logger.exception("autosignal_worker init error")
        return

    logger.info("Starting autosignal worker for id=%s %s %s %s", sid, chat_id, symbol, timeframe)
    while True:
        try:
            # check DB active flag
            cur.execute("SELECT active FROM autosignals WHERE id=?", (sid,))
            row = cur.fetchone()
            if not row or row[0] == 0:
                logger.info("Autosignal id=%s deactivated, stopping thread", sid)
                return
            # fetch OHLCV
            df = fetch_ohlcv_exchange(symbol, timeframe, limit=200)
            source = "exchange"
            if df.empty:
                # fallback to history
                df = fetch_ohlcv_history(symbol, timeframe, limit=200)
                source = "history"
            if df.empty:
                logger.debug("no data for autosignal %s %s", sid, symbol)
            else:
                eval_res = evaluate_strategy_on_df(df, strategy, params)
                if eval_res.get("signal"):
                    # send alert + chart
                    signal = eval_res["signal"]
                    reason = eval_res.get("reason", "")
                    idx = eval_res.get("index")
                    msg = f"[{strategy}] {symbol} ({timeframe})\n🔔 <b>{signal}</b>\n{reason}"
                    try:
                        # build chart with EMA overlays if present
                        ema_list = []
                        if strategy in ("EMA", "COMBO"):
                            ema_fast = params.get("ema_fast", 9)
                            ema_slow = params.get("ema_slow", 21)
                            ema_list = [ema_fast, ema_slow]
                        # always show EMA lines if provided in params
                        buf = plot_candles_with_indicators(df, symbol, timeframe, mark_index=idx, ema_list=ema_list, show_rsi=(strategy in ("RSI","COMBO")))
                        send_message(chat_id, msg)
                        if buf:
                            send_photo_bytes(chat_id, buf, caption=f"{symbol} {timeframe} — {strategy} ({signal})")
                        # record last_fired and avoid immediate duplicate: update DB last_fired
                        cur.execute("UPDATE autosignals SET last_fired=? WHERE id=?", (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), sid))
                        db_commit()
                        # small cooldown: sleep check_interval (DB controls re-fires)
                    except Exception:
                        logger.exception("failed to send autosignal message")
            time.sleep(max(1, int(check_interval)))
        except Exception:
            logger.exception("autosignal_worker loop error")
            time.sleep(5)

def start_all_autosignal_workers():
    try:
        cur.execute("SELECT id, chat_id, symbol, timeframe, strategy, params, check_interval, active FROM autosignals WHERE active=1")
        rows = cur.fetchall()
        for r in rows:
            sid = r[0]
            if sid in autosignal_threads:
                continue
            t = threading.Thread(target=autosignal_worker_thread, args=(r,), daemon=True)
            t.start()
            autosignal_threads[sid] = t
            logger.info("Started autosignal thread for id=%s", sid)
    except Exception:
        logger.exception("start_all_autosignal_workers")

# ---------------- HTTP/Webhook handlers ----------------
app = Flask(__name__)

def handle_text(chat_id, text):
    try:
        add_user(chat_id)
        state = pending_states.get(str(chat_id), {})
        text = text.strip()
        # if we are awaiting something
        if state.get("awaiting_new_symbol"):
            symbol = text.upper()
            if "/" not in symbol:
                symbol = f"{symbol}/USDT"
            ok = add_user_symbol(chat_id, symbol)
            send_message(chat_id, f"✅ Добавлено: {symbol}" if ok else f"Ошибка добавления {symbol}", reply_markup=main_menu_kb())
            pending_states.pop(str(chat_id), None)
            return

        if state.get("awaiting_level_symbol"):
            symbol = text.upper()
            if "/" not in symbol:
                symbol = f"{symbol}/USDT"
            pending_states[str(chat_id)]["level_symbol"] = symbol
            pending_states[str(chat_id)]["awaiting_level_price"] = True
            send_message(chat_id, f"Введите цену (число) для {symbol}:")
            return

        if state.get("awaiting_level_price"):
            try:
                price = float(text)
                symbol = state.get("level_symbol")
                save_level_alert(chat_id, symbol, price, alert_type='cross', is_recurring=0)
                send_message(chat_id, f"✅ Alert сохранён для {symbol} at {price}$", reply_markup=main_menu_kb())
            except Exception:
                send_message(chat_id, "Ошибка: введите число.", reply_markup=main_menu_kb())
            pending_states.pop(str(chat_id), None)
            return

        if state.get("awaiting_change_symbol"):
            symbol = text.upper()
            if "/" not in symbol:
                symbol = f"{symbol}/USDT"
            pending_states[str(chat_id)]["change_symbol"] = symbol
            pending_states[str(chat_id)]["awaiting_change_params"] = True
            send_message(chat_id, "Введите: percent change (например 5) или amount in $ (например 100) или оба через пробел (e.g. '5 100'):\nТакже укажи период в секундах через пробел, например '5 100 300' для 300s")
            return

        if state.get("awaiting_change_params"):
            parts = text.split()
            try:
                pct = float(parts[0]) if len(parts) >= 1 and parts[0] != "-" else None
                amt = float(parts[1]) if len(parts) >= 2 and parts[1] != "-" else None
                period_s = int(parts[2]) if len(parts) >= 3 else 300
                symbol = state.get("change_symbol")
                save_change_alert(chat_id, symbol, change_percent=pct, change_amount=amt, period_seconds=period_s)
                send_message(chat_id, f"✅ Change alert saved for {symbol}: pct={pct} amt={amt} period={period_s}s", reply_markup=main_menu_kb())
            except Exception:
                send_message(chat_id, "Ошибка параметров. Пример: '5 100 300' или '5 - 300' ('-' = no value).", reply_markup=main_menu_kb())
            pending_states.pop(str(chat_id), None)
            return

        if text.startswith("/start"):
            send_message(chat_id, "Привет! Выберите действие:", reply_markup=main_menu_kb())
            return

        # quick price list via /prices
        if text.startswith("/prices"):
            syms = get_user_symbols(chat_id)
            lines = []
            for s in syms:
                p = last_prices.get(s)
                if p is None:
                    try:
                        tck = exchange.fetch_ticker(s)
                        p = float(tck.get("last") or tck.get("close") or 0.0)
                    except Exception:
                        p = None
                lines.append(f"{s}: {p if p is not None else 'N/A'}$")
            send_message(chat_id, "\n".join(lines), reply_markup=main_menu_kb())
            return

        # fallback show menu
        send_message(chat_id, "Выберите действие:", reply_markup=main_menu_kb())
    except Exception:
        logger.exception("handle_text error")

def handle_callback_query(cb):
    try:
        cb_id = cb.get("id")
        data = cb.get("data","")
        user = cb.get("from",{})
        user_id = user.get("id")
        chat = cb.get("message",{}).get("chat",{}) or {}
        chat_id = chat.get("id") or user_id
        add_user(chat_id)
        log_db("info", f"callback {data} from {chat_id}")
        answer_callback(cb_id)  # remove loading spinner

        # navigation
        if data == "back_main":
            send_message(chat_id, "Главное меню:", reply_markup=main_menu_kb())
            return

        if data == "menu_price_all":
            syms = get_user_symbols(chat_id)
            lines = []
            for s in syms:
                p = last_prices.get(s)
                if p is None:
                    try:
                        tck = exchange.fetch_ticker(s)
                        p = float(tck.get("last") or tck.get("close") or 0.0)
                    except Exception:
                        p = None
                lines.append(f"{s}: {p if p is not None else 'N/A'}$")
            send_message(chat_id, "Цены:\n" + "\n".join(lines), reply_markup=main_menu_kb())
            return

        if data == "menu_chart":
            send_message(chat_id, "Выберите монету:", reply_markup=symbols_kb("chart", chat_id))
            return

        if data.startswith("chart|") and data.count("|") == 4:
            # full: chart|symbol|tf|source|type
            _, symbol, tf, source, ctype = data.split("|")
            try:
                if source == "exchange":
                    df = fetch_ohlcv_exchange(symbol, tf, limit=200)
                else:
                    df = fetch_ohlcv_history(symbol, tf, limit=200)
                if df.empty:
                    send_message(chat_id, "Недостаточно данных для графика.", reply_markup=main_menu_kb())
                    return
                # plot candles
                buf = plot_candles_with_indicators(df, symbol, tf, mark_index=None, ema_list=[9,21], show_rsi=True)
                if buf:
                    send_photo_bytes(chat_id, buf, caption=f"{symbol} {tf} — {source}/{ctype}")
                else:
                    send_message(chat_id, "Ошибка построения графика.", reply_markup=main_menu_kb())
            except Exception:
                logger.exception("chart callback error")
                send_message(chat_id, "Ошибка при построении графика.", reply_markup=main_menu_kb())
            return

        if data.startswith("chart|") and data.count("|") == 1:
            _, symbol = data.split("|",1)
            send_message(chat_id, f"Выберите таймфрейм для {symbol}:", reply_markup=timeframe_kb(symbol))
            return

        if data.startswith("tf|"):
            _, symbol, tf = data.split("|")
            send_message(chat_id, f"Выберите тип графика для {symbol} {tf}:", reply_markup=chart_type_source_kb(symbol, tf))
            return

        # level alert menu
        if data == "menu_level_alert":
            send_message(chat_id, "Добавить уровень - нажмите кнопку и введите монету текстом.", reply_markup={"inline_keyboard":[[{"text":"➕ Добавить уровень","callback_data":"level_add"}],[{"text":"⬅️ Назад","callback_data":"back_main"}]]})
            return

        if data == "level_add":
            pending_states[str(chat_id)] = {"awaiting_level_symbol": True}
            send_message(chat_id, "Введите символ монеты для уровня (например BTC или BTC/USDT):")
            return

        # change alert menu
        if data == "menu_change_alert":
            send_message(chat_id, "Добавить change-алерт. Нажмите кнопку, затем введите монету текстом.", reply_markup={"inline_keyboard":[[{"text":"➕ Добавить change","callback_data":"change_add"}],[{"text":"⬅️ Назад","callback_data":"back_main"}]]})
            return

        if data == "change_add":
            pending_states[str(chat_id)] = {"awaiting_change_symbol": True}
            send_message(chat_id, "Введите символ монеты (например BTC или BTC/USDT):")
            return

        # autosignal menu
        if data == "menu_autosignals":
            send_message(chat_id, "Автосигналы:", reply_markup=autosignal_menu_kb(chat_id))
            return

        if data == "autosignal_add":
            send_message(chat_id, "Выберите монету для автосигнала:", reply_markup=symbols_kb("autosig|select", chat_id))
            return

        if data.startswith("autosig|select|"):
            # data: autosig|select|SYMBOL
            parts = data.split("|",2)
            symbol = parts[2]
            # save temp
            pending_states[str(chat_id)] = {"autosig_symbol": symbol}
            # ask timeframe
            send_message(chat_id, f"Выбрано {symbol}. Выберите таймфрейм:", reply_markup=timeframe_kb(symbol))
            return

        if data.startswith("tf|"):
            # handled earlier to show chart type; but for autosignal flow we also reuse
            # we'll detect pending state
            _, symbol, tf = data.split("|")
            state = pending_states.get(str(chat_id), {})
            if state and state.get("autosig_symbol") == symbol:
                pending_states[str(chat_id)]["autosig_timeframe"] = tf
                # ask strategy
                kb = {"inline_keyboard":[
                    [{"text":"RSI","callback_data":f"autosig|strategy|{symbol}|{tf}|RSI"},
                     {"text":"EMA crossover","callback_data":f"autosig|strategy|{symbol}|{tf}|EMA"}],
                    [{"text":"COMBO (RSI+EMA)","callback_data":f"autosig|strategy|{symbol}|{tf}|COMBO"}],
                    [{"text":"⬅️ Отмена","callback_data":"back_main"}]
                ]}
                send_message(chat_id, "Выберите стратегию:", reply_markup=kb)
                return

        if data.startswith("autosig|strategy|"):
            # autosig|strategy|symbol|tf|STRAT
            _, _, rest = data.partition("|strategy|")
            symbol, tf, strat = rest.split("|")
            state = pending_states.get(str(chat_id), {})
            if not state or state.get("autosig_symbol") != symbol:
                # not in flow; start fresh: store and ask
                pending_states[str(chat_id)] = {"autosig_symbol": symbol, "autosig_timeframe": tf, "autosig_strategy": strat}
            else:
                state["autosig_strategy"] = strat
            # ask check interval options
            kb = {"inline_keyboard":[
                [{"text":"1 min","callback_data":f"autosig|interval|{symbol}|{tf}|{strat}|60"},
                 {"text":"5 min","callback_data":f"autosig|interval|{symbol}|{tf}|{strat}|300"},
                 {"text":"15 min","callback_data":f"autosig|interval|{symbol}|{tf}|{strat}|900"}],
                [{"text":"30 min","callback_data":f"autosig|interval|{symbol}|{tf}|{strat}|1800"},
                 {"text":"1 hour","callback_data":f"autosig|interval|{symbol}|{tf}|{strat}|3600"}],
                [{"text":"⬅️ Назад","callback_data":"menu_autosignals"}]
            ]}
            send_message(chat_id, "Выберите интервал проверки:", reply_markup=kb)
            return

        if data.startswith("autosig|interval|"):
            # autosig|interval|symbol|tf|strat|seconds
            _, _, rest = data.partition("|interval|")
            symbol, tf, strat, sec = rest.split("|")
            sec = int(sec)
            # build params defaults
            params = {"ema_fast": 9, "ema_slow": 21, "rsi_period": 14, "rsi_long": 30, "rsi_short": 70}
            # save autosignal
            ok = save_autosignal(chat_id, symbol, tf, strat, params, check_interval=sec)
            if ok:
                send_message(chat_id, f"✅ Autosignal saved: {symbol} {tf} {strat} every {sec}s", reply_markup=main_menu_kb())
                # start worker for it
                start_all_autosignal_workers()
            else:
                send_message(chat_id, "Ошибка сохранения autosignal.", reply_markup=main_menu_kb())
            pending_states.pop(str(chat_id), None)
            return

        if data == "autosignal_list":
            rows = list_autosignals(chat_id)
            if not rows:
                send_message(chat_id, "У вас нет автосигналов.", reply_markup=main_menu_kb())
                return
            kb = {"inline_keyboard":[ ]}
            for r in rows:
                sid, sym, tf, strat, params_json, interval, active = r[0], r[1], r[2], r[3], r[4], r[5], r[6]
                label = f"{sid}: {sym} {tf} {strat} every {interval}s {'(ON)' if active==1 else '(OFF)'}"
                kb["inline_keyboard"].append([{"text":label,"callback_data":f"autosig|manage|{sid}"}])
            kb["inline_keyboard"].append([{"text":"⬅️ Назад","callback_data":"menu_autosignals"}])
            send_message(chat_id, "Ваши автосигналы:", reply_markup=kb)
            return

        if data.startswith("autosig|manage|"):
            _, _, sid = data.partition("|manage|")
            send_message(chat_id, f"Управление сигналом {sid}", reply_markup={"inline_keyboard":[[{"text":"🛑 Отключить","callback_data":f"autosig|disable|{sid}"},{"text":"⬅️ Назад","callback_data":"autosignal_list"}]]})
            return

        if data.startswith("autosig|disable|"):
            _, _, sid = data.partition("|disable|")
            ok = False
            try:
                cur.execute("UPDATE autosignals SET active=0 WHERE id=?", (int(sid),))
                db_commit()
                ok = True
            except Exception:
                logger.exception("disable autosig")
            if ok:
                send_message(chat_id, f"✅ Autosignal {sid} disabled.", reply_markup=main_menu_kb())
            else:
                send_message(chat_id, "Ошибка отключения.", reply_markup=main_menu_kb())
            return

        # symbols management
        if data == "menu_my_symbols":
            send_message(chat_id, "Управление монетами:", reply_markup=symbols_kb("sym|", chat_id))
            return

        if data.startswith("sym|"):
            _, sym = data.split("|",1)
            send_message(chat_id, f"Выбраная монета: {sym}", reply_markup=main_menu_kb())
            return

        if data == "add_symbol_text":
            pending_states[str(chat_id)] = {"awaiting_new_symbol": True}
            send_message(chat_id, "Введите символ (например ADA или ADA/USDT):")
            return

        # unknown
        send_message(chat_id, "Неизвестная кнопка. Возврат в меню.", reply_markup=main_menu_kb())
    except Exception:
        logger.exception("handle_callback_query error")

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    try:
        update = request.get_json(force=True)
        try:
            logger.info("Incoming update: %s", json.dumps(update, ensure_ascii=False))
        except Exception:
            logger.info("Incoming update (raw): %s", str(update))
        if "callback_query" in update:
            handle_callback_query(update["callback_query"])
            return {"ok": True}
        if "message" in update:
            msg = update["message"]
            chat_id = msg.get("chat",{}).get("id")
            text = (msg.get("text") or "")
            handle_text(chat_id, text)
            return {"ok": True}
        return {"ok": True}
    except Exception:
        logger.exception("webhook exception")
        return {"ok": True}

# ---------------- Start background workers ----------------
def start_background_workers():
    try:
        t = threading.Thread(target=fetch_and_save_prices_loop, daemon=True)
        t.start()
        logger.info("Started price polling worker.")
    except Exception:
        logger.exception("start price poller error")
    try:
        start_all_autosignal_workers()
    except Exception:
        logger.exception("start autosignal workers error")

if __name__ == "__main__":
    start_background_workers()
    logger.info("Starting Flask webhook on port %s", PORT)
    # in Railway use gunicorn; for local debugging app.run is fine
    try:
        app.run(host="0.0.0.0", port=PORT)
    except Exception:
        logger.exception("Flask run error")
