"""Хранилище на SQLite: участники, журнал задач, вес, режимы дней."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .scheduler import Instance, State

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    member_id     TEXT PRIMARY KEY,
    chat_id       INTEGER NOT NULL UNIQUE,
    username      TEXT,
    registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instances (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    day           TEXT NOT NULL,
    due_at        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    assignee      TEXT NOT NULL,
    sent_count    INTEGER NOT NULL DEFAULT 0,
    last_sent_at  TEXT,
    snoozed_until TEXT,
    escalated_at  TEXT,
    done_at       TEXT,
    done_by       TEXT,
    followup_sent_at TEXT,
    leftovers     TEXT
);
CREATE INDEX IF NOT EXISTS instances_day ON instances(day);
CREATE TABLE IF NOT EXISTS messages (
    instance_id TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    PRIMARY KEY (instance_id, chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS weights (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    cat_id  TEXT NOT NULL,
    day     TEXT NOT NULL,
    kg      REAL NOT NULL,
    by      TEXT
);
CREATE TABLE IF NOT EXISTS day_modes (
    day  TEXT PRIMARY KEY,
    mode TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass(frozen=True)
class Record:
    id: str
    task_id: str
    day: date
    due_at: datetime
    status: str
    assignee: str
    sent_count: int
    last_sent_at: datetime | None
    snoozed_until: datetime | None
    escalated_at: datetime | None
    done_at: datetime | None
    done_by: str | None
    followup_sent_at: datetime | None = None
    leftovers: str | None = None

    def state(self) -> State:
        return State(
            status=self.status,
            assignee=self.assignee,
            sent_count=self.sent_count,
            last_sent_at=self.last_sent_at,
            snoozed_until=self.snoozed_until,
            escalated_at=self.escalated_at,
            done_at=self.done_at,
            followup_sent_at=self.followup_sent_at,
        )


def _record(row: sqlite3.Row) -> Record:
    return Record(
        id=row["id"],
        task_id=row["task_id"],
        day=date.fromisoformat(row["day"]),
        due_at=datetime.fromisoformat(row["due_at"]),
        status=row["status"],
        assignee=row["assignee"],
        sent_count=row["sent_count"],
        last_sent_at=_dt(row["last_sent_at"]),
        snoozed_until=_dt(row["snoozed_until"]),
        escalated_at=_dt(row["escalated_at"]),
        done_at=_dt(row["done_at"]),
        done_by=row["done_by"],
        followup_sent_at=_dt(row["followup_sent_at"]),
        leftovers=row["leftovers"],
    )


class Store:
    def __init__(self, path: str | Path):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # --- участники ---

    def bind_member(self, member_id: str, chat_id: int, username: str | None, now: datetime) -> str | None:
        """Привязывает чат к участнику. Возвращает id участника, ранее занимавшего этот чат."""
        with self.db:
            prev = self.db.execute("SELECT member_id FROM members WHERE chat_id = ?", (chat_id,)).fetchone()
            self.db.execute("DELETE FROM members WHERE chat_id = ? OR member_id = ?", (chat_id, member_id))
            self.db.execute(
                "INSERT INTO members(member_id, chat_id, username, registered_at) VALUES (?, ?, ?, ?)",
                (member_id, chat_id, username, now.isoformat()),
            )
        return prev["member_id"] if prev and prev["member_id"] != member_id else None

    def unbind_chat(self, chat_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM members WHERE chat_id = ?", (chat_id,))

    def member_by_chat(self, chat_id: int) -> str | None:
        row = self.db.execute("SELECT member_id FROM members WHERE chat_id = ?", (chat_id,)).fetchone()
        return row["member_id"] if row else None

    def chat_of(self, member_id: str) -> int | None:
        row = self.db.execute("SELECT chat_id FROM members WHERE member_id = ?", (member_id,)).fetchone()
        return row["chat_id"] if row else None

    def registered(self) -> dict[str, int]:
        return {r["member_id"]: r["chat_id"] for r in self.db.execute("SELECT member_id, chat_id FROM members")}

    # --- задачи ---

    def ensure_instances(self, instances: list[Instance]) -> None:
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO instances(id, task_id, day, due_at, assignee) VALUES (?, ?, ?, ?, ?)",
                [(i.id, i.task.id, i.day.isoformat(), i.due.isoformat(), i.task.primary) for i in instances],
            )

    def sync_due(self, instances: list[Instance]) -> None:
        """Обновляет время ещё не начатых задач (если поменяли режим дня или конфиг)."""
        with self.db:
            self.db.executemany(
                "UPDATE instances SET due_at = ? WHERE id = ? AND status = 'pending' AND sent_count = 0",
                [(i.due.isoformat(), i.id) for i in instances],
            )

    def drop_unplanned(self, day: date, keep_ids: set[str]) -> None:
        """Убирает с этого дня неначатые задачи, которых больше нет в плане (сменили режим дня)."""
        rows = self.db.execute(
            "SELECT id FROM instances WHERE day = ? AND status = 'pending' AND sent_count = 0", (day.isoformat(),)
        ).fetchall()
        stale = [r["id"] for r in rows if r["id"] not in keep_ids]
        if stale:
            with self.db:
                self.db.executemany("DELETE FROM instances WHERE id = ?", [(s,) for s in stale])

    def get(self, instance_id: str) -> Record | None:
        row = self.db.execute("SELECT * FROM instances WHERE id = ?", (instance_id,)).fetchone()
        return _record(row) if row else None

    def for_day(self, day: date) -> list[Record]:
        rows = self.db.execute("SELECT * FROM instances WHERE day = ? ORDER BY due_at, task_id", (day.isoformat(),))
        return [_record(r) for r in rows]

    def pending_before(self, day: date) -> list[Record]:
        rows = self.db.execute("SELECT * FROM instances WHERE day < ? AND status = 'pending'", (day.isoformat(),))
        return [_record(r) for r in rows]

    def mark_sent(self, instance_id: str, now: datetime) -> None:
        with self.db:
            self.db.execute(
                "UPDATE instances SET sent_count = sent_count + 1, last_sent_at = ?, snoozed_until = NULL WHERE id = ?",
                (now.isoformat(), instance_id),
            )

    def mark_escalated(self, instance_id: str, now: datetime) -> None:
        with self.db:
            self.db.execute("UPDATE instances SET escalated_at = ? WHERE id = ?", (now.isoformat(), instance_id))

    def snooze(self, instance_id: str, until: datetime) -> None:
        with self.db:
            self.db.execute(
                "UPDATE instances SET snoozed_until = ? WHERE id = ? AND status = 'pending'",
                (until.isoformat(), instance_id),
            )

    def reassign(self, instance_id: str, member_id: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE instances SET assignee = ?, sent_count = 0, last_sent_at = NULL, snoozed_until = NULL "
                "WHERE id = ? AND status = 'pending'",
                (member_id, instance_id),
            )

    def mark_done(self, instance_id: str, member_id: str, now: datetime) -> bool:
        """Отмечает выполнение. False, если задача уже была выполнена кем-то раньше."""
        with self.db:
            cur = self.db.execute(
                "UPDATE instances SET status = 'done', done_at = ?, done_by = ? WHERE id = ? AND status != 'done'",
                (now.isoformat(), member_id, instance_id),
            )
        return cur.rowcount == 1

    def mark_followup(self, instance_id: str, now: datetime) -> None:
        with self.db:
            self.db.execute("UPDATE instances SET followup_sent_at = ? WHERE id = ?", (now.isoformat(), instance_id))

    def set_leftovers(self, instance_id: str, value: str) -> None:
        with self.db:
            self.db.execute("UPDATE instances SET leftovers = ? WHERE id = ?", (value, instance_id))

    def previous_followup(self, instance_id: str, task_ids: set[str]) -> Record | None:
        """Предыдущее по времени кормление с вопросом об остатках (для проверки «два раза подряд»)."""
        rec = self.get(instance_id)
        if rec is None or not task_ids:
            return None
        marks = ",".join("?" * len(task_ids))
        row = self.db.execute(
            f"SELECT * FROM instances WHERE status = 'done' AND task_id IN ({marks}) AND due_at < ? "
            "ORDER BY due_at DESC LIMIT 1",
            (*sorted(task_ids), rec.due_at.isoformat()),
        ).fetchone()
        return _record(row) if row else None

    def mark_missed(self, instance_id: str) -> bool:
        with self.db:
            cur = self.db.execute(
                "UPDATE instances SET status = 'missed' WHERE id = ? AND status = 'pending'", (instance_id,)
            )
        return cur.rowcount == 1

    def add_message(self, instance_id: str, chat_id: int, message_id: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO messages(instance_id, chat_id, message_id) VALUES (?, ?, ?)",
                (instance_id, chat_id, message_id),
            )

    def messages_of(self, instance_id: str) -> list[tuple[int, int]]:
        rows = self.db.execute("SELECT chat_id, message_id FROM messages WHERE instance_id = ?", (instance_id,))
        return [(r["chat_id"], r["message_id"]) for r in rows]

    def done_count(self, member_id: str, since: date) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM instances WHERE done_by = ? AND status = 'done' AND day >= ?",
            (member_id, since.isoformat()),
        ).fetchone()
        return row["n"]

    def missed_count(self, since: date) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM instances WHERE status = 'missed' AND day >= ?", (since.isoformat(),)
        ).fetchone()
        return row["n"]

    # --- вес ---

    def add_weight(self, cat_id: str, day: date, kg: float, by: str | None) -> None:
        with self.db:
            self.db.execute("INSERT INTO weights(cat_id, day, kg, by) VALUES (?, ?, ?, ?)", (cat_id, day.isoformat(), kg, by))

    def weights(self, cat_id: str, limit: int = 10) -> list[tuple[date, float]]:
        rows = self.db.execute(
            "SELECT day, kg FROM weights WHERE cat_id = ? ORDER BY day DESC, id DESC LIMIT ?", (cat_id, limit)
        )
        return [(date.fromisoformat(r["day"]), r["kg"]) for r in rows]

    def latest_weight(self, cat_id: str) -> float | None:
        w = self.weights(cat_id, 1)
        return w[0][1] if w else None

    # --- режимы дней и настройки ---

    def set_day_mode(self, day: date, mode: str | None) -> None:
        with self.db:
            if mode is None:
                self.db.execute("DELETE FROM day_modes WHERE day = ?", (day.isoformat(),))
            else:
                self.db.execute(
                    "INSERT INTO day_modes(day, mode) VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET mode = excluded.mode",
                    (day.isoformat(), mode),
                )

    def day_mode(self, day: date) -> str | None:
        row = self.db.execute("SELECT mode FROM day_modes WHERE day = ?", (day.isoformat(),)).fetchone()
        return row["mode"] if row else None

    def get_kv(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_kv(self, key: str, value: str | None) -> None:
        with self.db:
            if value is None:
                self.db.execute("DELETE FROM kv WHERE key = ?", (key,))
            else:
                self.db.execute(
                    "INSERT INTO kv(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
