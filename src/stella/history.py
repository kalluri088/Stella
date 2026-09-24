"""Durable, bounded action history for the trusted dispatcher.

The dispatcher used to keep its audit trail in a 256-record in-memory
deque, so "what did Stella do yesterday?" was unanswerable after a
restart. This module stores the same bounded, metadata-only records
persistently. Entries never contain file contents or tool output: the
dispatcher redacts argument values before handing them over, exactly as
it always did for the in-memory trail.

Entries are plain JSON-serializable dicts so this module stays free of
imports from ``stella.tools`` (which owns ``AuditRecord`` and depends on
this one).
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from collections import deque
from pathlib import Path
from typing import Any


class ActionHistory(ABC):
    """Append-only bounded store of dispatcher audit entries."""

    @abstractmethod
    def append(self, entry: dict[str, Any]) -> None:
        """Record one dispatch attempt; must never raise on full storage."""

    @abstractmethod
    def recent(self, limit: int) -> list[dict[str, Any]]:
        """Return up to ``limit`` newest entries, oldest first."""

    def close(self) -> None:
        """Release any held resources."""


class InMemoryActionHistory(ActionHistory):
    """Process-local bounded trail (the pre-1.1.0 dispatcher behaviour)."""

    def __init__(self, max_records: int) -> None:
        self._records: deque[dict[str, Any]] = deque(maxlen=max_records)

    def append(self, entry: dict[str, Any]) -> None:
        self._records.append(entry)

    def recent(self, limit: int) -> list[dict[str, Any]]:
        records = list(self._records)
        return records[-limit:] if limit > 0 else []


class SQLiteActionHistory(ActionHistory):
    """Persistent bounded trail that survives process restarts.

    Retention is enforced on every append: only the newest
    ``max_records`` rows remain, so the file cannot grow without bound
    in a long-lived desktop install.
    """

    def __init__(self, database_path: str | Path, max_records: int) -> None:
        self.database_path = Path(database_path)
        self._max_records = max_records
        self._connection = sqlite3.connect(self.database_path)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS action_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry TEXT NOT NULL
            )
            """
        )
        self._connection.commit()

    def append(self, entry: dict[str, Any]) -> None:
        self._connection.execute(
            "INSERT INTO action_history (entry) VALUES (?)",
            (json.dumps(entry, sort_keys=True),),
        )
        self._connection.execute(
            "DELETE FROM action_history WHERE id NOT IN "
            "(SELECT id FROM action_history ORDER BY id DESC LIMIT ?)",
            (self._max_records,),
        )
        self._connection.commit()

    def recent(self, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        rows = self._connection.execute(
            "SELECT entry FROM action_history ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [json.loads(row[0]) for row in reversed(rows)]

    def close(self) -> None:
        self._connection.close()
