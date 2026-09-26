"""Minimal in-memory memory abstractions."""

import re
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Self

_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "do",
    "for",
    "how",
    "i",
    "is",
    "it",
    "me",
    "my",
    "of",
    "please",
    "the",
    "to",
    "what",
    "when",
    "where",
    "who",
    "why",
    "with",
    "you",
    "your",
}


def _terms(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", text.casefold())
    return {
        _normalize_term(word)
        for word in words
        if word not in _STOP_WORDS
    }


def _normalize_term(term: str) -> str:
    """Apply small deterministic suffix normalization for lexical matching."""

    if len(term) <= 3:
        return term
    if term.endswith("ies"):
        return term[:-3] + "y"
    if term.endswith(("ches", "shes", "sses", "xes", "zes")):
        return term[:-2]
    if term.endswith("s") and not term.endswith(("ss", "us", "is")):
        return term[:-1]
    if term.endswith("ing") and len(term) > 5:
        return term[:-3]
    if term.endswith("ed") and len(term) > 4:
        return term[:-2]
    return term


def _matches_query(content: str, query: str) -> bool:
    return relevance_score(content, query) > 0


def relevance_score(content: str, query: str) -> int:
    """Deterministic lexical relevance of stored content to a query."""

    query_terms = _terms(query)
    if not query_terms:
        return 0
    shared_terms = query_terms & _terms(content)
    if len(shared_terms) < min(2, len(query_terms)):
        return 0
    return len(shared_terms)


def memory_terms(text: str) -> set[str]:
    """Public view of the normalized term set used by matching above.

    Exported for the memory-scale policies (B3): dedupe guidance compares
    whole normalized term sets, and must use the same normalization the
    recall path uses, never a second dialect.
    """

    return _terms(text)


class MemoryType(str, Enum):
    """The deliberately small set of memory categories."""

    SEMANTIC = "semantic"
    EPISODIC = "episodic"


class MemoryScope(str, Enum):
    """Trusted single-owner scopes, not a multi-user identity system."""

    USER = "user"
    STELLA = "stella"


@dataclass(frozen=True)
class MemoryItem:
    """A single item explicitly stored in memory."""

    content: str
    id: int | None = field(default=None, compare=False)
    memory_type: MemoryType = field(default=MemoryType.SEMANTIC)
    scope: MemoryScope = field(default=MemoryScope.USER)
    created_at: int | None = field(default=None, compare=False)


def _valid_memory_id(memory_id: int) -> bool:
    return (
        isinstance(memory_id, int)
        and not isinstance(memory_id, bool)
        and memory_id > 0
    )


def _valid_lifecycle_request(memory_id: int, replacement: MemoryItem) -> bool:
    return (
        _valid_memory_id(memory_id)
        and replacement.id is None
        and bool(replacement.content.strip())
    )


def _valid_item(item: MemoryItem, scope: MemoryScope) -> bool:
    return (
        isinstance(item.memory_type, MemoryType)
        and isinstance(item.scope, MemoryScope)
        and item.scope is scope
        and bool(item.content.strip())
    )


@dataclass(frozen=True)
class MemoryWriteRequest:
    """An explicit request to store one memory item."""

    item: MemoryItem


@dataclass(frozen=True)
class MemoryWriteResult:
    """The result of an explicit memory write."""

    item: MemoryItem
    written: bool


class Memory(ABC):
    """Interface for storing and retrieving memory items."""

    @abstractmethod
    def store(self, item: MemoryItem) -> bool:
        """Store a memory item."""

    @abstractmethod
    def retrieve(
        self,
        query: str | None = None,
        memory_type: MemoryType | None = None,
    ) -> list[MemoryItem]:
        """Retrieve stored items, optionally matching their content."""

    @abstractmethod
    def update(self, memory_id: int, replacement: MemoryItem) -> bool:
        """Replace one existing item through a trusted lifecycle operation."""

    @abstractmethod
    def delete(self, memory_id: int) -> bool:
        """Delete one existing item through a trusted lifecycle operation."""


class InMemoryMemory(Memory):
    """A deterministic memory implementation backed by a list."""

    def __init__(self, scope: MemoryScope = MemoryScope.USER) -> None:
        if not isinstance(scope, MemoryScope):
            raise TypeError("scope must be a MemoryScope")
        self.scope = scope
        self._items: list[MemoryItem] = []
        self._next_id = 1
        self._next_recency = 1

    def store(self, item: MemoryItem) -> bool:
        if not _valid_item(item, self.scope):
            return False
        self._items.append(
            MemoryItem(
                content=item.content,
                id=self._next_id,
                memory_type=item.memory_type,
                scope=self.scope,
                created_at=self._next_recency,
            )
        )
        self._next_id += 1
        self._next_recency += 1
        return True

    def retrieve(
        self,
        query: str | None = None,
        memory_type: MemoryType | None = None,
    ) -> list[MemoryItem]:
        if memory_type is not None and not isinstance(memory_type, MemoryType):
            return []

        items = [
            item
            for item in self._items
            if memory_type is None or item.memory_type is memory_type
        ]
        if query is None:
            return items

        matches = [
            item for item in items if _matches_query(item.content, query)
        ]
        return sorted(
            matches,
            key=lambda item: (
                relevance_score(item.content, query),
                item.created_at or 0,
                item.id or 0,
            ),
            reverse=True,
        )

    def update(self, memory_id: int, replacement: MemoryItem) -> bool:
        if not (
            _valid_lifecycle_request(memory_id, replacement)
            and _valid_item(replacement, self.scope)
        ):
            return False

        matches = [
            index
            for index, item in enumerate(self._items)
            if item.id == memory_id
        ]
        if len(matches) != 1:
            return False

        self._items[matches[0]] = MemoryItem(
            content=replacement.content,
            id=memory_id,
            memory_type=replacement.memory_type,
            scope=self.scope,
            created_at=self._items[matches[0]].created_at,
        )
        return True

    def delete(self, memory_id: int) -> bool:
        if not _valid_memory_id(memory_id):
            return False

        matches = [
            index
            for index, item in enumerate(self._items)
            if item.id == memory_id
        ]
        if len(matches) != 1:
            return False

        del self._items[matches[0]]
        return True


class SQLiteMemory(Memory):
    """A persistent memory implementation backed by a SQLite database."""

    def __init__(
        self,
        database_path: str | Path,
        scope: MemoryScope = MemoryScope.USER,
    ) -> None:
        if not isinstance(scope, MemoryScope):
            raise TypeError("scope must be a MemoryScope")
        self.database_path = Path(database_path)
        self.scope = scope
        self._connection = sqlite3.connect(self.database_path)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                memory_type TEXT NOT NULL DEFAULT 'semantic',
                scope TEXT NOT NULL DEFAULT 'user',
                created_at INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        columns = {
            row[1]
            for row in self._connection.execute("PRAGMA table_info(memories)")
        }
        if "memory_type" not in columns:
            self._connection.execute(
                "ALTER TABLE memories ADD COLUMN memory_type TEXT NOT NULL "
                "DEFAULT 'semantic'"
            )
        if "scope" not in columns:
            self._connection.execute(
                "ALTER TABLE memories ADD COLUMN scope TEXT NOT NULL "
                "DEFAULT 'user'"
            )
        if "created_at" not in columns:
            self._connection.execute(
                "ALTER TABLE memories ADD COLUMN created_at INTEGER NOT NULL "
                "DEFAULT 0"
            )
        self._connection.commit()

    def store(self, item: MemoryItem) -> bool:
        if not _valid_item(item, self.scope):
            return False
        cursor = self._connection.execute(
            "INSERT INTO memories (content, memory_type, scope) "
            "VALUES (?, ?, ?)",
            (item.content, item.memory_type.value, self.scope.value),
        )
        self._connection.execute(
            "UPDATE memories SET created_at = ? WHERE id = ?",
            (cursor.lastrowid, cursor.lastrowid),
        )
        self._connection.commit()
        return True

    def retrieve(
        self,
        query: str | None = None,
        memory_type: MemoryType | None = None,
    ) -> list[MemoryItem]:
        if memory_type is not None and not isinstance(memory_type, MemoryType):
            return []

        rows = self._connection.execute(
            "SELECT id, content, memory_type, scope, created_at "
            "FROM memories "
            "WHERE scope = ? ORDER BY id",
            (self.scope.value,),
        )
        items = []
        for memory_id, content, item_type, scope, created_at in rows:
            try:
                item = MemoryItem(
                    content=content,
                    id=memory_id,
                    memory_type=MemoryType(item_type),
                    scope=MemoryScope(scope),
                    created_at=created_at,
                )
            except ValueError:
                continue
            if memory_type is None or item.memory_type is memory_type:
                items.append(item)
        if query is None:
            return items
        matches = [
            item for item in items if _matches_query(item.content, query)
        ]
        return sorted(
            matches,
            key=lambda item: (
                relevance_score(item.content, query),
                item.created_at or 0,
                item.id or 0,
            ),
            reverse=True,
        )

    def update(self, memory_id: int, replacement: MemoryItem) -> bool:
        if not (
            _valid_lifecycle_request(memory_id, replacement)
            and _valid_item(replacement, self.scope)
        ):
            return False

        cursor = self._connection.execute(
            "UPDATE memories SET content = ?, memory_type = ? "
            "WHERE id = ? AND scope = ?",
            (
                replacement.content,
                replacement.memory_type.value,
                memory_id,
                self.scope.value,
            ),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def delete(self, memory_id: int) -> bool:
        if not _valid_memory_id(memory_id):
            return False

        cursor = self._connection.execute(
            "DELETE FROM memories WHERE id = ? AND scope = ?",
            (memory_id, self.scope.value),
        )
        self._connection.commit()
        return cursor.rowcount == 1

    def close(self) -> None:
        """Close the database connection."""

        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
