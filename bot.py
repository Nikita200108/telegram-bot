import asyncio
import logging
import sqlite3
import io
import json
import aiohttp
import ccxt.async_support as ccxt
import pandas as pd
import mplfinance as mpf
from datetime import datetime

# --- КОНФИГУРАЦИЯ ---
TOKEN = "8054728348:AAHM1awWcJluyjkLPmxSSCVoP_KzsiqjwP8"
ADMIN_USERNAME = "Nikita_Fomenk"
DB_PATH = "terminal_v6.sqlite"
CHECK_INTERVAL = 0.3

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

# --- КЛАВИАТУРЫ ---
def main_kb():
    return {"inline_keyboard": [
        [{"text": "💰 Текущие цены", "callback_data": "menu_all_prices"}, {"text": "📊 Графики", "callback_data": "menu_charts"}],
        [{"text": "💼 Мои монеты", "callback_data": "menu_port"}, {"text": "🔔 Создать Алерт", "callback_data": "menu_newalert"}],
        [{"text": "📋 Мои Алерты", "callback_data": "menu_myalerts"}, {"text": "🧠 Сигналы AI", "callback_data": "menu_signals"}],
        [{"text": "🎓 КУРСЫ АДМИНА", "url": f"https://t.me/{ADMIN_USERNAME}"}]
    ]}

def tf_kb(symbol):
    # Сетка таймфреймов
    tfs = [["1m", "3m", "5m"], ["10m", "15m", "30m"], ["1h", "4h", "1d"]]
    keyboard = [[{"text": t, "callback_data": f"genchart_{symbol}_{t}"} for t in row] for row in tfs]
    keyboard.append([{"text": "⬅️ Назад", "callback_data": "menu_charts"}])
    return {"inline_keyboard": keyboard}

# --- ЛОГИКА ---
async def get_portfolio(chat_id):
    conn = sqlite3.connect(DB_PATH); cur = conn.cursor()
    cur.execute("SELECT symbol FROM portfolio WHERE chat_id=?", (str(chat_id),))
    coins = [r[0] for r in cur.fetchall()]; conn.close()
    return coins

async def handle_callback(cb):
    chat_id = cb["message"]["chat"]["id"]; data = cb["data"]; mid = cb["message"]["message_id"]

    if data == "home":
        await bot.request("editMessageText", {"chat_id": chat_id, "message_id": mid, "text": "💎 <b>CRYPTO TERMINAL</b>", "reply_markup": main_kb(), "parse_mode": "HTML"})

    elif data == "menu_all_prices":
        coins = await get_portfolio(chat_id)
        if not coins:
            await bot.send_msg(chat_id, "❌ Список пуст. Добавьте монеты в '💼 Мои монеты'.")
            return
        msg = "<b>💰 Текущие курсы:</b>\n\n"
        tickers = await asyncio.gather(*[exchange.fetch_ticker(s) for s in coins])
        for t in tickers:
            msg += f"• {t['symbol']}: <code>{t['last']}$</code> ({t['percentage']}%)\n"
        await bot.send_msg(chat_id, msg, {"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "home"}]]})

    elif data == "menu_port":
        coins = await get_portfolio(chat_id)
        if not coins:
            kb = {"inline_keyboard": [
                [{"text": "BTC", "callback_data": "quick_BTC/USDT"}, {"text": "ETH", "callback_data": "quick_ETH/USDT"}],
                [{"text": "SOL", "callback_data": "quick_SOL/USDT"}, {"text": "TON", "callback_data": "quick_TON/USDT"}],
                [{"text": "BNB", "callback_data": "quick_BNB/USDT"}],
                [{"text": "✍️ Ввести вручную", "callback_data": "manual_add"}],
                [{"text": "⬅️ Назад", "callback_data": "home"}]
            ]}
            await bot.request("editMessageText", {"chat_id": chat_id, "message_id": mid, "text": "💼 <b>Ваш портфель пуст.</b>\nВыберите из списка или введите свою:", "reply_markup": kb, "parse_mode": "HTML"})
        else:
            kb = [[{"text": f"❌ Удалить {c}", "callback_data": f"del_{c}"}] for c in coins]
            kb.append([{"text": "➕ Добавить монету", "callback_data": "manual_add"}])
            kb.append([{"text": "⬅️ Назад", "callback_data": "home"}])
            await bot.request("editMessageText", {"chat_id": chat_id, "message_id": mid, "text": "💼 <b>Управление монетами:</b>", "reply_markup": {"inline_keyboard": kb}, "parse_mode": "HTML"})

    elif data.startswith("quick_"):
        sym = data.split("_")[1]
        conn = sqlite3.connect(DB_PATH); conn.execute("INSERT OR IGNORE INTO portfolio VALUES (?,?)", (str(chat_id), sym)); conn.commit(); conn.close()
        await bot.send_msg(chat_id, f"✅ {sym} добавлена!"); await asyncio.sleep(0.5)
        cb["data"] = "menu_port"; await handle_callback(cb)

    elif data == "manual_add":
        user_states[chat_id] = "WAIT_ADD"
        await bot.send_msg(chat_id, "✍️ Введите тикер (например: SOL):", {"inline_keyboard": [[{"text": "Отмена", "callback_data": "menu_port"}]]})

    elif data.startswith("del_"):
        sym = data.split("_")[1]
        conn = sqlite3.connect(DB_PATH); conn.execute("DELETE FROM portfolio WHERE chat_id=? AND symbol=?", (str(chat_id), sym)); conn.commit(); conn.close()
        cb["data"] = "menu_port"; await handle_callback(cb)

    elif data == "menu_charts":
        coins = await get_portfolio(chat_id)
        if not coins: await bot.send_msg(chat_id, "Добавьте монеты в портфель!"); return
        kb = [[{"text": c, "callback_data": f"seltf_{c}"}] for c in coins] + [[{"text": "⬅️ Назад", "callback_data": "home"}]]
        await bot.request("editMessageText", {"chat_id": chat_id, "message_id": mid, "text": "Выберите монету для графика:", "reply_markup": {"inline_keyboard": kb}})

    elif data.startswith("seltf_"):
        sym = data.split("_")[1]
        await bot.request("editMessageText", {"chat_id": chat_id, "message_id": mid, "text": f"📈 Таймфрейм для {sym}:", "reply_markup": tf_kb(sym)})

    elif data.startswith("genchart_"):
        _, sym, tf = data.split("_")
        from bot_logic import send_pro_chart # Предполагаем наличие функции из v5
        await send_pro_chart(chat_id, sym, tf)

# --- ГРАФИКИ (ПЕРЕНЕСЕНО ДЛЯ ЦЕЛОСТНОСТИ) ---
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
        data.add_field('chat_id', str(chat_id)); data.add_field('photo', buf, filename='c.png'); data.add_field('caption', f"📊 {symbol} [{timeframe}]")
        await bot.request("sendPhoto", data=data)
    except: await bot.send_msg(chat_id, "❌ Ошибка графика. Проверьте тикер.")

# --- МОНИТОРИНГ И ОБРАБОТКА ТЕКСТА ---
async def handle_msg(m):
    cid = m["chat"]["id"]; txt = m.get("text", "")
    if txt == "/start": await bot.send_msg(cid, "💎 <b>TERMINAL v6.0</b>", main_kb())
    elif user_states.get(cid) == "WAIT_ADD":
        sym = txt.upper() + "/USDT" if "/" not in txt else txt.upper()
        conn = sqlite3.connect(DB_PATH); conn.execute("INSERT OR IGNORE INTO portfolio VALUES (?,?)", (str(cid), sym)); conn.commit(); conn.close()
        await bot.send_msg(cid, f"✅ {sym} добавлена!", {"inline_keyboard": [[{"text": "💼 В портфель", "callback_data": "menu_port"}]]})
        del user_states[cid]

async def run():
    await init_db(); offset = -1
    # Запуск монитора цен (из v5) отдельной задачей
    from bot_logic import monitor 
    asyncio.create_task(monitor()) 
    
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
