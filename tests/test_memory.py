import pytest

from stella.memory import (
    InMemoryMemory,
    Memory,
    MemoryItem,
    MemoryScope,
    MemoryType,
    MemoryWriteRequest,
    MemoryWriteResult,
    SQLiteMemory,
)


def test_memory_interface_is_abstract() -> None:
    with pytest.raises(TypeError):
        Memory()


def test_in_memory_memory_stores_and_retrieves_items_in_order() -> None:
    memory = InMemoryMemory()
    first = MemoryItem(content="Likes tea")
    second = MemoryItem(content="Lives in Bengaluru")

    memory.store(first)
    memory.store(second)

    assert memory.retrieve() == [first, second]


def test_in_memory_memory_retrieves_matching_items() -> None:
    memory = InMemoryMemory()
    matching = MemoryItem(content="Likes tea")
    memory.store(matching)
    memory.store(MemoryItem(content="Lives in Bengaluru"))

    assert memory.retrieve("TEA") == [matching]


def test_in_memory_memory_matches_obvious_natural_language_query() -> None:
    memory = InMemoryMemory()
    matching = MemoryItem(content="The user's favorite color is cobalt blue.")
    memory.store(matching)
    memory.store(MemoryItem(content="Lives in Bengaluru"))

    assert memory.retrieve("What is my favorite color?") == [matching]


def test_memory_write_request_and_result_are_explicit() -> None:
    item = MemoryItem(content="Likes tea")

    assert MemoryWriteRequest(item=item).item == item
    assert MemoryWriteResult(item=item, written=True).written is True


def test_sqlite_memory_stores_and_retrieves_items(tmp_path) -> None:
    database_path = tmp_path / "memory.db"

    with SQLiteMemory(database_path) as memory:
        memory.store(MemoryItem(content="Likes tea"))
        memory.store(MemoryItem(content="Lives in Bengaluru"))

        assert memory.retrieve() == [
            MemoryItem(content="Likes tea"),
            MemoryItem(content="Lives in Bengaluru"),
        ]
        assert memory.retrieve("TEA") == [MemoryItem(content="Likes tea")]


def test_sqlite_memory_persists_across_instances(tmp_path) -> None:
    database_path = tmp_path / "memory.db"

    with SQLiteMemory(database_path) as first:
        first.store(MemoryItem(content="Favorite language is Rust"))

    with SQLiteMemory(database_path) as second:
        assert second.retrieve("rust") == [
            MemoryItem(content="Favorite language is Rust")
        ]


def test_sqlite_memory_matches_natural_language_and_excludes_unrelated(
    tmp_path,
) -> None:
    database_path = tmp_path / "memory.db"
    matching = MemoryItem(content="The user's favorite color is cobalt blue.")
    unrelated = MemoryItem(content="The user lives in Bengaluru.")

    with SQLiteMemory(database_path) as memory:
        memory.store(matching)
        memory.store(unrelated)

        assert memory.retrieve("What is my favorite color?") == [matching]


def test_sqlite_memory_matches_morphological_variants_in_preference_query(
    tmp_path,
) -> None:
    database_path = tmp_path / "memory.db"
    matching = MemoryItem(
        content="The user prefers concise technical explanations."
    )

    with SQLiteMemory(database_path) as memory:
        memory.store(matching)

        assert memory.retrieve(
            "What do you know about my technical explanation preference?"
        ) == [matching]


def test_in_memory_memory_normalizes_common_plural_forms() -> None:
    memory = InMemoryMemory()
    matching = MemoryItem(content="The user likes quiet libraries.")
    memory.store(matching)

    assert memory.retrieve("Do I like a quiet library?") == [matching]


def test_memory_lifecycle_update_and_delete_have_backend_parity(tmp_path) -> None:
    for memory in (
        InMemoryMemory(),
        SQLiteMemory(tmp_path / "lifecycle.db"),
    ):
        try:
            memory.store(MemoryItem(content="The user prefers tea."))
            memory.store(MemoryItem(content="The user lives in Bengaluru."))
            stored = memory.retrieve()

            assert [item.id for item in stored] == [1, 2]
            assert memory.update(
                stored[0].id or 0,
                MemoryItem(content="The user prefers coffee."),
            ) is True
            assert memory.retrieve() == [
                MemoryItem(content="The user prefers coffee."),
                MemoryItem(content="The user lives in Bengaluru."),
            ]
            assert memory.retrieve("coffee") == [
                MemoryItem(content="The user prefers coffee.")
            ]

            assert memory.delete(stored[1].id or 0) is True
            assert memory.retrieve() == [
                MemoryItem(content="The user prefers coffee.")
            ]
            assert memory.retrieve("Bengaluru") == []
        finally:
            if isinstance(memory, SQLiteMemory):
                memory.close()


def test_memory_lifecycle_rejects_missing_and_conflicting_updates(tmp_path) -> None:
    for memory in (
        InMemoryMemory(),
        SQLiteMemory(tmp_path / "invalid-lifecycle.db"),
    ):
        try:
            memory.store(MemoryItem(content="The user prefers tea."))
            stored = memory.retrieve()[0]

            assert memory.update(999, MemoryItem(content="Unrelated")) is False
            assert memory.update(stored.id or 0, MemoryItem(content="   ")) is False
            assert memory.update(
                stored.id or 0,
                MemoryItem(content="Conflicting identity", id=stored.id),
            ) is False
            assert memory.delete(999) is False
            assert memory.retrieve() == [
                MemoryItem(content="The user prefers tea.")
            ]
        finally:
            if isinstance(memory, SQLiteMemory):
                memory.close()


def test_memory_types_are_stored_and_retrieved_consistently(tmp_path) -> None:
    for memory in (
        InMemoryMemory(),
        SQLiteMemory(tmp_path / "types.db"),
    ):
        try:
            semantic = MemoryItem(
                content="The user prefers concise answers.",
                memory_type=MemoryType.SEMANTIC,
            )
            episodic = MemoryItem(
                content="The user successfully read a project file.",
                memory_type=MemoryType.EPISODIC,
            )

            assert memory.store(semantic) is True
            assert memory.store(episodic) is True
            stored = memory.retrieve()

            assert [item.memory_type for item in stored] == [
                MemoryType.SEMANTIC,
                MemoryType.EPISODIC,
            ]
        finally:
            if isinstance(memory, SQLiteMemory):
                memory.close()


def test_memory_scope_isolation_is_equivalent_across_backends(tmp_path) -> None:
    in_memory_backends = (
        InMemoryMemory(scope=MemoryScope.USER),
        InMemoryMemory(scope=MemoryScope.STELLA),
    )
    sqlite_user = SQLiteMemory(tmp_path / "scopes.db", scope=MemoryScope.USER)
    sqlite_stella = SQLiteMemory(tmp_path / "scopes.db", scope=MemoryScope.STELLA)

    try:
        for user_memory, stella_memory in (
            in_memory_backends,
            (sqlite_user, sqlite_stella),
        ):
            assert user_memory.store(
                MemoryItem(
                    content="The user prefers concise answers.",
                    scope=MemoryScope.USER,
                )
            ) is True
            assert stella_memory.store(
                MemoryItem(
                    content="Stella uses bounded decision steps.",
                    scope=MemoryScope.STELLA,
                )
            ) is True

            assert user_memory.retrieve() == [
                MemoryItem(
                    content="The user prefers concise answers.",
                    scope=MemoryScope.USER,
                )
            ]
            assert stella_memory.retrieve() == [
                MemoryItem(
                    content="Stella uses bounded decision steps.",
                    scope=MemoryScope.STELLA,
                )
            ]
    finally:
        sqlite_user.close()
        sqlite_stella.close()


def test_memory_lifecycle_preserves_scope_and_applies_type_update(tmp_path) -> None:
    for memory in (
        InMemoryMemory(scope=MemoryScope.USER),
        SQLiteMemory(tmp_path / "typed-lifecycle.db", scope=MemoryScope.USER),
    ):
        try:
            assert memory.store(
                MemoryItem(
                    content="The user prefers tea.",
                    memory_type=MemoryType.SEMANTIC,
                    scope=MemoryScope.USER,
                )
            ) is True
            stored = memory.retrieve()[0]

            assert memory.update(
                stored.id or 0,
                MemoryItem(
                    content="The user chose tea during the evening interaction.",
                    memory_type=MemoryType.EPISODIC,
                    scope=MemoryScope.USER,
                ),
            ) is True
            updated = memory.retrieve()[0]
            assert updated.memory_type is MemoryType.EPISODIC
            assert updated.scope is MemoryScope.USER

            assert memory.delete(updated.id or 0) is True
            assert memory.retrieve() == []
        finally:
            if isinstance(memory, SQLiteMemory):
                memory.close()


def test_invalid_type_or_scope_fails_without_mutation(tmp_path) -> None:
    for memory in (
        InMemoryMemory(scope=MemoryScope.USER),
        SQLiteMemory(tmp_path / "invalid-metadata.db", scope=MemoryScope.USER),
    ):
        try:
            assert memory.store(
                MemoryItem(content="Invalid type", memory_type="unknown")
            ) is False
            assert memory.store(
                MemoryItem(content="Wrong owner", scope=MemoryScope.STELLA)
            ) is False
            assert memory.retrieve() == []

            assert memory.store(MemoryItem(content="Valid memory")) is True
            stored = memory.retrieve()[0]
            assert memory.update(
                stored.id or 0,
                MemoryItem(content="Invalid replacement", scope=MemoryScope.STELLA),
            ) is False
            assert memory.retrieve() == [MemoryItem(content="Valid memory")]
        finally:
            if isinstance(memory, SQLiteMemory):
                memory.close()
