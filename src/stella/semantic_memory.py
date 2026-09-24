"""Provider-neutral semantic retrieval with an optional local backend."""

import hashlib
import json
import math
import re
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from stella.memory import Memory, MemoryItem, MemoryScope, MemoryType

SemanticVector = tuple[float, ...]
DEFAULT_SEMANTIC_LIMIT = 8
DEFAULT_LOCAL_EMBEDDING_DIMENSION = 128
_LOCAL_STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "do",
    "for",
    "how",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
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


def _valid_vector(vector: SemanticVector) -> bool:
    return bool(vector) and all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        for value in vector
    )


def _valid_memory_id(memory_id: int | None) -> bool:
    return (
        isinstance(memory_id, int)
        and not isinstance(memory_id, bool)
        and memory_id > 0
    )


def _valid_provider_method(method: object) -> bool:
    return isinstance(method, str) and bool(method.strip())


@dataclass(frozen=True)
class SemanticMatch:
    """A semantic index result with an index-provided score."""

    item: MemoryItem
    score: float


class EmbeddingProvider(ABC):
    """Provider-neutral interface for turning text into a vector.

    Every provider carries a ``method`` label — reported as retrieval
    provenance and stored on index rows so vectors from different models
    are never compared — and honors the ``query`` flag: embedding a
    retrieval query may differ from embedding a stored document (for
    example nomic-embed-text requires its prompt prefixes). Providers that
    are query-insensitive simply ignore the flag.
    """

    method: str = ""

    @abstractmethod
    def embed(self, text: str, *, query: bool = False) -> SemanticVector:
        """Return a bounded vector for text without granting authority."""


class SemanticIndex(ABC):
    """Scoped semantic index boundary, without a concrete vector store."""

    def __init__(self, scope: MemoryScope) -> None:
        if not isinstance(scope, MemoryScope):
            raise TypeError("scope must be a MemoryScope")
        self.scope = scope

    def upsert(
        self,
        item: MemoryItem,
        vector: SemanticVector,
        provider_method: str,
    ) -> bool:
        """Index one existing memory item if its metadata is trusted-compatible.

        ``provider_method`` labels which embedding produced the vector;
        rows are only ever compared against vectors from the same method.
        """

        if (
            not _valid_memory_id(item.id)
            or not item.content.strip()
            or not isinstance(item.memory_type, MemoryType)
            or not isinstance(item.scope, MemoryScope)
            or item.scope is not self.scope
            or not _valid_vector(vector)
            or not _valid_provider_method(provider_method)
        ):
            return False
        return self._upsert(item, vector, provider_method)

    def search(
        self,
        vector: SemanticVector,
        memory_type: MemoryType | None = None,
        limit: int = DEFAULT_SEMANTIC_LIMIT,
        *,
        provider_method: str,
    ) -> list[SemanticMatch]:
        """Search only this scope and optional memory type, with a hard limit.

        Only rows embedded by ``provider_method`` at this vector's
        dimension participate — mixed-model cosine is rejected, not
        silently attempted.
        """

        if (
            not _valid_vector(vector)
            or (memory_type is not None and not isinstance(memory_type, MemoryType))
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or limit <= 0
            or not _valid_provider_method(provider_method)
        ):
            return []

        matches = self._search(vector, memory_type, limit, provider_method)
        return [
            match
            for match in matches
            if (
                isinstance(match, SemanticMatch)
                and isinstance(match.item.scope, MemoryScope)
                and match.item.scope is self.scope
                and (
                    memory_type is None
                    or match.item.memory_type is memory_type
                )
            )
        ][:limit]

    def clear(self) -> bool:
        """Remove every indexed item in this index's own scope."""

        return self._clear()

    @abstractmethod
    def _upsert(
        self,
        item: MemoryItem,
        vector: SemanticVector,
        provider_method: str,
    ) -> bool:
        """Implement storage in a concrete semantic index."""

    @abstractmethod
    def _search(
        self,
        vector: SemanticVector,
        memory_type: MemoryType | None,
        limit: int,
        provider_method: str,
    ) -> list[SemanticMatch]:
        """Implement retrieval in a concrete semantic index."""

    @abstractmethod
    def _clear(self) -> bool:
        """Implement a scope-bounded wipe in a concrete semantic index."""


class SemanticRetriever:
    """Compose an embedding provider with a trusted scoped semantic index."""

    def __init__(
        self,
        provider: EmbeddingProvider,
        index: SemanticIndex,
        max_results: int = DEFAULT_SEMANTIC_LIMIT,
    ) -> None:
        if not _valid_provider_method(provider.method):
            raise ValueError("provider.method must be a non-empty label")
        if not isinstance(max_results, int) or isinstance(max_results, bool):
            raise TypeError("max_results must be an integer")
        if max_results <= 0:
            raise ValueError("max_results must be positive")
        self.provider = provider
        self.index = index
        self.max_results = max_results

    def index_memory(self, item: MemoryItem) -> bool:
        """Embed and index one already-authorized memory item."""

        if (
            not _valid_memory_id(item.id)
            or not isinstance(item.memory_type, MemoryType)
            or item.scope is not self.index.scope
            or not isinstance(item.scope, MemoryScope)
        ):
            return False
        return self.index.upsert(
            item,
            self.provider.embed(item.content),
            self.provider.method,
        )

    def embed_query(self, query: str) -> SemanticVector:
        """Embed a search query; an empty vector means no embedding exists.

        Exposed separately from :meth:`retrieve` so callers can tell an
        unavailable provider (empty vector) apart from an index with no
        matches, instead of reporting silence as irrelevance.
        """

        if not isinstance(query, str) or not query.strip():
            return ()
        return self.provider.embed(query, query=True)

    def search_vector(
        self,
        vector: SemanticVector,
        memory_type: MemoryType | None = None,
        limit: int | None = None,
    ) -> list[SemanticMatch]:
        """Search the index with an already-embedded query vector."""

        if memory_type is not None and not isinstance(memory_type, MemoryType):
            return []
        if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0
        ):
            return []
        effective_limit = min(limit or self.max_results, self.max_results)
        return self.index.search(
            vector,
            memory_type,
            effective_limit,
            provider_method=self.provider.method,
        )

    def retrieve(
        self,
        query: str,
        memory_type: MemoryType | None = None,
        limit: int | None = None,
    ) -> list[SemanticMatch]:
        """Embed a query and retrieve bounded, scope-filtered matches."""

        return self.search_vector(
            self.embed_query(query), memory_type, limit
        )


def _hashed_feature_index(feature: str, dimension: int) -> tuple[int, int]:
    digest = hashlib.sha256(feature.encode("utf-8")).digest()
    index = int.from_bytes(digest[:8], "big") % dimension
    sign = -1 if digest[8] & 1 else 1
    return index, sign


def _local_features(text: str) -> list[str]:
    words = [
        word
        for word in re.findall(r"[a-z0-9]+", text.casefold())
        if word not in _LOCAL_STOP_WORDS
    ]
    features: list[str] = []
    for word in words:
        features.append(f"w:{word}")
        padded = f"^{word}$"
        features.extend(
            f"c:{padded[index : index + 3]}"
            for index in range(max(0, len(padded) - 2))
        )
    return features


def _cosine_similarity(left: SemanticVector, right: SemanticVector) -> float:
    if len(left) != len(right):
        return 0.0
    dot = sum(first * second for first, second in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


class LocalHashEmbeddingProvider(EmbeddingProvider):
    """Deterministic local feature-hashing embeddings with no model download.

    Query-insensitive by design: the same word/trigram shape is produced
    for a query and a document, so the ``query`` flag is accepted and
    ignored.
    """

    method = "local-hash-embedding"

    def __init__(
        self,
        dimension: int = DEFAULT_LOCAL_EMBEDDING_DIMENSION,
    ) -> None:
        if not isinstance(dimension, int) or isinstance(dimension, bool):
            raise TypeError("dimension must be an integer")
        if dimension < 16:
            raise ValueError("dimension must be at least 16")
        self.dimension = dimension

    def embed(self, text: str, *, query: bool = False) -> SemanticVector:
        if not isinstance(text, str):
            return ()
        vector = [0.0] * self.dimension
        for feature in _local_features(text):
            index, sign = _hashed_feature_index(feature, self.dimension)
            vector[index] += float(sign)
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return tuple(vector)
        return tuple(value / norm for value in vector)


class SQLiteSemanticIndex(SemanticIndex):
    """A small persistent linear-scan vector index backed by SQLite."""

    def __init__(
        self,
        database_path: str | Path,
        scope: MemoryScope,
    ) -> None:
        super().__init__(scope)
        self.database_path = database_path
        self._connection = sqlite3.connect(database_path)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS semantic_vectors (
                memory_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                scope TEXT NOT NULL,
                created_at INTEGER,
                vector TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT '',
                dimension INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (memory_id, scope)
            )
            """
        )
        # Indexes written before providers existed lack the identity
        # columns; they are added empty and healed by the next
        # reconciliation, which rewrites every row with its provider.
        columns = {
            row[1]
            for row in self._connection.execute(
                "PRAGMA table_info(semantic_vectors)"
            )
        }
        if "provider" not in columns:
            self._connection.execute(
                "ALTER TABLE semantic_vectors ADD COLUMN provider TEXT"
            )
        if "dimension" not in columns:
            self._connection.execute(
                "ALTER TABLE semantic_vectors ADD COLUMN dimension INTEGER"
            )
        self._connection.commit()

    def _upsert(
        self,
        item: MemoryItem,
        vector: SemanticVector,
        provider_method: str,
    ) -> bool:
        self._connection.execute(
            """
            INSERT INTO semantic_vectors
                (memory_id, content, memory_type, scope, created_at,
                 vector, provider, dimension)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(memory_id, scope) DO UPDATE SET
                content = excluded.content,
                memory_type = excluded.memory_type,
                created_at = excluded.created_at,
                vector = excluded.vector,
                provider = excluded.provider,
                dimension = excluded.dimension
            """,
            (
                item.id,
                item.content,
                item.memory_type.value,
                item.scope.value,
                item.created_at,
                json.dumps(vector, separators=(",", ":")),
                provider_method,
                len(vector),
            ),
        )
        self._connection.commit()
        return True

    def _search(
        self,
        vector: SemanticVector,
        memory_type: MemoryType | None,
        limit: int,
        provider_method: str,
    ) -> list[SemanticMatch]:
        rows = self._connection.execute(
            """
            SELECT memory_id, content, memory_type, scope, created_at, vector
            FROM semantic_vectors
            WHERE scope = ?
              AND provider = ?
              AND dimension = ?
            """,
            (self.scope.value, provider_method, len(vector)),
        )
        matches: list[SemanticMatch] = []
        for memory_id, content, item_type, scope, created_at, raw_vector in rows:
            try:
                parsed_type = MemoryType(item_type)
                parsed_scope = MemoryScope(scope)
                stored_vector = tuple(json.loads(raw_vector))
                if not _valid_vector(stored_vector):
                    continue
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if memory_type is not None and parsed_type is not memory_type:
                continue
            score = _cosine_similarity(vector, stored_vector)
            if score <= 0:
                continue
            matches.append(
                SemanticMatch(
                    item=MemoryItem(
                        content=content,
                        id=memory_id,
                        memory_type=parsed_type,
                        scope=parsed_scope,
                        created_at=created_at,
                    ),
                    score=score,
                )
            )
        matches.sort(
            key=lambda match: (
                match.score,
                match.item.created_at or 0,
                match.item.id or 0,
            ),
            reverse=True,
        )
        return matches[:limit]

    def _clear(self) -> bool:
        self._connection.execute(
            "DELETE FROM semantic_vectors WHERE scope = ?",
            (self.scope.value,),
        )
        self._connection.commit()
        return True

    def close(self) -> None:
        """Close the SQLite index connection."""

        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def reconcile_semantic_index(
    memory: Memory,
    retriever: SemanticRetriever,
) -> bool:
    """Rebuild the semantic index from the authoritative memory store.

    A full idempotent rebuild (rather than per-operation hooks) because
    ``Memory.store`` never reports the new id and the memory panels can
    change the store through any trusted path: reconciliation heals
    writes, updates and deletes at once. At personal scale the cost is
    a handful of SHA-256 hashes. A False return means the index may be
    stale; the memory store itself is never affected.
    """

    if not retriever.index.clear():
        return False
    ok = True
    for item in memory.retrieve(None):
        if item.scope is not retriever.index.scope:
            continue
        if not retriever.index_memory(item):
            ok = False
    return ok
