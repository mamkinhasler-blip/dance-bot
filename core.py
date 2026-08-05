"""
core.py — вся бизнес-логика и работа с БД.

Этот модуль НЕ знает ничего про Telegram или MAX. Он просто хранит учеников,
их баланс занятий и историю посещений. И Telegram-бот, и будущий MAX-бот
будут вызывать одни и те же функции отсюда. Логика пишется один раз.

БД — SQLite, один файл на диске. Для одной студии этого хватает на годы,
внешняя база и деньги за неё не нужны.
"""

import sqlite3
import secrets
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent / "dance.db"


@contextmanager
def _db():
    """
    Открывает соединение, коммитит при успехе, откатывает при ошибке
    и ГАРАНТИРОВАННО закрывает соединение (иначе они текут — типичная
    ловушка sqlite3, где `with conn` управляет только транзакцией).
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Создаёт таблицы при первом запуске."""
    with _db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS students (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                name     TEXT    NOT NULL,
                balance  INTEGER NOT NULL DEFAULT 0,
                tg_id    INTEGER,              -- telegram id ученика, если он привязался
                claim_code TEXT,               -- код для самопривязки по ссылке
                active   INTEGER NOT NULL DEFAULT 1,
                created  TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS visits (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                ts         TEXT    NOT NULL,
                FOREIGN KEY (student_id) REFERENCES students(id)
            );

            CREATE TABLE IF NOT EXISTS payments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id INTEGER NOT NULL,
                amount     INTEGER NOT NULL,
                ts         TEXT    NOT NULL,
                FOREIGN KEY (student_id) REFERENCES students(id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        # Миграция: колонка vk_id появилась позже, чем tg_id. Добавляем её,
        # если база создана старой версией — иначе VK-бот упадёт на запросах.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(students)")}
        if "vk_id" not in cols:
            c.execute("ALTER TABLE students ADD COLUMN vk_id INTEGER")

        # Миграция: поля анкеты (категория/родитель/дата рождения/телефон/
        # канал связи) и депозит (несданная сдача) появились позже.
        anketa_cols = {
            "category": "TEXT",          # 'child' или 'adult'
            "parent_name": "TEXT",       # ФИО родителя, только для детей
            "birth_date": "TEXT",
            "phone": "TEXT",
            "contact_channel": "TEXT",
            "deposit": "INTEGER NOT NULL DEFAULT 0",
        }
        for col, coltype in anketa_cols.items():
            if col not in cols:
                c.execute(f"ALTER TABLE students ADD COLUMN {col} {coltype}")


def get_setting(key: str) -> str | None:
    with _db() as c:
        row = c.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str):
    with _db() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# Поле в таблице students, где хранится id пользователя для каждой платформы.
_ID_FIELD = {"tg": "tg_id", "vk": "vk_id"}


def _id_field(platform: str) -> str:
    field = _ID_FIELD.get(platform)
    if not field:
        raise ValueError(f"Неизвестная платформа: {platform}")
    return field


def _now() -> str:
    return datetime.now().isoformat()


# ------------------------- ученики -------------------------

def add_student(name: str, lessons: int) -> int:
    """Добавить ученика с начальным балансом. Возвращает id.

    Имя обрезается по краям; пустое имя не допускается (проверяй на стороне бота).
    Отрицательный стартовый баланс приводится к нулю.
    """
    name = name.strip()
    if not name:
        raise ValueError("Пустое имя ученика")
    lessons = max(0, lessons)
    code = secrets.token_urlsafe(8)
    with _db() as c:
        cur = c.execute(
            "INSERT INTO students (name, balance, claim_code, created) VALUES (?,?,?,?)",
            (name, lessons, code, _now()),
        )
        sid = cur.lastrowid
        if lessons:
            c.execute(
                "INSERT INTO payments (student_id, amount, ts) VALUES (?,?,?)",
                (sid, lessons, _now()),
            )
    return sid


def get_or_create_by_platform_id(
    user_id: int, platform: str, name: str
) -> tuple[sqlite3.Row, bool]:
    """
    Найти ученика по id на платформе (vk/tg) или создать нового с балансом 0,
    если это первый контакт. Используется для авто-регистрации при подписке
    на сообщество или первом сообщении боту — без ручного /новый от педагога.

    Возвращает (запись, создан_ли_только_что). Защищено от дублей: если
    запись с таким user_id на этой платформе уже есть — вернёт её, вторую
    не создаст, даже если имя изменилось (например, человек переименовался
    в VK) — имя в базе не перезаписывается автоматически, чтобы не затереть
    правки, которые педагог мог внести вручную.
    """
    field = _id_field(platform)
    existing = get_student_by_user(user_id, platform)
    if existing:
        return existing, False

    name = (name or "").strip() or "Без имени"
    code = secrets.token_urlsafe(8)
    with _db() as c:
        cur = c.execute(
            f"INSERT INTO students (name, balance, {field}, claim_code, created) "
            f"VALUES (?,0,?,?,?)",
            (name, user_id, code, _now()),
        )
        sid = cur.lastrowid
        row = c.execute("SELECT * FROM students WHERE id = ?", (sid,)).fetchone()
    return row, True


def register_with_anketa(
    user_id: int,
    platform: str,
    *,
    category: str,
    display_name: str,
    parent_name: str | None = None,
    birth_date: str | None = None,
    phone: str | None = None,
    contact_channel: str | None = None,
) -> sqlite3.Row:
    """
    Создаёт ученика сразу с заполненной анкетой (категория ребёнок/взрослый +
    контактные данные) — используется после того, как человек прошёл опрос
    при первом обращении к боту. category: 'child' или 'adult'.
    Если запись для этого user_id на этой платформе уже существует —
    возвращает её как есть, не создавая дубль.
    """
    existing = get_student_by_user(user_id, platform)
    if existing:
        return existing
    field = _id_field(platform)
    code = secrets.token_urlsafe(8)
    with _db() as c:
        cur = c.execute(
            f"INSERT INTO students "
            f"(name, balance, {field}, claim_code, created, category, "
            f" parent_name, birth_date, phone, contact_channel) "
            f"VALUES (?,0,?,?,?,?,?,?,?,?)",
            (display_name.strip(), user_id, code, _now(), category,
             parent_name, birth_date, phone, contact_channel),
        )
        sid = cur.lastrowid
        row = c.execute("SELECT * FROM students WHERE id = ?", (sid,)).fetchone()
    return row


def set_deposit(student_id: int, amount: int) -> int:
    """Установить сумму депозита (несданной сдачи). Возвращает новое значение."""
    amount = max(0, int(amount))
    with _db() as c:
        c.execute("UPDATE students SET deposit = ? WHERE id = ?", (amount, student_id))
        row = c.execute("SELECT deposit FROM students WHERE id = ?", (student_id,)).fetchone()
    return row["deposit"]


def list_students(only_active: bool = True) -> list[sqlite3.Row]:
    q = "SELECT * FROM students"
    if only_active:
        q += " WHERE active = 1"
    q += " ORDER BY name COLLATE NOCASE"
    with _db() as c:
        return c.execute(q).fetchall()


def get_student(student_id: int) -> sqlite3.Row | None:
    with _db() as c:
        return c.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()


def top_up(student_id: int, amount: int) -> int:
    """Пополнить баланс. Возвращает новый баланс. amount должен быть > 0."""
    amount = int(amount)
    with _db() as c:
        c.execute(
            "UPDATE students SET balance = balance + ? WHERE id = ?",
            (amount, student_id),
        )
        c.execute(
            "INSERT INTO payments (student_id, amount, ts) VALUES (?,?,?)",
            (student_id, amount, _now()),
        )
        row = c.execute("SELECT balance FROM students WHERE id = ?", (student_id,)).fetchone()
    return row["balance"]


def mark_visit(student_id: int) -> int:
    """
    Отметить посещение: -1 занятие (но НЕ ниже нуля) + запись в историю.
    Возвращает новый баланс. Посещение записывается всегда — даже если
    занятия кончились (факт присутствия важно сохранить), но баланс в минус
    не уходит.
    """
    with _db() as c:
        # атомарно: не даём балансу уйти ниже 0
        c.execute(
            "UPDATE students SET balance = MAX(0, balance - 1) WHERE id = ?",
            (student_id,),
        )
        c.execute(
            "INSERT INTO visits (student_id, ts) VALUES (?,?)",
            (student_id, _now()),
        )
        row = c.execute("SELECT balance FROM students WHERE id = ?", (student_id,)).fetchone()
    return row["balance"]


def mark_lesson(student_ids: list[int]) -> list[dict]:
    """
    Отметить занятие для списка учеников сразу.
    Возвращает список словарей: {id, name, balance, out}, где out=True означает,
    что у ученика на момент отметки уже не было занятий (пришёл «в долг» —
    педагогу нужно взять оплату). id нужен, чтобы бот мог написать напоминание
    лично ученику, если у него закончился баланс.
    """
    result = []
    for sid in student_ids:
        s = get_student(sid)
        if not s:
            continue
        before = s["balance"]
        new_bal = mark_visit(sid)
        result.append({"id": sid, "name": s["name"], "balance": new_bal, "out": before <= 0})
    return result


def deactivate(student_id: int):
    """Убрать ученика из активного списка (не удаляем, чтобы сохранить историю)."""
    with _db() as c:
        c.execute("UPDATE students SET active = 0 WHERE id = ?", (student_id,))


# ------------------------- привязка ученика -------------------------

def claim_by_code(code: str, user_id: int, platform: str = "tg") -> sqlite3.Row | None:
    """Ученик открыл свою ссылку — привязываем его id к записи.

    platform: "tg" (Telegram) или "vk" (ВКонтакте). Один и тот же ученик может
    быть привязан к обеим платформам одновременно — это разные колонки.
    Привязываем только активных учеников. Пустой/несуществующий код -> None.
    """
    if not code:
        return None
    field = _id_field(platform)
    with _db() as c:
        row = c.execute(
            "SELECT * FROM students WHERE claim_code = ? AND active = 1", (code,)
        ).fetchone()
        if row:
            c.execute(
                f"UPDATE students SET {field} = ? WHERE id = ?", (user_id, row["id"])
            )
            return c.execute("SELECT * FROM students WHERE id = ?", (row["id"],)).fetchone()
    return None


def get_student_by_user(user_id: int, platform: str = "tg") -> sqlite3.Row | None:
    """Найти активного ученика по id пользователя на указанной платформе."""
    field = _id_field(platform)
    with _db() as c:
        return c.execute(
            f"SELECT * FROM students WHERE {field} = ? AND active = 1", (user_id,)
        ).fetchone()


def get_student_by_tg(tg_id: int) -> sqlite3.Row | None:
    """Совместимость со старым Telegram-ботом."""
    return get_student_by_user(tg_id, "tg")
