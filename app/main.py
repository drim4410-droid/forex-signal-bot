import asyncio
import os
import re
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


# =========================
# CONFIG (Railway Variables)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_TG_ID", "0"))

# Payment
PAY_ADDRESS = os.getenv("PAY_ADDRESS", "0x2bf4964c53c208966b007c30398c23198f018460").lower().strip()
SUB_PRICE_USDT = float(os.getenv("SUB_PRICE_USDT", "30"))
BSC_API_KEY = os.getenv("BSC_API_KEY", "").strip()
USDT_BSC_CONTRACT = "0x55d398326f99059ff775485246999027b3197955"

DB_PATH = "./bot.db"

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

# In-memory drafts (admin only)
drafts: dict[int, list[str]] = {}
draft_id = 0

# Waiting for tx hash from user
awaiting_txhash: set[int] = set()

TX_RE = re.compile(r"^0x[a-fA-F0-9]{64}$")


# =========================
# Time helpers
# =========================
def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()

def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


# =========================
# DB
# =========================
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    tg_id INTEGER PRIMARY KEY,
    username TEXT,
    free_left INTEGER NOT NULL DEFAULT 5,
    sub_until TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_id INTEGER NOT NULL,
    tx_hash TEXT NOT NULL UNIQUE,
    amount_usdt REAL,
    status TEXT NOT NULL, -- PENDING / VERIFIED / REJECTED
    reason TEXT,
    created_at TEXT NOT NULL,
    verified_at TEXT
);

-- Активные/исторические сигналы (один ACTIVE на symbol)
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    direction TEXT NOT NULL,
    entry TEXT NOT NULL,
    tp TEXT NOT NULL,
    sl TEXT NOT NULL,
    note TEXT,
    status TEXT NOT NULL, -- ACTIVE / CLOSED
    sent_at TEXT NOT NULL,
    closed_at TEXT,
    close_reason TEXT, -- TP / SL / MANUAL
    closed_by INTEGER
);

CREATE INDEX IF NOT EXISTS idx_signals_symbol_status ON signals(symbol, status);
"""

async def db_init():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(CREATE_SQL)
        await db.commit()

async def get_or_create_user(tg_id: int, username: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT tg_id, username, free_left, sub_until FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        if row:
            if username and username != row[1]:
                await db.execute("UPDATE users SET username=? WHERE tg_id=?", (username, tg_id))
                await db.commit()
            return {"tg_id": row[0], "username": row[1], "free_left": row[2], "sub_until": row[3]}

        await db.execute(
            "INSERT INTO users (tg_id, username, free_left, sub_until, created_at) VALUES (?, ?, ?, ?, ?)",
            (tg_id, username, 5, None, iso(utcnow()))
        )
        await db.commit()
        return {"tg_id": tg_id, "username": username, "free_left": 5, "sub_until": None

        }

def has_active_sub(sub_until_iso: str | None) -> bool:
    dt = parse_iso(sub_until_iso)
    return bool(dt and dt > utcnow())

async def user_status(tg_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT free_left, sub_until FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        if not row:
            return (5, None)
        return (row[0], row[1])

async def can_receive_signal(tg_id: int) -> bool:
    free_left, sub_until = await user_status(tg_id)
    return has_active_sub(sub_until) or free_left > 0

async def decrement_free_if_needed(tg_id: int):
    free_left, sub_until = await user_status(tg_id)
    if has_active_sub(sub_until):
        return
    if free_left <= 0:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET free_left = free_left - 1 WHERE tg_id=?", (tg_id,))
        await db.commit()

async def extend_subscription_30d(tg_id: int) -> datetime:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT sub_until FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        current = parse_iso(row[0]) if row else None
        base = current if (current and current > utcnow()) else utcnow()
        new_until = base + relativedelta(days=30)
        await db.execute("UPDATE users SET sub_until=? WHERE tg_id=?", (iso(new_until), tg_id))
        await db.commit()
        return new_until

async def add_payment(tg_id: int, tx_hash: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO payments (tg_id, tx_hash, status, created_at) VALUES (?, ?, 'PENDING', ?)",
                (tg_id, tx_hash.lower(), iso(utcnow()))
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

async def set_payment_verified(tx_hash: str, amount_usdt: float):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE payments SET status='VERIFIED', amount_usdt=?, reason=NULL, verified_at=? WHERE tx_hash=?",
            (amount_usdt, iso(utcnow()), tx_hash.lower())
        )
        await db.commit()

async def set_payment_rejected(tx_hash: str, reason: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE payments SET status='REJECTED', reason=?, verified_at=? WHERE tx_hash=?",
            (reason, iso(utcnow()), tx_hash.lower())
        )
        await db.commit()

# ---- Signals gating (one ACTIVE per symbol) ----
async def get_active_signal(symbol: str):
    sym = symbol.upper()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, symbol, timeframe, direction, entry, tp, sl, note, sent_at FROM signals WHERE symbol=? AND status='ACTIVE' ORDER BY id DESC LIMIT 1",
            (sym,)
        )
        return await cur.fetchone()

async def create_active_signal(symbol: str, timeframe: str, direction: str, entry: str, tp: str, sl: str, note: str | None):
    sym = symbol.upper()
    tf = timeframe.upper()
    diru = direction.upper()
    async with aiosqlite.connect(DB_PATH) as db:
        # double-check no active exists
        cur = await db.execute(
            "SELECT id FROM signals WHERE symbol=? AND status='ACTIVE' LIMIT 1",
            (sym,)
        )
        if await cur.fetchone():
            return None

        await db.execute(
            "INSERT INTO signals (symbol, timeframe, direction, entry, tp, sl, note, status, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)",
            (sym, tf, diru, entry, tp, sl, note, iso(utcnow()))
        )
        await db.commit()
        cur2 = await db.execute("SELECT last_insert_rowid()")
        row = await cur2.fetchone()
        return int(row[0]) if row else None

async def close_signal(signal_id: int, reason: str, closed_by: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE signals SET status='CLOSED', closed_at=?, close_reason=?, closed_by=? WHERE id=? AND status='ACTIVE'",
            (iso(utcnow()), reason, closed_by, signal_id)
        )
        await db.commit()

async def get_signal_by_id(signal_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id, symbol, timeframe, direction, entry, tp, sl, note, status, sent_at, closed_at, close_reason FROM signals WHERE id=?",
            (signal_id,)
        )
        return await cur.fetchone()

async def list_users():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT tg_id FROM users")
        return await cur.fetchall()


# =========================
# BscScan verify
# =========================
async def bscscan_find_usdt_transfer_to_our_address(tx_hash: str):
    if not BSC_API_KEY:
        return None, "BSC_API_KEY не задан"

    url = "https://api.bscscan.com/api"
    params = {
        "module": "account",
        "action": "tokentx",
        "address": PAY_ADDRESS,
        "contractaddress": USDT_BSC_CONTRACT,
        "page": 1,
        "offset": 100,
        "sort": "desc",
        "apikey": BSC_API_KEY,
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        async with session.get(url, params=params) as r:
            data = await r.json(content_type=None)

    result = data.get("result")
    if not isinstance(result, list):
        return None, f"BscScan error: {data.get('message') or 'no result'}"

    tx_hash_l = tx_hash.lower()
    for item in result:
        if str(item.get("hash", "")).lower() != tx_hash_l:
            continue

        if str(item.get("to", "")).lower() != PAY_ADDRESS:
            return None, "TX найден, но получатель не совпадает"
        if str(item.get("contractAddress", "")).lower() != USDT_BSC_CONTRACT:
            return None, "TX найден, но это не USDT контракт"

        try:
            value_raw = int(item.get("value", "0"))
            decimals = int(item.get("tokenDecimal", "18"))
            amount = value_raw / (10 ** decimals)
        except Exception:
            return None, "Не удалось прочитать сумму"

        return float(amount), None

    return None, "TX не найден (подожди подтверждений и попробуй ещё раз)"


# =========================
# UI
# =========================
def main_keyboard(is_admin_user: bool):
    kb = ReplyKeyboardBuilder()
    kb.button(text="📊 Статус")
    kb.button(text="📌 Активный сигнал")
    kb.button(text="💳 Оплата")
    kb.button(text="ℹ️ Помощь")
    if is_admin_user:
        kb.button(text="📝 Новый сигнал (админ)")
        kb.button(text="🧾 Платежи (админ)")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)

def pay_keyboard():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Я оплатил (ввести tx)", callback_data="pay:paid")
    kb.adjust(1)
    return kb.as_markup()

def admin_close_keyboard(signal_db_id: int, symbol: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Закрыть TP", callback_data=f"close:{signal_db_id}:TP")
    kb.button(text="❌ Закрыть SL", callback_data=f"close:{signal_db_id}:SL")
    kb.button(text="🟡 Закрыть вручную", callback_data=f"close:{signal_db_id}:MANUAL")
    kb.button(text="📌 Активный сигнал", callback_data=f"active:{symbol.upper()}")
    kb.adjust(1)
    return kb.as_markup()

def format_signal(parts: list[str]) -> str:
    symbol = parts[0].upper()
    tf = parts[1].upper()
    direction = parts[2].upper()
    entry = parts[3]
    tp = parts[4]
    sl = parts[5]
    note = parts[6] if len(parts) >= 7 else ""

    dir_emoji = "🟢 BUY" if direction == "BUY" else "🔴 SELL"
    text = (
        f"📊 <b>{symbol} SIGNAL</b> <i>({tf})</i>\n\n"
        f"<b>Direction:</b> {dir_emoji}\n"
        f"<b>Entry:</b> <code>{entry}</code>\n"
        f"<b>Take Profit:</b> <code>{tp}</code>\n"
        f"<b>Stop Loss:</b> <code>{sl}</code>\n"
    )
    if note.strip():
        text += f"\n<b>Note:</b> {note.strip()}\n"
    text += "\n⚠️ <i>Не является финансовой рекомендацией.</i>"
    return text

def format_active_signal_row(row) -> str:
    # row: id, symbol, timeframe, direction, entry, tp, sl, note, sent_at
    sid, sym, tf, direction, entry, tp, sl, note, sent_at = row
    dir_emoji = "🟢 BUY" if direction == "BUY" else "🔴 SELL"
    text = (
        f"📌 <b>АКТИВНЫЙ СИГНАЛ</b>\n\n"
        f"📊 <b>{sym} SIGNAL</b> <i>({tf})</i>\n\n"
        f"<b>Direction:</b> {dir_emoji}\n"
        f"<b>Entry:</b> <code>{entry}</code>\n"
        f"<b>Take Profit:</b> <code>{tp}</code>\n"
        f"<b>Stop Loss:</b> <code>{sl}</code>\n"
        f"<b>Sent:</b> <i>{sent_at}</i>\n"
    )
    if note:
        text += f"\n<b>Note:</b> {note}\n"
    return text


# =========================
# Commands / Buttons
# =========================
@dp.message(Command("start"))
async def start(m: Message):
    await get_or_create_user(m.from_user.id, m.from_user.username)
    await m.answer(
        "Привет! Я <b>inco FOREX BOT</b>.\n"
        "Сигналы выходят только после подтверждения админом.\n\n"
        "Важно: по каждому инструменту может быть <b>только 1 активный сигнал</b>.\n"
        "Новый сигнал по инструменту выйдет только после закрытия старого (TP/SL/Manual).",
        reply_markup=main_keyboard(is_admin(m.from_user.id)),
    )

@dp.message(Command("status"))
async def status_cmd(m: Message):
    await get_or_create_user(m.from_user.id, m.from_user.username)
    free_left, sub_until = await user_status(m.from_user.id)
    sub_active = has_active_sub(sub_until)
    sub_txt = "✅ активна" if sub_active else "❌ нет"
    until_txt = parse_iso(sub_until).strftime("%Y-%m-%d %H:%M UTC") if sub_active else "—"
    await m.answer(
        "📊 <b>Статус</b>\n"
        f"Подписка: <b>{sub_txt}</b>\n"
        f"До: <b>{until_txt}</b>\n"
        f"Бесплатных сигналов осталось: <b>{free_left}</b>",
        reply_markup=main_keyboard(is_admin(m.from_user.id)),
    )

@dp.message(Command("pay"))
async def pay_cmd(m: Message):
    await get_or_create_user(m.from_user.id, m.from_user.username)
    await m.answer(
        "💳 <b>Оплата доступа</b>\n\n"
        f"Цена: <b>{SUB_PRICE_USDT:.2f} USDT</b> за <b>30 дней</b>\n"
        "Сеть: <b>BEP20 (BSC)</b>\n\n"
        "Адрес для оплаты:\n"
        f"<code>{PAY_ADDRESS}</code>\n\n"
        "После оплаты нажми кнопку ниже и отправь <b>TX hash</b>.\n"
        "Если TX ещё не виден — подожди 1–2 минуты и отправь снова.",
        reply_markup=pay_keyboard(),
    )

@dp.callback_query(F.data == "pay:paid")
async def pay_paid(cb: CallbackQuery):
    awaiting_txhash.add(cb.from_user.id)
    await cb.message.answer(
        "Отправь сюда <b>TX hash</b> оплаты (начинается с <code>0x...</code>, 66 символов)."
    )
    await cb.answer()

@dp.message(Command("active"))
async def active_cmd(m: Message):
    await get_or_create_user(m.from_user.id, m.from_user.username)
    # покажем оба инструмента если есть
    eur = await get_active_signal("EURUSD")
    xau = await get_active_signal("XAUUSD")
    if not eur and not xau:
        return await m.answer("Сейчас нет активных сигналов.", reply_markup=main_keyboard(is_admin(m.from_user.id)))

    parts = []
    if eur:
        parts.append(format_active_signal_row(eur))
    if xau:
        parts.append(format_active_signal_row(xau))
    await m.answer("\n\n".join(parts), reply_markup=main_keyboard(is_admin(m.from_user.id)))

@dp.message(F.text == "📌 Активный сигнал")
async def active_btn(m: Message):
    await active_cmd(m)

@dp.message(F.text == "📊 Статус")
async def status_btn(m: Message):
    await status_cmd(m)

@dp.message(F.text == "💳 Оплата")
async def pay_btn(m: Message):
    await pay_cmd(m)

@dp.message(F.text == "ℹ️ Помощь")
async def help_btn(m: Message):
    await m.answer(
        "ℹ️ <b>Помощь</b>\n\n"
        "• 📊 Статус — подписка/лимиты\n"
        "• 📌 Активный сигнал — текущие активные сигналы\n"
        "• 💳 Оплата — покупка доступа на 30 дней\n\n"
        "<b>Правило:</b> по каждому инструменту (EURUSD/XAUUSD) может быть только 1 активный сигнал.\n"
        "Новый выходит только после закрытия старого (TP/SL/Manual) админом.",
        reply_markup=main_keyboard(is_admin(m.from_user.id)),
    )

# Admin helper buttons
@dp.message(F.text == "📝 Новый сигнал (админ)")
async def admin_newsignal_btn(m: Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer(
        "📝 <b>Создание сигнала (админ)</b>\n\n"
        "Отправь одной строкой:\n"
        "<code>SYMBOL;TF;DIR;ENTRY;TP;SL;NOTE(optional)</code>\n\n"
        "Пример:\n"
        "<code>EURUSD;5M;SELL;1.08320;1.08100;1.08450;liquidity sweep</code>\n\n"
        "⚠️ Если по SYMBOL уже есть активный сигнал — новый не будет отправлен, пока ты не закроешь старый.",
    )

@dp.message(F.text == "🧾 Платежи (админ)")
async def admin_payments_btn(m: Message):
    if not is_admin(m.from_user.id):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT tg_id, tx_hash, amount_usdt, status, created_at FROM payments ORDER BY id DESC LIMIT 10"
        )
        rows = await cur.fetchall()
    if not rows:
        return await m.answer("Платежей пока нет.", reply_markup=main_keyboard(True))

    lines = ["🧾 <b>Последние 10 оплат</b>:"]
    for tg_id, txh, amt, st, created in rows:
        amt_txt = f"{amt:.2f}" if amt is not None else "—"
        lines.append(f"• <code>{tg_id}</code> | <code>{txh[:10]}…</code> | {amt_txt} | <b>{st}</b> | {created}")
    await m.answer("\n".join(lines), reply_markup=main_keyboard(True))


# =========================
# TX hash input + Draft parsing
# =========================
@dp.message(F.text)
async def handle_text(m: Message):
    await get_or_create_user(m.from_user.id, m.from_user.username)

    # 1) Waiting TX hash
    if m.from_user.id in awaiting_txhash:
        tx = m.text.strip()
        if not TX_RE.match(tx):
            await m.answer("❌ Это не похоже на TX hash. Должно быть <code>0x</code> + 64 символа.")
            return

        awaiting_txhash.discard(m.from_user.id)

        ok = await add_payment(m.from_user.id, tx)
        if not ok:
            await m.answer("⏳ Этот TX уже есть в базе (или уже проверен). Если доступа нет — напиши админу @inco_44.")
            return

        await m.answer("⏳ Принял TX. Проверяю оплату в сети…")
        asyncio.create_task(verify_and_activate(m.from_user.id, tx))
        return

    # 2) Admin draft parsing by semicolons
    if is_admin(m.from_user.id) and ";" in m.text:
        await make_draft(m)
        return


async def verify_and_activate(tg_id: int, tx_hash: str):
    amount, err = await bscscan_find_usdt_transfer_to_our_address(tx_hash)
    if amount is None:
        await set_payment_rejected(tx_hash, err or "Не удалось проверить")
        await bot.send_message(
            tg_id,
            "❌ <b>Оплата не подтверждена</b>\n"
            f"Причина: <i>{err or 'неизвестно'}</i>\n\n"
            "Подожди 1–2 минуты и отправь TX снова через <b>💳 Оплата</b>.",
            reply_markup=pay_keyboard(),
        )
        if ADMIN_ID:
            await bot.send_message(ADMIN_ID, f"❌ Оплата отклонена\nTG: <code>{tg_id}</code>\nTX: <code>{tx_hash}</code>\nПричина: {err}")
        return

    if amount + 1e-9 < SUB_PRICE_USDT:
        reason = f"Сумма меньше цены: {amount:.2f} < {SUB_PRICE_USDT:.2f}"
        await set_payment_rejected(tx_hash, reason)
        await bot.send_message(
            tg_id,
            "❌ <b>Оплата найдена, но сумма недостаточная</b>\n"
            f"Оплачено: <b>{amount:.2f} USDT</b>\n"
            f"Нужно: <b>{SUB_PRICE_USDT:.2f} USDT</b>\n\n"
            "Напиши админу: <b>@inco_44</b>"
        )
        if ADMIN_ID:
            await bot.send_message(ADMIN_ID, f"⚠️ Недостаточная сумма\nTG: <code>{tg_id}</code>\nTX: <code>{tx_hash}</code>\nAmount: {amount:.2f}")
        return

    await set_payment_verified(tx_hash, amount)
    new_until = await extend_subscription_30d(tg_id)
    await bot.send_message(
        tg_id,
        "✅ <b>Оплата подтверждена!</b>\n"
        f"Подписка активна до: <b>{new_until.strftime('%Y-%m-%d %H:%M UTC')}</b>\n\n"
        "Теперь ты будешь получать сигналы без лимита.",
        reply_markup=main_keyboard(is_admin(tg_id)),
    )
    if ADMIN_ID:
        await bot.send_message(
            ADMIN_ID,
            "✅ Оплата подтверждена\n"
            f"TG: <code>{tg_id}</code>\n"
            f"TX: <code>{tx_hash}</code>\n"
            f"Amount: <b>{amount:.2f} USDT</b>\n"
            f"До: <b>{new_until.strftime('%Y-%m-%d %H:%M UTC')}</b>"
        )


# =========================
# Admin draft + approve with gating
# =========================
async def make_draft(m: Message):
    global draft_id
    parts = [p.strip() for p in m.text.split(";")]
    if len(parts) < 6:
        await m.answer("❌ Ошибка: нужно минимум 6 полей: SYMBOL;TF;DIR;ENTRY;TP;SL")
        return

    symbol = parts[0].upper()
    direction = parts[2].upper()
    if direction not in ("BUY", "SELL"):
        await m.answer("❌ Ошибка: DIR должен быть BUY или SELL")
        return

    # Inform if active exists (but still allow draft creation)
    active = await get_active_signal(symbol)
    warn = ""
    if active:
        warn = (
            f"⚠️ По <b>{symbol}</b> уже есть активный сигнал.\n"
            f"Новый по <b>{symbol}</b> <b>нельзя отправить</b>, пока не закроешь старый.\n\n"
        )

    draft_id += 1
    drafts[draft_id] = parts

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить и отправить", callback_data=f"approve:{draft_id}")
    kb.button(text="🗑 Отменить", callback_data=f"cancel:{draft_id}")
    kb.adjust(1)

    await m.answer(warn + "🧾 <b>Черновик сигнала</b>\n\n" + format_signal(parts), reply_markup=kb.as_markup())

@dp.callback_query(F.data.startswith("cancel:"))
async def cancel(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    did = int(cb.data.split(":")[1])
    drafts.pop(did, None)
    await cb.message.edit_text("🗑 Отменено.")
    await cb.answer()

@dp.callback_query(F.data.startswith("approve:"))
async def approve(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    did = int(cb.data.split(":")[1])
    parts = drafts.get(did)
    if not parts:
        await cb.answer("Черновик не найден", show_alert=True)
        return

    symbol = parts[0].upper()

    # HARD GATE: do not allow send if ACTIVE exists for that symbol
    active = await get_active_signal(symbol)
    if active:
        sid = active[0]
        await cb.message.edit_text(
            f"⛔️ Нельзя отправить новый сигнал по <b>{symbol}</b>.\n"
            f"Сначала закрой активный сигнал (ID: <code>{sid}</code>) по TP/SL/Manual.\n\n"
            "Нажми <b>📌 Активный сигнал</b> или закрой через кнопки у админ-сообщения активного сигнала."
        )
        await cb.answer()
        return

    # Create ACTIVE in DB
    sig_db_id = await create_active_signal(
        symbol=parts[0], timeframe=parts[1], direction=parts[2],
        entry=parts[3], tp=parts[4], sl=parts[5],
        note=(parts[6] if len(parts) >= 7 else None),
    )
    if not sig_db_id:
        await cb.message.edit_text(f"⛔️ Уже есть активный сигнал по <b>{symbol}</b>. Закрой его и повтори.")
        await cb.answer()
        return

    # Broadcast to users with access
    text = format_signal(parts)
    users_rows = await list_users()

    sent = 0
    blocked = 0

    for (uid,) in users_rows:
        if not await can_receive_signal(uid):
            blocked += 1
            continue
        try:
            await bot.send_message(uid, text)
            await decrement_free_if_needed(uid)
            sent += 1
        except Exception:
            pass

    # Admin gets management message with close buttons
    try:
        await bot.send_message(
            ADMIN_ID,
            "📌 <b>Сигнал активирован</b> (управление)\n\n" + text,
            reply_markup=admin_close_keyboard(sig_db_id, symbol),
        )
    except Exception:
        pass

    drafts.pop(did, None)
    await cb.message.edit_text(f"✅ Отправлено: {sent}\n⛔️ Без доступа: {blocked}\n\n📌 Активный ID: <code>{sig_db_id}</code>")
    await cb.answer()

@dp.callback_query(F.data.startswith("active:"))
async def active_inline(cb: CallbackQuery):
    sym = cb.data.split(":", 1)[1].upper()
    row = await get_active_signal(sym)
    if not row:
        await cb.answer("Нет активного сигнала", show_alert=True)
        return
    await cb.message.answer(format_active_signal_row(row))
    await cb.answer()

@dp.callback_query(F.data.startswith("close:"))
async def close_inline(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    _, sig_id_s, reason = cb.data.split(":")
    sig_id = int(sig_id_s)

    row = await get_signal_by_id(sig_id)
    if not row:
        await cb.answer("Сигнал не найден", show_alert=True)
        return

    status = row[8]
    if status != "ACTIVE":
        await cb.answer("Сигнал уже закрыт", show_alert=True)
        return

    await close_signal(sig_id, reason, cb.from_user.id)

    # Notify users that signal is closed
    symbol = row[1]
    msg = (
        f"✅ <b>Сигнал закрыт</b>\n"
        f"📊 <b>{symbol}</b>\n"
        f"Причина: <b>{reason}</b>\n\n"
        "Теперь можно публиковать следующий сигнал по этому инструменту."
    )

    users_rows = await list_users()
    for (uid,) in users_rows:
        try:
            await bot.send_message(uid, msg)
        except Exception:
            pass

    await cb.message.edit_text("✅ Закрыто. " + msg)
    await cb.answer()


# =========================
# MAIN
# =========================
async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в переменных окружения")
    if ADMIN_ID == 0:
        raise RuntimeError("ADMIN_TG_ID не задан или равен 0")
    await db_init()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
