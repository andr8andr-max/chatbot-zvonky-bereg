"""Хранилище на SQLite.

Ключевые решения:
* параметризованные запросы (нет конкатенации SQL);
* уникальный частичный индекс — дубль «аккаунт + категория» невозможен на уровне БД;
* все внешние данные проходят через bot.security.clean_text до записи;
* логи пишутся без персональных данных (PII-маскирование).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .security import blind_index, clean_text, redact

log = logging.getLogger("bot.storage")

STATUS_NEW = "Новая"
STATUS_REVIEW = "На рассмотрении"
STATUS_APPROVED = "Одобрена"
STATUS_REJECTED = "Отклонена"
STATUS_NEEDS_ANSWER = "Нужен ответ"

STATUSES = (STATUS_NEW, STATUS_REVIEW, STATUS_APPROVED, STATUS_REJECTED, STATUS_NEEDS_ANSWER)
STATUS_ALIASES = {
    "новая": STATUS_NEW, "new": STATUS_NEW,
    "рассмотрении": STATUS_REVIEW, "на рассмотрении": STATUS_REVIEW, "review": STATUS_REVIEW,
    "одобрена": STATUS_APPROVED, "одобрен": STATUS_APPROVED, "approved": STATUS_APPROVED, "да": STATUS_APPROVED,
    "отклонена": STATUS_REJECTED, "отклонено": STATUS_REJECTED, "отказ": STATUS_REJECTED,
    "rejected": STATUS_REJECTED, "нет": STATUS_REJECTED,
    "нужен ответ": STATUS_NEEDS_ANSWER, "вопрос": STATUS_NEEDS_ANSWER, "уточнить": STATUS_NEEDS_ANSWER,
}

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS applications (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    number           TEXT    NOT NULL UNIQUE,
    user_id          INTEGER NOT NULL,
    category         TEXT    NOT NULL,
    full_name        TEXT    NOT NULL DEFAULT '',
    city             TEXT    NOT NULL DEFAULT '',
    phone            TEXT    NOT NULL DEFAULT '',
    phone_hash       TEXT    NOT NULL DEFAULT '',
    email            TEXT    NOT NULL DEFAULT '',
    description      TEXT    NOT NULL DEFAULT '',
    link             TEXT    NOT NULL DEFAULT '',
    attachment       TEXT    NOT NULL DEFAULT '',
    day              TEXT    NOT NULL DEFAULT '',
    status           TEXT    NOT NULL DEFAULT 'Новая',
    curator_comment  TEXT    NOT NULL DEFAULT '',
    sheet_row        INTEGER,
    notified         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);

-- Жёсткая защита от дублей: одна живая заявка с аккаунта, категория не важна.
CREATE UNIQUE INDEX IF NOT EXISTS ux_app_live
    ON applications (user_id)
    WHERE status <> 'Отклонена';

CREATE INDEX IF NOT EXISTS ix_app_number ON applications (number);
CREATE INDEX IF NOT EXISTS ix_app_user   ON applications (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS users (
    user_id      INTEGER PRIMARY KEY,
    state        TEXT    NOT NULL DEFAULT 'menu',
    step         INTEGER NOT NULL DEFAULT 0,
    data         TEXT    NOT NULL DEFAULT '{}',
    muted        INTEGER NOT NULL DEFAULT 0,
    attempts     TEXT    NOT NULL DEFAULT '{}',
    screen_name  TEXT    NOT NULL DEFAULT '',
    last_peer    INTEGER NOT NULL DEFAULT 0,
    reminded_at  TEXT    NOT NULL DEFAULT '',
    greeted_at   TEXT    NOT NULL DEFAULT '',
    last_seen_at TEXT    NOT NULL DEFAULT '',
    updated_at   TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT    PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

-- История переписки для контекста модели. Персональные данные сюда не пишутся.
CREATE TABLE IF NOT EXISTS context (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ctx_user ON context (user_id, id DESC);

CREATE TABLE IF NOT EXISTS audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    actor      TEXT    NOT NULL,
    user_id    INTEGER NOT NULL DEFAULT 0,
    action     TEXT    NOT NULL,
    payload    TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_audit_created ON audit (created_at DESC);

CREATE TABLE IF NOT EXISTS seen_events (
    event_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

-- Журнал применённых миграций данных. Нужен там, где решение «что делать с
-- уже накопленными строками» нельзя вывести из самой строки: повторный запуск
-- не должен пересматривать её заново, иначе миграция начнёт срабатывать на
-- будущих данных, для которых она не писалась.
CREATE TABLE IF NOT EXISTS data_migrations (
    name       TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- Вопросы, переданные организаторам, и ответы на них. Без этой таблицы ответ
-- администратора некуда деть: он приходит в беседу обычным сообщением, и его
-- не с чем связать, кроме как с текстом.
CREATE TABLE IF NOT EXISTS pending_questions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id           INTEGER NOT NULL,
    user_peer         INTEGER NOT NULL DEFAULT 0,
    question          TEXT    NOT NULL DEFAULT '',
    status            TEXT    NOT NULL DEFAULT 'pending',
    admin_peer        INTEGER NOT NULL DEFAULT 0,
    admin_message     INTEGER NOT NULL DEFAULT 0,
    bot_message       INTEGER NOT NULL DEFAULT 0,
    answer            TEXT    NOT NULL DEFAULT '',
    answered_by       INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL,
    answered_at       TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_pq_status ON pending_questions (status, created_at);
CREATE INDEX IF NOT EXISTS ix_pq_user    ON pending_questions (user_id, status);
CREATE INDEX IF NOT EXISTS ix_pq_admin   ON pending_questions (admin_message);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Application:
    id: int
    number: str
    user_id: int
    category: str
    full_name: str
    city: str
    phone: str
    phone_hash: str
    email: str
    description: str
    link: str
    attachment: str
    day: str
    status: str
    curator_comment: str
    sheet_row: int | None
    notified: bool
    created_at: str
    updated_at: str

    @staticmethod
    def from_row(row: sqlite3.Row) -> "Application":
        return Application(
            id=row["id"],
            number=row["number"],
            user_id=row["user_id"],
            category=row["category"],
            full_name=row["full_name"],
            city=row["city"],
            phone=row["phone"],
            phone_hash=row["phone_hash"],
            email=row["email"],
            description=row["description"],
            link=row["link"],
            attachment=row["attachment"],
            day=row["day"],
            status=row["status"],
            curator_comment=row["curator_comment"],
            sheet_row=row["sheet_row"],
            notified=bool(row["notified"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def public(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "category": self.category,
            "status": self.status,
            "city": self.city,
            "day": self.day,
            "created_at": self.created_at,
            "description": self.description,
            "link": self.link,
        }


class Store:
    """Потокобезопасная (на случай нескольких обработчиков) обёртка над SQLite."""

    def __init__(self, path: Path, pii_salt: str) -> None:
        self._path = path
        self._salt = pii_salt
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, timeout=15)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()
        self._close = False

    #: Колонки, добавленные после первой версии схемы: (таблица, колонка, DDL).
    _ADDED_COLUMNS = (
        ("users", "screen_name", "TEXT NOT NULL DEFAULT ''"),
        ("users", "last_peer", "INTEGER NOT NULL DEFAULT 0"),
        ("users", "reminded_at", "TEXT NOT NULL DEFAULT ''"),
        ("users", "greeted_at", "TEXT NOT NULL DEFAULT ''"),
        ("applications", "phone_hash", "TEXT NOT NULL DEFAULT ''"),
        ("applications", "curator_comment", "TEXT NOT NULL DEFAULT ''"),
        ("applications", "sheet_row", "INTEGER"),
        ("applications", "notified", "INTEGER NOT NULL DEFAULT 0"),
    )

    def _migrate(self) -> None:
        """Добавляет недостающие колонки в базы, созданные прошлыми версиями.

        ``CREATE TABLE IF NOT EXISTS`` не трогает существующие таблицы, поэтому
        без этого шага запрос к новой колонке падает с ``no such column``.
        """
        for table, column, ddl in self._ADDED_COLUMNS:
            existing = {
                str(r["name"])
                for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not existing:
                continue  # таблицы ещё нет — её создаст SCHEMA
            if column in existing:
                continue
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            log.info("Миграция БД: %s.%s добавлена", table, column)
        # Старые базы без phone_hash: пересчитываем хеш из сохранённого телефона.
        rows = self._conn.execute(
            "SELECT id, phone, phone_hash FROM applications"
        ).fetchall()
        for row in rows:
            if not row["phone"] or row["phone_hash"]:
                continue
            self._conn.execute(
                "UPDATE applications SET phone_hash = ? WHERE id = ?",
                (blind_index(str(row["phone"]), self._salt), row["id"]),
            )
        self._migrate_single_application()
        self._migrate_greeted_at()

    def _migrate_greeted_at(self) -> None:
        """Один раз отмечает людей, знакомых ещё до появления ``greeted_at``.

        Что означает флаг. ``greeted_at`` — это «решение о представлении принято»,
        а не «представление показано»: в новом коде ``mark_greeted`` вызывается
        до проверки исключений, и человек, сказавший на первом сообщении только
        «пока», тоже считается знакомым, чтобы представление не всплыло позже.

        Проблема. Колонка добавилась с пустым значением по умолчанию, поэтому все,
        кто был в базе до неё, выглядели новичками и получали «Я Ирина» на своё
        следующее сообщение.

        Чем этот проход НЕ является. Раньше он брал ``last_seen_at`` как дату
        знакомства, то есть утверждал, что представление было показано в тот день.
        Это догадка: ``last_seen_at`` доказывает лишь, что человека видели. Если
        когда-нибудь появится версия, где представление не показывается вовсе, а
        ``last_seen_at`` пишется как раньше, такой перенос пометил бы чужих людей
        как знакомых.

        Как теперь. Это явное решение «на текущий момент»: всех, кто в базе на
        момент появления колонки, считаем знакомыми и ставим дату самой миграции.
        Дата ничего не утверждает о прошлом, она означает «факт принят сейчас».
        Решение применяется ровно один раз и записывается в ``data_migrations``,
        поэтому новые строки эта проверка больше никогда не рассмотрит.
        """
        name = "greeted_at_for_known_users"
        if self._conn.execute(
            "SELECT 1 FROM data_migrations WHERE name = ?", (name,)
        ).fetchone():
            return
        now = utc_now()
        updated = self._conn.execute(
            "UPDATE users SET greeted_at = ? "
            "WHERE (greeted_at = '' OR greeted_at IS NULL) AND last_seen_at != ''",
            (now,),
        ).rowcount
        self._conn.execute(
            "INSERT INTO data_migrations (name, applied_at) VALUES (?, ?)", (name, now)
        )
        if updated:
            log.info(
                "Миграция БД: знакомство отмечено у %d людей, известных до "
                "появления флага (дата — момент миграции, не дата знакомства)", updated,
            )

    def _migrate_single_application(self) -> None:
        """Переводит «одна заявка на категорию» на «одна заявка с аккаунта».

        Индекс ``ux_app_live`` раньше строился по ``(user_id, category)``, и
        ``CREATE UNIQUE INDEX IF NOT EXISTS`` молча оставил бы старую версию.
        Поэтому пересоздаём его явно, а лишние живые заявки (если они остались
        с прошлой версии) переводим в «Отклонена», иначе индекс не создастся.
        """
        current = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_app_live'"
        ).fetchone()
        sql = str(current["sql"]) if current else ""
        if "category" not in sql:
            return  # индекс уже по одному user_id
        self._conn.execute("DROP INDEX IF EXISTS ux_app_live")
        duplicates = self._conn.execute(
            "SELECT user_id, COUNT(*) AS c FROM applications "
            "WHERE status <> ? GROUP BY user_id HAVING c > 1",
            (STATUS_REJECTED,),
        ).fetchall()
        for row in duplicates:
            # Оставляем самую свежую заявку, прочим меняем статус, чтобы
            # они перестали считаться живыми и не потерялись из базы.
            self._conn.execute(
                "UPDATE applications SET status = ?, curator_comment = ? "
                "WHERE user_id = ? AND status <> ? AND id NOT IN ("
                "  SELECT id FROM applications WHERE user_id = ? "
                "  AND status <> ? ORDER BY id DESC LIMIT 1)",
                (
                    STATUS_REJECTED,
                    "Дубль: заявка с аккаунта принимается один раз",
                    row["user_id"], STATUS_REJECTED, row["user_id"], STATUS_REJECTED,
                ),
            )
            log.info(
                "Миграция БД: у user_id=%s лишние живые заявки помечены отклонёнными",
                row["user_id"],
            )
        # В частичном индексе параметры запрещены — значение подставляем текстом.
        rejected = STATUS_REJECTED.replace("'", "''")
        self._conn.execute(
            "CREATE UNIQUE INDEX ux_app_live ON applications (user_id) "
            f"WHERE status <> '{rejected}'"
        )
        log.info("Миграция БД: ux_app_live переведён на одну заявку с аккаунта")

    # ------------------------------------------------------------------ #
    def close(self) -> None:
        with self._lock:
            if not self._close:
                self._conn.commit()
                self._conn.close()
                self._close = True

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def _commit(self) -> None:
        with self._lock:
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # Заявки
    # ------------------------------------------------------------------ #

    def next_number(self) -> str:
        """Атомарный счётчик ЗБ-0001 (без-зависимости от поддержки RETURNING)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT value FROM counters WHERE name = 'application'"
                ).fetchone()
                value = (row["value"] if row else 0) + 1
                self._conn.execute(
                    "INSERT INTO counters (name, value) VALUES ('application', ?) "
                    "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                    (value,),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return f"ЗБ-{value:04d}"

    def find_active_any(self, user_id: int) -> Application | None:
        """Любая не отклонённая заявка аккаунта, независимо от категории.

        Заявка с аккаунта принимается только один раз, поэтому дубли ищем
        по пользователю, а не по паре «пользователь + категория».
        """
        row = self._exec(
            "SELECT * FROM applications WHERE user_id = ? AND status <> ? "
            "ORDER BY id DESC LIMIT 1",
            (user_id, STATUS_REJECTED),
        ).fetchone()
        return Application.from_row(row) if row else None

    def find_last_any(self, user_id: int) -> Application | None:
        """Последняя заявка аккаунта в любом статусе, включая отклонённую."""
        row = self._exec(
            "SELECT * FROM applications WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return Application.from_row(row) if row else None

    def list_for_user(self, user_id: int) -> list[Application]:
        rows = self._exec(
            "SELECT * FROM applications WHERE user_id = ? ORDER BY id DESC LIMIT 20",
            (user_id,),
        ).fetchall()
        return [Application.from_row(r) for r in rows]

    def get_by_number(self, number: str) -> Application | None:
        row = self._exec(
            "SELECT * FROM applications WHERE number = ?", (clean_text(number, 20),)
        ).fetchone()
        return Application.from_row(row) if row else None

    def last_number(self) -> str:
        """Номер, который получит следующая заявка."""
        row = self._conn.execute(
            "SELECT value FROM counters WHERE name = 'application'"
        ).fetchone()
        return f"ЗБ-{int(row['value'] if row else 0) + 1:04d}"

    def _renumber_after_delete(self) -> None:
        """Прижимает счётчик к последнему занятому номеру.

        Счётчик растёт только вверх, поэтому удаление заявки оставляло в
        нумерации дыру: следующая получалась ЗБ-0002, хотя ЗБ-0001 уже не
        существует. Организатор считает заявки глазами по номеру, и пропуск
        выглядит как потерянная заявка. Поэтому после удаления счётчик
        опускается до максимального оставшегося номера.
        """
        row = self._conn.execute(
            "SELECT MAX(CAST(SUBSTR(number, 4) AS INTEGER)) AS m FROM applications "
            "WHERE number LIKE 'ЗБ-%'"
        ).fetchone()
        top = int(row["m"] or 0)
        self._conn.execute(
            "INSERT INTO counters (name, value) VALUES ('application', ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (top,),
        )
        self._conn.commit()

    def delete_application(self, number: str) -> bool:
        """Удаляет заявку и сдвигает нумерацию, чтобы не осталось пропуска.

        Нужна, чтобы убрать заявку, созданную при проверке: иначе она навсегда
        занимает номер и следующая настоящая заявка получит его плюс один.
        Возвращает False, если такой заявки нет.
        """
        clean = clean_text(number, 20)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                cur = self._conn.execute(
                    "DELETE FROM applications WHERE number = ?", (clean,)
                )
                self._renumber_after_delete()
            except Exception:
                self._conn.rollback()
                raise
        return cur.rowcount > 0

    def find_by_phone_hash(self, phone_hash: str) -> list[Application]:
        """Поиск дубля по телефону: у разных аккаунтов с одним номером."""
        if not phone_hash:
            return []
        rows = self._exec(
            "SELECT * FROM applications WHERE phone_hash = ? AND status <> ? ORDER BY id DESC LIMIT 5",
            (phone_hash, STATUS_REJECTED),
        ).fetchall()
        return [Application.from_row(r) for r in rows]

    def create_application(self, payload: dict[str, Any], *, store_pii: bool) -> Application:
        now = utc_now()
        number = self.next_number()
        phone = clean_text(payload.get("phone", ""), 40)
        phone_hash = blind_index(phone, self._salt) if phone else ""
        category = clean_text(payload.get("category", ""), 40)
        with self._lock:
            try:
                cur = self._conn.execute(
                    """
                    INSERT INTO applications
                        (number, user_id, category, full_name, city, phone, phone_hash, email,
                         description, link, attachment, day, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        number,
                        int(payload["user_id"]),
                        category,
                        clean_text(payload.get("full_name", ""), 120),
                        clean_text(payload.get("city", ""), 80),
                        # Хеш нужен всегда: он необратим и служит только для
                        # поиска дублей. Чистые значения — по настройке.
                        phone if store_pii else "",
                        phone_hash,
                        clean_text(payload.get("email", ""), 120) if store_pii else "",
                        clean_text(payload.get("description", ""), 1500),
                        clean_text(payload.get("link", ""), 512),
                        clean_text(payload.get("attachment", ""), 300),
                        clean_text(payload.get("day", ""), 20),
                        STATUS_NEW,
                        now,
                        now,
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                existing = self.find_active_any(int(payload["user_id"]))
                raise DuplicateApplication(existing.number if existing else "") from exc
        app = self.get_by_number(number)
        assert app is not None
        return app

    def set_status(self, number: str, status: str, comment: str = "") -> Application | None:
        self._exec(
            "UPDATE applications SET status = ?, curator_comment = ?, updated_at = ? WHERE number = ?",
            (status, clean_text(comment, 500), utc_now(), clean_text(number, 20)),
        )
        self._commit()
        return self.get_by_number(number)

    def mark_notified(self, number: str) -> None:
        self._exec("UPDATE applications SET notified = 1 WHERE number = ?", (clean_text(number, 20),))
        self._commit()

    def pending_sheet_sync(self) -> list[Application]:
        rows = self._exec("SELECT * FROM applications WHERE notified = 0 ORDER BY id ASC LIMIT 50").fetchall()
        return [Application.from_row(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Состояние пользователя
    # ------------------------------------------------------------------ #

    def get_state(self, user_id: int) -> tuple[str, int, dict[str, Any], bool, dict[str, int]]:
        row = self._exec(
            "SELECT state, step, data, muted, attempts, last_seen_at FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return "menu", 0, {}, False, {}
        try:
            data = json.loads(row["data"])
        except (ValueError, TypeError):
            data = {}
        try:
            attempts = json.loads(row["attempts"])
        except (ValueError, TypeError):
            attempts = {}
        return row["state"], row["step"], data, bool(row["muted"]), attempts

    def set_state(
        self,
        user_id: int,
        state: str,
        step: int = 0,
        data: dict[str, Any] | None = None,
        muted: bool | None = None,
        attempts: dict[str, int] | None = None,
    ) -> None:
        now = utc_now()
        self._exec(
            """
            INSERT INTO users (user_id, state, step, data, muted, attempts, last_seen_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                state = excluded.state, step = excluded.step, data = excluded.data,
                muted = COALESCE(?, muted), attempts = COALESCE(?, attempts),
                last_seen_at = excluded.last_seen_at, updated_at = excluded.updated_at
            """,
            (
                user_id,
                state,
                step,
                json.dumps(data or {}, ensure_ascii=False),
                1 if muted else 0,
                json.dumps(attempts or {}, ensure_ascii=False),
                now,
                now,
                None if muted is None else (1 if muted else 0),
                None if attempts is None else json.dumps(attempts, ensure_ascii=False),
            ),
        )
        self._commit()

    def touch(self, user_id: int, peer_id: int = 0) -> None:
        now = utc_now()
        self._exec(
            "UPDATE users SET last_seen_at = ?, updated_at = ?, "
            "last_peer = CASE WHEN ? > 0 THEN ? ELSE last_peer END WHERE user_id = ?",
            (now, now, int(peer_id), int(peer_id), user_id),
        )
        self._exec(
            "INSERT OR IGNORE INTO users (user_id, last_seen_at, updated_at, last_peer) "
            "VALUES (?, ?, ?, ?)",
            (user_id, now, now, int(peer_id)),
        )
        self._commit()

    def last_seen(self, user_id: int) -> str:
        row = self._exec("SELECT last_seen_at FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return row["last_seen_at"] if row else ""

    def last_peer(self, user_id: int) -> int:
        row = self._exec("SELECT last_peer FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return int(row["last_peer"]) if row else 0

    def needs_idle_reminder(self, user_id: int, minutes: int) -> bool:
        """True, если пора напомнить о брошенной анкете (не чаще раза в 15 мин)."""
        row = self._exec(
            "SELECT last_seen_at, reminded_at FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return False
        now = utc_now()
        if row["reminded_at"]:
            try:
                if (datetime.fromisoformat(now) - datetime.fromisoformat(row["reminded_at"])).total_seconds() < 900:
                    return False
            except ValueError:
                pass
        if not row["last_seen_at"]:
            return False
        try:
            delta = (datetime.fromisoformat(now) - datetime.fromisoformat(row["last_seen_at"])).total_seconds()
        except ValueError:
            return False
        return delta >= minutes * 60

    def mark_reminded(self, user_id: int) -> None:
        self._exec("UPDATE users SET reminded_at = ? WHERE user_id = ?", (utc_now(), user_id))
        self._commit()

    def greeted_at(self, user_id: int) -> str:
        """Когда пользователю уже показывали представление.

        Пустая строка — значит знакомства ещё не было. Отдельная от ``state``
        колонка нужна, потому что состояние меняется постоянно, а факт
        знакомства должен оставаться навсегда.
        """
        row = self._exec(
            "SELECT greeted_at FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return str(row["greeted_at"]) if row else ""

    def mark_greeted(self, user_id: int) -> None:
        self._exec(
            "UPDATE users SET greeted_at = ? WHERE user_id = ?", (utc_now(), user_id)
        )
        self._commit()

    # ------------------------------------------------------------------ #
    # Вопросы организатору: передача, ответ, отмена
    # ------------------------------------------------------------------ #
    def create_question(self, user_id: int, user_peer: int, question: str) -> int:
        """Регистрирует переданный вопрос и возвращает его номер."""
        cur = self._exec(
            "INSERT INTO pending_questions (user_id, user_peer, question, created_at) "
            "VALUES (?, ?, ?, ?)",
            (int(user_id), int(user_peer), question, utc_now()),
        )
        self._commit()
        return int(cur.lastrowid or 0)

    def attach_admin_message(
        self, question_id: int, admin_peer: int, admin_message: int
    ) -> None:
        """Запоминает, в какое сообщение беседы ушёл вопрос.

        В беседе номер может не прийти: на ``messages.send`` VK то отдаёт
        вместо номера сообщения ноль, то нормальный id (в беседе 2000000001
        наблюдались успешные отправки с непустым номером). Поэтому
        сопоставление ответа администратора идёт по метке ``[вопрос N]`` в
        тексте, а не только по номеру: на номера полагаться нельзя.
        Поле остаётся: в личной переписке номер есть, и он там пригодится.
        """
        if question_id <= 0 or admin_message <= 0:
            return
        self._exec(
            "UPDATE pending_questions SET admin_peer = ?, admin_message = ? WHERE id = ?",
            (int(admin_peer), int(admin_message), int(question_id)),
        )
        self._commit()

    def attach_bot_message(self, question_id: int, bot_message: int) -> None:
        """Запоминает сообщение бота с кнопкой «Отменить вопрос».

        Нужен, чтобы снять кнопку, когда организатор ответит: номера сообщения
        в переписке с человеком иначе негде взять.
        """
        if question_id <= 0 or bot_message <= 0:
            return
        self._exec(
            "UPDATE pending_questions SET bot_message = ? WHERE id = ?",
            (int(bot_message), int(question_id)),
        )
        self._commit()

    def pending_for_user(self, user_id: int) -> sqlite3.Row | None:
        """Последний неотвеченный вопрос человека."""
        return self._exec(
            "SELECT * FROM pending_questions WHERE user_id = ? AND status = 'pending' "
            "ORDER BY id DESC LIMIT 1",
            (int(user_id),),
        ).fetchone()

    def question_by_id(self, question_id: int, *, only_pending: bool = True) -> sqlite3.Row | None:
        """Вопрос по номеру — им администратор отвечает командой «!ответ N»."""
        sql = "SELECT * FROM pending_questions WHERE id = ?"
        if only_pending:
            sql += " AND status = 'pending'"
        return self._exec(sql, (int(question_id),)).fetchone()

    def question_by_admin_message(
        self, admin_message: int, *, only_pending: bool = True
    ) -> sqlite3.Row | None:
        """Вопрос, на сообщение которого в беседе ответили."""
        if not admin_message:
            return None
        sql = "SELECT * FROM pending_questions WHERE admin_message = ?"
        if only_pending:
            sql += " AND status = 'pending'"
        return self._exec(sql, (int(admin_message),)).fetchone()

    def pending_questions(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._exec(
            "SELECT * FROM pending_questions WHERE status = 'pending' "
            "ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()

    def mark_answered(self, question_id: int, answer: str, admin_id: int) -> None:
        self._exec(
            "UPDATE pending_questions SET status = 'answered', answer = ?, answered_by = ?, "
            "answered_at = ? WHERE id = ?",
            (answer, int(admin_id), utc_now(), int(question_id)),
        )
        self._commit()

    def mark_cancelled(self, question_id: int) -> None:
        self._exec(
            "UPDATE pending_questions SET status = 'cancelled', answered_at = ? WHERE id = ?",
            (utc_now(), int(question_id)),
        )
        self._commit()

    def idle_candidates(self, minutes: int, states: set[str], limit: int = 20) -> list[sqlite3.Row]:
        """Пользователи, которые бросили анкету."""
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        rows = self._exec(
            f"SELECT user_id, state, step, last_peer, last_seen_at, reminded_at FROM users "
            f"WHERE state IN ({placeholders}) AND last_peer > 0",
            tuple(states),
        ).fetchall()
        now = datetime.now(timezone.utc)
        out: list[sqlite3.Row] = []
        for row in rows:
            try:
                seen = datetime.fromisoformat(row["last_seen_at"])
            except (ValueError, TypeError):
                continue
            if (now - seen).total_seconds() < minutes * 60:
                continue
            if row["reminded_at"]:
                try:
                    if (now - datetime.fromisoformat(row["reminded_at"])).total_seconds() < 900:
                        continue
                except (ValueError, TypeError):
                    pass
            out.append(row)
            if len(out) >= limit:
                break
        return out

    def set_screen_name(self, user_id: int, name: str) -> None:
        clean = clean_text(name, 120)
        if not clean:
            return
        self._exec("UPDATE users SET screen_name = ? WHERE user_id = ?", (clean, user_id))
        self._exec(
            "INSERT OR IGNORE INTO users (user_id, screen_name, updated_at) VALUES (?, ?, ?)",
            (user_id, clean, utc_now()),
        )
        self._commit()

    def get_screen_name(self, user_id: int) -> str:
        row = self._exec("SELECT screen_name FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return str(row["screen_name"]) if row and row["screen_name"] else ""

    def set_sheet_row(self, number: str, row: int) -> None:
        self._exec("UPDATE applications SET sheet_row = ? WHERE number = ?", (int(row), clean_text(number, 20)))
        self._commit()

    # ------------------------------------------------------------------ #
    # Контекст для модели
    # ------------------------------------------------------------------ #

    def add_context(self, user_id: int, role: str, content: str, limit: int) -> None:
        text = clean_text(content, 1200)
        if not text:
            return
        self._exec(
            "INSERT INTO context (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (user_id, role, text, utc_now()),
        )
        self._exec(
            "DELETE FROM context WHERE user_id = ? AND id NOT IN "
            "(SELECT id FROM context WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
            (user_id, user_id, max(0, limit)),
        )
        self._commit()

    def get_context(self, user_id: int, limit: int) -> list[dict[str, str]]:
        rows = self._exec(
            "SELECT role, content FROM context WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def clear_context(self, user_id: int) -> None:
        self._exec("DELETE FROM context WHERE user_id = ?", (user_id,))
        self._commit()

    # ------------------------------------------------------------------ #
    # Служебное
    # ------------------------------------------------------------------ #

    def mark_seen(self, event_id: str) -> bool:
        """True, если событие уже было (дубль, его не обрабатываем)."""
        clean = clean_text(event_id, 80)
        if not clean:
            return False
        self._exec("INSERT OR IGNORE INTO seen_events (event_id, created_at) VALUES (?, ?)", (clean, utc_now()))
        self._commit()
        cur = self._exec("SELECT changes() AS n")
        # changes() = 1, если строка реально вставлена (событие новое),
        # и 0, если INSERT OR IGNORE её пропустил (событие уже видели).
        inserted = int(cur.fetchone()["n"])
        return not inserted

    def audit(self, actor: str, action: str, user_id: int = 0, payload: str = "") -> None:
        self._exec(
            "INSERT INTO audit (actor, user_id, action, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (actor, user_id, action, clean_text(payload, 500), utc_now()),
        )
        self._commit()

    def stats(self) -> dict[str, int]:
        q = lambda sql: int(self._exec(sql).fetchone()["n"])  # noqa: E731
        return {
            "applications": q("SELECT COUNT(*) AS n FROM applications"),
            "new": q(f"SELECT COUNT(*) AS n FROM applications WHERE status = '{STATUS_NEW}'"),
            "approved": q(f"SELECT COUNT(*) AS n FROM applications WHERE status = '{STATUS_APPROVED}'"),
            "rejected": q(f"SELECT COUNT(*) AS n FROM applications WHERE status = '{STATUS_REJECTED}'"),
            "users": q("SELECT COUNT(*) AS n FROM users"),
        }

    def log_line(self, message: str) -> None:
        print(f"{utc_now()}  {redact(message, mask_pii=True)}", flush=True)


class DuplicateApplication(RuntimeError):
    """Попытка создать вторую житую заявку в одной категории."""

    def __init__(self, existing_number: str) -> None:
        super().__init__(existing_number)
        self.existing_number = existing_number


def normalize_status(raw: str) -> str | None:
    key = clean_text(raw, 40).lower().strip(" .!:—-")
    return STATUS_ALIASES.get(key)
