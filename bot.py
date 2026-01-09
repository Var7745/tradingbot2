import os
import asyncio
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from dotenv import load_dotenv

import pandas as pd
from binance.client import Client
from openai import OpenAI

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =====================================================
# RENDER KEEP-ALIVE SERVER (PREVENT SHUTDOWN)
# =====================================================
def keep_alive():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler).serve_forever()

threading.Thread(target=keep_alive, daemon=True).start()

# ================= LOAD ENV =================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BOT_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("Missing BOT_TOKEN or OPENAI_API_KEY")

# ================= GLOBALS =================
ai_client = OpenAI(api_key=OPENAI_API_KEY)
binance = Client()

bot_active = False
user_coins = {}

# ================= INDICATORS =================
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ================= MARKET DATA =================
def get_ohlcv(symbol, interval):
    klines = binance.futures_klines(
        symbol=symbol,
        interval=interval,
        limit=100
    )
    df = pd.DataFrame(klines, columns=[
        "time","open","high","low","close","volume",
        "close_time","qav","trades","tb_base","tb_quote","ignore"
    ])
    df["close"] = df["close"].astype(float)
    return df

# ================= STRATEGY =================
def generate_signal(coin):
    try:
        df_15m = get_ohlcv(f"{coin}USDT", "15m")
        df_1h = get_ohlcv(f"{coin}USDT", "1h")

        for df in (df_15m, df_1h):
            df["ema20"] = ema(df["close"], 20)
            df["ema50"] = ema(df["close"], 50)
            df["rsi"] = rsi(df["close"], 14)

        l15 = df_15m.iloc[-1]
        l1h = df_1h.iloc[-1]

        entry = round(l15["close"], 2)

        if l15["ema20"] > l15["ema50"] and l1h["ema20"] > l1h["ema50"] and l15["rsi"] > 55:
            return {
                "coin": coin,
                "direction": "LONG",
                "entry": entry,
                "sl": round(entry * 0.99, 2),
                "tp": round(entry * 1.02, 2),
                "rsi": round(l15["rsi"], 2)
            }

        if l15["ema20"] < l15["ema50"] and l1h["ema20"] < l1h["ema50"] and l15["rsi"] < 45:
            return {
                "coin": coin,
                "direction": "SHORT",
                "entry": entry,
                "sl": round(entry * 1.01, 2),
                "tp": round(entry * 0.98, 2),
                "rsi": round(l15["rsi"], 2)
            }

        return None

    except Exception as e:
        print("Signal error:", e)
        return None

# ================= AI =================
async def ask_ai(signal):
    prompt = f"""
Coin: {signal['coin']}
Direction: {signal['direction']}
RSI: {signal['rsi']}
Entry: {signal['entry']}
SL: {signal['sl']}
TP: {signal['tp']}
Explain briefly.
"""
    try:
        res = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Be simple and short."},
                {"role": "user", "content": prompt},
            ],
        )
        return res.choices[0].message.content.strip()
    except:
        return "Market conditions support this trade."

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Crypto Trading Bot\n\n"
        "/active – Start signals\n"
        "/sleep – Stop signals\n"
        "/add – Add coins\n"
        "Send: signal / trade / buy / sell"
    )

async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    bot_active = True
    await update.message.reply_text("✅ Bot ACTIVATED")

async def sleep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    bot_active = False
    await update.message.reply_text("😴 Bot SLEEPING")

# ================= ADD COINS =================
async def add_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["BTC", "ETH", "BNB"],
        ["SOL", "XRP", "ADA"],
        ["DOGE", "AVAX", "MATIC"]
    ]

    markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton(c, callback_data=c) for c in row] for row in keyboard]
    )

    await update.message.reply_text("Select coins:", reply_markup=markup)

async def coin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_coins.setdefault(query.message.chat_id, set()).add(query.data)
    await query.edit_message_text(f"✅ Added {query.data}")

# ================= MESSAGE HANDLER =================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    chat_id = update.message.chat_id

    signal_words = ["signal", "signals", "trade", "buy", "sell"]

    if any(word in text for word in signal_words):
        if not bot_active:
            await update.message.reply_text("⚠️ Bot sleeping. Use /active.")
            return

        coins = user_coins.get(chat_id, {"BTC"})

        for coin in coins:
            signal = generate_signal(coin)
            if not signal:
                await update.message.reply_text(f"❌ No trade setup for {coin}")
                continue

            explanation = await ask_ai(signal)

            await update.message.reply_text(
                f"🚨 {coin} SIGNAL 🚨\n\n"
                f"Direction: {signal['direction']}\n"
                f"Entry: {signal['entry']}\n"
                f"SL: {signal['sl']}\n"
                f"TP: {signal['tp']}\n"
                f"RSI: {signal['rsi']}\n\n"
                f"{explanation}"
            )
        return

    # BASIC CHAT RESPONSE
    await update.message.reply_text(
        "🤖 I can help with crypto signals.\n"
        "Type: signal / trade / buy / sell\n"
        "Or use /add to add coins."
    )

# ================= PERIODIC SIGNALS =================
async def periodic_signals(app):
    while True:
        await asyncio.sleep(900)

async def post_init(app):
    app.create_task(periodic_signals(app))

# ================= MAIN =================
def main():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("active", activate))
    app.add_handler(CommandHandler("sleep", sleep))
    app.add_handler(CommandHandler("add", add_coin))
    app.add_handler(CallbackQueryHandler(coin_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Bot running stable on Render")
    app.run_polling()

# ================= RUN =================
if __name__ == "__main__":
    main()
