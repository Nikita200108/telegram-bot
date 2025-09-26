#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Crypto Bot — полный файл
- Webhook Flask на /<TELEGRAM_TOKEN>
- ccxt (MEXC) для цен и OHLCV
- SQLite (users, user_symbols, alerts, history, logs)
- Inline кнопки (меню, выбор монеты, таймфрейма, тип графика, источник данных)
- Свечные и линейные графики (matplotlib) — из биржи или из БД
- Фоновый опрос цен и сохранение в history
- Логирование и устойчивость (ошибки не валят процесс)
"""

import os
import sys
import time
import json
import io
import logging
import threading
import sqlite3
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque

# external libs
try:
    import requests
except Exception as e:
    print("Install requests: pip install requests")
    raise

try:
    import ccxt
except Exception:
    print("Install ccxt: pip install ccxt")
    raise

# charts optional
HAS_CHARTS = True
try:
    import pandas as pd
    import numpy as np
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
except Exception:
    HAS_CHARTS = False

from flask import Flask, request

# ---------------- Config & Logging ----------------
# Read TELEGRAM_TOKEN only from env (no input())
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
PRICE_POLL_INTERVAL = int(os.getenv("PRICE_POLL_INTERVAL", "10"))
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "200"))

DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "NEAR/USDT"]
INTERVALS = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

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
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            symbol TEXT,
            target REAL,
            alert_type TEXT DEFAULT 'cross',
            is_recurring INTEGER DEFAULT 0,
            active_until TEXT DEFAULT NULL
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
    except Exception:
        logger.exception("init_db error")
init_db()

# ---------------- In-memory runtime ----------------
last_prices = {}
history_cache = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
pending_states = {}  # chat_id -> dict for interactive flows

# ---------------- DB helpers ----------------
def db_commit():
    try:
        conn.commit()
    except Exception:
        logger.exception("db_commit error")

def log_db(level, message):
    try:
        cur.execute("INSERT INTO logs (level, message) VALUES (?, ?)", (level.upper(), str(message)))
        db_commit()
    except Exception:
        logger.exception("log_db insert error")
    getattr(logger, level.lower())(message)

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
            # if empty, return default list but also store defaults for the user
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

def save_alert(chat_id, symbol, target, alert_type='cross', is_recurring=0, active_until=None):
    try:
        cur.execute("INSERT INTO alerts (chat_id, symbol, target, alert_type, is_recurring, active_until) VALUES (?, ?, ?, ?, ?, ?)",
                    (str(chat_id), symbol, float(target), alert_type, int(is_recurring), active_until))
        db_commit()
        return True
    except Exception:
        logger.exception("save_alert error")
        return False

def list_alerts(chat_id):
    try:
        cur.execute("SELECT id, symbol, target, alert_type, is_recurring, active_until FROM alerts WHERE chat_id=? ORDER BY id DESC", (str(chat_id),))
        return cur.fetchall()
    except Exception:
        logger.exception("list_alerts error")
        return []

def get_all_alerts():
    try:
        cur.execute("SELECT id, chat_id, symbol, target, alert_type, is_recurring, active_until FROM alerts")
        return cur.fetchall()
    except Exception:
        logger.exception("get_all_alerts error")
        return []

# ---------------- Telegram helpers ----------------
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
            # ensure reply_markup is dict
            if isinstance(reply_markup, str):
                try:
                    reply_markup = json.loads(reply_markup)
                except Exception:
                    logger.warning("reply_markup is string and not json: %s", reply_markup)
            payload["reply_markup"] = reply_markup
        r = requests.post(SEND_MESSAGE_URL, json=payload, timeout=10)
        try:
            resp = r.json()
        except Exception:
            resp = r.text
        logger.info("sendMessage -> status=%s ok=%s preview=%s", r.status_code, (resp.get("ok") if isinstance(resp, dict) else None), str(resp)[:200])
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
        logger.info("editMessage -> status=%s resp_preview=%s", r.status_code, str(resp)[:200])
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
            resp = r.json()
        except Exception:
            resp = r.text
        logger.info("sendPhoto -> status=%s resp_preview=%s", r.status_code, str(resp)[:200])
        return resp
    except Exception:
        logger.exception("send_photo exception")
        return {}

# ---------------- Keyboards ----------------
def main_menu_kb():
    return {"inline_keyboard": [
        [{"text": "💰 Цена", "callback_data": "menu_price"}],
        [{"text": "📈 График", "callback_data": "menu_chart"}],
        [{"text": "🔔 Добавить Alert", "callback_data": "menu_add_alert"}],
        [{"text": "📋 Мои Alerts", "callback_data": "menu_my_alerts"}],
        [{"text": "⚙️ Мои монеты", "callback_data": "menu_my_symbols"}],
    ]}

def symbols_kb_for(chat_id, prefix):
    syms = get_user_symbols(chat_id)
    rows = []
    row = []
    for i, s in enumerate(syms, 1):
        row.append({"text": s, "callback_data": f"{prefix}|{s}"})
        if i % 2 == 0:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([{"text":"⬅️ Назад","callback_data":"back_main"}])
    return {"inline_keyboard": rows}

def timeframe_kb_for(symbol):
    return {"inline_keyboard": [
        [{"text":"1m","callback_data":f"tf|{symbol}|1m"},{"text":"5m","callback_data":f"tf|{symbol}|5m"},{"text":"15m","callback_data":f"tf|{symbol}|15m"}],
        [{"text":"30m","callback_data":f"tf|{symbol}|30m"},{"text":"1h","callback_data":f"tf|{symbol}|1h"},{"text":"4h","callback_data":f"tf|{symbol}|4h"}],
        [{"text":"1d","callback_data":f"tf|{symbol}|1d"},{"text":"⬅️ Назад","callback_data":"back_main"}]
    ]}

def chart_type_and_source_kb(symbol, timeframe):
    return {"inline_keyboard": [
        [{"text":"🕯 Свечной (биржа)","callback_data":f"chart|{symbol}|{timeframe}|exchange|candles"},
         {"text":"📉 Линейный (биржа)","callback_data":f"chart|{symbol}|{timeframe}|exchange|line"}],
        [{"text":"🕯 Свечной (по истории)","callback_data":f"chart|{symbol}|{timeframe}|history|candles"},
         {"text":"📉 Линейный (по истории)","callback_data":f"chart|{symbol}|{timeframe}|history|line"}],
        [{"text":"⬅️ Назад","callback_data":f"menu_chart"}]
    ]}

# ---------------- Price polling & history ----------------
def fetch_and_store_prices_loop():
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
            # check alerts after updating prices
            check_alerts()
        except Exception:
            logger.exception("price polling loop error")
        time.sleep(max(1, PRICE_POLL_INTERVAL))

# ---------------- Alerts ----------------
def check_alerts():
    try:
        alerts = get_all_alerts()
        for a in alerts:
            aid, chat_id, symbol, target, alert_type, is_recurring, active_until = a
            # if active_until set and expired -> remove
            if active_until:
                try:
                    dt = datetime.strptime(active_until, "%Y-%m-%d %H:%M:%S")
                    if datetime.now() > dt:
                        cur.execute("DELETE FROM alerts WHERE id=?", (aid,))
                        db_commit()
                        continue
                except Exception:
                    pass
            cur_price = last_prices.get(symbol)
            if cur_price is None:
                continue
            triggered = False
            if alert_type == "above" and cur_price > target:
                triggered = True
            elif alert_type == "below" and cur_price < target:
                triggered = True
            elif alert_type == "cross":
                # simple crossing check using history cache
                hist = list(history_cache.get(symbol, []))
                prev = hist[-2][1] if len(hist) >= 2 else None
                if prev is not None and ((prev < target and cur_price >= target) or (prev > target and cur_price <= target)):
                    triggered = True
            if triggered:
                try:
                    send_message(chat_id, f"🔔 Alert сработал: {symbol} {alert_type} {target}$ — текущая {cur_price}$")
                    log_db("info", f"Alert fired: {chat_id} {symbol} {alert_type} {target}")
                except Exception:
                    logger.exception("sending alert")
                if not is_recurring:
                    cur.execute("DELETE FROM alerts WHERE id=?", (aid,))
                    db_commit()
    except Exception:
        logger.exception("check_alerts error")

# ---------------- Charting functions ----------------
def fetch_ohlcv_from_exchange(symbol, timeframe, limit=200):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("timestamp", inplace=True)
        return df
    except Exception:
        logger.exception("fetch_ohlcv_from_exchange error")
        return pd.DataFrame()

def fetch_ohlcv_from_history(symbol, timeframe, limit=200):
    # timeframe used only to choose number of points — DB stores raw ticks
    try:
        cur.execute("SELECT ts, price FROM history WHERE symbol=? ORDER BY id DESC LIMIT ?", (symbol, limit))
        rows = cur.fetchall()
        if not rows:
            return pd.DataFrame()
        rows = rows[::-1]
        times = [datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S") for r in rows]
        prices = [r[1] for r in rows]
        df = pd.DataFrame({"close": prices}, index=pd.to_datetime(times))
        # for candles: create open/high/low approximations using rolling windows
        df["open"] = df["close"].shift(1).fillna(df["close"])
        df["high"] = df["close"].rolling(3, min_periods=1).max()
        df["low"] = df["close"].rolling(3, min_periods=1).min()
        df["volume"] = 0
        df = df[["open", "high", "low", "close", "volume"]]
        return df
    except Exception:
        logger.exception("fetch_ohlcv_from_history error")
        return pd.DataFrame()

def plot_line(df, symbol, timeframe):
    try:
        fig, ax = plt.subplots(figsize=(10,4))
        ax.plot(df.index, df["close"], label="price")
        ax.set_title(f"{symbol} — {timeframe} (line)")
        ax.set_ylabel("Price")
        ax.grid(True, linestyle="--", alpha=0.4)
        fig.autofmt_xdate()
        buf = io.BytesIO()
        plt.tight_layout()
        fig.savefig(buf, format="png", dpi=150)
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception:
        logger.exception("plot_line error")
        return None

def plot_candles(df, symbol, timeframe):
    try:
        fig, ax = plt.subplots(figsize=(10,5))
        times = mdates.date2num(df.index.to_pydatetime())
        width = max(0.0005, (times[1]-times[0]) * 0.6) if len(times) > 1 else 0.0005
        for i in range(len(df)):
            o = df["open"].iat[i]
            c = df["close"].iat[i]
            h = df["high"].iat[i]
            l = df["low"].iat[i]
            t = times[i]
            color = "green" if c >= o else "red"
            ax.plot([t, t], [l, h], color="k", linewidth=0.6)
            rect_bottom = min(o, c)
            rect_height = max(abs(c - o), 1e-8)
            ax.add_patch(plt.Rectangle((t - width/2, rect_bottom), width, rect_height, color=color, alpha=0.9))
        ax.xaxis_date()
        ax.set_title(f"{symbol} — {timeframe} (candles)")
        ax.set_ylabel("Price")
        ax.grid(True, linestyle="--", alpha=0.4)
        fig.autofmt_xdate()
        buf = io.BytesIO()
        plt.tight_layout()
        fig.savefig(buf, format="png", dpi=150)
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception:
        logger.exception("plot_candles error")
        try:
            plt.close(fig)
        except Exception:
            pass
        return None

# ---------------- Handlers ----------------
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

        # interactive flows: add symbol
        if state.get("awaiting_new_symbol"):
            sym = text.upper().strip()
            if "/" not in sym:
                sym = f"{sym}/USDT"
            try:
                markets = exchange.load_markets()
                if sym in markets:
                    add_user_symbol(chat_id, sym)
                    send_message(chat_id, f"✅ Монета {sym} добавлена.", reply_markup=main_menu_kb())
                else:
                    send_message(chat_id, f"❌ Пара {sym} не найдена на MEXC.", reply_markup=main_menu_kb())
            except Exception:
                logger.exception("check symbol error")
                send_message(chat_id, "Ошибка при проверке символа.", reply_markup=main_menu_kb())
            pending_states.pop(str(chat_id), None)
            return

        if state.get("awaiting_delete_symbol"):
            sym = text.upper().strip()
            if "/" not in sym:
                sym = f"{sym}/USDT"
            ok = delete_user_symbol(chat_id, sym)
            if ok:
                send_message(chat_id, f"✅ {sym} удалена.", reply_markup=main_menu_kb())
            else:
                send_message(chat_id, f"❌ {sym} не найдена в вашем списке.", reply_markup=main_menu_kb())
            pending_states.pop(str(chat_id), None)
            return

        # add alert waiting for price
        if state.get("awaiting_alert_target"):
            try:
                target = float(text)
                sym = state.get("alert_symbol")
                save_alert(chat_id, sym, target, alert_type="cross", is_recurring=0)
                send_message(chat_id, f"✅ Alert сохранён: {sym} at {target}$", reply_markup=main_menu_kb())
            except Exception:
                send_message(chat_id, "Ошибка: введите корректное число для цены.", reply_markup=main_menu_kb())
            pending_states.pop(str(chat_id), None)
            return

        # commands
        if text.startswith("/start"):
            send_message(chat_id, "Привет! Выберите действие:", reply_markup=main_menu_kb())
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
                    try:
                        ticker = exchange.fetch_ticker(s)
                        p = float(ticker.get("last") or ticker.get("close") or 0.0)
                        send_message(chat_id, f"💰 {s}: {p}$", reply_markup=main_menu_kb())
                    except Exception:
                        send_message(chat_id, f"Нет данных для {s}.", reply_markup=main_menu_kb())
            else:
                send_message(chat_id, "Использование: /price SYMBOL", reply_markup=main_menu_kb())
            return

        # fallback
        send_message(chat_id, "Выберите действие:", reply_markup=main_menu_kb())
    except Exception:
        logger.exception("handle_text_message error")

def handle_callback(cq):
    try:
        cb_id = cq.get("id")
        data = cq.get("data", "")
        from_user = cq.get("from", {})
        user_id = from_user.get("id")
        message = cq.get("message", {}) or {}
        chat_id = message.get("chat", {}).get("id") or user_id
        add_user(chat_id)
        log_db("info", f"callback {data} from {chat_id}")
        # answer to remove spinner
        answer_callback(cb_id)

        # navigation
        if data == "back_main":
            send_message(chat_id, "Главное меню:", reply_markup=main_menu_kb())
            return

        if data == "menu_price":
            send_message(chat_id, "Выберите монету:", reply_markup=symbols_kb_for(chat_id, "price"))
            return

        if data.startswith("price|"):
            _, sym = data.split("|",1)
            try:
                ticker = exchange.fetch_ticker(sym)
                price = float(ticker.get("last") or ticker.get("close") or 0.0)
                send_message(chat_id, f"💰 {sym}: {price}$", reply_markup=main_menu_kb())
            except Exception:
                logger.exception("price fetch error")
                send_message(chat_id, f"Ошибка получения цены для {sym}", reply_markup=main_menu_kb())
            return

        if data == "menu_chart":
            send_message(chat_id, "Выберите монету для графика:", reply_markup=symbols_kb_for(chat_id, "chart"))
            return

        if data.startswith("chart|") and data.count("|") == 4:
            # full chart action already encoded: chart|symbol|tf|source|type
            _, sym, tf, source, chart_type = data.split("|")
            # choose source/type and generate
            try:
                if source == "exchange":
                    if not HAS_CHARTS:
                        send_message(chat_id, "Charts not available (pandas/matplotlib missing).", reply_markup=main_menu_kb())
                        return
                    df = fetch_ohlcv_from_exchange(sym, tf, limit=200)
                else:
                    if not HAS_CHARTS:
                        send_message(chat_id, "Charts not available (pandas/matplotlib missing).", reply_markup=main_menu_kb())
                        return
                    df = fetch_ohlcv_from_history(sym, tf, limit=200)
                if df.empty or len(df) < 3:
                    send_message(chat_id, "Недостаточно данных для графика.", reply_markup=main_menu_kb())
                    return
                if chart_type == "line":
                    buf = plot_line(df, sym, tf)
                else:
                    buf = plot_candles(df, sym, tf)
                if buf:
                    send_photo_bytes(chat_id, buf, caption=f"{sym} — {tf} ({source}/{chart_type})")
                else:
                    send_message(chat_id, "Ошибка при построении графика.", reply_markup=main_menu_kb())
            except Exception:
                logger.exception("chart generation error")
                send_message(chat_id, "Ошибка при построении графика.", reply_markup=main_menu_kb())
            return

        if data.startswith("chart|") and data.count("|") == 1:
            # user selected symbol for chart -> offer timeframe choices
            _, sym = data.split("|",1)
            send_message(chat_id, f"Выберите таймфрейм для {sym}:", reply_markup=timeframe_kb_for(sym))
            return

        if data.startswith("tf|"):
            # user selected timeframe -> ask chart type & source
            _, sym, tf = data.split("|")
            send_message(chat_id, f"Выберите тип графика и источник для {sym} {tf}:", reply_markup=chart_type_and_source_kb(sym, tf))
            return

        if data == "menu_add_alert":
            send_message(chat_id, "Выберите монету для Alert:", reply_markup=symbols_kb_for(chat_id, "addalert"))
            return

        if data.startswith("addalert|"):
            _, sym = data.split("|",1)
            pending_states[str(chat_id)] = {"awaiting_alert_target": True, "alert_symbol": sym}
            send_message(chat_id, f"Введите цену для Alert на {sym} (число) или /cancel")
            return

        if data == "menu_my_alerts":
            rows = list_alerts(chat_id)
            if not rows:
                send_message(chat_id, "У вас пока нет Alerts.", reply_markup=main_menu_kb())
                return
            kb = {"inline_keyboard": []}
            for r in rows:
                aid, sym, targ, atype, rec, a_until = r
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
                send_message(chat_id, "Ошибка удаления.", reply_markup=main_menu_kb())
            return

        if data == "menu_my_symbols":
            kb = symbols_kb_for(chat_id, "sym|")
            # add rows for text-add/delete
            kb["inline_keyboard"].append([{"text":"➕ Добавить (текстом)","callback_data":"add_symbol_text"},{"text":"🗑 Удалить (текстом)","callback_data":"del_symbol_text"}])
            kb["inline_keyboard"].append([{"text":"⬅️ Назад","callback_data":"back_main"}])
            send_message(chat_id, "Ваши монеты:", reply_markup=kb)
            return

        if data == "add_symbol_text":
            pending_states[str(chat_id)] = {"awaiting_new_symbol": True}
            send_message(chat_id, "Введите символ для добавления (например ADA или ADA/USDT):")
            return

        if data == "del_symbol_text":
            pending_states[str(chat_id)] = {"awaiting_delete_symbol": True}
            send_message(chat_id, "Введите символ для удаления (например ADA или ADA/USDT):")
            return

        # catch-all
        send_message(chat_id, "Неизвестная кнопка. Возврат в меню.", reply_markup=main_menu_kb())

    except Exception:
        logger.exception("handle_callback error")

# ---------------- Webhook route ----------------
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    try:
        update = request.get_json(force=True)
        # log update to help debug callback vs message
        try:
            logger.info("Incoming update: %s", json.dumps(update, ensure_ascii=False))
        except Exception:
            logger.info("Incoming update (raw): %s", str(update))

        if "callback_query" in update:
            handle_callback(update["callback_query"])
            return {"ok": True}

        msg = update.get("message") or update.get("edited_message")
        if msg:
            handle_text_message(msg)
            return {"ok": True}
        return {"ok": True}
    except Exception:
        logger.exception("webhook exception")
        return {"ok": True}

# ---------------- Start workers & app ----------------
def start_workers():
    try:
        t = threading.Thread(target=fetch_and_store_prices_loop, daemon=True)
        t.start()
        logger.info("Started price polling worker")
    except Exception:
        logger.exception("start_workers error")

if __name__ == "__main__":
    start_workers()
    logger.info("Starting Flask webhook app on port %s", PORT)
    # Use app.run for local debug; in production use gunicorn (Railway)
    try:
        app.run(host="0.0.0.0", port=PORT)
    except Exception:
        logger.exception("Flask run error")
