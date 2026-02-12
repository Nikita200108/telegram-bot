#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import io
import threading
import logging
import sqlite3
from datetime import datetime
from collections import defaultdict, deque

# Внешние библиотеки
import requests
import ccxt
import pandas as pd
import matplotlib
matplotlib.use("Agg")  # Для работы без графического интерфейса
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

# ---------------- КОНФИГУРАЦИЯ ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("crypto_bot")

DB_PATH = "bot_data.sqlite"
# Вставьте ваш токен здесь или в файл .env
TELEGRAM_TOKEN = "ВАШ_ТОКЕН_ЗДЕСЬ" 

# Интервал проверки (в секундах). 2 сек - оптимально для MEXC.
CHECK_INTERVAL = 2 

# Инициализация биржи
exchange = ccxt.mexc({'enableRateLimit': True})

# ---------------- БАЗА ДАННЫХ ----------------
def get_db_connection():
    return sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)

def init_db():
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS user_symbols (chat_id TEXT, symbol TEXT)")
        cur.execute("CREATE TABLE IF NOT EXISTS alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT, symbol TEXT, target REAL)")
        conn.commit()
        logger.info("База данных готова.")

init_db()

# ---------------- ЛОГИКА ЦЕН И АЛЕРТОВ ----------------
last_prices = {}

def price_monitor_loop():
    """Фоновый поток для мониторинга цен и проверки алертов"""
    logger.info(f"Мониторинг запущен. Интервал: {CHECK_INTERVAL} сек.")
    while True:
        try:
            with get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT DISTINCT symbol FROM user_symbols")
                db_symbols = [r[0] for r in cur.fetchall()]
                
                # Добавляем стандартные пары, если список пуст
                active_symbols = db_symbols if db_symbols else ["BTC/USDT", "ETH/USDT"]

                for sym in active_symbols:
                    try:
                        ticker = exchange.fetch_ticker(sym)
                        new_price = float(ticker['last'])
                        
                        if sym in last_prices:
                            old_price = last_prices[sym]
                            # Проверяем алерты
                            check_alerts_in_db(sym, old_price, new_price)
                        
                        last_prices[sym] = new_price
                    except Exception as e:
                        logger.error(f"Ошибка биржи ({sym}): {e}")
            
            time.sleep(CHECK_INTERVAL)
        except Exception as e:
            logger.exception("Критическая ошибка в потоке цен")
            time.sleep(5)

def check_alerts_in_db(symbol, old_p, new_p):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, chat_id, target FROM alerts WHERE symbol=?", (symbol,))
        alerts = cur.fetchall()
        
        for aid, chat_id, target in alerts:
            # Если цена пересекла уровень в любую сторону
            if (old_p < target <= new_p) or (old_p > target >= new_p):
                msg = f"🔔 <b>ALERT!</b>\n<b>{symbol}</b> достиг цены <b>{target}$</b>\nТекущая: {new_p}$"
                send_msg(chat_id, msg)
                cur.execute("DELETE FROM alerts WHERE id=?", (aid,))
                conn.commit()

# ---------------- ТЕЛЕГРАМ ФУНКЦИИ ----------------
def send_msg(chat_id, text, kb=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if kb: payload["reply_markup"] = kb
    try: requests.post(url, json=payload, timeout=10)
    except: pass

def send_chart(chat_id, symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=40)
        df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')

        plt.figure(figsize=(10, 5))
        plt.plot(df['ts'], df['close'], color='#00ff00', linewidth=2)
        plt.fill_between(df['ts'], df['close'], color='#00ff00', alpha=0.1)
        plt.title(f"График {symbol} (1h)", color='white')
        plt.grid(True, alpha=0.1)
        
        # Темная тема для графика
        plt.gcf().set_facecolor('#1a1a1a')
        plt.gca().set_facecolor('#1a1a1a')
        plt.tick_params(colors='white')

        buf = io.BytesIO()
        plt.savefig(buf, format='png', facecolor='#1a1a1a')
        buf.seek(0)
        plt.close()

        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
        requests.post(url, data={'chat_id': chat_id}, files={'photo': buf}, timeout=20)
    except Exception as e:
        logger.error(f"Ошибка графика: {e}")
        send_msg(chat_id, "❌ Не удалось загрузить график.")

# ---------------- LONG POLLING (ОСНОВНОЙ ЦИКЛ) ----------------
def main_bot():
    offset = 0
    init_db()
    
    # Запуск монитора цен в отдельном потоке
    threading.Thread(target=price_monitor_loop, daemon=True).start()
    
    logger.info("Бот запущен и слушает сообщения...")
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={offset}&timeout=30"
            resp = requests.get(url, timeout=35).json()
            
            if not resp.get("ok"): continue
            
            for update in resp.get("result", []):
                offset = update["update_id"] + 1
                
                if "message" in update:
                    msg = update["message"]
                    chat_id = msg["chat"]["id"]
                    text = msg.get("text", "").strip()

                    if text == "/start":
                        kb = {"inline_keyboard": [
                            [{"text": "💰 Курсы", "callback_data": "get_prices"}, {"text": "📈 График BTC", "callback_data": "chart_btc"}],
                            [{"text": "🔔 Как ставить алерт?", "callback_data": "help_alert"}]
                        ]}
                        send_msg(chat_id, "<b>Крипто-Бот запущен!</b>\n\nЯ проверяю цены каждые 2 секунды.", json.dumps(kb))

                    # Если пользователь прислал "BTC 65000"
                    elif len(text.split()) == 2:
                        try:
                            s, t = text.split()
                            s = s.upper() if "/" in s else f"{s.upper()}/USDT"
                            target = float(t)
                            
                            with get_db_connection() as conn:
                                cur = conn.cursor()
                                # Сохраняем монету в список отслеживания
                                cur.execute("INSERT INTO user_symbols (chat_id, symbol) SELECT ?, ? WHERE NOT EXISTS (SELECT 1 FROM user_symbols WHERE chat_id=? AND symbol=?)", (chat_id, s, chat_id, s))
                                # Ставим алерт
                                cur.execute("INSERT INTO alerts (chat_id, symbol, target) VALUES (?, ?, ?)", (chat_id, s, target))
                                conn.commit()
                            
                            send_msg(chat_id, f"✅ Ок! Сообщу, когда {s} достигнет {target}$")
                        except:
                            send_msg(chat_id, "❌ Ошибка. Пишите: <code>BTC 65000</code>")

                elif "callback_query" in update:
                    cb = update["callback_query"]
                    chat_id = cb["message"]["chat"]["id"]
                    data = cb["data"]

                    if data == "get_prices":
                        txt = "<b>Текущие цены:</b>\n"
                        for s, p in last_prices.items():
                            txt += f"• {s}: <code>{p}$</code>\n"
                        send_msg(chat_id, txt)
                    
                    elif data == "chart_btc":
                        send_chart(chat_id, "BTC/USDT")
                        
                    elif data == "help_alert":
                        send_msg(chat_id, "Чтобы поставить уведомление, просто напишите мне название монеты и цену.\n\nПример: <code>SOL 150</code>")

        except Exception as e:
            logger.error(f"Ошибка Long Polling: {e}")
            time.sleep(5)
async def main():
    """Основная функция запуска"""
    # 1. Инициализируем базу данных
    await init_db()
    
    # 2. Запускаем фоновые задачи (мониторинг цен)
    # Если у вас мониторинг асинхронный:
    asyncio.create_task(price_monitor_loop())
    
    # 3. Запускаем самого бота
    # Здесь должна быть функция, которая запускает получение обновлений
    await start_bot() 

if __name__ == "__main__":
    import asyncio
    try:
        # Это современный и безопасный способ запуска асинхронных программ
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
    except Exception as e:
        logger.error(f"Критическая ошибка при запуске: {e}")