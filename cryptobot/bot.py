#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Full-featured Telegram crypto bot with:
- Flask Webhook
- MEXC via ccxt
- InlineKeyboard кнопки (status, price, alerts, chart)
- Логирование ошибок (бот не падает)
"""

import os
import logging
import sqlite3
import threading
from datetime import datetime

import requests
import ccxt
import pandas as pd
import matplotlib.pyplot as plt
from flask import Flask, request
from dotenv import load_dotenv

# ---------------- Logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("crypto_bot")

# ---------------- Config ----------------
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    TELEGRAM_TOKEN = input("Введите TELEGRAM_TOKEN: ")
    with open(".env", "a", encoding="utf-8") as f:
        f.write(f"\nTELEGRAM_TOKEN={TELEGRAM_TOKEN}\n")

BOT_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
SEND_MESSAGE = BOT_API + "/sendMessage"
SEND_PHOTO = BOT_API + "/sendPhoto"
ANSWER_CB = BOT_API + "/answerCallbackQuery"

PORT = int(os.getenv("PORT") or 8000)
DB_PATH = os.getenv("DB_PATH") or "bot_data.sqlite"

DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]

# ---------------- Flask ----------------
app = Flask(__name__)

# ---------------- Exchange ----------------
exchange = ccxt.mexc({"enableRateLimit": True})


# ---------------- Helpers ----------------
def send_message(chat_id, text, reply_markup=None):
    try:
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        requests.post(SEND_MESSAGE, json=payload)
    except Exception as e:
        logger.error(f"send_message error: {e}")


def send_chart(chat_id, symbol, timeframe="1h"):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=50)
        df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
        df["time"] = pd.to_datetime(df["time"], unit="ms")

        plt.figure(figsize=(8, 4))
        plt.plot(df["time"], df["close"], label=symbol)
        plt.title(f"{symbol} ({timeframe})")
        plt.xlabel("Time")
        plt.ylabel("Price")
        plt.legend()
        plt.grid()

        chart_path = f"chart_{symbol.replace('/', '_')}.png"
        plt.savefig(chart_path)
        plt.close()

        with open(chart_path, "rb") as f:
            files = {"photo": f}
            data = {"chat_id": chat_id}
            requests.post(SEND_PHOTO, data=data, files=files)
    except Exception as e:
        logger.error(f"send_chart error: {e}")


def main_menu():
    """Главное меню"""
    return {
        "inline_keyboard": [
            [{"text": "📊 Статус", "callback_data": "status"}],
            [{"text": "💰 Цена", "callback_data": "price_menu"}],
            [{"text": "⏱ Установить алерт", "callback_data": "set_alert"}],
            [{"text": "📈 График", "callback_data": "chart_menu"}],
        ]
    }


def price_menu():
    """Меню выбора монеты для цены"""
    return {
        "inline_keyboard": [[{"text": s, "callback_data": f"price:{s}"}] for s in DEFAULT_SYMBOLS]
    }


def chart_menu():
    """Меню выбора монеты для графика"""
    return {
        "inline_keyboard": [[{"text": s, "callback_data": f"chart:{s}"}] for s in DEFAULT_SYMBOLS]
    }


def timeframe_menu(symbol):
    """Меню выбора таймфрейма"""
    tfs = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
    return {
        "inline_keyboard": [
            [{"text": tf, "callback_data": f"chart:{symbol}:{tf}"} for tf in tfs[:3]],
            [{"text": tf, "callback_data": f"chart:{symbol}:{tf}"} for tf in tfs[3:6]],
            [{"text": tfs[-1], "callback_data": f"chart:{symbol}:{tfs[-1]}"}],
        ]
    }


# ---------------- Webhook ----------------
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    try:
        update = request.get_json()
        logger.info(f"Update: {update}")

        if "message" in update:
            chat_id = update["message"]["chat"]["id"]
            text = update["message"].get("text", "")

            if text == "/start":
                send_message(chat_id, "Добро пожаловать! 👋", reply_markup=main_menu())
            else:
                send_message(chat_id, "Команда не распознана. Нажмите кнопку ⬇️", reply_markup=main_menu())

        elif "callback_query" in update:
            cq = update["callback_query"]
            chat_id = cq["message"]["chat"]["id"]
            data = cq["data"]

            # Ответ Telegram (чтобы кнопка не "крутилась")
            requests.post(ANSWER_CB, json={"callback_query_id": cq["id"]})

            if data == "status":
                send_message(chat_id, "📊 Бот работает исправно ✅")

            elif data == "price_menu":
                send_message(chat_id, "Выберите монету:", reply_markup=price_menu())

            elif data.startswith("price:"):
                symbol = data.split(":")[1]
                ticker = exchange.fetch_ticker(symbol)
                send_message(chat_id, f"💰 {symbol} = <b>{ticker['last']}</b> USDT")

            elif data == "chart_menu":
                send_message(chat_id, "Выберите монету для графика:", reply_markup=chart_menu())

            elif data.startswith("chart:") and len(data.split(":")) == 2:
                symbol = data.split(":")[1]
                send_message(chat_id, f"Выберите таймфрейм для {symbol}:", reply_markup=timeframe_menu(symbol))

            elif data.startswith("chart:") and len(data.split(":")) == 3:
                _, symbol, tf = data.split(":")
                send_chart(chat_id, symbol, timeframe=tf)

            elif data == "set_alert":
                send_message(chat_id, "⚠️ Настройка алертов в разработке...")

    except Exception as e:
        logger.error(f"webhook error: {e}")

    return {"ok": True}


# ---------------- Run ----------------
if __name__ == "__main__":
    logger.info("Starting bot...")
    app.run(host="0.0.0.0", port=PORT)
