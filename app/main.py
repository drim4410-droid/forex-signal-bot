import asyncio
import os
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_TG_ID", "0"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Пользователи: tg_id -> сколько бесплатных сигналов осталось
users: dict[int, int] = {}

# Черновики сигналов (только в памяти сервера)
drafts: dict[int, list[str]] = {}
draft_id = 0


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def format_signal(parts: list[str]) -> str:
    # parts: SYMBOL;TF;DIR;ENTRY;TP;SL;NOTE?
    symbol = parts[0]
    tf = parts[1]
    direction = parts[2].upper()
    entry = parts[3]
    tp = parts[4]
    sl = parts[5]
    note = parts[6] if len(parts) >= 7 else ""

    text = (
        f"📊 {symbol} SIGNAL ({tf})\n\n"
        f"Direction: {direction}\n"
        f"Entry: {entry}\n"
        f"Take Profit: {tp}\n"
        f"Stop Loss: {sl}\n"
    )
    if note.strip():
        text += f"\nNote: {note.strip()}\n"
    return text


@dp.message(Command("start"))
async def start(m: Message):
    if m.from_user.id not in users:
        users[m.from_user.id] = 5
    await m.answer(
        "Привет! Я бот от incognito.\n\n"
        "Команды:\n"
        "/status — сколько бесплатных сигналов осталось\n"
        "(админ) /newsignal — как создать сигнал\n"
    )


@dp.message(Command("status"))
async def status(m: Message):
    left = users.get(m.from_user.id, 5)
    await m.answer(f"Бесплатных сигналов осталось: {left}")


@dp.message(Command("newsignal"))
async def newsignal(m: Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer(
        "Создание сигнала (только админ):\n"
        "Отправь одной строкой:\n"
        "SYMBOL;TF;DIR;ENTRY;TP;SL;NOTE(optional)\n\n"
        "Пример:\n"
        "EURUSD;5M;SELL;1.08320;1.08100;1.08450;test"
    )


@dp.message(F.text.contains(";"))
async def make_draft(m: Message):
    global draft_id
    if not is_admin(m.from_user.id):
        return

    parts = [p.strip() for p in m.text.split(";")]
    if len(parts) < 6:
        await m.answer("Ошибка: нужно минимум 6 полей: SYMBOL;TF;DIR;ENTRY;TP;SL")
        return

    draft_id += 1
    drafts[draft_id] = parts

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить и отправить", callback_data=f"approve:{draft_id}")
    kb.button(text="🗑 Отменить", callback_data=f"cancel:{draft_id}")
    kb.adjust(1)

    await m.answer("Черновик:\n\n" + format_signal(parts), reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    did = int(cb.data.split(":")[1])
    drafts.pop(did, None)
    await cb.message.edit_text("Отменено.")
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

    text = format_signal(parts)

    sent = 0
    blocked = 0

    # рассылаем всем, у кого осталось > 0
    for uid in list(users.keys()):
        if users.get(uid, 0) <= 0:
            blocked += 1
            continue
        try:
            await bot.send_message(uid, text)
            users[uid] -= 1
            sent += 1
        except Exception:
            pass

    drafts.pop(did, None)
    await cb.message.edit_text(f"✅ Отправлено: {sent}\n⛔️ Лимит исчерпан: {blocked}")
    await cb.answer()


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в переменных окружения")
    if ADMIN_ID == 0:
        raise RuntimeError("ADMIN_TG_ID не задан или равен 0")
    await dp.start_polling(bot)
    

if __name__ == "__main__":
    asyncio.run(main())

