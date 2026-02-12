import os
import asyncio
import logging
import sqlite3
import io
import aiohttp
import ccxt.async_support as ccxt
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import json

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("CryptoBot")

# --- НАСТРОЙКИ ---
TOKEN = "8054728348:AAHM1awWcJluyjkLPmxSSCVoP_KzsiqjwP8"  # <--- ВСТАВЬ СЮДА СВОЙ ТОКЕН
DB_PATH = "bot_database.sqlite"
CHECK_INTERVAL = 0.5 

exchange = ccxt.mexc({'enableRateLimit': True})

# --- БАЗА ДАННЫХ ---
async def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY, chat_id TEXT, symbol TEXT, target REAL)")
    conn.commit()
    conn.close()

# --- АСИНХРОННЫЙ ТЕЛЕГРАМ КЛИЕНТ ---
class TelegramBot:
    def __init__(self, token):
        self.url = f"https://api.telegram.org/bot{token}"
        self.session = None

    async def init_session(self):
        if not self.session:
            self.session = aiohttp.ClientSession()

    async def request(self, method, params=None, data=None):
        await self.init_session()
        async with self.session.post(f"{self.url}/{method}", json=params, data=data) as resp:
            return await resp.json()

bot = TelegramBot(TOKEN)

# --- ГЛАВНОЕ МЕНЮ (КНОПКИ) ---
def main_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "💰 Текущие курсы", "callback_data": "get_prices"}],
            [{"text": "📈 График BTC", "callback_data": "chart_BTC/USDT"}, {"text": "📉 График ETH", "callback_data": "chart_ETH/USDT"}],
            [{"text": "📋 Мои алерты", "callback_data": "list_alerts"}]
        ]
    }

# --- МОНИТОРИНГ ---
async def price_monitor_loop():
    last_prices = {}
    while True:
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT id, chat_id, symbol, target FROM alerts")
            alerts = cur.fetchall()
            conn.close()

            if alerts:
                symbols = list(set([a[2] for a in alerts]))
                for symbol in symbols:
                    try:
                        ticker = await exchange.fetch_ticker(symbol)
                        new_price = float(ticker['last'])
                        
                        if symbol in last_prices:
                            old_price = last_prices[symbol]
                            for aid, chat_id, sym, target in alerts:
                                if sym == symbol:
                                    if (old_price < target <= new_price) or (old_price > target >= new_price):
                                        await bot.request("sendMessage", {
                                            "chat_id": chat_id, 
                                            "text": f"🔔 <b>ЦЕЛЬ ДОСТИГНУТА!</b>\n\nМонета: <b>{symbol}</b>\nЦена: <b>{new_price}$</b>",
                                            "parse_mode": "HTML"
                                        })
                                        c = sqlite3.connect(DB_PATH); c.execute("DELETE FROM alerts WHERE id=?", (aid,)); c.commit(); c.close()
                        last_prices[symbol] = new_price
                    except: continue
            
            await asyncio.sleep(CHECK_INTERVAL)
        except Exception as e:
            logger.error(f"Monitor error: {e}")
            await asyncio.sleep(5)

# --- ГРАФИКИ ---
async def send_chart(chat_id, symbol):
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe='1h', limit=40)
        df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        
        plt.style.use('dark_background')
        plt.figure(figsize=(10, 5))
        plt.plot(df['ts'], df['close'], color='#00ff88', linewidth=2)
        plt.fill_between(df['ts'], df['close'], color='#00ff88', alpha=0.1)
        plt.title(f"Market: {symbol} (1h)", fontsize=12, color='#888888')
        plt.grid(True, alpha=0.1)
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        buf.seek(0)
        plt.close()

        data = aiohttp.FormData()
        data.add_field('chat_id', str(chat_id))
        data.add_field('caption', f"📊 График {symbol} (таймфрейм 1ч)")
        data.add_field('photo', buf, filename='chart.png')
        await bot.request("sendPhoto", data=data)
    except:
        await bot.request("sendMessage", {"chat_id": chat_id, "text": "❌ Ошибка при построении графика."})

# --- ОБРАБОТКА ОБНОВЛЕНИЙ ---
async def start_polling():
    offset = -1 
    logger.info("Polling started...")
    
    while True:
        try:
            data = await bot.request("getUpdates", {"offset": offset, "timeout": 20})
            
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                
                # Обработка кнопок (Callback)
                if "callback_query" in update:
                    cb = update["callback_query"]
                    chat_id = cb["message"]["chat"]["id"]
                    call_data = cb["data"]

                    if call_data == "get_prices":
                        msg = "<b>💰 Текущие курсы:</b>\n\n"
                        for s, p in last_prices.items():
                            msg += f"• {s}: <code>{p}$</code>\n"
                        await bot.request("sendMessage", {"chat_id": chat_id, "text": msg, "parse_mode": "HTML"})
                    
                    elif call_data.startswith("chart_"):
                        sym = call_data.replace("chart_", "")
                        await send_chart(chat_id, sym)

                    elif call_data == "list_alerts":
                        conn = sqlite3.connect(DB_PATH); cur = conn.cursor(); cur.execute("SELECT symbol, target FROM alerts WHERE chat_id=?", (str(chat_id),)); rows = cur.fetchall(); conn.close()
                        if not rows:
                            await bot.request("sendMessage", {"chat_id": chat_id, "text": "У вас нет активных алертов."})
                        else:
                            txt = "<b>🔔 Ваши алерты:</b>\n"
                            for r in rows: txt += f"• {r[0]} на уровне {r[1]}$\n"
                            await bot.request("sendMessage", {"chat_id": chat_id, "text": txt, "parse_mode": "HTML"})

                # Обработка текстовых сообщений
                msg = update.get("message")
                if not msg or "text" not in msg: continue
                chat_id = msg["chat"]["id"]
                text = msg["text"].strip()

                if text == "/start":
                    await bot.request("sendMessage", {
                        "chat_id": chat_id, 
                        "text": "👋 <b>Привет! Я твой торговый ассистент.</b>\n\nИспользуй меню ниже или просто напиши монету и цену (например: <code>SOL 145</code>).",
                        "reply_markup": json.dumps(main_keyboard()),
                        "parse_mode": "HTML"
                    })
                
                elif len(text.split()) == 2:
                    try:
                        s, t = text.split(); s = s.upper()
                        if "/" not in s: s += "/USDT"
                        price_target = float(t)
                        conn = sqlite3.connect(DB_PATH); conn.execute("INSERT INTO alerts (chat_id, symbol, target) VALUES (?,?,?)", (str(chat_id), s, price_target)); conn.commit(); conn.close()
                        await bot.request("sendMessage", {"chat_id": chat_id, "text": f"✅ Ок! Я сообщу, когда <b>{s}</b> достигнет <b>{price_target}$</b>", "parse_mode": "HTML"})
                    except: pass

        except Exception as e:
            logger.error(f"Polling error: {e}")
            await asyncio.sleep(5)

# --- ЗАПУСК ---
last_prices = {} # Глобальный словарь для цен

async def main():
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except: pass
    
    await init_db()
    await asyncio.gather(price_monitor_loop(), start_polling())

if __name__ == "__main__":
    asyncio.run(main())
