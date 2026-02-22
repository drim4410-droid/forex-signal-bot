import os
import asyncio
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

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

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not TWELVE_API_KEY:
    raise RuntimeError("TWELVE_API_KEY is not set")

# Какие рынки анализируем (можешь менять)
SYMBOLS = ["EUR/USD", "XAU/USD"]
INTERVAL = "5min"
LOOKBACK = 150  # свечей для индикаторов
PRICE_POLL_SECONDS = 10  # как часто проверять цену для TP/SL

# Порог, чтобы не давать сигнал в “пилу”
MIN_ATR_REL = 0.00005  # для FX ~0.005% (для золота будет норм из-за цены)


@dataclass
class Signal:
    symbol: str
    interval: str
    direction: str  # BUY/SELL
    entry: float
    tp: float
    sl: float
    note: str
    created_at: float


# Активные сигналы по пользователям
active_signal_by_user: Dict[int, Signal] = {}
# Фоновая задача отслеживания по пользователям
watch_task_by_user: Dict[int, asyncio.Task] = {}


def main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📌 Новый сигнал", callback_data="new_signal"),
                InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help"),
            ]
        ]
    )


HELP_TEXT = (
    "<b>Как пользоваться ботом</b>\n\n"
    "1) Нажми <b>📌 Новый сигнал</b> — бот попробует найти сигнал.\n"
    "2) Если сигнал найден, бот сам будет отслеживать цену.\n"
    "3) Новый сигнал появится только после закрытия предыдущего (TP или SL).\n\n"
    "<b>Важно</b>: сигналы не являются финансовой рекомендацией."
)


# -------------------- Индикаторы --------------------

def ema(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    out = []
    ema_prev = sum(values[:period]) / period
    out.append(ema_prev)
    for v in values[period:]:
        ema_prev = v * k + ema_prev * (1 - k)
        out.append(ema_prev)
    # выравниваем длину под values: первые period-1 значений нет
    return [None] * (period - 1) + out  # type: ignore


def rsi(values: List[float], period: int = 14) -> List[float]:
    if len(values) < period + 1:
        return []
    gains = []
    losses = []
    for i in range(1, len(values)):
        ch = values[i] - values[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out = [None] * period  # type: ignore

    def calc(g, l):
        if l == 0:
            return 100.0
        rs = g / l
        return 100 - (100 / (1 + rs))

    out.append(calc(avg_gain, avg_loss))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out.append(calc(avg_gain, avg_loss))
    return out


def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
    if len(closes) < period + 1:
        return []
    tr = []
    for i in range(1, len(closes)):
        tr1 = highs[i] - lows[i]
        tr2 = abs(highs[i] - closes[i - 1])
        tr3 = abs(lows[i] - closes[i - 1])
        tr.append(max(tr1, tr2, tr3))

    # Wilder smoothing
    atr0 = sum(tr[:period]) / period
    out = [None] * period  # type: ignore
    out.append(atr0)
    prev = atr0
    for i in range(period, len(tr)):
        prev = (prev * (period - 1) + tr[i]) / period
        out.append(prev)
    return out


# -------------------- TwelveData API --------------------

async def td_time_series(symbol: str, interval: str, outputsize: int) -> Tuple[List[float], List[float], List[float], List[float]]:
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": str(outputsize),
        "apikey": TWELVE_API_KEY,
        "format": "JSON",
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params)
        data = r.json()

    if "status" in data and data["status"] == "error":
        raise RuntimeError(data.get("message", "TwelveData error"))

    values = data.get("values") or []
    if not values:
        raise RuntimeError("No candle data returned")

    # values идут от новых к старым → переворачиваем
    values = list(reversed(values))

    opens = [float(x["open"]) for x in values]
    highs = [float(x["high"]) for x in values]
    lows = [float(x["low"]) for x in values]
    closes = [float(x["close"]) for x in values]
    return opens, highs, lows, closes


async def td_quote(symbol: str) -> float:
    url = "https://api.twelvedata.com/quote"
    params = {"symbol": symbol, "apikey": TWELVE_API_KEY, "format": "JSON"}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(url, params=params)
        data = r.json()

    if "status" in data and data["status"] == "error":
        raise RuntimeError(data.get("message", "TwelveData error"))

    # price как строка
    p = data.get("price")
    if p is None:
        raise RuntimeError("No price in quote")
    return float(p)


# -------------------- Логика сигналов --------------------

def pick_best_signal(symbol: str, interval: str, highs: List[float], lows: List[float], closes: List[float]) -> Optional[Signal]:
    # Индикаторы
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    r14 = rsi(closes, 14)
    a14 = atr(highs, lows, closes, 14)

    if not e9 or not e21 or not r14 or not a14:
        return None

    i = len(closes) - 1
    if e9[i] is None or e21[i] is None or r14[i] is None or a14[i] is None:
        return None

    close = closes[i]
    ema9 = float(e9[i])
    ema21 = float(e21[i])
    rsi14 = float(r14[i])
    atr14 = float(a14[i])

    # Фильтр: слишком маленькая волатильность → не даём сигнал
    if atr14 / max(close, 1e-9) < MIN_ATR_REL:
        return None

    # Условия (простые, но не “рандом”):
    # BUY: EMA9 > EMA21 и RSI 50..65
    # SELL: EMA9 < EMA21 и RSI 35..50
    direction = None
    if ema9 > ema21 and 50.0 <= rsi14 <= 65.0:
        direction = "BUY"
    elif ema9 < ema21 and 35.0 <= rsi14 <= 50.0:
        direction = "SELL"
    else:
        return None

    # TP/SL по ATR
    # Риск/прибыль ~1:1.6
    sl_dist = 1.0 * atr14
    tp_dist = 1.6 * atr14

    entry = close  # берем последнюю цену закрытия как entry
    if direction == "BUY":
        sl = entry - sl_dist
        tp = entry + tp_dist
    else:
        sl = entry + sl_dist
        tp = entry - tp_dist

    note = f"EMA9 vs EMA21 | RSI={rsi14:.1f} | ATR={atr14:.5f}"

    return Signal(
        symbol=symbol,
        interval=interval,
        direction=direction,
        entry=round(entry, 5),
        tp=round(tp, 5),
        sl=round(sl, 5),
        note=note,
        created_at=time.time(),
    )


async def generate_signal() -> Optional[Signal]:
    # Пробуем по всем символам — найдём “самый адекватный” по ATR (больше движение = легче отработка)
    candidates: List[Signal] = []

    for sym in SYMBOLS:
        try:
            _, highs, lows, closes = await td_time_series(sym, INTERVAL, LOOKBACK)
            sig = pick_best_signal(sym, INTERVAL, highs, lows, closes)
            if sig:
                # чем больше ATR относительно цены — тем интереснее (условно)
                atr_rel = abs(sig.tp - sig.entry) / max(sig.entry, 1e-9)
                candidates.append((atr_rel, sig))  # type: ignore
        except Exception:
            continue

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def format_signal(sig: Signal) -> str:
    emoji = "🟢 BUY" if sig.direction == "BUY" else "🔴 SELL"
    return (
        f"✅ <b>Сигнал найден.</b> Отслеживаю TP/SL автоматически.\n\n"
        f"📊 <b>{sig.symbol} SIGNAL</b> <i>({sig.interval})</i>\n\n"
        f"<b>Direction:</b> {emoji}\n"
        f"<b>Entry:</b> <code>{sig.entry}</code>\n"
        f"<b>Take Profit:</b> <code>{sig.tp}</code>\n"
        f"<b>Stop Loss:</b> <code>{sig.sl}</code>\n\n"
        f"<b>Note:</b> {sig.note}\n\n"
        f"⚠️ <i>Не является финансовой рекомендацией.</i>"
    )


async def watch_tp_sl(bot: Bot, user_id: int, sig: Signal):
    try:
        while True:
            await asyncio.sleep(PRICE_POLL_SECONDS)
            price = await td_quote(sig.symbol)

            hit_tp = False
            hit_sl = False

            if sig.direction == "BUY":
                hit_tp = price >= sig.tp
                hit_sl = price <= sig.sl
            else:
                hit_tp = price <= sig.tp
                hit_sl = price >= sig.sl

            if hit_tp or hit_sl:
                result = "🎯 <b>TP достигнут</b> ✅" if hit_tp else "🛑 <b>SL достигнут</b> ❌"
                await bot.send_message(
                    user_id,
                    f"{result}\n\n"
                    f"<b>{sig.symbol}</b> ({sig.interval})\n"
                    f"<b>Direction:</b> {sig.direction}\n"
                    f"<b>Entry:</b> <code>{sig.entry}</code>\n"
                    f"<b>TP:</b> <code>{sig.tp}</code>\n"
                    f"<b>SL:</b> <code>{sig.sl}</code>\n"
                    f"<b>Last price:</b> <code>{price:.5f}</code>\n\n"
                    f"Теперь можно запросить <b>Новый сигнал</b>.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=main_kb(),
                )
                # Снимаем активный сигнал
                active_signal_by_user.pop(user_id, None)
                return
    except asyncio.CancelledError:
        return
    except Exception:
        # Если API временно упал — просто остановим отслеживание и дадим запросить заново
        active_signal_by_user.pop(user_id, None)
        try:
            await bot.send_message(
                user_id,
                "⚠️ Ошибка при отслеживании цены (API). Сигнал сброшен — можешь запросить новый.",
                reply_markup=main_kb(),
            )
        except Exception:
            pass
        return


# -------------------- Bot --------------------

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()


@dp.message(CommandStart())
async def start(m: Message):
    # Убираем старые нижние кнопки, если они “прилипли” от прошлого бота
    await m.answer("✅ Готово. Старые кнопки убраны.", reply_markup=ReplyKeyboardRemove())
    await m.answer(
        "Привет! Я сигнальный бот.\n\n"
        "Нажми <b>📌 Новый сигнал</b>, чтобы получить сигнал.\n"
        "Новый сигнал появится только после закрытия предыдущего (TP/SL).",
        reply_markup=main_kb(),
    )


@dp.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    await c.answer()
    await c.message.answer(HELP_TEXT, reply_markup=main_kb())


@dp.callback_query(F.data == "new_signal")
async def cb_new_signal(c: CallbackQuery):
    await c.answer()
    user_id = c.from_user.id

    if user_id in active_signal_by_user:
        sig = active_signal_by_user[user_id]
        await c.message.answer(
            "⏳ У тебя уже есть активный сигнал.\n"
            "Новый будет доступен после закрытия текущего (TP/SL).\n\n"
            f"<b>{sig.symbol}</b> {sig.interval} {sig.direction}\n"
            f"Entry <code>{sig.entry}</code> | TP <code>{sig.tp}</code> | SL <code>{sig.sl}</code>",
            reply_markup=main_kb(),
        )
        return

    msg = await c.message.answer("🔎 Ищу сигнал…", reply_markup=main_kb())

    sig = await generate_signal()
    if not sig:
        await msg.edit_text(
            "⚠️ Сейчас нет достаточно сильного сигнала по фильтрам.\n"
            "Попробуй чуть позже.",
            reply_markup=main_kb(),
        )
        return

    active_signal_by_user[user_id] = sig
    await msg.edit_text(format_signal(sig), reply_markup=main_kb())

    # Стартуем отслеживание TP/SL
    old = watch_task_by_user.get(user_id)
    if old and not old.done():
        old.cancel()

    task = asyncio.create_task(watch_tp_sl(bot, user_id, sig))
    watch_task_by_user[user_id] = task


async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
