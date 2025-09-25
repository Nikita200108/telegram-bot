
import ccxt
import requests
import time
import threading
import os
from flask import Flask, request

# ==== Настройки Telegram ====
telegram_token = '8054728348:AAHM1awWcJluyjkLPmxSSCVoP_KzsiqjwP8'
chat_id = '399812452'

# ==== Настройки биржи и монет ====
exchange = ccxt.mexc({'enableRateLimit': True})
symbols = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'NEAR/USDT']

# ==== Пороги для уведомлений (по умолчанию 50$ для всех) ====
default_threshold = 50.0
price_thresholds = {symbol: default_threshold for symbol in symbols}

last_prices = {}

app = Flask(__name__)

def send_telegram_message(message, chat_id_to_send=None):
    if chat_id_to_send is None:
        chat_id_to_send = chat_id
    url = f'https://api.telegram.org/bot{telegram_token}/sendMessage'
    payload = {'chat_id': chat_id_to_send, 'text': message}
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")

def fetch_prices():
    global last_prices
    for symbol in symbols:
        try:
            ticker = exchange.fetch_ticker(symbol)
            price = ticker['last']
            threshold = price_thresholds.get(symbol, default_threshold)
            if symbol not in last_prices:
                last_prices[symbol] = price
                send_telegram_message(f"📡 Первая цена {symbol}: {price} (Порог: {threshold}$)")
            else:
                diff = abs(price - last_prices[symbol])
                if diff >= threshold:
                    message = f"📈 Цена {symbol} изменилась более чем на {threshold}$: {last_prices[symbol]} → {price}"
                    send_telegram_message(message)
                    last_prices[symbol] = price
                else:
                    print(f"✔️ Цена {symbol} изменилась незначительно: {last_prices[symbol]} → {price} (Порог {threshold}$)")
        except Exception as e:
            print(f"Ошибка при получении цены {symbol}: {e}")

price_check_loop_running = True

def price_check_loop():
    while price_check_loop_running:
        fetch_prices()
        time.sleep(10)

@app.route(f'/{telegram_token}', methods=['POST'])
def telegram_webhook():
    global price_thresholds, default_threshold
    data = request.json
    message = data.get('message', {})
    chat_id_received = message.get('chat', {}).get('id')
    text = message.get('text', '')

    if text.startswith('/status'):
        status_lines = [f"Пороги уведомлений (в $):"]
        for sym in symbols:
            threshold = price_thresholds.get(sym, default_threshold)
            price = last_prices.get(sym, 'неизвестно')
            status_lines.append(f"{sym}: цена {price}, порог {threshold}$")
        send_telegram_message('\n'.join(status_lines), chat_id_received)

    elif text.startswith('/set'):
        # Форматы команды:
        # /set BTC 30  — поставить порог 30$ для BTC
        # /set default 40 — установить порог по умолчанию для всех валют, у которых нет индивидуального
        parts = text.split()
        if len(parts) == 3:
            symbol_input = parts[1].upper()
            try:
                new_threshold = float(parts[2])
                if symbol_input == 'DEFAULT':
                    default_threshold = new_threshold
                    send_telegram_message(f"Порог по умолчанию изменён на {default_threshold}$", chat_id_received)
                else:
                    symbol_full = f"{symbol_input}/USDT"
                    if symbol_full in symbols:
                        price_thresholds[symbol_full] = new_threshold
                        send_telegram_message(f"Порог для {symbol_full} изменён на {new_threshold}$", chat_id_received)
                    else:
                        send_telegram_message(f"Валюта {symbol_full} не поддерживается.", chat_id_received)
            except ValueError:
                send_telegram_message("Неверное значение порога. Используйте число.", chat_id_received)
        else:
            send_telegram_message("Использование команды:\n/set SYMBOL NUMBER\nНапример: /set BTC 30\nИли /set default 40", chat_id_received)

    elif text.startswith('/price'):
        parts = text.split()
        if len(parts) == 2:
            symbol_input = parts[1].upper()
            symbol_full = f"{symbol_input}/USDT"
            try:
                ticker = exchange.fetch_ticker(symbol_full)
                price = ticker['last']
                send_telegram_message(f"💰 Цена {symbol_full}: {price}$", chat_id_received)
            except Exception as e:
                send_telegram_message(f"Ошибка: не могу получить цену для {symbol_full}", chat_id_received)
        else:
            send_telegram_message("Использование: /price SYMBOL (например, /price BTC)", chat_id_received)

    else:
        send_telegram_message("Команда не распознана. Доступные команды:\n/status\n/set SYMBOL NUMBER\n/price SYMBOL", chat_id_received)

    return {"ok": True}

def run_flask():
    port = int(os.environ.get("PORT", 8443))
    app.run(host="0.0.0.0", port=port)

if __name__ == '__main__':
    threading.Thread(target=run_flask).start()
    print("Бот запущен. Следим за ценами...")
    price_check_loop()