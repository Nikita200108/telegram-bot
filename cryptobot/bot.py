import os
import asyncio
import logging
import sqlite3
import io
import time
from datetime import datetime

# Библиотеки
import ccxt.async_support as ccxt  # Асинхронная версия CCXT
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import requests  # Для отправки файлов в Telegram (простой способ)

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("CryptoBot")

# --- НАСТРОЙКИ ---
TOKEN = "ВАШ_ТЕЛЕГРАМ_ТОКЕН"  # <--- ВСТАВЬТЕ СВОЙ ТОКЕН
DB_PATH = "bot_database.sqlite"
CHECK_INTERVAL = 0.5  # Частота проверки (0.5 сек = 2 раза в секунду)

# Инициализация биржи (асинхронная)
exchange = ccxt.mexc({'enableRateLimit': True})

# --- БАЗА ДАННЫХ ---
async def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY, chat_id TEXT, symbol TEXT, target REAL)")
    cur.execute("CREATE TABLE IF NOT EXISTS symbols (symbol TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована.")

def get_alerts():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, chat_id, symbol, target FROM alerts")
    data = cur.fetchall()
    conn.close()
    return data

def delete_alert(alert_id):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM alerts WHERE id=?", (alert_id,))
    conn.commit()
    conn.close()

def add_alert_to_db(chat_id, symbol, target):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO alerts (chat_id, symbol, target) VALUES (?, ?, ?)", (chat_id, symbol, target))
    cur.execute("INSERT OR IGNORE INTO symbols (symbol) VALUES (?)", (symbol,))
    conn.commit()
    conn.close()

# --- ТЕЛЕГРАМ API ---
async def send_msg(chat_id, text, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения: {e}")

async def send_chart(chat_id, symbol):
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe='1h', limit=50)
        df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')

        plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(10, 6))
        ax.plot(df['ts'], df['close'], color='#00ff88', linewidth=2, label='Price')
        ax.fill_between(df['ts'], df['close'], color='#00ff88', alpha=0.1)
        
        ax.set_title(f"Market Chart: {symbol}", fontsize=14, color='white', pad=20)
        ax.grid(True, alpha=0.1)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        buf.seek(0)
        plt.close()

        url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
        requests.post(url, data={'chat_id': chat_id, 'caption': f"📊 График {symbol}"}, files={'photo': buf})
    except Exception as e:
        logger.error(f"Ошибка графика: {e}")
        await send_msg(chat_id, "❌ Не удалось загрузить график.")

# --- МОНИТОРИНГ ЦЕН ---
last_prices = {}

async def price_monitor_loop():
    logger.info("Поток мониторинга запущен.")
    while True:
        try:
            alerts = get_alerts()
            if not alerts:
                await asyncio.sleep(2)
                continue

            # Получаем уникальные символы для проверки
            unique_symbols = list(set([a[2] for a in alerts]))
            
            for symbol in unique_symbols:
                ticker = await exchange.fetch_ticker(symbol)
                new_price = float(ticker['last'])
                
                if symbol in last_prices:
                    old_price = last_prices[symbol]
                    
                    # Проверяем каждый алерт для этого символа
                    for aid, chat_id, sym, target in alerts:
                        if sym == symbol:
                            # Пересечение уровня вверх или вниз
                            if (old_price < target <= new_price) or (old_price > target >= new_price):
                                await send_msg(chat_id, f"🚀 <b>ЦЕЛЬ ДОСТИГНУТА!</b>\n{symbol} сейчас <b>{new_price}$</b> (Уровень: {target}$)")
                                delete_alert(aid)

                last_prices[symbol] = new_price
            
            await asyncio.sleep(CHECK_INTERVAL)
        except Exception as e:
            logger.error(f"Ошибка монитора: {e}")
            await asyncio.sleep(5)

# --- ОБРАБОТКА КОМАНД ---
async def start_bot():
    offset = 0
    logger.info("Бот начал опрос сообщений (Polling)...")
    while True:
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={offset}&timeout=20"
            response = requests.get(url, timeout=25).json()
            
            for update in response.get("result", []):
                offset = update["update_id"] + 1
                if "message" not in update: continue
                
                msg = update["message"]
                chat_id = msg["chat"]["id"]
                text = msg.get("text", "")

                if text == "/start":
                    await send_msg(chat_id, "🤖 <b>Я Крипто-Бот.</b>\n\n• Чтобы поставить алерт: <code>BTC 65000</code>\n• Чтобы увидеть график: <code>/chart BTC/USDT</code>")
                
                elif text.startswith("/chart"):
                    parts = text.split()
                    symbol = parts[1].upper() if len(parts) > 1 else "BTC/USDT"
                    if "/" not in symbol: symbol += "/USDT"
                    await send_chart(chat_id, symbol)

                # Логика алертов: "BTC 68000"
                elif len(text.split()) == 2:
                    try:
                        sym, target = text.split()
                        sym = sym.upper()
                        if "/" not in sym: sym += "/USDT"
                        price_target = float(target)
                        
                        add_alert_to_db(chat_id, sym, price_target)
                        await send_msg(chat_id, f"✅ Алерт установлен: <b>{sym}</b> при достижении <b>{price_target}$</b>")
                    except ValueError:
                        continue # Не формат алерта

        except Exception as e:
            logger.error(f"Ошибка Polling: {e}")
            await asyncio.sleep(5)

# --- ГЛАВНЫЙ ЗАПУСК ---
async def main():
    try:
        # Импортируем uvloop внутри, так как он специфичен для контейнера
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        logger.info("Используется uvloop")
    except ImportError:
        pass

    await init_db()
    
    # Запускаем две задачи одновременно
    await asyncio.gather(
        price_monitor_loop(),
        start_bot()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
