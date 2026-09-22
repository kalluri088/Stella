"""Deterministic comparison of Stella's lexical and semantic memory paths.

The expected observations below intentionally record current limitations. This
file does not combine, rerank, or change either retrieval backend.
"""

from dataclasses import dataclass

from stella.memory import InMemoryMemory, MemoryItem, MemoryScope, MemoryType
from stella.semantic_memory import (
    LocalHashEmbeddingProvider,
    SemanticRetriever,
    SQLiteSemanticIndex,
)


@dataclass(frozen=True)
class RetrievalCase:
    name: str
    query: str
    memories: tuple[str, ...]
    expected: tuple[str, ...]
    lexical_expected: tuple[str, ...]
    semantic_expected: tuple[str, ...]
    observation: str


def _run_case(case: RetrievalCase, tmp_path) -> tuple[list[str], list[str]]:
    lexical = InMemoryMemory(scope=MemoryScope.USER)
    index = SQLiteSemanticIndex(tmp_path / f"{case.name}.db", MemoryScope.USER)
    semantic = SemanticRetriever(LocalHashEmbeddingProvider(), index)
    try:
        for content in case.memories:
            lexical.store(MemoryItem(content=content, scope=MemoryScope.USER))
        for item in lexical.retrieve():
            assert semantic.index_memory(item) is True

        lexical_result = [item.content for item in lexical.retrieve(case.query)]
        semantic_result = [
            match.item.content for match in semantic.retrieve(case.query)
        ]
        return lexical_result, semantic_result
    finally:
        index.close()


CASES = (
    RetrievalCase(
        name="exact",
        query="What is my technical explanation preference?",
        memories=("The user prefers concise technical explanations.",),
        expected=("The user prefers concise technical explanations.",),
        lexical_expected=("The user prefers concise technical explanations.",),
        semantic_expected=("The user prefers concise technical explanations.",),
        observation="Both backends retrieve the exact memory.",
    ),
    RetrievalCase(
        name="paraphrase",
        query="Does the user like brief engineering answers?",
        memories=("The user prefers concise technical explanations.",),
        expected=("The user prefers concise technical explanations.",),
        lexical_expected=(),
        semantic_expected=("The user prefers concise technical explanations.",),
        observation="Lexical retrieval misses the paraphrase; local semantic retrieval finds it with a weak score.",
    ),
    RetrievalCase(
        name="unrelated",
        query="What is the machine hostname?",
        memories=("The user prefers concise technical explanations.",),
        expected=(),
        lexical_expected=(),
        semantic_expected=(),
        observation="Both backends correctly return no result for this unrelated query.",
    ),
    RetrievalCase(
        name="competing",
        query="What does the user prefer in the morning?",
        memories=(
            "The user prefers tea in the morning.",
            "The user prefers coffee in the morning.",
        ),
        expected=(
            "The user prefers tea in the morning.",
            "The user prefers coffee in the morning.",
        ),
        lexical_expected=(
            "The user prefers coffee in the morning.",
            "The user prefers tea in the morning.",
        ),
        semantic_expected=(
            "The user prefers tea in the morning.",
            "The user prefers coffee in the morning.",
        ),
        observation="Both retrieve both candidates but disagree on deterministic ordering.",
    ),
    RetrievalCase(
        name="strong_lexical_and_semantic",
        query="What is my technical explanation preference?",
        memories=(
            "The user prefers concise technical explanations.",
            "The user likes brief engineering answers.",
        ),
        expected=("The user prefers concise technical explanations.",),
        lexical_expected=("The user prefers concise technical explanations.",),
        semantic_expected=("The user prefers concise technical explanations.",),
        observation="The exact lexical match is also the strongest local semantic match.",
    ),
    RetrievalCase(
        name="semantic_false_positive",
        query="What is the user's passport number?",
        memories=(
            "The user prefers concise technical explanations.",
            "The user has a cat named Luna.",
        ),
        expected=(),
        lexical_expected=(),
        semantic_expected=(
            "The user has a cat named Luna.",
            "The user prefers concise technical explanations.",
        ),
        observation="Lexical retrieval is safely empty; the local hash index returns unrelated positive-similarity matches.",
    ),
)


def test_lexical_and_semantic_case_results_are_recorded(tmp_path) -> None:
    for case in CASES:
        lexical_result, semantic_result = _run_case(case, tmp_path)
        assert tuple(lexical_result) == case.lexical_expected, case.observation
        assert tuple(semantic_result) == case.semantic_expected, case.observation
        lexical_succeeded = set(lexical_result) == set(case.expected)
        expected_lexical_success = set(case.lexical_expected) == set(case.expected)
        assert lexical_succeeded is expected_lexical_success, case.observation
        semantic_succeeded = set(semantic_result) == set(case.expected)
        expected_semantic_success = set(case.semantic_expected) == set(case.expected)
        assert semantic_succeeded is expected_semantic_success, case.observation


def test_scope_and_type_filtering_are_preserved_independently(tmp_path) -> None:
    lexical = InMemoryMemory(scope=MemoryScope.USER)
    other_scope = InMemoryMemory(scope=MemoryScope.STELLA)
    user_index = SQLiteSemanticIndex(tmp_path / "user.db", MemoryScope.USER)
    stella_index = SQLiteSemanticIndex(tmp_path / "stella.db", MemoryScope.STELLA)
    semantic_user = SemanticRetriever(LocalHashEmbeddingProvider(), user_index)
    semantic_stella = SemanticRetriever(
        LocalHashEmbeddingProvider(), stella_index
    )
    user_semantic = MemoryItem(
        content="The user prefers concise answers.",
        memory_type=MemoryType.SEMANTIC,
        scope=MemoryScope.USER,
    )
    user_episodic = MemoryItem(
        content="The user chose a concise answer yesterday.",
        memory_type=MemoryType.EPISODIC,
        scope=MemoryScope.USER,
    )
    internal = MemoryItem(
        content="Stella uses concise bounded answers.",
        memory_type=MemoryType.EPISODIC,
        scope=MemoryScope.STELLA,
    )
    try:
        lexical.store(user_semantic)
        lexical.store(user_episodic)
        other_scope.store(internal)
        for item in lexical.retrieve():
            assert semantic_user.index_memory(item)
        for item in other_scope.retrieve():
            assert semantic_stella.index_memory(item)

        assert lexical.retrieve(
            "concise answer", memory_type=MemoryType.EPISODIC
        ) == [user_episodic]
        assert other_scope.retrieve("concise answers") == [internal]
        assert lexical.retrieve("bounded answers") == []
        assert [
            match.item.content
            for match in semantic_user.retrieve(
                "concise answer", memory_type=MemoryType.EPISODIC
            )
        ] == [user_episodic.content]
        assert all(
            match.item.scope is MemoryScope.STELLA
            for match in semantic_stella.retrieve("bounded answers")
        )
        assert all(
            match.item.scope is MemoryScope.USER
            for match in semantic_user.retrieve("bounded answers")
        )
    finally:
        user_index.close()
        stella_index.close()


def test_both_backends_are_deterministic_on_repeated_ordering(tmp_path) -> None:
    case = CASES[3]
    first_lexical, first_semantic = _run_case(case, tmp_path)
    second_lexical, second_semantic = _run_case(case, tmp_path)

    assert first_lexical == second_lexical
    assert first_semantic == second_semantic
