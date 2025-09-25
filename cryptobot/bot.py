#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Full-featured Telegram crypto bot with:
- Webhook (Flask)
- MEXC via ccxt
- SQLite persistence (users, user_symbols, alerts, history, logs)
- Inline-button UIs for all flows (add alert, manage alerts, set price via +/- buttons with acceleration, autotime)
- Alerts: types above/below/cross, one-time vs recurring, time limits
- Auto-confirmation after user-configurable seconds (5/10/30/disabled)
- Price history saving and basic charting
"""

import os
import time
import json
import threading
import logging
import sqlite3
import math
from datetime import datetime, timedelta
from collections import defaultdict, deque

import requests
import ccxt
import pandas as pd
import matplotlib.pyplot as plt
import numpy as np
from flask import Flask, request
from dotenv import load_dotenv

# ---------------- Configuration ----------------
# Загружаем переменные окружения
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    TELEGRAM_TOKEN = input("Введите Telegram Bot Token: ").strip()
    with open(".env", "a", encoding="utf-8") as f:
        f.write(f"\nTELEGRAM_TOKEN={TELEGRAM_TOKEN}\n")
    print("✅ Токен сохранён в .env")

BOT_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SEND_MESSAGE = BOT_API + "/sendMessage"
EDIT_MESSAGE = BOT_API + "/editMessageText"
SEND_PHOTO = BOT_API + "/sendPhoto"
SEND_DOC = BOT_API + "/sendDocument"
ANSWER_CB = BOT_API + "/answerCallbackQuery"

CHAT_ID_ADMIN = int(os.getenv("CHAT_ID_ADMIN") or 0)
PORT = int(os.getenv("PORT") or 8000)
DB_PATH = os.getenv("DB_PATH") or "bot_data.sqlite"

# default symbols available initially
DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "NEAR/USDT"]

# price steps shown in UI (signed)
PRICE_STEPS = [-10000, -5000, -1000, -100, -10, -1, 1, 10, 100, 1000, 5000, 10000]

# auto-confirm choices (seconds)
AUTOTIME_OPTIONS = [5, 10, 30, 0]  # 0 = disabled

# how often to fetch prices / check alerts
PRICE_POLL_INTERVAL = 10  # seconds

# max history points to store/display
HISTORY_LIMIT = 500

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("crypto_bot")

# ---------------- Exchange ----------------
exchange = ccxt.mexc({"enableRateLimit": True})


# ---------------- DB init ----------------
try:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    cur = conn.cursor()
except Exception:
    logger.exception("Не удалось подключиться к базе данных")
    raise

# Create tables
try:
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id TEXT PRIMARY KEY,
        created_at DATETIME
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_symbols (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        symbol TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        symbol TEXT,
        target REAL,
        alert_type TEXT DEFAULT 'cross',  -- 'above','below','cross'
        is_recurring INTEGER DEFAULT 0,
        active_until TEXT DEFAULT NULL,   -- "YYYY-MM-DD HH:MM:SS" or NULL
        time_start TEXT DEFAULT NULL,     -- "HH:MM"
        time_end TEXT DEFAULT NULL        -- "HH:MM"
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT,
        price REAL,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        level TEXT,
        message TEXT,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_settings (
        chat_id TEXT PRIMARY KEY,
        signals_enabled INTEGER DEFAULT 0
    )
    """)
    conn.commit()
except Exception:
    logger.exception("Ошибка при создании таблиц БД")

# ---------------- In-memory runtime structures ----------------
pending_alerts = {}  # chat_id -> dict with 'coin','price','msg_id','last_step','last_time','multiplier','autotime','type_selected','recurring'
last_prices = {}     # symbol -> last price
history_cache = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))  # symbol -> deque of (ts, price)

# ---------------- Helper: DB wrappers ----------------
def db_commit():
    try:
        conn.commit()
    except Exception:
        logger.exception("DB commit error")

def add_user(chat_id):
    try:
        cur.execute("INSERT OR IGNORE INTO users (chat_id, created_at) VALUES (?, ?)", (str(chat_id), datetime.utcnow()))
        conn.commit()
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
        conn.commit()
        return True
    except Exception:
        logger.exception("add_user_symbol error")
        return False

def list_user_alerts(chat_id):
    try:
        cur.execute("SELECT id, symbol, target, alert_type, is_recurring, active_until, time_start, time_end FROM alerts WHERE chat_id=? ORDER BY id DESC", (str(chat_id),))
        return cur.fetchall()
    except Exception:
        logger.exception("list_user_alerts error")
        return []

def delete_alert(alert_id, chat_id):
    try:
        cur.execute("DELETE FROM alerts WHERE id=? AND chat_id=?", (int(alert_id), str(chat_id)))
        conn.commit()
    except Exception:
        logger.exception("delete_alert error")

def save_alert_to_db(chat_id, symbol, target, alert_type='cross', is_recurring=0, active_until=None, time_start=None, time_end=None):
    try:
        cur.execute(
            "INSERT INTO alerts (chat_id, symbol, target, alert_type, is_recurring, active_until, time_start, time_end) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(chat_id), symbol, float(target), alert_type, int(is_recurring), active_until, time_start, time_end)
        )
        conn.commit()
    except Exception:
        logger.exception("save_alert_to_db error")

def get_all_alerts():
    try:
        cur.execute("SELECT id, chat_id, symbol, target, alert_type, is_recurring, active_until, time_start, time_end FROM alerts")
        return cur.fetchall()
    except Exception:
        logger.exception("get_all_alerts error")
        return []

def log_db(level, message):
    try:
        cur.execute("INSERT INTO logs (level, message) VALUES (?, ?)", (level.upper(), message))
        conn.commit()
    except Exception:
        logger.exception("log_db error")
    # also console
    try:
        getattr(logger, level.lower())(message)
    except Exception:
        logger.info(message)

# ---------------- Helper: Telegram I/O ----------------
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

def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": str(chat_id), "message_id": int(message_id), "text": text}
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

def send_photo(chat_id, img_bytes, caption=None):
    try:
        # Accept either file-like or tuple (name, buf) like existing usage
        files = {}
        data = {"chat_id": str(chat_id)}
        if isinstance(img_bytes, tuple) and len(img_bytes) == 2:
            # (filename, buffer)
            filename, buf = img_bytes
            buf.seek(0)
            files = {"photo": (filename, buf)}
        else:
            files = {"photo": img_bytes}
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

# ---------------- UI: keyboards ----------------
def main_menu_kb():
    kb = {
        "inline_keyboard": [
            [{"text": "💰 Цена", "callback_data": "price_menu"}, {"text": "📊 График", "callback_data": "chart_menu"}],
            [{"text": "🔔 Добавить Alert", "callback_data": "add_alert"}, {"text": "📋 Мои Alerts", "callback_data": "my_alerts"}],
            [{"text": "📈 Сигналы", "callback_data": "signals_menu"}, {"text": "⚙️ Мои монеты", "callback_data": "my_symbols"}],
            [{"text": "📜 История", "callback_data": "history_menu"}]
        ]
    }
    return kb

def price_steps_kb(include_autotime=True):
    # returns inline keyboard matrix for steps + autotime + confirm
    row_minus = [{"text": f"{int(s)}", "callback_data": f"price_step_{int(s)}"} for s in PRICE_STEPS[:6]]
    row_plus = [{"text": f"+{int(s)}", "callback_data": f"price_step_{int(s)}"} for s in PRICE_STEPS[6:]]
    kb = {"inline_keyboard": [row_minus, row_plus]}
    last_row = []
    if include_autotime:
        last_row.append({"text": "⏱️ Время автоподтв.", "callback_data": "auto_time_menu"})
    last_row.append({"text": "✅ Готово", "callback_data": "price_confirm"})
    kb["inline_keyboard"].append(last_row)
    return kb

def autotime_kb():
    kb = {"inline_keyboard": [
        [{"text": "⏱️ 5 сек", "callback_data": "auto_time_5"},
         {"text": "⏱️ 10 сек", "callback_data": "auto_time_10"},
         {"text": "⏱️ 30 сек", "callback_data": "auto_time_30"}],
        [{"text": "♾️ Отключить", "callback_data": "auto_time_0"},
         {"text": "⬅️ Назад", "callback_data": "auto_time_back"}]
    ]}
    return kb

def signals_menu_kb():
    kb = {"inline_keyboard": [
        [{"text": "🔁 Авто-сигналы Вкл/Выкл", "callback_data": "toggle_signals"}],
        [{"text": "⬅️ Назад", "callback_data": "back_main"}]
    ]}
    return kb

# ---------------- Price polling and history ----------------
def fetch_and_save_prices():
    """
    Poll prices for union of all user symbols and defaults.
    Save to history DB and update last_prices dict and cache.
    Also check alerts.
    """
    while True:
        try:
            # build set of symbols to fetch
            try:
                cur.execute("SELECT DISTINCT symbol FROM user_symbols")
                rows = cur.fetchall()
            except Exception:
                rows = []
                logger.debug("No user symbols table or fetch failed")
            symbols = set(DEFAULT_SYMBOLS)
            if rows:
                symbols.update([r[0] for r in rows])
            # fetch tickers in loop (ccxt may not support batch on mexc reliably)
            for sym in list(symbols):
                try:
                    ticker = exchange.fetch_ticker(sym)
                    price = float(ticker.get("last") or ticker.get("close") or 0.0)
                    now_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                    last_prices[sym] = price
                    # save to DB
                    try:
                        cur.execute("INSERT INTO history (symbol, price, ts) VALUES (?, ?, ?)", (sym, price, now_ts))
                        conn.commit()
                    except Exception:
                        logger.exception("history insert failed")
                    # update in-memory cache
                    history_cache[sym].append((now_ts, price))
                except Exception as e:
                    logger.debug("fetch price error for %s: %s", sym, str(e))
            # after updating prices, check alerts
            try:
                check_alerts()
            except Exception:
                logger.exception("check_alerts error in fetch loop")
        except Exception:
            logger.exception("fetch_and_save_prices loop error")
        time.sleep(PRICE_POLL_INTERVAL)

# ---------------- Alert checking ----------------
def is_within_time_window(time_start, time_end):
    """time_start/time_end are 'HH:MM' strings or None. Use UTC."""
    if not time_start or not time_end:
        return True
    now = datetime.utcnow().strftime("%H:%M")
    return time_start <= now <= time_end

def check_alerts():
    try:
        alerts = get_all_alerts()
        for alert in alerts:
            alert_id, chat_id, symbol, target, alert_type, is_recurring, active_until, time_start, time_end = alert
            # check active_until
            if active_until:
                try:
                    ru = datetime.strptime(active_until, "%Y-%m-%d %H:%M:%S")
                    if datetime.utcnow() > ru:
                        # expired
                        cur.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
                        conn.commit()
                        continue
                except:
                    pass
            # check time window
            if time_start and time_end:
                if not is_within_time_window(time_start, time_end):
                    continue
            # get current and previous price
            cur_price = last_prices.get(symbol)
            if cur_price is None:
                continue
            # previous price from history cache
            prev_price = None
            hist = list(history_cache.get(symbol, []))
            if len(hist) >= 2:
                prev_price = hist[-2][1]
            # determine trigger
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
                    send_message(chat_id, f"🔔 Alert сработал: {symbol} {alert_type} {target}$ (текущая {cur_price}$)")
                    log_db("info", f"Alert fired for {chat_id} {symbol} {alert_type} {target}")
                except Exception:
                    logger.exception("sending alert message failed")
                # delete if one-time
                if not is_recurring:
                    try:
                        cur.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
                        conn.commit()
                    except Exception:
                        logger.exception("delete fired alert failed")
    except Exception:
        logger.exception("check_alerts error")

# ---------------- Charting ----------------
def build_chart_image(symbol, points=50, sma_list=None, ema_list=None, show_rsi=False, show_macd=False):
    try:
        cur.execute("SELECT ts, price FROM history WHERE symbol=? ORDER BY id DESC LIMIT ?", (symbol, points))
        rows = cur.fetchall()
    except Exception:
        rows = []
        logger.exception("build_chart_image DB read failed")
    if not rows:
        return None
    rows = rows[::-1]
    times = [datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S") for r in rows]
    prices = [r[1] for r in rows]
    df = pd.DataFrame({"time": times, "price": prices}).set_index("time")
    fig = None
    try:
        if show_rsi or show_macd:
            n_sub = 1 + (1 if show_rsi else 0) + (1 if show_macd else 0)
            fig, axes = plt.subplots(n_sub, 1, figsize=(8, 3*n_sub), sharex=True)
            if n_sub == 1:
                axes = [axes]
            ax_price = axes[0]
            ax_price.plot(df.index, df["price"], label="Price")
            if sma_list:
                for s in sma_list:
                    df[f"SMA{s}"] = df["price"].rolling(window=s).mean()
                    ax_price.plot(df.index, df[f"SMA{s}"], label=f"SMA{s}")
            if ema_list:
                for e in ema_list:
                    df[f"EMA{e}"] = df["price"].ewm(span=e, adjust=False).mean()
                    ax_price.plot(df.index, df[f"EMA{e}"], label=f"EMA{e}")
            ax_price.legend(); ax_price.grid(True)
            idx = 1
            if show_rsi:
                rsi = calculate_rsi(prices, 14)
                axes[idx].plot(df.index, rsi, color="purple"); axes[idx].axhline(70, color="red", linestyle="--"); axes[idx].axhline(30, color="green", linestyle="--")
                axes[idx].set_ylim(0,100); axes[idx].set_title("RSI(14)")
                idx += 1
            if show_macd:
                macd_line, signal_line, hist = calculate_macd(prices)
                axes[idx].plot(df.index, macd_line, label="MACD"); axes[idx].plot(df.index, signal_line, label="Signal")
                axes[idx].bar(df.index, hist, label="Hist", alpha=0.4)
                axes[idx].legend()
        else:
            fig, ax = plt.subplots(figsize=(8,4))
            ax.plot(df.index, df["price"], label="Price")
            if sma_list:
                for s in sma_list:
                    df[f"SMA{s}"] = df["price"].rolling(window=s).mean()
                    ax.plot(df.index, df[f"SMA{s}"], label=f"SMA{s}")
            if ema_list:
                for e in ema_list:
                    df[f"EMA{e}"] = df["price"].ewm(span=e, adjust=False).mean()
                    ax.plot(df.index, df[f"EMA{e}"], label=f"EMA{e}")
            ax.legend(); ax.grid(True)
        # save to bytes
        import io
        buf = io.BytesIO()
        plt.tight_layout()
        fig.savefig(buf, format="png", dpi=150)
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception:
        logger.exception("build_chart_image error")
        if fig:
            plt.close(fig)
        return None

# ---------------- Indicators ----------------
def calculate_rsi(prices, period=14):
    try:
        if len(prices) < period + 1:
            return [np.nan]*len(prices)
        s = pd.Series(prices)
        delta = s.diff().dropna()
        up = delta.clip(lower=0)
        down = -delta.clip(upper=0)
        ma_up = up.rolling(window=period).mean()
        ma_down = down.rolling(window=period).mean()
        rs = ma_up / ma_down
        rsi = 100 - 100/(1+rs)
        rsi = rsi.reindex(range(len(prices))).fillna(method='bfill').values
        return rsi
    except Exception:
        logger.exception("calculate_rsi error")
        return [np.nan]*len(prices)

def calculate_macd(prices, fast=12, slow=26, signal=9):
    try:
        s = pd.Series(prices)
        ema_fast = s.ewm(span=fast, adjust=False).mean()
        ema_slow = s.ewm(span=slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=signal, adjust=False).mean()
        histogram = macd_line - signal_line
        return macd_line.values, signal_line.values, histogram.values
    except Exception:
        logger.exception("calculate_macd error")
        return np.array([]), np.array([]), np.array([])

# ---------------- Auto-confirm timer per pending alert ----------------
def start_autoconfirm_worker(chat_id):
    def worker():
        while True:
            if str(chat_id) not in pending_alerts:
                return
            alert = pending_alerts[str(chat_id)]
            autotime = alert.get("autotime", 10)
            if autotime == 0:
                return
            last = alert.get("last_time", 0)
            wait = max(0, autotime - (time.time() - last))
            time.sleep(wait)
            # check again
            if str(chat_id) not in pending_alerts:
                return
            alert2 = pending_alerts[str(chat_id)]
            last2 = alert2.get("last_time", 0)
            if time.time() - last2 >= autotime:
                # perform auto-confirm
                try:
                    msg_id = alert2.get("msg_id")
                    coin = alert2.get("coin")
                    price_val = alert2.get("price")
                    # edit message to show confirmed
                    edit_message(str(chat_id), msg_id, f"✅ Автоподтверждение: {coin} при цене {price_val}$.\nВыберите тип сигнала:")
                    # send separate message with types
                    kb = {"inline_keyboard": [
                        [{"text": "📈 Выше", "callback_data": "alert_type_above"},
                         {"text": "📉 Ниже", "callback_data": "alert_type_below"},
                         {"text": "🔄 Пересечение", "callback_data": "alert_type_cross"}]
                    ]}
                    send_message(chat_id, "Какой тип сигнала поставить?", reply_markup=kb)
                    # keep pending_alerts data but mark awaiting_type
                    alert2["awaiting_type"] = True
                    # stop worker for this chat
                    return
                except Exception:
                    logger.exception("auto confirm failed")
                    return
            # else loop and continue
    t = threading.Thread(target=worker, daemon=True)
    t.start()

# ---------------- Webhook / Bot logic ----------------
app = Flask(__name__)

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        data = request.json or {}
        # Callback query handling
        if "callback_query" in data:
            cb = data["callback_query"]
            cb_id = cb.get("id")
            from_user = cb.get("from", {})
            user_id = from_user.get("id")
            chat = cb.get("message", {}).get("chat", {})
            chat_id = chat.get("id") or user_id
            action = cb.get("data", "")
            try:
                add_user(chat_id)
            except Exception:
                logger.exception("add_user failed in callback")
            log_db("info", f"callback {action} from {chat_id}")
            # handle many actions:
            # navigation
            if action == "back_main":
                send_message(chat_id, "Главное меню:", reply_markup=main_menu_kb())
                answer_callback(cb_id)
                return {"ok": True}

            if action == "price_menu":
                send_message(chat_id, "Введите /price SYMBOL или выберите монету в 'Мои монеты'.", reply_markup=None)
                send_message(chat_id, "Меню:", reply_markup=main_menu_kb())
                answer_callback(cb_id)
                return {"ok": True}

            if action == "chart_menu":
                send_message(chat_id, "Чтобы построить график, используйте команды или выберите монету в 'Мои монеты'.", reply_markup=main_menu_kb())
                answer_callback(cb_id)
                return {"ok": True}

            if action == "signals_menu":
                send_message(chat_id, "Управление авто-сигналами:", reply_markup=signals_menu_kb())
                answer_callback(cb_id)
                return {"ok": True}

            if action == "my_symbols":
                syms = get_user_symbols(chat_id)
                lines = ["Ваши монеты:"]
                for s in syms:
                    lines.append(s)
                kb = {"inline_keyboard": [[{"text": "➕ Добавить монету", "callback_data": "add_symbol"}], [{"text": "⬅️ Назад", "callback_data": "back_main"}]]}
                send_message(chat_id, "\n".join(lines), reply_markup=kb)
                answer_callback(cb_id)
                return {"ok": True}

            # add symbol flow
            if action == "add_symbol":
                send_message(chat_id, "Введите символ монеты (например: ADA или ADA/USDT).")
                # set a simple state by pending_alerts slot to reuse structure
                pending_alerts[str(chat_id)] = {"awaiting_new_symbol": True}
                answer_callback(cb_id)
                return {"ok": True}

            # add alert flow: show coins
            if action == "add_alert":
                syms = get_user_symbols(chat_id)
                # build keyboard of user's symbols (up to 6 per row)
                rows = []
                row = []
                for i, s in enumerate(syms):
                    label = s.split("/")[0]
                    row.append({"text": label, "callback_data": f"set_alert_{label}"})
                    if (i+1) % 3 == 0:
                        rows.append(row); row=[]
                if row: rows.append(row)
                rows.append([{"text":"⬅️ Назад","callback_data":"back_main"}])
                kb = {"inline_keyboard": rows}
                send_message(chat_id, "Выберите монету для Alert:", reply_markup=kb)
                answer_callback(cb_id)
                return {"ok": True}

            # step: user selected a coin to set alert
            if action.startswith("set_alert_"):
                coin = action.split("_",2)[2]
                symbol = coin + "/USDT"
                try:
                    ticker = exchange.fetch_ticker(symbol)
                    base_price = float(ticker.get("last") or ticker.get("close") or 0.0)
                except Exception:
                    base_price = 0.0
                # initialise pending alert
                pending_alerts[str(chat_id)] = {
                    "coin": coin,
                    "price": base_price,
                    "msg_id": None,
                    "last_step": None,
                    "last_time": time.time(),
                    "multiplier": 1,
                    "autotime": 10,  # default
                    "awaiting_type": False
                }
                # send initial message and store message_id
                kb = price_steps_kb(include_autotime=True)
                resp = send_message(chat_id, f"{coin}\nБазовая цена: {base_price}$\nНастройте цену кнопками:", reply_markup=kb)
                mid = resp.get("result",{}).get("message_id")
                if mid:
                    pending_alerts[str(chat_id)]["msg_id"] = mid
                # start auto-confirm worker
                start_autoconfirm_worker(chat_id)
                answer_callback(cb_id)
                return {"ok": True}

            # price step adjustment (callback)
            if action.startswith("price_step_"):
                try:
                    step = int(action.split("_",2)[2])
                except:
                    step = 0
                key = str(chat_id)
                if key in pending_alerts:
                    try:
                        alert = pending_alerts[key]
                        now = time.time()
                        # acceleration: if same step within 3 seconds, increase multiplier
                        if alert.get("last_step") == step and now - alert.get("last_time",0) < 3:
                            alert["multiplier"] = alert.get("multiplier",1) + 1
                        else:
                            alert["multiplier"] = 1
                        final_step = step * alert["multiplier"]
                        alert["price"] = max(0, alert.get("price",0) + final_step)
                        alert["last_step"] = step
                        alert["last_time"] = now
                        # update message
                        msg_id = alert.get("msg_id")
                        coin = alert.get("coin")
                        price_now = alert.get("price")
                        kb = price_steps_kb(include_autotime=True)
                        text = f"📊 {coin}\nТекущая цена: {price_now}$\n(шаг {step}$ ×{alert['multiplier']} = {final_step}$)"
                        edit_message(chat_id, msg_id, text, reply_markup=kb)
                    except Exception:
                        logger.exception("price_step handling failed")
                answer_callback(cb_id)
                return {"ok": True}

            # auto time menu
            if action == "auto_time_menu":
                key = str(chat_id)
                if key in pending_alerts:
                    alert = pending_alerts[key]
                    mid = alert.get("msg_id")
                    at = alert.get("autotime",10)
                    text = f"⏱️ Сейчас: {'выкл' if at==0 else str(at)+' сек'}\nВыберите новое:"
                    edit_message(chat_id, alert.get("msg_id"), text, reply_markup=autotime_kb())
                answer_callback(cb_id)
                return {"ok": True}

            if action.startswith("auto_time_"):
                val = int(action.split("_",2)[2])
                key = str(chat_id)
                if key in pending_alerts:
                    pending_alerts[key]["autotime"] = val
                    # return to price steps UI
                    alert = pending_alerts[key]
                    kb = price_steps_kb(include_autotime=True)
                    text = f"📊 {alert['coin']}\nТекущая цена: {alert['price']}$\n⏱ Автоподтв.: {'выкл' if val==0 else str(val)+' сек'}"
                    edit_message(chat_id, alert.get("msg_id"), text, reply_markup=kb)
                answer_callback(cb_id)
                return {"ok": True}

            if action == "auto_time_back":
                key = str(chat_id)
                if key in pending_alerts:
                    alert = pending_alerts[key]
                    kb = price_steps_kb(include_autotime=True)
                    edit_message(chat_id, alert.get("msg_id"), f"📊 {alert['coin']}\nТекущая цена: {alert['price']}$", reply_markup=kb)
                answer_callback(cb_id)
                return {"ok": True}

            # price confirm
            if action == "price_confirm":
                key = str(chat_id)
                if key in pending_alerts:
                    alert = pending_alerts[key]
                    mid = alert.get("msg_id")
                    coin = alert.get("coin")
                    price_val = alert.get("price")
                    # edit original message to summary
                    edit_message(chat_id, mid, f"✅ Вы выбрали {coin} при цене {price_val}$.\nТеперь выберите тип сигнала:")
                    # send separate message with types
                    kb = {"inline_keyboard": [
                        [{"text": "📈 Выше", "callback_data": "alert_type_above"},
                         {"text": "📉 Ниже", "callback_data": "alert_type_below"},
                         {"text": "🔄 Пересечение", "callback_data": "alert_type_cross"}]
                    ]}
                    send_message(chat_id, "Выберите тип сигнала:", reply_markup=kb)
                    alert["awaiting_type"] = True
                answer_callback(cb_id)
                return {"ok": True}

            # type selection
            if action.startswith("alert_type_"):
                typ = action.split("_",2)[2]
                key = str(chat_id)
                if key in pending_alerts and pending_alerts[key].get("awaiting_type"):
                    pending_alerts[key]["type_selected"] = typ
                    # ask recurring or one-time
                    kb = {"inline_keyboard":[
                        [{"text":"☑️ Одноразовый","callback_data":"alert_recurring_no"},
                         {"text":"🔂 Постоянный","callback_data":"alert_recurring_yes"}]
                    ]}
                    send_message(chat_id, "Одноразовый или постоянный?", reply_markup=kb)
                answer_callback(cb_id)
                return {"ok": True}

            if action.startswith("alert_recurring_"):
                rec = 1 if action.endswith("yes") else 0
                key = str(chat_id)
                if key in pending_alerts:
                    pending_alerts[key]["recurring"] = rec
                    # ask about active_until options
                    kb = {"inline_keyboard":[
                        [{"text":"⏰ Только сегодня","callback_data":"time_today"},
                         {"text":"📅 Задать часы","callback_data":"time_custom"}],
                        [{"text":"♾️ Без ограничений","callback_data":"time_none"}]
                    ]}
                    send_message(chat_id, "Ограничить сигнал по времени?", reply_markup=kb)
                answer_callback(cb_id)
                return {"ok": True}

            if action == "time_today":
                key = str(chat_id)
                if key in pending_alerts:
                    active_until = (datetime.utcnow().replace(hour=23, minute=59, second=59)).strftime("%Y-%m-%d %H:%M:%S")
                    pending_alerts[key]["active_until"] = active_until
                    # now save final alert
                    a = pending_alerts[key]
                    save_alert_to_db(chat_id, f"{a['coin']}/USDT", a['price'], a.get("type_selected","cross"), a.get("recurring",0), a.get("active_until"), None, None)
                    send_message(chat_id, f"✅ Alert добавлен: {a['coin']}/USDT {a.get('type_selected','cross')} {a['price']}$ (до конца дня)")
                    del pending_alerts[key]
                answer_callback(cb_id)
                return {"ok": True}

            if action == "time_none":
                key = str(chat_id)
                if key in pending_alerts:
                    a = pending_alerts[key]
                    save_alert_to_db(chat_id, f"{a['coin']}/USDT", a['price'], a.get("type_selected","cross"), a.get("recurring",0), None, None, None)
                    send_message(chat_id, f"✅ Alert добавлен: {a['coin']}/USDT {a.get('type_selected','cross')} {a['price']}$ (без ограничений)")
                    del pending_alerts[key]
                answer_callback(cb_id)
                return {"ok": True}

            if action == "time_custom":
                # ask user to send hours number; set awaiting_time flag
                key = str(chat_id)
                if key in pending_alerts:
                    pending_alerts[key]["awaiting_time"] = True
                    send_message(chat_id, "Введите срок действия в часах (например: 24):")
                answer_callback(cb_id)
                return {"ok": True}

            # delete alert from my_alerts
            if action.startswith("del_alert_"):
                alert_id = int(action.split("_",2)[2])
                delete_alert(alert_id, chat_id)
                send_message(chat_id, "✅ Alert удалён.")
                answer_callback(cb_id)
                return {"ok": True}

            # show my alerts
            if action == "my_alerts":
                rows = list_user_alerts(chat_id)
                if not rows:
                    send_message(chat_id, "У вас пока нет активных Alerts.", reply_markup={"inline_keyboard":[[{"text":"⬅️ Назад","callback_data":"back_main"}]]})
                else:
                    kb = {"inline_keyboard": []}
                    for r in rows:
                        aid, sym, targ, atype, rec, a_until, ts, te = r
                        label = ("🔂" if rec else "☑️") + f" {sym} {atype} {targ}$"
                        kb["inline_keyboard"].append([{"text": label, "callback_data": f"del_alert_{aid}"}])
                    kb["inline_keyboard"].append([{"text":"⬅️ Назад","callback_data":"back_main"}])
                    send_message(chat_id, "Ваши Alerts (нажмите чтобы удалить):", reply_markup=kb)
                answer_callback(cb_id)
                return {"ok": True}

            # toggle signals setting
            if action == "toggle_signals":
                try:
                    cur.execute("SELECT signals_enabled FROM user_settings WHERE chat_id=?", (str(chat_id),))
                    row = cur.fetchone()
                    cur_val = row[0] if row else 0
                    new_val = 0 if cur_val == 1 else 1
                    cur.execute("INSERT OR REPLACE INTO user_settings (chat_id, signals_enabled) VALUES (?, ?)", (str(chat_id), new_val))
                    conn.commit()
                    send_message(chat_id, f"Авто-сигналы {'включены' if new_val==1 else 'выключены'}.")
                except Exception:
                    logger.exception("toggle_signals failed")
                answer_callback(cb_id)
                return {"ok": True}

            # unknown callback
            answer_callback(cb_id, "Нажата неизвестная кнопка.")
            return {"ok": True}

        # Message handling (text)
        msg = data.get("message") or data.get("edited_message")
        if not msg:
            return {"ok": True}
        chat_id = msg.get("chat",{}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id:
            return {"ok": True}
        try:
            add_user(chat_id)
        except Exception:
            logger.exception("add_user in message handling failed")
        log_db("info", f"msg from {chat_id}: {text}")

        # If awaiting new symbol
        key = str(chat_id)
        if key in pending_alerts and pending_alerts[key].get("awaiting_new_symbol"):
            symbol = text.upper().strip()
            if "/" not in symbol:
                symbol = f"{symbol}/USDT"
            try:
                markets = exchange.load_markets()
                if symbol in markets:
                    add_user_symbol(chat_id, symbol)
                    send_message(chat_id, f"✅ Монета {symbol} добавлена в ваш список.")
                else:
                    send_message(chat_id, f"❌ Пара {symbol} не найдена на MEXC.")
            except Exception:
                logger.exception("error checking market for new symbol")
                send_message(chat_id, f"Ошибка при проверке {symbol}.")
            try:
                del pending_alerts[key]
            except Exception:
                pass
            send_message(chat_id, "Меню:", reply_markup=main_menu_kb())
            return {"ok": True}

        # If awaiting custom time hours for alert
        if key in pending_alerts and pending_alerts[key].get("awaiting_time"):
            try:
                hours = int(text.strip())
                active_until = (datetime.utcnow() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
                a = pending_alerts[key]
                a["active_until"] = active_until
                save_alert_to_db(chat_id, f"{a['coin']}/USDT", a['price'], a.get("type_selected","cross"), a.get("recurring",0), a.get("active_until"), None, None)
                send_message(chat_id, f"✅ Alert добавлен: {a['coin']}/USDT {a.get('type_selected','cross')} {a['price']}$ (до {active_until})")
                del pending_alerts[key]
            except Exception:
                logger.exception("awaiting_time processing failed")
                send_message(chat_id, "Ошибка: введите целое число часов (например: 24).")
            send_message(chat_id, "Меню:", reply_markup=main_menu_kb())
            return {"ok": True}

        # Normal commands typed manually (fallback) - we still show menu after
        if text.startswith("/start"):
            welcome = ("👋 Привет! Я крипто-бот.\n\n"
                       "Управление через кнопки. Нажми меню ниже.")
            send_message(chat_id, welcome, reply_markup=main_menu_kb())
            return {"ok": True}

        if text.startswith("/price"):
            parts = text.split()
            if len(parts) == 2:
                s = parts[1].upper()
                if "/" not in s:
                    s = s + "/USDT"
                price = last_prices.get(s)
                if price is not None:
                    send_message(chat_id, f"💰 {s}: {price}$")
                else:
                    send_message(chat_id, f"Нет данных для {s}.")
            else:
                send_message(chat_id, "Использование: /price SYMBOL")
            send_message(chat_id, "Меню:", reply_markup=main_menu_kb())
            return {"ok": True}

        if text.startswith("/chart"):
            parts = text.split()
            if len(parts) >= 2:
                s = parts[1].upper()
                if "/" not in s:
                    s = s + "/USDT"
                buf = build_chart_image(s, points=80, sma_list=[5,20], ema_list=[], show_rsi=False, show_macd=False)
                if buf:
                    send_photo(chat_id, ("chart.png", buf), caption=f"{s} — график")
                else:
                    send_message(chat_id, "Недостаточно данных для графика.")
            else:
                send_message(chat_id, "Использование: /chart SYMBOL")
            send_message(chat_id, "Меню:", reply_markup=main_menu_kb())
            return {"ok": True}

        if text.startswith("/alerts"):
            # show my alerts list (same as button)
            rows = list_user_alerts(chat_id)
            if not rows:
                send_message(chat_id, "У вас нет Alerts.")
            else:
                lines = []
                for r in rows:
                    aid, sym, targ, atype, rec, a_until, ts, te = r
                    lines.append(f"{aid}: {sym} {atype} {targ}$ {'🔂' if rec else '☑️'}")
                send_message(chat_id, "\n".join(lines))
            send_message(chat_id, "Меню:", reply_markup=main_menu_kb())
            return {"ok": True}

        # fallback - show menu
        send_message(chat_id, "Выберите действие:", reply_markup=main_menu_kb())
        return {"ok": True}

    except Exception:
        logger.exception("telegram_webhook exception")
        return {"ok": True}

# ---------------- Start background threads ----------------
if __name__ == "__main__":
    try:
        # start price polling thread
        t = threading.Thread(target=fetch_and_save_prices, daemon=True)
        t.start()
        logger.info("Started price polling thread.")
    except Exception:
        logger.exception("Failed to start price polling thread")

    try:
        # run Flask
        logger.info("Starting Flask webhook on port %s", PORT)
        app.run(host="0.0.0.0", port=PORT)
    except Exception:
        logger.exception("Flask app crashed")
