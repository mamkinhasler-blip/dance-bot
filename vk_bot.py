"""
vk_bot.py — адаптер ВКонтакте. Тонкая обёртка вокруг core.py.

База общая с Telegram-ботом: отметил занятие в ВК — в телеграме у ученика
сразу новый остаток. Можно запускать оба бота одновременно.

Работает напрямую с Bots Long Poll API через requests, без сторонних
фреймворков — меньше зависимостей, нечему ломаться при их обновлениях.

ВАЖНО про клавиатуры: везде используются обычные reply-кнопки (внизу экрана),
а не inline/callback. В VK-клиентах (и на телефоне, и в браузере) обнаружился
надёжно воспроизводимый баг: callback-кнопки внутри сообщения зависают на
бесконечной загрузке, хотя сервер получает корректные данные (проверено
напрямую через API). Reply-кнопки рендерятся стабильно всегда — поэтому весь
интерфейс сделан только на них, ценой того, что кнопки выбора появляются
внизу экрана, а не приклеенными к конкретному сообщению.

Запуск:
    export VK_TOKEN="ключ_доступа_сообщества"
    export VK_GROUP_ID="123456789"     # только цифры, без 'club'
    export VK_ADMIN_ID="12345,67890"   # VK id админов через запятую (можно один)
    python vk_bot.py
"""

import json
import logging
import os
import random
import sqlite3
import time

import requests

import core

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vk_bot")

VK_TOKEN = os.environ["VK_TOKEN"]
GROUP_ID = int(os.environ["VK_GROUP_ID"])
# Несколько админов сразу: и педагог, и разработчик могут управлять ботом
# каждый под своим VK-аккаунтом. Формат: "111,222,333" или просто "111".
ADMIN_IDS = {int(x.strip()) for x in os.environ["VK_ADMIN_ID"].split(",") if x.strip()}
# ADMIN_ID оставлен как алиас первого админа — используется там, где раньше
# было единственное значение (например, некому конкретно писать, если админов
# несколько — тогда шлём всем через notify_all_admins).
ADMIN_ID = next(iter(ADMIN_IDS))

API_URL = "https://api.vk.com/method/"
API_VERSION = "5.131"

# Обычная (не-inline) клавиатура ограничена мягче, чем inline, но всё равно
# не резиновая — держим список коротким и листаем страницами.
PAGE_SIZE = 6

session = requests.Session()


# ------------------------- низкоуровневый доступ к API -------------------------

def api(method: str, **params):
    """Вызов метода VK API. При сетевой ошибке повторяет попытку."""
    params["access_token"] = VK_TOKEN
    params["v"] = API_VERSION
    for attempt in range(3):
        try:
            r = session.post(API_URL + method, data=params, timeout=30)
            data = r.json()
        except Exception as e:
            log.warning("Сеть недоступна (%s), попытка %s/3", e, attempt + 1)
            time.sleep(2)
            continue
        if "error" in data:
            err = data["error"]
            log.error("VK API %s: %s", method, err.get("error_msg"))
            return None
        return data.get("response")
    return None


def send(peer_id: int, text: str, keyboard=None):
    params = {
        "peer_id": peer_id,
        "message": text,
        "random_id": random.randint(1, 2**31),
    }
    if keyboard is not None:
        params["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
    return api("messages.send", **params)


def send_with_attachment(peer_id: int, text: str, attachment: str, keyboard=None):
    params = {
        "peer_id": peer_id,
        "message": text,
        "random_id": random.randint(1, 2**31),
        "attachment": attachment,
    }
    if keyboard is not None:
        params["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
    return api("messages.send", **params)


def extract_photo_attachment(msg: dict) -> str | None:
    """Достаёт строку вложения-фото ('photo{owner}_{id}[_{access_key}]') из
    входящего сообщения, если админ прислал картинку расписания."""
    for att in (msg.get("attachments") or []):
        if att.get("type") == "photo":
            p = att.get("photo", {})
            if "owner_id" in p and "id" in p:
                s = f"photo{p['owner_id']}_{p['id']}"
                if p.get("access_key"):
                    s += f"_{p['access_key']}"
                return s
    return None


# ------------------------- клавиатуры (только reply-тип) -------------------------

BTN_NEW = "➕ Новый ученик"
BTN_LESSON = "✅ Занятие"
BTN_LIST = "📋 Список"
BTN_PAY = "💰 Оплата"
BTN_LINK = "🔗 Ссылка"
BTN_DELETE = "🗑 Удалить"
BTN_CANCEL = "◀️ Отмена"
BTN_BACK = "⬅️ Назад"
BTN_MORE = "➡️ Ещё"
BTN_DONE = "✅ Готово"
BTN_CONFIRM_DELETE = "🗑 Да, удалить"
BTN_MY_BALANCE = "💳 Мой баланс"
BTN_MY_DEPOSIT = "🏦 Мой депозит"
BTN_PRICE = "💵 Стоимость"
BTN_SCHEDULE = "🗓 Расписание"
BTN_DEPOSIT = "🏦 Депозит"
BTN_CARD = "ℹ️ Карточка ученика"
BTN_UPLOAD_SCHEDULE = "📤 Обновить расписание"
BTN_CHILD = "👶 Ребёнок"
BTN_ADULT = "🧑 Взрослый"

# Цены — по запросу педагога. Если поменяются, править только тут.
PRICE_TEXT = (
    "💵 Стоимость абонементов\n\n"
    "👶 Групповые занятия — дети:\n"
    "1 занятие — 400₽\n"
    "4 занятия — 1500₽\n"
    "8 занятий — 2900₽\n"
    "12 занятий — 4300₽\n\n"
    "🧑 Групповые занятия — взрослые:\n"
    "1 занятие — 600₽\n"
    "4 занятия — 2300₽\n"
    "8 занятий — 4500₽\n"
    "12 занятий — 6700₽\n\n"
    "🎯 Индивидуальные занятия:\n"
    "Дети — 1200₽ / занятие\n"
    "Взрослые — 1800₽ / занятие\n\n"
    "⏱ Длительность одного занятия — 45 минут."
)

# Команды главного меню педагога — доступны только админу.
ADMIN_MENU_CMDS = {"new", "lesson", "list", "pay", "link", "delete", "deposit", "card", "upload_schedule"}


def _btn(label: str, cmd: str, extra: dict | None = None, color: str = "secondary"):
    payload = {"cmd": cmd}
    if extra:
        payload.update(extra)
    return {
        "action": {"type": "text", "label": label, "payload": json.dumps(payload)},
        "color": color,
    }


ADMIN_MENU = {
    "one_time": False,
    "inline": False,
    "buttons": [
        [_btn(BTN_NEW, "new", color="primary"), _btn(BTN_LESSON, "lesson", color="positive")],
        [_btn(BTN_LIST, "list"), _btn(BTN_PAY, "pay")],
        [_btn(BTN_LINK, "link"), _btn(BTN_DELETE, "delete", color="negative")],
        [_btn(BTN_CARD, "card"), _btn(BTN_DEPOSIT, "deposit")],
        [_btn(BTN_SCHEDULE, "schedule"), _btn(BTN_UPLOAD_SCHEDULE, "upload_schedule")],
    ],
}

CANCEL_MENU = {
    "one_time": False,
    "inline": False,
    "buttons": [[_btn(BTN_CANCEL, "cancel", color="negative")]],
}

STUDENT_MENU = {
    "one_time": False,
    "inline": False,
    "buttons": [
        [_btn(BTN_MY_BALANCE, "mybalance", color="primary"), _btn(BTN_MY_DEPOSIT, "mydeposit")],
        [_btn(BTN_PRICE, "price"), _btn(BTN_SCHEDULE, "schedule")],
    ],
}

CATEGORY_MENU = {
    "one_time": False,
    "inline": False,
    "buttons": [[
        _btn(BTN_CHILD, "reg_cat", {"cat": "child"}, color="primary"),
        _btn(BTN_ADULT, "reg_cat", {"cat": "adult"}, color="primary"),
    ]],
}


def paged(students: list, page: int) -> tuple[list, int, int]:
    total = max(1, (len(students) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total - 1))
    return students[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], page, total


def pick_keyboard(action: str, page: int) -> dict:
    """Список учеников для выбора: оплата / ссылка / удаление."""
    students = core.list_students()
    chunk, page, total = paged(students, page)
    rows = [
        [_btn(f"{s['name']} ({s['balance']})", "pick", {"act": action, "id": s["id"]})]
        for s in chunk
    ]
    nav = []
    if page > 0:
        nav.append(_btn(BTN_BACK, "pick_page", {"act": action, "page": page - 1}))
    if page < total - 1:
        nav.append(_btn(BTN_MORE, "pick_page", {"act": action, "page": page + 1}))
    if nav:
        rows.append(nav)
    rows.append([_btn(BTN_CANCEL, "pick_cancel", color="negative")])
    return {"one_time": False, "inline": False, "buttons": rows}


def attendance_keyboard(selected: set[int], page: int) -> dict:
    students = core.list_students()
    chunk, page, total = paged(students, page)
    rows = []
    for s in chunk:
        box = "☑️" if s["id"] in selected else "☐"
        rows.append([_btn(
            f"{box} {s['name']} ({s['balance']})",
            "att", {"id": s["id"], "page": page},
            color="positive" if s["id"] in selected else "secondary",
        )])
    nav = []
    if page > 0:
        nav.append(_btn(BTN_BACK, "att_page", {"page": page - 1}))
    if page < total - 1:
        nav.append(_btn(BTN_MORE, "att_page", {"page": page + 1}))
    if nav:
        rows.append(nav)
    rows.append([
        _btn(BTN_DONE, "att_done", color="primary"),
        _btn(BTN_CANCEL, "att_cancel", color="negative"),
    ])
    return {"one_time": False, "inline": False, "buttons": rows}


def confirm_delete_keyboard(sid: int) -> dict:
    return {
        "one_time": False,
        "inline": False,
        "buttons": [[
            _btn(BTN_CONFIRM_DELETE, "del_ok", {"id": sid}, color="negative"),
            _btn(BTN_CANCEL, "pick_cancel", color="secondary"),
        ]],
    }


# ------------------------- состояние диалогов -------------------------
# Простой словарь в памяти: педагог один, сложный FSM тут излишен.
# {user_id: {"step": "...", "data": {...}}}
_state: dict[int, dict] = {}
# выбранные на текущем занятии ученики: {user_id: set(student_id)}
_attendance: dict[int, set[int]] = {}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def student_link(claim_code: str) -> str:
    return f"https://vk.me/club{GROUP_ID}?ref={claim_code}"


def student_card_text(s: sqlite3.Row) -> str:
    """
    Полная карточка ученика для педагога: категория, анкетные данные,
    баланс и депозит. Показывается по кнопке «Карточка ученика», раз
    анкету заполняли не просто так, а чтобы её можно было потом посмотреть.
    """
    lines = [f"ℹ️ Карточка ученика: {s['name']}"]

    category = s["category"]
    if category == "child":
        lines.append("Категория: ребёнок")
        if s["parent_name"]:
            lines.append(f"Родитель: {s['parent_name']}")
    elif category == "adult":
        lines.append("Категория: взрослый")
    else:
        lines.append("Категория: не указана (добавлен(а) вручную, без анкеты)")

    if s["birth_date"]:
        lines.append(f"Дата рождения: {s['birth_date']}")
    if s["phone"]:
        lines.append(f"Телефон: {s['phone']}")
    if s["contact_channel"]:
        lines.append(f"Канал связи: {s['contact_channel']}")

    lines.append("")
    lines.append(f"💳 Баланс: {s['balance']} занятий")
    lines.append(f"🏦 Депозит: {s['deposit']} ₽")

    return "\n".join(lines)


def send_schedule(peer_id: int, user_id: int):
    photo = core.get_setting("schedule_photo")
    kb = ADMIN_MENU if is_admin(user_id) else STUDENT_MENU
    if photo:
        send_with_attachment(peer_id, "🗓 Расписание:", photo, kb)
    else:
        send(peer_id, "Расписание пока готовится и скоро появится здесь 🙂", kb)


def notify_admin_registration(student: sqlite3.Row):
    """
    Всем админам (педагогу и кому ещё нужно) приходит вся анкета нового
    ученика сразу, как только человек её заполнил, плюс кнопка сразу
    назначить количество занятий.
    """
    if student["category"] == "child":
        details = (
            f"Ребёнок: {student['name']}\n"
            f"Родитель: {student['parent_name']}\n"
        )
    else:
        details = f"Взрослый: {student['name']}\n"
    details += (
        f"Дата рождения: {student['birth_date']}\n"
        f"Телефон: {student['phone']}\n"
        f"Канал связи: {student['contact_channel']}"
    )
    text = f"🆕 Новая анкета\n\n{details}\n\n💰 Баланс пока 0 занятий. Назначить количество занятий?"
    kb = {
        "one_time": False,
        "inline": False,
        "buttons": [[
            _btn("💰 Задать занятия", "pick", {"act": "pay", "id": student["id"]}, color="primary"),
        ]],
    }
    for admin_id in ADMIN_IDS:
        send(admin_id, text, kb)


def notify_student_low_balance(student_id: int, balance: int):
    """
    Пишет лично ученику (не только педагогу), когда у него осталось 1 занятие
    или занятия закончились — чтобы он сам вспомнил продлить абонемент.
    Отправляется, только если у ученика есть привязанный vk_id.
    """
    s = core.get_student(student_id)
    if not s or not s["vk_id"]:
        return
    if balance <= 0:
        text = (
            f"Ваши занятия закончились. Чтобы продолжить, пожалуйста, продлите "
            f"абонемент — нажмите «{BTN_PRICE}», чтобы посмотреть варианты, и "
            f"свяжитесь с педагогом."
        )
    else:
        text = (
            f"У Вас осталось всего 1 занятие! Если хотите продолжить без перерыва, "
            f"стоит заранее продлить абонемент. Нажмите «{BTN_PRICE}», чтобы "
            f"посмотреть варианты."
        )
    send(s["vk_id"], text, STUDENT_MENU)


# ------------------------- обработка сообщений -------------------------

def handle_message(msg: dict, client_info: dict | None = None):
    user_id = msg.get("from_id")
    peer_id = msg.get("peer_id")
    text = (msg.get("text") or "").strip()

    payload = {}
    raw = msg.get("payload")
    if raw:
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            payload = {}

    cmd = payload.get("cmd")

    # --- ученик пришёл по персональной ссылке (?ref=КОД) ---
    ref = payload.get("ref")
    if ref and not is_admin(user_id):
        student = core.claim_by_code(ref, user_id, "vk")
        if student:
            send(peer_id,
                 f"Здравствуйте, {student['name']}! 👋\n"
                 f"Вы успешно привязаны к боту.\n\n"
                 f"📊 Осталось занятий: {student['balance']}",
                 STUDENT_MENU)
            return
    if ref and is_admin(user_id):
        send(peer_id, "Вы педагог — эта ссылка предназначена для учеников, Вам привязываться не нужно.",
             ADMIN_MENU)
        return

    # --- отмена в любой момент ---
    if cmd == "cancel":
        _state.pop(user_id, None)
        _attendance.pop(user_id, None)
        if is_admin(user_id):
            send(peer_id, "Отменено.", ADMIN_MENU)
        else:
            send(peer_id, "Действие отменено. Напишите что-нибудь, чтобы начать заново.")
        return

    # --- «Стоимость» / «Расписание» — доступны всем, в любой момент ---
    if cmd == "price" or text == BTN_PRICE:
        send(peer_id, PRICE_TEXT, ADMIN_MENU if is_admin(user_id) else STUDENT_MENU)
        return
    if cmd == "schedule" or text == BTN_SCHEDULE:
        send_schedule(peer_id, user_id)
        return

    # --- выбор категории при регистрации (кнопка Ребёнок/Взрослый) ---
    if cmd == "reg_cat":
        handle_reg_category(payload, peer_id, user_id)
        return

    # --- педагог прислал фото расписания по ранее запрошенному шагу ---
    st = _state.get(user_id)
    if st and st.get("step") == "await_schedule_photo" and is_admin(user_id):
        attachment = extract_photo_attachment(msg)
        if attachment:
            core.set_setting("schedule_photo", attachment)
            _state.pop(user_id, None)
            send(peer_id, "Расписание успешно обновлено ✅", ADMIN_MENU)
        else:
            send(peer_id, "Пожалуйста, пришлите именно фото — как картинку, не файлом и не ссылкой.")
        return

    # --- команды главного меню и выбора ученика (оплата/ссылка/удаление/
    #     депозит/расписание) — идут ДО проверки диалога, т.к. сами эти
    #     кнопки переключают режим ---
    if cmd and is_admin(user_id) and handle_admin_cmd(cmd, payload, peer_id, user_id):
        return
    if cmd and not is_admin(user_id) and cmd in (ADMIN_MENU_CMDS | {
        "pick", "pick_page", "pick_cancel", "att", "att_page", "att_done",
        "att_cancel", "del_ok",
    }):
        send(peer_id, "Эта функция доступна только педагогу.")
        return

    # --- незавершённый диалог (ввод текста внутри многошагового процесса) ---
    if st:
        handle_dialog(user_id, peer_id, text, st)
        return

    # --- ученик смотрит баланс или депозит ---
    if cmd == "mybalance" or text == BTN_MY_BALANCE:
        student = core.get_student_by_user(user_id, "vk")
        if student:
            send(peer_id, f"📊 Осталось занятий: {student['balance']}", STUDENT_MENU)
        else:
            send(peer_id, "Вы пока не привязаны. Пожалуйста, попросите у педагога ссылку.")
        return

    if cmd == "mydeposit" or text == BTN_MY_DEPOSIT:
        student = core.get_student_by_user(user_id, "vk")
        if student:
            dep = student["deposit"]
            if dep > 0:
                send(peer_id, f"🏦 На Вашем депозите (несданная сдача): {dep} ₽", STUDENT_MENU)
            else:
                send(peer_id, "🏦 На данный момент депозита у Вас нет.", STUDENT_MENU)
        else:
            send(peer_id, "Вы пока не привязаны. Пожалуйста, попросите у педагога ссылку.")
        return

    # --- главное меню педагога по кнопке из reply-клавиатуры (текстом) ---
    if is_admin(user_id) and text in {
        BTN_NEW, BTN_LESSON, BTN_LIST, BTN_PAY, BTN_LINK, BTN_DELETE,
        BTN_DEPOSIT, BTN_CARD, BTN_UPLOAD_SCHEDULE,
    }:
        handle_admin_cmd(_label_to_cmd(text), {}, peer_id, user_id)
        return

    # --- старт / любое прочее сообщение педагога ---
    if is_admin(user_id):
        send(peer_id, "Панель педагога. Пользуйтесь кнопками ниже 👇", ADMIN_MENU)
        return

    student = core.get_student_by_user(user_id, "vk")
    if student:
        send(peer_id, f"Нажмите «{BTN_MY_BALANCE}», чтобы увидеть остаток.", STUDENT_MENU)
        return

    # --- ученик прислал код текстом (запасной вариант ручной привязки,
    #     когда педагог уже завёл его через "Новый ученик" и дал код) ---
    if text:
        claimed = core.claim_by_code(text, user_id, "vk")
        if claimed:
            send(peer_id,
                 f"Здравствуйте, {claimed['name']}! 👋\n"
                 f"Вы успешно привязаны к боту.\n\n"
                 f"📊 Осталось занятий: {claimed['balance']}",
                 STUDENT_MENU)
            return

    # --- иначе это первый контакт совсем нового человека — запускаем анкету ---
    _state[user_id] = {"step": "reg_category", "data": {}}
    send(peer_id, "Здравствуйте! 👋\nПодскажите, пожалуйста, для кого будут занятия:", CATEGORY_MENU)


def handle_reg_category(payload: dict, peer_id: int, user_id: int):
    cat = payload.get("cat")
    if cat == "child":
        _state[user_id] = {"step": "reg_parent_name", "data": {"category": "child"}}
        send(peer_id, "Подскажите, пожалуйста, ФИО родителя:", CANCEL_MENU)
    else:
        _state[user_id] = {"step": "reg_full_name", "data": {"category": "adult"}}
        send(peer_id, "Подскажите, пожалуйста, Ваше ФИО:", CANCEL_MENU)


def _label_to_cmd(label: str) -> str:
    return {
        BTN_NEW: "new", BTN_LESSON: "lesson", BTN_LIST: "list",
        BTN_PAY: "pay", BTN_LINK: "link", BTN_DELETE: "delete",
        BTN_DEPOSIT: "deposit", BTN_CARD: "card", BTN_UPLOAD_SCHEDULE: "upload_schedule",
    }.get(label, "")


def handle_admin_cmd(cmd: str, payload: dict, peer_id: int, user_id: int) -> bool:
    """
    Обрабатывает все команды, приходящие через payload кнопок педагога.
    Возвращает True, если команда была обработана (чтобы вызывающий код
    не пытался интерпретировать сообщение как что-то ещё).
    """
    titles = {
        "pay": "Кому пополнить занятия?", "link": "Кому дать ссылку?",
        "delete": "Кого удалить?", "deposit": "У кого проверить депозит?",
        "card": "О ком показать карточку?",
    }

    if cmd == "new":
        _state[user_id] = {"step": "name", "data": {}}
        send(peer_id, "Введите, пожалуйста, имя ученика:", CANCEL_MENU)
        return True

    if cmd == "upload_schedule":
        _state[user_id] = {"step": "await_schedule_photo", "data": {}}
        send(peer_id, "Пожалуйста, пришлите фото расписания следующим сообщением (как картинку).",
             CANCEL_MENU)
        return True

    if cmd == "list":
        students = core.list_students()
        if not students:
            send(peer_id, f'Учеников пока нет. Добавьте через «{BTN_NEW}».', ADMIN_MENU)
            return True
        lines = [
            f"{s['name']} — {s['balance']}" + (" ⚠️" if s["balance"] <= 1 else "")
            for s in students
        ]
        send(peer_id, "📋 Список учеников:\n\n" + "\n".join(lines), ADMIN_MENU)
        return True

    if cmd == "lesson":
        if not core.list_students():
            send(peer_id, f'Сначала добавьте учеников через «{BTN_NEW}».', ADMIN_MENU)
            return True
        _attendance[user_id] = set()
        _state[user_id] = {"step": "attendance", "data": {"page": 0}}
        send(peer_id, "Отметьте, кто был на занятии, затем нажмите «Готово»:",
             attendance_keyboard(set(), 0))
        return True

    if cmd in {"pay", "link", "delete", "deposit", "card"}:
        if not core.list_students():
            send(peer_id, "Учеников пока нет.", ADMIN_MENU)
            return True
        _state[user_id] = {"step": "picking", "data": {"act": cmd, "page": 0}}
        send(peer_id, titles[cmd], pick_keyboard(cmd, 0))
        return True

    if cmd == "pick_page":
        act = payload.get("act")
        page = payload.get("page", 0)
        _state[user_id] = {"step": "picking", "data": {"act": act, "page": page}}
        send(peer_id, titles.get(act, "Выберите ученика:"), pick_keyboard(act, page))
        return True

    if cmd == "pick_cancel":
        _state.pop(user_id, None)
        send(peer_id, "Отменено.", ADMIN_MENU)
        return True

    if cmd == "pick":
        act = payload.get("act")
        sid = payload.get("id")
        s = core.get_student(sid)
        if not s:
            send(peer_id, "Ученик не найден.", ADMIN_MENU)
            return True
        if act == "pay":
            _state[user_id] = {"step": "topup", "data": {"sid": sid}}
            send(peer_id, f"Сколько занятий добавить для «{s['name']}»? Введите число:", CANCEL_MENU)
        elif act == "link":
            _state.pop(user_id, None)
            send(peer_id,
                 f"🔗 Ссылка для «{s['name']}» — отправьте её ученику:\n"
                 f"{student_link(s['claim_code'])}\n\n"
                 f"Запасной код (если ссылка не сработает): {s['claim_code']}",
                 ADMIN_MENU)
        elif act == "delete":
            _state[user_id] = {"step": "confirm_delete", "data": {"sid": sid}}
            send(peer_id,
                 f"Удалить ученика «{s['name']}» (баланс {s['balance']})?\n"
                 f"Он пропадёт из всех списков, но история оплат сохранится.",
                 confirm_delete_keyboard(sid))
        elif act == "deposit":
            _state[user_id] = {"step": "deposit_amount", "data": {"sid": sid}}
            send(peer_id,
                 f"У «{s['name']}» сейчас на депозите: {s['deposit']} ₽.\n"
                 f"Введите новую сумму (только число, ₽ указывать не нужно):",
                 CANCEL_MENU)
        elif act == "card":
            _state.pop(user_id, None)
            send(peer_id, student_card_text(s), ADMIN_MENU)
        return True

    if cmd == "del_ok":
        sid = payload.get("id")
        s = core.get_student(sid)
        name = s["name"] if s else "ученик"
        core.deactivate(sid)
        _state.pop(user_id, None)
        send(peer_id, f"Удалён: {name} ✅", ADMIN_MENU)
        return True

    if cmd == "att":
        selected = _attendance.setdefault(user_id, set())
        sid = payload.get("id")
        if sid in selected:
            selected.remove(sid)
        else:
            selected.add(sid)
        page = payload.get("page", 0)
        send(peer_id, "Отметьте, кто был на занятии, затем нажмите «Готово»:",
             attendance_keyboard(selected, page))
        return True

    if cmd == "att_page":
        selected = _attendance.setdefault(user_id, set())
        page = payload.get("page", 0)
        send(peer_id, "Отметьте, кто был на занятии, затем нажмите «Готово»:",
             attendance_keyboard(selected, page))
        return True

    if cmd == "att_cancel":
        _attendance.pop(user_id, None)
        _state.pop(user_id, None)
        send(peer_id, "Отменено.", ADMIN_MENU)
        return True

    if cmd == "att_done":
        selected = _attendance.get(user_id) or set()
        _state.pop(user_id, None)
        if not selected:
            send(peer_id, "Никто не отмечен.", ADMIN_MENU)
            return True
        result = core.mark_lesson(list(selected))
        _attendance.pop(user_id, None)
        lines = [f"{r['name']} → осталось {r['balance']}" for r in result]
        ran_out = [r["name"] for r in result if r["out"]]
        text = "✅ Занятие отмечено:\n\n" + "\n".join(lines)
        if ran_out:
            text += "\n\n⚠️ Занятия закончились, нужна оплата:\n" + "\n".join(ran_out)
        send(peer_id, text, ADMIN_MENU)
        for r in result:
            if r["balance"] <= 1:
                notify_student_low_balance(r["id"], r["balance"])
        return True

    return False


def handle_dialog(user_id: int, peer_id: int, text: str, st: dict):
    """Пошаговый текстовый ввод: имя ученика, количество занятий, оплата."""
    step = st["step"]

    if step == "name":
        if not text:
            send(peer_id, "Пожалуйста, введите имя ученика текстом.")
            return
        st["data"]["name"] = text
        st["step"] = "lessons"
        send(peer_id, "Сколько занятий оплачено? Введите число:")
        return

    if step == "lessons":
        if not text.isdigit():
            send(peer_id, "Нужно просто число. Сколько занятий оплачено?")
            return
        lessons = int(text)
        sid = core.add_student(st["data"]["name"], lessons)
        _state.pop(user_id, None)
        s = core.get_student(sid)
        send(peer_id,
             f"✅ Ученик добавлен: {s['name']}\n"
             f"💳 Баланс: {lessons} занятий\n\n"
             f"🔗 Ссылка для ученика:\n{student_link(s['claim_code'])}\n\n"
             f"Если ссылка не сработает, ученик может прислать боту этот код: {s['claim_code']}",
             ADMIN_MENU)
        return

    if step == "topup":
        if not text.isdigit():
            send(peer_id, "Нужно просто число.")
            return
        sid = st["data"]["sid"]
        new_bal = core.top_up(sid, int(text))
        _state.pop(user_id, None)
        s = core.get_student(sid)
        send(peer_id, f"✅ {s['name']}: баланс теперь {new_bal} занятий.", ADMIN_MENU)
        return

    if step == "deposit_amount":
        if not text.isdigit():
            send(peer_id, "Нужно просто число (сумма депозита в рублях).")
            return
        sid = st["data"]["sid"]
        new_dep = core.set_deposit(sid, int(text))
        _state.pop(user_id, None)
        s = core.get_student(sid)
        send(peer_id, f"✅ {s['name']}: депозит теперь {new_dep} ₽.", ADMIN_MENU)
        return

    # --- анкета нового ученика (ребёнок или взрослый) ---
    if step == "reg_category":
        send(peer_id, "Пожалуйста, выберите вариант кнопкой ниже:", CATEGORY_MENU)
        return

    if step == "reg_parent_name":
        if not text:
            send(peer_id, "Пожалуйста, напишите ФИО родителя текстом.")
            return
        st["data"]["parent_name"] = text
        st["step"] = "reg_child_name"
        send(peer_id, "Спасибо! Теперь подскажите, пожалуйста, ФИО ребёнка:")
        return

    if step == "reg_child_name":
        if not text:
            send(peer_id, "Пожалуйста, напишите ФИО ребёнка текстом.")
            return
        st["data"]["name"] = text
        st["step"] = "reg_birth"
        send(peer_id, "Дата рождения ребёнка? Например: 01.01.2015")
        return

    if step == "reg_full_name":
        if not text:
            send(peer_id, "Пожалуйста, напишите ФИО текстом.")
            return
        st["data"]["name"] = text
        st["step"] = "reg_birth"
        send(peer_id, "Дата рождения? Например: 01.01.1995")
        return

    if step == "reg_birth":
        if not text:
            send(peer_id, "Пожалуйста, укажите дату рождения текстом.")
            return
        st["data"]["birth_date"] = text
        st["step"] = "reg_phone"
        send(peer_id, "Укажите, пожалуйста, номер телефона для связи:")
        return

    if step == "reg_phone":
        if not text:
            send(peer_id, "Пожалуйста, укажите номер телефона текстом.")
            return
        st["data"]["phone"] = text
        st["step"] = "reg_channel"
        send(peer_id, "Какой канал связи для Вас удобен? (соцсеть и ссылка на неё)")
        return

    if step == "reg_channel":
        if not text:
            send(peer_id, "Пожалуйста, укажите канал связи текстом.")
            return
        st["data"]["contact_channel"] = text
        data = st["data"]
        student = core.register_with_anketa(
            user_id, "vk",
            category=data["category"],
            display_name=data["name"],
            parent_name=data.get("parent_name"),
            birth_date=data.get("birth_date"),
            phone=data.get("phone"),
            contact_channel=data.get("contact_channel"),
        )
        _state.pop(user_id, None)
        send(peer_id,
             f"Спасибо, {student['name']}! ✅\n"
             f"Анкета заполнена. Как только педагог назначит количество занятий, "
             f"Вы сможете видеть остаток через «{BTN_MY_BALANCE}».",
             STUDENT_MENU)
        notify_admin_registration(student)
        return

    # Остальные шаги (picking / attendance / confirm_delete / await_schedule_photo)
    # полностью управляются кнопками с payload и обрабатываются в
    # handle_admin_cmd / handle_message — сюда доходят, только если админ
    # написал что-то текстом мимо кнопок.
    if is_admin(user_id):
        send(peer_id, "Пожалуйста, пользуйтесь кнопками ниже 👇")


# ------------------------- подписка на сообщество -------------------------

def handle_group_join(obj: dict):
    """
    Событие group_join прилетает, когда человек подписывается на сообщество.
    Пытаемся сразу начать анкету — если у человека разрешены сообщения от
    сообщества, он получит вопрос "для кого занятия" сразу же, не дожидаясь,
    пока сам напишет боту. Если сообщения запрещены его настройками
    приватности — отправка молча не удастся, ничего страшного: анкета
    запустится, как только он сам напишет боту в первый раз.
    """
    user_id = obj.get("user_id")
    if not user_id or is_admin(user_id):
        return
    if core.get_student_by_user(user_id, "vk"):
        return
    _state[user_id] = {"step": "reg_category", "data": {}}
    send(user_id, "Здравствуйте! 👋\nПодскажите, пожалуйста, для кого будут занятия:", CATEGORY_MENU)
    log.info("Подписчик %s: отправлена анкета", user_id)


# ------------------------- цикл Long Poll -------------------------

def get_longpoll_server():
    resp = api("groups.getLongPollServer", group_id=GROUP_ID)
    if not resp:
        raise RuntimeError(
            "Не удалось получить Long Poll сервер. Проверь VK_TOKEN, VK_GROUP_ID "
            "и что Long Poll API включён в настройках сообщества."
        )
    return resp["server"], resp["key"], resp["ts"]


def run():
    core.init_db()
    server, key, ts = get_longpoll_server()
    log.info("VK-бот запущен, сообщество %s", GROUP_ID)

    while True:
        try:
            r = session.get(
                server,
                params={"act": "a_check", "key": key, "ts": ts, "wait": 25},
                timeout=40,
            )
            data = r.json()
        except Exception as e:
            log.warning("Ошибка Long Poll (%s), переподключаюсь", e)
            time.sleep(3)
            try:
                server, key, ts = get_longpoll_server()
            except Exception:
                time.sleep(5)
            continue

        if "failed" in data:
            code = data["failed"]
            if code == 1:
                ts = data.get("ts", ts)
            else:
                server, key, ts = get_longpoll_server()
            continue

        ts = data.get("ts", ts)

        for upd in data.get("updates", []):
            try:
                if upd.get("type") == "message_new":
                    obj = upd["object"]
                    handle_message(obj.get("message", obj), obj.get("client_info"))
                elif upd.get("type") == "group_join":
                    handle_group_join(upd["object"])
            except Exception:
                log.exception("Ошибка при обработке события")


if __name__ == "__main__":
    run()
