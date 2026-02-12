import asyncio
import logging
import sqlite3
import io
import json
import aiohttp
import ccxt.async_support as ccxt
import pandas as pd
import mplfinance as mpf
import matplotlib.pyplot as plt
from datetime import datetime

# --- КОНФИГУРАЦИЯ ---
TOKEN = "8054728348:AAHM1awWcJluyjkLPmxSSCVoP_KzsiqjwP8"
ADMIN_USERNAME = "Nikita_Fomenk"
DB_PATH = "terminal_v6.sqlite"
CHECK_INTERVAL = 0.5

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("CryptoTerminal")

exchange = ccxt.mexc({'enableRateLimit': True, 'options': {'defaultType': 'spot'}})
user_states = {}

# --- БАЗА ДАННЫХ ---
async def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS portfolio (chat_id TEXT, symbol TEXT, UNIQUE(chat_id, symbol))")
    cur.execute("CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, symbol TEXT, target REAL, is_persistent INTEGER DEFAULT 0)")
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
        try:
            if data:
                async with session.post(f"{self.url}/{method}", data=data) as resp: return await resp.json()
            async with session.post(f"{self.url}/{method}", json=payload) as resp: return await resp.json()
        except: return {}

    async def send_msg(self, chat_id, text, keyboard=None):
        return await self.request("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "reply_markup": keyboard})

bot = BotInterface(TOKEN)

# --- АНАЛИТИКА И МОНИТОРИНГ (ВНУТРИ ФАЙЛА) ---
async def get_ai_signals(symbol):
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, '1h', limit=50)
        df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
        delta = df['c'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rsi = 100 - (100 / (1 + (gain / loss))).iloc[-1]
        
        ma20 = df['c'].rolling(20).mean()
        std20 = df['c'].rolling(20).std()
        upper_bb = (ma20 + (std20 * 2)).iloc[-1]
        lower_bb = (ma20 - (std20 * 2)).iloc[-1]
        curr_price = df['c'].iloc[-1]
        
        signal = "⚖️ Нейтрально"
        if rsi < 35 or curr_price <= lower_bb: signal = "🟢 ПОКУПАТЬ (Long)"
        elif rsi > 65 or curr_price >= upper_bb: signal = "🔴 ПРОДАВАТЬ (Short)"
        
        return f"<b>{symbol}</b>\nСигнал: {signal}\nRSI: {rsi:.1f}"
    except: return f"⚠️ {symbol}: Ошибка анализа"

async def monitor_logic():
    last_prices = {}
    while True:
        try:
            conn = sqlite3.connect(DB_PATH)
            alerts = conn.execute("SELECT id, chat_id, symbol, target, is_persistent FROM alerts").fetchall()
            conn.close()
            if alerts:
                symbols = list(set([a[2] for a in alerts]))
                tickers = await asyncio.gather(*[exchange.fetch_ticker(s) for s in symbols], return_exceptions=True)
                for t in tickers:
                    if isinstance(t, dict):
                        s = t['symbol']; cp = float(t['last'])
                        if s in last_prices:
                            for aid, cid, sym, tar, per in alerts:
                                if sym == s and ((last_prices[s] < tar <= cp) or (last_prices[s] > tar >= cp)):
                                    await bot.send_msg(cid, f"🚨 <b>АЛЕРТ!</b>\n{s} пробил уровень {tar}$")
                                    if not per:
                                        c = sqlite3.connect(DB_PATH); c.execute("DELETE FROM alerts WHERE id=?", (aid,)); c.commit(); c.close()
                        last_prices[s] = cp
            await asyncio.sleep(CHECK_INTERVAL)
        except: await asyncio.sleep(1)

# --- ГРАФИКИ ---
async def send_pro_chart(chat_id, symbol, timeframe):
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=100)
        df = pd.DataFrame(ohlcv, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['Date'] = pd.to_datetime(df['Date'], unit='ms')
        df.set_index('Date', inplace=True)
        
        mc = mpf.make_marketcolors(up='#00ff88', down='#ff3355', inherit=True)
        s = mpf.make_mpf_style(marketcolors=mc, gridstyle='--', gridcolor='#333333', facecolor='#0b0e11')
        
        buf = io.BytesIO()
        mpf.plot(df, type='candle', style=s, volume=True, figsize=(12, 7), savefig=dict(fname=buf, format='png'))
        buf.seek(0)
        
        data = aiohttp.FormData()
        data.add_field('chat_id', str(chat_id))
        data.add_field('photo', buf, filename='chart.png')
        data.add_field('caption', f"📊 {symbol} [{timeframe}]")
        await bot.request("sendPhoto", data=data)
    except Exception as e:
        await bot.send_msg(chat_id, f"❌ Ошибка графика: {e}")

# --- КЛАВИАТУРЫ ---
def main_kb():
    return {"inline_keyboard": [
        [{"text": "💰 Текущие цены", "callback_data": "menu_all_prices"}, {"text": "📊 Графики", "callback_data": "menu_charts"}],
        [{"text": "💼 Мои монеты", "callback_data": "menu_port"}, {"text": "🔔 Создать Алерт", "callback_data": "menu_newalert"}],
        [{"text": "📋 Мои Алерты", "callback_data": "menu_myalerts"}, {"text": "🧠 Сигналы AI", "callback_data": "menu_signals"}],
        [{"text": "🎓 КУРСЫ АДМИНА", "url": f"https://t.me/{ADMIN_USERNAME}"}]
    ]}

def tf_kb(symbol):
    tfs = [["1m", "3m", "5m"], ["15m", "30m", "1h"], ["4h", "1d"]]
    kb = [[{"text": t, "callback_data": f"genchart_{symbol}_{t}"} for t in row] for row in tfs]
    kb.append([{"text": "⬅️ Назад", "callback_data": "menu_charts"}])
    return {"inline_keyboard": kb}

# --- ОБРАБОТЧИК CALLBACK ---
async def handle_callback(cb):
    chat_id = cb["message"]["chat"]["id"]; data = cb["data"]; mid = cb["message"]["message_id"]

    if data == "home":
        await bot.request("editMessageText", {"chat_id": chat_id, "message_id": mid, "text": "💎 <b>CRYPTO TERMINAL</b>", "reply_markup": main_kb(), "parse_mode": "HTML"})

    elif data == "menu_all_prices":
        conn = sqlite3.connect(DB_PATH); coins = [r[0] for r in conn.execute("SELECT symbol FROM portfolio WHERE chat_id=?", (str(chat_id),)).fetchall()]; conn.close()
        if not coins:
            await bot.send_msg(chat_id, "❌ Сначала добавьте монеты в портфель.")
        else:
            tickers = await asyncio.gather(*[exchange.fetch_ticker(s) for s in coins])
            msg = "<b>💰 Текущие курсы:</b>\n\n"
            for t in tickers: msg += f"• {t['symbol']}: <code>{t['last']}$</code> ({t['percentage']}%)\n"
            await bot.send_msg(chat_id, msg, {"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "home"}]]})

    elif data == "menu_port":
        conn = sqlite3.connect(DB_PATH); coins = [r[0] for r in conn.execute("SELECT symbol FROM portfolio WHERE chat_id=?", (str(chat_id),)).fetchall()]; conn.close()
        if not coins:
            kb = {"inline_keyboard": [
                [{"text": "BTC", "callback_data": "quick_BTC/USDT"}, {"text": "ETH", "callback_data": "quick_ETH/USDT"}],
                [{"text": "SOL", "callback_data": "quick_SOL/USDT"}, {"text": "TON", "callback_data": "quick_TON/USDT"}],
                [{"text": "✍️ Ввести вручную", "callback_data": "manual_add"}],
                [{"text": "⬅️ Назад", "callback_data": "home"}]
            ]}
            await bot.request("editMessageText", {"chat_id": chat_id, "message_id": mid, "text": "💼 Портфель пуст. Добавьте ТОП монеты:", "reply_markup": kb})
        else:
            kb = [[{"text": f"❌ Удалить {c}", "callback_data": f"del_{c}"}] for c in coins]
            kb.append([{"text": "➕ Добавить монету", "callback_data": "manual_add"}])
            kb.append([{"text": "⬅️ Назад", "callback_data": "home"}])
            await bot.request("editMessageText", {"chat_id": chat_id, "message_id": mid, "text": "💼 <b>Управление портфелем:</b>", "reply_markup": {"inline_keyboard": kb}, "parse_mode": "HTML"})

    elif data.startswith("quick_"):
        sym = data.split("_")[1]
        conn = sqlite3.connect(DB_PATH); conn.execute("INSERT OR IGNORE INTO portfolio VALUES (?,?)", (str(chat_id), sym)); conn.commit(); conn.close()
        cb["data"] = "menu_port"; await handle_callback(cb)

    elif data == "manual_add":
        user_states[chat_id] = "WAIT_ADD"
        await bot.send_msg(chat_id, "✍️ Введите тикер (например SOL):")

    elif data.startswith("del_"):
        sym = data.split("_")[1]
        conn = sqlite3.connect(DB_PATH); conn.execute("DELETE FROM portfolio WHERE chat_id=? AND symbol=?", (str(chat_id), sym)); conn.commit(); conn.close()
        cb["data"] = "menu_port"; await handle_callback(cb)

    elif data == "menu_charts":
        conn = sqlite3.connect(DB_PATH); coins = [r[0] for r in conn.execute("SELECT symbol FROM portfolio WHERE chat_id=?", (str(chat_id),)).fetchall()]; conn.close()
        if not coins: await bot.send_msg(chat_id, "Сначала добавьте монеты!"); return
        kb = [[{"text": c, "callback_data": f"seltf_{c}"}] for c in coins] + [[{"text": "⬅️ Назад", "callback_data": "home"}]]
        await bot.request("editMessageText", {"chat_id": chat_id, "message_id": mid, "text": "Выберите монету для графика:", "reply_markup": {"inline_keyboard": kb}})

    elif data.startswith("seltf_"):
        sym = data.split("_")[1]
        await bot.request("editMessageText", {"chat_id": chat_id, "message_id": mid, "text": f"📈 Таймфрейм для {sym}:", "reply_markup": tf_kb(sym)})

    elif data.startswith("genchart_"):
        _, sym, tf = data.split("_")
        await send_pro_chart(chat_id, sym, tf)

    elif data == "menu_signals":
        conn = sqlite3.connect(DB_PATH); coins = [r[0] for r in conn.execute("SELECT symbol FROM portfolio WHERE chat_id=?", (str(chat_id),)).fetchall()]; conn.close()
        if not coins: await bot.send_msg(chat_id, "Портфель пуст."); return
        for c in coins:
            sig = await get_ai_signals(c)
            await bot.send_msg(chat_id, sig)

# --- СООБЩЕНИЯ ---
async def handle_msg(m):
    cid = m["chat"]["id"]; txt = m.get("text", "")
    if txt == "/start": await bot.send_msg(cid, "💎 <b>CRYPTO TERMINAL v6.1</b>", main_kb())
    elif user_states.get(cid) == "WAIT_ADD":
        sym = txt.upper() + "/USDT" if "/" not in txt else txt.upper()
        conn = sqlite3.connect(DB_PATH); conn.execute("INSERT OR IGNORE INTO portfolio VALUES (?,?)", (str(cid), sym)); conn.commit(); conn.close()
        await bot.send_msg(cid, f"✅ {sym} добавлена!"); del user_states[cid]

# --- ЗАПУСК ---
async def main():
    await init_db()
    asyncio.create_task(monitor_logic())
    offset = -1
    logger.info("Бот запущен...")
    while True:
        try:
            res = await bot.request("getUpdates", {"offset": offset, "timeout": 20})
            for u in res.get("result", []):
                offset = u["update_id"] + 1
                if "callback_query" in u: await handle_callback(u["callback_query"])
                if "message" in u: await handle_msg(u["message"])
        except: await asyncio.sleep(1)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
