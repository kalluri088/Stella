"""Trusted, one-shot reminder storage built on the proactivity foundation.

A reminder is an event source, never an authority source. Anything stored
here can only cause a bounded INFORM/ASK/DO_NOTHING proactivity outcome; it
cannot execute tools, modify files, or grant permission to anything else.
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Self

MAX_REMINDER_CONTENT_CHARS = 512


class ReminderStatus(str, Enum):
    """The complete reminder lifecycle. HANDLED and CANCELLED are terminal."""

    PENDING = "pending"
    HANDLED = "handled"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class Reminder:
    """One persisted one-shot reminder with a safe identifying record."""

    id: int
    content: str
    due_at: datetime
    status: ReminderStatus
    created_at: datetime

    def __post_init__(self) -> None:
        if self.id is None or not isinstance(self.id, int) or self.id < 1:
            raise ValueError("reminder id must be a positive integer")
        if not self.content.strip():
            raise ValueError("reminder content must not be empty")
        if not isinstance(self.due_at, datetime) or self.due_at.tzinfo is None:
            raise ValueError("reminder due_at must be a timezone-aware datetime")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("reminder created_at must be a timezone-aware datetime")
        if not isinstance(self.status, ReminderStatus):
            raise TypeError("status must be a ReminderStatus")


def reminder_validation_error(
    content: object,
    due_at: object,
    now: datetime,
) -> str | None:
    """Return why this reminder cannot be created, or None when valid.

    Already-expired due times are rejected deterministically: a one-shot
    reminder whose moment has already passed would otherwise fire instantly
    on the next check without ever being a real scheduled notification.
    """

    if not isinstance(content, str) or not content.strip():
        return "Reminder content must be a non-empty string."
    if len(content) > MAX_REMINDER_CONTENT_CHARS:
        return (
            "Reminder content is too long "
            f"(maximum {MAX_REMINDER_CONTENT_CHARS} characters)."
        )
    if not isinstance(due_at, datetime):
        return "Reminder due time must be a datetime."
    if due_at.tzinfo is None:
        return "Reminder due time must include a timezone offset."
    if due_at <= now:
        return "Reminder due time must be in the future."
    return None


class ReminderStore(ABC):
    """Application-owned persistence for one-shot reminders."""

    @abstractmethod
    def create(self, content: str, due_at: datetime, now: datetime) -> Reminder | None:
        """Store one validated reminder, or return None when invalid."""

    @abstractmethod
    def pending(self) -> tuple[Reminder, ...]:
        """Return all not-yet-fired, not-yet-cancelled reminders."""

    @abstractmethod
    def due(self, now: datetime) -> tuple[Reminder, ...]:
        """Return pending reminders whose due time has arrived."""

    @abstractmethod
    def mark_handled(self, reminder_id: int) -> bool:
        """Move one pending reminder to the terminal HANDLED state."""

    @abstractmethod
    def cancel(self, reminder_id: int) -> bool:
        """Move one pending reminder to the terminal CANCELLED state."""

    def close(self) -> None:
        """Release any backing resources. Stores without any may ignore."""


def _valid_lifecycle_id(reminder_id: object) -> bool:
    return (
        isinstance(reminder_id, int)
        and not isinstance(reminder_id, bool)
        and reminder_id >= 1
    )


class InMemoryReminderStore(ReminderStore):
    """Deterministic in-process reminder store used by tests and previews."""

    def __init__(self) -> None:
        self._reminders: list[Reminder] = []
        self._next_id = 1

    def create(self, content: str, due_at: datetime, now: datetime) -> Reminder | None:
        if reminder_validation_error(content, due_at, now) is not None:
            return None
        reminder = Reminder(
            id=self._next_id,
            content=content.strip(),
            due_at=due_at,
            status=ReminderStatus.PENDING,
            created_at=now,
        )
        self._next_id += 1
        self._reminders.append(reminder)
        return reminder

    def pending(self) -> tuple[Reminder, ...]:
        return tuple(
            reminder
            for reminder in self._reminders
            if reminder.status is ReminderStatus.PENDING
        )

    def due(self, now: datetime) -> tuple[Reminder, ...]:
        return tuple(
            reminder
            for reminder in self.pending()
            if reminder.due_at <= now
        )

    def _transition(
        self, reminder_id: int, target: ReminderStatus
    ) -> bool:
        if not _valid_lifecycle_id(reminder_id):
            return False
        for index, reminder in enumerate(self._reminders):
            if reminder.id == reminder_id:
                if reminder.status is not ReminderStatus.PENDING:
                    return False
                self._reminders[index] = replace(reminder, status=target)
                return True
        return False

    def mark_handled(self, reminder_id: int) -> bool:
        return self._transition(reminder_id, ReminderStatus.HANDLED)

    def cancel(self, reminder_id: int) -> bool:
        return self._transition(reminder_id, ReminderStatus.CANCELLED)


class SQLiteReminderStore(ReminderStore):
    """Persistent reminder store that survives process restarts."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self._connection = sqlite3.connect(self.database_path)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                due_at TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._connection.commit()

    @staticmethod
    def _row_to_reminder(row: tuple) -> Reminder:
        return Reminder(
            id=row[0],
            content=row[1],
            due_at=datetime.fromisoformat(row[2]),
            status=ReminderStatus(row[3]),
            created_at=datetime.fromisoformat(row[4]),
        )

    def create(self, content: str, due_at: datetime, now: datetime) -> Reminder | None:
        if reminder_validation_error(content, due_at, now) is not None:
            return None
        cursor = self._connection.execute(
            "INSERT INTO reminders (content, due_at, status, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                content.strip(),
                due_at.isoformat(),
                ReminderStatus.PENDING.value,
                now.isoformat(),
            ),
        )
        self._connection.commit()
        return Reminder(
            id=int(cursor.lastrowid),
            content=content.strip(),
            due_at=due_at,
            status=ReminderStatus.PENDING,
            created_at=now,
        )

    def pending(self) -> tuple[Reminder, ...]:
        rows = self._connection.execute(
            "SELECT id, content, due_at, status, created_at FROM reminders "
            "WHERE status = ? ORDER BY due_at, id",
            (ReminderStatus.PENDING.value,),
        )
        return tuple(self._row_to_reminder(row) for row in rows)

    def due(self, now: datetime) -> tuple[Reminder, ...]:
        return tuple(
            reminder for reminder in self.pending() if reminder.due_at <= now
        )

    def _transition(self, reminder_id: object, target: ReminderStatus) -> bool:
        if not _valid_lifecycle_id(reminder_id):
            return False
        cursor = self._connection.execute(
            "UPDATE reminders SET status = ? "
            "WHERE id = ? AND status = ?",
            (target.value, reminder_id, ReminderStatus.PENDING.value),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def mark_handled(self, reminder_id: int) -> bool:
        return self._transition(reminder_id, ReminderStatus.HANDLED)

    def cancel(self, reminder_id: int) -> bool:
        return self._transition(reminder_id, ReminderStatus.CANCELLED)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
