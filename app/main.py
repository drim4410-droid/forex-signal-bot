import asyncio
import os
import sqlite3
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import CommandStart
from aiogram.utils.keyboard import ReplyKeyboardBuilder

TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

bot = Bot(token=TOKEN)
dp = Dispatcher()

# ===== DATABASE =====
conn = sqlite3.connect("users.db")
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    status TEXT
)
""")
conn.commit()

def get_status(user_id):
    cursor.execute("SELECT status FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    return row[0] if row else None

def set_status(user_id, status):
    cursor.execute("INSERT OR REPLACE INTO users VALUES (?,?)", (user_id, status))
    conn.commit()

# ===== START =====
@dp.message(CommandStart())
async def start(message: Message):
    user_id = message.from_user.id
    status = get_status(user_id)

    if status == "approved":
        await show_menu(message)
        return

    if status == "pending":
        await message.answer("⏳ Ожидайте одобрения администратора.")
        return

    set_status(user_id, "pending")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_{user_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user_id}")
        ]
    ])

    await bot.send_message(
        ADMIN_ID,
        f"Новая заявка:\n\nИмя: {message.from_user.full_name}\nID: {user_id}",
        reply_markup=kb
    )

    await message.answer("⏳ Заявка отправлена администратору.")

# ===== APPROVAL =====
@dp.callback_query(F.data.startswith("approve_"))
async def approve(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    user_id = int(callback.data.split("_")[1])
    set_status(user_id, "approved")
    await bot.send_message(user_id, "✅ Доступ одобрен! Нажмите /start")
    await callback.answer("Одобрено")

@dp.callback_query(F.data.startswith("reject_"))
async def reject(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    user_id = int(callback.data.split("_")[1])
    set_status(user_id, "rejected")
    await bot.send_message(user_id, "❌ Ваша заявка отклонена.")
    await callback.answer("Отклонено")

# ===== MENU =====
async def show_menu(message: Message):
    kb = ReplyKeyboardBuilder()
    kb.button(text="📊 Новый сигнал")
    kb.button(text="❓ Помощь")
    kb.adjust(1)

    await message.answer("Добро пожаловать!", reply_markup=kb.as_markup(resize_keyboard=True))

@dp.message(F.text == "📊 Новый сигнал")
async def signal(message: Message):
    if get_status(message.from_user.id) != "approved":
        await message.answer("⛔ Нет доступа.")
        return

    # ТВОЙ СИГНАЛ (сюда можешь вставить свой анализ)
    await message.answer(
        "📊 XAU/USD SIGNAL\n\n"
        "Direction: BUY 🟢\n"
        "Entry: 2050.00\n"
        "TP: 2060.00\n"
        "SL: 2045.00"
    )

@dp.message(F.text == "❓ Помощь")
async def help_msg(message: Message):
    await message.answer("Бот даёт сигналы по EUR/USD и XAU/USD.\nДоступ только после одобрения.")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
