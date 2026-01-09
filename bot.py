import os
import asyncio
from dotenv import load_dotenv

import pandas as pd
from binance.client import Client

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    CallbackQueryHandler,
    filters,
)

from openai import OpenAI

# ================= LOAD ENV =================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not BOT_TOKEN or not OPENAI_API_KEY:
    raise RuntimeError("BOT_TOKEN or OPENAI_API_KEY missing")

# ================= GLOBALS =================
ai_client = OpenAI(api_key=OPENAI_API_KEY)
binance = Client()

TIMEFRAME = "15m"
bot_active = False
user_coins = {}

# ================= INDICATORS (NO pandas-ta) =================
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

# ================= AI =================
async def ask_ai(prompt: str) -> str:
    try:
        response = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a crypto trading assistant. Use very simple English."},
                {"role": "user", "content": prompt},
            ],
        )
        return response.choices[0].message.content.strip()
    except:
        return "AI explanation unavailable."

# ================= MARKET DATA =================
def get_ohlcv(symbol: str, interval="15m", limit=100):
    klines = binance.futures_klines(
        symbol=symbol,
        interval=interval,
        limit=limit
    )
    df = pd.DataFrame(klines, columns=[
        "time","open","high","low","close","volume",
        "close_time","qav","trades","tb_base","tb_quote","ignore"
    ])
    df["close"] = df["close"].astype(float)
    return df

# ================= STRATEGY =================
def generate_signal(coin: str):
    try:
        df = get_ohlcv(f"{coin}USDT", TIMEFRAME)

        df["ema20"] = ema(df["close"], 20)
        df["ema50"] = ema(df["close"], 50)
        df["rsi"] = rsi(df["close"], 14)

        last = df.iloc[-1]
        entry = round(last["close"], 2)

        if last["ema20"] > last["ema50"] and last["rsi"] > 55:
            direction = "LONG"
            sl = round(entry * 0.99, 2)
            tp = round(entry * 1.02, 2)

        elif last["ema20"] < last["ema50"] and last["rsi"] < 45:
            direction = "SHORT"
            sl = round(entry * 1.01, 2)
            tp = round(entry * 0.98, 2)

        else:
            return None

        confidence = min(90, max(65, int(abs(last["rsi"] - 50) * 2)))

        return {
            "coin": coin,
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "confidence": confidence,
            "rsi": round(last["rsi"], 2)
        }

    except Exception as e:
        print("Signal error:", e)
        return None

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Crypto AI Trading Bot\n\n"
        "Commands:\n"
        "/active – start signals\n"
        "/sleep – stop signals\n"
        "/add – add coins\n"
        "signal – get signal"
    )

async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    bot_active = True
    await update.message.reply_text("✅ Bot ACTIVATED")

async def sleep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    bot_active = False
    await update.message.reply_text("😴 Bot SLEEPING")

# ================= ADD COIN =================
async def add_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("BTC", callback_data="add_BTC")],
        [InlineKeyboardButton("ETH", callback_data="add_ETH")],
        [InlineKeyboardButton("SOL", callback_data="add_SOL")],
    ]
    await update.message.reply_text(
        "Select coins:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def coin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    coin = query.data.replace("add_", "")
    chat_id = query.message.chat_id

    user_coins.setdefault(chat_id, set()).add(coin)
    await query.edit_message_text(f"✅ Added {coin}")

# ================= MESSAGE HANDLER =================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.lower()
    chat_id = update.message.chat_id

    if "signal" in text:
        if not bot_active:
            await update.message.reply_text("⚠️ Bot is sleeping. Send /active.")
            return

        coins = user_coins.get(chat_id, {"BTC"})

        for coin in coins:
            signal = generate_signal(coin)
            if not signal:
                continue

            explanation = await ask_ai(
                f"""
                Coin: {coin}
                Direction: {signal['direction']}
                RSI: {signal['rsi']}
                Entry: {signal['entry']}
                Stop Loss: {signal['sl']}
                Take Profit: {signal['tp']}
                Explain simply why this trade is valid.
                """
            )

            await update.message.reply_text(
                f"""
🚨 {coin} FUTURES SIGNAL 🚨

📌 Direction: {signal['direction']}
⏱ Timeframe: {TIMEFRAME}

💰 Entry: {signal['entry']}
🛑 Stop Loss: {signal['sl']}
🎯 Take Profit: {signal['tp']}

📊 Confidence: {signal['confidence']}%
📈 RSI: {signal['rsi']}

🤖 AI Insight:
{explanation}

⚠️ Not financial advice
"""
            )

# ================= PERIODIC SIGNALS =================
async def periodic_signals(app):
    while True:
        if not bot_active:
            await asyncio.sleep(60)
            continue

        for chat_id, coins in user_coins.items():
            for coin in coins:
                signal = generate_signal(coin)
                if not signal:
                    continue

                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"⏰ Auto Signal {coin}\n{signal['direction']} @ {signal['entry']}"
                )

        await asyncio.sleep(900)

# ================= MAIN =================
async def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("active", activate))
    app.add_handler(CommandHandler("sleep", sleep))
    app.add_handler(CommandHandler("add", add_coin))
    app.add_handler(CallbackQueryHandler(coin_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    asyncio.create_task(periodic_signals(app))

    print("🤖 Crypto AI Bot running on Render...")
    await app.run_polling()

# ================= RUN =================
if __name__ == "__main__":
    asyncio.run(main())
