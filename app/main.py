import os
import sqlite3
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder


# ======================
# ENV
# ======================
TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

if not TOKEN:
    raise RuntimeError("BOT_TOKEN missing (Railway -> Variables)")
if ADMIN_ID == 0:
    raise RuntimeError("ADMIN_ID missing (Railway -> Variables)")

bot = Bot(token=TOKEN)
dp = Dispatcher()

TZ_UTC = timezone.utc


# ======================
# DATABASE (auto-migration)
# ======================
conn = sqlite3.connect("users.db", check_same_thread=False)
cursor = conn.cursor()

# Create base table if not exists
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    status TEXT
)
""")
conn.commit()

# Auto-migrate: add access_until if missing
cursor.execute("PRAGMA table_info(users)")
cols = {row[1] for row in cursor.fetchall()}  # row[1] = column name
if "access_until" not in cols:
    cursor.execute("ALTER TABLE users ADD COLUMN access_until TEXT")
    conn.commit()


def now_utc() -> datetime:
    return datetime.now(TZ_UTC)


def get_user(user_id: int):
    cursor.execute("SELECT status, access_until FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    return row if row else None


def set_user(user_id: int, status: str, access_until: str | None = None):
    cursor.execute(
        "INSERT OR REPLACE INTO users (user_id, status, access_until) VALUES (?, ?, ?)",
        (user_id, status, access_until),
    )
    conn.commit()


def expire_if_needed(user_id: int):
    row = get_user(user_id)
    if not row:
        return
    status, access_until = row
    if status != "approved" or not access_until:
        return
    try:
        until_dt = datetime.fromisoformat(access_until)
    except Exception:
        return
    if until_dt <= now_utc():
        set_user(user_id, "expired", access_until)


def is_active(user_id: int) -> bool:
    row = get_user(user_id)
    if not row:
        return False
    status, access_until = row
    if status != "approved" or not access_until:
        return False
    try:
        until_dt = datetime.fromisoformat(access_until)
    except Exception:
        return False
    return until_dt > now_utc()


# ======================
# KEYBOARDS
# ======================
def menu_kb():
    kb = ReplyKeyboardBuilder()
    kb.button(text="📈 Новый сигнал")
    kb.button(text="❓ Помощь")
    kb.adjust(1)
    return kb.as_markup(resize_keyboard=True)


def approval_kb(user_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить на 30 дней", callback_data=f"approve:{user_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{user_id}"),
        ]
    ])


# ======================
# SIGNAL (заглушка — чтобы бот точно работал)
# ======================
def generate_signal_text() -> str:
    # Позже заменим на твою реальную логику сигналов (EUR/USD + XAU/USD)
    return (
        "📊 SIGNAL (TEST)\n\n"
        "Pair: EUR/USD\n"
        "Direction: BUY 🟢\n"
        "Entry: 1.08000\n"
        "TP: 1.08200\n"
        "SL: 1.07900\n\n"
        "✅ Доступ работает. Дальше подключим твою логику сигналов."
    )


async def require_access(message: Message) -> bool:
    uid = message.from_user.id
    expire_if_needed(uid)
    if is_active(uid):
        return True
    await message.answer("⛔ Нет доступа или доступ истёк.\nНажми /start и дождись одобрения.")
    return False


# ======================
# START (request access)
# ======================
@dp.message(CommandStart())
async def start(message: Message):
    user_id = message.from_user.id
    expire_if_needed(user_id)

    row = get_user(user_id)
    if row:
        status, access_until = row

        if status == "approved" and access_until:
            try:
                until_dt = datetime.fromisoformat(access_until)
            except Exception:
                until_dt = None

            if until_dt and until_dt > now_utc():
                await message.answer(f"✅ Доступ активен до {access_until[:10]}", reply_markup=menu_kb())
                return
            else:
                set_user(user_id, "expired", access_until)

        if status == "pending":
            await message.answer("⏳ Ожидайте одобрения администратора.", reply_markup=menu_kb())
            return

    set_user(user_id, "pending", None)

    username = f"@{message.from_user.username}" if message.from_user.username else "—"
    await bot.send_message(
        ADMIN_ID,
        "📩 Новая заявка на доступ\n\n"
        f"👤 Имя: {message.from_user.full_name}\n"
        f"🔗 Username: {username}\n"
        f"🆔 ID: {user_id}\n\n"
        "Выдать доступ на 30 дней?",
        reply_markup=approval_kb(user_id),
    )
    await message.answer("⏳ Заявка отправлена администратору. Ожидайте одобрения.", reply_markup=menu_kb())


# ======================
# APPROVE / REJECT
# ======================
@dp.callback_query(F.data.startswith("approve:"))
async def approve(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет прав", show_alert=True)
        return

    user_id = int(callback.data.split(":")[1])
    until = (now_utc() + timedelta(days=30)).isoformat()
    set_user(user_id, "approved", until)

    await bot.send_message(user_id, f"✅ Доступ одобрен до {until[:10]}\nНажми /start", reply_markup=menu_kb())
    await callback.answer("✅ Одобрено")

    try:
        await callback.message.edit_text(f"✅ Одобрено для {user_id} до {until[:10]}")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("reject:"))
async def reject(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Нет прав", show_alert=True)
        return

    user_id = int(callback.data.split(":")[1])
    set_user(user_id, "rejected", None)

    await bot.send_message(user_id, "❌ Ваша заявка отклонена.")
    await callback.answer("❌ Отклонено")

    try:
        await callback.message.edit_text(f"❌ Отклонено для {user_id}")
    except Exception:
        pass


# ======================
# MENU
# ======================
@dp.message(F.text == "❓ Помощь")
async def help_cmd(message: Message):
    await message.answer(
        "ℹ️ Помощь\n\n"
        "• Нажми /start чтобы запросить доступ\n"
        "• Доступ выдаётся админом на 30 дней\n"
        "• «Новый сигнал» работает только с активным доступом\n",
        reply_markup=menu_kb(),
    )


@dp.message(F.text == "📈 Новый сигнал")
async def new_signal(message: Message):
    if not await require_access(message):
        return
    await message.answer(generate_signal_text(), reply_markup=menu_kb())


# ======================
# RUN
# ======================
async def main():
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
