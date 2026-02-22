import os
import asyncio
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("TWELVE_API_KEY")

if not BOT_TOKEN or not API_KEY:
    raise RuntimeError("BOT_TOKEN or TWELVE_API_KEY missing")

SYMBOLS = ["EUR/USD", "XAU/USD"]
TIMEFRAMES = ["5min", "15min", "30min"]

PRICE_CHECK = 10

active_signals: Dict[int, "Signal"] = {}
watch_tasks: Dict[int, asyncio.Task] = {}


@dataclass
class Signal:
    symbol: str
    tf: str
    direction: str
    entry: float
    tp: float
    sl: float


# ================= INDICATORS =================

def ema(values: List[float], period: int):
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    result = [None] * (period - 1) + [ema_val]
    for price in values[period:]:
        ema_val = price * k + ema_val * (1 - k)
        result.append(ema_val)
    return result


def rsi(values: List[float], period=14):
    gains, losses = [], []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    rs = avg_gain / avg_loss if avg_loss != 0 else 0
    rsi_vals = [None] * period + [100 - 100 / (1 + rs)]

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else 0
        rsi_vals.append(100 - 100 / (1 + rs))

    return rsi_vals


# ================= API =================

async def get_candles(symbol, tf):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": tf,
        "outputsize": "150",
        "apikey": API_KEY,
    }
    async with httpx.AsyncClient() as client:
        r = await client.get(url, params=params)
        data = r.json()

    values = list(reversed(data["values"]))
    closes = [float(v["close"]) for v in values]
    highs = [float(v["high"]) for v in values]
    lows = [float(v["low"]) for v in values]

    return closes, highs, lows


async def get_price(symbol):
    url = "https://api.twelvedata.com/price"
    params = {"symbol": symbol, "apikey": API_KEY}
    async with httpx.AsyncClient() as client:
        r = await client.get(url, params=params)
        return float(r.json()["price"])


# ================= SIGNAL LOGIC =================

async def generate_signal():

    for symbol in SYMBOLS:

        # 30m — определяем глобальный тренд
        closes30, _, _ = await get_candles(symbol, "30min")
        e9_30 = ema(closes30, 9)
        e21_30 = ema(closes30, 21)

        if not e9_30[-1] or not e21_30[-1]:
            continue

        trend_up = e9_30[-1] > e21_30[-1]
        trend_down = e9_30[-1] < e21_30[-1]

        # 15m подтверждение
        closes15, _, _ = await get_candles(symbol, "15min")
        e9_15 = ema(closes15, 9)
        e21_15 = ema(closes15, 21)
        rsi15 = rsi(closes15)

        if not e9_15[-1] or not e21_15[-1] or not rsi15[-1]:
            continue

        # 5m вход
        closes5, highs5, lows5 = await get_candles(symbol, "5min")
        e9_5 = ema(closes5, 9)
        e21_5 = ema(closes5, 21)
        rsi5 = rsi(closes5)

        if not e9_5[-1] or not e21_5[-1] or not rsi5[-1]:
            continue

        price = closes5[-1]

        # BUY логика
        if trend_up and e9_15[-1] > e21_15[-1] and 50 < rsi5[-1] < 65 and e9_5[-1] > e21_5[-1]:
            sl = price - (price * 0.001)
            tp = price + (price * 0.002)
            return Signal(symbol, "5/15/30m", "BUY", round(price, 5), round(tp, 5), round(sl, 5))

        # SELL логика
        if trend_down and e9_15[-1] < e21_15[-1] and 35 < rsi5[-1] < 50 and e9_5[-1] < e21_5[-1]:
            sl = price + (price * 0.001)
            tp = price - (price * 0.002)
            return Signal(symbol, "5/15/30m", "SELL", round(price, 5), round(tp, 5), round(sl, 5))

    return None


# ================= UI =================

def keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📌 Новый сигнал", callback_data="new"),
                InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help"),
            ]
        ]
    )


bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()


@dp.message(CommandStart())
async def start(m: Message):
    await m.answer("Кнопки обновлены ✅", reply_markup=ReplyKeyboardRemove())
    await m.answer("Forex Signal Bot\n\nНажми Новый сигнал", reply_markup=keyboard())


@dp.callback_query(F.data == "help")
async def help_btn(c: CallbackQuery):
    await c.answer()
    await c.message.answer(
        "Бот анализирует EUR/USD и XAU/USD\n"
        "ТФ: 5m, 15m, 30m\n"
        "Новый сигнал появляется после закрытия предыдущего.",
        reply_markup=keyboard(),
    )


@dp.callback_query(F.data == "new")
async def new_signal(c: CallbackQuery):
    await c.answer()

    uid = c.from_user.id

    if uid in active_signals:
        await c.message.answer("Есть активный сигнал. Дождись TP/SL.", reply_markup=keyboard())
        return

    msg = await c.message.answer("Анализирую рынок...", reply_markup=keyboard())

    sig = await generate_signal()

    if not sig:
        await msg.edit_text("Сейчас нет сильного сигнала.", reply_markup=keyboard())
        return

    active_signals[uid] = sig

    await msg.edit_text(
        f"📊 <b>{sig.symbol}</b>\n\n"
        f"Direction: {sig.direction}\n"
        f"Entry: <code>{sig.entry}</code>\n"
        f"TP: <code>{sig.tp}</code>\n"
        f"SL: <code>{sig.sl}</code>",
        reply_markup=keyboard(),
    )

    watch_tasks[uid] = asyncio.create_task(watch_price(uid, sig))


async def watch_price(uid, sig):
    while True:
        await asyncio.sleep(PRICE_CHECK)
        price = await get_price(sig.symbol)

        if sig.direction == "BUY":
            if price >= sig.tp or price <= sig.sl:
                break
        else:
            if price <= sig.tp or price >= sig.sl:
                break

    active_signals.pop(uid, None)

    await bot.send_message(
        uid,
        "Сигнал закрыт.\nТеперь можно запросить новый.",
        reply_markup=keyboard(),
    )


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
