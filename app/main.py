import os
import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode


# ----------------------------
# ENV
# ----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not TWELVE_API_KEY:
    raise RuntimeError("TWELVE_API_KEY is not set")


# ----------------------------
# LOGGING
# ----------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("signal_bot")


# ----------------------------
# SETTINGS
# ----------------------------
DB_PATH = os.getenv("DB_PATH", "app/data.db")

# Пары, которые бот будет анализировать (можешь оставить только нужные)
# TwelveData чаще принимает форекс как "EUR/USD", золото как "XAU/USD"
SYMBOLS = ["EUR/USD", "XAU/USD"]

TIMEFRAME = "5min"
CANDLES_LIMIT = 120  # достаточно для EMA/RSI/ATR

# Индикаторы
EMA_FAST = 9
EMA_SLOW = 21
RSI_LEN = 14
ATR_LEN = 14

# Мультипликаторы риск/профит
SL_ATR_MULT = 1.0
TP_ATR_MULT = 1.5

# Как часто проверять цену для закрытия сигнала
PRICE_CHECK_SECONDS = 30


# ----------------------------
# DB
# ----------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS active_signals (
                user_id INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                entry REAL NOT NULL,
                tp REAL NOT NULL,
                sl REAL NOT NULL,
                note TEXT,
                opened_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


@dataclass
class Signal:
    user_id: int
    symbol: str
    direction: str  # BUY/SELL
    timeframe: str
    entry: float
    tp: float
    sl: float
    note: str
    opened_at: str  # ISO


def get_active_signal(user_id: int) -> Optional[Signal]:
    with db() as conn:
        row = conn.execute(
            "SELECT user_id, symbol, direction, timeframe, entry, tp, sl, COALESCE(note,''), opened_at "
            "FROM active_signals WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return Signal(*row)


def set_active_signal(s: Signal) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO active_signals (user_id, symbol, direction, timeframe, entry, tp, sl, note, opened_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                symbol=excluded.symbol,
                direction=excluded.direction,
                timeframe=excluded.timeframe,
                entry=excluded.entry,
                tp=excluded.tp,
                sl=excluded.sl,
                note=excluded.note,
                opened_at=excluded.opened_at
            """,
            (s.user_id, s.symbol, s.direction, s.timeframe, s.entry, s.tp, s.sl, s.note, s.opened_at),
        )
        conn.commit()


def clear_active_signal(user_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM active_signals WHERE user_id=?", (user_id,))
        conn.commit()


def list_all_active_signals() -> List[Signal]:
    with db() as conn:
        rows = conn.execute(
            "SELECT user_id, symbol, direction, timeframe, entry, tp, sl, COALESCE(note,''), opened_at FROM active_signals"
        ).fetchall()
    return [Signal(*r) for r in rows]


# ----------------------------
# INDICATORS
# ----------------------------
def ema(values: List[float], period: int) -> List[float]:
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    out = []
    prev = sum(values[:period]) / period
    out.extend([None] * (period - 1))
    out.append(prev)
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(values: List[float], period: int) -> List[float]:
    if len(values) < period + 1:
        return []
    gains = []
    losses = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))

    out = [None] * period
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    rs = (avg_gain / avg_loss) if avg_loss != 0 else float("inf")
    out.append(100 - (100 / (1 + rs)))

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = (avg_gain / avg_loss) if avg_loss != 0 else float("inf")
        out.append(100 - (100 / (1 + rs)))
    return out


def atr(highs: List[float], lows: List[float], closes: List[float], period: int) -> List[float]:
    if len(closes) < period + 1:
        return []
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)

    out = [None] * period
    prev = sum(trs[:period]) / period
    out.append(prev)
    for tr in trs[period:]:
        prev = (prev * (period - 1) + tr) / period
        out.append(prev)
    return out


# ----------------------------
# TWELVE DATA API
# ----------------------------
async def td_time_series(session: aiohttp.ClientSession, symbol: str, interval: str, outputsize: int) -> Dict[str, Any]:
    # https://twelvedata.com/docs#time-series
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": str(outputsize),
        "apikey": TWELVE_API_KEY,
        "format": "JSON",
    }
    async with session.get("https://api.twelvedata.com/time_series", params=params, timeout=20) as resp:
        data = await resp.json()
        return data


async def td_price(session: aiohttp.ClientSession, symbol: str) -> Optional[float]:
    # https://twelvedata.com/docs#price
    params = {"symbol": symbol, "apikey": TWELVE_API_KEY, "format": "JSON"}
    async with session.get("https://api.twelvedata.com/price", params=params, timeout=20) as resp:
        data = await resp.json()
        if isinstance(data, dict) and "price" in data:
            try:
                return float(data["price"])
            except Exception:
                return None
        return None


def parse_candles(td_data: Dict[str, Any]) -> Tuple[List[float], List[float], List[float]]:
    # TwelveData returns "values" as list of dicts, newest first
    if "values" not in td_data or not isinstance(td_data["values"], list):
        raise ValueError(f"TwelveData error: {td_data}")

    values = td_data["values"][::-1]  # oldest -> newest
    closes, highs, lows = [], [], []
    for v in values:
        closes.append(float(v["close"]))
        highs.append(float(v["high"]))
        lows.append(float(v["low"]))
    return closes, highs, lows


# ----------------------------
# SIGNAL ENGINE (простая, но адекватная логика)
# ----------------------------
def build_signal(symbol: str, closes: List[float], highs: List[float], lows: List[float]) -> Optional[Tuple[str, float, float, float, str]]:
    """
    Возвращает (direction, entry, tp, sl, note) или None если условий нет.
    Логика:
      - тренд: EMA(9) vs EMA(21)
      - фильтр: RSI не перегрет (для BUY < 70, для SELL > 30)
      - SL/TP от ATR
    """
    if len(closes) < max(EMA_SLOW, RSI_LEN + 1, ATR_LEN + 1) + 5:
        return None

    ema_fast = ema(closes, EMA_FAST)
    ema_slow = ema(closes, EMA_SLOW)
    rsi_v = rsi(closes, RSI_LEN)
    atr_v = atr(highs, lows, closes, ATR_LEN)

    if not ema_fast or not ema_slow or not rsi_v or not atr_v:
        return None

    last_close = closes[-1]
    ef = ema_fast[-1]
    es = ema_slow[-1]
    rv = rsi_v[-1]
    av = atr_v[-1]

    if ef is None or es is None or rv is None or av is None:
        return None

    # Тренд + фильтр по RSI
    direction = None
    if ef > es and rv < 70:
        direction = "BUY"
    elif ef < es and rv > 30:
        direction = "SELL"
    else:
        return None

    entry = last_close
    if direction == "BUY":
        sl = entry - (SL_ATR_MULT * av)
        tp = entry + (TP_ATR_MULT * av)
    else:
        sl = entry + (SL_ATR_MULT * av)
        tp = entry - (TP_ATR_MULT * av)

    note = f"EMA{EMA_FAST}>{EMA_SLOW if direction=='BUY' else ''}{''} | RSI={rv:.1f} | ATR={av:.5f}"
    return direction, entry, tp, sl, note


def fmt_price(x: float, symbol: str) -> str:
    # форекс обычно 5 знаков, золото 2
    if "XAU" in symbol:
        return f"{x:.2f}"
    return f"{x:.5f}"


def signal_text(s: Signal) -> str:
    dir_emoji = "🟢 BUY" if s.direction == "BUY" else "🔴 SELL"
    return (
        f"📊 <b>{s.symbol} SIGNAL</b> <i>({s.timeframe})</i>\n\n"
        f"<b>Direction:</b> {dir_emoji}\n"
        f"<b>Entry:</b> <code>{fmt_price(s.entry, s.symbol)}</code>\n"
        f"<b>Take Profit:</b> <code>{fmt_price(s.tp, s.symbol)}</code>\n"
        f"<b>Stop Loss:</b> <code>{fmt_price(s.sl, s.symbol)}</code>\n\n"
        f"<b>Note:</b> {s.note}\n\n"
        f"⚠️ <i>Не является финансовой рекомендацией.</i>"
    )


def help_text() -> str:
    return (
        "ℹ️ <b>Как пользоваться ботом</b>\n\n"
        "1) Нажми <b>Новый сигнал</b> — бот попробует найти сетап по рынку.\n"
        "2) Пока сигнал активен, новый не выдаётся.\n"
        "3) Бот сам отслеживает цену и закроет сигнал, когда будет достигнут <b>TP</b> или <b>SL</b>.\n\n"
        "⚠️ Важно: это экспериментальная аналитика, не гарантия прибыли."
    )


# ----------------------------
# TELEGRAM UI
# ----------------------------
def main_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📌 Новый сигнал", callback_data="new_signal")
    kb.button(text="ℹ️ Помощь", callback_data="help")
    kb.adjust(2)
    return kb.as_markup()


# ----------------------------
# BOT
# ----------------------------
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

# Background tasks
_price_task: Optional[asyncio.Task] = None


@dp.message(CommandStart())
async def start(m: Message):
    await m.answer(
        "Привет! Я сигнальный бот.\n\n"
        "Нажми кнопку <b>Новый сигнал</b>, чтобы получить сигнал.\n"
        "Новый сигнал появится только после закрытия предыдущего (TP/SL).",
        reply_markup=main_kb(),
    )


@dp.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    await c.answer()
    await c.message.answer(help_text(), reply_markup=main_kb())


@dp.callback_query(F.data == "new_signal")
async def cb_new_signal(c: CallbackQuery):
    await c.answer()
    user_id = c.from_user.id

    active = get_active_signal(user_id)
    if active:
        await c.message.answer(
            "⛔️ У тебя уже есть активный сигнал. Новый появится после закрытия TP/SL.\n\n"
            + signal_text(active),
            reply_markup=main_kb(),
        )
        return

    # Получаем свечи и пытаемся построить сигнал
    async with aiohttp.ClientSession() as session:
        best = None  # (score, Signal)
        for sym in SYMBOLS:
            try:
                data = await td_time_series(session, sym, TIMEFRAME, CANDLES_LIMIT)
                closes, highs, lows = parse_candles(data)
                built = build_signal(sym, closes, highs, lows)
                if not built:
                    continue
                direction, entry, tp, sl, note = built

                # простой "скоринг": чем дальше TP от entry относительно ATR - тем лучше,
                # но мы уже фиксировали TP/SL. Поэтому просто предпочтём форекс, если оба есть
                score = 1.0
                if "EUR" in sym:
                    score += 0.1

                sig = Signal(
                    user_id=user_id,
                    symbol=sym,
                    direction=direction,
                    timeframe=TIMEFRAME.upper(),
                    entry=float(entry),
                    tp=float(tp),
                    sl=float(sl),
                    note=note,
                    opened_at=datetime.now(timezone.utc).isoformat(),
                )
                if best is None or score > best[0]:
                    best = (score, sig)
            except Exception as e:
                logger.warning("Failed to build signal for %s: %s", sym, e)

        if not best:
            await c.message.answer(
                "Сейчас нет хорошего сетапа по моим фильтрам. Попробуй через 5–15 минут.",
                reply_markup=main_kb(),
            )
            return

        sig = best[1]
        set_active_signal(sig)
        await c.message.answer("✅ Сигнал найден. Отслеживаю TP/SL автоматически.\n\n" + signal_text(sig), reply_markup=main_kb())


async def price_watcher():
    """
    Следит за всеми активными сигналами и закрывает при достижении TP/SL.
    """
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                actives = list_all_active_signals()
                if actives:
                    # группируем по symbol чтобы не дергать цену 100 раз
                    symbols = sorted(set(s.symbol for s in actives))
                    prices: Dict[str, Optional[float]] = {}
                    for sym in symbols:
                        prices[sym] = await td_price(session, sym)

                    for s in actives:
                        p = prices.get(s.symbol)
                        if p is None:
                            continue

                        hit_tp = (p >= s.tp) if s.direction == "BUY" else (p <= s.tp)
                        hit_sl = (p <= s.sl) if s.direction == "BUY" else (p >= s.sl)

                        if hit_tp or hit_sl:
                            result = "🎯 TP достигнут" if hit_tp else "🛑 SL достигнут"
                            text = (
                                f"{result}\n\n"
                                f"<b>{s.symbol}</b> ({s.timeframe})\n"
                                f"Направление: <b>{s.direction}</b>\n"
                                f"Текущая цена: <code>{fmt_price(p, s.symbol)}</code>\n"
                                f"Entry: <code>{fmt_price(s.entry, s.symbol)}</code>\n"
                                f"TP: <code>{fmt_price(s.tp, s.symbol)}</code>\n"
                                f"SL: <code>{fmt_price(s.sl, s.symbol)}</code>\n\n"
                                f"Теперь можно запросить новый сигнал."
                            )
                            clear_active_signal(s.user_id)
                            try:
                                await bot.send_message(s.user_id, text, reply_markup=main_kb())
                            except Exception as e:
                                logger.warning("Failed to notify user %s: %s", s.user_id, e)

            except Exception as e:
                logger.exception("Watcher loop error: %s", e)

            await asyncio.sleep(PRICE_CHECK_SECONDS)


async def on_startup():
    global _price_task
    init_db()
    _price_task = asyncio.create_task(price_watcher())
    logger.info("Bot started. Watcher running.")


async def main():
    await on_startup()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
