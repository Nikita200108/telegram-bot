#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Полный bot.py:
- Flask webhook на /<TELEGRAM_TOKEN>
- MEXC via ccxt (цены + fetch_ohlcv для графиков)
- SQLite: users, user_symbols (индивидуальные), alerts, history, logs
- Reply-клавиатура (главное меню) + Inline кнопки (монеты/интервалы/шаги/удаление)
- Добавление/удаление монет пользователем
- Charts: 1m,5m,15m,30m,1h,4h,1d — свечи (green/red)
- Логирование, устойчивость к ошибкам
- Токен: берётся из env TELEGRAM_TOKEN или спрашивается и записывается в .env
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
except Exception as e:
    print("Установите requests: pip install requests")
    raise

try:
    import ccxt
except Exception as e:
    print("Установите ccxt: pip install ccxt")
    raise

# charts libs optional
HAS_CHARTS = True
try:
    import pandas as pd
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import numpy as np
except Exception:
    HAS_CHARTS = False

from flask import Flask, request

# ---------------- Config & .env handling ----------------
ENV_PATH = ".env"

def load_env_file():
    env = {}
    if os.path.exists(ENV_PATH):
        try:
            with open(ENV_PATH, "r", encoding="utf-8") as f:
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
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            for k, v in current.items():
                f.write(f"{k}={v}\n")
    except Exception:
        logger = logging.getLogger("crypto_bot")
        logger.exception("Не удалось записать .env")

# merge .env into os.environ if missing
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
    # run interactively: ask user to paste token, save to .env
    try:
        TELEGRAM_TOKEN = input("Введите TELEGRAM_TOKEN (BotFather): ").strip()
        if TELEGRAM_TOKEN:
            os.environ["TELEGRAM_TOKEN"] = TELEGRAM_TOKEN
            save_env({"TELEGRAM_TOKEN": TELEGRAM_TOKEN})
            logger.info("TELEGRAM_TOKEN сохранён в .env")
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
GET_WEBHOOK_INFO = BOT_API + "/getWebhookInfo"

# ---------------- Other config ----------------
PORT = int(os.getenv("PORT", "8000"))
DB_PATH = os.getenv("DB_PATH", "bot_data.sqlite")
PRICE_POLL_INTERVAL = int(os.getenv("PRICE_POLL_INTERVAL", "10"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "500"))

# default symbols per user initially
DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "NEAR/USDT"]

# chart intervals allowed
INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

# ---------------- Exchange ----------------
exchange = ccxt.mexc({"enableRateLimit": True})

# ---------------- Database init ----------------
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
        logger.info("DB initialized")
    except Exception:
        logger.exception("init_db error")

init_db()

# ---------------- In-memory runtime structures ----------------
pending_states = {}  # chat_id -> state dict (awaiting_new_symbol, awaiting_delete, etc.)
last_prices = {}     # symbol -> last price
history_cache = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))  # symbol -> deque((ts, price))

# ---------------- DB wrappers ----------------
def db_commit():
    try:
        conn.commit()
    except Exception:
        logger.exception("DB commit failed")

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
            symbols = [r[0] for r in rows]
            # ensure defaults exist if user has none
            if not symbols:
                for s in DEFAULT_SYMBOLS:
                    add_user_symbol(chat_id, s)
                return DEFAULT_SYMBOLS.copy()
            return symbols
        else:
            # initialize with defaults
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

# ---------------- Telegram helpers ----------------
def send_message(chat_id, text, reply_markup=None, parse_mode="HTML", disable_web_page_preview=True):
    payload = {"chat_id": str(chat_id), "text": text, "disable_web_page_preview": disable_web_page_preview}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        r = requests.post(SEND_MESSAGE_URL, data=payload, timeout=10)
        if not r.ok:
            logger.warning("send_message failed %s %s", r.status_code, r.text)
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
            logger.warning("edit_message failed %s %s", r.status_code, r.text)
        return r.json()
    except Exception:
        logger.exception("edit_message exception")
        return {}

def answer_callback(callback_query_id, text=None, show_alert=False):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    if show_alert:
        payload["show_alert"] = "true"
    try:
        requests.post(ANSWER_CB_URL, data=payload, timeout=5)
    except Exception:
        pass

def send_photo_bytes(chat_id, bytes_buf, caption=None):
    try:
        files = {"photo": ("chart.png", bytes_buf.getvalue() if hasattr(bytes_buf, "getvalue") else bytes_buf)}
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

# ---------------- Keyboards (reply + inline) ----------------
def reply_main_menu():
    kb = {
        "keyboard": [
            [{"text": "📈 Price"}, {"text": "⏰ Set Alert"}],
            [{"text": "📋 Status"}, {"text": "📊 Chart"}],
            [{"text": "➕ Add Coin"}, {"text": "🗑 Delete Coin"}],
            [{"text": "⚙️ Settings"}]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }
    return kb

def inline_symbols_buttons(symbols, prefix):  # prefix used in callback_data e.g. price_, chart_
    rows = []
    # 2 per row
    row = []
    for i, s in enumerate(symbols, 1):
        row.append({"text": s, "callback_data": f"{prefix}|{s}"})
        if i % 2 == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "⬅️ Back", "callback_data": "back_main"}])
    return {"inline_keyboard": rows}

def inline_intervals_buttons(symbol):
    rows = [
        [{"text": "1m", "callback_data": f"chart|{symbol}|1m"},
         {"text": "5m", "callback_data": f"chart|{symbol}|5m"},
         {"text": "15m", "callback_data": f"chart|{symbol}|15m"}],
        [{"text": "30m", "callback_data": f"chart|{symbol}|30m"},
         {"text": "1h", "callback_data": f"chart|{symbol}|1h"},
         {"text": "4h", "callback_data": f"chart|{symbol}|4h"}],
        [{"text": "1d", "callback_data": f"chart|{symbol}|1d"},
         {"text": "⬅️ Back", "callback_data": "back_main"}]
    ]
    return {"inline_keyboard": rows}

def inline_delete_coin_buttons(symbols):
    rows = []
    for s in symbols:
        rows.append([{"text": s, "callback_data": f"delcoin|{s}"}])
    rows.append([{"text": "⬅️ Back", "callback_data": "back_main"}])
    return {"inline_keyboard": rows}

# ---------------- Price fetching and history ----------------
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
                    logger.debug("fetch price error %s: %s", sym, str(e))
            check_alerts()
        except Exception:
            logger.exception("price polling error")
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
            aid, chat_id, symbol, target, alert_type, is_recurring, active_until, time_start, time_end = alert
            if active_until:
                try:
                    ru = datetime.strptime(active_until, "%Y-%m-%d %H:%M:%S")
                    if datetime.now() > ru:
                        cur.execute("DELETE FROM alerts WHERE id=?", (aid,))
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
                    send_message(chat_id, f"🔔 ALERT: {symbol} {alert_type} {target}$ — current {cur_price}$")
                    log_db("info", f"Alert fired {chat_id} {symbol} {alert_type} {target}")
                except Exception:
                    logger.exception("alert send failed")
                if not is_recurring:
                    cur.execute("DELETE FROM alerts WHERE id=?", (aid,))
                    db_commit()
    except Exception:
        logger.exception("check_alerts error")

# ---------------- Charts ----------------
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
        return None

# ---------------- Flask webhook & handlers ----------------
app = Flask(__name__)

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    try:
        update = request.json or {}
        logger.debug("incoming update: %s", json.dumps(update)[:2000])
        # callback queries
        if "callback_query" in update:
            handle_callback(update["callback_query"])
            return {"ok": True}
        # messages
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return {"ok": True}
        handle_message(msg)
        return {"ok": True}
    except Exception:
        logger.exception("webhook exception")
        return {"ok": True}

def handle_message(msg):
    try:
        chat_id = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        if not chat_id:
            return
        add_user(chat_id)
        log_db("info", f"msg from {chat_id}: {text}")

        # if user in pending state (awaiting add/delete)
        state = pending_states.get(str(chat_id), {})

        if state.get("awaiting_new_symbol"):
            sym = text.upper().strip()
            if "/" not in sym:
                sym = f"{sym}/USDT"
            try:
                markets = exchange.load_markets()
                if sym in markets:
                    ok = add_user_symbol(chat_id, sym)
                    if ok:
                        send_message(chat_id, f"✅ {sym} added to your list.", reply_markup=reply_main_menu())
                    else:
                        send_message(chat_id, f"❌ Can't add {sym}.", reply_markup=reply_main_menu())
                else:
                    send_message(chat_id, f"❌ Pair {sym} not found on MEXC.", reply_markup=reply_main_menu())
            except Exception:
                logger.exception("error checking symbol")
                send_message(chat_id, "Ошибка при проверке символа.", reply_markup=reply_main_menu())
            pending_states.pop(str(chat_id), None)
            return

        if state.get("awaiting_delete_choice"):
            # user typed symbol to delete
            sym = text.upper().strip()
            if "/" not in sym:
                sym = f"{sym}/USDT"
            ok = delete_user_symbol(chat_id, sym)
            if ok:
                send_message(chat_id, f"✅ {sym} removed from your list.", reply_markup=reply_main_menu())
            else:
                send_message(chat_id, f"❌ {sym} not found in your list.", reply_markup=reply_main_menu())
            pending_states.pop(str(chat_id), None)
            return

        # standard commands / reply keyboard
        if text.lower().startswith("/start") or text == "Menu" or text == "Главное":
            send_message(chat_id, "Привет! Выбери действие:", reply_markup=reply_main_menu())
            return

        if text == "📈 Price" or text.startswith("/price"):
            symbols = get_user_symbols(chat_id)
            send_message(chat_id, "Выберите монету:", reply_markup=inline_symbols_buttons(symbols, "price"))
            return

        if text == "📊 Chart" or text.startswith("/chart"):
            symbols = get_user_symbols(chat_id)
            send_message(chat_id, "Выберите монету для графика:", reply_markup=inline_symbols_buttons(symbols, "chart"))
            return

        if text == "➕ Add Coin":
            pending_states[str(chat_id)] = {"awaiting_new_symbol": True}
            send_message(chat_id, "Введите символ монеты (например: ADA или ADA/USDT):")
            return

        if text == "🗑 Delete Coin":
            syms = get_user_symbols(chat_id)
            send_message(chat_id, "Выберите монету для удаления:", reply_markup=inline_delete_coin_buttons(syms))
            # or allow typed deletion
            pending_states[str(chat_id)] = {"awaiting_delete_choice": True}
            return

        if text == "📋 Status" or text.startswith("/status"):
            rows = list_user_alerts(chat_id)
            if not rows:
                send_message(chat_id, "У вас пока нет Alerts.", reply_markup=reply_main_menu())
            else:
                lines = []
                for r in rows:
                    aid, sym, targ, atype, rec, a_until, ts, te = r
                    lines.append(f"{aid}: {sym} {atype} {targ}$ {'🔂' if rec else '☑️'}")
                send_message(chat_id, "\n".join(lines), reply_markup=reply_main_menu())
            return

        # fallback
        send_message(chat_id, "Команда не распознана. Используй меню.", reply_markup=reply_main_menu())

    except Exception:
        logger.exception("handle_message error")

def handle_callback(cb):
    try:
        cb_id = cb.get("id")
        from_user = cb.get("from", {})
        user_id = from_user.get("id")
        chat = cb.get("message", {}).get("chat", {})
        chat_id = chat.get("id") or user_id
        data = cb.get("data", "")
        add_user(chat_id)
        log_db("info", f"callback {data} from {chat_id}")

        # navigation
        if data == "back_main":
            send_message(chat_id, "Меню:", reply_markup=reply_main_menu())
            answer_callback(cb_id)
            return

        # price flow: callback price|SYMBOL
        if data.startswith("price|"):
            _, sym = data.split("|", 1)
            try:
                ticker = exchange.fetch_ticker(sym)
                price = float(ticker.get("last") or ticker.get("close") or 0.0)
                send_message(chat_id, f"💰 {sym}: {price}$", reply_markup=reply_main_menu())
            except Exception:
                logger.exception("price fetch error")
                send_message(chat_id, f"Ошибка получения цены для {sym}", reply_markup=reply_main_menu())
            answer_callback(cb_id)
            return

        # chart flow: first callback "chart|SYMBOL" -> show intervals, then "chart_send|SYMBOL|INTERVAL"
        if data.startswith("chart|"):
            _, sym = data.split("|", 1)
            # show intervals inline
            send_message(chat_id, f"Выберите интервал для {sym}:", reply_markup=inline_intervals_buttons(sym))
            answer_callback(cb_id)
            return

        if data.startswith("chart|") is False and data.count("|") == 2 and data.split("|")[0] == "chart_send":
            # fallback not used, but keep for compatibility
            pass

        # handle chart (callback exactly like chart|SYMBOL|INTERVAL when user presses interval buttons)
        if data.startswith("chart|") and data.count("|") == 2:
            _, sym, interval = data.split("|")
            # validate interval
            if interval not in INTERVALS:
                send_message(chat_id, "Неподдерживаемый интервал.", reply_markup=reply_main_menu())
                answer_callback(cb_id)
                return
            # build and send chart
            try:
                if not HAS_CHARTS:
                    send_message(chat_id, "Charts unavailable (missing pandas/matplotlib).", reply_markup=reply_main_menu())
                    answer_callback(cb_id)
                    return
                df = fetch_ohlcv_df(sym, interval, limit=200)
                if df.empty:
                    send_message(chat_id, f"Недостаточно данных для {sym} {interval}", reply_markup=reply_main_menu())
                else:
                    img = plot_candles_image(df, sym, interval)
                    if img:
                        send_photo_bytes(chat_id, img, caption=f"{sym} — {interval}")
                    else:
                        send_message(chat_id, "Ошибка построения графика.", reply_markup=reply_main_menu())
            except Exception:
                logger.exception("chart handling error")
                send_message(chat_id, "Ошибка при построении графика.", reply_markup=reply_main_menu())
            answer_callback(cb_id)
            return

        # add coin confirm (for inline flows) - not used but keep for future
        if data.startswith("addcoin|"):
            _, sym = data.split("|", 1)
            ok = add_user_symbol(chat_id, sym)
            if ok:
                send_message(chat_id, f"✅ {sym} added.", reply_markup=reply_main_menu())
            else:
                send_message(chat_id, f"❌ Can't add {sym}.", reply_markup=reply_main_menu())
            answer_callback(cb_id)
            return

        # delete coin via inline button
        if data.startswith("delcoin|"):
            _, sym = data.split("|", 1)
            ok = delete_user_symbol(chat_id, sym)
            if ok:
                send_message(chat_id, f"✅ {sym} removed from your list.", reply_markup=reply_main_menu())
            else:
                send_message(chat_id, f"❌ {sym} not in your list.", reply_markup=reply_main_menu())
            answer_callback(cb_id)
            return

        # unknown callback
        answer_callback(cb_id, "Неизвестная кнопка.")
    except Exception:
        logger.exception("handle_callback error")

# ---------------- Start background workers & run ----------------
def start_workers():
    try:
        t = threading.Thread(target=fetch_and_save_prices_loop, daemon=True)
        t.start()
        logger.info("Started price polling thread.")
    except Exception:
        logger.exception("start_workers error")

if __name__ == "__main__":
    start_workers()
    logger.info("Starting Flask on port %s", PORT)
    try:
        # When running on Railway use gunicorn; for local dev flask.run is ok
        app.run(host="0.0.0.0", port=PORT)
    except Exception:
        logger.exception("Flask run error")
