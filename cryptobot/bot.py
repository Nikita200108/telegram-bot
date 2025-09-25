import ccxt
import requests
import time
import threading
from flask import Flask, request

# ==== Настройки Telegram ====
telegram_token = '8054728348:AAHM1awWcJluyjkLPmxSSCVoP_KzsiqjwP8'
chat_id = '399812452'  # Можно игнорировать, если используем webhook и chat_id получаем из сообщений

# ==== Настройки биржи и монет ====
exchange = ccxt.mexc({'enableRateLimit': True})

symbols = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'NEAR/USDT']

# ==== Порог для уведомлений ($) ====
price_threshold = 50.0

# ==== Словарь для хранения последних цен ====
last_prices = {}

# ==== Flask для обработки команд Telegram ====
app = Flask(__name__)

def send_telegram_message(message, chat_id_to_send=None):
    if chat_id_to_send is None:
        chat_id_to_send = chat_id
    url = f'https://api.telegram.org/bot{telegram_token}/sendMessage'
    payload = {
        'chat_id': chat_id_to_send,
        'text': message
    }
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
            if symbol not in last_prices:
                last_prices[symbol] = price
                send_telegram_message(f"📡 Первая цена {symbol}: {price}")
            else:
                diff = abs(price - last_prices[symbol])
                if diff >= price_threshold:
                    message = f"📈 Цена {symbol} изменилась более чем на {price_threshold}$: {last_prices[symbol]} → {price}"
                    send_telegram_message(message)
                    last_prices[symbol] = price
                else:
                    print(f"✔️ Цена {symbol} изменилась незначительно: {last_prices[symbol]} → {price}")
        except Exception as e:
            print(f"Ошибка при получении цены {symbol}: {e}")

def price_check_loop():
    while True:
        fetch_prices()
        time.sleep(10)

# ==== Обработка команд через Telegram webhook ====

@app.route(f'/{telegram_token}', methods=['POST'])
def telegram_webhook():
    global price_threshold

    data = request.json
    message = data.get('message', {})
    chat_id_received = message.get('chat', {}).get('id')
    text = message.get('text', '')

    if text.startswith('/status'):
        # Отправляем текущие цены и порог
        status_lines = [f"Порог уведомлений: {price_threshold}$"]
        for sym in symbols:
            price = last_prices.get(sym, 'неизвестно')
            status_lines.append(f"{sym}: {price}")
        send_telegram_message('\n'.join(status_lines), chat_id_received)
        pass

    elif text.startswith('/set'):
        
            parts = text.split()
            if len(parts) == 2:
                new_threshold = float(parts[1])
                
                price_threshold = new_threshold
                send_telegram_message(f"Порог изменён на {price_threshold}$", chat_id_received)
                pass 

    elif text.startswith('/price'):
        parts = text.split()
        if len(parts) == 2:
            symbol = parts[1].upper()
            # добавим формат символа для MEXC (например BTC/USDT)
            symbol_full = f"{symbol}/USDT"
            try:
                ticker = exchange.fetch_ticker(symbol_full)
                price = ticker['last']
                send_telegram_message(f"💰 Цена {symbol_full}: {price}$", chat_id_received)
            except Exception as e:
                send_telegram_message(f"Ошибка: не могу получить цену для {symbol_full}", chat_id_received)
        else:
            send_telegram_message("Использование: /price SYMBOL (например, /price BTC)", chat_id_received)

    else:
        send_telegram_message("Команда не распознана. Доступные команды:\n/status\n/set <число>\n/price SYMBOL", chat_id_received)

    return {"ok": True}
        
 
def run_flask():
    app.run(host="0.0.0.0", port=8443)

if __name__ == '__main__':
    # Запускаем Flask в отдельном потоке
    threading.Thread(target=run_flask).start()
    print("Бот запущен. Следим за ценами...")

    price_check_loop()