#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Crypto Bot (aiogram v2.x)
- polling / webhook modes
- MEXC prices via ccxt (sync version executed in executor)
- SQLite persistence (users, symbols, alerts, history, logs, settings)
- Inline button UI for all flows
- Background price poller, alert checker
- Chart generation (matplotlib) in threadpool
- Robust logging: errors don't crash the process
"""

import os
import sys
import time
import json
import math
import sqlite3
import logging
import inspect
import traceback
import asyncio
import concurrent.futures
from datetime import datetime, timedelta
from collections import defaultdict, deque
from typing import Optional, List, Dict, Any, Tuple

import requests
import ccxt  # sync ccxt - using run_in_executor to avoid blocking
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
# optional: mplfinance for candlesticks (if installed)
try:
    import mplfinance as mpf
    HAS_MPLFINANCE = True
except Exception:
    HAS_MPLFINANCE = False

from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# ---------------- Config & Logging ----------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("crypto_bot")

# ThreadPool for blocking tasks
EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=6)

# ---------------- Token loading ----------------
def load_token_interactive_or_file() -> Optional[str]:
    """
    Load Telegram token from environment, token.txt or (if interactive) ask user.
    If running in non-interactive environment (Railway), don't prompt.
    """
    token = os.getenv("TELEGRAM_TOKEN")
    if token:
        logger.info("TELEGRAM_TOKEN loaded from environment.")
        return token.strip()
    # try token.txt
    token_path = os.path.join(os.getcwd(), "token.txt")
    if os.path.exists(token_path):
        try:
            with open(token_path, "r", encoding="utf-8") as f:
                t = f.read().strip()
                if t:
                    logger.info("TELEGRAM_TOKEN loaded from token.txt.")
                    return t
        except Exception as e:
            logger.exception("Failed reading token.txt: %s", e)
    # interactive prompt if attached to tty
    if sys.stdin and sys.stdin.isatty():
        try:
            t = input("Введите TELEGRAM_TOKEN (BotFather): ").strip()
            if t:
                # save to token.txt for convenience
                try:
                    with open(token_path, "w", encoding="utf-8") as f:
                        f.write(t)
                        logger.info("Token saved to token.txt")
                except Exception:
                    logger.exception("Failed to save token to token.txt")
                return t
        except Exception as e:
            logger.exception("Interactive token input failed: %s", e)
    # nothing found
    logger.error("TELEGRAM_TOKEN not found. Set TELEGRAM_TOKEN env var or token.txt, or run interactively.")
    return None

TELEGRAM_TOKEN = load_token_interactive_or_file()
if not TELEGRAM_TOKEN:
    # fatal for bot to run; but we avoid raising raw exception to let logs be clean
    logger.critical("Token is missing. Exiting.")
    sys.exit(1)

MODE = os.getenv("MODE", "polling").lower()  # 'polling' or 'webhook'
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()  # required for webhook mode

# ---------------- Globals and DB ----------------
DB_PATH = os.getenv("DB_PATH", "bot_data.sqlite")
PRICE_POLL_INTERVAL = int(os.getenv("PRICE_POLL_INTERVAL", "5"))  # seconds (can be fractional)
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "1000"))

# default symbols
DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "NEAR/USDT"]

# price steps for UI
PRICE_STEPS = [-10000, -5000, -1000, -100, -10, -1, 1, 10, 100, 1000, 5000, 10000]

# create Bot & Dispatcher
bot = Bot(token=TELEGRAM_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)

# connection will be created in async init
_conn: Optional[sqlite3.Connection] = None
_db_lock = asyncio.Lock()

def db_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        # fallback synchronous open (should not happen if init_db called)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return _conn

# ---------------- Database initialization ----------------
def _create_tables_sync():
    """Synchronous table creation executed inside thread."""
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
    conn.close()

async def init_db():
    """Initialize DB (to be awaited at startup)."""
    try:
        logger.info("Initializing DB at %s", DB_PATH)
        await asyncio.get_event_loop().run_in_executor(EXECUTOR, _create_tables_sync)
        # open main connection for fast operations in thread-safe mode
        global _conn
        if _conn is None:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.execute("PRAGMA journal_mode=WAL;")
            _conn.execute("PRAGMA synchronous=NORMAL;")
        logger.info("DB initialized at %s", DB_PATH)
    except Exception:
        logger.exception("init_db failed")

# ---------------- DB helper wrappers (run in executor) ----------------
def _db_execute(query: str, params: tuple = ()):
    """Run SQL synchronously in threadpool."""
    try:
        conn = db_conn()
        cur = conn.cursor()
        cur.execute(query, params)
        conn.commit()
        return cur
    except Exception:
        logger.exception("DB exec error: %s params: %s", query, params)
        raise

async def db_execute(query: str, params: tuple = ()):
    return await asyncio.get_event_loop().run_in_executor(EXECUTOR, _db_execute, query, params)

async def db_fetchall(query: str, params: tuple = ()):
    def _(): 
        cur = _db_execute(query, params)
        return cur.fetchall()
    return await asyncio.get_event_loop().run_in_executor(EXECUTOR, _)

async def db_fetchone(query: str, params: tuple = ()):
    def _():
        cur = _db_execute(query, params)
        return cur.fetchone()
    return await asyncio.get_event_loop().run_in_executor(EXECUTOR, _)

async def log_db(level: str, message: str):
    try:
        await db_execute("INSERT INTO logs (level, message) VALUES (?, ?)", (level.upper(), message))
        getattr(logger, level.lower(), logger.info)(message)
    except Exception:
        logger.exception("log_db failed")

# -------------- Exchange (ccxt) utils --------------
exchange = ccxt.mexc({'enableRateLimit': True})

async def fetch_ticker_async(symbol: str) -> Optional[float]:
    """Fetch ticker via ccxt in threadpool to avoid blocking event loop."""
    loop = asyncio.get_event_loop()
    try:
        def _fetch():
            try:
                t = exchange.fetch_ticker(symbol)
                return float(t.get("last") or t.get("close") or 0.0)
            except Exception as e:
                # if pair not found, return None
                raise e
        price = await loop.run_in_executor(EXECUTOR, _fetch)
        return price
    except Exception as e:
        logger.debug("fetch_ticker_async error for %s: %s", symbol, e)
        return None

async def fetch_ohlcv_async(symbol: str, timeframe: str = "1m", limit: int = 200):
    loop = asyncio.get_event_loop()
    try:
        def _fetch():
            try:
                return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
            except Exception as e:
                raise e
        data = await loop.run_in_executor(EXECUTOR, _fetch)
        return data
    except Exception as e:
        logger.debug("fetch_ohlcv_async error %s %s: %s", symbol, timeframe, e)
        return None

# -------------- In-memory runtime --------------
pending_alerts: Dict[str, dict] = {}
last_prices: Dict[str, float] = {}
history_cache: Dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))

# -------------- Helper: users & symbols --------------
async def add_user(chat_id: int):
    try:
        await db_execute("INSERT OR IGNORE INTO users (chat_id, created_at) VALUES (?, ?)", (str(chat_id), datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
    except Exception:
        logger.exception("add_user error")

async def get_user_symbols(chat_id: int) -> List[str]:
    rows = await db_fetchall("SELECT symbol FROM user_symbols WHERE chat_id=?", (str(chat_id),))
    if rows:
        return [r[0] for r in rows]
    return DEFAULT_SYMBOLS.copy()

async def add_user_symbol(chat_id: int, symbol: str) -> bool:
    try:
        await db_execute("INSERT INTO user_symbols (chat_id, symbol) VALUES (?, ?)", (str(chat_id), symbol))
        return True
    except Exception:
        logger.exception("add_user_symbol error")
        return False

# -------------- Alerts storage --------------
async def save_alert_to_db(chat_id: int, symbol: str, target: float, alert_type: str = "cross", is_recurring: int = 0, active_until: Optional[str] = None, time_start: Optional[str] = None, time_end: Optional[str] = None):
    try:
        await db_execute(
            "INSERT INTO alerts (chat_id, symbol, target, alert_type, is_recurring, active_until, time_start, time_end) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (str(chat_id), symbol, float(target), alert_type, int(is_recurring), active_until, time_start, time_end)
        )
        await log_db("info", f"Alert saved: {chat_id} {symbol} {alert_type} {target}")
        return True
    except Exception:
        logger.exception("save_alert_to_db error")
        return False

async def list_user_alerts(chat_id: int):
    rows = await db_fetchall("SELECT id, symbol, target, alert_type, is_recurring, active_until FROM alerts WHERE chat_id=? ORDER BY id DESC", (str(chat_id),))
    return rows

async def delete_alert(alert_id: int, chat_id: int):
    try:
        await db_execute("DELETE FROM alerts WHERE id=? AND chat_id=?", (int(alert_id), str(chat_id)))
        await log_db("info", f"Alert deleted: {alert_id} by {chat_id}")
        return True
    except Exception:
        logger.exception("delete_alert error")
        return False

async def get_all_alerts():
    rows = await db_fetchall("SELECT id, chat_id, symbol, target, alert_type, is_recurring, active_until, time_start, time_end FROM alerts")
    return rows

# -------------- History saving --------------
async def save_history_point(symbol: str, price: float):
    try:
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        await db_execute("INSERT INTO history (symbol, price, ts) VALUES (?, ?, ?)", (symbol, price, ts))
        history_cache[symbol].append((ts, price))
    except Exception:
        logger.exception("save_history_point error")

# -------------- Charting (in threadpool) --------------
def _build_line_chart_image_sync(symbol: str, points: int = 80, sma_list: List[int] = None, ema_list: List[int] = None, show_rsi: bool = False, show_macd: bool = False):
    try:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        cur = conn.cursor()
        cur.execute("SELECT ts, price FROM history WHERE symbol=? ORDER BY id DESC LIMIT ?", (symbol, points))
        rows = cur.fetchall()
        conn.close()
        if not rows:
            raise ValueError("not enough history")
        rows = rows[::-1]
        times = [datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S") for r in rows]
        prices = [r[1] for r in rows]
        df = pd.DataFrame({"time": times, "price": prices}).set_index("time")
        fig = None
        if show_rsi or show_macd:
            n_sub = 1 + (1 if show_rsi else 0) + (1 if show_macd else 0)
            fig, axes = plt.subplots(n_sub, 1, figsize=(10, 3*n_sub), sharex=True)
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
                # simple RSI
                delta = pd.Series(prices).diff().dropna()
                up = delta.clip(lower=0).rolling(window=14).mean()
                down = (-delta.clip(upper=0)).rolling(window=14).mean()
                rs = up / (down.replace(0, np.nan))
                rsi = 100 - 100 / (1 + rs)
                axes[idx].plot(df.index[1:], rsi, label="RSI")
                axes[idx].axhline(70, linestyle="--")
                axes[idx].axhline(30, linestyle="--")
                axes[idx].set_ylim(0, 100)
                idx += 1
            if show_macd:
                s = pd.Series(prices)
                ema_fast = s.ewm(span=12, adjust=False).mean()
                ema_slow = s.ewm(span=26, adjust=False).mean()
                macd_line = ema_fast - ema_slow
                signal_line = macd_line.ewm(span=9, adjust=False).mean()
                axes[idx].plot(df.index, macd_line, label="MACD")
                axes[idx].plot(df.index, signal_line, label="Signal")
                axes[idx].legend()
        else:
            fig, ax = plt.subplots(figsize=(10, 4))
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
        import io
        buf = io.BytesIO()
        plt.tight_layout()
        fig.savefig(buf, format="png", dpi=150)
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        logger.exception("chart generation sync error")
        raise

def _build_candlestick_chart_sync(symbol: str, timeframe: str = "1m", points: int = 200):
    """
    Build candlestick chart using OHLCV via ccxt (synchronous). Returns bytes.
    Uses mplfinance if available, else draws simplified candlesticks.
    """
    try:
        # fetch OHLCV sync (we'll use global exchange - blocking)
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=points)
        if not ohlcv:
            raise ValueError("no ohlcv")
        # ohlcv: [ [ts, open, high, low, close, volume], ... ]
        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit='ms')
        df = df.set_index("ts")
        if HAS_MPLFINANCE:
            import io
            buf = io.BytesIO()
            mpf.plot(df, type='candle', style='charles', volume=True, savefig=buf, tight_layout=True)
            buf.seek(0)
            return buf.read()
        else:
            # fallback simple candles
            import matplotlib.dates as mdates
            import io
            fig, ax = plt.subplots(figsize=(10, 5))
            ax.plot(df.index, df["close"], label="Close")
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d %H:%M'))
            ax.grid(True)
            plt.xticks(rotation=30)
            buf = io.BytesIO()
            plt.tight_layout()
            fig.savefig(buf, format="png", dpi=150)
            plt.close(fig)
            buf.seek(0)
            return buf.read()
    except Exception:
        logger.exception("candlestick generation error")
        raise

async def build_line_chart_image(symbol: str, points: int = 80, sma_list=None, ema_list=None, show_rsi=False, show_macd=False):
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(EXECUTOR, _build_line_chart_image_sync, symbol, points, sma_list, ema_list, show_rsi, show_macd)
        return data
    except Exception as e:
        logger.exception("build_line_chart_image failed for %s", symbol)
        return None

async def build_candlestick_chart(symbol: str, timeframe: str = "1m", points: int = 200):
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(EXECUTOR, _build_candlestick_chart_sync, symbol, timeframe, points)
        return data
    except Exception:
        logger.exception("build_candlestick_chart failed for %s %s", symbol, timeframe)
        return None

# -------------- Alert checking --------------
def _is_within_time_window(time_start: Optional[str], time_end: Optional[str]) -> bool:
    if not time_start or not time_end:
        return True
    now = datetime.utcnow().strftime("%H:%M")
    return time_start <= now <= time_end

async def check_alerts():
    try:
        alerts = await get_all_alerts()
        for alert in alerts:
            alert_id, chat_id, symbol, target, alert_type, is_recurring, active_until, time_start, time_end = alert
            # expiration
            if active_until:
                try:
                    ru = datetime.strptime(active_until, "%Y-%m-%d %H:%M:%S")
                    if datetime.utcnow() > ru:
                        await db_execute("DELETE FROM alerts WHERE id=?", (alert_id,))
                        continue
                except Exception:
                    pass
            if time_start and time_end:
                if not _is_within_time_window(time_start, time_end):
                    continue
            cur_price = last_prices.get(symbol)
            if cur_price is None:
                continue
            prev_price = None
            hist = history_cache.get(symbol, [])
            if len(hist) >= 2:
                prev_price = hist[-2][1]
            triggered = False
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
            if triggered:
                try:
                    text = f"🔔 Alert сработал: {symbol} {alert_type} {target}$ (текущая {cur_price}$)"
                    await bot.send_message(chat_id, text)
                    await log_db("info", f"Alert fired for {chat_id} {symbol} {alert_type} {target}")
                except Exception:
                    logger.exception("Failed to send alert to %s", chat_id)
                if not is_recurring:
                    await db_execute("DELETE FROM alerts WHERE id=?", (alert_id,))
    except Exception:
        logger.exception("check_alerts error")

# -------------- Price poller --------------
async def price_polling_worker(stop_event: asyncio.Event):
    """
    Background worker that fetches prices for all symbols of interest and saves history.
    stop_event used to gracefully stop the loop.
    """
    logger.info("Price polling worker started, interval %s sec", PRICE_POLL_INTERVAL)
    while not stop_event.is_set():
        try:
            # determine full symbol set
            rows = await db_fetchall("SELECT DISTINCT symbol FROM user_symbols")
            symbols = set(DEFAULT_SYMBOLS)
            for r in rows:
                symbols.add(r[0])
            # fetch each ticker
            coros = [fetch_ticker_async(sym) for sym in symbols]
            results = await asyncio.gather(*coros, return_exceptions=True)
            for sym, res in zip(list(symbols), results):
                try:
                    if isinstance(res, Exception):
                        logger.debug("Error fetching %s: %s", sym, res)
                        continue
                    price = float(res) if res is not None else None
                    if price is not None and price > 0:
                        last_prices[sym] = price
                        await save_history_point(sym, price)
                        # keep history_cache updated
                        history_cache[sym].append((datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), price))
                except Exception:
                    logger.exception("price polling per-symbol error %s", sym)
            # check alerts
            await check_alerts()
        except Exception:
            logger.exception("price_polling_worker loop error")
        # wait
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=PRICE_POLL_INTERVAL)
        except asyncio.TimeoutError:
            continue
    logger.info("Price polling worker stopped")

# -------------- UI helpers (keyboards) --------------
def main_menu_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("💰 Цена (все)", callback_data="price_all"),
        InlineKeyboardButton("📊 График", callback_data="chart_menu")
    )
    kb.add(
        InlineKeyboardButton("🔔 Добавить Alert", callback_data="add_alert"),
        InlineKeyboardButton("📋 Мои Alerts", callback_data="my_alerts")
    )
    kb.add(
        InlineKeyboardButton("📈 Авто-сигналы", callback_data="signals_menu"),
        InlineKeyboardButton("⚙️ Мои монеты", callback_data="my_symbols")
    )
    kb.add(InlineKeyboardButton("📜 История", callback_data="history_menu"))
    return kb

def symbol_list_kb(symbols: List[str], prefix: str):
    kb = InlineKeyboardMarkup(row_width=3)
    for s in symbols:
        label = s.split("/")[0]
        kb.insert(InlineKeyboardButton(label, callback_data=f"{prefix}|{label}"))
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="back_main"))
    return kb

def price_steps_kb(include_autotime: bool = True):
    kb = InlineKeyboardMarkup(row_width=6)
    # negative steps (display plain)
    for s in PRICE_STEPS[:6]:
        kb.insert(InlineKeyboardButton(str(int(s)), callback_data=f"step|{int(s)}"))
    # positive
    for s in PRICE_STEPS[6:]:
        kb.insert(InlineKeyboardButton(f"+{int(s)}", callback_data=f"step|{int(s)}"))
    last_row = []
    if include_autotime:
        last_row.append(InlineKeyboardButton("⏱ Автоподтв.", callback_data="auto_time"))
    last_row.append(InlineKeyboardButton("✅ Готово", callback_data="price_confirm"))
    for b in last_row:
        kb.add(b)
    return kb

def autotime_kb():
    kb = InlineKeyboardMarkup(row_width=3)
    kb.add(
        InlineKeyboardButton("5 сек", callback_data="autotime|5"),
        InlineKeyboardButton("10 сек", callback_data="autotime|10"),
        InlineKeyboardButton("30 сек", callback_data="autotime|30")
    )
    kb.add(InlineKeyboardButton("Отключить", callback_data="autotime|0"))
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="back_price"))
    return kb

def alerts_list_kb(alerts: List[tuple]):
    kb = InlineKeyboardMarkup(row_width=1)
    for a in alerts:
        aid, sym, targ, atype, rec, a_until = a
        label = ( "🔂" if rec else "☑️") + f" {sym.split('/')[0]} {atype} {targ}"
        kb.add(InlineKeyboardButton(label, callback_data=f"del_alert|{aid}"))
    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="back_main"))
    return kb

# -------------- Command & Callback handlers --------------
@dp.message_handler(commands=["start", "help"])
async def cmd_start(message: types.Message):
    try:
        await add_user(message.chat.id)
        send_text = ("👋 Привет! Я крипто-бот.\n\n"
                     "Управление через кнопки ниже. Нажми меню.")
        await bot.send_message(message.chat.id, send_text, reply_markup=main_menu_kb())
    except Exception:
        logger.exception("cmd_start failed")

@dp.callback_query_handler(lambda c: True)
async def all_callbacks(cb: types.CallbackQuery):
    data = cb.data or ""
    chat_id = cb.from_user.id
    try:
        await add_user(chat_id)
        await log_db("info", f"callback: {data} from {chat_id}")
        # navigation
        if data == "back_main":
            await bot.send_message(chat_id, "Главное меню:", reply_markup=main_menu_kb())
            await cb.answer()
            return
        if data == "price_all":
            # send all prices immediately
            # gather user's symbols
            syms = await get_user_symbols(chat_id)
            lines = []
            for s in syms:
                price = last_prices.get(s)
                if price is None:
                    lines.append(f"{s}: n/a")
                else:
                    lines.append(f"{s}: {price}$")
            await bot.send_message(chat_id, "📡 Текущие цены:\n" + "\n".join(lines))
            await cb.answer()
            return
        if data == "chart_menu":
            # ask user to choose symbol and timeframe
            syms = await get_user_symbols(chat_id)
            kb = symbol_list_kb(syms, "chart_select")
            await bot.send_message(chat_id, "Выберите монету для графика:", reply_markup=kb)
            await cb.answer()
            return
        if data.startswith("chart_select|"):
            label = data.split("|", 1)[1]
            symbol = label + "/USDT"
            # ask timeframe
            kb = InlineKeyboardMarkup(row_width=3)
            tf_list = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
            for tf in tf_list:
                kb.insert(InlineKeyboardButton(tf, callback_data=f"chart_build|{symbol}|{tf}"))
            kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="chart_menu"))
            await bot.send_message(chat_id, f"Выбрана {symbol}. Выберите таймфрейм:", reply_markup=kb)
            await cb.answer()
            return
        if data.startswith("chart_build|"):
            _, symbol, tf = data.split("|", 2)
            await cb.answer("Генерирую график... Подождите")
            # build candlestick chart (could be heavy)
            chart_bytes = await build_candlestick_chart(symbol, timeframe=tf, points=200)
            if chart_bytes:
                await bot.send_photo(chat_id, ('chart.png', chart_bytes), caption=f"{symbol} {tf} свечи")
            else:
                await bot.send_message(chat_id, "Не удалось сгенерировать график.")
            return
        if data == "add_alert":
            syms = await get_user_symbols(chat_id)
            kb = symbol_list_kb(syms, "add_alert_select")
            await bot.send_message(chat_id, "Выберите монету для Alert:", reply_markup=kb)
            await cb.answer()
            return
        if data.startswith("add_alert_select|"):
            label = data.split("|",1)[1]
            symbol = label + "/USDT"
            # initialize pending alert state
            pending_alerts[str(chat_id)] = {
                "coin": label,
                "symbol": symbol,
                "price": 0.0,
                "msg_id": None,
                "last_step": None,
                "last_time": time.time(),
                "multiplier": 1,
                "autotime": 10,
                "awaiting_type": False
            }
            # fetch base price
            base_price = await fetch_ticker_async(symbol) or 0.0
            pending_alerts[str(chat_id)]["price"] = base_price
            kb = price_steps_kb(include_autotime=True)
            resp = await bot.send_message(chat_id, f"{label}\nБазовая цена: {base_price}$\nНастройте цену кнопками:", reply_markup=kb)
            pending_alerts[str(chat_id)]["msg_id"] = resp.message_id
            # start auto-confirm task (just mark time - actual timer uses asyncio.create_task)
            pending_alerts[str(chat_id)]["last_time"] = time.time()
            # start autoconfirm watcher
            asyncio.create_task(_autoconfirm_watcher(chat_id))
            await cb.answer()
            return
        if data.startswith("step|"):
            step_val = int(data.split("|",1)[1])
            key = str(chat_id)
            if key in pending_alerts:
                alert = pending_alerts[key]
                now = time.time()
                if alert.get("last_step") == step_val and now - alert.get("last_time",0) < 3:
                    alert["multiplier"] = alert.get("multiplier",1) + 1
                else:
                    alert["multiplier"] = 1
                final_step = step_val * alert["multiplier"]
                alert["price"] = max(0, alert.get("price",0) + final_step)
                alert["last_step"] = step_val
                alert["last_time"] = now
                # update message
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=alert["msg_id"],
                                                text=f"📊 {alert['coin']}\nТекущая цена: {alert['price']}$\n(шаг {step_val}$ ×{alert['multiplier']} = {final_step}$)",
                                                reply_markup=price_steps_kb(include_autotime=True))
                except Exception:
                    logger.debug("Failed edit message for price step")
            await cb.answer()
            return
        if data == "auto_time":
            key = str(chat_id)
            if key in pending_alerts:
                alert = pending_alerts[key]
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=alert["msg_id"],
                                                text=f"⏱️ Сейчас: {'выкл' if alert.get('autotime',0)==0 else str(alert.get('autotime'))+' сек'}\nВыберите новое:",
                                                reply_markup=autotime_kb())
                except Exception:
                    logger.debug("auto_time edit failed")
            await cb.answer()
            return
        if data.startswith("autotime|"):
            val = int(data.split("|",1)[1])
            key = str(chat_id)
            if key in pending_alerts:
                pending_alerts[key]["autotime"] = val
                alert = pending_alerts[key]
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=alert["msg_id"],
                                                text=f"📊 {alert['coin']}\nТекущая цена: {alert['price']}$\n⏱ Автоподтв.: {'выкл' if val==0 else str(val)+' сек'}",
                                                reply_markup=price_steps_kb(include_autotime=True))
                except Exception:
                    logger.debug("autotime edit failed")
            await cb.answer()
            return
        if data == "price_confirm":
            key = str(chat_id)
            if key in pending_alerts:
                alert = pending_alerts[key]
                try:
                    await bot.edit_message_text(chat_id=chat_id, message_id=alert["msg_id"],
                                                text=f"✅ Вы выбрали {alert['coin']} при цене {alert['price']}$.\nТеперь выберите тип сигнала:")
                except Exception:
                    logger.debug("price_confirm edit failed")
                kb = InlineKeyboardMarkup(row_width=3)
                kb.add(InlineKeyboardButton("📈 Выше", callback_data="type|above"),
                       InlineKeyboardButton("📉 Ниже", callback_data="type|below"),
                       InlineKeyboardButton("🔄 Пересечение", callback_data="type|cross"))
                await bot.send_message(chat_id, "Выберите тип сигнала:", reply_markup=kb)
                alert["awaiting_type"] = True
            await cb.answer()
            return
        if data.startswith("type|"):
            typ = data.split("|",1)[1]
            key = str(chat_id)
            if key in pending_alerts and pending_alerts[key].get("awaiting_type"):
                pending_alerts[key]["type_selected"] = typ
                kb = InlineKeyboardMarkup(row_width=2)
                kb.add(InlineKeyboardButton("☑️ Одноразовый", callback_data="rec|no"),
                       InlineKeyboardButton("🔂 Постоянный", callback_data="rec|yes"))
                await bot.send_message(chat_id, "Одноразовый или постоянный?", reply_markup=kb)
            await cb.answer()
            return
        if data.startswith("rec|"):
            rec = 1 if data.split("|",1)[1] == "yes" else 0
            key = str(chat_id)
            if key in pending_alerts:
                pending_alerts[key]["recurring"] = rec
                kb = InlineKeyboardMarkup(row_width=2)
                kb.add(InlineKeyboardButton("⏰ Только сегодня", callback_data="time|today"),
                       InlineKeyboardButton("📅 Задать часы", callback_data="time|custom"))
                kb.add(InlineKeyboardButton("♾️ Без огранич.", callback_data="time|none"))
                await bot.send_message(chat_id, "Ограничить сигнал по времени?", reply_markup=kb)
            await cb.answer()
            return
        if data == "time|today":
            key = str(chat_id)
            if key in pending_alerts:
                pending_alerts[key]["active_until"] = (datetime.utcnow().replace(hour=23, minute=59, second=59)).strftime("%Y-%m-%d %H:%M:%S")
                a = pending_alerts[key]
                await save_alert_to_db(chat_id, f"{a['coin']}/USDT", a['price'], a.get("type_selected","cross"), a.get("recurring",0), a.get("active_until"))
                await bot.send_message(chat_id, f"✅ Alert добавлен: {a['coin']}/USDT {a.get('type_selected','cross')} {a['price']}$ (до конца дня)")
                del pending_alerts[key]
            await cb.answer()
            return
        if data == "time|none":
            key = str(chat_id)
            if key in pending_alerts:
                a = pending_alerts[key]
                await save_alert_to_db(chat_id, f"{a['coin']}/USDT", a['price'], a.get("type_selected","cross"), a.get("recurring",0), None)
                await bot.send_message(chat_id, f"✅ Alert добавлен: {a['coin']}/USDT {a.get('type_selected','cross')} {a['price']}$ (без ограничений)")
                del pending_alerts[key]
            await cb.answer()
            return
        if data == "time|custom":
            key = str(chat_id)
            if key in pending_alerts:
                pending_alerts[key]["awaiting_time"] = True
                await bot.send_message(chat_id, "Введите срок действия в часах (например: 24):")
            await cb.answer()
            return
        if data == "my_alerts":
            rows = await list_user_alerts(chat_id)
            if not rows:
                await bot.send_message(chat_id, "У вас пока нет активных Alerts.", reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("⬅️ Назад", callback_data="back_main")))
            else:
                kb = alerts_list_kb(rows)
                await bot.send_message(chat_id, "Ваши Alerts (нажмите чтобы удалить):", reply_markup=kb)
            await cb.answer()
            return
        if data.startswith("del_alert|"):
            aid = int(data.split("|",1)[1])
            await delete_alert(aid, chat_id)
            await bot.send_message(chat_id, "✅ Alert удалён.")
            await cb.answer()
            return
        if data == "my_symbols":
            syms = await get_user_symbols(chat_id)
            lines = ["Ваши монеты:"]
            lines.extend(syms)
            kb = InlineKeyboardMarkup().add(InlineKeyboardButton("➕ Добавить монету", callback_data="add_symbol")).add(InlineKeyboardButton("⬅️ Назад", callback_data="back_main"))
            await bot.send_message(chat_id, "\n".join(lines), reply_markup=kb)
            await cb.answer()
            return
        if data == "add_symbol":
            pending_alerts[str(chat_id)] = {"awaiting_new_symbol": True}
            await bot.send_message(chat_id, "Введите символ монеты (например: ADA или ADA/USDT).")
            await cb.answer()
            return
        # fallback
        await cb.answer("Неизвестная кнопка")
    except Exception:
        logger.exception("callback handler error for %s", data)
        try:
            await cb.answer("Ошибка обработки. Посмотрите логи.")
        except Exception:
            pass

@dp.message_handler()
async def all_text_handler(message: types.Message):
    chat_id = message.chat.id
    text = (message.text or "").strip()
    try:
        await add_user(chat_id)
        await log_db("info", f"msg from {chat_id}: {text}")
        key = str(chat_id)
        # awaiting new symbol
        if key in pending_alerts and pending_alerts[key].get("awaiting_new_symbol"):
            symbol = text.upper().strip()
            if "/" not in symbol:
                symbol = f"{symbol}/USDT"
            try:
                markets = await asyncio.get_event_loop().run_in_executor(EXECUTOR, exchange.load_markets)
                if symbol in markets:
                    await add_user_symbol(chat_id, symbol)
                    await bot.send_message(chat_id, f"✅ Монета {symbol} добавлена в ваш список.")
                else:
                    await bot.send_message(chat_id, f"❌ Пара {symbol} не найдена на MEXC.")
            except Exception:
                logger.exception("Error checking symbol")
                await bot.send_message(chat_id, f"Ошибка при проверке {symbol}.")
            del pending_alerts[key]
            await bot.send_message(chat_id, "Меню:", reply_markup=main_menu_kb())
            return
        # awaiting custom time hours for alert
        if key in pending_alerts and pending_alerts[key].get("awaiting_time"):
            try:
                hours = int(text.strip())
                active_until = (datetime.utcnow() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
                a = pending_alerts[key]
                a["active_until"] = active_until
                await save_alert_to_db(chat_id, f"{a['coin']}/USDT", a['price'], a.get("type_selected","cross"), a.get("recurring",0), a.get("active_until"))
                await bot.send_message(chat_id, f"✅ Alert добавлен: {a['coin']}/USDT {a.get('type_selected','cross')} {a['price']}$ (до {active_until})")
                del pending_alerts[key]
            except Exception:
                logger.exception("awaiting_time error")
                await bot.send_message(chat_id, "Ошибка: введите целое число часов (например: 24).")
            await bot.send_message(chat_id, "Меню:", reply_markup=main_menu_kb())
            return
        # commands fallback
        if text.startswith("/price"):
            parts = text.split()
            if len(parts) == 2:
                s = parts[1].upper()
                if "/" not in s:
                    s = s + "/USDT"
                price = last_prices.get(s)
                if price is not None:
                    await bot.send_message(chat_id, f"💰 {s}: {price}$", reply_markup=main_menu_kb())
                else:
                    await bot.send_message(chat_id, f"Нет данных для {s}.", reply_markup=main_menu_kb())
            else:
                await bot.send_message(chat_id, "Использование: /price SYMBOL", reply_markup=main_menu_kb())
            return
        if text.startswith("/chart"):
            parts = text.split()
            if len(parts) >= 2:
                s = parts[1].upper()
                if "/" not in s:
                    s = s + "/USDT"
                buf = await build_line_chart_image(s, points=80, sma_list=[5,20], ema_list=[10], show_rsi=False, show_macd=False)
                if buf:
                    await bot.send_photo(chat_id, ('chart.png', buf), caption=f"{s} — график", reply_markup=main_menu_kb())
                else:
                    await bot.send_message(chat_id, "Недостаточно данных для графика.", reply_markup=main_menu_kb())
            else:
                await bot.send_message(chat_id, "Использование: /chart SYMBOL", reply_markup=main_menu_kb())
            return
        if text.startswith("/alerts"):
            rows = await list_user_alerts(chat_id)
            if not rows:
                await bot.send_message(chat_id, "У вас нет Alerts.", reply_markup=main_menu_kb())
            else:
                lines = []
                for r in rows:
                    aid, sym, targ, atype, rec, a_until = r
                    lines.append(f"{aid}: {sym} {atype} {targ}$ {'🔂' if rec else '☑️'}")
                await bot.send_message(chat_id, "\n".join(lines), reply_markup=main_menu_kb())
            return
        # default show menu
        await bot.send_message(chat_id, "Выберите действие:", reply_markup=main_menu_kb())
    except Exception:
        logger.exception("all_text_handler error")
        try:
            await bot.send_message(chat_id, "Произошла ошибка. Посмотрите логи.")
        except Exception:
            pass

# -------------- Autoconfirm watcher for pending alerts --------------
async def _autoconfirm_watcher(chat_id: int):
    """
    Watches pending_alerts[chat_id] and performs auto-confirm if user doesn't interact.
    """
    try:
        key = str(chat_id)
        # wait until autotime elapsed from last_time
        while key in pending_alerts:
            alert = pending_alerts[key]
            autotime = alert.get("autotime", 10)
            if autotime == 0:
                return
            last = alert.get("last_time", time.time())
            wait = max(0, autotime - (time.time() - last))
            await asyncio.sleep(wait)
            if key not in pending_alerts:
                return
            alert2 = pending_alerts[key]
            if time.time() - alert2.get("last_time", 0) >= autotime:
                # auto-confirm: ask for type
                try:
                    mid = alert2.get("msg_id")
                    if mid:
                        try:
                            await bot.edit_message_text(chat_id=chat_id, message_id=mid, text=f"✅ Автоподтверждение: {alert2.get('coin')} при цене {alert2.get('price')}$.\nВыберите тип сигнала:")
                        except Exception:
                            pass
                    kb = InlineKeyboardMarkup(row_width=3)
                    kb.add(InlineKeyboardButton("📈 Выше", callback_data="type|above"),
                           InlineKeyboardButton("📉 Ниже", callback_data="type|below"),
                           InlineKeyboardButton("🔄 Пересечение", callback_data="type|cross"))
                    await bot.send_message(chat_id, "Какой тип сигнала поставить?", reply_markup=kb)
                    alert2["awaiting_type"] = True
                except Exception:
                    logger.exception("auto confirm failed")
                return
    except Exception:
        logger.exception("_autoconfirm_watcher error")

# -------------- Startup & Shutdown --------------
_stop_event = asyncio.Event()
_price_task: Optional[asyncio.Task] = None

async def on_startup(dp):
    try:
        await init_db()
        # start price poller background task
        global _price_task, _stop_event
        _stop_event = asyncio.Event()
        _price_task = asyncio.create_task(price_polling_worker(_stop_event))
        logger.info("Started background price task")
        # optionally set webhook if MODE=webhook
        if MODE == "webhook":
            if not WEBHOOK_URL:
                logger.error("WEBHOOK_URL not set but running in webhook mode.")
            else:
                # aiogram's set_webhook is sync method (called via bot)
                try:
                    await bot.delete_webhook()
                    await bot.set_webhook(WEBHOOK_URL + "/" + TELEGRAM_TOKEN)
                    logger.info("Webhook set to %s", WEBHOOK_URL + "/" + TELEGRAM_TOKEN)
                except Exception:
                    logger.exception("Failed to set webhook")
    except Exception:
        logger.exception("on_startup error")

async def on_shutdown(dp):
    try:
        logger.info("Shutting down: stopping price task")
        global _price_task, _stop_event
        if _stop_event and not _stop_event.is_set():
            _stop_event.set()
        if _price_task:
            await asyncio.wait_for(_price_task, timeout=5)
        # close db
        try:
            if _conn:
                _conn.close()
                logger.info("Closed DB connection")
        except Exception:
            logger.exception("Closing DB connection failed")
        await bot.close()
    except Exception:
        logger.exception("on_shutdown error")

# -------------- Main entry --------------
def main():
    # choose to run polling or webhook
    if MODE == "webhook":
        # prepare webhook via executor.start_webhook
        # Note: aiogram v2's executor.start_webhook expects certain args.
        # To simplify: use polling as default; for webhook you must set WEBHOOK_URL env var.
        logger.info("Running in webhook mode. Be sure WEBHOOK_URL is set: %s", WEBHOOK_URL)
        from aiogram.utils.executor import start_webhook
        WEBAPP_HOST = os.getenv("WEBAPP_HOST", "0.0.0.0")
        WEBAPP_PORT = int(os.getenv("PORT", "8000"))
        WEBHOOK_PATH = f"/{TELEGRAM_TOKEN}"
        start_webhook(
            dispatcher=dp,
            webhook_path=WEBHOOK_PATH,
            on_startup=on_startup,
            on_shutdown=on_shutdown,
            skip_updates=True,
            host=WEBAPP_HOST,
            port=WEBAPP_PORT,
        )
    else:
        # polling
        logger.info("Running in polling mode")
        executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)

if __name__ == "__main__":
    main()
