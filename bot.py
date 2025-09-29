#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Crypto Bot (aiogram v2.25.1)
Features:
- aiogram v2 handlers, inline keyboards
- SQLite persistence (users, user_symbols, alerts, history, logs)
- Background price polling (ccxt.MEXC) in separate thread
- Alerts: above/below/cross, recurring or one-time, time limits
- Inline UI to add alerts, choose symbols, quick price list, charts
- Chart generation (matplotlib / mplfinance) if available; fails gracefully if libs missing
- Logging (console + DB). Errors don't crash the bot; they are logged.
- Token is read from env TELEGRAM_TOKEN or token.txt; in interactive mode -- prompts and saves.
- Can run in Railway using polling (recommended) or switch to webhook mode by configuring USE_WEBHOOK env var.
"""

import os
import sys
import time
import json
import math
import threading
import logging
import sqlite3
import traceback
from datetime import datetime, timedelta
from collections import defaultdict, deque
from typing import Optional, List, Tuple

# ---------- Third-party libs ----------
# Make sure to install matching versions:
# pip install aiogram==2.25.1 ccxt flask pandas matplotlib numpy python-dotenv mplfinance
try:
    from aiogram import Bot, types
    from aiogram.dispatcher import Dispatcher
    from aiogram.utils import executor
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
except Exception as e:
    print("aiogram import failed. Make sure aiogram==2.25.1 is installed.")
    raise

import requests
import ccxt
# optional libs
try:
    import pandas as pd
except Exception:
    pd = None
try:
    import matplotlib.pyplot as plt
except Exception:
    plt = None
try:
    import mplfinance as mpf
except Exception:
    mpf = None
import numpy as np

# ---------- Configuration ----------
# async defaults
DB_PATH = os.getenv("DB_PATH", "bot_data.sqlite")
PRICE_POLL_INTERVAL = int(os.getenv("PRICE_POLL_INTERVAL", "5"))  # seconds
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "1000"))
defAULT_SYMBOLS = os.getenv("async defAULT_SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT,NEAR/USDT").split(",")
PRICE_FETCH_TIMEOUT = float(os.getenv("PRICE_FETCH_TIMEOUT", "5.0"))
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "").lower() in ("1", "true", "yes")

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("crypto_bot")

# ---------- Token handling ----------
def load_token() -> Optional[str]:
    """Load TELEGRAM_TOKEN from env or token.txt; if interactive, ask and save."""
    token = os.getenv("TELEGRAM_TOKEN")
    if token:
        logger.info("TELEGRAM_TOKEN loaded from environment.")
        return token.strip()

    # try token.txt
    if os.path.exists("token.txt"):
        try:
            with open("token.txt", "r", encoding="utf-8") as f:
                t = f.read().strip()
                if t:
                    logger.info("TELEGRAM_TOKEN loaded from token.txt.")
                    return t
        except Exception:
            logger.exception("Failed reading token.txt")

    # if interactive, prompt and save
    try:
        if sys.stdin and sys.stdin.isatty():
            t = input("Введите TELEGRAM_TOKEN (BotFather): ").strip()
            if t:
                try:
                    with open("token.txt", "w", encoding="utf-8") as f:
                        f.write(t)
                    logger.info("Token saved to token.txt")
                except Exception:
                    logger.exception("Failed to save token to token.txt")
                return t
    except Exception:
        logger.info("Not interactive or input failed; TELEGRAM_TOKEN not set.")

    return None

TELEGRAM_TOKEN = load_token()
if not TELEGRAM_TOKEN:
    logger.critical("TELEGRAM_TOKEN not found. Set TELEGRAM_TOKEN as env var or put token in token.txt.")
    # Exit because bot cannot run without token in non-interactive environment
    # If you want to continue without token (for static checks), comment the next line.
    sys.exit(1)

# ---------- Create bot + dispatcher ----------
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(bot)

# ---------- Exchange (synchronous ccxt used in background thread) ----------
try:
    exchange = ccxt.mexc({"enableRateLimit": True})
    logger.info("Initialized ccxt mexc exchange")
except Exception as e:
    logger.exception("Failed to initialize ccxt mexc.")
    exchange = None

# ---------- Database (SQLite) ----------
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()

async def init_db():
    try:
        cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id TEXT PRIMARY KEY,
            created_at DATETIME
        );
        CREATE TABLE IF NOT EXISTS user_symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            symbol TEXT
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            symbol TEXT,
            target REAL,
            alert_type TEXT async defAULT 'cross',
            is_recurring INTEGER async defAULT 0,
            active_until TEXT async defAULT NULL,
            time_start TEXT async defAULT NULL,
            time_end TEXT async defAULT NULL,
            created_at DATETIME async defAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            price REAL,
            ts DATETIME async defAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            level TEXT,
            message TEXT,
            ts DATETIME async defAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_settings (
            chat_id TEXT PRIMARY KEY,
            signals_enabled INTEGER async defAULT 0
        );
        """)
        conn.commit()
        logger.info("DB initialized at %s", DB_PATH)
    except Exception:
        logger.exception("DB init error")

init_db()

# ---------- Logging helper (store in DB + logger) ----------
async def log_db(level: str, message: str):
    try:
        cur.execute("INSERT INTO logs (level, message) VALUES (?, ?)", (level.upper(), message))
        conn.commit()
    except Exception:
        logger.exception("Failed to insert log into DB")
    # console
    getattr(logger, level.lower(), logger.info)(message)

# ---------- In-memory runtime structures ----------
pending_alerts = {}  # chat_id -> dict for flows
last_prices = {}     # symbol -> price
history_cache = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))

# ---------- DB wrappers ----------
async def add_user(chat_id: int):
    try:
        cur.execute("INSERT OR IGNORE INTO users (chat_id, created_at) VALUES (?, ?)", (str(chat_id), datetime.utcnow()))
        conn.commit()
    except Exception:
        logger.exception("add_user error")

async def get_user_symbols(chat_id: int) -> List[str]:
    try:
        cur.execute("SELECT symbol FROM user_symbols WHERE chat_id=?", (str(chat_id),))
        rows = cur.fetchall()
        if rows:
            return [r[0] for r in rows]
    except Exception:
        logger.exception("get_user_symbols error")
    return  defAULT_SYMBOLS.copy()

async def add_user_symbol(chat_id: int, symbol: str) -> bool:
    try:
        cur.execute("INSERT INTO user_symbols (chat_id, symbol) VALUES (?, ?)", (str(chat_id), symbol))
        conn.commit()
        return True
    except Exception:
        logger.exception("add_user_symbol error")
        return False

async def list_user_alerts(chat_id: int):
    try:
        cur.execute("SELECT id, symbol, target, alert_type, is_recurring, active_until FROM alerts WHERE chat_id=? ORDER BY id DESC", (str(chat_id),))
        return cur.fetchall()
    except Exception:
        logger.exception("list_user_alerts error")
        return []

async def delete_alert(alert_id: int, chat_id: int):
    try:
        cur.execute("DELETE FROM alerts WHERE id=? AND chat_id=?", (int(alert_id), str(chat_id)))
        conn.commit()
    except Exception:
        logger.exception("delete_alert error")

async def save_alert_to_db(chat_id: int, symbol: str, target: float, alert_type='cross', is_recurring=0, active_until=None, time_start=None, time_end=None):
    try:
        cur.execute(
            "INSERT INTO alerts (chat_id, symbol, target, alert_type, is_recurring, active_until, time_start, time_end) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(chat_id), symbol, float(target), alert_type, int(is_recurring), active_until, time_start, time_end)
        )
        conn.commit()
        return True
    except Exception:
        logger.exception("save_alert_to_db error")
        return False

async def get_all_alerts():
    try:
        cur.execute("SELECT id, chat_id, symbol, target, alert_type, is_recurring, active_until, time_start, time_end FROM alerts")
        return cur.fetchall()
    except Exception:
        logger.exception("get_all_alerts error")
        return []

# ---------- Telegram UI helpers ----------
async def main_menu_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("💰 Все цены", callback_data="price_all"),
           InlineKeyboardButton("📊 График", callback_data="chart_menu"))
    kb.add(InlineKeyboardButton("🔔 Добавить Alert", callback_data="add_alert"),
           InlineKeyboardButton("📋 Мои Alerts", callback_data="my_alerts"))
    kb.add(InlineKeyboardButton("⚙️ Мои монеты", callback_data="my_symbols"),
           InlineKeyboardButton("⚙️ Настройки", callback_data="settings"))
    return kb

async def price_menu_kb(symbols: List[str]):
    kb = InlineKeyboardMarkup(row_width=3)
    for s in symbols:
        kb.insert(InlineKeyboardButton(s.split("/")[0], callback_data=f"price_{s.split('/')[0]}"))
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="back_main"))
    return kb

async def symbols_keyboard(symbols: List[str], prefix: str, per_row=3):
    kb = InlineKeyboardMarkup(row_width=per_row)
    for s in symbols:
        kb.insert(InlineKeyboardButton(s.split("/")[0], callback_data=f"{prefix}_{s.split('/')[0]}"))
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="back_main"))
    return kb

async def price_steps_kb(include_confirm=True):
    steps = [-10000, -5000, -1000, -100, -10, -1, 1, 10, 100, 1000, 5000, 10000]
    kb = InlineKeyboardMarkup(row_width=6)
    # show sign-less labels for negative rows
    neg = [str(s) for s in steps[:6]]
    pos = [f"+{s}" for s in steps[6:]]
    for s in neg:
        kb.insert(InlineKeyboardButton(s, callback_data=f"step_{s}"))
    for s in pos:
        kb.insert(InlineKeyboardButton(s, callback_data=f"step_{s.replace('+','')}"))
    if include_confirm:
        kb.add(InlineKeyboardButton("✅ Готово", callback_data="price_confirm"))
    kb.add(InlineKeyboardButton("⏱ Автоподтв.", callback_data="auto_time"))
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="back_main"))
    return kb

async def autotime_kb():
    kb = InlineKeyboardMarkup(row_width=3)
    kb.row(InlineKeyboardButton("5s", callback_data="auto_5"),
           InlineKeyboardButton("10s", callback_data="auto_10"),
           InlineKeyboardButton("30s", callback_data="auto_30"))
    kb.row(InlineKeyboardButton("Откл", callback_data="auto_0"),
           InlineKeyboardButton("⬅️ Назад", callback_data="price_steps_back"))
    return kb

# ---------- Price fetching logic (background thread) ----------
async def fetch_price_for_symbol(symbol: str) -> Optional[float]:
    """Synchronous fetch from ccxt mexc. Returns last price or None."""
    if not exchange:
        return None
    try:
        ticker = exchange.fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close")
        if price is None:
            return None
        return float(price)
    except Exception as e:
        logger.debug("fetch error %s: %s", symbol, str(e))
        return None

async def price_polling_worker(stop_event: threading.Event):
    """Background thread that polls prices, saves to DB, checks alerts."""
    logger.info("Price polling worker started, interval %s sec", PRICE_POLL_INTERVAL)
    while not stop_event.is_set():
        try:
            # build symbols list from DB + async defaults
            try:
                cur.execute("SELECT DISTINCT symbol FROM user_symbols")
                rows = cur.fetchall()
                symbols = set( defAULT_SYMBOLS)
                symbols.update(r[0] for r in rows)
            except Exception:
                logger.exception("Error building symbol list")
                symbols = set( defAULT_SYMBOLS)

            for sym in list(symbols):
                try:
                    price = fetch_price_for_symbol(sym)
                    if price is None:
                        continue
                    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
                    last_prices[sym] = price
                    # insert history
                    try:
                        cur.execute("INSERT INTO history (symbol, price, ts) VALUES (?, ?, ?)", (sym, price, ts))
                        conn.commit()
                        history_cache[sym].append((ts, price))
                    except Exception:
                        logger.exception("history insert error for %s", sym)
                except Exception:
                    logger.exception("fetch loop inner error for %s", sym)
            # check alerts (synchronously)
            try:
                check_alerts()
            except Exception:
                logger.exception("check_alerts error")
        except Exception:
            logger.exception("price_polling_worker top error")
        # sleep
        stop_event.wait(PRICE_POLL_INTERVAL)
    logger.info("Price polling worker stopping")

# ---------- Alert checking (uses last_prices and history_cache) ----------
async def is_within_time_window(time_start: Optional[str], time_end: Optional[str]) -> bool:
    if not time_start or not time_end:
        return True
    now = datetime.utcnow().strftime("%H:%M")
    try:
        return time_start <= now <= time_end
    except Exception:
        return True

async def check_alerts():
    """Checks DB alerts against last_prices/history and sends messages via bot (from background thread)."""
    alerts = get_all_alerts()
    for alert in alerts:
        alert_id, chat_id, symbol, target, alert_type, is_recurring, active_until, time_start, time_end = alert
        # expiry
        if active_until:
            try:
                ru = datetime.strptime(active_until, "%Y-%m-%d %H:%M:%S")
                if datetime.utcnow() > ru:
                    cur.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
                    conn.commit()
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
        try:
            if alert_type == "above":
                if cur_price > target:
                    triggered = True
            elif alert_type == "below":
                if cur_price < target:
                    triggered = True
            else:  # cross
                if prev_price is not None:
                    if (prev_price < target <= cur_price) or (prev_price > target >= cur_price):
                        triggered = True
        except Exception:
            logger.exception("alert eval error for id %s", alert_id)
        if triggered:
            # send message via bot in asyncio loop
            text = f"🔔 Alert сработал: {symbol} {alert_type} {target}$ (текущая {cur_price}$)"
            try:
                # schedule coroutine from thread
                loop = dp.loop
                if loop and loop.is_running():
                    loop.call_soon_threadsafe(lambda: dp.loop.create_task( bot.send_message(chat_id, text)))
                else:
                    logger.warning("Event loop not running, cannot send alert to %s", chat_id)
                log_db("info", f"Alert fired for {chat_id} {symbol} {alert_type} {target}")
            except Exception:
                logger.exception("Failed to schedule alert message")
            # delete one-time
            if not is_recurring:
                try:
                    cur.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
                    conn.commit()
                except Exception:
                    logger.exception("Failed to delete one-time alert %s", alert_id)

# ---------- Chart generation ----------
async def build_candlestick_chart(symbol: str, timeframe: str = "1m", points: int = 200):
    """
    Build candlestick chart using history DB or last_prices.
    timeframe not used to fetch new data here; assumes history has sufficiently frequent rows.
    Returns bytes-like object or None.
    """
    # prefer history DB
    try:
        cur.execute("SELECT ts, price FROM history WHERE symbol=? ORDER BY id DESC LIMIT ?", (symbol, points*10))
        rows = cur.fetchall()
        if not rows:
            return None
        # rows are newest first; reverse
        rows = rows[::-1]
        # We only have price per time point; for candles need OHLC.
        # We'll convert sequential points into candles of requested timeframe using pandas if available.
        if pd is None or mpf is None:
            # fallback: simple line chart with matplotlib
            if plt is None:
                logger.warning("No plotting libs available")
                return None
            times = [datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S") for r in rows]
            prices = [r[1] for r in rows]
            import io
            fig, ax = plt.subplots(figsize=(10,4))
            ax.plot(times, prices)
            ax.set_title(symbol)
            ax.grid(True)
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=120)
            plt.close(fig)
            buf.seek(0)
            return buf
        # build DataFrame with timestamp index
        times = [datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S") for r in rows]
        prices = [r[1] for r in rows]
        df = pd.DataFrame({"price": prices}, index=pd.DatetimeIndex(times))
        # resample to desired candle timeframe using 'timeframe' parameter mapping
        # user requested timeframe as '1m','5m','15m','30m','1h','4h','1d'
        tf_map = {"1m":"1T","5m":"5T","15m":"15T","30m":"30T","1h":"1H","4h":"4H","1d":"1D"}
        rule = tf_map.get(timeframe, "1T")
        ohlc = df["price"].resample(rule).ohlc()
        ohlc.dropna(inplace=True)
        if ohlc.empty:
            return None
        # use mplfinance to plot candlestick
        import io
        fig_buf = io.BytesIO()
        try:
            mpf.plot(ohlc, type='candle', style='yahoo', volume=False, savefig=dict(fname=fig_buf, dpi=120, bbox_inches='tight'))
            fig_buf.seek(0)
            return fig_buf
        except Exception:
            # fallback to matplotlib line chart
            fig, ax = plt.subplots(figsize=(10,4))
            ax.plot(ohlc.index, ohlc['close'])
            ax.set_title(f"{symbol} {timeframe}")
            ax.grid(True)
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=120)
            plt.close(fig)
            buf.seek(0)
            return buf
    except Exception:
        logger.exception("build_candlestick_chart error")
        return None

# ---------- Bot handlers: commands + callbacks ----------
@dp.message_handler(commands=['start', 'help'])
async  def cmd_start(message: types.Message):
    try:
        add_user(message.chat.id)
        send_text = ("👋 Привет! Я крипто-бот.\n\n"
                     "Управление через кнопки. Нажми кнопку ниже.")
        await  bot.send_message(message.chat.id, send_text, reply_markup=main_menu_kb())
    except Exception:
        logger.exception("cmd_start error")

@dp.callback_query_handler(lambda c: c.data == 'back_main')
async def cb_back_main(callback_query: types.CallbackQuery):
    try:
        bot.edit_message_text("Главное меню:", callback_query.from_user.id, callback_query.message.message_id, reply_markup=main_menu_kb())
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_back_main error")
        try:
            bot.answer_callback_query(callback_query.id, text="Ошибка")
        except Exception:
            pass

# Price all
@dp.callback_query_handler(lambda c: c.data == 'price_all')
async def cb_price_all(callback_query: types.CallbackQuery):
    try:
        add_user(callback_query.from_user.id)
        text_lines = []
        # gather symbols (user-specific plus async defaults)
        syms = set( defAULT_SYMBOLS)
        try:
            cur.execute("SELECT DISTINCT symbol FROM user_symbols WHERE chat_id=?", (str(callback_query.from_user.id),))
            rows = cur.fetchall()
            syms.update(r[0] for r in rows)
        except Exception:
            logger.exception("cb_price_all symbols fetch error")
        for s in sorted(syms):
            price = last_prices.get(s)
            display = f"{price}$" if price is not None else "n/a"
            text_lines.append(f"{s}: {display}")
        msg = "\n".join(text_lines) if text_lines else "Нет данных"
        await bot.send_message(callback_query.from_user.id, f"💱 Текущие цены:\n{msg}", reply_markup=main_menu_kb())
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_price_all error")

# Price single by symbol from inline buttons
@dp.callback_query_handler(lambda c: c.data and c.data.startswith("price_"))
async def cb_price_single(callback_query: types.CallbackQuery):
    try:
        symbol_label = callback_query.data.split("_",1)[1]
        # find full symbol (search in user list and async defaults)
        full = None
        candidates = defAULT_SYMBOLS.copy()
        try:
            cur.execute("SELECT symbol FROM user_symbols WHERE chat_id=?", (str(callback_query.from_user.id),))
            rows = cur.fetchall()
            if rows:
                candidates += [r[0] for r in rows]
        except Exception:
            logger.exception("cb_price_single fetch user_symbols")
        for c in candidates:
            if c.split("/")[0] == symbol_label:
                full = c
                break
        if full is None:
            bot.answer_callback_query(callback_query.id, text="Монета не найдена")
            return
        price = last_prices.get(full)
        await bot.send_message(callback_query.from_user.id, f"💰 {full}: {price if price is not None else 'n/a'}$", reply_markup=main_menu_kb())
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_price_single error")

# Chart menu: choose symbol then timeframe
@dp.callback_query_handler(lambda c: c.data == 'chart_menu')
async def cb_chart_menu(callback_query: types.CallbackQuery):
    try:
        syms = get_user_symbols(callback_query.from_user.id)
        kb = symbols_keyboard(syms, prefix="chart", per_row=3)
        await bot.send_message(callback_query.from_user.id, "Выберите монету для графика:", reply_markup=kb)
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_chart_menu error")

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("chart_"))
async def cb_chart_choose_symbol(callback_query: types.CallbackQuery):
    try:
        label = callback_query.data.split("_",1)[1]
        # save pending state
        pending_alerts[str(callback_query.from_user.id)] = {"chart_symbol": label}
        # ask timeframe
        kb = InlineKeyboardMarkup(row_width=3)
        for tf in ["1m","5m","15m","30m","1h","4h","1d"]:
            kb.insert(InlineKeyboardButton(tf, callback_data=f"chart_tf_{tf}"))
        kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="back_main"))
        await bot.send_message(callback_query.from_user.id, f"Выбрана {label}. Выберите таймфрейм:", reply_markup=kb)
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_chart_choose_symbol error")

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("chart_tf_"))
async def cb_chart_generate(callback_query: types.CallbackQuery):
    try:
        tf = callback_query.data.split("_",2)[2]
        key = str(callback_query.from_user.id)
        state = pending_alerts.get(key, {})
        symbol_label = state.get("chart_symbol")
        if not symbol_label:
            bot.answer_callback_query(callback_query.id, text="Сначала выберите монету")
            return
        # find full symbol
        full = None
        syms = get_user_symbols(callback_query.from_user.id)
        for s in syms:
            if s.split("/")[0] == symbol_label:
                full = s
                break
        if not full:
            bot.answer_callback_query(callback_query.id, text="Пара не найдена")
            return
        bot.answer_callback_query(callback_query.id, text="Генерирую график... (может занять время)")
        imgbuf = build_candlestick_chart(full, timeframe=tf, points=200)
        if not imgbuf:
            await bot.send_message(callback_query.from_user.id, "Не удалось построить график (недостаточно данных или отсутствуют библиотеки).", reply_markup=main_menu_kb())
            return
        # send as photo
        try:
            imgbuf.seek(0)
            bot.send_photo(callback_query.from_user.id, ('chart.png', imgbuf), caption=f"{full} {tf}")
        except Exception:
            # alternative: send as document
            try:
                imgbuf.seek(0)
                bot.send_document(callback_query.from_user.id, ('chart.png', imgbuf), caption=f"{full} {tf}")
            except Exception:
                logger.exception("Failed to send chart")
                await bot.send_message(callback_query.from_user.id, "Ошибка при отправке графика.")
    except Exception:
        logger.exception("cb_chart_generate error")

# Add alert flow: choose coin via buttons
@dp.callback_query_handler(lambda c: c.data == 'add_alert')
async def cb_add_alert_start(callback_query: types.CallbackQuery):
    try:
        syms = get_user_symbols(callback_query.from_user.id)
        kb = symbols_keyboard(syms, prefix="set_alert", per_row=3)
        await bot.send_message(callback_query.from_user.id, "Выберите монету для создания alert:", reply_markup=kb)
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_add_alert_start error")

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("set_alert_"))
async def cb_add_alert_choose(callback_query: types.CallbackQuery):
    try:
        coin = callback_query.data.split("_",2)[2]
        symbol = coin + "/USDT"
        base_price = fetch_price_for_symbol(symbol) or 0.0
        # pending flow
        pending_alerts[str(callback_query.from_user.id)] = {
            "coin": coin,
            "symbol": symbol,
            "price": base_price,
            "msg_id": None,
            "last_step": None,
            "last_time": time.time(),
            "multiplier": 1,
            "autotime": 10,
            "awaiting_type": False
        }
        kb = price_steps_kb(include_confirm=True)
        resp = await bot.send_message(callback_query.from_user.id, f"{coin}\nБазовая цена: {base_price}$\nНастройте цену кнопками:", reply_markup=kb)
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_add_alert_choose error")

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("step_"))
async def cb_price_step(callback_query: types.CallbackQuery):
    try:
        raw = callback_query.data.split("_",1)[1]
        try:
            step = int(raw)
        except:
            step = 0
        key = str(callback_query.from_user.id)
        if key not in pending_alerts:
            bot.answer_callback_query(callback_query.id, text="Нет активного создания alert")
            return
        alert = pending_alerts[key]
        now = time.time()
        if alert.get("last_step") == step and now - alert.get("last_time",0) < 3:
            alert["multiplier"] = alert.get("multiplier",1) + 1
        else:
            alert["multiplier"] = 1
        final_step = step * alert["multiplier"]
        alert["price"] = max(0, alert.get("price",0) + final_step)
        alert["last_step"] = step
        alert["last_time"] = now
        # update message
        text = f"📊 {alert['coin']}\nТекущая цена: {alert['price']}$\n(шаг {step}$ ×{alert['multiplier']} = {final_step}$)"
        try:
            # try editing last bot message (best-effort)
            if callback_query.message:
                bot.edit_message_text(text, callback_query.from_user.id, callback_query.message.message_id, reply_markup=price_steps_kb(include_confirm=True))
        except Exception:
            # fallback: send new message
            await bot.send_message(callback_query.from_user.id, text, reply_markup=price_steps_kb(include_confirm=True))
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_price_step error")

@dp.callback_query_handler(lambda c: c.data == 'auto_time')
async def cb_auto_time(callback_query: types.CallbackQuery):
    try:
        key = str(callback_query.from_user.id)
        if key not in pending_alerts:
            bot.answer_callback_query(callback_query.id, text="Нет активного создания alert")
            return
        bot.edit_message_text("Выберите время автоподтв.:", callback_query.from_user.id, callback_query.message.message_id, reply_markup=autotime_kb())
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_auto_time error")

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("auto_"))
async def cb_auto_set(callback_query: types.CallbackQuery):
    try:
        val = int(callback_query.data.split("_",1)[1])
        key = str(callback_query.from_user.id)
        if key in pending_alerts:
            pending_alerts[key]["autotime"] = val
            alert = pending_alerts[key]
            text = f"📊 {alert['coin']}\nТекущая цена: {alert['price']}$\n⏱ Автоподтв.: {'выкл' if val==0 else str(val)+' сек'}"
            bot.edit_message_text(text, callback_query.from_user.id, callback_query.message.message_id, reply_markup=price_steps_kb(include_confirm=True))
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_auto_set error")

@dp.callback_query_handler(lambda c: c.data == 'price_confirm')
async def cb_price_confirm(callback_query: types.CallbackQuery):
    try:
        key = str(callback_query.from_user.id)
        if key not in pending_alerts:
            bot.answer_callback_query(callback_query.id, text="Нет активного создания alert")
            return
        a = pending_alerts[key]
        # ask type
        kb = InlineKeyboardMarkup(row_width=3)
        kb.row(InlineKeyboardButton("📈 Выше", callback_data="alert_type_above"),
               InlineKeyboardButton("📉 Ниже", callback_data="alert_type_below"),
               InlineKeyboardButton("🔄 Пересечение", callback_data="alert_type_cross"))
        await bot.send_message(callback_query.from_user.id, f"✅ Вы выбрали {a['coin']} при цене {a['price']}$.\nВыберите тип сигнала:", reply_markup=kb)
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_price_confirm error")

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("alert_type_"))
async def cb_alert_type(callback_query: types.CallbackQuery):
    try:
        typ = callback_query.data.split("_",2)[2]
        key = str(callback_query.from_user.id)
        if key in pending_alerts:
            pending_alerts[key]["type_selected"] = typ
            # ask recurring
            kb = InlineKeyboardMarkup(row_width=2)
            kb.row(InlineKeyboardButton("☑️ Одноразовый", callback_data="alert_rec_no"),
                   InlineKeyboardButton("🔂 Постоянный", callback_data="alert_rec_yes"))
            await bot.send_message(callback_query.from_user.id, "Одноразовый или постоянный?", reply_markup=kb)
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_alert_type error")

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("alert_rec_"))
async def cb_alert_recurring(callback_query: types.CallbackQuery):
    try:
        rec = 1 if callback_query.data.endswith("yes") else 0
        key = str(callback_query.from_user.id)
        if key in pending_alerts:
            pending_alerts[key]["recurring"] = rec
            kb = InlineKeyboardMarkup(row_width=2)
            kb.row(InlineKeyboardButton("⏰ Только сегодня", callback_data="time_today"),
                   InlineKeyboardButton("📅 Задать часы", callback_data="time_custom"))
            kb.row(InlineKeyboardButton("♾️ Без ограничений", callback_data="time_none"),
                   InlineKeyboardButton("⬅️ Назад", callback_data="back_main"))
            await bot.send_message(callback_query.from_user.id, "Ограничить сигнал по времени?", reply_markup=kb)
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_alert_recurring error")

@dp.callback_query_handler(lambda c: c.data == 'time_today')
async def cb_time_today(callback_query: types.CallbackQuery):
    try:
        key = str(callback_query.from_user.id)
        if key in pending_alerts:
            active_until = (datetime.utcnow().replace(hour=23, minute=59, second=59)).strftime("%Y-%m-%d %H:%M:%S")
            pending_alerts[key]["active_until"] = active_until
            a = pending_alerts[key]
            ok = save_alert_to_db(callback_query.from_user.id, f"{a['symbol']}", a['price'], a.get("type_selected","cross"), a.get("recurring",0), a.get("active_until"), None, None)
            if ok:
                await bot.send_message(callback_query.from_user.id, f"✅ Alert добавлен: {a['symbol']} {a.get('type_selected','cross')} {a['price']}$ (до конца дня)")
                log_db("info", f"Alert saved for {callback_query.from_user.id} {a['symbol']} {a['price']}")
            else:
                await bot.send_message(callback_query.from_user.id, "Ошибка при сохранении алерта.")
            del pending_alerts[key]
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_time_today error")

@dp.callback_query_handler(lambda c: c.data == 'time_none')
async def cb_time_none(callback_query: types.CallbackQuery):
    try:
        key = str(callback_query.from_user.id)
        if key in pending_alerts:
            a = pending_alerts[key]
            ok = save_alert_to_db(callback_query.from_user.id, f"{a['symbol']}", a['price'], a.get("type_selected","cross"), a.get("recurring",0), None, None, None)
            if ok:
                await bot.send_message(callback_query.from_user.id, f"✅ Alert добавлен: {a['symbol']} {a.get('type_selected','cross')} {a['price']}$")
                log_db("info", f"Alert saved for {callback_query.from_user.id} {a['symbol']} {a['price']}")
            else:
                await bot.send_message(callback_query.from_user.id, "Ошибка при сохранении алерта.")
            del pending_alerts[key]
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_time_none error")

@dp.callback_query_handler(lambda c: c.data == 'time_custom')
async def cb_time_custom(callback_query: types.CallbackQuery):
    try:
        key = str(callback_query.from_user.id)
        if key in pending_alerts:
            pending_alerts[key]["awaiting_time"] = True
            await bot.send_message(callback_query.from_user.id, "Введите срок действия в часах (например: 24):")
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_time_custom error")

# message handler for awaiting_time and awaiting_new_symbol (text messages)
@dp.message_handler()
async def msg_text_handler(message: types.Message):
    try:
        add_user(message.chat.id)
        key = str(message.chat.id)
        state = pending_alerts.get(key)
        if state and state.get("awaiting_time"):
            try:
                hours = int(message.text.strip())
                active_until = (datetime.utcnow() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
                a = pending_alerts[key]
                a["active_until"] = active_until
                ok = save_alert_to_db(message.chat.id, f"{a['symbol']}", a['price'], a.get("type_selected","cross"), a.get("recurring",0), a.get("active_until"), None, None)
                if ok:
                    await bot.send_message(message.chat.id, f"✅ Alert добавлен: {a['symbol']} {a.get('type_selected','cross')} {a['price']}$ (до {active_until})")
                    log_db("info", f"Alert saved for {message.chat.id} {a['symbol']} {a['price']}")
                else:
                    await bot.send_message(message.chat.id, "Ошибка при сохранении алерта.")
                del pending_alerts[key]
            except Exception:
                await bot.send_message(message.chat.id, "Ошибка: введите целое число часов (например: 24).")
            finally:
                await bot.send_message(message.chat.id, "Меню:", reply_markup=main_menu_kb())
            return

        # new symbol adding flow (if any)
        if state and state.get("awaiting_new_symbol"):
            symbol = message.text.upper().strip()
            if "/" not in symbol:
                symbol = f"{symbol}/USDT"
            try:
                # check markets
                markets = exchange.load_markets()
                if symbol in markets:
                    add_user_symbol(message.chat.id, symbol)
                    await bot.send_message(message.chat.id, f"✅ Монета {symbol} добавлена в ваш список.")
                else:
                    await bot.send_message(message.chat.id, f"❌ Пара {symbol} не найдена на MEXC.")
            except Exception:
                logger.exception("Error checking markets")
                await bot.send_message(message.chat.id, f"Ошибка при проверке {symbol}.")
            del pending_alerts[key]
            await bot.send_message(message.chat.id, "Меню:", reply_markup=main_menu_kb())
            return

        # fallback - other text commands: /price SYMBOL, /chart SYMBOL
        text = (message.text or "").strip()
        if text.startswith("/price"):
            parts = text.split()
            if len(parts) == 2:
                s = parts[1].upper()
                if "/" not in s:
                    s = s + "/USDT"
                price = last_prices.get(s)
                await bot.send_message(message.chat.id, f"💰 {s}: {price if price is not None else 'n/a'}$")
            else:
                await bot.send_message(message.chat.id, "Использование: /price SYMBOL")
            await bot.send_message(message.chat.id, "Меню:", reply_markup=main_menu_kb())
            return

        if text.startswith("/chart"):
            parts = text.split()
            if len(parts) >= 2:
                s = parts[1].upper()
                if "/" not in s:
                    s = s + "/USDT"
                buf = build_candlestick_chart(s, timeframe="1h", points=200)
                if buf:
                    try:
                        buf.seek(0)
                        bot.send_photo(message.chat.id, ('chart.png', buf), caption=f"{s} — график")
                    except Exception:
                        await bot.send_message(message.chat.id, "Ошибка отправки графика")
                else:
                    await bot.send_message(message.chat.id, "Недостаточно данных или отсутствует библиотека графиков.")
            else:
                await bot.send_message(message.chat.id, "Использование: /chart SYMBOL")
            await bot.send_message(message.chat.id, "Меню:", reply_markup=main_menu_kb())
            return

        # async default menu
        await bot.send_message(message.chat.id, "Выберите действие:", reply_markup=main_menu_kb())
    except Exception:
        logger.exception("msg_text_handler error")

# My alerts list
@dp.callback_query_handler(lambda c: c.data == 'my_alerts')
async def cb_my_alerts(callback_query: types.CallbackQuery):
    try:
        rows = list_user_alerts(callback_query.from_user.id)
        if not rows:
            await bot.send_message(callback_query.from_user.id, "У вас пока нет активных Alerts.", reply_markup=main_menu_kb())
            bot.answer_callback_query(callback_query.id)
            return
        kb = InlineKeyboardMarkup(row_width=1)
        for r in rows:
            aid, sym, targ, atype, rec, a_until = r
            label = ("🔂" if rec else "☑️") + f" {sym} {atype} {targ}$"
            kb.add(InlineKeyboardButton(label, callback_data=f"del_alert_{aid}"))
        kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="back_main"))
        await bot.send_message(callback_query.from_user.id, "Ваши Alerts (нажмите чтобы удалить):", reply_markup=kb)
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_my_alerts error")

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("del_alert_"))
async def cb_delete_alert(callback_query: types.CallbackQuery):
    try:
        aid = int(callback_query.data.split("_",2)[2])
        delete_alert(aid, callback_query.from_user.id)
        await bot.send_message(callback_query.from_user.id, "✅ Alert удалён.", reply_markup=main_menu_kb())
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_delete_alert error")

# My symbols
@dp.callback_query_handler(lambda c: c.data == 'my_symbols')
async def cb_my_symbols(callback_query: types.CallbackQuery):
    try:
        syms = get_user_symbols(callback_query.from_user.id)
        text = "Ваши монеты:\n" + "\n".join(syms)
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("➕ Добавить монету", callback_data="add_symbol"))
        kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="back_main"))
        await bot.send_message(callback_query.from_user.id, text, reply_markup=kb)
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_my_symbols error")

@dp.callback_query_handler(lambda c: c.data == 'add_symbol')
async def cb_add_symbol(callback_query: types.CallbackQuery):
    try:
        pending_alerts[str(callback_query.from_user.id)] = {"awaiting_new_symbol": True}
        await bot.send_message(callback_query.from_user.id, "Введите символ монеты (например: ADA или ADA/USDT).")
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_add_symbol error")

# Settings (toggle signals)
@dp.callback_query_handler(lambda c: c.data == 'settings')
async def cb_settings(callback_query: types.CallbackQuery):
    try:
        cur.execute("SELECT signals_enabled FROM user_settings WHERE chat_id=?", (str(callback_query.from_user.id),))
        row = cur.fetchone()
        cur_val = row[0] if row else 0
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("🔁 Авто-сигналы Вкл/Выкл", callback_data="toggle_signals"))
        kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="back_main"))
        await bot.send_message(callback_query.from_user.id, f"Авто-сигналы: {'Вкл' if cur_val==1 else 'Выкл'}", reply_markup=kb)
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_settings error")

@dp.callback_query_handler(lambda c: c.data == 'toggle_signals')
async def cb_toggle_signals(callback_query: types.CallbackQuery):
    try:
        cur.execute("SELECT signals_enabled FROM user_settings WHERE chat_id=?", (str(callback_query.from_user.id),))
        row = cur.fetchone()
        cur_val = row[0] if row else 0
        new_val = 0 if cur_val == 1 else 1
        cur.execute("INSERT OR REPLACE INTO user_settings (chat_id, signals_enabled) VALUES (?, ?)", (str(callback_query.from_user.id), new_val))
        conn.commit()
        await bot.send_message(callback_query.from_user.id, f"Авто-сигналы {'включены' if new_val==1 else 'выключены'}.")
        bot.answer_callback_query(callback_query.id)
    except Exception:
        logger.exception("cb_toggle_signals error")

# ---------- Startup / Shutdown ----------

price_thread_stop = threading.Event()
price_thread = threading.Thread(target=price_polling_worker, args=(price_thread_stop,), daemon=True)

async def on_startup(dp: Dispatcher):
    try:
        logger.info("Starting background price thread")
        price_thread.start()
    except Exception:
        logger.exception("on_startup error")

async def on_shutdown(dp: Dispatcher):
    try:
        logger.info("Shutting down: stopping price thread")
        price_thread_stop.set()
        price_thread.join(timeout=5)
    except Exception:
        logger.exception("on_shutdown error")
    try:
        logger.info("Closing DB connection")
        conn.close()
    except Exception:
        logger.exception("Closing DB error")

# ---------- Main entry ----------
if __name__ == '__main__':
    # Print small usage message
    logger.info("Bot starting. Use TELEGRAM_TOKEN env var or token.txt. Polling mode by async default.")
    # Start long polling
    try:
        executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)
    except Exception:
        logger.exception("executor.start_polling failed")

import asyncio

async def main():
    # тут твои настройки бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

async def on_startup(dp):
    logging.info("Bot started")

async def on_shutdown(dp):
    logging.info("Bot stopped")
