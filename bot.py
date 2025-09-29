#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Crypto Bot (personal autosignals per user)

Features:
- aiogram v2.25.1 based
- async ccxt (MEXC) price fetch
- aiosqlite persistence
- inline keyboard UI for user flows
- personal autosignals using indicators (MA, RSI, MACD, Bollinger)
- price alerts (absolute $ or percent)
- robust logging, exceptions don't crash bot (logged)
- token from env TELEGRAM_TOKEN or token.txt fallback
"""

import os
import asyncio
import logging
import json
import math
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

import aiosqlite
import ccxt.async_support as ccxt_async
import pandas as pd
import numpy as np

from aiogram import Bot, Dispatcher, executor, types

# ---------------- Config ----------------
# Token: priority -> env TELEGRAM_TOKEN -> token.txt file -> raise
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    # try token.txt in current dir (useful for local dev only)
    try:
        with open("token.txt", "r", encoding="utf-8") as f:
            TELEGRAM_TOKEN = f.read().strip()
            logging.getLogger("crypto_bot").info("Loaded TELEGRAM_TOKEN from token.txt")
    except Exception:
        TELEGRAM_TOKEN = None

if not TELEGRAM_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN not set (set env var TELEGRAM_TOKEN or create token.txt)")

DATABASE = os.getenv("DB_PATH", "bot_data.sqlite")
PRICE_POLL_INTERVAL = int(os.getenv("PRICE_POLL_INTERVAL", "5"))  # seconds; can tune
EXCHANGE_ID = os.getenv("EXCHANGE", "mexc")
DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "NEAR/USDT"]

# Which timeframes to support (for autosignals)
SUPPORTED_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("crypto_bot")

# ---------------- Globals ----------------
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(bot)

# Exchange client (async)
exchange = ccxt_async.mexc({"enableRateLimit": True})

# DB handle
DB: Optional[aiosqlite.Connection] = None

# In-memory caches (for speed)
last_prices: Dict[str, float] = {}
ohlc_cache: Dict[str, Dict[str, List]] = {}  # symbol -> timeframe -> list of candles
# We'll store minimal candles in DB as well; but keep cache for analysis speed

# ---------------- Utilities / Indicators ----------------
def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=1).mean()

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()

def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ma_up = up.rolling(window=period, min_periods=1).mean()
    ma_down = down.rolling(window=period, min_periods=1).mean()
    rs = ma_up / (ma_down.replace(0, np.nan))
    rsi_series = 100 - (100 / (1 + rs))
    rsi_series = rsi_series.fillna(50)  # neutral for initial values
    return rsi_series

def macd(series: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist

def bollinger_bands(series: pd.Series, period=20, stds=2):
    ma = series.rolling(window=period, min_periods=1).mean()
    std = series.rolling(window=period, min_periods=1).std().fillna(0)
    upper = ma + stds * std
    lower = ma - stds * std
    return upper, lower

# ---------------- DB helpers ----------------
async def init_db():
    global DB
    DB = await aiosqlite.connect(DATABASE)
    await DB.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id TEXT PRIMARY KEY,
        created_at TEXT
    )""")
    await DB.execute("""
    CREATE TABLE IF NOT EXISTS user_symbols (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        symbol TEXT
    )""")
    await DB.execute("""
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        symbol TEXT,
        mode TEXT,          -- 'absolute' or 'percent' or 'change'
        value REAL,
        alert_type TEXT,    -- 'one-shot' or 'recurring'
        active INTEGER DEFAULT 1
    )""")
    await DB.execute("""
    CREATE TABLE IF NOT EXISTS autosignals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        symbol TEXT,
        timeframe TEXT,
        indicators TEXT,  -- json list of indicators enabled
        active INTEGER DEFAULT 1
    )""")
    await DB.execute("""
    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT,
        price REAL,
        ts TEXT
    )""")
    await DB.commit()
    logger.info("DB initialized at %s", DATABASE)

async def add_user_if_missing(chat_id: int):
    try:
        cur = await DB.execute("SELECT chat_id FROM users WHERE chat_id=?", (str(chat_id),))
        row = await cur.fetchone()
        if not row:
            await DB.execute("INSERT INTO users (chat_id, created_at) VALUES (?, ?)", (str(chat_id), datetime.utcnow().isoformat()))
            await DB.commit()
            logger.info("Added new user %s", chat_id)
    except Exception:
        logger.exception("add_user_if_missing error")

async def get_user_symbols(chat_id: int) -> List[str]:
    try:
        cur = await DB.execute("SELECT symbol FROM user_symbols WHERE chat_id=?", (str(chat_id),))
        rows = await cur.fetchall()
        if rows:
            return [r[0] for r in rows]
        else:
            return DEFAULT_SYMBOLS.copy()
    except Exception:
        logger.exception("get_user_symbols")
        return DEFAULT_SYMBOLS.copy()

async def add_user_symbol(chat_id: int, symbol: str) -> bool:
    try:
        await DB.execute("INSERT INTO user_symbols (chat_id, symbol) VALUES (?, ?)", (str(chat_id), symbol))
        await DB.commit()
        logger.info("User %s added symbol %s", chat_id, symbol)
        return True
    except Exception:
        logger.exception("add_user_symbol")
        return False

async def save_alert(chat_id: int, symbol: str, mode: str, value: float, alert_type: str = "one-shot"):
    try:
        await DB.execute("INSERT INTO alerts (chat_id, symbol, mode, value, alert_type, active) VALUES (?, ?, ?, ?, ?, ?)",
                         (str(chat_id), symbol, mode, float(value), alert_type, 1))
        await DB.commit()
        logger.info("Saved alert for %s: %s %s %s", chat_id, symbol, mode, value)
        return True
    except Exception:
        logger.exception("save_alert")
        return False

async def list_alerts(chat_id: int):
    try:
        cur = await DB.execute("SELECT id, symbol, mode, value, alert_type, active FROM alerts WHERE chat_id=? ORDER BY id DESC", (str(chat_id),))
        rows = await cur.fetchall()
        return rows
    except Exception:
        logger.exception("list_alerts")
        return []

async def remove_alert(chat_id: int, alert_id: int):
    try:
        await DB.execute("DELETE FROM alerts WHERE id=? AND chat_id=?", (int(alert_id), str(chat_id)))
        await DB.commit()
        logger.info("Removed alert %s for %s", alert_id, chat_id)
    except Exception:
        logger.exception("remove_alert")

async def save_autosignal(chat_id: int, symbol: str, timeframe: str, indicators: List[str]):
    try:
        await DB.execute("INSERT INTO autosignals (chat_id, symbol, timeframe, indicators, active) VALUES (?, ?, ?, ?, ?)",
                         (str(chat_id), symbol, timeframe, json.dumps(indicators), 1))
        await DB.commit()
        logger.info("Saved autosignal: %s %s %s", chat_id, symbol, timeframe)
        return True
    except Exception:
        logger.exception("save_autosignal")
        return False

async def list_autosignals(chat_id: int):
    try:
        cur = await DB.execute("SELECT id, symbol, timeframe, indicators, active FROM autosignals WHERE chat_id=? ORDER BY id DESC", (str(chat_id),))
        rows = await cur.fetchall()
        return rows
    except Exception:
        logger.exception("list_autosignals")
        return []

async def remove_autosignal(chat_id: int, aid: int):
    try:
        await DB.execute("DELETE FROM autosignals WHERE id=? AND chat_id=?", (int(aid), str(chat_id)))
        await DB.commit()
        logger.info("Removed autosignal %s for %s", aid, chat_id)
    except Exception:
        logger.exception("remove_autosignal")

# ---------------- Exchange helpers ----------------
async def fetch_price(symbol: str) -> Optional[float]:
    """Fetch latest ticker price from exchange (async). Returns None on error."""
    try:
        # ccxt symbol must be as in markets
        ticker = await exchange.fetch_ticker(symbol)
        price = ticker.get("last") or ticker.get("close") or ticker.get("info", {}).get("lastPrice")
        if price is None:
            return None
        price = float(price)
        last_prices[symbol] = price
        # save into history DB
        try:
            await DB.execute("INSERT INTO history (symbol, price, ts) VALUES (?, ?, ?)", (symbol, price, datetime.utcnow().isoformat()))
            await DB.commit()
        except Exception:
            logger.exception("failed to save history")
        return price
    except Exception:
        logger.exception("fetch_price error for %s", symbol)
        return None

async def fetch_ohlcv(symbol: str, timeframe: str = "1m", limit: int = 200):
    """Fetch OHLCV candle data from exchange using ccxt (async)."""
    try:
        # ccxt uses timeframe strings like '1m','5m','1h' etc.
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        # convert to pandas DataFrame
        df = pd.DataFrame(ohlcv, columns=["ts", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["ts"], unit="ms")
        return df
    except Exception:
        logger.exception("fetch_ohlcv error %s %s", symbol, timeframe)
        return None

# ---------------- Signal generation ----------------
async def analyze_for_signals_for_user(chat_id: int, autosig_row):
    """
    autosig_row: (id, symbol, timeframe, indicators_json, active)
    Evaluate current market and indicators and decide whether to send a signal to user.
    Simple logic:
      - compute MA cross, RSI thresholds, MACD cross
      - if majority (>=1) of enabled indicators say 'long'/'short', send signal
    """
    try:
        aid, symbol, timeframe, indicators_json, active = autosig_row
        indicators = json.loads(indicators_json or "[]")
        df = await fetch_ohlcv(symbol, timeframe=timeframe, limit=100)
        if df is None or df.empty:
            logger.debug("No OHLVC for %s %s", symbol, timeframe)
            return

        close = df["close"]
        votes = {"long": 0, "short": 0}

        # MA cross example: sma(50) vs sma(200)
        if "ma" in indicators:
            s50 = sma(close, 50).iloc[-1]
            s200 = sma(close, 200).iloc[-1] if len(close) >= 200 else sma(close, 200).iloc[-1]
            if np.isnan(s50) or np.isnan(s200):
                pass
            else:
                if s50 > s200:
                    votes["long"] += 1
                elif s50 < s200:
                    votes["short"] += 1

        # RSI example
        if "rsi" in indicators:
            rsi_series = rsi(close, 14)
            last_rsi = float(rsi_series.iloc[-1])
            if last_rsi < 30:
                votes["long"] += 1
            elif last_rsi > 70:
                votes["short"] += 1

        # MACD
        if "macd" in indicators:
            macd_line, signal_line, hist = macd(close)
            if macd_line.iloc[-1] > signal_line.iloc[-1]:
                votes["long"] += 1
            elif macd_line.iloc[-1] < signal_line.iloc[-1]:
                votes["short"] += 1

        # Bollinger - touch lower -> long, upper -> short
        if "boll" in indicators:
            upper, lower = bollinger_bands(close, period=20)
            if close.iloc[-1] <= lower.iloc[-1]:
                votes["long"] += 1
            elif close.iloc[-1] >= upper.iloc[-1]:
                votes["short"] += 1

        # decision rule: if any vote (>=1) in long or short, notify user
        # To avoid spam, you may want a debounce mechanism (not implemented: simple)
        long_votes = votes["long"]
        short_votes = votes["short"]
        txt = None
        if long_votes > short_votes and long_votes >= 1:
            txt = f"🔔 Автосигнал (LONG) для {symbol} [{timeframe}]\nПричины: {long_votes} индикатора"
        elif short_votes > long_votes and short_votes >= 1:
            txt = f"🔔 Автосигнал (SHORT) для {symbol} [{timeframe}]\nПричины: {short_votes} индикатора"

        if txt:
            price_now = close.iloc[-1]
            txt += f"\nТекущая цена: {price_now}$"
            # send message (non-blocking)
            try:
                await bot.send_message(chat_id, txt)
                logger.info("Sent autosignal to %s for %s %s", chat_id, symbol, timeframe)
            except Exception:
                logger.exception("failed to send autosignal to %s", chat_id)
    except Exception:
        logger.exception("analyze_for_signals_for_user error")

# ---------------- Background polling worker ----------------
async def price_polling_worker():
    """Background worker which:
       - collects set of user symbols
       - fetches prices and saves history
       - evaluates alerts and autosignals for each user
    """
    logger.info("Price polling worker started, interval %s sec", PRICE_POLL_INTERVAL)
    while True:
        try:
            # 1) gather unique symbols from DB
            try:
                cur = await DB.execute("SELECT DISTINCT symbol FROM user_symbols")
                rows = await cur.fetchall()
                symbols = set(DEFAULT_SYMBOLS)
                symbols.update([r[0] for r in rows if r and r[0]])
            except Exception:
                logger.exception("error reading user_symbols")
                symbols = set(DEFAULT_SYMBOLS)

            # 2) fetch prices concurrently
            tasks = [fetch_price(sym) for sym in symbols]
            await asyncio.gather(*tasks, return_exceptions=True)

            # 3) Evaluate alerts for each user
            try:
                cur = await DB.execute("SELECT id, chat_id, symbol, mode, value, alert_type, active FROM alerts WHERE active=1")
                alert_rows = await cur.fetchall()
                for ar in alert_rows:
                    aid, chat_id, symbol, mode, value, alert_type, active = ar
                    price = last_prices.get(symbol)
                    if price is None:
                        continue
                    triggered = False
                    if mode == "absolute":
                        if price >= value:
                            triggered = True
                    elif mode == "percent":
                        # percent relative to last saved price from history; we take previous price from history table
                        # naive approach: check last history price
                        try:
                            c2 = await DB.execute("SELECT price FROM history WHERE symbol=? ORDER BY id DESC LIMIT 2", (symbol,))
                            last_two = await c2.fetchall()
                            if last_two and len(last_two) >= 2:
                                prev_price = float(last_two[1][0])
                                change_pct = abs((price - prev_price) / prev_price) * 100 if prev_price != 0 else 0
                                if change_pct >= float(value):
                                    triggered = True
                            else:
                                # fallback: cannot evaluate
                                pass
                        except Exception:
                            logger.exception("percent alert eval")
                    elif mode == "change":  # absolute delta
                        # compare to last history price
                        try:
                            c2 = await DB.execute("SELECT price FROM history WHERE symbol=? ORDER BY id DESC LIMIT 2", (symbol,))
                            last_two = await c2.fetchall()
                            if last_two and len(last_two) >= 2:
                                prev_price = float(last_two[1][0])
                                if abs(price - prev_price) >= float(value):
                                    triggered = True
                        except Exception:
                            logger.exception("change alert eval")

                    if triggered:
                        # send message and deactivate if one-shot
                        try:
                            await bot.send_message(chat_id, f"🔔 Alert: {symbol} {mode} {value} triggered. Current price: {price}$")
                            logger.info("Alert triggered for %s %s", chat_id, symbol)
                        except Exception:
                            logger.exception("sending alert message failed")
                        if alert_type == "one-shot":
                            try:
                                await DB.execute("UPDATE alerts SET active=0 WHERE id=?", (aid,))
                                await DB.commit()
                            except Exception:
                                logger.exception("deactivate alert failed")
            except Exception:
                logger.exception("alert processing error")

            # 4) autosignals: for each user, each autosignal row evaluate
            try:
                cur = await DB.execute("SELECT id, chat_id, symbol, timeframe, indicators, active FROM autosignals WHERE active=1")
                autos = await cur.fetchall()
                # process asynchronously but limited concurrency
                async def worker_row(row):
                    try:
                        await analyze_for_signals_for_user(row[1], row)
                    except Exception:
                        logger.exception("autosignal row error")

                workers = [worker_row(a) for a in autos]
                # limit concurrency - run in chunks
                if workers:
                    await asyncio.gather(*workers, return_exceptions=True)
            except Exception:
                logger.exception("autosignal processing error")

        except Exception:
            logger.exception("price_polling_worker top-level error")

        await asyncio.sleep(PRICE_POLL_INTERVAL)

# ---------------- UI / Handlers ----------------
def main_menu_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(types.InlineKeyboardButton("💰 Показать все цены", callback_data="show_all_prices"),
           types.InlineKeyboardButton("📊 График", callback_data="menu_chart"))
    kb.add(types.InlineKeyboardButton("🔔 Добавить Alert", callback_data="menu_add_alert"),
           types.InlineKeyboardButton("📋 Мои Alerts", callback_data="menu_list_alerts"))
    kb.add(types.InlineKeyboardButton("📈 Автосигналы", callback_data="menu_autosignals"),
           types.InlineKeyboardButton("⚙️ Мои монеты", callback_data="menu_my_symbols"))
    return kb

@dp.message_handler(commands=['start', 'help'])
async def cmd_start(message: types.Message):
    try:
        await add_user_if_missing(message.chat.id)
        await message.answer("👋 Привет! Я крипто-бот. Управление — через кнопки ниже.", reply_markup=main_menu_kb())
    except Exception:
        logger.exception("cmd_start")

@dp.callback_query_handler(lambda c: True)
async def callbacks_handler(callback_query: types.CallbackQuery):
    data = callback_query.data or ""
    chat_id = callback_query.from_user.id
    try:
        await add_user_if_missing(chat_id)

        # navigation
        if data == "show_all_prices":
            # fetch and list last prices for user's coins
            syms = await get_user_symbols(chat_id)
            lines = []
            for s in syms:
                price = last_prices.get(s)
                if price is None:
                    # fetch on-demand
                    p = await fetch_price(s)
                    price = p or "n/a"
                lines.append(f"{s}: {price}$")
            await bot.send_message(chat_id, "💱 Текущие цены:\n" + "\n".join(lines))
            await callback_query.answer()
            return

        if data == "menu_my_symbols":
            syms = await get_user_symbols(chat_id)
            kb = types.InlineKeyboardMarkup()
            for s in syms:
                kb.add(types.InlineKeyboardButton(s, callback_data=f"sym_{s}"))
            kb.add(types.InlineKeyboardButton("➕ Добавить монету", callback_data="add_symbol"))
            kb.add(types.InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu"))
            await bot.send_message(chat_id, "Ваши монеты:", reply_markup=kb)
            await callback_query.answer()
            return

        if data == "add_symbol":
            # set state using simple in-memory dict
            await bot.send_message(chat_id, "Введите символ монеты (например ADA или ADA/USDT):")
            # store awaiting symbol in a simple per-chat file in DB? For simplicity use in-memory map
            # but to keep across restarts you'd need persistent state. We'll use a transient dict:
            pending_symbol_inputs[chat_id] = True
            await callback_query.answer()
            return

        if data.startswith("sym_"):
            # user clicked a symbol in their list: show quick actions
            symbol = data.split("sym_",1)[1]
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("📊 График (выбрать TF)", callback_data=f"chart_for_{symbol}"))
            kb.add(types.InlineKeyboardButton("🔔 Добавить alert для этой монеты", callback_data=f"alert_for_{symbol}"))
            kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_my_symbols"))
            await bot.send_message(chat_id, f"Монета {symbol} — выберите действие:", reply_markup=kb)
            await callback_query.answer()
            return

        if data == "menu_add_alert":
            # show option: choose symbol (from user's) or type symbol manually
            syms = await get_user_symbols(chat_id)
            kb = types.InlineKeyboardMarkup()
            for s in syms:
                kb.add(types.InlineKeyboardButton(s, callback_data=f"alert_for_{s}"))
            kb.add(types.InlineKeyboardButton("✏️ Ввести вручную", callback_data="alert_manual"))
            kb.add(types.InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu"))
            await bot.send_message(chat_id, "Выберите монету для Alert:", reply_markup=kb)
            await callback_query.answer()
            return

        if data == "alert_manual":
            await bot.send_message(chat_id, "Введите монету (например BTC или BTC/USDT):")
            pending_alert_manual[chat_id] = {"step":"await_symbol"}
            await callback_query.answer()
            return

        if data.startswith("alert_for_"):
            symbol = data.split("alert_for_",1)[1]
            # ask for mode: absolute $ / percent / change $
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("💵 Абсолютно ($)", callback_data=f"alert_mode_abs|{symbol}"))
            kb.add(types.InlineKeyboardButton("📈 В процентах (%)", callback_data=f"alert_mode_pct|{symbol}"))
            kb.add(types.InlineKeyboardButton("🔁 Изменение ($)", callback_data=f"alert_mode_chg|{symbol}"))
            kb.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_add_alert"))
            await bot.send_message(chat_id, f"Добавление alert для {symbol}. Выберите режим:", reply_markup=kb)
            await callback_query.answer()
            return

        if data.startswith("alert_mode_"):
            # format: alert_mode_abs|SYMBOL
            parts = data.split("|")
            mode_part = parts[0]  # e.g. alert_mode_abs
            symbol = parts[1] if len(parts) > 1 else None
            if symbol is None:
                await callback_query.answer("Ошибка: нет символа")
                return
            if mode_part.endswith("abs"):
                pending_alert_inputs[chat_id] = {"symbol":symbol, "mode":"absolute"}
                await bot.send_message(chat_id, f"Введите цену в $ на которую поставить alert для {symbol}:")
            elif mode_part.endswith("pct"):
                pending_alert_inputs[chat_id] = {"symbol":symbol, "mode":"percent"}
                await bot.send_message(chat_id, f"Введите процент (например 2.5) изменения для {symbol}:")
            elif mode_part.endswith("chg"):
                pending_alert_inputs[chat_id] = {"symbol":symbol, "mode":"change"}
                await bot.send_message(chat_id, f"Введите абсолютное изменение в $ (например 50) для {symbol}:")
            await callback_query.answer()
            return

        if data == "menu_list_alerts":
            rows = await list_alerts(chat_id)
            if not rows:
                await bot.send_message(chat_id, "У вас нет Alerts.")
            else:
                kb = types.InlineKeyboardMarkup()
                for r in rows:
                    aid, sym, mode, val, atype, active = r
                    label = f"{sym} {mode} {val} ({'on' if active else 'off'})"
                    kb.add(types.InlineKeyboardButton(label, callback_data=f"del_alert_{aid}"))
                kb.add(types.InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu"))
                await bot.send_message(chat_id, "Ваши Alerts (нажмите чтобы удалить):", reply_markup=kb)
            await callback_query.answer()
            return

        if data.startswith("del_alert_"):
            aid = int(data.split("del_alert_",1)[1])
            await remove_alert(chat_id, aid)
            await bot.send_message(chat_id, "✅ Alert удалён.")
            await callback_query.answer()
            return

        if data == "menu_autosignals":
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("➕ Добавить автосигнал", callback_data="autosig_add"))
            kb.add(types.InlineKeyboardButton("📋 Мои автосигналы", callback_data="autosig_list"))
            kb.add(types.InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu"))
            await bot.send_message(chat_id, "Управление автосигналами:", reply_markup=kb)
            await callback_query.answer()
            return

        if data == "autosig_add":
            # ask symbol choice (from user symbols)
            syms = await get_user_symbols(chat_id)
            kb = types.InlineKeyboardMarkup()
            for s in syms:
                kb.add(types.InlineKeyboardButton(s, callback_data=f"autosig_choose_{s}"))
            kb.add(types.InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu"))
            await bot.send_message(chat_id, "Выберите монету для автосигнала:", reply_markup=kb)
            await callback_query.answer()
            return

        if data.startswith("autosig_choose_"):
            symbol = data.split("autosig_choose_",1)[1]
            # store pending and ask timeframe
            pending_autosig_inputs[chat_id] = {"symbol": symbol}
            kb = types.InlineKeyboardMarkup(row_width=3)
            for tf in SUPPORTED_TIMEFRAMES:
                kb.add(types.InlineKeyboardButton(tf, callback_data=f"autosig_tf|{tf}"))
            kb.add(types.InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu"))
            await bot.send_message(chat_id, f"Выберите таймфрейм для {symbol}:", reply_markup=kb)
            await callback_query.answer()
            return

        if data.startswith("autosig_tf|"):
            tf = data.split("autosig_tf|",1)[1]
            p = pending_autosig_inputs.get(chat_id)
            if not p:
                await callback_query.answer("Не удалось найти состояние. Начните заново.")
                return
            p["timeframe"] = tf
            # ask indicators selection
            kb = types.InlineKeyboardMarkup(row_width=2)
            kb.add(types.InlineKeyboardButton("MA", callback_data="autosig_ind_ma"),
                   types.InlineKeyboardButton("RSI", callback_data="autosig_ind_rsi"))
            kb.add(types.InlineKeyboardButton("MACD", callback_data="autosig_ind_macd"),
                   types.InlineKeyboardButton("Boll", callback_data="autosig_ind_boll"))
            kb.add(types.InlineKeyboardButton("Готово", callback_data="autosig_done"),
                   types.InlineKeyboardButton("Отмена", callback_data="main_menu"))
            # store chosen indicators in memory
            p["indicators"] = []
            await bot.send_message(chat_id, "Выберите индикаторы (можно по очереди нажимать, затем 'Готово'):", reply_markup=kb)
            await callback_query.answer()
            return

        if data.startswith("autosig_ind_"):
            ind = data.split("autosig_ind_",1)[1]
            p = pending_autosig_inputs.get(chat_id)
            if not p:
                await callback_query.answer("Структура отсутствует")
                return
            # toggle indicator in list
            if ind not in p["indicators"]:
                p["indicators"].append(ind)
                await callback_query.answer(f"Добавлено: {ind}")
            else:
                p["indicators"].remove(ind)
                await callback_query.answer(f"Удалено: {ind}")
            return

        if data == "autosig_done":
            p = pending_autosig_inputs.get(chat_id)
            if not p:
                await callback_query.answer("Нет данных")
                return
            # save autosignal
            ok = await save_autosignal(chat_id, p["symbol"], p["timeframe"], p["indicators"])
            if ok:
                await bot.send_message(chat_id, f"✅ Автосигнал сохранён для {p['symbol']} {p['timeframe']} ({','.join(p['indicators'])})")
            else:
                await bot.send_message(chat_id, "Ошибка при сохранении автосигнала.")
            pending_autosig_inputs.pop(chat_id, None)
            await callback_query.answer()
            return

        if data == "autosig_list":
            rows = await list_autosignals(chat_id)
            if not rows:
                await bot.send_message(chat_id, "У вас нет автосигналов.")
            else:
                kb = types.InlineKeyboardMarkup()
                for r in rows:
                    aid, sym, tf, inds, active = r
                    label = f"{sym} {tf} {inds}"
                    kb.add(types.InlineKeyboardButton(label, callback_data=f"autosig_del_{aid}"))
                kb.add(types.InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu"))
                await bot.send_message(chat_id, "Ваши автосигналы (нажмите чтобы удалить):", reply_markup=kb)
            await callback_query.answer()
            return

        if data.startswith("autosig_del_"):
            aid = int(data.split("autosig_del_",1)[1])
            await remove_autosignal(chat_id, aid)
            await bot.send_message(chat_id, "✅ Автосигнал удалён.")
            await callback_query.answer()
            return

        if data == "main_menu":
            await bot.send_message(chat_id, "Главное меню:", reply_markup=main_menu_kb())
            await callback_query.answer()
            return

        # Chart flow: user clicked menu_chart or chart_for_<symbol>
        if data == "menu_chart":
            syms = await get_user_symbols(chat_id)
            kb = types.InlineKeyboardMarkup()
            for s in syms:
                kb.add(types.InlineKeyboardButton(s, callback_data=f"chart_for_{s}"))
            kb.add(types.InlineKeyboardButton("⬅️ Главное меню", callback_data="main_menu"))
            await bot.send_message(chat_id, "Выберите монету для графика:", reply_markup=kb)
            await callback_query.answer()
            return

        if data.startswith("chart_for_"):
            symbol = data.split("chart_for_",1)[1]
            # ask timeframe
            kb = types.InlineKeyboardMarkup(row_width=3)
            for tf in SUPPORTED_TIMEFRAMES:
                kb.add(types.InlineKeyboardButton(tf, callback_data=f"chart_do|{symbol}|{tf}"))
            await bot.send_message(chat_id, "Выберите таймфрейм для графика:", reply_markup=kb)
            await callback_query.answer()
            return

        if data.startswith("chart_do|"):
            # chart_do|SYMBOL|TF
            parts = data.split("|")
            if len(parts) != 3:
                await callback_query.answer("Некорректно")
                return
            symbol, tf = parts[1], parts[2]
            await callback_query.answer("Генерирую график...")
            df = await fetch_ohlcv(symbol, timeframe=tf, limit=200)
            if df is None or df.empty:
                await bot.send_message(chat_id, "Недостаточно данных для графика или ошибка.")
                return
            # Build a simple line chart with matplotlib (avoid blocking long work)
            try:
                import io
                import matplotlib.pyplot as plt
                plt.switch_backend('Agg')
                fig, ax = plt.subplots(figsize=(10,4))
                ax.plot(df["ts"], df["close"], label="close")
                ax.set_title(f"{symbol} {tf}")
                ax.set_xlabel("time"); ax.set_ylabel("price")
                ax.grid(True)
                buf = io.BytesIO()
                fig.tight_layout()
                fig.savefig(buf, format="png", dpi=150)
                plt.close(fig)
                buf.seek(0)
                await bot.send_photo(chat_id, buf, caption=f"{symbol} {tf}")
            except Exception:
                logger.exception("chart generation failed")
                await bot.send_message(chat_id, "Ошибка при генерации графика.")
            return

        # default: answer unknown
        await callback_query.answer()
    except Exception:
        logger.exception("callbacks_handler")
        try:
            await callback_query.answer("Ошибка при обработке кнопки")
        except Exception:
            pass

# ---------------- Simple message handlers for pending states ----------------
# We'll keep simple in-memory dicts for short-lived flows (OK for small bot)
pending_symbol_inputs: Dict[int, bool] = {}
pending_alert_inputs: Dict[int, Dict[str, Any]] = {}
pending_alert_manual: Dict[int, Dict[str, Any]] = {}
pending_autosig_inputs: Dict[int, Dict[str, Any]] = {}

@dp.message_handler(lambda message: message.chat.id in pending_symbol_inputs)
async def handle_pending_symbol(message: types.Message):
    chat_id = message.chat.id
    symbol = message.text.strip().upper()
    if "/" not in symbol:
        symbol = f"{symbol}/USDT"
    ok = await add_user_symbol(chat_id, symbol)
    if ok:
        await message.reply(f"✅ Монета {symbol} добавлена в ваш список.", reply_markup=main_menu_kb())
    else:
        await message.reply(f"Ошибка при добавлении {symbol}.", reply_markup=main_menu_kb())
    pending_symbol_inputs.pop(chat_id, None)

@dp.message_handler(lambda message: message.chat.id in pending_alert_manual)
async def handle_pending_alert_manual(message: types.Message):
    chat_id = message.chat.id
    state = pending_alert_manual.get(chat_id)
    if not state:
        await message.reply("Состояние устарело. Начните заново.", reply_markup=main_menu_kb())
        return
    if state.get("step") == "await_symbol":
        symbol = message.text.strip().upper()
        if "/" not in symbol:
            symbol = f"{symbol}/USDT"
        state["symbol"] = symbol
        state["step"] = "await_mode"
        # ask mode choices
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("💵 Абсолютно ($)", callback_data=f"alert_mode_abs|{symbol}"))
        kb.add(types.InlineKeyboardButton("📈 В процентах (%)", callback_data=f"alert_mode_pct|{symbol}"))
        kb.add(types.InlineKeyboardButton("🔁 Изменение ($)", callback_data=f"alert_mode_chg|{symbol}"))
        await message.reply("Выберите режим:", reply_markup=kb)
    else:
        await message.reply("Неизвестный шаг.")

@dp.message_handler(lambda message: message.chat.id in pending_alert_inputs)
async def handle_pending_alert_input(message: types.Message):
    chat_id = message.chat.id
    state = pending_alert_inputs.get(chat_id)
    if not state:
        await message.reply("Состояние устарело.")
        return
    try:
        value = float(message.text.strip())
    except Exception:
        await message.reply("Введите числовое значение (например 25000 или 2.5).")
        return
    ok = await save_alert(chat_id, state["symbol"], state["mode"], value, alert_type="one-shot")
    if ok:
        await message.reply(f"✅ Alert сохранён: {state['symbol']} {state['mode']} {value}", reply_markup=main_menu_kb())
    else:
        await message.reply("Ошибка при сохранении алерта.", reply_markup=main_menu_kb())
    pending_alert_inputs.pop(chat_id, None)

# ---------------- Startup / Shutdown ----------------
async def on_startup(dp):
    logger.info("Bot starting. Use TELEGRAM_TOKEN env var or token.txt. Polling mode by async default.")
    # init DB
    await init_db()
    # start background worker
    asyncio.create_task(price_polling_worker())
    logger.info("Started background price task")

async def on_shutdown(dp):
    logger.info("Shutting down: stopping background tasks and DB")
    try:
        await exchange.close()
    except Exception:
        logger.exception("exchange close failed")
    try:
        await DB.close()
    except Exception:
        logger.exception("DB close failed")
    await bot.close()

# ---------------- Entrypoint ----------------
if __name__ == '__main__':
    # run long polling (for Railway you may prefer webhook; but long polling works in many cases)
    # For webhook deployment adapt to set webhook URL and run aiohttp/uvicorn app wrapper
    try:
        executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)
    except Exception:
        logger.exception("executor.start_polling failed")
