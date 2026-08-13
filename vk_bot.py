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


VK_LABEL_LIMIT = 40   # максимальная длина подписи кнопки
VK_MESSAGE_LIMIT = 4000  # у messages.send лимит ~4096, берём с запасом


def _cut(label: str, limit: int = VK_LABEL_LIMIT) -> str:
    """Обрезать подпись кнопки под лимит VK.

    Без этого длинное ФИО («Верещагина-Оболенская Анна-Мария
    Константиновна») вместе с чекбоксом и балансом выходит за 40 символов,
    и VK отказывается рисовать ВСЮ клавиатуру целиком — педагог остаётся
    без кнопок вообще.
    """
    label = str(label)
    return label if len(label) <= limit else label[:limit - 1].rstrip() + "…"


def send(peer_id: int, text: str, keyboard=None):
    """Отправить сообщение. Длинный текст (большой список или отчёт) режется
    на части: VK молча не доставит сообщение длиннее ~4096 символов.
    Клавиатура прикрепляется к последней части."""
    parts = _split_message(text)
    result = None
    for i, part in enumerate(parts):
        params = {
            "peer_id": peer_id,
            "message": part,
            "random_id": random.randint(1, 2**31),
        }
        if keyboard is not None and i == len(parts) - 1:
            params["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
        result = api("messages.send", **params)
    return result


def _split_message(text: str, limit: int = VK_MESSAGE_LIMIT) -> list[str]:
    """Разбить длинный текст по строкам, не разрывая строки посередине."""
    if len(text) <= limit:
        return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        # одна строка сама по себе длиннее лимита — режем жёстко
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


def send_with_attachment(peer_id: int, text: str, attachment: str, keyboard=None):
    params = {
        "peer_id": peer_id,
        "message": _split_message(text)[0],
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
BTN_GROUPS = "👥 Группы"
BTN_NEW_GROUP = "➕ Новая группа"
BTN_RENAME_GROUP = "✏️ Переименовать"
BTN_DELETE_GROUP = "🗑 Удалить группу"
BTN_CONFIRM_DELETE_GROUP = "🗑 Да, удалить группу"
BTN_GROUP_STUDENTS = "👥 Состав группы"
BTN_ASSIGN_GROUPS = "👥 Назначить группы"
BTN_ALL_STUDENTS = "👥 Все ученики"
BTN_NO_GROUP = "🚫 Без группы"
BTN_SELECT_ALL = "☑️ Отметить всех"
BTN_CLEAR_ALL = "☐ Снять всех"
BTN_REPORT = "📊 Отчёт"
# --- личный вопрос педагогу ---
BTN_SIGNUP = "📝 Записаться"
BTN_ASK = "❓ Задать вопрос"
BTN_RESUME_SELF = "🔙 Вернуться в меню"
BTN_DIALOGS = "💬 Вопросы"
BTN_REPLY = "💬 Ответить"
BTN_RESUME_BOT = "🔙 Вернуть бота"

# На сколько бот замолкает после вопроса. Пауза всегда конечная: если педагог
# забудет вернуть бота кнопкой, он включится сам — иначе человек останется
# без меню навсегда и решит, что бот сломался.
PAUSE_HOURS = 12

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
ADMIN_MENU_CMDS = {"new", "lesson", "list", "pay", "link", "delete", "deposit", "card",
                    "upload_schedule", "groups", "assign_groups", "report", "dialogs"}

# Дополнительные служебные команды (кнопки внутри многошаговых сценариев) —
# тоже только для админа, но не входят в главное меню.
ADMIN_ONLY_EXTRA_CMDS = {
    "pick", "pick_page", "pick_cancel", "att", "att_page", "att_done", "att_cancel",
    "att_all", "att_none", "del_ok",
    "lesson_pick_group", "lesson_all", "lesson_group", "lesson_nogroup", "lesson_group_page",
    "grp_new", "grp_open", "grp_rename", "grp_delete", "grp_delete_ok", "grp_page",
    "grp_students", "grp_stud_toggle", "grp_stud_page", "grp_stud_done", "grp_stud_cancel",
    "sgrp_start", "sgrp_toggle", "sgrp_page", "sgrp_done", "sgrp_cancel",
    "report_run", "noop", "reply_start", "resume_bot",
}


def _btn(label: str, cmd: str, extra: dict | None = None, color: str = "secondary"):
    payload = {"cmd": cmd}
    if extra:
        payload.update(extra)
    return {
        "action": {"type": "text", "label": _cut(label), "payload": json.dumps(payload)},
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
        [_btn(BTN_GROUPS, "groups"), _btn(BTN_ASSIGN_GROUPS, "assign_groups")],
        [_btn(BTN_REPORT, "report"), _btn(BTN_DIALOGS, "dialogs")],
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
        [_btn(BTN_ASK, "ask")],
    ],
}

# Первый экран для нового человека: сразу развилка «записаться» или «спросить».
# Раньше бот без спроса начинал анкету, и человеку с простым вопросом
# («а со скольки лет берёте?») приходилось её проходить.
WELCOME_MENU = {
    "one_time": False,
    "inline": False,
    "buttons": [
        [_btn(BTN_SIGNUP, "signup", color="primary"), _btn(BTN_ASK, "ask")],
        [_btn(BTN_PRICE, "price"), _btn(BTN_SCHEDULE, "schedule")],
    ],
}

# Единственная кнопка, которая остаётся у человека, пока с ним говорит педагог.
PAUSED_MENU = {
    "one_time": False,
    "inline": False,
    "buttons": [[_btn(BTN_RESUME_SELF, "resume_self")]],
}

WELCOME_TEXT = (
    "Здравствуйте! 👋\n"
    "Это бот студии. Чем можем помочь?\n\n"
    f"«{BTN_SIGNUP}» — заполнить короткую анкету и записаться на занятия.\n"
    f"«{BTN_ASK}» — написать педагогу лично, он ответит Вам в этом же чате."
)

CATEGORY_MENU = {
    "one_time": False,
    "inline": False,
    "buttons": [[
        _btn(BTN_CHILD, "reg_cat", {"cat": "child"}, color="primary"),
        _btn(BTN_ADULT, "reg_cat", {"cat": "adult"}, color="primary"),
    ]],
}


def paged(items: list, page: int) -> tuple[list, int, int]:
    total = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total - 1))
    return items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], page, total


def build_list_keyboard(items: list, page: int, row_btn, nav_cmd: str,
                        nav_extra: dict | None = None,
                        footer: list | None = None,
                        empty_text: str | None = None) -> dict:
    """
    Общий конструктор клавиатуры-списка с постраничной навигацией.
    Раньше эта логика была скопирована в четырёх местах, из-за чего экраны
    групп ушли без пагинации и ломались бы о лимит VK (10 рядов на клавиатуру).

    items    — что показываем (ученики, группы...)
    row_btn  — функция (item, page) -> кнопка
    nav_cmd  — cmd для кнопок «Назад»/«Ещё»
    nav_extra— что ещё положить в payload навигации (например, id группы)
    footer   — фиксированные ряды внизу (Готово/Отмена)
    """
    chunk, page, total = paged(items, page)
    rows = [[row_btn(it, page)] for it in chunk]
    extra = dict(nav_extra or {})
    nav = []
    if page > 0:
        nav.append(_btn(BTN_BACK, nav_cmd, {**extra, "page": page - 1}))
    if page < total - 1:
        nav.append(_btn(BTN_MORE, nav_cmd, {**extra, "page": page + 1}))
    if nav:
        rows.append(nav)
    if not chunk and empty_text:
        rows.append([_btn(empty_text, "noop", color="secondary")])
    rows.extend(footer or [])
    return {"one_time": False, "inline": False, "buttons": rows}


def pick_keyboard(action: str, page: int) -> dict:
    """Список учеников для выбора: оплата / ссылка / удаление / группы."""
    return build_list_keyboard(
        core.list_students(), page,
        row_btn=lambda s, p: _btn(f"{s['name']} ({s['balance']})", "pick",
                                  {"act": action, "id": s["id"]}),
        nav_cmd="pick_page", nav_extra={"act": action},
        footer=[[_btn(BTN_CANCEL, "pick_cancel", color="negative")]],
        empty_text="(список пуст)",
    )


def attendance_students(filter_type: str, group_id: int | None = None) -> list:
    """Список учеников для отметки занятия — либо все, либо только по группе,
    либо только те, кто ни в одной группе не состоит."""
    if filter_type == "group":
        return core.list_students_by_group(group_id)
    if filter_type == "none":
        return core.list_students_without_group()
    return core.list_students()


def attendance_title(filter_type: str, group_id: int | None = None) -> str:
    """Человеческое название текущего списка отметки."""
    if filter_type == "group":
        g = core.get_group(group_id)
        return g["name"] if g else "группа удалена"
    if filter_type == "none":
        return "без группы"
    return "все ученики"


def attendance_prompt(selected: set[int], filter_type: str = "all",
                       group_id: int | None = None) -> str:
    """
    Подпись над клавиатурой отметки. Показывает, ПО КАКОЙ группе идёт отметка
    и сколько уже отмечено — раньше педагог видел только безликое «отметьте,
    кто был», а при листании страниц было легко потерять счёт.
    """
    students = attendance_students(filter_type, group_id)
    ids = {s["id"] for s in students}
    done = len(selected & ids)
    return (
        f"✅ Занятие — {attendance_title(filter_type, group_id)}\n"
        f"Отмечено: {done} из {len(students)}\n\n"
        f"Отметьте, кто был, затем нажмите «{BTN_DONE}»."
    )


def attendance_keyboard(selected: set[int], page: int, filter_type: str = "all",
                         group_id: int | None = None) -> dict:
    students = attendance_students(filter_type, group_id)
    extra = {"filter_type": filter_type}
    if group_id is not None:
        extra["group_id"] = group_id

    def row(s, p):
        box = "☑️" if s["id"] in selected else "☐"
        return _btn(f"{box} {s['name']} ({s['balance']})", "att",
                    {**extra, "id": s["id"], "page": p},
                    color="positive" if s["id"] in selected else "secondary")

    # «Отметить всех» — в группе из 12 человек обычно проще снять двоих
    # отсутствующих, чем натыкать десять присутствующих.
    all_ids = {s["id"] for s in students}
    if all_ids and all_ids <= selected:
        bulk = _btn(BTN_CLEAR_ALL, "att_none", extra)
    else:
        bulk = _btn(BTN_SELECT_ALL, "att_all", extra, color="primary")

    return build_list_keyboard(
        students, page, row_btn=row, nav_cmd="att_page", nav_extra=extra,
        footer=[
            [bulk],
            [_btn(BTN_DONE, "att_done", color="primary"),
             _btn(BTN_CANCEL, "att_cancel", color="negative")],
        ],
        empty_text="(в этом списке пока никого нет)",
    )


def lesson_group_choice_keyboard(page: int = 0) -> dict:
    """Перед отметкой занятия — выбор, по какой группе отмечаем."""
    # «Все» и «Без группы» кладём в один ряд: у VK лимит 10 рядов на
    # клавиатуру, и при полной странице групп запас лишним не бывает.
    extras = [_btn(BTN_ALL_STUDENTS, "lesson_all")]
    if core.list_students_without_group():
        extras.append(_btn(BTN_NO_GROUP, "lesson_nogroup"))
    return build_list_keyboard(
        core.list_groups(), page,
        row_btn=lambda g, p: _btn(f"👥 {g['name']}", "lesson_group", {"id": g["id"]}),
        nav_cmd="lesson_group_page",
        footer=[extras, [_btn(BTN_CANCEL, "pick_cancel", color="negative")]],
    )


# ------------------------- клавиатуры для управления группами -------------------------

def groups_menu_keyboard(page: int = 0) -> dict:
    return build_list_keyboard(
        core.list_groups(), page,
        row_btn=lambda g, p: _btn(f"👥 {g['name']}", "grp_open", {"id": g["id"]}),
        nav_cmd="grp_page",
        footer=[
            [_btn(BTN_NEW_GROUP, "grp_new", color="primary")],
            [_btn(BTN_CANCEL, "pick_cancel", color="negative")],
        ],
        empty_text="(групп пока нет)",
    )


def group_detail_keyboard(gid: int) -> dict:
    return {
        "one_time": False,
        "inline": False,
        "buttons": [
            [_btn(BTN_GROUP_STUDENTS, "grp_students", {"id": gid}, color="primary")],
            [_btn(BTN_RENAME_GROUP, "grp_rename", {"id": gid})],
            [_btn(BTN_DELETE_GROUP, "grp_delete", {"id": gid}, color="negative")],
            [_btn(BTN_BACK, "groups")],
        ],
    }


def group_confirm_delete_keyboard(gid: int) -> dict:
    return {
        "one_time": False,
        "inline": False,
        "buttons": [[
            _btn(BTN_CONFIRM_DELETE_GROUP, "grp_delete_ok", {"id": gid}, color="negative"),
            _btn(BTN_CANCEL, "pick_cancel", color="secondary"),
        ]],
    }


def group_students_keyboard(gid: int, page: int) -> dict:
    """Состав группы: чек-лист всех активных учеников, галочка = в группе.
    Нажатие сразу пишет в базу — отдельного «сохранить» не нужно."""
    member_ids = {s["id"] for s in core.list_students_by_group(gid)}

    def row(s, p):
        box = "☑️" if s["id"] in member_ids else "☐"
        return _btn(f"{box} {s['name']}", "grp_stud_toggle",
                    {"gid": gid, "id": s["id"], "page": p},
                    color="positive" if s["id"] in member_ids else "secondary")

    return build_list_keyboard(
        core.list_students(), page, row_btn=row,
        nav_cmd="grp_stud_page", nav_extra={"gid": gid},
        footer=[[_btn(BTN_DONE, "grp_stud_done", {"id": gid}, color="primary")]],
        empty_text="(учеников пока нет)",
    )


def student_groups_keyboard(sid: int, selected: set[int], page: int) -> dict:
    """Обратная сторона того же самого: список групп для одного ученика."""
    def row(g, p):
        box = "☑️" if g["id"] in selected else "☐"
        return _btn(f"{box} {g['name']}", "sgrp_toggle",
                    {"sid": sid, "id": g["id"], "page": p},
                    color="positive" if g["id"] in selected else "secondary")

    return build_list_keyboard(
        core.list_groups(), page, row_btn=row,
        nav_cmd="sgrp_page", nav_extra={"sid": sid},
        footer=[[
            _btn(BTN_DONE, "sgrp_done", {"sid": sid}, color="primary"),
            _btn(BTN_CANCEL, "sgrp_cancel", color="negative"),
        ]],
        empty_text="Групп пока нет — создайте в «Группы»",
    )


def report_period_keyboard() -> dict:
    return {
        "one_time": False,
        "inline": False,
        "buttons": [
            [_btn("📊 За неделю", "report_run", {"days": 7}, color="primary"),
             _btn("📊 За месяц", "report_run", {"days": 30}, color="primary")],
            [_btn("📊 За 3 месяца", "report_run", {"days": 90})],
            [_btn(BTN_CANCEL, "pick_cancel", color="negative")],
        ],
    }


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
# Состояние дублируется в базу (таблица sessions): раньше оно жило только в
# памяти процесса, и перезапуск бота посреди анкеты или отметки занятия
# терял всё молча. Теперь при старте состояние поднимается обратно.

class _Store(dict):
    """Словарь {user_id: значение}, зеркалящий записи в базу.

    encode/decode нужны для множеств (set не сериализуется в JSON напрямую).
    Мутации значения на месте (selected.add(...)) базой не отслеживаются —
    после них нужно явно вызвать .save(user_id).
    """

    def __init__(self, kind: str, encode=lambda v: v, decode=lambda v: v):
        super().__init__()
        self.kind = kind
        self._encode = encode
        self._decode = decode

    def load(self):
        for uid, payload in core.load_sessions("vk", self.kind).items():
            try:
                super().__setitem__(uid, self._decode(payload))
            except Exception:
                continue

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.save(key)

    def save(self, key):
        if key in self:
            try:
                core.save_session(key, "vk", self.kind, self._encode(self[key]))
            except Exception:
                log.exception("Не удалось сохранить сессию %s/%s", key, self.kind)

    def pop(self, key, default=None):
        try:
            core.delete_session(key, "vk", self.kind)
        except Exception:
            log.exception("Не удалось удалить сессию %s/%s", key, self.kind)
        return super().pop(key, default)

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]


# {user_id: {"step": "...", "data": {...}}}
_state: _Store = _Store("state")
# выбранные на текущем занятии ученики: {user_id: set(student_id)}
_attendance: _Store = _Store("attendance", encode=sorted, decode=set)
# выбранные группы в мультивыборе (назначение групп одному ученику)
_multi: _Store = _Store("multi", encode=sorted, decode=set)


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

    groups = core.get_student_groups(s["id"])
    lines.append(f"Группы: {', '.join(g['name'] for g in groups) if groups else 'не назначены'}")

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
    buttons = [[
        _btn("💰 Задать занятия", "pick", {"act": "pay", "id": student["id"]}, color="primary"),
    ]]
    if core.list_groups():
        buttons.append([_btn(BTN_ASSIGN_GROUPS, "sgrp_start", {"id": student["id"]})])
    kb = {"one_time": False, "inline": False, "buttons": buttons}
    for admin_id in ADMIN_IDS:
        send(admin_id, text, kb)


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение: 1 занятие, 2 занятия, 5 занятий."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def fetch_user_name(user_id: int) -> str | None:
    """Имя и фамилия человека из VK. Нужно, чтобы педагог видел, КТО задал
    вопрос, ещё до того как человек прошёл анкету. Если запрос не удался —
    ничего страшного, покажем id."""
    data = api("users.get", user_ids=user_id)
    if not data:
        return None
    try:
        u = data[0]
        return " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x) or None
    except (IndexError, KeyError, TypeError):
        return None


def student_display_name(user_id: int) -> str:
    """Как показать человека педагогу: имя из базы, если он уже ученик,
    иначе имя из VK, иначе просто ссылка на страницу."""
    s = core.get_student_by_user(user_id, "vk")
    if s:
        return s["name"]
    name = fetch_user_name(user_id)
    return name or f"id{user_id}"


def dialog_actions_keyboard(target_id: int) -> dict:
    """Кнопки педагога для конкретного личного диалога."""
    return {
        "one_time": False,
        "inline": False,
        "buttons": [
            [_btn(BTN_REPLY, "reply_start", {"uid": target_id}, color="primary")],
            [_btn(BTN_RESUME_BOT, "resume_bot", {"uid": target_id})],
            [_btn(BTN_BACK, "dialogs")],
        ],
    }


def notify_admins_question(user_id: int, question: str):
    """Новый вопрос от человека — уходит всем педагогам с кнопкой ответа."""
    who = student_display_name(user_id)
    text = (
        f"❓ Вопрос от {who}\n"
        f"vk.com/id{user_id}\n\n"
        f"{question}\n\n"
        f"Бот замолчал в этом диалоге на {PAUSE_HOURS} ч. "
        f"Ответьте кнопкой «{BTN_REPLY}» или прямо в сообщениях сообщества."
    )
    for admin_id in ADMIN_IDS:
        send(admin_id, text, dialog_actions_keyboard(user_id))


def relay_to_admins(user_id: int, text: str, msg: dict | None = None):
    """Пересылать педагогу всё, что пишет человек в режиме тишины —
    чтобы сообщение не потерялось, даже если педагог не открывает диалоги
    сообщества. Сам человек ответа от бота при этом не получает."""
    who = student_display_name(user_id)
    body = text.strip() if text else ""
    if not body:
        body = "(вложение — фото или файл, смотрите в диалогах сообщества)"
    out = f"💬 {who} пишет:\n\n{body}"
    for admin_id in ADMIN_IDS:
        send(admin_id, out, dialog_actions_keyboard(user_id))


def build_report_text(days: int) -> str:
    """Отчёт по посещаемости за период: сколько занятий провели по каждой
    группе и кто сколько раз пришёл. Верх списка — самые активные,
    низ — те, кто пропадает (им стоит написать)."""
    groups = core.group_attendance_report(days)
    students = core.student_attendance_report(days)
    lines = [f"📊 Отчёт за последние {days} дн."]

    lines.append("")
    if groups:
        lines.append("Занятия по группам:")
        for g in groups:
            lines.append(
                f"{g['name']} — {g['lessons']} "
                f"{_plural(g['lessons'], 'занятие', 'занятия', 'занятий')}, "
                f"{g['visits']} "
                f"{_plural(g['visits'], 'посещение', 'посещения', 'посещений')}"
            )
    else:
        lines.append("Занятий за период не отмечено.")

    if students:
        lines.append("")
        lines.append("Посещения учеников:")
        for s in students:
            mark = " ⚠️ не был(а)" if s["visits"] == 0 else ""
            lines.append(f"{s['name']} — {s['visits']} (баланс {s['balance']}){mark}")

    return "\n".join(lines)


def start_group_assignment(user_id: int, peer_id: int, sid: int):
    """Открыть мультивыбор групп для одного ученика — используется и из
    пункта меню «Назначить группы», и сразу после создания/регистрации
    ученика (кнопка-ярлык у админа)."""
    s = core.get_student(sid)
    if not s:
        send(peer_id, "Ученик не найден.", ADMIN_MENU)
        return
    selected = {g["id"] for g in core.get_student_groups(sid)}
    _multi[user_id] = selected
    _state[user_id] = {"step": "assign_groups", "data": {"sid": sid}}
    send(peer_id, f"Отметьте группы для «{s['name']}», затем нажмите «Готово»:",
         student_groups_keyboard(sid, selected, 0))


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

    # --- РЕЖИМ ТИШИНЫ: с человеком разговаривает живой педагог ---
    # Стоит раньше всех остальных проверок: пока пауза активна, бот не должен
    # вставлять ни одной своей реплики — ни меню, ни «пользуйтесь кнопками».
    # Исключение — кнопка возврата меню: она всегда должна работать, иначе
    # человек окажется в тупике.
    if not is_admin(user_id) and core.is_paused(user_id, "vk"):
        if cmd == "resume_self" or text == BTN_RESUME_SELF:
            core.resume_dialog(user_id, "vk")
            _state.pop(user_id, None)
            student = core.get_student_by_user(user_id, "vk")
            send(peer_id, "Возвращаю меню 👇", STUDENT_MENU if student else WELCOME_MENU)
            return
        # Бот молчит, но пересылает сообщение педагогу — чтобы вопрос не
        # потерялся, даже если педагог не сидит в диалогах сообщества.
        relay_to_admins(user_id, text, msg)
        return

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

    # --- развилка первого экрана: записаться / задать вопрос ---
    if cmd == "signup" or text == BTN_SIGNUP:
        student = core.get_student_by_user(user_id, "vk")
        if student:
            send(peer_id, "Вы уже записаны 🙂", STUDENT_MENU)
            return
        _state[user_id] = {"step": "reg_category", "data": {}}
        send(peer_id, "Подскажите, пожалуйста, для кого будут занятия:", CATEGORY_MENU)
        return

    if cmd == "ask" or text == BTN_ASK:
        _state[user_id] = {"step": "ask_question", "data": {}}
        send(peer_id,
             "Напишите Ваш вопрос одним сообщением — я передам его педагогу.\n"
             "Он ответит Вам лично в этом же чате.",
             CANCEL_MENU)
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
    if cmd and not is_admin(user_id) and cmd in (ADMIN_MENU_CMDS | ADMIN_ONLY_EXTRA_CMDS):
        send(peer_id, "Эта функция доступна только педагогу.")
        return

    # --- незавершённый диалог (ввод текста внутри многошагового процесса) ---
    if st:
        handle_dialog(user_id, peer_id, text, st)
        # Шаги диалога меняют st на месте (st["step"] = ...), а такие мутации
        # словарь сам в базу не пишет — сохраняем явно, иначе после
        # перезапуска человек откатится на шаг назад.
        if user_id in _state:
            _state.save(user_id)
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
        BTN_DEPOSIT, BTN_CARD, BTN_UPLOAD_SCHEDULE, BTN_GROUPS, BTN_ASSIGN_GROUPS,
        BTN_REPORT, BTN_DIALOGS,
    }:
        handle_admin_cmd(_label_to_cmd(text), {}, peer_id, user_id)
        return

    # --- старт / любое прочее сообщение педагога ---
    if is_admin(user_id):
        send(peer_id, "Панель педагога. Пользуйтесь кнопками ниже 👇", ADMIN_MENU)
        return

    student = core.get_student_by_user(user_id, "vk")
    if student:
        send(peer_id, f"Нажмите «{BTN_MY_BALANCE}», чтобы увидеть остаток, "
                      f"или «{BTN_ASK}», если нужен педагог.", STUDENT_MENU)
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

    # --- иначе это первый контакт совсем нового человека ---
    # Анкету больше не запускаем без спроса: сначала спрашиваем, чего человек
    # хочет — записаться или просто задать вопрос.
    send(peer_id, WELCOME_TEXT, WELCOME_MENU)


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
        BTN_GROUPS: "groups", BTN_ASSIGN_GROUPS: "assign_groups", BTN_REPORT: "report",
        BTN_DIALOGS: "dialogs",
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
        "card": "О ком показать карточку?", "assign_groups": "Кому назначить группы?",
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
        groups = core.list_groups()
        if not groups:
            # групп ещё нет — старое плоское поведение
            lines = [
                f"{s['name']} — {s['balance']}" + (" ⚠️" if s["balance"] <= 1 else "")
                for s in students
            ]
            send(peer_id, "📋 Список учеников:\n\n" + "\n".join(lines), ADMIN_MENU)
            return True

        def _line(s):
            return f"{s['name']} — {s['balance']}" + (" ⚠️" if s["balance"] <= 1 else "")

        blocks = []
        for g in groups:
            members = core.list_students_by_group(g["id"])
            header = f"👥 {g['name']} ({len(members)}):"
            block = header + "\n" + ("\n".join(_line(s) for s in members) if members else "(пусто)")
            blocks.append(block)
        no_group = core.list_students_without_group()
        if no_group:
            blocks.append(f"🚫 Без группы ({len(no_group)}):\n" + "\n".join(_line(s) for s in no_group))
        header = f"📋 Список учеников — всего {len(students)}"
        send(peer_id, header + "\n\n" + "\n\n".join(blocks), ADMIN_MENU)
        return True

    if cmd == "lesson":
        if not core.list_students():
            send(peer_id, f'Сначала добавьте учеников через «{BTN_NEW}».', ADMIN_MENU)
            return True
        if not core.list_groups():
            # групп ещё нет — сразу общий список, как раньше
            _attendance[user_id] = set()
            _state[user_id] = {"step": "attendance", "data": {"page": 0, "filter_type": "all"}}
            send(peer_id, attendance_prompt(set(), "all"),
                 attendance_keyboard(set(), 0))
            return True
        _state[user_id] = {"step": "lesson_pick_group", "data": {}}
        send(peer_id, "По какой группе отмечаем занятие?", lesson_group_choice_keyboard())
        return True

    if cmd in {"lesson_all", "lesson_group", "lesson_nogroup"}:
        if cmd == "lesson_group":
            gid = payload.get("id")
            data = {"page": 0, "filter_type": "group", "group_id": gid}
        elif cmd == "lesson_nogroup":
            data = {"page": 0, "filter_type": "none"}
        else:
            data = {"page": 0, "filter_type": "all"}
        _attendance[user_id] = set()
        _state[user_id] = {"step": "attendance", "data": data}
        send(peer_id, attendance_prompt(set(), data["filter_type"], data.get("group_id")),
             attendance_keyboard(set(), 0, data["filter_type"], data.get("group_id")))
        return True

    if cmd == "dialogs":
        paused = core.list_paused("vk")
        if not paused:
            send(peer_id, "Открытых личных диалогов нет — бот отвечает всем сам.", ADMIN_MENU)
            return True
        lines = []
        rows = []
        for p in paused:
            who = p["name"] or fetch_user_name(p["user_id"]) or f"id{p['user_id']}"
            q = (p["question"] or "").strip().replace("\n", " ")
            if len(q) > 100:
                q = q[:99] + "…"
            lines.append(f"• {who}: {q or '(без текста)'}")
            rows.append([_btn(f"{BTN_REPLY} {who}", "reply_start", {"uid": p["user_id"]})])
        rows.append([_btn(BTN_CANCEL, "pick_cancel", color="negative")])
        send(peer_id,
             f"💬 Бот молчит в {len(paused)} "
             f"{_plural(len(paused), 'диалоге', 'диалогах', 'диалогах')}:\n\n"
             + "\n".join(lines),
             {"one_time": False, "inline": False, "buttons": rows[:10]})
        return True

    if cmd == "reply_start":
        uid = payload.get("uid")
        _state[user_id] = {"step": "reply_text", "data": {"uid": uid}}
        send(peer_id, f"Напишите ответ для {student_display_name(uid)} — "
                      f"я отправлю его от имени сообщества:", CANCEL_MENU)
        return True

    if cmd == "resume_bot":
        uid = payload.get("uid")
        core.resume_dialog(uid, "vk")
        student = core.get_student_by_user(uid, "vk")
        send(uid,
             "Спасибо за обращение! Бот снова на связи 👇",
             STUDENT_MENU if student else WELCOME_MENU)
        send(peer_id, f"Бот вернулся в диалог — {student_display_name(uid)} ✅", ADMIN_MENU)
        return True

    if cmd == "groups":
        groups = core.list_groups()
        text = "👥 Группы:" if groups else "Групп пока нет. Создайте первую:"
        send(peer_id, text, groups_menu_keyboard())
        return True

    if cmd == "grp_page":
        send(peer_id, "👥 Группы:", groups_menu_keyboard(payload.get("page", 0)))
        return True

    if cmd == "lesson_group_page":
        send(peer_id, "По какой группе отмечаем занятие?",
             lesson_group_choice_keyboard(payload.get("page", 0)))
        return True

    if cmd == "report":
        send(peer_id, "За какой период показать отчёт?", report_period_keyboard())
        return True

    if cmd == "report_run":
        days = payload.get("days", 30)
        send(peer_id, build_report_text(days), ADMIN_MENU)
        return True

    if cmd == "noop":
        return True

    if cmd == "grp_new":
        _state[user_id] = {"step": "group_name", "data": {}}
        send(peer_id, "Введите название группы:", CANCEL_MENU)
        return True

    if cmd == "grp_open":
        gid = payload.get("id")
        g = core.get_group(gid)
        if not g:
            send(peer_id, "Группа не найдена.", groups_menu_keyboard())
            return True
        members = core.list_students_by_group(gid)
        lines = [f"{s['name']} — {s['balance']}" + (" ⚠️" if s["balance"] <= 1 else "")
                 for s in members]
        text = f"👥 {g['name']}\n\nСостав ({len(members)}):\n" + (
            "\n".join(lines) if members else "(пока никого нет)"
        )
        send(peer_id, text, group_detail_keyboard(gid))
        return True

    if cmd == "grp_rename":
        gid = payload.get("id")
        _state[user_id] = {"step": "group_rename", "data": {"gid": gid}}
        send(peer_id, "Введите новое название группы:", CANCEL_MENU)
        return True

    if cmd == "grp_delete":
        gid = payload.get("id")
        g = core.get_group(gid)
        name = g["name"] if g else "группа"
        send(peer_id, f"Удалить группу «{name}»?\nУченики останутся, история посещений сохранится.",
             group_confirm_delete_keyboard(gid))
        return True

    if cmd == "grp_delete_ok":
        gid = payload.get("id")
        g = core.get_group(gid)
        name = g["name"] if g else "группа"
        core.deactivate_group(gid)
        send(peer_id, f"Группа «{name}» удалена ✅", ADMIN_MENU)
        return True

    if cmd == "grp_students":
        gid = payload.get("id")
        _state[user_id] = {"step": "grp_students", "data": {"gid": gid, "page": 0}}
        g = core.get_group(gid)
        send(peer_id, f"Состав группы «{g['name'] if g else ''}» — нажимайте, чтобы добавить/убрать:",
             group_students_keyboard(gid, 0))
        return True

    if cmd == "grp_stud_toggle":
        gid = payload.get("gid")
        sid = payload.get("id")
        page = payload.get("page", 0)
        current = {g["id"] for g in core.get_student_groups(sid)}
        if gid in current:
            current.discard(gid)
        else:
            current.add(gid)
        core.set_student_groups(sid, list(current))
        send(peer_id, "Отметьте состав группы:", group_students_keyboard(gid, page))
        return True

    if cmd == "grp_stud_page":
        gid = payload.get("gid")
        page = payload.get("page", 0)
        _state[user_id] = {"step": "grp_students", "data": {"gid": gid, "page": page}}
        send(peer_id, "Отметьте состав группы:", group_students_keyboard(gid, page))
        return True

    if cmd == "grp_stud_done":
        gid = payload.get("id")
        _state.pop(user_id, None)
        g = core.get_group(gid)
        send(peer_id, f"Состав группы «{g['name'] if g else ''}» сохранён ✅", ADMIN_MENU)
        return True

    if cmd == "sgrp_start":
        sid = payload.get("id")
        start_group_assignment(user_id, peer_id, sid)
        return True

    if cmd == "sgrp_toggle":
        sid = payload.get("sid")
        gid = payload.get("id")
        page = payload.get("page", 0)
        selected = _multi.setdefault(user_id, set())
        if gid in selected:
            selected.remove(gid)
        else:
            selected.add(gid)
        _multi.save(user_id)
        send(peer_id, "Отметьте группы ученика, затем нажмите «Готово»:",
             student_groups_keyboard(sid, selected, page))
        return True

    if cmd == "sgrp_page":
        sid = payload.get("sid")
        page = payload.get("page", 0)
        selected = _multi.setdefault(user_id, set())
        send(peer_id, "Отметьте группы ученика, затем нажмите «Готово»:",
             student_groups_keyboard(sid, selected, page))
        return True

    if cmd == "sgrp_done":
        sid = payload.get("sid")
        selected = _multi.pop(user_id, set())
        core.set_student_groups(sid, list(selected))
        _state.pop(user_id, None)
        s = core.get_student(sid)
        send(peer_id, f"✅ Группы для «{s['name'] if s else ''}» сохранены.", ADMIN_MENU)
        return True

    if cmd == "sgrp_cancel":
        _multi.pop(user_id, None)
        _state.pop(user_id, None)
        send(peer_id, "Отменено.", ADMIN_MENU)
        return True

    if cmd == "att_noop":
        return True

    if cmd in {"pay", "link", "delete", "deposit", "card", "assign_groups"}:
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
        elif act == "assign_groups":
            start_group_assignment(user_id, peer_id, sid)
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
        _attendance.save(user_id)
        page = payload.get("page", 0)
        filter_type = payload.get("filter_type", "all")
        group_id = payload.get("group_id")
        send(peer_id, attendance_prompt(selected, filter_type, group_id),
             attendance_keyboard(selected, page, filter_type, group_id))
        return True

    if cmd in {"att_all", "att_none"}:
        filter_type = payload.get("filter_type", "all")
        group_id = payload.get("group_id")
        if cmd == "att_all":
            selected = {s["id"] for s in attendance_students(filter_type, group_id)}
        else:
            selected = set()
        _attendance[user_id] = selected
        send(peer_id, attendance_prompt(selected, filter_type, group_id),
             attendance_keyboard(selected, 0, filter_type, group_id))
        return True

    if cmd == "att_page":
        selected = _attendance.setdefault(user_id, set())
        page = payload.get("page", 0)
        filter_type = payload.get("filter_type", "all")
        group_id = payload.get("group_id")
        send(peer_id, attendance_prompt(selected, filter_type, group_id),
             attendance_keyboard(selected, page, filter_type, group_id))
        return True

    if cmd == "att_cancel":
        _attendance.pop(user_id, None)
        _state.pop(user_id, None)
        send(peer_id, "Отменено.", ADMIN_MENU)
        return True

    if cmd == "att_done":
        selected = _attendance.get(user_id) or set()
        st = _state.get(user_id) or {}
        group_id = (st.get("data") or {}).get("group_id")
        _state.pop(user_id, None)
        if not selected:
            send(peer_id, "Никто не отмечен.", ADMIN_MENU)
            return True
        result = core.mark_lesson(list(selected), group_id)
        _attendance.pop(user_id, None)
        lines = [f"{r['name']} → осталось {r['balance']}" for r in result]
        ran_out = [r["name"] for r in result if r["out"]]
        filter_type = (st.get("data") or {}).get("filter_type", "all")
        title = attendance_title(filter_type, group_id)
        text = (f"✅ Занятие отмечено — {title}\n"
                f"Всего: {len(result)} "
                f"{_plural(len(result), 'человек', 'человека', 'человек')}\n\n"
                + "\n".join(lines))
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
        if core.list_groups():
            send(peer_id, "Сразу назначить группы?", {
                "one_time": False, "inline": False,
                "buttons": [[_btn(BTN_ASSIGN_GROUPS, "sgrp_start", {"id": sid}, color="primary")]],
            })
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

    # --- управление группами (создание / переименование) ---
    if step == "group_name":
        if not text:
            send(peer_id, "Пожалуйста, введите название группы текстом.")
            return
        try:
            core.add_group(text)
        except ValueError:
            send(peer_id, "Название не может быть пустым. Введите ещё раз:")
            return
        _state.pop(user_id, None)
        send(peer_id, f"✅ Группа «{text.strip()}» создана.", ADMIN_MENU)
        return

    if step == "group_rename":
        if not text:
            send(peer_id, "Пожалуйста, введите новое название текстом.")
            return
        gid = st["data"]["gid"]
        try:
            core.rename_group(gid, text)
        except ValueError:
            send(peer_id, "Название не может быть пустым. Введите ещё раз:")
            return
        _state.pop(user_id, None)
        send(peer_id, f"✅ Группа переименована в «{text.strip()}».", ADMIN_MENU)
        return

    # --- человек пишет вопрос педагогу ---
    if step == "ask_question":
        if not text:
            send(peer_id, "Напишите, пожалуйста, вопрос текстом одним сообщением.", CANCEL_MENU)
            return
        _state.pop(user_id, None)
        core.pause_dialog(user_id, "vk", PAUSE_HOURS, question=text)
        notify_admins_question(user_id, text)
        send(peer_id,
             "Вопрос передан педагогу ✅\n"
             "Он ответит Вам здесь же — можете просто дождаться ответа "
             "и продолжить переписку в этом чате.\n\n"
             f"Кнопка «{BTN_RESUME_SELF}» вернёт меню бота, когда оно понадобится.",
             PAUSED_MENU)
        return

    # --- педагог пишет ответ на вопрос через бота ---
    if step == "reply_text":
        target = st["data"]["uid"]
        if not text:
            send(peer_id, "Напишите ответ текстом.", CANCEL_MENU)
            return
        _state.pop(user_id, None)
        ok = send(target, text, PAUSED_MENU)
        who = student_display_name(target)
        if ok:
            # Пауза продлевается: разговор идёт, боту рано вмешиваться.
            core.pause_dialog(target, "vk", PAUSE_HOURS)
            send(peer_id, f"Ответ отправлен ✅ — {who}", dialog_actions_keyboard(target))
        else:
            send(peer_id,
                 f"Не удалось отправить сообщение ({who}).\n"
                 "Скорее всего, человек запретил сообщения от сообщества — "
                 "ответьте ему через диалоги сообщества.",
                 ADMIN_MENU)
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
    Сразу здороваемся и показываем развилку «записаться / задать вопрос» —
    анкету без спроса больше не навязываем. Если сообщения от сообщества
    запрещены настройками приватности, отправка молча не удастся: ничего
    страшного, приветствие покажется, когда человек сам напишет боту.
    """
    user_id = obj.get("user_id")
    if not user_id or is_admin(user_id):
        return
    if core.get_student_by_user(user_id, "vk"):
        return
    if core.is_paused(user_id, "vk"):
        return
    send(user_id, WELCOME_TEXT, WELCOME_MENU)
    log.info("Подписчик %s: отправлено приветствие", user_id)


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
    # Поднимаем незавершённые диалоги и чистим зависшие: человек мог начать
    # анкету и пропасть — держать его состояние вечно смысла нет.
    purged = core.purge_old_sessions(24)
    for store in (_state, _attendance, _multi):
        store.load()
    if purged or _state:
        log.info("Сессии: восстановлено %s, удалено просроченных %s", len(_state), purged)
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
