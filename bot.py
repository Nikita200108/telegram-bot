import os
import asyncio
import logging
import sqlite3
import io
import json
import aiohttp
import ccxt.async_support as ccxt
import pandas as pd
import matplotlib.pyplot as plt

# Настройка логов
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("CryptoUltraBot")

# --- КОНФИГУРАЦИЯ ---
TOKEN = "8054728348:AAHM1awWcJluyjkLPmxSSCVoP_KzsiqjwP8"
DB_PATH = "crypto_pro.sqlite"
# Список монет для быстрого выбора в меню
POPULAR_COINS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "TON/USDT", "XRP/USDT"]

exchange = ccxt.mexc({'enableRateLimit': True})

# --- БАЗА ДАННЫХ ---
async def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY, chat_id TEXT, symbol TEXT, target REAL)")
    cur.execute("CREATE TABLE IF NOT EXISTS portfolio (chat_id TEXT, symbol TEXT, UNIQUE(chat_id, symbol))")
    conn.commit()
    conn.close()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
class BotInterface:
    def __init__(self, token):
        self.url = f"https://api.telegram.org/bot{token}"
        self.session = None

    async def get_session(self):
        if not self.session: self.session = aiohttp.ClientSession()
        return self.session

    async def send_request(self, method, payload):
        session = await self.get_session()
        async with session.post(f"{self.url}/{method}", json=payload) as resp:
            return await resp.json()

bot_api = BotInterface(TOKEN)

# --- КЛАВИАТУРЫ ---
def main_menu():
    return {
        "inline_keyboard": [
            [{"text": "💰 Курсы (Выбор)", "callback_data": "menu_prices"}, {"text": "📈 Графики", "callback_data": "menu_charts"}],
            [{"text": "🔔 Добавить Алерт", "callback_data": "menu_add_alert"}, {"text": "💼 Мои монеты", "callback_data": "menu_portfolio"}],
            [{"text": "🚀 Авто-сигналы (On/Off)", "callback_data": "menu_signals"}]
        ]
    }

def coin_selection_menu(prefix):
    keyboard = []
    for i in range(0, len(POPULAR_COINS), 2):
        row = [
            {"text": POPULAR_COINS[i], "callback_data": f"{prefix}_{POPULAR_COINS[i]}"},
            {"text": POPULAR_COINS[i+1], "callback_data": f"{prefix}_{POPULAR_COINS[i+1]}"} if i+1 < len(POPULAR_COINS) else None
        ]
        keyboard.append([btn for btn in row if btn])
    keyboard.append([{"text": "⬅️ Назад", "callback_data": "main_menu"}])
    return {"inline_keyboard": keyboard}

# --- МОНИТОРИНГ И СИГНАЛЫ ---
async def monitor_logic():
    last_prices = {}
    while True:
        try:
            conn = sqlite3.connect(DB_PATH); cur = conn.cursor()
            cur.execute("SELECT id, chat_id, symbol, target FROM alerts"); alerts = cur.fetchall()
            conn.close()

            # Собираем все монеты для проверки (алерты + популярные для сигналов)
            all_syms = list(set([a[2] for a in alerts] + POPULAR_COINS))
            
            for sym in all_syms:
                ticker = await exchange.fetch_ticker(sym)
                current_price = float(ticker['last'])
                
                if sym in last_prices:
                    old_price = last_prices[sym]
                    # 1. Проверка алертов
                    for aid, chat_id, symbol, target in alerts:
                        if symbol == sym:
                            if (old_price < target <= current_price) or (old_price > target >= current_price):
                                await bot_api.send_request("sendMessage", {"chat_id": chat_id, "text": f"🔔 <b>ALERT: {sym}</b> достиг {target}$!" , "parse_mode": "HTML"})
                                c = sqlite3.connect(DB_PATH); c.execute("DELETE FROM alerts WHERE id=?", (aid,)); c.commit(); c.close()
                    
                    # 2. Авто-сигналы (резкое изменение > 1% за цикл)
                    change = ((current_price - old_price) / old_price) * 100
                    if abs(change) >= 1.5:
                        direction = "🚀 Памп" if change > 0 else "🔻 Дамп"
                        # В реальности тут нужен фильтр пользователей, подписанных на сигналы
                        logger.info(f"Сигнал: {sym} {direction} {change:.2f}%")

                last_prices[sym] = current_price
            await asyncio.sleep(2)
        except: await asyncio.sleep(5)

# --- ОБРАБОТКА ОБНОВЛЕНИЙ ---
async def run_bot():
    offset = -1
    await init_db()
    asyncio.create_task(monitor_logic())
    logger.info("Бот запущен...")

    while True:
        try:
            updates = await bot_api.send_request("getUpdates", {"offset": offset, "timeout": 20})
            for upd in updates.get("result", []):
                offset = upd["update_id"] + 1
                
                if "callback_query" in upd:
                    cb = upd["callback_query"]; chat_id = cb["message"]["chat"]["id"]; data = cb["data"]
                    
                    if data == "main_menu":
                        await bot_api.send_request("sendMessage", {"chat_id": chat_id, "text": "Главное меню:", "reply_markup": main_menu()})
                    
                    elif data == "menu_prices":
                        await bot_api.send_request("sendMessage", {"chat_id": chat_id, "text": "Выберите монету для курса:", "reply_markup": coin_selection_menu("price")})
                    
                    elif data.startswith("price_"):
                        sym = data.replace("price_", ""); tick = await exchange.fetch_ticker(sym)
                        await bot_api.send_request("sendMessage", {"chat_id": chat_id, "text": f"💰 Цена {sym}: <b>{tick['last']}$</b>", "parse_mode": "HTML"})

                    elif data == "menu_portfolio":
                        conn = sqlite3.connect(DB_PATH); cur = conn.cursor()
                        cur.execute("SELECT symbol FROM portfolio WHERE chat_id=?", (str(chat_id),)); coins = cur.fetchall()
                        conn.close()
                        txt = "💼 Ваши монеты:\n" + ("\n".join([f"• {c[0]}" for c in coins]) if coins else "Пусто")
                        kb = {"inline_keyboard": [[{"text": "➕ Добавить", "callback_data": "menu_prices"}], [{"text": "⬅️ Назад", "callback_data": "main_menu"}]]}
                        await bot_api.send_request("sendMessage", {"chat_id": chat_id, "text": txt, "reply_markup": kb})

                if "message" in upd and "text" in upd["message"]:
                    msg = upd["message"]; chat_id = msg["chat"]["id"]; text = msg["text"]
                    if text == "/start":
                        await bot_api.send_request("sendMessage", {"chat_id": chat_id, "text": "💎 <b>CRYPTO PRO TERMINAL</b>\nДобро пожаловать!", "reply_markup": main_menu(), "parse_mode": "HTML"})

        except Exception as e:
            logger.error(f"Error: {e}"); await asyncio.sleep(2)

if __name__ == "__main__":
    asyncio.run(run_bot())
