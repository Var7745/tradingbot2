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

# ================= RENDER PORT FIX (IMPORTANT) =================
# This removes: "No open ports detected" warning
def start_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler).serve_forever()

threading.Thread(target=start_dummy_server, daemon=True).start()

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

trade_stats = {
    "total": 0,
    "wins": 0,
    "losses": 0
}

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

# ================= STRATEGY (FEATURE 2) =================
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
            direction = "LONG"
            sl = round(entry * 0.99, 2)
            tp = round(entry * 1.02, 2)

        elif l15["ema20"] < l15["ema50"] and l1h["ema20"] < l1h["ema50"] and l15["rsi"] < 45:
            direction = "SHORT"
            sl = round(entry * 1.01, 2)
            tp = round(entry * 0.98, 2)

        else:
            return None

        trade_stats["total"] += 1

        return {
            "coin": coin,
            "direction": direction,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "rsi": round(l15["rsi"], 2),
        }

    except Exception as e:
        print("Signal error:", e)
        return None

# ================= AI (FEATURE 5) =================
async def ask_ai(signal):
    prompt = f"""
Coin: {signal['coin']}
Direction: {signal['direction']}
RSI: {signal['rsi']}
Entry: {signal['entry']}
Stop Loss: {signal['sl']}
Take Profit: {signal['tp']}

Explain briefly why this trade is valid.
"""
    try:
        res = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Be short and clear."},
                {"role": "user", "content": prompt},
            ],
        )
        return res.choices[0].message.content.strip()
    except:
        return "AI explanation unavailable."

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Crypto Trading Bot Ready\n\n"
        "/active – Start signals\n"
        "/sleep – Stop signals\n"
        "/add – Add coins\n"
        "/stats – Bot stats\n"
        "signal – Get signal"
    )

async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    bot_active = True
    await update.message.reply_text("✅ Bot ACTIVATED")

async def sleep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    bot_active = False
    await update.message.reply_text("😴 Bot SLEEPING")

# ================= STATS (FEATURE 4) =================
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = trade_stats["total"]
    wins = trade_stats["wins"]
    losses = trade_stats["losses"]
    accuracy = (wins / total * 100) if total else 0

    await update.message.reply_text(
        f"📊 BOT STATS\n\n"
        f"Total Trades: {total}\n"
        f"Wins: {wins}\n"
        f"Losses: {losses}\n"
        f"Accuracy: {accuracy:.2f}%"
    )

# ================= ADD COINS =================
async def add_coin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("BTC", callback_data="BTC")],
        [InlineKeyboardButton("ETH", callback_data="ETH")],
        [InlineKeyboardButton("SOL", callback_data="SOL")]
    ]
    await update.message.reply_text(
        "Select coins:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def coin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_coins.setdefault(query.message.chat_id, set()).add(query.data)
    await query.edit_message_text(f"✅ Added {query.data}")

# ================= MESSAGE HANDLER =================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id

    if "signal" in update.message.text.lower():
        if not bot_active:
            await update.message.reply_text("⚠️ Bot sleeping. Use /active.")
            return

        coins = user_coins.get(chat_id, {"BTC"})
        for coin in coins:
            signal = generate_signal(coin)
            if not signal:
                continue

            explanation = await ask_ai(signal)

            await update.message.reply_text(
                f"🚨 {coin} SIGNAL 🚨\n\n"
                f"Direction: {signal['direction']}\n"
                f"Entry: {signal['entry']}\n"
                f"SL: {signal['sl']}\n"
                f"TP: {signal['tp']}\n"
                f"RSI: {signal['rsi']}\n\n"
                f"🤖 {explanation}"
            )

# ================= BACKGROUND TASK (CORRECT WAY) =================
async def periodic_signals(app):
    while True:
        if bot_active:
            for chat_id, coins in user_coins.items():
                for coin in coins:
                    signal = generate_signal(coin)
                    if signal:
                        await app.bot.send_message(
                            chat_id=chat_id,
                            text=f"⏰ Auto {coin}: {signal['direction']} @ {signal['entry']}"
                        )
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
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CallbackQueryHandler(coin_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("🤖 Crypto AI Bot running cleanly on Render...")
    app.run_polling()

# ================= RUN =================
if __name__ == "__main__":
    main()
