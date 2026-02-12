import os
import asyncio
import logging
import sqlite3
import io
import aiohttp  # Асинхронные запросы
import ccxt.async_support as ccxt
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("CryptoBot")

# --- НАСТРОЙКИ ---
TOKEN = "8054728348:AAHM1awWcJluyjkLPmxSSCVoP_KzsiqjwP8"  # ОБЯЗАТЕЛЬНО ПРОВЕРЬТЕ ТОКЕН
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

    async def send_msg(self, chat_id, text):
        await self.init_session()
        try:
            async with self.session.post(f"{self.url}/sendMessage", json={
                "chat_id": chat_id, "text": text, "parse_mode": "HTML"
            }) as resp:
                return await resp.json()
        except Exception as e:
            logger.error(f"Send error: {e}")

    async def send_photo(self, chat_id, buf, caption):
        await self.init_session()
        data = aiohttp.FormData()
        data.add_field('chat_id', str(chat_id))
        data.add_field('caption', caption)
        data.add_field('photo', buf, filename='chart.png')
        try:
            async with self.session.post(f"{self.url}/sendPhoto", data=data) as resp:
                return await resp.json()
        except Exception as e:
            logger.error(f"Photo error: {e}")

bot = TelegramBot(TOKEN)

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
                    ticker = await exchange.fetch_ticker(symbol)
                    new_price = float(ticker['last'])
                    
                    if symbol in last_prices:
                        old_price = last_prices[symbol]
                        for aid, chat_id, sym, target in alerts:
                            if sym == symbol:
                                if (old_price < target <= new_price) or (old_price > target >= new_price):
                                    await bot.send_msg(chat_id, f"🔔 <b>ЦЕЛЬ!</b> {symbol}: {new_price}$")
                                    c = sqlite3.connect(DB_PATH); c.execute("DELETE FROM alerts WHERE id=?", (aid,)); c.commit(); c.close()
                    last_prices[symbol] = new_price
            
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
        plt.plot(df['ts'], df['close'], color='#00ff88')
        plt.grid(alpha=0.2)
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        buf.seek(0)
        plt.close()
        await bot.send_photo(chat_id, buf, f"📊 {symbol}")
    except:
        await bot.send_msg(chat_id, "❌ Ошибка графика")

# --- ОБРАБОТКА ОБНОВЛЕНИЙ ---
async def start_polling():
    offset = -1 # Очищаем старые сообщения при запуске
    logger.info("Polling started...")
    await bot.init_session()
    
    while True:
        try:
            async with bot.session.get(f"{bot.url}/getUpdates", params={"offset": offset, "timeout": 20}) as resp:
                data = await resp.json()
                
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    msg = update.get("message")
                    if not msg or "text" not in msg: continue
                    
                    chat_id = msg["chat"]["id"]
                    text = msg["text"].strip()

                    if text == "/start":
                        await bot.send_msg(chat_id, "✅ <b>Бот работает!</b>\n\nПиши: <code>BTC 70000</code>\nИли: <code>/chart BTC/USDT</code>")
                    
                    elif text.startswith("/chart"):
                        sym = text.split()[1].upper() if len(text.split()) > 1 else "BTC/USDT"
                        if "/" not in sym: sym += "/USDT"
                        await send_chart(chat_id, sym)
                    
                    elif len(text.split()) == 2:
                        try:
                            s, t = text.split(); s = s.upper()
                            if "/" not in s: s += "/USDT"
                            conn = sqlite3.connect(DB_PATH); conn.execute("INSERT INTO alerts (chat_id, symbol, target) VALUES (?,?,?)", (chat_id, s, float(t))); conn.commit(); conn.close()
                            await bot.send_msg(chat_id, f"🎯 Слежу за {s} на уровне {t}")
                        except: pass
        except Exception as e:
            logger.error(f"Polling error: {e}")
            await asyncio.sleep(5)

# --- ЗАПУСК ---
async def main():
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except: pass
    
    await init_db()
    await asyncio.gather(price_monitor_loop(), start_polling())

if __name__ == "__main__":
    asyncio.run(main())


