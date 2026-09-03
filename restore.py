"""
restore.py — восстановление учеников после потери базы.

Что делает:
  1) читает файл anketas.txt, куда ты вставляешь сообщения-анкеты из чата
     (те самые «🆕 Новая анкета ...», что бот присылал педагогу),
  2) заново создаёт учеников со всеми данными,
  3) по желанию — сам находит их VK-аккаунты в списке диалогов сообщества
     и восстанавливает привязку, чтобы людям НЕ пришлось регистрироваться
     заново и они сразу видели свой баланс.

Запуск:
    export VK_TOKEN="ключ_сообщества"      # нужен только для шага 3
    export DATA_DIR="/app/data"            # там же, где база у бота
    python3 restore.py anketas.txt

Сначала показывает, что собирается сделать, и ждёт подтверждения —
ничего не пишется в базу, пока не согласишься.
"""

import os
import re
import sys
import time

import core

try:
    import requests
except ImportError:
    requests = None

API_URL = "https://api.vk.com/method/"
API_VERSION = "5.131"


# ------------------------- разбор анкет из текста -------------------------

FIELD_PATTERNS = {
    "child_name": re.compile(r"^Реб[её]нок:\s*(.+)$", re.M),
    "adult_name": re.compile(r"^Взрослый:\s*(.+)$", re.M),
    "parent_name": re.compile(r"^Родитель:\s*(.+)$", re.M),
    "birth_date": re.compile(r"^Дата рождения:\s*(.+)$", re.M),
    "phone": re.compile(r"^Телефон:\s*(.+)$", re.M),
    "contact_channel": re.compile(r"^Канал связи:\s*(.+)$", re.M),
}


def parse_anketas(text: str) -> list[dict]:
    """
    Разбивает текст на блоки анкет. Разделителем считается строка с «Новая
    анкета» — всё до следующей такой строки относится к одному человеку.
    Блоки без имени пропускаются.
    """
    chunks = re.split(r"(?=🆕?\s*Нов(?:ая|ые) анкет)", text)
    out = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        data = {}
        for key, pat in FIELD_PATTERNS.items():
            m = pat.search(chunk)
            if m:
                data[key] = m.group(1).strip()
        if "child_name" in data:
            data["category"] = "child"
            data["name"] = data.pop("child_name")
        elif "adult_name" in data:
            data["category"] = "adult"
            data["name"] = data.pop("adult_name")
        else:
            continue  # блок без имени — не анкета
        data.pop("adult_name", None)
        data.pop("child_name", None)
        out.append(data)
    return out


# ------------------------- поиск VK id по диалогам -------------------------

def api(method: str, token: str, **params):
    params["access_token"] = token
    params["v"] = API_VERSION
    r = requests.post(API_URL + method, data=params, timeout=30)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"VK API {method}: {data['error'].get('error_msg')}")
    return data.get("response")


def fetch_dialog_users(token: str) -> dict[int, str]:
    """
    Все люди, которые когда-либо писали сообществу: {vk_id: "Имя Фамилия"}.
    Именно отсюда берутся привязки — переписка никуда не делась, даже если
    база стёрлась.
    """
    users: dict[int, str] = {}
    offset = 0
    while True:
        resp = api("messages.getConversations", token,
                   count=200, offset=offset, extended=1)
        profiles = {p["id"]: f"{p.get('first_name','')} {p.get('last_name','')}".strip()
                    for p in resp.get("profiles", [])}
        users.update(profiles)
        items = resp.get("items", [])
        if len(items) < 200:
            break
        offset += 200
        time.sleep(0.34)  # лимит VK — не больше 3 запросов в секунду
    return users


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def match_vk_id(student_name: str, parent_name: str | None,
                dialog_users: dict[int, str]) -> int | None:
    """
    Ищем аккаунт по имени. В анкете обычно ФИО целиком («Петрова Анна
    Сергеевна»), а в профиле VK — только имя и фамилия («Анна Петрова»),
    причём в другом порядке. Поэтому сравниваем наборы слов в обе стороны:
    совпадением считается, если одно имя целиком содержится в другом
    (минимум два общих слова — по одному имени легко привязать не того).

    Для детей пробуем ещё и имя родителя — писал-то обычно он.
    Если совпадений несколько, возвращаем None: лучше не привязать,
    чем привязать чужой аккаунт.
    """
    for cand in (n for n in (student_name, parent_name) if n):
        c_words = set(_norm(cand).split())
        if len(c_words) < 2:
            continue
        hits = []
        for uid, name in dialog_users.items():
            n_words = set(_norm(name).split())
            if len(n_words) < 2:
                continue
            common = c_words & n_words
            if len(common) >= 2 and (c_words <= n_words or n_words <= c_words):
                hits.append(uid)
        if len(hits) == 1:
            return hits[0]
    return None


# ------------------------- основной сценарий -------------------------

def main():
    if len(sys.argv) < 2:
        print("Использование: python3 restore.py anketas.txt")
        sys.exit(1)

    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        text = f.read()

    anketas = parse_anketas(text)
    if not anketas:
        print("В файле не найдено ни одной анкеты.")
        print("Проверь, что вставлены сообщения вида «🆕 Новая анкета ...» целиком.")
        sys.exit(1)

    core.init_db()
    print(f"База: {core.DB_PATH}")
    print(f"Сейчас в базе учеников: {len(core.list_students())}")
    print(f"Найдено анкет в файле: {len(anketas)}\n")

    # Уже существующих (по имени) пропускаем, чтобы не наплодить дублей,
    # если скрипт запустят дважды.
    existing = {_norm(s["name"]) for s in core.list_students()}
    to_add = [a for a in anketas if _norm(a["name"]) not in existing]
    skipped = len(anketas) - len(to_add)

    for a in to_add:
        who = "ребёнок" if a["category"] == "child" else "взрослый"
        extra = f", родитель: {a['parent_name']}" if a.get("parent_name") else ""
        print(f"  + {a['name']} ({who}{extra})")
    if skipped:
        print(f"\n  (пропущено, уже есть в базе: {skipped})")

    if not to_add:
        print("\nДобавлять нечего — все эти ученики уже в базе.")
        return

    # Привязки VK — только если есть токен и библиотека
    token = os.environ.get("VK_TOKEN", "").strip()
    dialog_users: dict[int, str] = {}
    if token and requests is not None:
        print("\nИщу их VK-аккаунты в диалогах сообщества...")
        try:
            dialog_users = fetch_dialog_users(token)
            print(f"Найдено собеседников: {len(dialog_users)}")
        except Exception as e:
            print(f"Не удалось получить диалоги ({e}). Привязки восстановлены не будут.")
    else:
        print("\nVK_TOKEN не задан — привязки восстанавливать не буду, "
              "только данные учеников.")

    ans = input("\nВосстановить этих учеников? [y/N] ").strip().lower()
    if ans not in ("y", "yes", "д", "да"):
        print("Отменено, в базу ничего не записано.")
        return

    created = linked = 0
    for a in to_add:
        vk_id = match_vk_id(a["name"], a.get("parent_name"), dialog_users) \
            if dialog_users else None
        if vk_id:
            core.register_with_anketa(
                vk_id, "vk",
                category=a["category"],
                display_name=a["name"],
                parent_name=a.get("parent_name"),
                birth_date=a.get("birth_date"),
                phone=a.get("phone"),
                contact_channel=a.get("contact_channel"),
            )
            linked += 1
            print(f"  ✔ {a['name']} — восстановлен, привязка найдена (vk id {vk_id})")
        else:
            sid = core.add_student(a["name"], 0)
            with core._db() as c:
                c.execute(
                    "UPDATE students SET category=?, parent_name=?, birth_date=?, "
                    "phone=?, contact_channel=? WHERE id=?",
                    (a["category"], a.get("parent_name"), a.get("birth_date"),
                     a.get("phone"), a.get("contact_channel"), sid),
                )
            print(f"  ✔ {a['name']} — восстановлен (привязку не нашёл, "
                  f"выдай ему ссылку из бота)")
        created += 1

    print(f"\nГотово. Восстановлено: {created}, из них с привязкой VK: {linked}.")
    print(f"Всего учеников в базе: {len(core.list_students())}")
    print("\nЧто дальше:")
    print("  • Проставь всем баланс занятий кнопкой «💰 Оплата» — он не хранится в анкетах.")
    print("  • Заново создай группы и распредели учеников («👥 Группы»).")
    if created - linked:
        print(f"  • Тем {created - linked}, кому привязку не нашёл, отправь ссылку "
              f"из кнопки «🔗 Ссылка» — они привяжутся в один клик.")


if __name__ == "__main__":
    main()
