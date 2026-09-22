"""Хранилище записей (SQLite)."""
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    username    TEXT,
    client_name TEXT NOT NULL,
    phone       TEXT NOT NULL,
    service_id  TEXT NOT NULL,
    master_id   TEXT NOT NULL,
    starts_at   TEXT NOT NULL,
    ends_at     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    reminded    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS questions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    admin_msg_id  INTEGER NOT NULL,
    text          TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    answered_at   TEXT
);
"""


@dataclass
class Booking:
    id: int
    user_id: int
    username: str | None
    client_name: str
    phone: str
    service_id: str
    master_id: str
    starts_at: datetime
    ends_at: datetime


class Bookings:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    @staticmethod
    def _row(r: sqlite3.Row) -> Booking:
        return Booking(
            id=r["id"], user_id=r["user_id"], username=r["username"],
            client_name=r["client_name"], phone=r["phone"],
            service_id=r["service_id"], master_id=r["master_id"],
            starts_at=datetime.fromisoformat(r["starts_at"]),
            ends_at=datetime.fromisoformat(r["ends_at"]),
        )

    def add(self, *, user_id: int, username: str | None, client_name: str, phone: str,
            service_id: str, master_id: str, starts_at: datetime, ends_at: datetime,
            now: datetime) -> Booking:
        cur = self.conn.execute(
            "INSERT INTO bookings (user_id, username, client_name, phone, service_id,"
            " master_id, starts_at, ends_at, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, username, client_name, phone, service_id, master_id,
             starts_at.isoformat(), ends_at.isoformat(), now.isoformat()),
        )
        self.conn.commit()
        return self.get(cur.lastrowid)

    def get(self, booking_id: int) -> Booking | None:
        r = self.conn.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        return self._row(r) if r else None

    def busy(self, master_id: str, day: date) -> list[tuple[datetime, datetime]]:
        rows = self.conn.execute(
            "SELECT starts_at, ends_at FROM bookings WHERE master_id = ? AND status = 'active'"
            " AND substr(starts_at, 1, 10) = ?",
            (master_id, day.isoformat()),
        ).fetchall()
        return [(datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in rows]

    def upcoming(self, now: datetime, user_id: int | None = None,
                 until: datetime | None = None) -> list[Booking]:
        sql = "SELECT * FROM bookings WHERE status = 'active' AND starts_at >= ?"
        args: list = [now.isoformat()]
        if user_id is not None:
            sql += " AND user_id = ?"
            args.append(user_id)
        if until is not None:
            sql += " AND starts_at < ?"
            args.append(until.isoformat())
        rows = self.conn.execute(sql + " ORDER BY starts_at", args).fetchall()
        return [self._row(r) for r in rows]

    def cancel(self, booking_id: int, user_id: int) -> Booking | None:
        cur = self.conn.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE id = ? AND user_id = ? AND status = 'active'",
            (booking_id, user_id),
        )
        self.conn.commit()
        return self.get(booking_id) if cur.rowcount else None

    def due_reminders(self, now: datetime, until: datetime) -> list[Booking]:
        rows = self.conn.execute(
            "SELECT * FROM bookings WHERE status = 'active' AND reminded = 0"
            " AND starts_at >= ? AND starts_at < ?",
            (now.isoformat(), until.isoformat()),
        ).fetchall()
        return [self._row(r) for r in rows]

    def mark_reminded(self, booking_id: int) -> None:
        self.conn.execute("UPDATE bookings SET reminded = 1 WHERE id = ?", (booking_id,))
        self.conn.commit()

    # --- Вопросы администратору ---

    def add_question(self, user_id: int, admin_msg_id: int, text: str, now: datetime) -> None:
        self.conn.execute(
            "INSERT INTO questions (user_id, admin_msg_id, text, created_at) VALUES (?,?,?,?)",
            (user_id, admin_msg_id, text, now.isoformat()),
        )
        self.conn.commit()

    def question_user(self, admin_msg_id: int) -> int | None:
        """Кому из клиентов адресован ответ на это сообщение в чате администратора."""
        r = self.conn.execute(
            "SELECT user_id FROM questions WHERE admin_msg_id = ?", (admin_msg_id,)
        ).fetchone()
        return r["user_id"] if r else None

    def mark_answered(self, admin_msg_id: int, now: datetime) -> None:
        self.conn.execute(
            "UPDATE questions SET answered_at = ? WHERE admin_msg_id = ?",
            (now.isoformat(), admin_msg_id),
        )
        self.conn.commit()
