import asyncio
import logging
import sqlite3
import io
import json
import aiohttp
import ccxt.async_support as ccxt
import pandas as pd
import matplotlib.pyplot as plt
import mplfinance as mpf
from datetime import datetime

# --- КОНФИГУРАЦИЯ ---
TOKEN = "8054728348:AAHM1awWcJluyjkLPmxSSCVoP_KzsiqjwP8"
ADMIN_USERNAME = "Nikita_Fomenk"
DB_PATH = "terminal_v5.sqlite"
CHECK_INTERVAL = 0.3 # Высокая скорость опроса

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("CryptoTerminal")

exchange = ccxt.mexc({'enableRateLimit': True, 'options': {'defaultType': 'spot'}})
user_states = {}

# --- БАЗА ДАННЫХ ---
async def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS portfolio (chat_id TEXT, symbol TEXT, UNIQUE(chat_id, symbol))")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            chat_id TEXT, symbol TEXT, target REAL, is_persistent INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

# --- ТЕЛЕГРАМ API ---
class BotInterface:
    def __init__(self, token):
        self.url = f"https://api.telegram.org/bot{token}"
        self.session = None

    async def get_session(self):
        if not self.session or self.session.closed:
            self.session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=100))
        return self.session

    async def request(self, method, payload=None, data=None):
        session = await self.get_session()
        target_url = f"{self.url}/{method}"
        try:
            if data:
                async with session.post(target_url, data=data) as resp: return await resp.json()
            async with session.post(target_url, json=payload) as resp: return await resp.json()
        except: return {}

    async def send_msg(self, chat_id, text, keyboard=None):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if keyboard: payload["reply_markup"] = keyboard
        return await self.request("sendMessage", payload)

bot = BotInterface(TOKEN)

# --- АНАЛИТИКА (АВТОСИГНАЛЫ) ---
async def get_ai_signals(symbol):
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, '1h', limit=50)
        df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        
        # Индикаторы
        # 1. RSI
        delta = df['c'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rsi = 100 - (100 / (1 + (gain / loss))).iloc[-1]

        # 2. Bollinger Bands
        ma20 = df['c'].rolling(20).mean()
        std20 = df['c'].rolling(20).std()
        upper_bb = (ma20 + (std20 * 2)).iloc[-1]
        lower_bb = (ma20 - (std20 * 2)).iloc[-1]
        curr_price = df['c'].iloc[-1]

        # 3. Volume Spike
        avg_vol = df['v'].tail(10).mean()
        curr_vol = df['v'].iloc[-1]

        signal = "⚖️ Нейтрально"
        strength = "Слабый"
        
        if rsi < 30 or curr_price <= lower_bb:
            signal = "🟢 ПОКУПАТЬ (Long)"
            strength = "Сильный" if curr_vol > avg_vol * 1.5 else "Средний"
        elif rsi > 70 or curr_price >= upper_bb:
            signal = "🔴 ПРОДАВАТЬ (Short)"
            strength = "Сильный" if curr_vol > avg_vol * 1.5 else "Средний"

        return f"<b>{symbol}</b>\nСигнал: {signal}\nСила: {strength}\nRSI: {rsi:.1f}\nОбъем: {'📈 Выше нормы' if curr_vol > avg_vol else '📉 Низкий'}"
    except: return f"⚠️ {symbol}: Ошибка анализа"

# --- ГРАФИКИ (MEXC STYLE) ---
async def send_pro_chart(chat_id, symbol, timeframe):
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=80)
        df = pd.DataFrame(ohlcv, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['Date'] = pd.to_datetime(df['Date'], unit='ms')
        df.set_index('Date', inplace=True)

        mc = mpf.make_marketcolors(up='#00ff88', down='#ff3355', inherit=True)
        s = mpf.make_mpf_style(marketcolors=mc, gridstyle='--', gridcolor='#333333', facecolor='#0b0e11')
        
        buf = io.BytesIO()
        mpf.plot(df, type='candle', style=s, volume=True, figsize=(11, 6), savefig=dict(fname=buf, format='png'))
        buf.seek(0)
        
        data = aiohttp.FormData()
        data.add_field('chat_id', str(chat_id))
        data.add_field('photo', buf, filename='chart.png')
        data.add_field('caption', f"📊 {symbol} [{timeframe}]\nСвечной анализ готов.")
        await bot.request("sendPhoto", data=data)
    except: await bot.send_msg(chat_id, "❌ Ошибка генерации графика")

# --- КЛАВИАТУРЫ ---
def main_kb():
    return {"inline_keyboard": [
        [{"text": "💰 Цена", "callback_data": "menu_price"}, {"text": "📊 Графики", "callback_data": "menu_charts"}],
        [{"text": "💼 Портфель", "callback_data": "menu_port"}, {"text": "🔔 Создать Алерт", "callback_data": "menu_newalert"}],
        [{"text": "📋 Мои Алерты", "callback_data": "menu_myalerts"}, {"text": "🧠 Сигналы AI", "callback_data": "menu_signals"}],
        [{"text": "🎓 КУРСЫ АДМИНА", "url": f"https://t.me/{ADMIN_USERNAME}"}]
    ]}

def coin_kb(chat_id, prefix):
    conn = sqlite3.connect(DB_PATH); cur = conn.cursor()
    cur.execute("SELECT symbol FROM portfolio WHERE chat_id=?", (str(chat_id),)); coins = [r[0] for r in cur.fetchall()]
    if not coins: return None
    return {"inline_keyboard": [[{"text": c, "callback_data": f"{prefix}_{c}"}] for c in coins] + [[{"text": "⬅️ Назад", "callback_data": "home"}]]}

# --- ОБРАБОТКА CALLBACK ---
async def handle_callback(cb):
    chat_id = cb["message"]["chat"]["id"]; data = cb["data"]; mid = cb["message"]["message_id"]

    if data == "home":
        await bot.request("editMessageText", {"chat_id": chat_id, "message_id": mid, "text": "💎 <b>CRYPTO TERMINAL v5.0</b>", "reply_markup": main_kb(), "parse_mode": "HTML"})
    elif data == "menu_price":
        kb = coin_kb(chat_id, "getp")
        await bot.request("editMessageText", {"chat_id": chat_id, "message_id": mid, "text": "Выберите монету:", "reply_markup": kb or {"inline_keyboard":[[{"text":"+ Добавить","callback_data":"add"}]]}})
    elif data.startswith("getp_"):
        sym = data.split("_")[1]; t = await exchange.fetch_ticker(sym)
        await bot.send_msg(chat_id, f"💰 {sym}: <b>{t['last']}$</b> ({t['percentage']}%)")
    elif data == "menu_port":
        user_states[chat_id] = "ADD"
        await bot.send_msg(chat_id, "Введите тикер (например BTC):")
    elif data == "menu_signals":
        conn = sqlite3.connect(DB_PATH); cur = conn.cursor(); cur.execute("SELECT symbol FROM portfolio WHERE chat_id=?", (str(chat_id),)); coins = cur.fetchall()
        for c in coins:
            sig = await get_ai_signals(c[0])
            await bot.send_msg(chat_id, sig)
    elif data == "menu_charts":
        kb = coin_kb(chat_id, "chart")
        await bot.request("editMessageText", {"chat_id": chat_id, "message_id": mid, "text": "График какой монеты?", "reply_markup": kb})
    elif data.startswith("chart_"):
        sym = data.split("_")[1]
        await send_pro_chart(chat_id, sym, "1h")

# --- МОНИТОРИНГ ---
async def monitor():
    lp = {}
    while True:
        try:
            conn = sqlite3.connect(DB_PATH); alerts = conn.execute("SELECT id, chat_id, symbol, target, is_persistent FROM alerts").fetchall(); conn.close()
            if alerts:
                syms = list(set([a[2] for a in alerts]))
                tickers = await asyncio.gather(*[exchange.fetch_ticker(s) for s in syms], return_exceptions=True)
                for t in tickers:
                    if isinstance(t, dict):
                        s = t['symbol']; cp = float(t['last'])
                        if s in lp:
                            for aid, cid, sym, tar, per in alerts:
                                if sym == s and ((lp[s] < tar <= cp) or (lp[s] > tar >= cp)):
                                    await bot.send_msg(cid, f"🚨 <b>ПРОБИТИЕ!</b> {s} -> {tar}$")
                                    if not per:
                                        c = sqlite3.connect(DB_PATH); c.execute("DELETE FROM alerts WHERE id=?", (aid,)); c.commit(); c.close()
                        lp[s] = cp
            await asyncio.sleep(CHECK_INTERVAL)
        except: await asyncio.sleep(1)

# --- MESSAGES ---
async def handle_msg(m):
    cid = m["chat"]["id"]; txt = m.get("text", "")
    if txt == "/start": await bot.send_msg(cid, "💎 <b>TERMINAL v5.0</b>", main_kb())
    elif cid in user_states:
        if user_states[cid] == "ADD":
            sym = txt.upper() + "/USDT" if "/" not in txt else txt.upper()
            conn = sqlite3.connect(DB_PATH); conn.execute("INSERT OR IGNORE INTO portfolio VALUES (?,?)", (str(cid), sym)); conn.commit(); conn.close()
            await bot.send_msg(cid, f"✅ {sym} добавлена!"); del user_states[cid]

# --- MAIN ---
async def run():
    await init_db(); asyncio.create_task(monitor()); offset = -1
    while True:
        try:
            res = await bot.request("getUpdates", {"offset": offset, "timeout": 20})
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                if "callback_query" in u: await handle_callback(u["callback_query"])
                if "message" in u: await handle_msg(u["message"])
        except: await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(run())
