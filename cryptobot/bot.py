#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Полный bot.py — интегрированный:
- Flask webhook (маршрут /<TELEGRAM_TOKEN>)
- MEXC via ccxt (fetch_ticker, fetch_ohlcv)
- SQLite persistence (users, user_symbols, alerts, history, logs)
- Reply keyboard (главное меню) + inline клавиатуры (интервалы, монеты, шаги цены)
- Сигналы: above/below/cross, одноразовые/recurring, time window
- Автоподтверждение (autotime)
- Charts: candlestick (1m,5m,15m,30m,1h,4h,1d) via ccxt.fetch_ohlcv + matplotlib, отправка в чат (без сохранения)
- Логирование и защита от падений
- Токен: берётся из окружения, если нет — спрашивается и записывается в .env
"""

import os
import sys
import time
import json
import io
import math
import threading
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque

# External libraries
try:
    import requests
except Exception as e:
    print("Install 'requests' package. Error:", e)
    raise

try:
    import ccxt
except Exception as e:
    print("Install 'ccxt' package. Error:", e)
    raise

# pandas/matplotlib are optional for charts — we try to import and if missing we'll still run other features
try:
    import pandas as pd
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import numpy as np
    HAS_CHARTS = True
except Exception:
    HAS_CHARTS = False

from flask import Flask, request, abort

# ---------------- Configuration & token handling ----------------

ENV_PATH = ".env"

def load_env():
    """Simple .env loader (only KEY=VALUE lines)."""
    env = {}
    if os.path.exists(ENV_PATH):
        try:
            with open(ENV_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
        except Exception:
            pass
    return env

def save_env(var_dict):
    """Append or update keys in .env file (simple implementation)."""
    current = load_env()
    current.update(var_dict)
    try:
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            for k, v in current.items():
                f.write(f"{k}={v}\n")
    except Exception as e:
        logger = logging.getLogger("crypto_bot")
        logger.exception("Failed to save .env: %s", e)

# load existing .env values into os.environ if not present
_env = load_env()
for k, v in _env.items():
    if k not in os.environ:
        os.environ[k] = v

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("crypto_bot")

# TELEGRAM_TOKEN: from env or prompt
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    try:
        # prompt in interactive mode
        TELEGRAM_TOKEN = input("Введите TELEGRAM_TOKEN (скопируйте из BotFather): ").strip()
        if TELEGRAM_TOKEN:
            os.environ["TELEGRAM_TOKEN"] = TELEGRAM_TOKEN
            # save to .env
            try:
                save_env({"TELEGRAM_TOKEN": TELEGRAM_TOKEN})
                logger.info(".env записан с TELEGRAM_TOKEN")
            except Exception:
                logger.exception("Не удалось записать .env")
        else:
            raise RuntimeError("TELEGRAM_TOKEN не введён")
    except Exception as e:
        logger.exception("Токен не задан: %s", e)
        raise

BOT_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SEND_MESSAGE_URL = BOT_API + "/sendMessage"
EDIT_MESSAGE_URL = BOT_API + "/editMessageText"
SEND_PHOTO_URL = BOT_API + "/sendPhoto"
ANSWER_CB_URL = BOT_API + "/answerCallbackQuery"
DELETE_WEBHOOK_URL = BOT_API + "/deleteWebhook"
GET_WEBHOOK_INFO_URL = BOT_API + "/getWebhookInfo"

# other envs
CHAT_ID_ADMIN = os.getenv("CHAT_ID_ADMIN") or ""
PORT = int(os.getenv("PORT") or 8000)
DB_PATH = os.getenv("DB_PATH") or "bot_data.sqlite"

# ---------------- Exchange & intervals ----------------
exchange = ccxt.mexc({"enableRateLimit": True})

INTERVALS = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "4h": "4h",
    "1d": "1d"
}

DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "NEAR/USDT"]

PRICE_POLL_INTERVAL = 10  # seconds for background polling / history saving
HISTORY_LIMIT = 500  # in-memory cache per symbol

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
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            symbol TEXT,
            target REAL,
            alert_type TEXT DEFAULT 'cross',  -- 'above','below','cross'
            is_recurring INTEGER DEFAULT 0,
            active_until TEXT DEFAULT NULL,
            time_start TEXT DEFAULT NULL,
            time_end TEXT DEFAULT NULL
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
            signals_enabled INTEGER DEFAULT 0
        )""")
        conn.commit()
        logger.info("DB initialized")
    except Exception:
        logger.exception("init_db error")

init_db()

# ---------------- In-memory runtime ----------------
pending_alerts = {}  # chat_id -> pending flow states
last_prices = {}     # symbol -> last price
history_cache = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))  # symbol -> deque of (ts, price)

# ---------------- DB wrappers ----------------
def db_commit():
    try:
        conn.commit()
    except Exception:
        logger.exception("DB commit error")

def add_user(chat_id):
    try:
        cur.execute("INSERT OR IGNORE INTO users (chat_id, created_at) VALUES (?, ?)", (str(chat_id), datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
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
            return DEFAULT_SYMBOLS.copy()
    except Exception:
        logger.exception("get_user_symbols error")
        return DEFAULT_SYMBOLS.copy()

def add_user_symbol(chat_id, symbol):
    try:
        cur.execute("INSERT INTO user_symbols (chat_id, symbol) VALUES (?, ?)", (str(chat_id), symbol))
        db_commit()
        return True
    except Exception:
        logger.exception("add_user_symbol error")
        return False

def list_user_alerts(chat_id):
    cur.execute("SELECT id, symbol, target, alert_type, is_recurring, active_until, time_start, time_end FROM alerts WHERE chat_id=? ORDER BY id DESC", (str(chat_id),))
    return cur.fetchall()

def delete_alert(alert_id, chat_id):
    cur.execute("DELETE FROM alerts WHERE id=? AND chat_id=?", (int(alert_id), str(chat_id)))
    db_commit()

def save_alert_to_db(chat_id, symbol, target, alert_type='cross', is_recurring=0, active_until=None, time_start=None, time_end=None):
    cur.execute(
        "INSERT INTO alerts (chat_id, symbol, target, alert_type, is_recurring, active_until, time_start, time_end) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (str(chat_id), symbol, float(target), alert_type, int(is_recurring), active_until, time_start, time_end)
    )
    db_commit()

def get_all_alerts():
    cur.execute("SELECT id, chat_id, symbol, target, alert_type, is_recurring, active_until, time_start, time_end FROM alerts")
    return cur.fetchall()

def log_db(level, message):
    try:
        cur.execute("INSERT INTO logs (level, message) VALUES (?, ?)", (level.upper(), message))
        db_commit()
    except Exception:
        logger.exception("log_db error")
    getattr(logger, level.lower())(message)

# ---------------- Telegram helpers ----------------
def send_message(chat_id, text, reply_markup=None, parse_mode="HTML", disable_web_page_preview=True):
    payload = {"chat_id": str(chat_id), "text": text, "disable_web_page_preview": disable_web_page_preview}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        # reply_markup must be JSON
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(SEND_MESSAGE_URL, data=payload, timeout=10)
        if not r.ok:
            logger.warning("send_message failed: %s %s", r.status_code, r.text)
        return r.json()
    except Exception:
        logger.exception("send_message exception")
        return {}

def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": str(chat_id), "message_id": int(message_id), "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(EDIT_MESSAGE_URL, data=payload, timeout=10)
        if not r.ok:
            logger.warning("edit_message failed: %s %s", r.status_code, r.text)
        return r.json()
    except Exception:
        logger.exception("edit_message exception")
        return {}

def answer_callback(callback_query_id, text=None, show_alert=False):
    payload = {"callback_query_id": callback_query_id, "show_alert": "true" if show_alert else "false"}
    if text:
        payload["text"] = text
    try:
        requests.post(ANSWER_CB_URL, data=payload, timeout=5)
    except Exception:
        pass

def send_photo_bytes(chat_id, img_bytes, caption=None):
    try:
        files = {"photo": ("chart.png", img_bytes.getvalue() if hasattr(img_bytes, "getvalue") else img_bytes)}
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
            data["parse_mode"] = "HTML"
        r = requests.post(SEND_PHOTO_URL, files=files, data=data, timeout=30)
        if not r.ok:
            logger.warning("send_photo failed: %s %s", r.status_code, r.text)
        return r.json()
    except Exception:
        logger.exception("send_photo exception")
        return {}

# ---------------- Keyboards ----------------
def build_reply_main_menu():
    # Reply keyboard (persistent)
    kb = {
        "keyboard": [
            [{"text": "📈 Price"}, {"text": "⏰ Set Alert"}],
            [{"text": "📋 Status"}, {"text": "📊 Chart"}],
            [{"text": "➕ Add Coin"}, {"text": "⚙️ Settings"}]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }
    return kb

def build_price_symbols_inline(symbols):
    rows = []
    for s in symbols:
        rows.append([{"text": s.split("/")[0], "callback_data": f"price_symbol|{s}"}])
    rows.append([{"text": "⬅️ Back", "callback_data": "back_main"}])
    return {"inline_keyboard": rows}

def build_chart_intervals_inline():
    rows = [[{"text": "1m", "callback_data": "chart_interval|1m"},
             {"text": "5m", "callback_data": "chart_interval|5m"},
             {"text": "15m", "callback_data": "chart_interval|15m"}],
            [{"text": "30m", "callback_data": "chart_interval|30m"},
             {"text": "1h", "callback_data": "chart_interval|1h"},
             {"text": "4h", "callback_data": "chart_interval|4h"}],
            [{"text": "1d", "callback_data": "chart_interval|1d"},
             {"text": "⬅️ Back", "callback_data": "back_main"}]]
    return {"inline_keyboard": rows}

def build_symbols_for_interval_inline(symbols, interval):
    rows = []
    for s in symbols:
        rows.append([{"text": s, "callback_data": f"chart_symbol|{s}|{interval}"}])
    rows.append([{"text": "⬅️ Back", "callback_data": "back_main"}])
    return {"inline_keyboard": rows}

def build_alert_type_kb():
    return {"inline_keyboard":[
        [{"text":"📈 Above","callback_data":"alert_type|above"},
         {"text":"📉 Below","callback_data":"alert_type|below"},
         {"text":"🔄 Cross","callback_data":"alert_type|cross"}]
    ]}

def build_alert_recurring_kb():
    return {"inline_keyboard":[
        [{"text":"☑️ One-time","callback_data":"alert_rec|0"},
         {"text":"🔂 Recurring","callback_data":"alert_rec|1"}],
        [{"text":"⬅️ Cancel","callback_data":"back_main"}]
    ]}

def build_my_alerts_kb(chat_id):
    rows = []
    rows.append([{"text":"⬅️ Back", "callback_data":"back_main"}])
    return {"inline_keyboard": rows}

# ---------------- Price polling & history ----------------
def fetch_and_save_prices_loop():
    while True:
        try:
            # gather symbols (default + user added)
            cur.execute("SELECT DISTINCT symbol FROM user_symbols")
            rows = cur.fetchall()
            symbols = set(DEFAULT_SYMBOLS)
            for r in rows:
                symbols.add(r[0])
            for sym in list(symbols):
                try:
                    ticker = exchange.fetch_ticker(sym)
                    price = float(ticker.get("last") or ticker.get("close") or 0.0)
                    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    last_prices[sym] = price
                    # save to DB history
                    cur.execute("INSERT INTO history (symbol, price, ts) VALUES (?, ?, ?)", (sym, price, now_ts))
                    db_commit()
                    history_cache[sym].append((now_ts, price))
                except Exception as e:
                    logger.debug("fetch price error for %s: %s", sym, str(e))
            # check alerts after prices updated
            check_alerts()
        except Exception:
            logger.exception("fetch_and_save_prices loop error")
        time.sleep(PRICE_POLL_INTERVAL)

# ---------------- Alerts checking ----------------
def is_within_time_window(time_start, time_end):
    if not time_start or not time_end:
        return True
    now = datetime.now(timezone.utc).strftime("%H:%M")
    return time_start <= now <= time_end

def check_alerts():
    try:
        alerts = get_all_alerts()
        for alert in alerts:
            alert_id, chat_id, symbol, target, alert_type, is_recurring, active_until, time_start, time_end = alert
            if active_until:
                try:
                    ru = datetime.strptime(active_until, "%Y-%m-%d %H:%M:%S")
                    if datetime.now() > ru:
                        cur.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
                        db_commit()
                        continue
                except Exception:
                    pass
            if time_start and time_end:
                if not is_within_time_window(time_start, time_end):
                    continue
            cur_price = last_prices.get(symbol)
            if cur_price is None:
                continue
            prev_price = None
            hist = list(history_cache.get(symbol, []))
            if len(hist) >= 2:
                prev_price = hist[-2][1]
            triggered = False
            if alert_type == "above":
                if cur_price > target:
                    triggered = True
            elif alert_type == "below":
                if cur_price < target:
                    triggered = True
            elif alert_type == "cross":
                if prev_price is not None:
                    if (prev_price < target and cur_price >= target) or (prev_price > target and cur_price <= target):
                        triggered = True
            if triggered:
                try:
                    send_message(chat_id, f"🔔 Alert fired: {symbol} {alert_type} {target}$ (current {cur_price}$)")
                    log_db("info", f"Alert fired for {chat_id} {symbol} {alert_type} {target}")
                except Exception:
                    logger.exception("sending alert message failed")
                if not is_recurring:
                    cur.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
                    db_commit()
    except Exception:
        logger.exception("check_alerts error")

# ---------------- Chart building ----------------
def fetch_ohlcv_df(symbol, timeframe, limit=200):
    """
    Fetch OHLCV from ccxt and return pandas DataFrame with timestamp (datetime) and open/high/low/close/volume
    """
    if not HAS_CHARTS:
        raise RuntimeError("pandas/matplotlib not available")
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        return df
    except Exception:
        logger.exception("fetch_ohlcv_df error for %s %s", symbol, timeframe)
        return pd.DataFrame()

def plot_candles(df, symbol, timeframe):
    """
    Plot candlestick chart and return BytesIO
    """
    if not HAS_CHARTS:
        raise RuntimeError("pandas/matplotlib not available")
    try:
        import matplotlib.ticker as ticker
        fig, ax = plt.subplots(figsize=(10,5))
        # width in days for rectangles depending on timeframe
        widths = {
            "1m": 0.0006, "5m": 0.003, "15m": 0.009, "30m": 0.02, "1h": 0.04, "4h": 0.16, "1d": 0.8
        }
        width = widths.get(timeframe, 0.02)
        times = mdates.date2num(df.index.to_pydatetime())
        for idx in range(len(df)):
            o = df["open"].iat[idx]
            c = df["close"].iat[idx]
            h = df["high"].iat[idx]
            l = df["low"].iat[idx]
            t = times[idx]
            color = "green" if c >= o else "red"
            # wick
            ax.plot([t, t], [l, h], color=color, linewidth=0.8)
            # body
            rect_bottom = min(o, c)
            rect_height = abs(c - o)
            ax.add_patch(plt.Rectangle((t - width/2, rect_bottom), width, rect_height, color=color, alpha=0.8))
        ax.xaxis_date()
        ax.set_title(f"{symbol} / {timeframe}")
        ax.set_ylabel("Price (USDT)")
        ax.grid(True, linewidth=0.3, linestyle="--", alpha=0.5)
        fig.autofmt_xdate()
        buf = io.BytesIO()
        plt.tight_layout()
        fig.savefig(buf, format="png", dpi=150)
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception:
        logger.exception("plot_candles error")
        return None

# ---------------- Webhook / Bot logic (Flask) ----------------
app = Flask(__name__)

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        data = request.json or {}
        # log raw update for debugging
        logger.debug("Incoming update: %s", json.dumps(data)[:2000])
        # Callback query handling
        if "callback_query" in data:
            cb = data["callback_query"]
            cb_id = cb.get("id")
            from_user = cb.get("from", {})
            user_id = from_user.get("id")
            chat = cb.get("message", {}).get("chat", {})
            chat_id = chat.get("id") or user_id
            action = cb.get("data", "")
            add_user(chat_id)
            log_db("info", f"callback {action} from {chat_id}")

            # navigation
            if action == "back_main":
                send_message(chat_id, "Main menu:", reply_markup=build_reply_main_menu())
                answer_callback(cb_id)
                return {"ok": True}

            # Price symbol button
            if action.startswith("price_symbol|"):
                symbol = action.split("|", 1)[1]
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    price = float(ticker.get("last") or ticker.get("close") or 0.0)
                    send_message(chat_id, f"💰 {symbol}: {price}$", reply_markup=build_reply_main_menu())
                except Exception:
                    send_message(chat_id, f"Ошибка при получении цены для {symbol}", reply_markup=build_reply_main_menu())
                answer_callback(cb_id)
                return {"ok": True}

            # Chart interval selection
            if action.startswith("chart_interval|"):
                interval = action.split("|", 1)[1]
                symbols = get_user_symbols(chat_id)
                send_message(chat_id, f"Choose symbol for interval {interval}:", reply_markup=build_symbols_for_interval_inline(symbols, interval))
                answer_callback(cb_id)
                return {"ok": True}

            # Chart symbol selection with interval
            if action.startswith("chart_symbol|"):
                parts = action.split("|")
                if len(parts) >= 3:
                    symbol = parts[1]
                    interval = parts[2]
                    # fetch and plot
                    try:
                        if not HAS_CHARTS:
                            send_message(chat_id, "Charts not available (missing pandas/matplotlib).", reply_markup=build_reply_main_menu())
                            answer_callback(cb_id)
                            return {"ok": True}
                        df = fetch_ohlcv_df(symbol, interval, limit=200)
                        if df.empty:
                            send_message(chat_id, f"Недостаточно данных для {symbol} {interval}", reply_markup=build_reply_main_menu())
                        else:
                            img = plot_candles(df, symbol, interval)
                            if img:
                                send_photo_bytes(chat_id, img, caption=f"{symbol} — {interval}")
                            else:
                                send_message(chat_id, "Ошибка построения графика", reply_markup=build_reply_main_menu())
                    except Exception:
                        logger.exception("chart error")
                        send_message(chat_id, "Ошибка при получении графика", reply_markup=build_reply_main_menu())
                answer_callback(cb_id)
                return {"ok": True}

            # add symbol confirm from inline (if flow uses)
            if action.startswith("add_symbol_confirm|"):
                sym = action.split("|",1)[1]
                ok = add_user_symbol(chat_id, sym)
                if ok:
                    send_message(chat_id, f"✅ {sym} added to your list.", reply_markup=build_reply_main_menu())
                else:
                    send_message(chat_id, f"❌ Can't add {sym}.", reply_markup=build_reply_main_menu())
                answer_callback(cb_id)
                return {"ok": True}

            # alert flows: types/recurring/time etc (skeleton - reuses pending_alerts)
            if action.startswith("alert_type|"):
                typ = action.split("|",1)[1]
                key = str(chat_id)
                if key in pending_alerts:
                    pending_alerts[key]["type_selected"] = typ
                    send_message(chat_id, "Choose recurring or one-time:", reply_markup=build_alert_recurring_kb())
                answer_callback(cb_id)
                return {"ok": True}

            if action.startswith("alert_rec|"):
                val = action.split("|",1)[1]
                key = str(chat_id)
                if key in pending_alerts:
                    pending_alerts[key]["recurring"] = int(val)
                    # finalize: save to DB
                    a = pending_alerts[key]
                    save_alert_to_db(chat_id, f"{a['coin']}/USDT", a['price'], a.get("type_selected","cross"), a.get("recurring",0), a.get("active_until"))
                    send_message(chat_id, f"✅ Alert added: {a['coin']}/USDT {a.get('type_selected','cross')} {a['price']}$", reply_markup=build_reply_main_menu())
                    del pending_alerts[key]
                answer_callback(cb_id)
                return {"ok": True}

            # unknown callback
            answer_callback(cb_id, "Unknown action")
            return {"ok": True}

        # Message handling
        msg = data.get("message") or data.get("edited_message")
        if not msg:
            return {"ok": True}
        chat_id = msg.get("chat",{}).get("id")
        text = (msg.get("text") or "").strip()
        add_user(chat_id)
        log_db("info", f"msg from {chat_id}: {text}")

        # handle buttons from reply keyboard and text commands
        if text.lower().startswith("/start") or text == "Menu" or text == "Главное":
            send_message(chat_id, "Welcome! Choose action:", reply_markup=build_reply_main_menu())
            return {"ok": True}

        if text == "📈 Price" or text.startswith("/price"):
            # show price menu as inline buttons
            symbols = get_user_symbols(chat_id)
            send_message(chat_id, "Select symbol:", reply_markup=build_price_symbols_inline(symbols))
            return {"ok": True}

        if text == "📋 Status" or text.startswith("/status"):
            rows = list_user_alerts(chat_id)
            if not rows:
                send_message(chat_id, "You have no alerts.", reply_markup=build_reply_main_menu())
            else:
                lines = []
                for r in rows:
                    aid, sym, targ, atype, rec, a_until, ts, te = r
                    lines.append(f"{aid}: {sym} {atype} {targ}$ {'🔂' if rec else '☑️'}")
                send_message(chat_id, "\n".join(lines), reply_markup=build_reply_main_menu())
            return {"ok": True}

        if text == "⏰ Set Alert" or text.startswith("/set"):
            # start add alert flow: choose coin
            symbols = get_user_symbols(chat_id)
            rows = []
            for s in symbols:
                rows.append([{"text": s.split("/")[0], "callback_data": f"set_alert_{s.split('/')[0]}"}])
            rows.append([{"text":"⬅️ Back", "callback_data":"back_main"}])
            kb = {"inline_keyboard": rows}
            send_message(chat_id, "Choose coin to set alert:", reply_markup=kb)
            return {"ok": True}

        if text == "➕ Add Coin":
            send_message(chat_id, "Send coin symbol (e.g. ADA or ADA/USDT):", reply_markup=None)
            # mark awaiting new coin
            pending_alerts[str(chat_id)] = {"awaiting_new_symbol": True}
            return {"ok": True}

        if text == "📊 Chart" or text.startswith("/chart"):
            send_message(chat_id, "Choose interval:", reply_markup=build_chart_intervals_inline())
            return {"ok": True}

        if text == "⚙️ Settings":
            send_message(chat_id, "Settings:\n- Signals auto on/off coming soon", reply_markup=build_reply_main_menu())
            return {"ok": True}

        # If awaiting adding a new symbol
        key = str(chat_id)
        if key in pending_alerts and pending_alerts[key].get("awaiting_new_symbol"):
            symbol = text.upper().strip()
            if "/" not in symbol:
                symbol = f"{symbol}/USDT"
            try:
                markets = exchange.load_markets()
                if symbol in markets:
                    add_user_symbol(chat_id, symbol)
                    send_message(chat_id, f"✅ Coin {symbol} added to your list.", reply_markup=build_reply_main_menu())
                else:
                    send_message(chat_id, f"❌ Pair {symbol} not found on MEXC.", reply_markup=build_reply_main_menu())
            except Exception:
                send_message(chat_id, f"Error checking {symbol}.", reply_markup=build_reply_main_menu())
            del pending_alerts[key]
            return {"ok": True}

        # fallback
        send_message(chat_id, "Choose action from menu:", reply_markup=build_reply_main_menu())
        return {"ok": True}

    except Exception:
        logger.exception("telegram_webhook exception")
        return {"ok": True}

# ---------------- Start background threads ----------------
def start_background_workers():
    try:
        t = threading.Thread(target=fetch_and_save_prices_loop, daemon=True)
        t.start()
        logger.info("Started price polling thread.")
    except Exception:
        logger.exception("start_background_workers error")

# ---------------- Run Flask (main) ----------------
if __name__ == "__main__":
    # start background workers
    start_background_workers()

    logger.info("Starting Flask webhook on port %s", PORT)

    # If running locally for dev you may want to set debug True; for Railway use gunicorn in Procfile.
    try:
        app.run(host="0.0.0.0", port=PORT)
    except Exception:
        logger.exception("Flask run error")
