import sqlite3
import ccxt
import pandas as pd
import requests
import asyncio
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler

# --- КОНФИГУРАЦИЯ ---
import os
TOKEN = os.getenv("TELEGRAM_TOKEN")
CRYPTO_PANIC_KEY = os.getenv("CRYPTO_PANIC_KEY")

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS subscriptions 
                      (user_id INTEGER, symbol TEXT, PRIMARY KEY (user_id, symbol))''')
    conn.commit()
    conn.close()

def get_user_subs(user_id):
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    cursor.execute('SELECT symbol FROM subscriptions WHERE user_id = ?', (user_id,))
    subs = [row[0] for row in cursor.fetchall()]
    conn.close()
    return subs

def add_subscription(user_id, symbol):
    conn = sqlite3.connect('crypto_bot.db')
    cursor = conn.cursor()
    cursor.execute('INSERT OR IGNORE INTO subscriptions (user_id, symbol) VALUES (?, ?)', (user_id, symbol))
    conn.commit()
    conn.close()

# --- АНАЛИЗ ---
def get_market_sentiment(coin_ticker):
    url = f"https://cryptopanic.com/api/v1/posts/?auth_token={CRYPTO_PANIC_KEY}&currencies={coin_ticker}&kind=news"
    try:
        response = requests.get(url, timeout=5).json()
        pos, neg = 0, 0
        for post in response.get('results', [])[:5]:
            v = post.get('votes', {})
            pos += v.get('positive', 0) + v.get('bullish', 0)
            neg += v.get('negative', 0) + v.get('bearish', 0)
        return 1 if pos > neg else (-1 if neg > pos else 0)
    except: return 0

def generate_advanced_signal(df, symbol):
    # Технические индикаторы
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rsi = 100 - (100 / (1 + (gain / loss))).iloc[-1]

    sma20 = df['close'].rolling(window=20).mean()
    std20 = df['close'].rolling(window=20).std()
    upper_bb = (sma20 + (std20 * 2)).iloc[-1]
    lower_bb = (sma20 - (std20 * 2)).iloc[-1]

    recent_high = df['high'].tail(50).max()
    recent_low = df['low'].tail(50).min()
    price_range = recent_high - recent_low
    current_price = df['close'].iloc[-1]
    sentiment = get_market_sentiment(symbol.split('/')[0])

    score = 0
    details = []
    if macd_line.iloc[-1] > signal_line.iloc[-1]: score += 1; details.append("📈 MACD: Бычий")
    else: score -= 1; details.append("📉 MACD: Медвежий")
    if rsi < 35: score += 2; details.append(f"🆘 RSI ({rsi:.1f}): Перепродан")
    elif rsi > 65: score -= 2; details.append(f"🔥 RSI ({rsi:.1f}): Перекуплен")
    if current_price <= lower_bb * 1.005: score += 1; details.append("🛡 У нижней границы Bollinger")
    if sentiment > 0: score += 1; details.append("📰 Новости: Позитив")

    if score >= 2:
        verdict = "🟢 **СИЛЬНЫЙ LONG**"
        tp1 = recent_low + (price_range * 0.618)
        sl = recent_low * 0.98
        targets = f"🎯 TP1: `{tp1:.4f}`\n🛑 SL: `{sl:.4f}`"
    elif score <= -2:
        verdict = "🔴 **СИЛЬНЫЙ SHORT**"
        tp1 = recent_high - (price_range * 0.618)
        sl = recent_high * 1.02
        targets = f"🎯 TP1: `{tp1:.4f}`\n🛑 SL: `{sl:.4f}`"
    else:
        verdict = "🟡 **НЕЙТРАЛЬНО**"
        targets = "Ждем подтверждения"

    return f"📊 **Анализ {symbol}**\nЦена: `{current_price}`\n\nВердикт: {verdict}\nИндикаторы:\n" + "\n".join(details) + f"\n\n--- УРОВНИ ---\n{targets}"

# --- ОБРАБОТЧИКИ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💎 Бот готов.\n/add BTC - подписаться\n/check BTC - анализ\n/list - мои подписки")

async def add_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Пример: /add BTC")
    symbol = f"{context.args[0].upper()}/USDT"
    add_subscription(update.effective_user.id, symbol)
    await update.message.reply_text(f"✅ Подписка на {symbol} активна.")

async def list_subs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    subs = get_user_subs(update.effective_user.id)
    msg = "📋 **Ваши подписки:**\n\n" + "\n".join(subs) if subs else "У вас нет активных подписок."
    await update.message.reply_text(msg, parse_mode='Markdown')

async def check_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args: return await update.message.reply_text("Пример: /check BTC")
    symbol = f"{context.args[0].upper()}/USDT"
    await send_analysis(update, symbol)

async def send_analysis(update_or_query, symbol, edit=False):
    ex = ccxt.binance()
    try:
        bars = ex.fetch_ohlcv(symbol, timeframe='1h', limit=100)
        df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'vol'])
        report = generate_advanced_signal(df, symbol)
        keyboard = [[InlineKeyboardButton("🔄 Обновить данные", callback_data=f"refresh_{symbol}")]]
        markup = InlineKeyboardMarkup(keyboard)
        
        if edit:
            await update_or_query.edit_message_text(report, reply_markup=markup, parse_mode='Markdown')
        else:
            await update_or_query.message.reply_text(report, reply_markup=markup, parse_mode='Markdown')
    except:
        msg = "❌ Ошибка получения данных. Проверьте тикер."
        if edit: await update_or_query.edit_message_text(msg)
        else: await update_or_query.message.reply_text(msg)

async def button_tap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # Моментальный ответ, чтобы кнопка не «залипала»
    await query.answer("Обновляю...") 
    
    if query.data.startswith("refresh_"):
        symbol = query.data.split("_")[1]
        await send_analysis(query, symbol, edit=True)

# --- ЗАПУСК ---
if __name__ == '__main__':
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_coin))
    app.add_handler(CommandHandler("list", list_subs)) # ТЕПЕРЬ ДОБАВЛЕНО
    app.add_handler(CommandHandler("check", check_coin))
    app.add_handler(CallbackQueryHandler(button_tap))
    
    print("Бот запущен...")
    app.run_polling()

