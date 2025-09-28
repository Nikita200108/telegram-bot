#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Async Telegram crypto bot (aiogram + aiohttp) with:
 - webhook (aiohttp web server) suitable for Railway
 - fast async price polling from MEXC via aiohttp + ccxt fallback
 - in-memory cache + persistent aiosqlite storage
 - alerts (percent/$), autosignals (RSI/EMA cross), candle charts
 - inline buttons everywhere, "price -> all my coins" immediate
 - resilient: errors logged and do not crash worker
 - rate limits and concurrency control to avoid getting blocked
"""
import os
import sys
import json
import math
import time
import asyncio
import logging
import traceback
import atexit
from datetime import datetime, timedelta
from collections import defaultdict, deque
from typing import Dict, Any, List, Tuple, Optional

# ---------- Optional libraries for plotting/data ----------
HAS_PANDAS = True
HAS_MPLFINANCE = True
HAS_MATPLOTLIB = True
try:
    import pandas as pd
    import numpy as np
except Exception:
    HAS_PANDAS = False

try:
    import mplfinance as mpf
except Exception:
    HAS_MPLFINANCE = False

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
except Exception:
    HAS_MATPLOTLIB = False

# ---------- Async libraries ----------
try:
    from aiogram import Bot, types
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    from aiogram.utils.markdown import bold, code
except Exception as e:
    print("aiogram is required. Install aiogram. Exception:", e)
    raise

try:
    import aiohttp
    import aiosqlite
    import ccxt.async_support as ccxt_async
except Exception as e:
    print("aiohttp, aiosqlite, ccxt.async_support are required. Exception:", e)
    raise

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("async_crypto_bot")

# ---------- Config (env overrides) ----------
PRICE_POLL_INTERVAL = float(os.getenv("PRICE_POLL_INTERVAL", "2.0"))  # seconds
AUTOSIGNAL_INTERVAL = float(os.getenv("AUTOSIGNAL_INTERVAL", "10.0"))  # seconds
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "1000"))
DEFAULT_SYMBOLS = os.getenv("DEFAULT_SYMBOLS", "BTC/USDT,ETH/USDT,SOL/USDT,NEAR/USDT").split(",")
DEFAULT_SYMBOLS = [s.strip() for s in DEFAULT_SYMBOLS if s.strip()]
MAX_CONCURRENT_REQS = int(os.getenv("MAX_CONCURRENT_REQS", "6"))  # concurrency when fetching many tickers
RATE_LIMIT_PER_SEC = float(os.getenv("RATE_LIMIT_PER_SEC", "5.0"))  # max requests per second (rough)
PORT = int(os.getenv("PORT", "8000"))
DB_PATH = os.getenv("DB_PATH", "bot_data_async.sqlite")
WEBHOOK_PATH = None  # will be set to /{TOKEN}

# ---------- Token resolution ----------
def resolve_token() -> str:
    t = os.getenv("TELEGRAM_TOKEN")
    if t:
        logger.info("TELEGRAM_TOKEN loaded from environment.")
        return t.strip()
    # fallback to token.txt (if deployed locally)
    if os.path.exists("token.txt"):
        try:
            with open("token.txt", "r", encoding="utf-8") as f:
                s = f.read().strip()
                if s:
                    logger.info("TELEGRAM_TOKEN loaded from token.txt")
                    return s
        except Exception:
            logger.exception("Failed to read token.txt")
    logger.critical("TELEGRAM_TOKEN not found. Set TELEGRAM_TOKEN env var or place token.txt.")
    raise RuntimeError("TELEGRAM_TOKEN not found")

TELEGRAM_TOKEN = resolve_token()
WEBHOOK_PATH = f"/{TELEGRAM_TOKEN}"
BOT_API_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ---------- aiohttp session & ccxt async exchange ----------
aiohttp_session: Optional[aiohttp.ClientSession] = None
ccxt_exchange = None

async def init_http_clients():
    global aiohttp_session, ccxt_exchange
    if aiohttp_session is None:
        aiohttp_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    if ccxt_exchange is None:
        try:
            ccxt_exchange = ccxt_async.mexc({"enableRateLimit": True})
        except Exception:
            ccxt_exchange = None

# ---------- DB helpers (aiosqlite) ----------
db: Optional[aiosqlite.Connection] = None

async def init_db():
    global db
    if db:
        return
    db = await aiosqlite.connect(DB_PATH)
    await db.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id TEXT PRIMARY KEY,
        created_at DATETIME
    )""")
    await db.execute("""
    CREATE TABLE IF NOT EXISTS user_symbols (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        symbol TEXT
    )""")
    await db.execute("""
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        symbol TEXT,
        is_percent INTEGER DEFAULT 0,
        value REAL,
        is_recurring INTEGER DEFAULT 0,
        active INTEGER DEFAULT 1,
        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    await db.execute("""
    CREATE TABLE IF NOT EXISTS autosignals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        symbol TEXT,
        timeframe TEXT,
        enabled INTEGER DEFAULT 1,
        last_notified DATETIME
    )""")
    await db.execute("""
    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT,
        price REAL,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    await db.execute("""
    CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        level TEXT,
        message TEXT,
        ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    await db.commit()
    logger.info("DB initialized at %s", DB_PATH)

async def db_log(level: str, message: str):
    try:
        await db.execute("INSERT INTO logs (level, message) VALUES (?, ?)", (level.upper(), message))
        await db.commit()
    except Exception:
        logger.exception("db_log failed")
    getattr(logger, level.lower())(message)

# ---------- In-memory runtime state ----------
price_cache: Dict[str, float] = {}               # symbol -> price
history_cache: Dict[str, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))  # symbol -> deque of (ts, price)
watch_symbols: set = set(DEFAULT_SYMBOLS)        # symbols to bulk fetch
watch_lock = asyncio.Lock()

pending_flows: Dict[str, Dict[str, Any]] = {}   # chat_id -> flow state (for multi-step actions)
user_settings: Dict[str, Dict[str, Any]] = {}   # optional cache

# Rate-limiting helpers (token-bucket-like)
req_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQS)
last_req_times = deque(maxlen=100)

async def rate_limit_wait():
    """
    Ensure we don't exceed RATE_LIMIT_PER_SEC. Very simple: if we've made > RATE_LIMIT_PER_SEC requests in last second, wait.
    """
    now = time.monotonic()
    last_req_times.append(now)
    # remove older than 1s
    while last_req_times and last_req_times[0] < now - 1.0:
        last_req_times.popleft()
    if len(last_req_times) > RATE_LIMIT_PER_SEC:
        await asyncio.sleep(0.2)

# ---------- Utility: normalize symbol ----------
def normalize_symbol(sym: str) -> str:
    s = sym.strip().upper()
    if "/" not in s:
        s = f"{s}/USDT"
    # ensure MXEC uses X/Y format, ccxt expects same
    return s

# ---------- Fetching prices (async) ----------
async def fetch_price_ccxt(symbol: str) -> Optional[float]:
    """Try using ccxt async exchange (fallback)"""
    try:
        if ccxt_exchange:
            ticker = await ccxt_exchange.fetch_ticker(symbol)
            last = ticker.get("last") or ticker.get("close")
            if last is not None:
                return float(last)
    except Exception:
        logger.debug("ccxt fetch failed for %s", symbol)
    return None

async def fetch_price_http(symbol: str) -> Optional[float]:
    """Try public REST endpoints (MEXC). Non-blocking via aiohttp."""
    try:
        # Many MEXC endpoints present symbol without slash e.g. BTCUSDT
        s = symbol.replace("/", "").upper()
        # try public ticker endpoint
        urls = [
            f"https://api.mexc.com/api/v4/market/ticker?symbol={s}",  # v4 generic
            f"https://www.mexc.com/open/api/v2/market/ticker?symbol={s}",
            f"https://api.mexc.com/api/v3/ticker/price?symbol={s}"
        ]
        async with req_semaphore:
            await rate_limit_wait()
            for u in urls:
                try:
                    async with aiohttp_session.get(u, timeout=5) as resp:
                        if resp.status != 200:
                            continue
                        j = await resp.json()
                        # different structures: try to discover price
                        if isinstance(j, dict):
                            # v4 returns list under "data" maybe
                            if "data" in j:
                                d = j["data"]
                                if isinstance(d, list) and d:
                                    item = d[0]
                                    for key in ("lastPrice","last","price","close"):
                                        if key in item:
                                            return float(item[key])
                                if isinstance(d, dict):
                                    for key in ("lastPrice","last","price","close"):
                                        if key in d:
                                            return float(d[key])
                            for key in ("price","lastPrice","last"):
                                if key in j:
                                    return float(j[key])
                        if isinstance(j, list) and j:
                            item = j[0]
                            for key in ("price","lastPrice","last"):
                                if key in item:
                                    return float(item[key])
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    continue
    except Exception:
        logger.exception("fetch_price_http failed top-level for %s", symbol)
    return None

async def update_price_for(symbol: str) -> Optional[float]:
    """Try HTTP then ccxt fallback, update caches and db."""
    try:
        sym = normalize_symbol(symbol)
        # try best-effort HTTP
        p = await fetch_price_http(sym)
        if p is None:
            p = await fetch_price_ccxt(sym)
        if p is not None:
            ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            price_cache[sym] = p
            history_cache[sym].append((ts, p))
            # write to DB non-blocking (no await heavy blocking)
            try:
                await db.execute("INSERT INTO history (symbol, price, ts) VALUES (?, ?, ?)", (sym, float(p), ts))
                await db.commit()
            except Exception:
                logger.exception("write history failed")
            return p
    except Exception:
        logger.exception("update_price_for failed for %s", symbol)
    return None

async def bulk_update_prices(symbols: List[str]):
    """Update many symbols concurrently with semaphore and rate limiting."""
    async with watch_lock:
        syms = list(set(normalize_symbol(s) for s in symbols))
    # schedule tasks with concurrency control
    tasks = []
    for s in syms:
        tasks.append(asyncio.create_task(update_price_for(s)))
    # gather but do not fail whole loop on individual errors
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return results

# ---------- Background tasks ----------
async def price_poll_worker():
    """Continuously poll watch_symbols at configured interval."""
    logger.info("Starting price poll worker (interval %s s)", PRICE_POLL_INTERVAL)
    while True:
        try:
            async with watch_lock:
                syms = list(watch_symbols)
            if syms:
                await bulk_update_prices(syms)
            # evaluate alerts after updating prices
            await evaluate_alerts()
        except Exception:
            logger.exception("price_poll_worker exception")
        await asyncio.sleep(PRICE_POLL_INTERVAL)

# ---------- Alerts evaluation ----------
async def evaluate_alerts():
    """Check DB for active alerts and trigger notifications."""
    try:
        rows = await db.execute_fetchall("SELECT id, chat_id, symbol, is_percent, value, is_recurring, active FROM alerts WHERE active=1")
        for r in rows:
            aid, chat_id, symbol, is_percent, value, is_recurring, active = r
            sym = normalize_symbol(symbol)
            cur_price = price_cache.get(sym)
            if cur_price is None:
                continue
            hist = history_cache.get(sym, [])
            prev_price = hist[-2][1] if len(hist) >= 2 else None
            triggered = False
            if is_percent:
                if prev_price is not None and prev_price != 0:
                    pct = (cur_price - prev_price) / prev_price * 100.0
                    if abs(pct) >= abs(value):
                        triggered = True
            else:
                try:
                    v = float(value)
                    if prev_price is not None:
                        if (prev_price < v and cur_price >= v) or (prev_price > v and cur_price <= v):
                            triggered = True
                    else:
                        if cur_price >= v:
                            triggered = True
                except Exception:
                    logger.exception("parse alert value")
            if triggered:
                try:
                    text = f"🔔 Alert сработал: {sym} {'%' if is_percent else '$'}{value}\nТекущая цена: {cur_price}$"
                    await send_message_async(chat_id, text, reply_markup=kb_main())
                    await db_log("info", f"Alert fired for {chat_id} {sym} {value}")
                except Exception:
                    logger.exception("notify alert failed")
                if not is_recurring:
                    try:
                        await db.execute("UPDATE alerts SET active=0 WHERE id=?", (aid,))
                        await db.commit()
                    except Exception:
                        logger.exception("deactivate alert failed")
    except Exception:
        logger.exception("evaluate_alerts failed")

# ---------- Autosignals (RSI/EMA) ----------
def calculate_rsi_list(prices: List[float], period: int = 14) -> List[float]:
    if not HAS_PANDAS:
        # naive fallback
        res = []
        for i in range(len(prices)):
            res.append(50.0)
        return res
    s = pd.Series(prices)
    delta = s.diff().dropna()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    ma_up = up.rolling(window=period).mean()
    ma_down = down.rolling(window=period).mean()
    rs = ma_up / ma_down
    rsi = 100 - 100 / (1 + rs)
    return rsi.fillna(50).tolist()

def calculate_ema_list(prices: List[float], span: int) -> List[float]:
    if not HAS_PANDAS:
        return [sum(prices)/len(prices)] * len(prices)
    s = pd.Series(prices)
    return s.ewm(span=span, adjust=False).mean().tolist()

AUTOSIGNAL_MIN_GAP = 60  # seconds between notifications per subscription

async def autosignal_worker():
    logger.info("Starting autosignal worker (interval %s s)", AUTOSIGNAL_INTERVAL)
    while True:
        try:
            rows = await db.execute_fetchall("SELECT id, chat_id, symbol, timeframe, enabled, last_notified FROM autosignals WHERE enabled=1")
            for r in rows:
                aid, chat_id, symbol, timeframe, enabled, last_notified = r
                # fetch ohlcv for timeframe (use ccxt or HTTP fallback)
                try:
                    ohlcv = []
                    # prefer ccxt async if available
                    try:
                        if ccxt_exchange:
                            ohlcv = await ccxt_exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=200)
                    except Exception:
                        ohlcv = []
                    if not ohlcv:
                        # fallback to ccxt sync? we don't have sync. Skip if no data.
                        logger.debug("No ohlcv for %s %s via ccxt", symbol, timeframe)
                        continue
                    closes = [c[4] for c in ohlcv]
                    if len(closes) < 20:
                        continue
                    rsi = calculate_rsi_list(closes, 14)
                    ema5 = calculate_ema_list(closes, 5)
                    ema20 = calculate_ema_list(closes, 20)
                    msg = None
                    # RSI
                    rsi_now = rsi[-1] if rsi else None
                    if rsi_now is not None:
                        if rsi_now > 70:
                            msg = f"🔻 {symbol} {timeframe}: RSI {rsi_now:.1f} (overbought) — возможный SHORT"
                        elif rsi_now < 30:
                            msg = f"🔺 {symbol} {timeframe}: RSI {rsi_now:.1f} (oversold) — возможный LONG"
                    # EMA cross
                    if len(ema5) >= 2 and len(ema20) >= 2:
                        if ema5[-2] <= ema20[-2] and ema5[-1] > ema20[-1]:
                            msg = f"🔺 {symbol} {timeframe}: EMA5 crossed above EMA20 (LONG)"
                        elif ema5[-2] >= ema20[-2] and ema5[-1] < ema20[-1]:
                            msg = f"🔻 {symbol} {timeframe}: EMA5 crossed below EMA20 (SHORT)"
                    if msg:
                        send_allowed = True
                        if last_notified:
                            try:
                                last_dt = datetime.strptime(last_notified, "%Y-%m-%d %H:%M:%S")
                                if datetime.utcnow() - last_dt < timedelta(seconds=AUTOSIGNAL_MIN_GAP):
                                    send_allowed = False
                            except Exception:
                                send_allowed = True
                        if send_allowed:
                            try:
                                await send_message_async(chat_id, f"🤖 Автосигнал: {msg}", reply_markup=kb_main())
                                await db.execute("UPDATE autosignals SET last_notified=? WHERE id=?", (datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), aid))
                                await db.commit()
                            except Exception:
                                logger.exception("autosignal notify failed")
                except Exception:
                    logger.exception("autosignal per-subscription failed")
        except Exception:
            logger.exception("autosignal_worker top-level exception")
        await asyncio.sleep(AUTOSIGNAL_INTERVAL)

# ---------- Telegram helpers (async) ----------
bot = Bot(token=TELEGRAM_TOKEN, parse_mode="HTML")

async def send_message_async(chat_id: int, text: str, reply_markup: Optional[Dict]=None):
    try:
        if reply_markup:
            await bot.send_message(chat_id, text, reply_markup=types.InlineKeyboardMarkup.from_dict(reply_markup))
        else:
            await bot.send_message(chat_id, text)
    except Exception:
        logger.exception("send_message_async failed")

async def edit_message_async(chat_id: int, message_id: int, text: str, reply_markup: Optional[Dict]=None):
    try:
        if reply_markup:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=types.InlineKeyboardMarkup.from_dict(reply_markup))
        else:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
    except Exception:
        logger.exception("edit_message_async failed")

async def answer_callback_async(callback_query_id: str, text: Optional[str] = None):
    try:
        await bot.answer_callback_query(callback_query_id, text=text or "")
    except Exception:
        logger.exception("answer_callback_async failed")

# ---------- Keyboards builders ----------
def kb_main():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(InlineKeyboardButton("💰 Цена (все мои монеты)", callback_data="price_all"),
           InlineKeyboardButton("📊 График", callback_data="chart_menu"))
    kb.add(InlineKeyboardButton("🔔 Алерт", callback_data="alerts_menu"),
           InlineKeyboardButton("🤖 Автосигналы", callback_data="autosignals_menu"))
    kb.add(InlineKeyboardButton("⚙️ Настройки", callback_data="settings_menu"))
    return kb.to_python()

def kb_symbols_grid(chat_id: int, callback_prefix: str):
    # fetch user symbols from DB synchronously via await? We'll return python dict; caller will wrap it if needed
    # We'll build keyboard with default symbols for simplicity (user symbols require DB fetch in handler)
    kb = {"inline_keyboard": []}
    # fetch user's symbols will be done in handler and we will then reconstruct a keyboard (helper used in handler)
    # placeholder
    return kb

def build_kb_from_symbols(symbols: List[str], prefix: str, add_main=True, per_row=3):
    rows = []
    row = []
    for i, s in enumerate(symbols):
        txt = s.split("/")[0]
        row.append({"text": txt, "callback_data": f"{prefix}_{s}"})
        if (i+1) % per_row == 0:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if add_main:
        rows.append([{"text":"🏠 Главное меню", "callback_data":"main"}])
    return {"inline_keyboard": rows}

def kb_timeframes_for(symbol: str, prefix: str):
    groups = [["1m","5m","15m"], ["30m","1h","4h"], ["1d"]]
    rows = []
    for g in groups:
        rows.append([{"text":tf, "callback_data": f"{prefix}_{symbol}_{tf}"} for tf in g])
    rows.append([{"text":"🏠 Главное меню", "callback_data":"main"}])
    return {"inline_keyboard": rows}

def kb_alerts_menu():
    return {"inline_keyboard":[[{"text":"➕ Добавить","callback_data":"add_alert"},{"text":"📋 Мои алерты","callback_data":"list_alerts"}],
                               [{"text":"🏠 Главное меню","callback_data":"main"}]]}

def kb_alert_type():
    return {"inline_keyboard":[[{"text":"%","callback_data":"alert_type_pct"},{"text":"$","callback_data":"alert_type_usd"}],
                                [{"text":"🏠 Главное меню","callback_data":"main"}]]}

# ---------- Chart building (run_in_executor for heavy synchronous matplotlib) ----------
async def build_candlestick_bytes(symbol: str, timeframe: str="1h", limit: int=200) -> Tuple[Optional[bytes], Optional[str]]:
    """
    Returns (bytes, None) on success or (None, error message) on failure.
    Uses ccxt async fetch_ohlcv if available, otherwise fails.
    """
    try:
        if not HAS_PANDAS or not HAS_MATPLOTLIB:
            return None, "Pandas or Matplotlib not installed for charts"
        # fetch ohlcv via ccxt async
        ohlcv = []
        try:
            if ccxt_exchange:
                ohlcv = await ccxt_exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        except Exception:
            ohlcv = []
        if not ohlcv:
            return None, "No OHLCV data"
        # convert to pandas DF
        df = pd.DataFrame(ohlcv, columns=["ts","open","high","low","close","vol"])
        df['date'] = pd.to_datetime(df['ts'], unit='ms')
        df.set_index('date', inplace=True)
        df = df[["open","high","low","close","vol"]]
        # run plotting in executor (blocking)
        loop = asyncio.get_event_loop()
        def plot_and_return():
            import io
            buf = io.BytesIO()
            try:
                if HAS_MPLFINANCE:
                    mpf.plot(df, type='candle', style='charles', volume=True, savefig=dict(fname=buf, dpi=150, bbox_inches="tight"))
                    buf.seek(0)
                    return buf.getvalue()
            except Exception:
                # fallback manual
                pass
            try:
                fig, ax = plt.subplots(figsize=(10,6))
                df_reset = df.reset_index()
                dates = mdates.date2num(df_reset['date'].to_pydatetime())
                width = max(0.0005, (dates[1] - dates[0]) * 0.6) if len(dates) > 1 else 0.0005
                for idx, row in df_reset.iterrows():
                    color = 'green' if row['close'] >= row['open'] else 'red'
                    ax.vlines(dates[idx], row['low'], row['high'], color='black', linewidth=0.5)
                    ax.add_patch(plt.Rectangle((dates[idx]-width/2, min(row['open'], row['close'])), width, abs(row['open']-row['close']), color=color))
                ax.xaxis_date()
                ax.set_title(f"{symbol} {timeframe}")
                ax.grid(True)
                plt.tight_layout()
                fig.savefig(buf, format='png', dpi=150)
                plt.close(fig)
                buf.seek(0)
                return buf.getvalue()
            except Exception:
                logger.exception("manual plotting failed")
                return None
        data_bytes = await loop.run_in_executor(None, plot_and_return)
        if data_bytes:
            return data_bytes, None
        return None, "chart generation failed"
    except Exception:
        logger.exception("build_candlestick_bytes failed")
        return None, "chart generation error"

# ---------- Webhook handling (aiohttp routes) ----------
from aiohttp import web

routes = web.RouteTableDef()

# helper to fetch user's symbols from DB
async def get_user_symbols_db(chat_id: int) -> List[str]:
    try:
        rows = await db.execute_fetchall("SELECT symbol FROM user_symbols WHERE chat_id=?", (str(chat_id),))
        if rows:
            return [r[0] for r in rows]
    except Exception:
        logger.exception("get_user_symbols_db failed")
    return list(DEFAULT_SYMBOLS)

# helper to add user symbol
async def add_user_symbol_db(chat_id: int, symbol: str):
    try:
        await db.execute("INSERT INTO user_symbols (chat_id, symbol) VALUES (?, ?)", (str(chat_id), symbol))
        await db.commit()
        async with watch_lock:
            watch_symbols.add(normalize_symbol(symbol))
        return True
    except Exception:
        logger.exception("add_user_symbol_db failed")
        return False

# route for Telegram webhook (POST)
@routes.post(WEBHOOK_PATH)
async def telegram_webhook(request):
    try:
        update = await request.json()
    except Exception:
        return web.json_response({"ok": True})
    # quickly schedule processing and return 200 immediately
    asyncio.create_task(handle_update(update))
    return web.json_response({"ok": True})

async def handle_update(update: dict):
    """Main update dispatcher (runs asynchronously)."""
    try:
        # callback_query
        if "callback_query" in update:
            cb = update["callback_query"]
            data = cb.get("data", "")
            from_user = cb.get("from", {})
            chat_id = cb.get("message", {}).get("chat", {}).get("id") or from_user.get("id")
            callback_id = cb.get("id")
            await db_log("info", f"callback {data} from {chat_id}")
            # navigation
            if data == "main":
                await answer_callback_async(callback_id, "Открываю главное меню")
                await bot.edit_message_text("🏠 Главное меню", chat_id=chat_id, message_id=cb.get("message", {}).get("message_id"), reply_markup=kb_main())
                return
            if data == "price_all":
                syms = await get_user_symbols_db(chat_id)
                lines = []
                for s in syms:
                    sym = normalize_symbol(s)
                    p = price_cache.get(sym)
                    lines.append(f"{sym}: {p if p is not None else 'N/A'}$")
                await answer_callback_async(callback_id, "Цены отправлены")
                await send_message_async(chat_id, "💰 Цены (источник: MEXC):\n" + "\n".join(lines), reply_markup=kb_main().to_python() if hasattr(kb_main(), "to_python") else kb_main())
                return
            if data == "chart_menu":
                syms = await get_user_symbols_db(chat_id)
                kb = build_kb_from_symbols(syms, "chart_sel")
                await answer_callback_async(callback_id)
                # edit inline keyboard in message if exists
                try:
                    await bot.edit_message_text("📊 Выберите монету для графика", chat_id=chat_id, message_id=cb.get("message", {}).get("message_id"), reply_markup=types.InlineKeyboardMarkup.from_dict(kb))
                except Exception:
                    await send_message_async(chat_id, "📊 Выберите монету для графика", reply_markup=kb)
                return
            if data.startswith("chart_sel_"):
                _, rest = data.split("chart_sel_",1)
                sym = rest
                kb_tf = kb_timeframes_for(sym, "chart_tf")
                await answer_callback_async(callback_id)
                try:
                    await bot.edit_message_text(f"📊 {sym} — выберите таймфрейм", chat_id=chat_id, message_id=cb.get("message", {}).get("message_id"), reply_markup=types.InlineKeyboardMarkup.from_dict(kb_tf))
                except Exception:
                    await send_message_async(chat_id, f"📊 {sym} — выберите таймфрейм", reply_markup=kb_tf)
                return
            if data.startswith("chart_tf_"):
                # format chart_tf_SYMBOL_TF
                try:
                    _, rest = data.split("chart_tf_",1)
                    # rest like "BTC/USDT_1h"
                    symbol, tf = rest.rsplit("_",1)
                except Exception:
                    await answer_callback_async(callback_id, "Ошибка парсинга")
                    return
                await answer_callback_async(callback_id, "Запрос принят. Строю график...")
                # build chart async
                async def build_send_chart():
                    buf, err = await build_candlestick_bytes(symbol, timeframe=tf)
                    if buf:
                        # send photo via bot (bytes)
                        try:
                            await bot.send_photo(chat_id, (buf,), caption=f"{symbol} {tf} свечи (MEXC)")
                        except Exception:
                            # fallback send as document
                            await bot.send_document(chat_id, (buf,), caption=f"{symbol} {tf} свечи")
                    else:
                        await send_message_async(chat_id, f"Не удалось построить график: {err}")
                asyncio.create_task(build_send_chart())
                return
            # Alerts menu interactions (add/list/del/edit)
            if data == "alerts_menu":
                kb = kb_alerts_menu()
                await answer_callback_async(callback_id)
                try:
                    await bot.edit_message_text("🔔 Меню алертов", chat_id=chat_id, message_id=cb.get("message", {}).get("message_id"), reply_markup=types.InlineKeyboardMarkup.from_dict(kb))
                except Exception:
                    await send_message_async(chat_id, "🔔 Меню алертов", reply_markup=kb)
                return
            if data == "add_alert":
                pending_flows[str(chat_id)] = {"state":"awaiting_alert_type"}
                kb = kb_alert_type()
                await answer_callback_async(callback_id)
                try:
                    await bot.edit_message_text("➕ Выберите тип алерта", chat_id=chat_id, message_id=cb.get("message", {}).get("message_id"), reply_markup=types.InlineKeyboardMarkup.from_dict(kb))
                except Exception:
                    await send_message_async(chat_id, "➕ Выберите тип алерта", reply_markup=kb)
                return
            if data in ("alert_type_pct","alert_type_usd"):
                is_pct = 1 if data=="alert_type_pct" else 0
                pending_flows[str(chat_id)] = {"state":"awaiting_alert_value","is_percent":is_pct}
                syms = await get_user_symbols_db(chat_id)
                kb = build_kb_from_symbols(syms, "alert_symbol")
                await answer_callback_async(callback_id)
                await send_message_async(chat_id, "Введите значение (например 2.5 или 50). Затем нажмите монету кнопкой.", reply_markup=kb)
                return
            if data.startswith("alert_symbol_"):
                _, rest = data.split("alert_symbol_",1)
                symbol = rest
                key = str(chat_id)
                flow = pending_flows.get(key)
                if not flow or flow.get("state") not in ("awaiting_alert_value","awaiting_value_for_symbol"):
                    # ask for value first
                    pending_flows[key] = {"state":"awaiting_value_for_symbol","symbol":symbol}
                    await answer_callback_async(callback_id)
                    await send_message_async(chat_id, f"Вы выбрали {symbol}. Введите значение (например 50 или 2.5).")
                    return
                # if value was already set
                val = flow.get("value")
                is_pct = flow.get("is_percent",0)
                if val is None:
                    await answer_callback_async(callback_id, "Вы ещё не ввели значение. Введите число сообщением, затем нажмите монету.")
                    return
                # save alert
                try:
                    await db.execute("INSERT INTO alerts (chat_id, symbol, is_percent, value, is_recurring, active) VALUES (?, ?, ?, ?, ?, ?)",
                                     (str(chat_id), symbol, int(is_pct), float(val), 0, 1))
                    await db.commit()
                    await send_message_async(chat_id, f"✅ Алерт добавлен для {symbol}: {'%' if is_pct else '$'}{val}", reply_markup=kb_main())
                    pending_flows.pop(key, None)
                except Exception:
                    logger.exception("save alert failed")
                    await send_message_async(chat_id, "Ошибка при сохранении алерта.")
                await answer_callback_async(callback_id)
                return
            if data == "list_alerts":
                # fetch alerts and show
                rows = await db.execute_fetchall("SELECT id, symbol, is_percent, value, is_recurring, active FROM alerts WHERE chat_id=? ORDER BY id DESC", (str(chat_id),))
                if not rows:
                    await answer_callback_async(callback_id)
                    await bot.edit_message_text("У вас нет алертов.", chat_id=chat_id, message_id=cb.get("message", {}).get("message_id"), reply_markup=kb_alerts_menu())
                else:
                    # build keyboard rows
                    kb_rows = []
                    for r in rows:
                        aid, sym, is_pct, val, rec, active = r
                        label = f"{sym} {'%' if is_pct else '$'}{val} {'🔁' if rec else ''}"
                        kb_rows.append([{"text": label, "callback_data": f"alert_item_{aid}"}, {"text":"❌", "callback_data": f"alert_del_{aid}"}])
                    kb_rows.append([{"text":"⬅️ Назад","callback_data":"alerts_menu"},{"text":"🏠 Главное меню","callback_data":"main"}])
                    kb = {"inline_keyboard": kb_rows}
                    await answer_callback_async(callback_id)
                    await bot.edit_message_text("📋 Ваши алерты (нажмите ❌, чтобы удалить):", chat_id=chat_id, message_id=cb.get("message", {}).get("message_id"), reply_markup=types.InlineKeyboardMarkup.from_dict(kb))
                return
            if data.startswith("alert_del_"):
                aid = int(data.split("_")[-1])
                await db.execute("DELETE FROM alerts WHERE id=? AND chat_id=?", (aid, str(chat_id)))
                await db.commit()
                await answer_callback_async(callback_id, "Алерт удалён")
                await send_message_async(chat_id, "✅ Алерт удалён.", reply_markup=kb_alerts_menu())
                return
            # Autosignals menu
            if data == "autosignals_menu":
                syms = await get_user_symbols_db(chat_id)
                kb = build_kb_from_symbols(syms, "autosel")
                await answer_callback_async(callback_id)
                try:
                    await bot.edit_message_text("🤖 Автосигналы — выберите монету", chat_id=chat_id, message_id=cb.get("message", {}).get("message_id"), reply_markup=types.InlineKeyboardMarkup.from_dict(kb))
                except Exception:
                    await send_message_async(chat_id, "🤖 Автосигналы — выберите монету", reply_markup=kb)
                return
            if data.startswith("autosel_"):
                _, rest = data.split("autosel_",1)
                symbol = rest
                kb_tf = kb_timeframes_for(symbol, "autosel_tf")
                await answer_callback_async(callback_id)
                try:
                    await bot.edit_message_text(f"🤖 {symbol} — выберите таймфрейм", chat_id=chat_id, message_id=cb.get("message", {}).get("message_id"), reply_markup=types.InlineKeyboardMarkup.from_dict(kb_tf))
                except Exception:
                    await send_message_async(chat_id, f"🤖 {symbol} — выберите таймфрейм", reply_markup=kb_tf)
                return
            if data.startswith("autosel_tf_"):
                try:
                    _, rest = data.split("autosel_tf_",1)
                    symbol, tf = rest.rsplit("_",1)
                except Exception:
                    await answer_callback_async(callback_id, "Ошибка парсинга")
                    return
                # upsert autosignal
                try:
                    # check existing
                    row = await db.execute_fetchone("SELECT id FROM autosignals WHERE chat_id=? AND symbol=? AND timeframe=?", (str(chat_id), symbol, tf))
                    if row:
                        await db.execute("UPDATE autosignals SET enabled=1 WHERE id=?", (row[0],))
                    else:
                        await db.execute("INSERT INTO autosignals (chat_id, symbol, timeframe, enabled) VALUES (?, ?, ?, ?)", (str(chat_id), symbol, tf, 1))
                    await db.commit()
                    await answer_callback_async(callback_id)
                    await send_message_async(chat_id, f"✅ Автосигнал подписан: {symbol} {tf}", reply_markup=kb_main())
                except Exception:
                    logger.exception("upsert autosignal failed")
                    await answer_callback_async(callback_id, "Ошибка сохранения автосигнала")
                return

            # default fallback
            await answer_callback_async(callback_id, "Неизвестная кнопка")
            return

        # message handling (text)
        if "message" in update:
            msg = update["message"]
            chat_id = msg.get("chat", {}).get("id")
            text = (msg.get("text") or "").strip()
            if not chat_id:
                return
            await db_log("info", f"msg {text} from {chat_id}")
            # process flows (pending)
            key = str(chat_id)
            flow = pending_flows.get(key)
            if flow:
                state = flow.get("state")
                if state == "awaiting_alert_value":
                    # parse number
                    try:
                        val = float(text.replace(",","."))
                        flow["value"] = val
                        pending_flows[key] = flow
                        await send_message_async(chat_id, f"Значение {val} сохранено. Теперь выберите монету кнопкой.", reply_markup=build_kb_from_symbols(await get_user_symbols_db(chat_id), "alert_symbol"))
                    except Exception:
                        await send_message_async(chat_id, "Неверный формат числа. Введите число (например 2.5 или 50).")
                    return
                if state == "awaiting_value_for_symbol":
                    try:
                        v = float(text.replace(",",".")) 
                        symbol = flow.get("symbol")
                        is_pct = flow.get("is_percent", 0)
                        await db.execute("INSERT INTO alerts (chat_id, symbol, is_percent, value, is_recurring, active) VALUES (?, ?, ?, ?, ?, ?)", (str(chat_id), symbol, int(is_pct), float(v), 0, 1))
                        await db.commit()
                        await send_message_async(chat_id, f"✅ Алерт добавлен: {symbol} {'%' if is_pct else '$'}{v}", reply_markup=kb_main())
                        pending_flows.pop(key, None)
                    except Exception:
                        await send_message_async(chat_id, "Ошибка при сохранении алерта.")
                    return
                if state == "awaiting_edit_value":
                    try:
                        newv = float(text.replace(",",".")) 
                        aid = flow.get("edit_aid")
                        await db.execute("UPDATE alerts SET value=? WHERE id=?", (newv, aid))
                        await db.commit()
                        await send_message_async(chat_id, f"✅ Алерт #{aid} обновлён", reply_markup=kb_main())
                        pending_flows.pop(key, None)
                    except Exception:
                        await send_message_async(chat_id, "Ошибка при обновлении алерта.")
                    return

            # plain commands
            if text.startswith("/start"):
                await add_user_db(chat_id)
                await send_message_async(chat_id, "👋 Привет! Я асинхронный крипто-бот. Управление через кнопки ниже.", reply_markup=kb_main())
                return
            if text.startswith("/price"):
                parts = text.split()
                if len(parts) == 2:
                    s = normalize_symbol(parts[1])
                    p = price_cache.get(s) or await update_price_for(s)
                    fair = await fetch_mexc_fair_price_async(s)
                    await send_message_async(chat_id, f"{s}: last {p if p is not None else 'N/A'}$, fair {fair if fair is not None else 'N/A'}", reply_markup=kb_main())
                else:
                    await send_message_async(chat_id, "Использование: /price SYMBOL (например /price BTC)", reply_markup=kb_main())
                return
            if text.startswith("/chart"):
                parts = text.split()
                if len(parts) >= 2:
                    s = normalize_symbol(parts[1])
                    tf = parts[2] if len(parts) >= 3 else "1h"
                    await send_message_async(chat_id, f"Строю график {s} {tf} ...")
                    async def build_send():
                        buf, err = await build_candlestick_bytes(s, timeframe=tf)
                        if buf:
                            try:
                                await bot.send_photo(chat_id, (buf,), caption=f"{s} {tf} свечи")
                            except Exception:
                                await bot.send_document(chat_id, (buf,), caption=f"{s} {tf} свечи")
                        else:
                            await send_message_async(chat_id, f"Не удалось: {err}")
                    asyncio.create_task(build_send())
                else:
                    await send_message_async(chat_id, "Использование: /chart SYMBOL [tf]\nНапример /chart BTC 1h", reply_markup=kb_main())
                return
            # fallback show menu
            await send_message_async(chat_id, "Выберите действие:", reply_markup=kb_main())
            return
    except Exception:
        logger.exception("handle_update top-level exception")

# ---------- helper DB user add ----------
async def add_user_db(chat_id: int):
    try:
        await db.execute("INSERT OR IGNORE INTO users (chat_id, created_at) VALUES (?, ?)", (str(chat_id), datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")))
        await db.commit()
    except Exception:
        logger.exception("add_user_db failed")

# ---------- fetch fair price async wrapper ----------
async def fetch_mexc_fair_price_async(symbol: str) -> Optional[float]:
    """Best-effort HTTP calls to get a fair price; short timeout; non-blocking"""
    try:
        s = symbol.replace("/", "").upper()
        urls = [
            f"https://api.mexc.com/api/v4/market/ticker?symbol={s}",
            f"https://www.mexc.com/open/api/v2/market/ticker?symbol={s}",
            f"https://api.mexc.com/api/v3/ticker/price?symbol={s}"
        ]
        for u in urls:
            try:
                async with aiohttp_session.get(u, timeout=3) as resp:
                    if resp.status != 200:
                        continue
                    j = await resp.json()
                    if isinstance(j, dict):
                        data = j.get("data") or j
                        if isinstance(data, list) and data:
                            item = data[0]
                            for key in ("lastPrice","price","last","close"):
                                if key in item:
                                    try:
                                        return float(item[key])
                                    except:
                                        pass
                        if isinstance(data, dict):
                            for key in ("lastPrice","price","last","close"):
                                if key in data:
                                    try:
                                        return float(data[key])
                                    except:
                                        pass
                    if isinstance(j, list) and j:
                        item = j[0]
                        for key in ("lastPrice","price","last","close"):
                            if key in item:
                                try:
                                    return float(item[key])
                                except:
                                    pass
            except Exception:
                continue
    except Exception:
        logger.exception("fetch_mexc_fair_price_async failed")
    return None

# ---------- Startup and shutdown ----------
async def start_background_tasks(app):
    logger.info("Starting background tasks and http clients...")
    await init_http_clients()
    await init_db()
    # start watchers
    app['price_worker'] = asyncio.create_task(price_poll_worker())
    app['autosignal_worker'] = asyncio.create_task(autosignal_worker())
    logger.info("Background tasks started.")

async def cleanup_background_tasks(app):
    logger.info("Cancelling background tasks...")
    for t in ("price_worker","autosignal_worker"):
        task = app.get(t)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    if aiohttp_session:
        await aiohttp_session.close()
    if ccxt_exchange:
        try:
            await ccxt_exchange.close()
        except Exception:
            pass
    if db:
        try:
            await db.close()
        except Exception:
            pass
    logger.info("Cleanup done.")

# register routes and lifecycle
app = web.Application()
app.add_routes(routes)
app.on_startup.append(start_background_tasks)
app.on_cleanup.append(cleanup_background_tasks)

# ---------- Utility to execute DB fetchone/fetchall easily ----------
# monkeypatch simple convenience methods into aiosqlite connection (if not present)
async def _db_execute_fetchone(self, query, params=()):
    cur = await self.execute(query, params)
    row = await cur.fetchone()
    await cur.close()
    return row

async def _db_execute_fetchall(self, query, params=()):
    cur = await self.execute(query, params)
    rows = await cur.fetchall()
    await cur.close()
    return rows

# attach monkeypatch
aiosqlite.Connection.execute_fetchone = _db_execute_fetchone
aiosqlite.Connection.execute_fetchall = _db_execute_fetchall

# ---------- Run server ----------
def main():
    # print helpful info
    logger.info("Starting aiohttp webhook server for Telegram.")
    logger.info("Webhook path: %s (set webhook to https://<your-domain>%s)", WEBHOOK_PATH, WEBHOOK_PATH)
    # On Railway, use web: gunicorn bot:app -k aiohttp.GunicornWebWorker ... or just use built-in app runner (for local dev)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
