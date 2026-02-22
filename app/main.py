import os
import math
import asyncio
import random
import sqlite3
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardRemove,
)

# ==========================
# ENV
# ==========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
TWELVE_API_KEY = os.getenv("TWELVE_API_KEY", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not TWELVE_API_KEY:
    raise RuntimeError("TWELVE_API_KEY is missing")

# ==========================
# CONFIG
# ==========================
ALLOWED_SYMBOLS = ["EUR/USD", "XAU/USD"]
ALLOWED_TIMEFRAMES = ["5min", "15min", "30min"]  # TwelveData interval names

# индикаторы
EMA_FAST = 9
EMA_SLOW = 21
RSI_LEN = 14
ATR_LEN = 14

# риск/прибыль (в ATR)
TP_ATR = 1.2
SL_ATR = 1.0

# фильтр силы сигнала
MIN_EMA_GAP_ATR = 0.15   # разница EMA в долях ATR
RSI_BUY_MIN = 52
RSI_BUY_MAX = 68
RSI_SELL_MIN = 32
RSI_SELL_MAX = 48

# периодичность трекинга TP/SL
TRACK_INTERVAL_SEC = 20

DB_PATH = os.getenv("DB_PATH", "bot.db").strip()

# ==========================
# DB
# ==========================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        symbol TEXT NOT NULL,
        tf TEXT NOT NULL,
        direction TEXT NOT NULL,     -- BUY/SELL
        entry REAL NOT NULL,
        tp REAL NOT NULL,
        sl REAL NOT NULL,
        atr REAL NOT NULL,
        ema_fast REAL NOT NULL,
        ema_slow REAL NOT NULL,
        rsi REAL NOT NULL,
        status TEXT NOT NULL,        -- ACTIVE/TP/SL/CANCELLED
        opened_at TEXT NOT NULL,
        closed_at TEXT,
        close_price REAL,
        FOREIGN KEY(user_id) REFERENCES users(user_id)
    )
    """)

    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_signals_active
    ON signals(user_id, status)
    """)

    conn.commit()
    conn.close()

def ensure_user(user_id: int) -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            "INSERT INTO users(user_id, created_at) VALUES(?, ?)",
            (user_id, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
    conn.close()

def get_active_signal(user_id: int) -> Optional[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM signals WHERE user_id = ? AND status = 'ACTIVE' ORDER BY id DESC LIMIT 1",
        (user_id,)
    )
    row = cur.fetchone()
    conn.close()
    return row

def create_signal_row(
    user_id: int, symbol: str, tf: str, direction: str,
    entry: float, tp: float, sl: float,
    atr: float, ema_fast: float, ema_slow: float, rsi: float
) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO signals(
            user_id, symbol, tf, direction, entry, tp, sl,
            atr, ema_fast, ema_slow, rsi, status, opened_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'ACTIVE',?)
    """, (
        user_id, symbol, tf, direction, entry, tp, sl,
        atr, ema_fast, ema_slow, rsi,
        datetime.now(timezone.utc).isoformat()
    ))
    conn.commit()
    signal_id = cur.lastrowid
    conn.close()
    return signal_id

def close_signal(signal_id: int, status: str, close_price: float) -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE signals
        SET status = ?, closed_at = ?, close_price = ?
        WHERE id = ? AND status = 'ACTIVE'
    """, (
        status,
        datetime.now(timezone.utc).isoformat(),
        float(close_price),
        signal_id
    ))
    conn.commit()
    conn.close()

def list_all_active_signals() -> List[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM signals WHERE status = 'ACTIVE'")
    rows = cur.fetchall()
    conn.close()
    return rows

# ==========================
# TwelveData API
# ==========================
async def td_get_json(session: aiohttp.ClientSession, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=25)) as resp:
        return await resp.json()

async def td_timeseries(session: aiohttp.ClientSession, symbol: str, interval: str, outputsize: int = 120) -> List[Dict[str, Any]]:
    # https://twelvedata.com/docs#time-series
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": symbol,
        "interval": interval,
        "outputsize": outputsize,
        "format": "JSON",
        "apikey": TWELVE_API_KEY,
    }
    data = await td_get_json(session, url, params)

    if "status" in data and data["status"] == "error":
        raise RuntimeError(f"TwelveData error: {data.get('message', 'unknown')}")
    values = data.get("values")
    if not values:
        raise RuntimeError("No candles returned")
    # values are newest->oldest, we want oldest->newest
    values.reverse()
    return values

async def td_quote(session: aiohttp.ClientSession, symbol: str) -> float:
    # https://twelvedata.com/docs#quote
    url = "https://api.twelvedata.com/quote"
    params = {"symbol": symbol, "apikey": TWELVE_API_KEY}
    data = await td_get_json(session, url, params)
    if "status" in data and data["status"] == "error":
        raise RuntimeError(f"TwelveData error: {data.get('message', 'unknown')}")
    price = data.get("close") or data.get("price")
    if price is None:
        raise RuntimeError("No price returned")
    return float(price)

# ==========================
# Indicators
# ==========================
def ema(values: List[float], length: int) -> float:
    if len(values) < length:
        raise ValueError("not enough values for ema")
    k = 2 / (length + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e

def rsi(closes: List[float], length: int) -> float:
    if len(closes) < length + 1:
        raise ValueError("not enough values for rsi")
    gains = 0.0
    losses = 0.0
    for i in range(1, length + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses += -diff
    avg_gain = gains / length
    avg_loss = losses / length if losses > 0 else 1e-12
    rs = avg_gain / avg_loss
    out = 100 - (100 / (1 + rs))
    # Wilder smoothing for the rest
    for i in range(length + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = max(diff, 0.0)
        loss = max(-diff, 0.0)
        avg_gain = (avg_gain * (length - 1) + gain) / length
        avg_loss = (avg_loss * (length - 1) + loss) / length if (avg_loss * (length - 1) + loss) > 0 else 1e-12
        rs = avg_gain / avg_loss
        out = 100 - (100 / (1 + rs))
    return float(out)

def atr(highs: List[float], lows: List[float], closes: List[float], length: int) -> float:
    if len(closes) < length + 1:
        raise ValueError("not enough values for atr")
    trs: List[float] = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    # simple avg first
    atr_val = sum(trs[:length]) / length
    # Wilder smoothing
    for i in range(length, len(trs)):
        atr_val = (atr_val * (length - 1) + trs[i]) / length
    return float(atr_val)

# ==========================
# Signal logic
# ==========================
def fmt_price(x: float, symbol: str) -> str:
    # EURUSD -> 5 знаков, XAUUSD -> 2 знака
    if symbol == "XAU/USD":
        return f"{x:.2f}"
    return f"{x:.5f}"

def decide_signal(
    closes: List[float], highs: List[float], lows: List[float], symbol: str
) -> Optional[Dict[str, Any]]:
    # используем последние N точек
    c = closes[-80:]
    h = highs[-80:]
    l = lows[-80:]
    if len(c) < 30:
        return None

    ema_fast = ema(c, EMA_FAST)
    ema_slow = ema(c, EMA_SLOW)
    rsi_v = rsi(c, RSI_LEN)
    atr_v = atr(h, l, c, ATR_LEN)

    if atr_v <= 0:
        return None

    gap = abs(ema_fast - ema_slow)
    gap_atr = gap / atr_v

    last = c[-1]

    # BUY rules
    if ema_fast > ema_slow and RSI_BUY_MIN <= rsi_v <= RSI_BUY_MAX and gap_atr >= MIN_EMA_GAP_ATR:
        entry = last
        tp = entry + atr_v * TP_ATR
        sl = entry - atr_v * SL_ATR
        return {
            "direction": "BUY",
            "entry": entry,
            "tp": tp,
            "sl": sl,
            "atr": atr_v,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "rsi": rsi_v,
        }

    # SELL rules
    if ema_fast < ema_slow and RSI_SELL_MIN <= rsi_v <= RSI_SELL_MAX and gap_atr >= MIN_EMA_GAP_ATR:
        entry = last
        tp = entry - atr_v * TP_ATR
        sl = entry + atr_v * SL_ATR
        return {
            "direction": "SELL",
            "entry": entry,
            "tp": tp,
            "sl": sl,
            "atr": atr_v,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "rsi": rsi_v,
        }

    return None

async def build_signal(session: aiohttp.ClientSession) -> Optional[Tuple[str, str, Dict[str, Any]]]:
    # пробуем несколько комбинаций (символ/ТФ), чтобы найти сигнал
    combos = [(s, tf) for s in ALLOWED_SYMBOLS for tf in ALLOWED_TIMEFRAMES]
    random.shuffle(combos)

    for symbol, tf in combos:
        try:
            candles = await td_timeseries(session, symbol, tf, outputsize=120)
            closes = [float(x["close"]) for x in candles]
            highs = [float(x["high"]) for x in candles]
            lows = [float(x["low"]) for x in candles]

            sig = decide_signal(closes, highs, lows, symbol)
            if sig:
                return symbol, tf, sig
        except Exception:
            continue

    return None

# ==========================
# UI
# ==========================
def kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📌 Новый сигнал", callback_data="new_signal")],
        [InlineKeyboardButton(text="ℹ️ Помощь", callback_data="help")],
    ])

HELP_TEXT = (
    "<b>ℹ️ Помощь</b>\n\n"
    "1) Нажми <b>📌 Новый сигнал</b>, чтобы получить сигнал.\n"
    "2) Пока есть <b>активный сигнал</b>, новый не выдаётся.\n"
    "3) Бот автоматически отслеживает <b>TP/SL</b> и сообщит результат.\n\n"
    "<b>Инструменты:</b> EUR/USD и XAU/USD\n"
    "<b>Таймфреймы:</b> 5m / 15m / 30m\n\n"
    "⚠️ <i>Сигналы не являются финансовой рекомендацией.</i>"
)

def signal_text(symbol: str, tf: str, sig: Dict[str, Any]) -> str:
    d = sig["direction"]
    entry = sig["entry"]
    tp = sig["tp"]
    sl = sig["sl"]
    rsi_v = sig["rsi"]
    atr_v = sig["atr"]
    ema_fast = sig["ema_fast"]
    ema_slow = sig["ema_slow"]

    tf_label = {"5min": "5MIN", "15min": "15MIN", "30min": "30MIN"}.get(tf, tf)

    return (
        "✅ <b>Сигнал найден. Отслеживаю TP/SL автоматически.</b>\n\n"
        f"📈 <b>{symbol} SIGNAL ({tf_label})</b>\n\n"
        f"<b>Direction:</b> {'🟢 BUY' if d == 'BUY' else '🔴 SELL'}\n"
        f"<b>Entry:</b> <code>{fmt_price(entry, symbol)}</code>\n"
        f"<b>Take Profit:</b> <code>{fmt_price(tp, symbol)}</code>\n"
        f"<b>Stop Loss:</b> <code>{fmt_price(sl, symbol)}</code>\n\n"
        f"<b>Note:</b> EMA{EMA_FAST}/EMA{EMA_SLOW} | RSI={rsi_v:.1f} | ATR={atr_v:.5f}\n\n"
        "⚠️ <i>Не является финансовой рекомендацией.</i>"
    )

def active_signal_text(row: sqlite3.Row) -> str:
    symbol = row["symbol"]
    tf = row["tf"]
    tf_label = {"5min": "5MIN", "15min": "15MIN", "30min": "30MIN"}.get(tf, tf)
    return (
        "⏳ <b>У тебя уже есть активный сигнал.</b>\n"
        "Новый появится после закрытия (TP/SL).\n\n"
        f"📌 <b>{symbol} ({tf_label})</b>\n"
        f"<b>Direction:</b> {row['direction']}\n"
        f"<b>Entry:</b> <code>{fmt_price(float(row['entry']), symbol)}</code>\n"
        f"<b>TP:</b> <code>{fmt_price(float(row['tp']), symbol)}</code>\n"
        f"<b>SL:</b> <code>{fmt_price(float(row['sl']), symbol)}</code>\n"
    )

# ==========================
# BOT
# ==========================
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

# ==========================
# Handlers
# ==========================
@dp.message(CommandStart())
async def start(m: Message):
    ensure_user(m.from_user.id)

    # Принудительно убираем старые reply-кнопки (серые снизу)
    await m.answer("Клавиатура обновлена ✅", reply_markup=ReplyKeyboardRemove())

    await m.answer(
        "Привет! Я выдаю сигналы по EUR/USD и XAU/USD.\n\n"
        "• Таймфреймы: 5m / 15m / 30m\n"
        "• Новый сигнал только после закрытия предыдущего (TP/SL)\n\n"
        "Нажми кнопку ниже 👇",
        reply_markup=kb(),
    )

@dp.callback_query(lambda c: c.data == "help")
async def on_help(c: CallbackQuery):
    await c.answer()
    await c.message.answer(HELP_TEXT, reply_markup=kb())

@dp.callback_query(lambda c: c.data == "new_signal")
async def on_new_signal(c: CallbackQuery):
    await c.answer()
    user_id = c.from_user.id
    ensure_user(user_id)

    active = get_active_signal(user_id)
    if active:
        await c.message.answer(active_signal_text(active), reply_markup=kb())
        return

    await c.message.answer("🔎 Ищу сильный сигнал...", reply_markup=kb())

    async with aiohttp.ClientSession() as session:
        found = await build_signal(session)

    if not found:
        await c.message.answer(
            "Сейчас нет достаточно сильного сигнала. Попробуй позже.",
            reply_markup=kb(),
        )
        return

    symbol, tf, sig = found

    # Записываем в БД
    create_signal_row(
        user_id=user_id,
        symbol=symbol,
        tf=tf,
        direction=sig["direction"],
        entry=float(sig["entry"]),
        tp=float(sig["tp"]),
        sl=float(sig["sl"]),
        atr=float(sig["atr"]),
        ema_fast=float(sig["ema_fast"]),
        ema_slow=float(sig["ema_slow"]),
        rsi=float(sig["rsi"]),
    )

    await c.message.answer(signal_text(symbol, tf, sig), reply_markup=kb())

# ==========================
# Tracker
# ==========================
async def tracker_loop():
    # Постоянно проверяет активные сигналы по TP/SL
    await asyncio.sleep(3)  # небольшая задержка на старт
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                actives = list_all_active_signals()
                for row in actives:
                    signal_id = int(row["id"])
                    user_id = int(row["user_id"])
                    symbol = row["symbol"]
                    direction = row["direction"]
                    tp = float(row["tp"])
                    sl = float(row["sl"])

                    try:
                        price = await td_quote(session, symbol)
                    except Exception:
                        continue

                    hit_tp = (price >= tp) if direction == "BUY" else (price <= tp)
                    hit_sl = (price <= sl) if direction == "BUY" else (price >= sl)

                    if hit_tp:
                        close_signal(signal_id, "TP", price)
                        await bot.send_message(
                            user_id,
                            f"✅ <b>TP достигнут</b>\n"
                            f"{symbol} | <code>{fmt_price(price, symbol)}</code>\n\n"
                            "Теперь можно запросить новый сигнал.",
                            reply_markup=kb(),
                        )
                    elif hit_sl:
                        close_signal(signal_id, "SL", price)
                        await bot.send_message(
                            user_id,
                            f"❌ <b>SL сработал</b>\n"
                            f"{symbol} | <code>{fmt_price(price, symbol)}</code>\n\n"
                            "Теперь можно запросить новый сигнал.",
                            reply_markup=kb(),
                        )

            except Exception:
                # чтобы цикл не падал
                pass

            await asyncio.sleep(TRACK_INTERVAL_SEC)

async def main():
    init_db()
    # запускаем трекер
    asyncio.create_task(tracker_loop())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
