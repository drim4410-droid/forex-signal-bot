import os
import asyncio
import sqlite3
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone

import httpx
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties

# ================== ENV ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_KEY = os.getenv("TWELVE_API_KEY")

if not BOT_TOKEN or not API_KEY:
    raise RuntimeError("Missing BOT_TOKEN or TWELVE_API_KEY in Railway Variables")

SYMBOLS = ["EUR/USD", "XAU/USD"]
POLL_SECONDS = 10          # проверка цены для TP/SL
MIN_SIGNAL_GAP_SEC = 60    # чтобы не спамить (если пользователь быстро тыкает)

DB_PATH = "bot.db"

# ================== DATA ==================
@dataclass
class Signal:
    user_id: int
    symbol: str
    tf: str                 # "5/15/30m"
    direction: str          # "BUY" / "SELL"
    entry: float
    tp: float
    sl: float
    atr: float
    created_at: int         # unix
    last_price: float = 0.0


active_signals: Dict[int, Signal] = {}
watch_tasks: Dict[int, asyncio.Task] = {}

# ================== DB ==================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            user_id INTEGER PRIMARY KEY,
            total INTEGER NOT NULL DEFAULT 0,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            last_signal_ts INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()

def ensure_user(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO stats(user_id) VALUES(?)", (user_id,))
    conn.commit()
    conn.close()

def get_stats(user_id: int) -> Tuple[int, int, int, int]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT total, wins, losses, last_signal_ts FROM stats WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return (0, 0, 0, 0)
    return row  # total, wins, losses, last_signal_ts

def set_last_signal_ts(user_id: int, ts: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE stats SET last_signal_ts=? WHERE user_id=?", (ts, user_id))
    conn.commit()
    conn.close()

def add_signal_total(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE stats SET total=total+1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def add_win(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE stats SET wins=wins+1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def add_loss(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE stats SET losses=losses+1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

# ================== INDICATORS ==================
def ema(values: List[float], period: int) -> List[Optional[float]]:
    if len(values) < period:
        return [None] * len(values)
    k = 2 / (period + 1)
    ema_val = sum(values[:period]) / period
    out: List[Optional[float]] = [None] * (period - 1) + [ema_val]
    for price in values[period:]:
        ema_val = price * k + ema_val * (1 - k)
        out.append(ema_val)
    return out

def rsi(values: List[float], period: int = 14) -> List[Optional[float]]:
    if len(values) < period + 1:
        return [None] * len(values)
    gains, losses = [], []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def calc_rsi(ag, al):
        if al == 0:
            return 100.0
        rs = ag / al
        return 100 - 100 / (1 + rs)

    out: List[Optional[float]] = [None] * period + [calc_rsi(avg_gain, avg_loss)]
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out.append(calc_rsi(avg_gain, avg_loss))

    if len(out) < len(values):
        out = out + [out[-1]] * (len(values) - len(out))
    return out[:len(values)]

def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[Optional[float]]:
    if len(closes) < period + 1:
        return [None] * len(closes)
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1])
        )
        trs.append(tr)
    atr0 = sum(trs[:period]) / period
    out: List[Optional[float]] = [None] * period + [atr0]
    atr_val = atr0
    for i in range(period, len(trs)):
        atr_val = (atr_val * (period - 1) + trs[i]) / period
        out.append(atr_val)
    if len(out) < len(closes):
        out = out + [out[-1]] * (len(closes) - len(out))
    return out[:len(closes)]

def last_swing(highs: List[float], lows: List[float], lookback: int = 20) -> Tuple[float, float]:
    h = max(highs[-lookback:])
    l = min(lows[-lookback:])
    return h, l

# ================== TWELVEDATA API ==================
async def get_candles(symbol: str, interval: str, outputsize: int = 200):
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": str(outputsize),
        "apikey": API_KEY,
    }
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params)
        data = r.json()

    if "values" not in data:
        raise RuntimeError(f"TwelveData error: {data}")

    values = list(reversed(data["values"]))
    closes = [float(v["close"]) for v in values]
    highs = [float(v["high"]) for v in values]
    lows = [float(v["low"]) for v in values]
    return closes, highs, lows

async def get_price(symbol: str) -> float:
    url = "https://api.twelvedata.com/price"
    params = {"symbol": symbol, "apikey": API_KEY}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url, params=params)
        data = r.json()
    if "price" not in data:
        raise RuntimeError(f"TwelveData price error: {data}")
    return float(data["price"])

# ================== SIGNAL ENGINE ==================
def score_signal(trend_strength: float, rsi_ok: bool, pullback_ok: bool, structure_ok: bool) -> float:
    # простая “оценка качества”: чем выше, тем лучше
    s = 0.0
    s += min(abs(trend_strength), 1.0) * 2.0
    s += 1.0 if rsi_ok else 0.0
    s += 1.0 if pullback_ok else 0.0
    s += 1.0 if structure_ok else 0.0
    return s

async def find_best_signal() -> Optional[Tuple[str, str, float, float, float, float]]:
    """
    Возвращает: symbol, direction, entry, tp, sl, atr_value
    """
    best = None
    best_score = 0.0

    for symbol in SYMBOLS:
        # 30m trend filter
        c30, h30, l30 = await get_candles(symbol, "30min", 200)
        e30_50 = ema(c30, 50)
        e30_200 = ema(c30, 200)
        if not e30_50[-1] or not e30_200[-1]:
            continue

        trend_up = e30_50[-1] > e30_200[-1]
        trend_down = e30_50[-1] < e30_200[-1]
        trend_strength = (e30_50[-1] - e30_200[-1]) / c30[-1]

        # 15m confirm
        c15, h15, l15 = await get_candles(symbol, "15min", 200)
        e15_20 = ema(c15, 20)
        e15_50 = ema(c15, 50)
        r15 = rsi(c15, 14)
        if not e15_20[-1] or not e15_50[-1] or not r15[-1]:
            continue

        # 5m entry
        c5, h5, l5 = await get_candles(symbol, "5min", 200)
        e5_9 = ema(c5, 9)
        e5_21 = ema(c5, 21)
        r5 = rsi(c5, 14)
        a5 = atr(h5, l5, c5, 14)
        if not e5_9[-1] or not e5_21[-1] or not r5[-1] or not a5[-1]:
            continue

        entry = c5[-1]
        atr_v = a5[-1]
        if atr_v <= 0:
            continue

        # "SMC-ish" структура: цена возле диапазона свинга (чуть ближе к границам)
        swing_h, swing_l = last_swing(h5, l5, 25)
        near_high = (swing_h - entry) / max(entry, 1e-9) < 0.002
        near_low = (entry - swing_l) / max(entry, 1e-9) < 0.002

        # Pullback to EMA (чтобы не входить в “середине пустоты”)
        pullback_ok_buy = abs(entry - e5_21[-1]) / entry < 0.0015
        pullback_ok_sell = abs(entry - e5_21[-1]) / entry < 0.0015

        # BUY setup
        if trend_up and e15_20[-1] > e15_50[-1] and e5_9[-1] > e5_21[-1]:
            rsi_ok = 48.0 <= r5[-1] <= 66.0 and r15[-1] >= 45.0
            structure_ok = near_low  # покупка лучше от нижней части диапазона
            s = score_signal(trend_strength, rsi_ok, pullback_ok_buy, structure_ok)
            if s > best_score and s >= 3.2:
                sl = entry - 1.0 * atr_v
                tp = entry + 3.0 * atr_v
                best = (symbol, "BUY", round(entry, 5), round(tp, 5), round(sl, 5), float(atr_v))
                best_score = s

        # SELL setup
        if trend_down and e15_20[-1] < e15_50[-1] and e5_9[-1] < e5_21[-1]:
            rsi_ok = 34.0 <= r5[-1] <= 52.0 and r15[-1] <= 55.0
            structure_ok = near_high  # продажа лучше от верхней части диапазона
            s = score_signal(trend_strength, rsi_ok, pullback_ok_sell, structure_ok)
            if s > best_score and s >= 3.2:
                sl = entry + 1.0 * atr_v
                tp = entry - 3.0 * atr_v
                best = (symbol, "SELL", round(entry, 5), round(tp, 5), round(sl, 5), float(atr_v))
                best_score = s

    return best

def now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())

def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

# ================== UI ==================
def kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📌 Новый сигнал", callback_data="new_signal"),
            InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help"),
        ]
    ])

def signal_text(sig: Signal) -> str:
    return (
        f"📊 <b>{sig.symbol} SIGNAL</b> <i>({sig.tf})</i>\n\n"
        f"<b>Direction:</b> {'🟢 BUY' if sig.direction=='BUY' else '🔴 SELL'}\n"
        f"<b>Entry:</b> <code>{sig.entry}</code>\n"
        f"<b>Take Profit:</b> <code>{sig.tp}</code>\n"
        f"<b>Stop Loss:</b> <code>{sig.sl}</code>\n\n"
        f"<b>Note:</b> ATR={sig.atr:.6f} | RR=1:3\n\n"
        f"⚠️ <i>Не является финансовой рекомендацией.</i>"
    )

def close_text(symbol: str, result: str, price: float) -> str:
    return f"✅ <b>{symbol}</b> закрыт: <b>{result}</b>\nЦена: <code>{price}</code>\n\nТеперь можно запросить новый сигнал."

# ================== BOT ==================
bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

@dp.message(CommandStart())
async def start(m: Message):
    ensure_user(m.from_user.id)
    await m.answer(
        "Привет! Я сигнальный бот.\n\n"
        "• Доступные пары: EUR/USD, XAU/USD\n"
        "• Таймфреймы: 5m/15m/30m\n"
        "• Новый сигнал появится только после закрытия предыдущего (TP/SL).\n\n"
        "Нажми кнопку ниже 👇",
        reply_markup=kb(),
    )

@dp.callback_query(F.data == "help")
async def help_cb(c: CallbackQuery):
    await c.answer()
    uid = c.from_user.id
    ensure_user(uid)
    total, wins, losses, _ = get_stats(uid)
    wr = (wins / total * 100.0) if total > 0 else 0.0

    active = active_signals.get(uid)
    active_line = ""
    if active:
        active_line = f"\n\n📌 Активный сигнал: <b>{active.symbol}</b> ({active.direction})"

    await c.message.answer(
        "ℹ️ <b>Как пользоваться</b>\n\n"
        "1) Нажми <b>Новый сигнал</b> — бот проверит EUR/USD и XAU/USD.\n"
        "2) Если сетап сильный — выдаст сигнал и начнёт автоматически отслеживать TP/SL.\n"
        "3) Пока сигнал активен — новый не выдаётся.\n\n"
        f"📈 Статистика: всего <b>{total}</b> | ✅ <b>{wins}</b> | ❌ <b>{losses}</b> | WR <b>{wr:.1f}%</b>"
        f"{active_line}\n\n"
        "⚠️ Сигналы — не гарантия прибыли. Рынок может вести себя непредсказуемо.",
        reply_markup=kb(),
    )

@dp.callback_query(F.data == "new_signal")
async def new_signal_cb(c: CallbackQuery):
    await c.answer()
    uid = c.from_user.id
    ensure_user(uid)

    if uid in active_signals:
        await c.message.answer("⏳ Есть активный сигнал. Дождись TP/SL.", reply_markup=kb())
        return

    total, wins, losses, last_ts = get_stats(uid)
    ts = now_ts()
    if last_ts and (ts - last_ts) < MIN_SIGNAL_GAP_SEC:
        await c.message.answer("⏳ Подожди немного и попробуй ещё раз.", reply_markup=kb())
        return

    set_last_signal_ts(uid, ts)

    msg = await c.message.answer("🔎 Анализирую рынок (5m/15m/30m)...", reply_markup=kb())

    try:
        best = await find_best_signal()
    except Exception as e:
        await msg.edit_text(f"Ошибка данных. Попробуй позже.\n<code>{e}</code>", reply_markup=kb())
        return

    if not best:
        await msg.edit_text("Сейчас нет достаточно сильного сигнала. Попробуй позже.", reply_markup=kb())
        return

    symbol, direction, entry, tp, sl, atr_v = best
    sig = Signal(
        user_id=uid,
        symbol=symbol,
        tf="5/15/30m",
        direction=direction,
        entry=float(entry),
        tp=float(tp),
        sl=float(sl),
        atr=float(atr_v),
        created_at=ts,
    )
    active_signals[uid] = sig
    add_signal_total(uid)

    await msg.edit_text("✅ Сигнал найден. Отслеживаю TP/SL автоматически.\n\n" + signal_text(sig), reply_markup=kb())

    # запуск наблюдателя
    task = asyncio.create_task(watch_tp_sl(sig))
    watch_tasks[uid] = task

async def watch_tp_sl(sig: Signal):
    uid = sig.user_id
    while True:
        await asyncio.sleep(POLL_SECONDS)
        try:
            price = await get_price(sig.symbol)
        except Exception:
            continue

        sig.last_price = price

        if sig.direction == "BUY":
            if price >= sig.tp:
                add_win(uid)
                break
            if price <= sig.sl:
                add_loss(uid)
                break
        else:  # SELL
            if price <= sig.tp:
                add_win(uid)
                break
            if price >= sig.sl:
                add_loss(uid)
                break

    # Закрываем сигнал
    active_signals.pop(uid, None)
    watch_tasks.pop(uid, None)

    result = "TP ✅" if ((sig.direction == "BUY" and sig.last_price >= sig.tp) or (sig.direction == "SELL" and sig.last_price <= sig.tp)) else "SL ❌"
    await bot.send_message(uid, close_text(sig.symbol, result, sig.last_price), reply_markup=kb())

async def main():
    init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
