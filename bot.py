import os
import threading
from http.server import HTTPServer, SimpleHTTPRequestHandler
from dotenv import load_dotenv

import pandas as pd
from binance.client import Client

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
# KEEP RENDER ALIVE
# =====================================================
def keep_alive():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), SimpleHTTPRequestHandler).serve_forever()

threading.Thread(target=keep_alive, daemon=True).start()

# ================= ENV =================
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")

# ================= GLOBALS =================
binance = Client()
bot_active = False

# store coin selections per user
selected_coins = {}

# ================= INDICATORS =================
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    rs = gain.rolling(period).mean() / loss.rolling(period).mean()
    return 100 - (100 / (1 + rs))

# ================= MARKET DATA =================
def get_ohlcv(symbol, interval):
    klines = binance.futures_klines(symbol=symbol, interval=interval, limit=100)
    df = pd.DataFrame(klines, columns=[
        "t","o","h","l","c","v","ct","q","n","tb","tq","i"
    ])
    df["c"] = df["c"].astype(float)
    return df

def get_price(symbol):
    return float(binance.futures_mark_price(symbol=symbol)["markPrice"])

# ================= STRATEGY =================
def generate_signal(coin):
    df15 = get_ohlcv(f"{coin}USDT", "15m")
    df1h = get_ohlcv(f"{coin}USDT", "1h")

    for df in (df15, df1h):
        df["ema20"] = ema(df["c"], 20)
        df["ema50"] = ema(df["c"], 50)
        df["rsi"] = rsi(df["c"])

    l15, l1h = df15.iloc[-1], df1h.iloc[-1]
    entry = round(l15["c"], 2)

    if l15["ema20"] > l15["ema50"] and l1h["ema20"] > l1h["ema50"] and l15["rsi"] > 55:
        return ("LONG", entry, entry * 0.99, entry * 1.02)

    if l15["ema20"] < l15["ema50"] and l1h["ema20"] < l1h["ema50"] and l15["rsi"] < 45:
        return ("SHORT", entry, entry * 1.01, entry * 0.98)

    return None

# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Crypto Signal Bot\n\n"
        "/active – enable signals\n"
        "/sleep – disable signals\n"
        "/status – bot status\n"
        "/price BTC – current price\n"
        "signal – select coins"
    )

async def activate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    bot_active = True
    await update.message.reply_text("✅ Bot ACTIVATED")

async def sleep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global bot_active
    bot_active = False
    await update.message.reply_text("😴 Bot SLEEPING")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coins = ", ".join(selected_coins.get(update.message.chat_id, [])) or "None"
    await update.message.reply_text(
        f"📊 STATUS\nActive: {bot_active}\nSelected coins: {coins}"
    )

async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /price BTC ETH")
        return

    for c in context.args:
        p = get_price(c.upper() + "USDT")
        await update.message.reply_text(f"{c.upper()}: {round(p,2)}")

# ================= SIGNAL SELECTION =================
async def signal_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not bot_active:
        await update.message.reply_text("⚠️ Bot sleeping. Use /active")
        return

    chat_id = update.message.chat_id
    selected_coins[chat_id] = []

    coins = [
        ["BTC", "ETH", "SOL"],
        ["BNB", "XRP", "ADA"],
        ["DOGE", "AVAX", "MATIC"],
        ["DONE"]
    ]

    keyboard = []
    for row in coins:
        buttons = []
        for c in row:
            if c == "DONE":
                buttons.append(InlineKeyboardButton("✅ DONE", callback_data="done"))
            else:
                buttons.append(InlineKeyboardButton(c, callback_data=f"pick_{c}"))
        keyboard.append(buttons)

    await update.message.reply_text(
        "Select coins (tap multiple, then DONE):",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================= CALLBACK =================
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    data = query.data

    if data.startswith("pick_"):
        coin = data.replace("pick_", "")
        selected_coins.setdefault(chat_id, [])
        if coin not in selected_coins[chat_id]:
            selected_coins[chat_id].append(coin)

        await query.message.reply_text(f"➕ Added {coin}")
        return

    if data == "done":
        coins = selected_coins.get(chat_id, [])
        if not coins:
            await query.message.reply_text("❌ No coins selected")
            return

        for coin in coins:
            signal = generate_signal(coin)
            if not signal:
                await query.message.reply_text(f"❌ {coin}: No setup")
                continue

            side, entry, sl, tp = signal
            await query.message.reply_text(
                f"🚨 {coin} SIGNAL\n\n"
                f"Direction: {side}\n"
                f"Entry: {round(entry,2)}\n"
                f"SL: {round(sl,2)}\n"
                f"TP: {round(tp,2)}"
            )

# ================= MAIN =================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("active", activate))
    app.add_handler(CommandHandler("sleep", sleep))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("signal", signal_command))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, start))

    app.run_polling()

if __name__ == "__main__":
    main()
