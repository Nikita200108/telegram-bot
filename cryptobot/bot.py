#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Полный bot.py — объединённый и исправленный:
- Webhook (Flask) на /<TELEGRAM_TOKEN>
- MEXC via ccxt
- SQLite (users, user_symbols, alerts, history, logs, user_settings)
- Inline кнопки и обработка callback_query
- Графики (если установлены pandas/matplotlib)
- Token из env или запрос при запуске (сохранение в .env)
- Надёжная отправка reply_markup через json, логирование запросов и ответов
- Ошибки пишутся в лог и в БД, не валят процесс
"""

import os
import sys
import time
import json
import io
import threading
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque

# external libs
try:
    import requests
except Exception:
    print("Please install requests: pip install requests")
    raise

try:
    import ccxt
except Exception:
    print("Please install ccxt: pip install ccxt")
    raise

# optional charting libs
HAS_CHARTS = True
try:
    import pandas as pd
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import numpy as np
except Exception:
    HAS_CHARTS = False

from flask import Flask, request

# ---------------- env file helpers (no python-dotenv dependency) ----------------
ENV_FILE = ".env"

def load_env_file():
    env = {}
    if os.path.exists(ENV_FILE):
        try:
            with open(ENV_FILE, "r", encoding="utf-8") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln or ln.startswith("#") or "=" not in ln:
                        continue
                    k, v = ln.split("=", 1)
                    env[k.strip()] = v.strip()
        except Exception:
            pass
    return env

def save_env(updates: dict):
    current = load_env_file()
    current.update(updates)
    try:
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            for k, v in current.items():
                f.write(f"{k}={v}\n")
    except Exception:
        logger = logging.getLogger("crypto_bot")
        logger.exception("Failed to write .env")

# merge .env into os.environ
_envfile = load_env_file()
for k, v in _envfile.items():
    if k not in os.environ:
        os.environ[k] = v

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("crypto_bot")

# ---------------- TELEGRAM TOKEN ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    try:
        TELEGRAM_TOKEN = input("Введите TELEGRAM_TOKEN (BotFather): ").strip()
        if TELEGRAM_TOKEN:
            os.environ["TELEGRAM_TOKEN"] = TELEGRAM_TOKEN
            save_env({"TELEGRAM_TOKEN": TELEGRAM_TOKEN})
            logger.info("TELEGRAM_TOKEN сохранён в .env")
        else:
            raise RuntimeError("TELEGRAM_TOKEN не введён")
    except Exception as e:
        logger.exception("Token not set: %s", e)
        raise

BOT_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SEND_MESSAGE_URL = BOT_API + "/sendMessage"
EDIT_MESSAGE_URL = BOT_API + "/editMessageText"
SEND_PHOTO_URL = BOT_API + "/sendPhoto"
ANSWER_CB_URL = BOT_API + "/answerCallbackQuery"
GET_WEBHOOK_INFO_URL = BOT_API + "/getWebhookInfo"

# ---------------- Config ----------------
PORT = int(os.getenv("PORT", "8000"))
DB_PATH = os.getenv("DB_PATH", "bot_data.sqlite")
PRICE_POLL_INTERVAL = int(os.getenv("PRICE_POLL_INTERVAL", "10"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "500"))

DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "NEAR/USDT"]
INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

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
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            symbol TEXT,
            target REAL,
            alert_type TEXT DEFAULT 'cross',
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
        logger.info("Database initialized at %s", DB_PATH)
    except Exception:
        logger.exception("init_db error")

init_db()

# ---------------- In-memory runtime ----------------
pending_alerts = {}
last_prices = {}
history_cache = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))

# ---------------- DB helpers ----------------
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
            # init defaults
            for s in DEFAULT_SYMBOLS:
                add_user_symbol(chat_id, s)
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

def delete_user_symbol(chat_id, symbol):
    try:
        cur.execute("DELETE FROM user_symbols WHERE chat_id=? AND symbol=?", (str(chat_id), symbol))
        db_commit()
        return True
    except Exception:
        logger.exception("delete_user_symbol error")
        return False

def list_user_alerts(chat_id):
    try:
        cur.execute("SELECT id, symbol, target, alert_type, is_recurring, active_until, time_start, time_end FROM alerts WHERE chat_id=? ORDER BY id DESC", (str(chat_id),))
        return cur.fetchall()
    except Exception:
        logger.exception("list_user_alerts error")
        return []

def save_alert_to_db(chat_id, symbol, target, alert_type='cross', is_recurring=0, active_until=None, time_start=None, time_end=None):
    try:
        cur.execute(
            "INSERT INTO alerts (chat_id, symbol, target, alert_type, is_recurring, active_until, time_start, time_end) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(chat_id), symbol, float(target), alert_type, int(is_recurring), active_until, time_start, time_end)
        )
        db_commit()
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
        db_commit()
    except Exception:
        logger.exception("log_db insert error")
    getattr(logger, level.lower())(message)

# ---------------- Telegram low-level helpers ----------------
def send_message(chat_id, text, reply_markup=None, parse_mode="HTML", disable_web_page_preview=True):
    try:
        payload = {
            "chat_id": str(chat_id),
            "text": text,
            "disable_web_page_preview": disable_web_page_preview
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup is not None:
            # ensure reply_markup is a dict (not pre-serialized)
            if isinstance(reply_markup, str):
                try:
                    reply_markup = json.loads(reply_markup)
                except Exception:
                    logger.warning("reply_markup passed as string and could not be parsed: %s", reply_markup)
            payload["reply_markup"] = reply_markup
        r = requests.post(SEND_MESSAGE_URL, json=payload, timeout=10)
        try:
            resp = r.json()
        except Exception:
            resp = r.text
        logger.info("sendMessage -> status=%s ok=%s text_preview=%s", r.status_code, getattr(resp, "get", lambda x: None)("ok") if isinstance(resp, dict) else None, str(resp)[:200])
        if not r.ok:
            logger.warning("send_message non-ok: %s", r.text)
        return resp
    except Exception:
        logger.exception("send_message exception")
        return {}

def edit_message(chat_id, message_id, text, reply_markup=None):
    try:
        payload = {"chat_id": str(chat_id), "message_id": int(message_id), "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        r = requests.post(EDIT_MESSAGE_URL, json=payload, timeout=10)
        try:
            resp = r.json()
        except Exception:
            resp = r.text
        logger.info("editMessage -> status=%s resp=%s", r.status_code, str(resp)[:200])
        return resp
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
        r = requests.post(ANSWER_CB_URL, json=payload, timeout=5)
        logger.info("answerCallbackQuery -> status=%s text_preview=%s", r.status_code, (r.text[:200] if r.text else ""))
    except Exception:
        logger.exception("answer_callback exception")

def send_photo_bytes(chat_id, bytes_buf, caption=None):
    try:
        files = {"photo": ("chart.png", bytes_buf.getvalue() if hasattr(bytes_buf, "getvalue") else bytes_buf)}
        data = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
            data["parse_mode"] = "HTML"
        r = requests.post(SEND_PHOTO_URL, files=files, data=data, timeout=30)
        try:
            resp = r.json()
        except Exception:
            resp = r.text
        logger.info("sendPhoto -> status=%s resp=%s", r.status_code, str(resp)[:200])
        return resp
    except Exception:
        logger.exception("send_photo exception")
        return {}

# ---------------- UI keyboards ----------------
def main_menu_kb():
    kb = {
        "inline_keyboard": [
            [{"text": "💰 Цена", "callback_data": "menu_price"}],
            [{"text": "📈 График", "callback_data": "menu_chart"}],
            [{"text": "🔔 Добавить Alert", "callback_data": "menu_add_alert"}],
            [{"text": "📋 Мои Alerts", "callback_data": "menu_my_alerts"}],
            [{"text": "⚙️ Мои монеты", "callback_data": "menu_my_symbols"}]
        ]
    }
    return kb

def symbols_inline_kb(symbols, prefix):
    rows = []
    row = []
    for i, s in enumerate(symbols, 1):
        row.append({"text": s, "callback_data": f"{prefix}|{s}"})
        if i % 2 == 0:
            rows.append(row); row=[]
    if row:
        rows.append(row)
    rows.append([{"text":"⬅️ Назад","callback_data":"back_main"}])
    return {"inline_keyboard": rows}

def timeframe_kb(symbol):
    return {
        "inline_keyboard": [
            [{"text":"1m","callback_data":f"chart|{symbol}|1m"}, {"text":"5m","callback_data":f"chart|{symbol}|5m"}, {"text":"15m","callback_data":f"chart|{symbol}|15m"}],
            [{"text":"30m","callback_data":f"chart|{symbol}|30m"}, {"text":"1h","callback_data":f"chart|{symbol}|1h"}, {"text":"4h","callback_data":f"chart|{symbol}|4h"}],
            [{"text":"1d","callback_data":f"chart|{symbol}|1d"}, {"text":"⬅️ Назад","callback_data":"back_main"}]
        ]
    }

def my_symbols_kb(chat_id):
    syms = get_user_symbols(chat_id)
    return symbols_inline_kb(syms, "price")

# ---------------- Price polling & history ----------------
def fetch_and_save_prices_loop():
    while True:
        try:
            cur.execute("SELECT DISTINCT symbol FROM user_symbols")
            rows = cur.fetchall()
            symbols = set(DEFAULT_SYMBOLS)
            for r in rows:
                symbols.add(r[0])
            for sym in list(symbols):
                try:
                    ticker = exchange.fetch_ticker(sym)
                    price = float(ticker.get("last") or ticker.get("close") or 0.0)
                    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                    last_prices[sym] = price
                    cur.execute("INSERT INTO history (symbol, price, ts) VALUES (?, ?, ?)", (sym, price, ts))
                    db_commit()
                    history_cache[sym].append((ts, price))
                except Exception as e:
                    logger.debug("fetch price error for %s: %s", sym, str(e))
            check_alerts()
        except Exception:
            logger.exception("price polling loop error")
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
                except:
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
                    send_message(chat_id, f"🔔 Alert: {symbol} {alert_type} {target}$ — current {cur_price}$")
                    log_db("info", f"Alert fired for {chat_id} {symbol} {alert_type} {target}")
                except:
                    logger.exception("sending alert message failed")
                if not is_recurring:
                    cur.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
                    db_commit()
    except Exception:
        logger.exception("check_alerts error")

# ---------------- Charting ----------------
def fetch_ohlcv_df(symbol, timeframe, limit=200):
    if not HAS_CHARTS:
        raise RuntimeError("Charts disabled (pandas/matplotlib missing)")
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        return df
    except Exception:
        logger.exception("fetch_ohlcv_df error %s %s", symbol, timeframe)
        return pd.DataFrame()

def plot_candles_image(df, symbol, timeframe):
    if not HAS_CHARTS:
        return None
    try:
        fig, ax = plt.subplots(figsize=(10,5))
        widths = {"1m": 0.0006, "5m": 0.003, "15m": 0.009, "30m": 0.02, "1h": 0.04, "4h": 0.16, "1d": 0.8}
        width = widths.get(timeframe, 0.02)
        times = mdates.date2num(df.index.to_pydatetime())
        for i in range(len(df)):
            o = df["open"].iat[i]
            c = df["close"].iat[i]
            h = df["high"].iat[i]
            l = df["low"].iat[i]
            t = times[i]
            color = "green" if c >= o else "red"
            ax.plot([t, t], [l, h], color=color, linewidth=0.8)
            rect_bottom = min(o, c)
            rect_height = max(abs(c - o), 1e-8)
            ax.add_patch(plt.Rectangle((t - width/2, rect_bottom), width, rect_height, color=color, alpha=0.9))
        ax.xaxis_date()
        ax.set_title(f"{symbol} — {timeframe}")
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
        logger.exception("plot_candles_image error")
        if fig:
            plt.close(fig)
        return None

# ---------------- States for interactive flows ----------------
pending_states = {}  # chat_id -> dict

# ---------------- Message & Callback handlers ----------------
app = Flask(__name__)

def handle_text_message(msg):
    try:
        chat_id = msg.get("chat",{}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id:
            return
        add_user(chat_id)
        log_db("info", f"msg from {chat_id}: {text}")

        state = pending_states.get(str(chat_id), {})

        # awaiting add new symbol
        if state.get("awaiting_new_symbol"):
            sym = text.upper().strip()
            if "/" not in sym:
                sym = f"{sym}/USDT"
            try:
                markets = exchange.load_markets()
                if sym in markets:
                    ok = add_user_symbol(chat_id, sym)
                    if ok:
                        send_message(chat_id, f"✅ Монета {sym} добавлена.", reply_markup=main_menu_kb())
                    else:
                        send_message(chat_id, f"❌ Не удалось добавить {sym}.", reply_markup=main_menu_kb())
                else:
                    send_message(chat_id, f"❌ Пара {sym} не найдена на MEXC.", reply_markup=main_menu_kb())
            except Exception:
                logger.exception("error checking symbol")
                send_message(chat_id, "Ошибка при проверке символа.", reply_markup=main_menu_kb())
            pending_states.pop(str(chat_id), None)
            return

        # awaiting delete
        if state.get("awaiting_delete_symbol"):
            sym = text.upper().strip()
            if "/" not in sym:
                sym = f"{sym}/USDT"
            ok = delete_user_symbol(chat_id, sym)
            if ok:
                send_message(chat_id, f"✅ Монета {sym} удалена.", reply_markup=main_menu_kb())
            else:
                send_message(chat_id, f"❌ {sym} не найдена в вашем списке.", reply_markup=main_menu_kb())
            pending_states.pop(str(chat_id), None)
            return

        # commands
        if text.startswith("/start"):
            send_message(chat_id, "Привет! Выберите действие:", reply_markup=main_menu_kb())
            return

        if text.startswith("/addsymbol"):
            parts = text.split()
            if len(parts) == 2:
                sym = parts[1].upper()
                if "/" not in sym:
                    sym = f"{sym}/USDT"
                ok = add_user_symbol(chat_id, sym)
                if ok:
                    send_message(chat_id, f"✅ {sym} добавлена в ваш список.", reply_markup=main_menu_kb())
                else:
                    send_message(chat_id, f"❌ Не удалось добавить {sym}.", reply_markup=main_menu_kb())
            else:
                pending_states[str(chat_id)] = {"awaiting_new_symbol": True}
                send_message(chat_id, "Введите символ для добавления (например: ADA или ADA/USDT):")
            return

        if text.startswith("/delsymbol"):
            parts = text.split()
            if len(parts) == 2:
                sym = parts[1].upper()
                if "/" not in sym:
                    sym = f"{sym}/USDT"
                ok = delete_user_symbol(chat_id, sym)
                if ok:
                    send_message(chat_id, f"✅ {sym} удалена.", reply_markup=main_menu_kb())
                else:
                    send_message(chat_id, f"❌ {sym} не найдена.", reply_markup=main_menu_kb())
            else:
                pending_states[str(chat_id)] = {"awaiting_delete_symbol": True}
                send_message(chat_id, "Введите символ для удаления (например: ADA или ADA/USDT):")
            return

        if text.startswith("/price"):
            parts = text.split()
            if len(parts) == 2:
                s = parts[1].upper()
                if "/" not in s:
                    s = s + "/USDT"
                price = last_prices.get(s)
                if price is not None:
                    send_message(chat_id, f"💰 {s}: {price}$", reply_markup=main_menu_kb())
                else:
                    # try fetch immediately
                    try:
                        ticker = exchange.fetch_ticker(s)
                        p = float(ticker.get("last") or ticker.get("close") or 0.0)
                        send_message(chat_id, f"💰 {s}: {p}$", reply_markup=main_menu_kb())
                    except Exception:
                        send_message(chat_id, f"Нет данных для {s}.", reply_markup=main_menu_kb())
            else:
                send_message(chat_id, "Использование: /price SYMBOL (например: /price BTC)", reply_markup=main_menu_kb())
            return

        # fallback show menu
        send_message(chat_id, "Выберите действие:", reply_markup=main_menu_kb())
    except Exception:
        logger.exception("handle_text_message error")

def handle_callback_query(cq):
    try:
        cb_id = cq.get("id")
        from_user = cq.get("from", {})
        user_id = from_user.get("id")
        message = cq.get("message", {}) or {}
        chat_id = message.get("chat", {}).get("id") or user_id
        data = cq.get("data", "")
        add_user(chat_id)
        log_db("info", f"callback {data} from {chat_id}")

        # answer callback immediately to remove spinner
        answer_callback(cb_id)

        # navigation
        if data == "back_main":
            send_message(chat_id, "Главное меню:", reply_markup=main_menu_kb())
            return

        if data == "menu_price":
            syms = get_user_symbols(chat_id)
            send_message(chat_id, "Выберите монету:", reply_markup=symbols_inline_kb(syms, "price"))
            return

        if data.startswith("price|"):
            _, sym = data.split("|", 1)
            try:
                ticker = exchange.fetch_ticker(sym)
                price = float(ticker.get("last") or ticker.get("close") or 0.0)
                send_message(chat_id, f"💰 {sym}: {price}$", reply_markup=main_menu_kb())
            except Exception:
                logger.exception("price fetch error")
                send_message(chat_id, f"Ошибка получения цены для {sym}", reply_markup=main_menu_kb())
            return

        if data == "menu_chart":
            syms = get_user_symbols(chat_id)
            send_message(chat_id, "Выберите монету для графика:", reply_markup=symbols_inline_kb(syms, "chart"))
            return

        if data.startswith("chart|") and data.count("|") == 1:
            _, sym = data.split("|",1)
            send_message(chat_id, f"Выберите таймфрейм для {sym}:", reply_markup=timeframe_kb(sym))
            return

        if data.startswith("chart|") and data.count("|") == 2:
            _, sym, tf = data.split("|")
            if tf not in INTERVALS:
                send_message(chat_id, "Неподдерживаемый таймфрейм.", reply_markup=main_menu_kb())
                return
            # build and send chart
            try:
                if not HAS_CHARTS:
                    send_message(chat_id, "Charts unavailable (install pandas & matplotlib).", reply_markup=main_menu_kb())
                    return
                df = fetch_ohlcv_df(sym, tf, limit=200)
                if df.empty or len(df) < 3:
                    send_message(chat_id, "Недостаточно данных для графика.", reply_markup=main_menu_kb())
                    return
                img = plot_candles_image(df, sym, tf)
                if img:
                    send_photo_bytes(chat_id, img, caption=f"{sym} — {tf}")
                else:
                    send_message(chat_id, "Ошибка построения графика.", reply_markup=main_menu_kb())
            except Exception:
                logger.exception("chart handling error")
                send_message(chat_id, "Ошибка при построении графика.", reply_markup=main_menu_kb())
            return

        if data == "menu_add_alert":
            # start a simple interactive flow: choose coin then target
            syms = get_user_symbols(chat_id)
            send_message(chat_id, "Выберите монету для Alert:", reply_markup=symbols_inline_kb(syms, "addalert"))
            return

        if data.startswith("addalert|"):
            _, sym = data.split("|",1)
            # store partial state: awaiting target input
            pending_states[str(chat_id)] = {"awaiting_alert_target": True, "alert_symbol": sym}
            send_message(chat_id, f"Введите цену для Alert на {sym} (число), или отмена /cancel")
            return

        if data == "menu_my_alerts":
            rows = list_user_alerts(chat_id)
            if not rows:
                send_message(chat_id, "У вас пока нет Alerts.", reply_markup=main_menu_kb())
                return
            kb = {"inline_keyboard": []}
            for r in rows:
                aid, sym, targ, atype, rec, a_until, ts, te = r
                label = ("🔂" if rec else "☑️") + f" {sym} {atype} {targ}$"
                kb["inline_keyboard"].append([{"text": label, "callback_data": f"del_alert|{aid}"}])
            kb["inline_keyboard"].append([{"text":"⬅️ Назад","callback_data":"back_main"}])
            send_message(chat_id, "Ваши Alerts (нажмите чтобы удалить):", reply_markup=kb)
            return

        if data.startswith("del_alert|"):
            _, aid = data.split("|",1)
            try:
                cur.execute("DELETE FROM alerts WHERE id=? AND chat_id=?", (int(aid), str(chat_id)))
                db_commit()
                send_message(chat_id, "✅ Alert удалён.", reply_markup=main_menu_kb())
            except Exception:
                logger.exception("del_alert error")
                send_message(chat_id, "Ошибка при удалении Alert.", reply_markup=main_menu_kb())
            return

        if data == "menu_my_symbols":
            syms = get_user_symbols(chat_id)
            kb = symbols_inline_kb(syms, "sym_op")
            # add extra row for add/del text commands
            kb["inline_keyboard"].append([{"text":"➕ Добавить (текстом)", "callback_data":"add_symbol_text"}, {"text":"🗑 Удалить (текстом)", "callback_data":"del_symbol_text"}])
            kb["inline_keyboard"].append([{"text":"⬅️ Назад", "callback_data":"back_main"}])
            send_message(chat_id, "Ваши монеты:", reply_markup=kb)
            return

        if data.startswith("sym_op|"):
            # noop for now — selecting symbol could do more later
            _, sym = data.split("|",1)
            send_message(chat_id, f"Монета: {sym}", reply_markup=main_menu_kb())
            return

        if data == "add_symbol_text":
            pending_states[str(chat_id)] = {"awaiting_new_symbol": True}
            send_message(chat_id, "Введите символ для добавления (например ADA или ADA/USDT):")
            return

        if data == "del_symbol_text":
            pending_states[str(chat_id)] = {"awaiting_delete_symbol": True}
            send_message(chat_id, "Введите символ для удаления (например ADA или ADA/USDT):")
            return

        # fallback unknown callback
        send_message(chat_id, "Нажата неизвестная кнопка.", reply_markup=main_menu_kb())

    except Exception:
        logger.exception("handle_callback_query error")

# ---------------- Webhook route ----------------
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    try:
        update = request.get_json(force=True)
        # log entire update to help debugging (Railway Logs)
        try:
            logger.info("Incoming update: %s", json.dumps(update, ensure_ascii=False))
        except Exception:
            logger.info("Incoming update (non-jsonable): %s", str(update))

        # callback_query first
        if "callback_query" in update:
            handle_callback_query(update["callback_query"])
            return {"ok": True}

        # message handling
        msg = update.get("message") or update.get("edited_message")
        if msg:
            # if pending state awaiting alert target:
            chat_id = msg.get("chat",{}).get("id")
            text = (msg.get("text") or "").strip()
            # if user had started add-alert flow by clicking addalert|SYMBOL, then enters target
            state = pending_states.get(str(chat_id), {})
            if state.get("awaiting_alert_target"):
                try:
                    val = float(text)
                    sym = state.get("alert_symbol")
                    save_alert_to_db(chat_id, sym, val, alert_type="cross", is_recurring=0)
                    send_message(chat_id, f"✅ Alert сохранён: {sym} at {val}$", reply_markup=main_menu_kb())
                except Exception:
                    send_message(chat_id, "Ошибка: введите корректную цену (например: 30000).", reply_markup=main_menu_kb())
                pending_states.pop(str(chat_id), None)
                return {"ok": True}

            # normal message handling
            handle_text_message(msg)
            return {"ok": True}

        return {"ok": True}
    except Exception:
        logger.exception("telegram_webhook exception")
        return {"ok": True}

# ---------------- Start workers & run ----------------
def start_background_workers():
    try:
        t = threading.Thread(target=fetch_and_save_prices_loop, daemon=True)
        t.start()
        logger.info("Started price polling thread.")
    except Exception:
        logger.exception("start_background_workers error")

if __name__ == "__main__":
    start_background_workers()
    logger.info("Starting Flask on port %s", PORT)
    try:
        app.run(host="0.0.0.0", port=PORT)
    except Exception:
        logger.exception("Flask run error")
