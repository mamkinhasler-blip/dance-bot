"""
core.py — вся бизнес-логика и работа с БД.

Этот модуль НЕ знает ничего про Telegram или MAX. Он просто хранит учеников,
их баланс занятий и историю посещений. И Telegram-бот, и будущий MAX-бот
будут вызывать одни и те же функции отсюда. Логика пишется один раз.

БД — SQLite, один файл на диске. Для одной студии этого хватает на годы,
внешняя база и деньги за неё не нужны.
"""

import json
import logging
import os
import shutil
import sqlite3
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("core")

# ГДЕ ЛЕЖИТ БАЗА — самое важное место в файле.
#
# На хостинге папка с кодом пересоздаётся из репозитория при каждой
# пересборке, и всё, что лежало рядом с кодом, стирается вместе с ней.
# Данные обязаны жить в отдельном постоянном каталоге, который хостинг
# сохраняет между перезапусками — его путь приходит в переменной DATA_DIR
# (на bothost это /app/data).
#
# Если DATA_DIR не задан (локальный запуск на своём компьютере) — база
# просто лежит рядом с кодом, как раньше.
_DATA_DIR = os.environ.get("DATA_DIR", "").strip()
if _DATA_DIR:
    DB_DIR = Path(_DATA_DIR)
    DB_DIR.mkdir(parents=True, exist_ok=True)
else:
    DB_DIR = Path(__file__).parent

DB_PATH = DB_DIR / "dance.db"

# Переезд со старого места: если база ещё лежит рядом с кодом, а в постоянном
# каталоге её нет — переносим, чтобы не потерять учеников при обновлении.
_LEGACY_DB = Path(__file__).parent / "dance.db"
if DB_PATH != _LEGACY_DB and _LEGACY_DB.exists():
    if not DB_PATH.exists():
        try:
            shutil.copy2(_LEGACY_DB, DB_PATH)
            log.warning("База перенесена в постоянный каталог: %s -> %s", _LEGACY_DB, DB_PATH)
        except OSError as e:
            log.error("Не удалось перенести базу в %s: %s", DB_PATH, e)
    else:
        # Рабочая база уже есть в постоянном каталоге, а рядом с кодом лежит
        # ещё одна. Обычно это значит, что dance.db закоммичен в репозиторий:
        # при каждой пересборке он приезжает из гита и раньше затирал живые
        # данные. Сейчас он ни на что не влияет, но убрать его из репозитория
        # нужно — иначе однажды снова окажется единственным и подменит базу.
        log.warning(
            "Рядом с кодом лежит посторонний %s — похоже, файл базы попал в "
            "репозиторий. Рабочая база (%s) не затронута, но этот файл нужно "
            "удалить из репозитория.",
            _LEGACY_DB, DB_PATH,
        )


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

            CREATE TABLE IF NOT EXISTS groups (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                name    TEXT    NOT NULL,
                active  INTEGER NOT NULL DEFAULT 1,
                created TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS student_groups (
                student_id INTEGER NOT NULL,
                group_id   INTEGER NOT NULL,
                PRIMARY KEY (student_id, group_id),
                FOREIGN KEY (student_id) REFERENCES students(id),
                FOREIGN KEY (group_id)   REFERENCES groups(id)
            );

            -- Первичный ключ (student_id, group_id) не помогает выборке
            -- "все ученики группы" — для неё нужен отдельный индекс.
            CREATE INDEX IF NOT EXISTS idx_sg_group ON student_groups(group_id);
            CREATE INDEX IF NOT EXISTS idx_visits_ts ON visits(ts);

            -- Незавершённые диалоги (анкета, ввод числа, отметка занятия).
            -- Раньше жили только в памяти процесса и терялись при перезапуске.
            CREATE TABLE IF NOT EXISTS sessions (
                user_id  INTEGER NOT NULL,
                platform TEXT    NOT NULL,
                kind     TEXT    NOT NULL,
                payload  TEXT    NOT NULL,
                updated  TEXT    NOT NULL,
                PRIMARY KEY (user_id, platform, kind)
            );

            -- Человек задал вопрос педагогу: бот замолкает и не лезет в
            -- разговор, пока педагог не вернёт его кнопкой (или пока не
            -- истечёт срок паузы — чтобы забытая пауза не выключила бота
            -- для человека навсегда).
            CREATE TABLE IF NOT EXISTS paused_dialogs (
                user_id  INTEGER NOT NULL,
                platform TEXT    NOT NULL,
                since    TEXT    NOT NULL,
                until    TEXT    NOT NULL,
                question TEXT,
                PRIMARY KEY (user_id, platform)
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

        # Миграция: в какой группе проходило занятие. Нужна, чтобы потом
        # можно было построить статистику посещаемости по группам —
        # задним числом эти данные восстановить неоткуда.
        visit_cols = {r["name"] for r in c.execute("PRAGMA table_info(visits)")}
        if "group_id" not in visit_cols:
            c.execute("ALTER TABLE visits ADD COLUMN group_id INTEGER")


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


def mark_visit(student_id: int, group_id: int | None = None) -> int:
    """
    Отметить посещение: -1 занятие (но НЕ ниже нуля) + запись в историю.
    Возвращает новый баланс. Посещение записывается всегда — даже если
    занятия кончились (факт присутствия важно сохранить), но баланс в минус
    не уходит. group_id — по какой группе было занятие (может быть None).
    """
    with _db() as c:
        # атомарно: не даём балансу уйти ниже 0
        c.execute(
            "UPDATE students SET balance = MAX(0, balance - 1) WHERE id = ?",
            (student_id,),
        )
        c.execute(
            "INSERT INTO visits (student_id, ts, group_id) VALUES (?,?,?)",
            (student_id, _now(), group_id),
        )
        row = c.execute("SELECT balance FROM students WHERE id = ?", (student_id,)).fetchone()
    return row["balance"]


def mark_lesson(student_ids: list[int], group_id: int | None = None) -> list[dict]:
    """
    Отметить занятие для списка учеников сразу — одной транзакцией на всех,
    а не тремя запросами на каждого.
    Возвращает список словарей: {id, name, balance, out}, где out=True означает,
    что у ученика на момент отметки уже не было занятий (пришёл «в долг» —
    педагогу нужно взять оплату). id нужен, чтобы бот мог написать напоминание
    лично ученику, если у него закончился баланс.
    """
    result = []
    ts = _now()
    with _db() as c:
        for sid in student_ids:
            # active = 1: ученика могли удалить между выбором галочки и
            # нажатием «Готово» — списывать у него занятие уже не нужно
            s = c.execute(
                "SELECT id, name, balance FROM students WHERE id = ? AND active = 1", (sid,)
            ).fetchone()
            if not s:
                continue
            before = s["balance"]
            c.execute(
                "UPDATE students SET balance = MAX(0, balance - 1) WHERE id = ?", (sid,)
            )
            c.execute(
                "INSERT INTO visits (student_id, ts, group_id) VALUES (?,?,?)",
                (sid, ts, group_id),
            )
            new_bal = max(0, before - 1)
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


# ------------------------- группы -------------------------
# Ученик может состоять в нескольких группах одновременно (например,
# групповое расписание + индивидуальные) — связь "многие ко многим" через
# student_groups. Группами педагог управляет сам через бота: создаёт,
# переименовывает, удаляет (soft delete, история не теряется).

def add_group(name: str) -> int:
    """Создать группу. Возвращает id. Пустое имя — ValueError."""
    name = name.strip()
    if not name:
        raise ValueError("Пустое имя группы")
    with _db() as c:
        cur = c.execute(
            "INSERT INTO groups (name, created) VALUES (?, ?)",
            (name, _now()),
        )
        return cur.lastrowid


def list_groups(only_active: bool = True) -> list[sqlite3.Row]:
    q = "SELECT * FROM groups"
    if only_active:
        q += " WHERE active = 1"
    q += " ORDER BY name COLLATE NOCASE"
    with _db() as c:
        return c.execute(q).fetchall()


def get_group(group_id: int) -> sqlite3.Row | None:
    with _db() as c:
        return c.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()


def rename_group(group_id: int, name: str):
    name = name.strip()
    if not name:
        raise ValueError("Пустое имя группы")
    with _db() as c:
        c.execute("UPDATE groups SET name = ? WHERE id = ?", (name, group_id))


def deactivate_group(group_id: int):
    """Убрать группу из активного списка (soft delete). Связи учеников с
    группой и история посещений не трогаются."""
    with _db() as c:
        c.execute("UPDATE groups SET active = 0 WHERE id = ?", (group_id,))


def get_student_groups(student_id: int) -> list[sqlite3.Row]:
    """Активные группы ученика, отсортированные по имени."""
    with _db() as c:
        return c.execute(
            """
            SELECT g.* FROM groups g
            JOIN student_groups sg ON sg.group_id = g.id
            WHERE sg.student_id = ? AND g.active = 1
            ORDER BY g.name COLLATE NOCASE
            """,
            (student_id,),
        ).fetchall()


def set_student_groups(student_id: int, group_ids: list[int]):
    """Полностью заменить набор групп ученика новым списком (одной
    транзакцией: старые связи удаляются, новые вставляются)."""
    group_ids = list(dict.fromkeys(int(g) for g in group_ids))  # без дублей
    with _db() as c:
        c.execute("DELETE FROM student_groups WHERE student_id = ?", (student_id,))
        c.executemany(
            "INSERT INTO student_groups (student_id, group_id) VALUES (?, ?)",
            [(student_id, gid) for gid in group_ids],
        )


def list_students_by_group(group_id: int, only_active: bool = True) -> list[sqlite3.Row]:
    q = """
        SELECT s.* FROM students s
        JOIN student_groups sg ON sg.student_id = s.id
        WHERE sg.group_id = ?
    """
    params: list = [group_id]
    if only_active:
        q += " AND s.active = 1"
    q += " ORDER BY s.name COLLATE NOCASE"
    with _db() as c:
        return c.execute(q, params).fetchall()


def list_students_without_group(only_active: bool = True) -> list[sqlite3.Row]:
    """Ученики, не состоящие ни в одной группе."""
    q = """
        SELECT s.* FROM students s
        WHERE s.id NOT IN (SELECT student_id FROM student_groups)
    """
    if only_active:
        q += " AND s.active = 1"
    q += " ORDER BY s.name COLLATE NOCASE"
    with _db() as c:
        return c.execute(q).fetchall()


# ------------------------- сессии (незавершённые диалоги) -------------------------
# Раньше состояние диалогов жило только в памяти процесса: перезапустил бота
# посреди анкеты или отметки занятия — всё потерялось, а человек остался
# висеть на середине. Теперь состояние дублируется в базу и поднимается при старте.

def save_session(user_id: int, platform: str, kind: str, payload) -> None:
    with _db() as c:
        c.execute(
            "INSERT INTO sessions (user_id, platform, kind, payload, updated) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(user_id, platform, kind) DO UPDATE SET "
            "payload = excluded.payload, updated = excluded.updated",
            (user_id, platform, kind, json.dumps(payload, ensure_ascii=False), _now()),
        )


def delete_session(user_id: int, platform: str, kind: str) -> None:
    with _db() as c:
        c.execute(
            "DELETE FROM sessions WHERE user_id = ? AND platform = ? AND kind = ?",
            (user_id, platform, kind),
        )


def load_sessions(platform: str, kind: str) -> dict[int, object]:
    """Все сохранённые сессии данного вида: {user_id: payload}."""
    with _db() as c:
        rows = c.execute(
            "SELECT user_id, payload FROM sessions WHERE platform = ? AND kind = ?",
            (platform, kind),
        ).fetchall()
    out = {}
    for r in rows:
        try:
            out[r["user_id"]] = json.loads(r["payload"])
        except (ValueError, TypeError):
            continue
    return out


def purge_old_sessions(max_age_hours: int = 24) -> int:
    """Выкинуть зависшие диалоги старше N часов (человек начал анкету и
    пропал). Возвращает число удалённых. Вызывается при старте бота."""
    cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat()
    with _db() as c:
        cur = c.execute("DELETE FROM sessions WHERE updated < ?", (cutoff,))
        return cur.rowcount


# ------------------------- пауза диалога (личный вопрос педагогу) -------------------------
# Пока пауза активна, бот не отвечает этому человеку ничего — разговор ведёт
# живой педагог. Пауза всегда имеет срок: если педагог забудет вернуть бота,
# он включится сам, и человек не останется без кнопок навсегда.

def pause_dialog(user_id: int, platform: str, hours: int = 12,
                 question: str | None = None) -> None:
    until = (datetime.now() + timedelta(hours=hours)).isoformat()
    with _db() as c:
        c.execute(
            "INSERT INTO paused_dialogs (user_id, platform, since, until, question) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(user_id, platform) DO UPDATE SET "
            "until = excluded.until, question = COALESCE(excluded.question, question)",
            (user_id, platform, _now(), until, question),
        )


def is_paused(user_id: int, platform: str) -> bool:
    """Активна ли пауза. Просроченные записи удаляются на месте — отдельная
    уборка не нужна."""
    with _db() as c:
        row = c.execute(
            "SELECT until FROM paused_dialogs WHERE user_id = ? AND platform = ?",
            (user_id, platform),
        ).fetchone()
        if not row:
            return False
        if row["until"] <= _now():
            c.execute(
                "DELETE FROM paused_dialogs WHERE user_id = ? AND platform = ?",
                (user_id, platform),
            )
            return False
    return True


def resume_dialog(user_id: int, platform: str) -> None:
    with _db() as c:
        c.execute(
            "DELETE FROM paused_dialogs WHERE user_id = ? AND platform = ?",
            (user_id, platform),
        )


def list_paused(platform: str) -> list[sqlite3.Row]:
    """Активные паузы + имя ученика, если он уже есть в базе."""
    with _db() as c:
        return c.execute(
            """
            SELECT p.user_id, p.since, p.until, p.question, s.name
            FROM paused_dialogs p
            LEFT JOIN students s ON s.vk_id = p.user_id AND s.active = 1
            WHERE p.platform = ? AND p.until > ?
            ORDER BY p.since
            """,
            (platform, _now()),
        ).fetchall()


# ------------------------- статистика посещаемости -------------------------
def _period_start(days: int) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat()


def group_attendance_report(days: int = 30) -> list[dict]:
    """
    Отчёт по группам за период: сколько занятий проведено (по разным датам)
    и сколько всего отметок посещения. Плюс отдельная строка для занятий,
    отмеченных без группы (group_id IS NULL).
    """
    since = _period_start(days)
    report = []
    with _db() as c:
        for g in list_groups():
            row = c.execute(
                "SELECT COUNT(DISTINCT date(ts)) AS lessons, COUNT(*) AS visits "
                "FROM visits WHERE group_id = ? AND ts >= ?",
                (g["id"], since),
            ).fetchone()
            report.append({
                "group_id": g["id"], "name": g["name"],
                "lessons": row["lessons"], "visits": row["visits"],
            })
        row = c.execute(
            "SELECT COUNT(DISTINCT date(ts)) AS lessons, COUNT(*) AS visits "
            "FROM visits WHERE group_id IS NULL AND ts >= ?",
            (since,),
        ).fetchone()
        if row["visits"]:
            report.append({
                "group_id": None, "name": "Без группы",
                "lessons": row["lessons"], "visits": row["visits"],
            })
    return report


def student_attendance_report(days: int = 30, group_id: int | None = None) -> list[dict]:
    """
    Сколько занятий посетил каждый активный ученик за период.
    Если задан group_id — только ученики этой группы (посещения при этом
    считаются все, а не только по этой группе: человек мог ходить и в другую).
    Отсортировано по убыванию посещений — сверху самые активные, снизу те,
    кто пропадает.
    """
    since = _period_start(days)
    q = """
        SELECT s.id, s.name, s.balance,
               (SELECT COUNT(*) FROM visits v
                 WHERE v.student_id = s.id AND v.ts >= ?) AS visits
        FROM students s
        WHERE s.active = 1
    """
    params: list = [since]
    if group_id is not None:
        q += " AND s.id IN (SELECT student_id FROM student_groups WHERE group_id = ?)"
        params.append(group_id)
    q += " ORDER BY visits DESC, s.name COLLATE NOCASE"
    with _db() as c:
        rows = c.execute(q, params).fetchall()
    return [{"id": r["id"], "name": r["name"], "balance": r["balance"], "visits": r["visits"]}
            for r in rows]
