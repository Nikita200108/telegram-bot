import os
import asyncio
import logging
import sqlite3
import io
import json
import aiohttp
import ccxt.async_support as ccxt
import pandas as pd
# Строка с pandas_core удалена, она вызывала ошибку
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np

# --- КОНФИГУРАЦИЯ ---
TOKEN = "8054728348:AAHM1awWcJluyjkLPmxSSCVoP_KzsiqjwP8" 
ADMIN_USERNAME = "Nikita_Fomenk" # Без @, например: durov
DB_PATH = "terminal_v3.sqlite"
CHECK_INTERVAL = 1  # Частота проверки (сек)

# Настройка логов
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger("CryptoBot")

exchange = ccxt.mexc({'enableRateLimit': True})

# Глобальные переменные состояния
user_states = {} 

# --- БАЗА ДАННЫХ ---
async def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS portfolio (chat_id TEXT, symbol TEXT, UNIQUE(chat_id, symbol))")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            chat_id TEXT, 
            symbol TEXT, 
            target REAL, 
            condition TEXT,
            is_persistent INTEGER DEFAULT 0
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
        if not self.session: self.session = aiohttp.ClientSession()
        return self.session

    async def request(self, method, payload):
        session = await self.get_session()
        try:
            async with session.post(f"{self.url}/{method}", json=payload) as resp:
                return await resp.json()
        except: return {}

    async def send_msg(self, chat_id, text, keyboard=None):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if keyboard: payload["reply_markup"] = keyboard
        await self.request("sendMessage", payload)

    async def send_photo(self, chat_id, photo_buf, caption, keyboard=None):
        session = await self.get_session()
        data = aiohttp.FormData()
        data.add_field('chat_id', str(chat_id))
        data.add_field('caption', caption)
        data.add_field('parse_mode', 'HTML')
        data.add_field('photo', photo_buf, filename='chart.png')
        if keyboard: data.add_field('reply_markup', json.dumps(keyboard))
        await session.post(f"{self.url}/sendPhoto", data=data)

bot = BotInterface(TOKEN)

# --- ЛОГИКА АНАЛИЗА (RSI) ---
def calculate_rsi(prices, period=14):
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

# --- КЛАВИАТУРЫ ---
def main_menu():
    return {
        "inline_keyboard": [
            [{"text": "💰 Цена", "callback_data": "menu_price"}, {"text": "📊 Графики", "callback_data": "menu_charts"}],
            [{"text": "💼 Мои монеты", "callback_data": "menu_portfolio"}, {"text": "🔔 Создать Алерт", "callback_data": "menu_create_alert"}],
            [{"text": "📋 Управление Алертами", "callback_data": "menu_my_alerts"}],
            [{"text": "🧠 Авто-сигналы", "callback_data": "menu_signals"}, {"text": "🎓 КУПИТЬ КУРСЫ", "url": f"https://t.me/{ADMIN_USERNAME}"}]
        ]
    }

def back_btn(to="main_menu"):
    return {"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": to}]]}

def dynamic_coin_keyboard(chat_id, action_prefix):
    conn = sqlite3.connect(DB_PATH); cur = conn.cursor()
    cur.execute("SELECT symbol FROM portfolio WHERE chat_id=?", (str(chat_id),))
    coins = [r[0] for r in cur.fetchall()]
    conn.close()
    
    if not coins: return None
    
    kb = []
    for i in range(0, len(coins), 2):
        row = [{"text": coins[i], "callback_data": f"{action_prefix}_{coins[i]}"}]
        if i+1 < len(coins):
            row.append({"text": coins[i+1], "callback_data": f"{action_prefix}_{coins[i+1]}"})
        kb.append(row)
    kb.append([{"text": "⬅️ Назад", "callback_data": "main_menu"}])
    return {"inline_keyboard": kb}

# --- ОСНОВНОЙ ЦИКЛ ОБНОВЛЕНИЙ ---
async def start_polling():
    offset = -1
    logger.info("Бот запущен...")
    
    while True:
        try:
            updates = await bot.request("getUpdates", {"offset": offset, "timeout": 10})
            if not updates or "result" not in updates:
                continue

            for upd in updates["result"]:
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    await handle_callback(upd["callback_query"])
                elif "message" in upd and "text" in upd["message"]:
                    await handle_message(upd["message"])

        except Exception as e:
            logger.error(f"Polling error: {e}")
            await asyncio.sleep(2)

# --- ОБРАБОТЧИК КНОПОК ---
async def handle_callback(cb):
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    data = cb["data"]
    
    if data == "main_menu":
        await bot.request("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": "💎 <b>Главный Терминал</b>\nВыберите действие:", "reply_markup": main_menu(), "parse_mode": "HTML"})

    elif data == "menu_price":
        kb = dynamic_coin_keyboard(chat_id, "getprice")
        if not kb:
            await bot.send_msg(chat_id, "⚠️ Ваш список монет пуст. Зайдите в '💼 Мои монеты' и добавьте пару.", back_btn())
        else:
            await bot.request("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": "Выберите монету для просмотра цены:", "reply_markup": kb})

    elif data.startswith("getprice_"):
        sym = data.split("_")[1]
        try:
            tick = await exchange.fetch_ticker(sym)
            p = tick['last']
            perc = tick['percentage']
            await bot.send_msg(chat_id, f"💰 <b>{sym}</b>\nЦена: <code>{p}$</code>\nИзм. 24ч: {perc:.2f}%")
        except:
            await bot.send_msg(chat_id, "❌ Ошибка получения цены.")

    elif data == "menu_portfolio":
        conn = sqlite3.connect(DB_PATH); cur = conn.cursor()
        cur.execute("SELECT symbol FROM portfolio WHERE chat_id=?", (str(chat_id),))
        coins = [r[0] for r in cur.fetchall()]
        conn.close()
        
        txt = "💼 <b>Ваш Портфель:</b>\n" + ("\n".join([f"• {c}" for c in coins]) if coins else "Пусто")
        kb = {"inline_keyboard": [
            [{"text": "➕ Добавить монету", "callback_data": "port_add"}, {"text": "➖ Удалить монету", "callback_data": "port_del"}],
            [{"text": "⬅️ Назад", "callback_data": "main_menu"}]
        ]}
        await bot.request("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": txt, "reply_markup": kb, "parse_mode": "HTML"})

    elif data == "port_add":
        user_states[chat_id] = "WAITING_COIN_ADD"
        await bot.send_msg(chat_id, "✍️ Напишите тикер монеты (например: <code>BTC</code> или <code>TON</code>):")

    elif data == "port_del":
        kb = dynamic_coin_keyboard(chat_id, "delcoin")
        if not kb: 
            await bot.send_msg(chat_id, "Нечего удалять.", back_btn("menu_portfolio"))
        else:
            await bot.request("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": "Выберите монету для удаления:", "reply_markup": kb})
            
    elif data.startswith("delcoin_"):
        sym = data.split("_")[1]
        conn = sqlite3.connect(DB_PATH); conn.execute("DELETE FROM portfolio WHERE chat_id=? AND symbol=?", (str(chat_id), sym)); conn.commit(); conn.close()
        await bot.send_msg(chat_id, f"🗑 {sym} удалена из портфеля.")
        await handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": "menu_portfolio"})

    elif data == "menu_create_alert":
        kb = dynamic_coin_keyboard(chat_id, "newalert")
        if not kb: await bot.send_msg(chat_id, "Сначала добавьте монеты в портфель!", back_btn()); return
        await bot.request("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": "На какую монету ставим алерт?", "reply_markup": kb})

    elif data.startswith("newalert_"):
        sym = data.split("_")[1]
        kb = {"inline_keyboard": [
            [{"text": "✍️ Ввести цену вручную", "callback_data": f"setalert_manual_{sym}"}],
            [{"text": "🔢 Изменение в %", "callback_data": f"setalert_percent_{sym}"}],
            [{"text": "⬅️ Отмена", "callback_data": "menu_create_alert"}]
        ]}
        await bot.request("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": f"🔔 Настройка алерта для <b>{sym}</b>", "reply_markup": kb, "parse_mode": "HTML"})

    elif data.startswith("setalert_manual_"):
        sym = data.split("_")[2]
        user_states[chat_id] = f"WAITING_PRICE_MANUAL_{sym}"
        await bot.send_msg(chat_id, f"Напишите точную цену для {sym} (например: <code>65000.5</code>):")

    elif data.startswith("setalert_percent_"):
        sym = data.split("_")[2]
        user_states[chat_id] = f"WAITING_PERCENT_{sym}"
        await bot.send_msg(chat_id, f"Напишите процент изменения для {sym} (например: <code>5</code> для +5% или <code>-3</code> для -3%):")

    elif data == "menu_my_alerts":
        conn = sqlite3.connect(DB_PATH); cur = conn.cursor()
        cur.execute("SELECT id, symbol, target, is_persistent FROM alerts WHERE chat_id=?", (str(chat_id),))
        rows = cur.fetchall()
        conn.close()

        if not rows:
            await bot.request("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": "🔕 Активных алертов нет.", "reply_markup": back_btn()})
        else:
            kb_list = []
            txt = "<b>📋 Ваши активные алерты:</b>\n"
            for r in rows:
                status = "🔄 Пост." if r[3] else "1️⃣ Раз."
                txt += f"ID:{r[0]} | {r[1]} -> {r[2]}$ ({status})\n"
                kb_list.append([
                    {"text": f"❌ Удалить ID {r[0]}", "callback_data": f"delalert_{r[0]}"},
                    {"text": f"Сделать {'1 раз' if r[3] else 'Пост.'}", "callback_data": f"togglealert_{r[0]}"}
                ])
            kb_list.append([{"text": "⬅️ Назад", "callback_data": "main_menu"}])
            await bot.request("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": txt, "reply_markup": {"inline_keyboard": kb_list}, "parse_mode": "HTML"})

    elif data.startswith("delalert_"):
        aid = data.split("_")[1]
        conn = sqlite3.connect(DB_PATH); conn.execute("DELETE FROM alerts WHERE id=?", (aid,)); conn.commit(); conn.close()
        await handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": "menu_my_alerts"})

    elif data.startswith("togglealert_"):
        aid = data.split("_")[1]
        conn = sqlite3.connect(DB_PATH); cur = conn.cursor()
        cur.execute("UPDATE alerts SET is_persistent = NOT is_persistent WHERE id=?", (aid,))
        conn.commit(); conn.close()
        await handle_callback({"message": {"chat": {"id": chat_id}, "message_id": msg_id}, "data": "menu_my_alerts"})

    elif data == "menu_charts":
        kb = dynamic_coin_keyboard(chat_id, "selectchart")
        if not kb: await bot.send_msg(chat_id, "Добавьте монеты в портфель, чтобы видеть графики.", back_btn())
        else: await bot.request("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": "Выберите монету для графика:", "reply_markup": kb})

    elif data.startswith("selectchart_"):
        sym = data.split("_")[1]
        tf_kb = {"inline_keyboard": [
            [{"text": "15m", "callback_data": f"genchart_{sym}_15m"}, {"text": "1h", "callback_data": f"genchart_{sym}_1h"}],
            [{"text": "4h", "callback_data": f"genchart_{sym}_4h"}, {"text": "1d", "callback_data": f"genchart_{sym}_1d"}],
            [{"text": "⬅️ Назад", "callback_data": "menu_charts"}]
        ]}
        await bot.request("editMessageText", {"chat_id": chat_id, "message_id": msg_id, "text": f"📈 Выберите таймфрейм для {sym}:", "reply_markup": tf_kb})

    elif data.startswith("genchart_"):
        parts = data.split("_")
        sym = parts[1]
        tf = parts[2]
        await generate_and_send_chart(chat_id, sym, tf)

    elif data == "menu_signals":
        report = await auto_signal_check(chat_id)
        await bot.send_msg(chat_id, report, back_btn())

# --- ГЕНЕРАЦИЯ ГРАФИКА ---
async def generate_and_send_chart(chat_id, symbol, timeframe):
    try:
        ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=60)
        df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'vol'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')

        plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.plot(df['ts'], df['close'], color='#00ff88', lw=2)
        ax.fill_between(df['ts'], df['close'], alpha=0.1, color='#00ff88')
        ax.set_title(f"{symbol} ({timeframe}) - Futures Style", color='white', pad=20)
        ax.grid(alpha=0.2)
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight')
        buf.seek(0)
        plt.close()
        
        await bot.send_photo(chat_id, buf, f"📈 График {symbol} ({timeframe})")
    except Exception as e:
        logger.error(f"Chart error: {e}")
        await bot.send_msg(chat_id, "❌ Ошибка загрузки графика.")

# --- ОБРАБОТЧИК СООБЩЕНИЙ ---
async def handle_message(msg):
    chat_id = msg["chat"]["id"]
    text = msg["text"].strip()
    
    state = user_states.get(chat_id)
    
    if state == "WAITING_COIN_ADD":
        sym = text.upper()
        if "/" not in sym: sym += "/USDT"
        try:
            await exchange.fetch_ticker(sym)
            conn = sqlite3.connect(DB_PATH)
            conn.execute("INSERT OR IGNORE INTO portfolio (chat_id, symbol) VALUES (?, ?)", (str(chat_id), sym))
            conn.commit(); conn.close()
            await bot.send_msg(chat_id, f"✅ {sym} добавлен в портфель!", back_btn("menu_portfolio"))
            del user_states[chat_id]
        except:
            await bot.send_msg(chat_id, "❌ Не нашел такой монеты. Попробуйте снова (например BTC).")

    elif state and state.startswith("WAITING_PRICE_MANUAL_"):
        sym = state.split("_")[3]
        try:
            target = float(text)
            conn = sqlite3.connect(DB_PATH)
            conn.execute("INSERT INTO alerts (chat_id, symbol, target, condition, is_persistent) VALUES (?, ?, ?, ?, ?)", 
                         (str(chat_id), sym, target, "CROSS", 0))
            conn.commit(); conn.close()
            await bot.send_msg(chat_id, f"🔔 Алерт на {sym} (Цена: {target}$) установлен!", back_btn("menu_my_alerts"))
            del user_states[chat_id]
        except ValueError:
            await bot.send_msg(chat_id, "❌ Введите число (например 65000.5).")

    elif state and state.startswith("WAITING_PERCENT_"):
        sym = state.split("_")[2]
        try:
            percent = float(text)
            ticker = await exchange.fetch_ticker(sym)
            curr = float(ticker['last'])
            target = curr * (1 + percent/100)
            
            conn = sqlite3.connect(DB_PATH)
            conn.execute("INSERT INTO alerts (chat_id, symbol, target, condition, is_persistent) VALUES (?, ?, ?, ?, ?)", 
                         (str(chat_id), sym, target, "CROSS", 0))
            conn.commit(); conn.close()
            
            await bot.send_msg(chat_id, f"🔔 Алерт установлен!\nТекущая: {curr}\nЦель ({percent}%): {target:.2f}", back_btn("menu_my_alerts"))
            del user_states[chat_id]
        except ValueError:
            await bot.send_msg(chat_id, "❌ Введите число (например 5 или -10).")

    elif text == "/start":
        await bot.send_msg(chat_id, "🚀 <b>CRYPTO TERMINAL V3</b>\n\nДобро пожаловать в профессиональный инструмент трейдера.", main_menu())

# --- МОНИТОРИНГ ЦЕН ---
async def price_monitor_loop():
    last_prices = {}
    while True:
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT id, chat_id, symbol, target, is_persistent FROM alerts")
            alerts = cur.fetchall()
            conn.close()

            if alerts:
                symbols = list(set([a[2] for a in alerts]))
                for sym in symbols:
                    ticker = await exchange.fetch_ticker(sym)
                    curr_price = float(ticker['last'])
                    
                    if sym in last_prices:
                        old_price = last_prices[sym]
                        for aid, chat_id, s, target, persist in alerts:
                            if s == sym:
                                if (old_price < target <= curr_price) or (old_price > target >= curr_price):
                                    await bot.send_msg(chat_id, f"🚨 <b>СИГНАЛ!</b>\n{sym} пробил уровень <b>{target}$</b>\nТекущая цена: {curr_price}$")
                                    if not persist:
                                        c = sqlite3.connect(DB_PATH)
                                        c.execute("DELETE FROM alerts WHERE id=?", (aid,))
                                        c.commit(); c.close()
                    last_prices[sym] = curr_price
            await asyncio.sleep(CHECK_INTERVAL)
        except Exception as e:
            logger.error(f"Monitor error: {e}")
            await asyncio.sleep(5)

# --- АВТО-СИГНАЛЫ (RSI) ---
async def auto_signal_check(chat_id):
    conn = sqlite3.connect(DB_PATH); cur = conn.cursor()
    cur.execute("SELECT symbol FROM portfolio WHERE chat_id=?", (str(chat_id),))
    coins = [r[0] for r in cur.fetchall()]
    conn.close()

    if not coins:
        return "⚠️ Сначала добавьте монеты в портфель."

    report = "🧠 <b>AI Анализ Рынка (RSI Strategy):</b>\n\n"
    for sym in coins:
        try:
            ohlcv = await exchange.fetch_ohlcv(sym, '1h', limit=20)
            df = pd.DataFrame(ohlcv, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
            rsi = calculate_rsi(df['c']).iloc[-1]
            status = "⚪️ Нейтрально"
            if rsi > 70: status = "🔴 <b>ПРОДАВАТЬ</b> (Перекуплен)"
            elif rsi < 30: status = "🟢 <b>ПОКУПАТЬ</b> (Перепродан)"
            report += f"• {sym}: RSI {rsi:.1f} -> {status}\n"
        except:
            report += f"• {sym}: Ошибка данных\n"
    return report

# --- ЗАПУСК ---
async def main():
    try:
        import uvloop
        asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    except: pass
    
    await init_db()
    logger.info("Система запущена.")
    await asyncio.gather(price_monitor_loop(), start_polling())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
