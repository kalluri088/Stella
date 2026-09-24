from __future__ import annotations

import json
import sqlite3

from stella.memory import (
    InMemoryMemory,
    MemoryItem,
    MemoryScope,
    MemoryType,
)
from stella.semantic_memory import (
    EmbeddingProvider,
    LocalHashEmbeddingProvider,
    SemanticIndex,
    SemanticMatch,
    SemanticRetriever,
    SemanticVector,
    SQLiteSemanticIndex,
    reconcile_semantic_index,
)


class FakeEmbeddingProvider(EmbeddingProvider):
    method = "fake-embedding"

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def embed(self, text: str, *, query: bool = False) -> SemanticVector:
        self.calls.append((text, query))
        return (float(len(text)), 1.0)


class RecordingSemanticIndex(SemanticIndex):
    def __init__(self, scope: MemoryScope) -> None:
        super().__init__(scope)
        self.indexed: list[tuple[MemoryItem, SemanticVector]] = []
        self.indexed_methods: list[str] = []
        self.searched_methods: list[str] = []
        self.matches: list[SemanticMatch] = []
        self.cleared = 0

    def _upsert(
        self,
        item: MemoryItem,
        vector: SemanticVector,
        provider_method: str,
    ) -> bool:
        self.indexed.append((item, vector))
        self.indexed_methods.append(provider_method)
        return True

    def _search(
        self,
        vector: SemanticVector,
        memory_type: MemoryType | None,
        limit: int,
        provider_method: str,
    ) -> list[SemanticMatch]:
        self.searched_methods.append(provider_method)
        return list(self.matches)

    def _clear(self) -> bool:
        self.cleared += 1
        self.indexed.clear()
        return True


class AlternateSemanticIndex(RecordingSemanticIndex):
    """A second implementation proving the retriever is backend-neutral."""


def test_semantic_retriever_separates_embedding_index_and_retrieval() -> None:
    provider = FakeEmbeddingProvider()
    index = RecordingSemanticIndex(MemoryScope.USER)
    retriever = SemanticRetriever(provider, index)
    item = MemoryItem(
        content="The user prefers concise explanations.",
        id=1,
        memory_type=MemoryType.SEMANTIC,
        scope=MemoryScope.USER,
    )
    index.matches = [SemanticMatch(item=item, score=0.9)]

    assert retriever.index_memory(item) is True
    assert index.indexed == [(item, (float(len(item.content)), 1.0))]
    assert index.indexed_methods == ["fake-embedding"]
    assert retriever.retrieve("What does the user prefer?") == index.matches
    assert index.searched_methods == ["fake-embedding"]
    assert provider.calls == [
        (item.content, False),
        ("What does the user prefer?", True),
    ]


def test_retriever_requires_a_labelled_provider() -> None:
    unlabeled = FakeEmbeddingProvider()
    unlabeled.method = ""

    try:
        SemanticRetriever(unlabeled, RecordingSemanticIndex(MemoryScope.USER))
    except ValueError as error:
        assert "non-empty label" in str(error)
    else:
        raise AssertionError("an unlabelled provider must not be retrievable")


def test_semantic_index_rejects_mismatched_scope_and_invalid_metadata() -> None:
    index = RecordingSemanticIndex(MemoryScope.USER)
    valid_vector = (1.0, 2.0)

    assert index.upsert(
        MemoryItem(content="Other owner", id=1, scope=MemoryScope.STELLA),
        valid_vector,
        "fake-embedding",
    ) is False
    assert index.upsert(
        MemoryItem(content="Invalid type", id=2, memory_type="unknown"),
        valid_vector,
        "fake-embedding",
    ) is False
    # An unlabelled provider identity is refused like any other invalid input.
    assert index.upsert(
        MemoryItem(content="User fact", id=3, scope=MemoryScope.USER),
        valid_vector,
        "",
    ) is False
    assert index.indexed == []
    assert index.clear() is True
    assert index.cleared == 1


def test_semantic_search_filters_scope_and_type_and_bounds_results() -> None:
    index = RecordingSemanticIndex(MemoryScope.USER)
    user_semantic = MemoryItem(
        content="User fact",
        id=1,
        memory_type=MemoryType.SEMANTIC,
        scope=MemoryScope.USER,
    )
    user_episodic = MemoryItem(
        content="User event",
        id=2,
        memory_type=MemoryType.EPISODIC,
        scope=MemoryScope.USER,
    )
    other_scope = MemoryItem(
        content="Other scope",
        id=3,
        memory_type=MemoryType.SEMANTIC,
        scope=MemoryScope.STELLA,
    )
    index.matches = [
        SemanticMatch(user_semantic, 0.9),
        SemanticMatch(user_episodic, 0.8),
        SemanticMatch(other_scope, 1.0),
    ]

    assert index.search((1.0,), limit=2, provider_method="fake-embedding") == [
        SemanticMatch(user_semantic, 0.9),
        SemanticMatch(user_episodic, 0.8),
    ]
    assert index.search(
        (1.0,),
        memory_type=MemoryType.EPISODIC,
        provider_method="fake-embedding",
    ) == [SemanticMatch(user_episodic, 0.8)]
    assert index.search(
        (1.0,), memory_type="unknown", provider_method="fake-embedding"
    ) == []  # type: ignore[arg-type]
    assert index.search((1.0,), limit=0, provider_method="fake-embedding") == []
    # A search without a provider identity would silently compare against
    # vectors from an unknown model, so it is refused outright.
    assert index.search((1.0,), provider_method="") == []


def test_semantic_retriever_rejects_empty_invalid_queries_without_embedding() -> None:
    provider = FakeEmbeddingProvider()
    retriever = SemanticRetriever(provider, RecordingSemanticIndex(MemoryScope.USER))

    assert retriever.retrieve("") == []
    assert retriever.retrieve("   ") == []
    assert retriever.retrieve(42) == []  # type: ignore[arg-type]
    assert provider.calls == []


def test_semantic_retriever_can_substitute_another_index_backend() -> None:
    provider = FakeEmbeddingProvider()
    index = AlternateSemanticIndex(MemoryScope.USER)
    retriever = SemanticRetriever(provider, index, max_results=1)
    item = MemoryItem(content="User fact", id=1, scope=MemoryScope.USER)
    index.matches = [SemanticMatch(item, 0.5), SemanticMatch(item, 0.4)]

    assert retriever.index_memory(item) is True
    assert retriever.retrieve("fact", limit=9) == [SemanticMatch(item, 0.5)]


def test_local_backend_finds_similar_text_and_excludes_unrelated_text(tmp_path) -> None:
    provider = LocalHashEmbeddingProvider()
    index = SQLiteSemanticIndex(str(tmp_path / "semantic.db"), MemoryScope.USER)
    retriever = SemanticRetriever(provider, index)
    similar = MemoryItem(
        content="The user prefers tea in the evening.",
        id=1,
        scope=MemoryScope.USER,
    )
    unrelated = MemoryItem(
        content="Quantum entanglement laboratory protocol.",
        id=2,
        scope=MemoryScope.USER,
    )

    try:
        assert retriever.index_memory(similar) is True
        assert retriever.index_memory(unrelated) is True
        matches = retriever.retrieve("Which beverage do I like in the evening?")
        assert matches
        assert matches[0].item.content == similar.content
        assert retriever.retrieve("quantum entanglement protocol")[0].item == unrelated
    finally:
        index.close()


def test_local_backend_preserves_scope_isolation(tmp_path) -> None:
    database_path = str(tmp_path / "scoped-semantic.db")
    provider = LocalHashEmbeddingProvider()
    user_index = SQLiteSemanticIndex(database_path, MemoryScope.USER)
    stella_index = SQLiteSemanticIndex(database_path, MemoryScope.STELLA)
    user_item = MemoryItem(
        content="The user prefers concise answers.",
        id=1,
        scope=MemoryScope.USER,
    )
    stella_item = MemoryItem(
        content="Stella uses bounded answers.",
        id=1,
        scope=MemoryScope.STELLA,
    )

    try:
        assert SemanticRetriever(provider, user_index).index_memory(user_item)
        assert SemanticRetriever(provider, stella_index).index_memory(stella_item)
        user_matches = SemanticRetriever(provider, user_index).retrieve(
            "bounded answers"
        )
        stella_matches = SemanticRetriever(provider, stella_index).retrieve(
            "bounded answers"
        )
        assert all(match.item.content != stella_item.content for match in user_matches)
        assert stella_matches[0].item.content == stella_item.content
    finally:
        user_index.close()
        stella_index.close()


def test_local_backend_filters_type_and_bounds_deterministically(tmp_path) -> None:
    provider = LocalHashEmbeddingProvider()
    index = SQLiteSemanticIndex(str(tmp_path / "typed-semantic.db"), MemoryScope.USER)
    retriever = SemanticRetriever(provider, index, max_results=2)
    items = [
        MemoryItem(
            content="The user prefers tea in the evening.",
            id=1,
            memory_type=MemoryType.SEMANTIC,
            scope=MemoryScope.USER,
        ),
        MemoryItem(
            content="The user drank tea yesterday evening.",
            id=2,
            memory_type=MemoryType.EPISODIC,
            scope=MemoryScope.USER,
        ),
        MemoryItem(
            content="The user drinks tea after work.",
            id=3,
            memory_type=MemoryType.SEMANTIC,
            scope=MemoryScope.USER,
        ),
    ]

    try:
        for item in items:
            assert retriever.index_memory(item) is True
        first = retriever.retrieve("tea evening", limit=99)
        second = retriever.retrieve("tea evening", limit=99)
        assert len(first) == 2
        assert first == second
        assert all(match.item.memory_type is MemoryType.SEMANTIC for match in retriever.retrieve(
            "tea", memory_type=MemoryType.SEMANTIC
        ))
    finally:
        index.close()


def test_local_backend_persists_vectors_and_metadata(tmp_path) -> None:
    database_path = str(tmp_path / "persistent-semantic.db")
    provider = LocalHashEmbeddingProvider()
    item = MemoryItem(
        content="The user prefers concise technical explanations.",
        id=7,
        memory_type=MemoryType.EPISODIC,
        scope=MemoryScope.USER,
        created_at=12,
    )
    first_index = SQLiteSemanticIndex(database_path, MemoryScope.USER)
    try:
        assert SemanticRetriever(provider, first_index).index_memory(item)
    finally:
        first_index.close()

    second_index = SQLiteSemanticIndex(database_path, MemoryScope.USER)
    try:
        matches = SemanticRetriever(provider, second_index).retrieve(
            "concise technical explanations"
        )
        assert matches[0].item == item
        assert matches[0].item.created_at == 12
    finally:
        second_index.close()


def test_search_never_compares_vectors_from_a_different_model(
    tmp_path,
) -> None:
    database_path = str(tmp_path / "mixed-provider.db")
    index = SQLiteSemanticIndex(database_path, MemoryScope.USER)
    provider = FakeEmbeddingProvider()
    item = MemoryItem(content="The user prefers tea.", id=1, scope=MemoryScope.USER)

    try:
        assert SemanticRetriever(provider, index).index_memory(item) is True
        # Same dimension, different provider: the row is filtered out by
        # identity, so cross-model cosine is never even attempted.
        other = FakeEmbeddingProvider()
        other.method = "other-embedding"
        assert SemanticRetriever(other, index).retrieve("tea") == []
        # A different dimension is likewise never multiplied against the
        # stored vector.
        assert (
            SemanticRetriever(LocalHashEmbeddingProvider(), index).retrieve(
                "tea"
            )
            == []
        )
        # The embedding's own provider still finds its row.
        found = SemanticRetriever(provider, index).retrieve("tea")
        assert [match.item for match in found] == [item]
    finally:
        index.close()


def test_legacy_rows_without_provider_identity_are_skipped_then_healed(
    tmp_path,
) -> None:
    database_path = str(tmp_path / "legacy-semantic.db")
    provider = LocalHashEmbeddingProvider()
    item = MemoryItem(
        content="The user prefers tea.",
        id=1,
        memory_type=MemoryType.SEMANTIC,
        scope=MemoryScope.USER,
    )
    # Recreate the pre-B2.1 schema exactly: no provider or dimension
    # columns, one row that predates provider identity entirely.
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE semantic_vectors (
            memory_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            memory_type TEXT NOT NULL,
            scope TEXT NOT NULL,
            created_at INTEGER,
            vector TEXT NOT NULL,
            PRIMARY KEY (memory_id, scope)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO semantic_vectors
            (memory_id, content, memory_type, scope, created_at, vector)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            item.id,
            item.content,
            item.memory_type.value,
            item.scope.value,
            item.created_at,
            json.dumps(list(provider.embed(item.content))),
        ),
    )
    connection.commit()
    connection.close()

    index = SQLiteSemanticIndex(database_path, MemoryScope.USER)
    retriever = SemanticRetriever(provider, index)
    try:
        # The identity-less row cannot participate in any search.
        assert retriever.retrieve("prefers tea") == []
        # Rewriting it (what reconciliation does per item) heals it.
        assert retriever.index_memory(item) is True
        matches = retriever.retrieve("prefers tea")
        assert [match.item.content for match in matches] == [item.content]
    finally:
        index.close()


def test_reconcile_rebuilds_the_index_from_the_authoritative_memory_store(
    tmp_path,
) -> None:
    provider = LocalHashEmbeddingProvider()
    index = SQLiteSemanticIndex(str(tmp_path / "reconcile.db"), MemoryScope.USER)
    retriever = SemanticRetriever(provider, index)
    memory = InMemoryMemory()
    memory.store(MemoryItem(content="The user prefers tea in the morning."))
    memory.store(MemoryItem(content="The user works late on Thursdays."))
    # A row for a memory that no longer exists: only a rebuild can heal it.
    retriever.index_memory(
        MemoryItem(
            content="Quantum entanglement laboratory protocol.",
            id=99,
            scope=MemoryScope.USER,
        )
    )

    try:
        assert reconcile_semantic_index(memory, retriever) is True
        assert retriever.retrieve("quantum entanglement protocol") == []
        morning = retriever.retrieve("tea in the morning")
        assert morning[0].item.content == "The user prefers tea in the morning."
        # Reconciliation is idempotent: a second pass changes nothing.
        assert reconcile_semantic_index(memory, retriever) is True
        assert retriever.retrieve("tea in the morning") == morning
    finally:
        index.close()


def test_reconcile_isolates_other_scopes_in_the_same_database(tmp_path) -> None:
    database_path = str(tmp_path / "scoped-reconcile.db")
    provider = LocalHashEmbeddingProvider()
    user_index = SQLiteSemanticIndex(database_path, MemoryScope.USER)
    stella_index = SQLiteSemanticIndex(database_path, MemoryScope.STELLA)
    user_retriever = SemanticRetriever(provider, user_index)
    memory = InMemoryMemory()
    memory.store(
        MemoryItem(
            content="The user prefers tea.",
            id=1,
            scope=MemoryScope.USER,
        )
    )
    stella_item = MemoryItem(
        content="Stella prefers tea.",
        id=1,
        scope=MemoryScope.STELLA,
    )

    try:
        assert SemanticRetriever(provider, stella_index).index_memory(stella_item)
        assert reconcile_semantic_index(memory, user_retriever) is True
        # The wipe and rebuild stayed inside the user scope only.
        surviving = SemanticRetriever(provider, stella_index).retrieve(
            "prefers tea"
        )
        assert [match.item.content for match in surviving] == [stella_item.content]
    finally:
        user_index.close()
        stella_index.close()


class FailingClearIndex(RecordingSemanticIndex):
    def _clear(self) -> bool:
        return False


def test_reconcile_reports_failure_without_touching_the_memory_store() -> None:
    memory = InMemoryMemory()
    memory.store(MemoryItem(content="The user prefers tea."))
    index = FailingClearIndex(MemoryScope.USER)
    retriever = SemanticRetriever(LocalHashEmbeddingProvider(), index)

    assert reconcile_semantic_index(memory, retriever) is False
    # The authoritative store is never the casualty of an index failure.
    assert [item.content for item in memory.retrieve(None)] == [
        "The user prefers tea."
    ]
    assert retriever.index.indexed == []
