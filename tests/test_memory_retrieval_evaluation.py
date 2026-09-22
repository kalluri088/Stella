"""Deterministic evaluation cases for the current lexical memory matcher.

These tests document current behavior and its deliberate limits. They do not
add retrieval behavior or ranking.
"""

import pytest

from stella.memory import (
    InMemoryMemory,
    MemoryItem,
    MemoryScope,
    MemoryType,
    SQLiteMemory,
)


def test_exact_meaningful_term_match_is_correct() -> None:
    memory = InMemoryMemory()
    matching = MemoryItem(content="The user prefers tea.")
    memory.store(matching)

    assert memory.retrieve("tea") == [matching]


def test_multiple_meaningful_terms_match_without_semantic_ranking() -> None:
    memory = InMemoryMemory()
    matching = MemoryItem(content="The user's favorite color is cobalt blue.")
    memory.store(matching)

    assert memory.retrieve("favorite color") == [matching]


def test_stronger_lexical_match_outranks_weaker_match_even_when_older() -> None:
    memory = InMemoryMemory()
    strong = MemoryItem(content="The user prefers tea morning and evening.")
    weak = MemoryItem(content="The user prefers tea in the morning.")
    memory.store(strong)
    memory.store(weak)

    assert memory.retrieve("What does the user prefer morning evening?") == [
        strong,
        weak,
    ]


def test_relevance_beats_recency_and_equal_relevance_uses_recency(tmp_path) -> None:
    for memory in (
        InMemoryMemory(),
        SQLiteMemory(tmp_path / "ranking.db"),
    ):
        try:
            strong_old = MemoryItem(
                content="The user prefers tea morning and evening."
            )
            weak_new = MemoryItem(content="The user prefers tea morning.")
            equal_new = MemoryItem(content="The user prefers coffee morning.")
            memory.store(strong_old)
            memory.store(weak_new)
            memory.store(equal_new)

            ranked = memory.retrieve("What does the user prefer morning evening?")
            assert [item.content for item in ranked] == [
                strong_old.content,
                equal_new.content,
                weak_new.content,
            ]
        finally:
            if isinstance(memory, SQLiteMemory):
                memory.close()


def test_simple_inflections_and_plurals_match() -> None:
    memory = InMemoryMemory()
    matching = MemoryItem(content="The user likes quiet libraries.")
    memory.store(matching)

    assert memory.retrieve("Do I like a quiet library?") == [matching]


def test_unrelated_memories_are_excluded() -> None:
    memory = InMemoryMemory()
    matching = MemoryItem(content="The user prefers tea.")
    unrelated = MemoryItem(content="The user lives in Bengaluru.")
    memory.store(matching)
    memory.store(unrelated)

    assert memory.retrieve("Where is the user located?") == []
    assert memory.retrieve("tea") == [matching]


def test_competing_relevant_memories_are_both_returned_newest_first() -> None:
    memory = InMemoryMemory()
    first = MemoryItem(content="The user prefers tea in the morning.")
    second = MemoryItem(content="The user prefers coffee in the morning.")
    memory.store(first)
    memory.store(second)

    assert memory.retrieve("What does the user prefer in the morning?") == [
        second,
        first,
    ]


def test_synonym_style_query_is_not_understood_by_lexical_retrieval() -> None:
    memory = InMemoryMemory()
    memory.store(MemoryItem(content="The user prefers concise explanations."))

    # "brief" is semantically related to "concise" but has no lexical overlap.
    assert memory.retrieve("Does the user like brief answers?") == []


def test_types_are_metadata_not_retrieval_filters_and_scope_still_isolates() -> None:
    user_memory = InMemoryMemory(scope=MemoryScope.USER)
    stella_memory = InMemoryMemory(scope=MemoryScope.STELLA)
    semantic = MemoryItem(
        content="The user prefers concise explanations.",
        memory_type=MemoryType.SEMANTIC,
        scope=MemoryScope.USER,
    )
    episodic = MemoryItem(
        content="The user chose a concise explanation yesterday.",
        memory_type=MemoryType.EPISODIC,
        scope=MemoryScope.USER,
    )
    internal = MemoryItem(
        content="Stella uses bounded explanations.",
        memory_type=MemoryType.EPISODIC,
        scope=MemoryScope.STELLA,
    )
    user_memory.store(semantic)
    user_memory.store(episodic)
    stella_memory.store(internal)

    assert user_memory.retrieve("concise explanations") == [episodic, semantic]
    assert user_memory.retrieve(
        "concise explanations", memory_type=MemoryType.EPISODIC
    ) == [episodic]
    assert stella_memory.retrieve("bounded explanations") == [internal]
    assert stella_memory.retrieve("concise explanations") == []


def test_recency_orders_equal_relevance_matches_newest_first() -> None:
    memory = InMemoryMemory()
    old = MemoryItem(content="The user prefers tea.")
    new = MemoryItem(content="The user prefers coffee.")
    memory.store(old)
    memory.store(new)

    assert memory.retrieve("What drink does the user prefer?") == [new, old]


def test_empty_and_non_matching_queries_are_empty() -> None:
    memory = InMemoryMemory()
    memory.store(MemoryItem(content="The user prefers tea."))

    assert memory.retrieve("") == []
    assert memory.retrieve("   ") == []
    assert memory.retrieve("?!") == []


def test_repeated_retrieval_is_deterministic() -> None:
    memory = InMemoryMemory()
    memory.store(MemoryItem(content="The user prefers tea morning."))
    memory.store(MemoryItem(content="The user prefers coffee morning."))

    first = memory.retrieve("What does the user prefer morning?")
    second = memory.retrieve("What does the user prefer morning?")

    assert [item.id for item in first] == [item.id for item in second]
    assert first == second


def test_non_string_query_fails_at_the_declared_type_boundary() -> None:
    memory = InMemoryMemory()
    memory.store(MemoryItem(content="The user prefers tea."))

    # The public contract accepts str | None. Other values are invalid caller
    # input and currently fail rather than being coerced or treated as a hit.
    with pytest.raises(AttributeError):
        memory.retrieve(42)  # type: ignore[arg-type]


def test_sqlite_scope_retrieval_isolated_from_other_trusted_scope(tmp_path) -> None:
    database_path = tmp_path / "retrieval-scopes.db"
    with SQLiteMemory(database_path, scope=MemoryScope.USER) as user_memory:
        assert user_memory.store(
            MemoryItem(content="The user prefers tea.", scope=MemoryScope.USER)
        ) is True

    with SQLiteMemory(database_path, scope=MemoryScope.STELLA) as stella_memory:
        assert stella_memory.store(
            MemoryItem(
                content="Stella prefers bounded context.",
                scope=MemoryScope.STELLA,
            )
        ) is True
        assert stella_memory.retrieve("user prefers tea") == []
        assert stella_memory.retrieve("bounded context") == [
            MemoryItem(
                content="Stella prefers bounded context.",
                scope=MemoryScope.STELLA,
            )
        ]

    with SQLiteMemory(database_path, scope=MemoryScope.USER) as user_memory:
        assert user_memory.retrieve("user prefers tea") == [
            MemoryItem(content="The user prefers tea.", scope=MemoryScope.USER)
        ]
