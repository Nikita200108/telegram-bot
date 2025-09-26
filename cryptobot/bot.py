#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import logging
from datetime import datetime

import requests
import ccxt
import pandas as pd
import matplotlib.pyplot as plt
from flask import Flask, request
from dotenv import load_dotenv

# ---------------- Load ENV ----------------
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    TELEGRAM_TOKEN = input("Введите TELEGRAM_TOKEN: ")
    with open(".env", "a", encoding="utf-8") as f:
        f.write(f"\nTELEGRAM_TOKEN={TELEGRAM_TOKEN}")

BOT_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SEND_MESSAGE = BOT_API + "/sendMessage"
SEND_PHOTO = BOT_API + "/sendPhoto"
ANSWER_CB = BOT_API + "/answerCallbackQuery"

PORT = int(os.getenv("PORT") or 8000)

# ---------------- Logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("crypto_bot")

# ---------------- Flask ----------------
app = Flask(__name__)

# ---------------- Exchange ----------------
exchange = ccxt.mexc({"enableRateLimit": True})

# ---------------- Default symbols ----------------
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "NEAR/USDT"]

# ---------------- Utils ----------------
def send_message(chat_id, text, reply_markup=None):
    try:
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        requests.post(SEND_MESSAGE, json=payload)
    except Exception as e:
        logger.error(f"send_message error: {e}")

def send_photo(chat_id, photo_path, caption=""):
    try:
        with open(photo_path, "rb") as f:
            requests.post(SEND_PHOTO, data={"chat_id": chat_id, "caption": caption}, files={"photo": f})
    except Exception as e:
        logger.error(f"send_photo error: {e}")

def get_price(symbol):
    try:
        ticker = exchange.fetch_ticker(symbol)
        return ticker["last"]
    except Exception as e:
        logger.error(f"get_price error: {e}")
        return None

def get_chart(symbol, timeframe="1h", limit=100):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
        df["time"] = pd.to_datetime(df["time"], unit="ms")

        plt.figure(figsize=(10, 5))
        plt.plot(df["time"], df["close"], label=f"{symbol} {timeframe}")
        plt.title(f"{symbol} ({timeframe})")
        plt.xlabel("Time")
        plt.ylabel("Price")
        plt.legend()
        plt.grid()

        file_path = f"chart_{symbol.replace('/', '')}_{timeframe}.png"
        plt.savefig(file_path)
        plt.close()
        return file_path
    except Exception as e:
        logger.error(f"get_chart error: {e}")
        return None

# ---------------- Handlers ----------------
def handle_start(chat_id):
    keyboard = {
        "inline_keyboard": [
            [{"text": "📊 Цена", "callback_data": "choose_price"}],
            [{"text": "💹 График", "callback_data": "choose_chart"}],
            [{"text": "⏰ Установить алерт", "callback_data": "set_alert"}],
            [{"text": "⚙️ Статус", "callback_data": "status"}],
        ]
    }
    send_message(chat_id, "Выбери действие:", reply_markup=keyboard)

def handle_symbol_choice(chat_id, action):
    keyboard = {"inline_keyboard": []}
    for sym in SYMBOLS:
        keyboard["inline_keyboard"].append(
            [{"text": sym, "callback_data": f"{action}_{sym}"}]
        )
    send_message(chat_id, "Выбери монету:", reply_markup=keyboard)

def handle_callback(query):
    try:
        data = query["data"]
        chat_id = query["message"]["chat"]["id"]
        query_id = query["id"]

        # подтверждаем callback
        requests.post(ANSWER_CB, json={"callback_query_id": query_id})

        if data == "choose_price":
            handle_symbol_choice(chat_id, "price")

        elif data == "choose_chart":
            handle_symbol_choice(chat_id, "chart")

        elif data.startswith("price_"):
            symbol = data.split("_", 1)[1]
            price = get_price(symbol)
            if price:
                send_message(chat_id, f"Цена {symbol}: {price} USDT")
            else:
                send_message(chat_id, f"Не удалось получить цену {symbol}")

        elif data.startswith("chart_"):
            symbol = data.split("_", 1)[1]
            keyboard = {
                "inline_keyboard": [
                    [{"text": "1m", "callback_data": f"tf_{symbol}_1m"},
                     {"text": "5m", "callback_data": f"tf_{symbol}_5m"},
                     {"text": "15m", "callback_data": f"tf_{symbol}_15m"}],
                    [{"text": "30m", "callback_data": f"tf_{symbol}_30m"},
                     {"text": "1h", "callback_data": f"tf_{symbol}_1h"},
                     {"text": "4h", "callback_data": f"tf_{symbol}_4h"}],
                    [{"text": "1d", "callback_data": f"tf_{symbol}_1d"}],
                ]
            }
            send_message(chat_id, f"Выбери таймфрейм для {symbol}:", reply_markup=keyboard)

        elif data.startswith("tf_"):
            _, symbol, timeframe = data.split("_")
            file_path = get_chart(symbol, timeframe)
            if file_path:
                send_photo(chat_id, file_path, caption=f"{symbol} ({timeframe})")
            else:
                send_message(chat_id, f"Не удалось построить график {symbol} ({timeframe})")

        elif data == "set_alert":
            send_message(chat_id, "Установка алертов будет добавлена позже ✨")

        elif data == "status":
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            send_message(chat_id, f"Бот работает ✅\nВремя сервера: {now}")

        else:
            send_message(chat_id, "Команда не распознана.")
    except Exception as e:
        logger.error(f"handle_callback error: {e}")

# ---------------- Flask Routes ----------------
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    try:
        update = request.get_json()
        if not update:
            return "no update"

        if "message" in update:
            msg = update["message"]
            chat_id = msg["chat"]["id"]
            text = msg.get("text", "")

            if text == "/start":
                handle_start(chat_id)
            else:
                send_message(chat_id, "Команда не распознана. Нажмите /start")
        elif "callback_query" in update:
            handle_callback(update["callback_query"])

    except Exception as e:
        logger.error(f"webhook error: {e}")
    return "ok"

# ---------------- Main ----------------
if __name__ == "__main__":
    logger.info("Starting Flask webhook on port %s", PORT)
    app.run(host="0.0.0.0", port=PORT)
