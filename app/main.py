import asyncio
import os
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_TG_ID", "0"))

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# tg_id -> сколько бесплатных сигналов осталось
users: dict[int, int] = {}

# draft_id -> parts
drafts: dict[int, list[str]] = {}
draft_id = 0


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def main_keyboard(is_admin_user: bool):
    kb = ReplyKeyboardBuilder()
    kb.button(text="📊 Статус")
    kb.button(text="ℹ️ Помощь")
    if is_admin_user:
        kb.button(text="📝 Новый сигнал (админ)")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True)


def format_signal(parts: list[str]) -> str:
    # SYMBOL;TF;DIR;ENTRY;TP;SL;NOTE?
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


@dp.message(Command("start"))
async def start(m: Message):
    if m.from_user.id not in users:
        users[m.from_user.id] = 5

    await m.answer(
        "Привет! Я <b>inco FOREX BOT</b>.\n"
        "Я отправляю сигналы только после подтверждения админом.\n\n"
        "Нажимай кнопки ниже 👇",
        reply_markup=main_keyboard(is_admin(m.from_user.id)),
    )


@dp.message(Command("status"))
async def status_cmd(m: Message):
    left = users.get(m.from_user.id, 5)
    await m.answer(
        f"📊 <b>Статус</b>\n"
        f"Бесплатных сигналов осталось: <b>{left}</b>",
        reply_markup=main_keyboard(is_admin(m.from_user.id)),
    )


@dp.message(Command("newsignal"))
async def newsignal_cmd(m: Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer(
        "📝 <b>Создание сигнала (админ)</b>\n\n"
        "Отправь одной строкой:\n"
        "<code>SYMBOL;TF;DIR;ENTRY;TP;SL;NOTE(optional)</code>\n\n"
        "Пример:\n"
        "<code>EURUSD;5M;SELL;1.08320;1.08100;1.08450;liquidity sweep</code>"
    )


# ===== КНОПКИ (обычные сообщения) =====

@dp.message(F.text == "📊 Статус")
async def status_btn(m: Message):
    await status_cmd(m)


@dp.message(F.text == "ℹ️ Помощь")
async def help_btn(m: Message):
    msg = (
        "ℹ️ <b>Помощь</b>\n\n"
        "Команды:\n"
        "• /start — запустить бота\n"
        "• /status — остаток бесплатных сигналов\n\n"
        "Для админа:\n"
        "• /newsignal — инструкция создания сигнала\n"
        "• или кнопка <b>Новый сигнал (админ)</b>\n\n"
        "Формат сигнала:\n"
        "<code>SYMBOL;TF;DIR;ENTRY;TP;SL;NOTE(optional)</code>\n"
        "DIR = BUY или SELL"
    )
    await m.answer(msg, reply_markup=main_keyboard(is_admin(m.from_user.id)))


@dp.message(F.text == "📝 Новый сигнал (админ)")
async def newsignal_btn(m: Message):
    await newsignal_cmd(m)


# ===== СОЗДАНИЕ ЧЕРНОВИКА (админ) =====

@dp.message(F.text.contains(";"))
async def make_draft(m: Message):
    global draft_id
    if not is_admin(m.from_user.id):
        return

    parts = [p.strip() for p in m.text.split(";")]
    if len(parts) < 6:
        await m.answer("❌ Ошибка: нужно минимум 6 полей: SYMBOL;TF;DIR;ENTRY;TP;SL")
        return

    direction = parts[2].upper()
    if direction not in ("BUY", "SELL"):
        await m.answer("❌ Ошибка: DIR должен быть BUY или SELL")
        return

    draft_id += 1
    drafts[draft_id] = parts

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Одобрить и отправить", callback_data=f"approve:{draft_id}")
    kb.button(text="🗑 Отменить", callback_data=f"cancel:{draft_id}")
    kb.adjust(1)

    await m.answer("🧾 <b>Черновик сигнала</b>\n\n" + format_signal(parts), reply_markup=kb.as_markup())


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

    text = format_signal(parts)

    sent = 0
    blocked = 0

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
