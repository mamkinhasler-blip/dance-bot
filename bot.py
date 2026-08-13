"""
bot.py — Telegram-адаптер. Тонкая обёртка вокруг core.py.

Всё управление — кнопками. Педагог не набирает команды: внизу экрана
постоянное меню (reply-кнопки), а выбор конкретного ученика — через
инлайн-кнопки в самом сообщении.

Педагог (админ) определяется по ADMIN_ID из переменных окружения. Можно
указать несколько через запятую — например, педагог и разработчик оба
получат доступ к панели, каждый под своим Telegram-аккаунтом.

Запуск:
    export BOT_TOKEN="токен_от_@BotFather"
    export ADMIN_ID="111111,222222"      # свой id узнать: напишите боту @userinfobot
    python bot.py
"""

import asyncio
import html
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

import core

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x.strip()) for x in os.environ["ADMIN_ID"].split(",") if x.strip()}

dp = Dispatcher()
bot = Bot(BOT_TOKEN)

# username бота кешируется один раз при старте — чтобы не дёргать Telegram
# по сети на каждую генерацию ссылки.
BOT_USERNAME: str | None = None


def student_link(claim_code: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start={claim_code}"


def esc(text: str) -> str:
    """Экранируем имя для вставки в сообщения с parse_mode=HTML."""
    return html.escape(text or "")


# ------------------------- подписи кнопок главного меню -------------------------

BTN_NEW = "➕ Новый ученик"
BTN_LESSON = "✅ Занятие"
BTN_LIST = "📋 Список"
BTN_PAY = "💰 Оплата"
BTN_LINK = "🔗 Ссылка"
BTN_DELETE = "🗑 Удалить"
BTN_CANCEL = "◀️ Отмена"
BTN_MY_BALANCE = "💳 Мой баланс"

ADMIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_LESSON)],
        [KeyboardButton(text=BTN_LIST), KeyboardButton(text=BTN_PAY)],
        [KeyboardButton(text=BTN_LINK), KeyboardButton(text=BTN_DELETE)],
    ],
    resize_keyboard=True,
)

CANCEL_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
    resize_keyboard=True,
)

STUDENT_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_MY_BALANCE)]],
    resize_keyboard=True,
)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ------------------------- состояния диалогов (FSM) -------------------------

class AddStudent(StatesGroup):
    name = State()
    lessons = State()


class TopUp(StatesGroup):
    amount = State()


# ------------------------- /start -------------------------

@dp.message(CommandStart(deep_link=True))
async def start_with_code(message: Message, command: CommandObject):
    """Ученик открыл персональную ссылку вида t.me/bot?start=CODE."""
    # Педагог не может привязаться как ученик — иначе засорит данные и
    # случайно окажется "на панели ученика". Ссылки для учеников, не для него.
    if is_admin(message.from_user.id):
        await message.answer(
            "Ты педагог — эта ссылка предназначена для учеников, "
            "тебе привязываться не нужно.",
            reply_markup=ADMIN_MENU,
        )
        return
    code = command.args
    student = core.claim_by_code(code, message.from_user.id)
    if student:
        await message.answer(
            f"Привет, {esc(student['name'])}! Ты привязан к боту.\n"
            f"Осталось занятий: <b>{student['balance']}</b>",
            parse_mode="HTML",
            reply_markup=STUDENT_MENU,
        )
    else:
        await message.answer("Ссылка недействительна. Попроси у педагога новую.")


@dp.message(CommandStart())
async def start(message: Message):
    if is_admin(message.from_user.id):
        await message.answer(
            "Панель педагога. Пользуйся кнопками внизу 👇",
            reply_markup=ADMIN_MENU,
        )
    else:
        student = core.get_student_by_tg(message.from_user.id)
        if student:
            await message.answer(
                f"Осталось занятий: <b>{student['balance']}</b>",
                parse_mode="HTML",
                reply_markup=STUDENT_MENU,
            )
        else:
            await message.answer(
                "Привет! Чтобы видеть свой баланс, открой персональную ссылку от педагога."
            )


# ------------------------- Мой баланс (ученик) -------------------------

@dp.message(F.text == BTN_MY_BALANCE)
async def balance(message: Message):
    student = core.get_student_by_tg(message.from_user.id)
    if student:
        await message.answer(
            f"Осталось занятий: <b>{student['balance']}</b>",
            parse_mode="HTML",
            reply_markup=STUDENT_MENU,
        )
    else:
        await message.answer("Ты пока не привязан. Попроси у педагога ссылку.")


# ------------------------- отмена (выход из любого диалога) -------------------------

@dp.message(F.text == BTN_CANCEL)
async def cancel_any(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("Отменено.", reply_markup=ADMIN_MENU)


# ------------------------- Новый ученик -------------------------

@dp.message(F.text == BTN_NEW)
async def new_student(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AddStudent.name)
    await message.answer("Имя ученика?", reply_markup=CANCEL_MENU)


@dp.message(AddStudent.name)
async def new_student_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Пришли имя ученика текстом.")
        return
    await state.update_data(name=name)
    await state.set_state(AddStudent.lessons)
    await message.answer("Сколько занятий оплачено? (пришли число)")


@dp.message(AddStudent.lessons)
async def new_student_lessons(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("Нужно просто число. Сколько занятий оплачено?")
        return
    data = await state.get_data()
    lessons = int(message.text)
    sid = core.add_student(data["name"], lessons)
    await state.clear()

    student = core.get_student(sid)
    link = student_link(student["claim_code"])
    await message.answer(
        f"Добавлен: <b>{esc(data['name'])}</b>, баланс {lessons}.\n\n"
        f"Ссылка для ученика (по ней он сможет смотреть свой остаток):\n{link}",
        parse_mode="HTML",
        reply_markup=ADMIN_MENU,
    )


# ------------------------- Список -------------------------

@dp.message(F.text == BTN_LIST)
async def student_list(message: Message):
    if not is_admin(message.from_user.id):
        return
    students = core.list_students()
    if not students:
        await message.answer(f'Учеников пока нет. Добавь через "{BTN_NEW}".')
        return
    lines = []
    for s in students:
        mark = " ⚠️" if s["balance"] <= 1 else ""
        lines.append(f"{s['name']} — {s['balance']}{mark}")
    await message.answer("\n".join(lines))


# ------------------------- Занятие (отметить посещаемость) -------------------------

def attendance_keyboard(selected: set[int]) -> InlineKeyboardMarkup:
    rows = []
    for s in core.list_students():
        box = "☑️" if s["id"] in selected else "☐"
        rows.append([
            InlineKeyboardButton(
                text=f"{box} {s['name']} ({s['balance']})",
                callback_data=f"att:{s['id']}",
            )
        ])
    rows.append([InlineKeyboardButton(text="✅ Готово", callback_data="att:done")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="att:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# храним выбор в памяти по chat_id (для одного педагога этого достаточно)
_attendance: dict[int, set[int]] = {}


@dp.message(F.text == BTN_LESSON)
async def lesson(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not core.list_students():
        await message.answer(f'Сначала добавь учеников через "{BTN_NEW}".')
        return
    _attendance[message.chat.id] = set()
    await message.answer(
        "Отметь, кто был на занятии, потом нажми «Готово»:",
        reply_markup=attendance_keyboard(set()),
    )


@dp.callback_query(F.data.startswith("att:"))
async def attendance_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    action = cb.data.split(":", 1)[1]
    selected = _attendance.setdefault(cb.message.chat.id, set())

    if action == "cancel":
        _attendance.pop(cb.message.chat.id, None)
        await cb.message.edit_text("Отменено.")
        await cb.answer()
        return

    if action == "done":
        if not selected:
            await cb.answer("Никто не отмечен", show_alert=True)
            return
        result = core.mark_lesson(list(selected))
        _attendance.pop(cb.message.chat.id, None)
        lines = []
        ran_out = []
        for r in result:
            lines.append(f"{r['name']} → осталось {r['balance']}")
            if r["out"]:
                ran_out.append(r["name"])
        text = "Занятие отмечено:\n\n" + "\n".join(lines)
        if ran_out:
            text += "\n\n⚠️ Занятия закончились, нужна оплата:\n" + "\n".join(ran_out)
        await cb.message.edit_text(text)
        await cb.answer("Готово")
        return

    # переключаем конкретного ученика
    sid = int(action)
    if sid in selected:
        selected.remove(sid)
    else:
        selected.add(sid)
    await cb.message.edit_reply_markup(reply_markup=attendance_keyboard(selected))
    await cb.answer()


# ------------------------- Оплата (пополнить баланс) -------------------------

def students_keyboard(prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{s['name']} ({s['balance']})",
                              callback_data=f"{prefix}:{s['id']}")]
        for s in core.list_students()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(F.text == BTN_PAY)
async def payment(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not core.list_students():
        await message.answer("Учеников пока нет.")
        return
    await message.answer("Кому пополнить?", reply_markup=students_keyboard("pay"))


@dp.callback_query(F.data.startswith("pay:"))
async def payment_pick(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    sid = int(cb.data.split(":", 1)[1])
    await state.set_state(TopUp.amount)
    await state.update_data(sid=sid)
    s = core.get_student(sid)
    await cb.message.edit_text(f"Сколько занятий добавить для {s['name']}? (пришли число)")
    await cb.answer()


@dp.message(TopUp.amount)
async def payment_amount(message: Message, state: FSMContext):
    if not message.text or not message.text.isdigit():
        await message.answer("Нужно просто число.")
        return
    data = await state.get_data()
    new_bal = core.top_up(data["sid"], int(message.text))
    s = core.get_student(data["sid"])
    await state.clear()
    await message.answer(f"{s['name']}: баланс теперь {new_bal}.", reply_markup=ADMIN_MENU)


# ------------------------- Ссылка (дать ученику ссылку) -------------------------

@dp.message(F.text == BTN_LINK)
async def get_link(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not core.list_students():
        await message.answer("Учеников пока нет.")
        return
    await message.answer("Кому дать ссылку?", reply_markup=students_keyboard("link"))


@dp.callback_query(F.data.startswith("link:"))
async def send_link(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    sid = int(cb.data.split(":", 1)[1])
    s = core.get_student(sid)
    link = student_link(s["claim_code"])
    await cb.message.edit_text(
        f"Ссылка для {s['name']} — отправь её ученику:\n{link}"
    )
    await cb.answer()


# ------------------------- Удаление ученика (с подтверждением) -------------------------

@dp.message(F.text == BTN_DELETE)
async def delete_menu(message: Message):
    if not is_admin(message.from_user.id):
        return
    if not core.list_students():
        await message.answer("Учеников пока нет.")
        return
    await message.answer("Кого удалить?", reply_markup=students_keyboard("del"))


@dp.callback_query(F.data.startswith("del:"))
async def delete_pick(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    sid = int(cb.data.split(":", 1)[1])
    s = core.get_student(sid)
    if not s:
        await cb.message.edit_text("Ученик не найден.")
        await cb.answer()
        return
    # шаг подтверждения — чтобы не снести случайно
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"delok:{sid}"),
        InlineKeyboardButton(text="Отмена", callback_data="delno"),
    ]])
    await cb.message.edit_text(
        f"Удалить ученика «{s['name']}» (баланс {s['balance']})?\n"
        f"Он пропадёт из всех списков. История оплат сохранится.",
        reply_markup=kb,
    )
    await cb.answer()


@dp.callback_query(F.data == "delno")
async def delete_cancel(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.edit_text("Удаление отменено.")
    await cb.answer()


@dp.callback_query(F.data.startswith("delok:"))
async def delete_confirm(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    sid = int(cb.data.split(":", 1)[1])
    s = core.get_student(sid)
    name = s["name"] if s else "ученик"
    core.deactivate(sid)
    await cb.message.edit_text(f"Удалён: {name}.")
    await cb.answer("Готово")


# ------------------------- fallback (регистрируется последним) -------------------------
# Ловит всё, что не попало в обработчики выше. Порядок важен: этот хендлер
# должен идти самым последним, иначе перехватит кнопки педагога/ученика.

ADMIN_BTN_TEXTS = {BTN_NEW, BTN_LESSON, BTN_LIST, BTN_PAY, BTN_LINK, BTN_DELETE}


@dp.message()
async def fallback(message: Message):
    # Ученик (или посторонний) пытается использовать кнопку педагога —
    # даём понятный отказ, а не молчание.
    if not is_admin(message.from_user.id) and message.text in ADMIN_BTN_TEXTS:
        await message.answer("Эта функция доступна только педагогу.")
        return
    if is_admin(message.from_user.id):
        await message.answer("Не понял. Пользуйся кнопками внизу 👇", reply_markup=ADMIN_MENU)
    else:
        student = core.get_student_by_tg(message.from_user.id)
        if student:
            await message.answer("Нажми «💳 Мой баланс», чтобы увидеть остаток.",
                                 reply_markup=STUDENT_MENU)
        else:
            await message.answer("Открой персональную ссылку от педагога, чтобы привязаться.")


# ------------------------- запуск -------------------------

async def main():
    global BOT_USERNAME
    core.init_db()
    me = await bot.get_me()
    BOT_USERNAME = me.username
    logging.info("Bot @%s запущен", BOT_USERNAME)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
