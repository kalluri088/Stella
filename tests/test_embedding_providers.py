"""Tests for the real-embedding providers and their failure policies."""

import sqlite3
import sys
import types

from stella.memory import MemoryItem, MemoryScope
from stella.minilm_embedding import (
    MiniLMEmbeddingProvider,
    minilm_extra_available,
)
from stella.ollama_embedding import OllamaEmbeddingProvider, native_embed_url
from stella.semantic_memory import (
    LocalHashEmbeddingProvider,
    SemanticRetriever,
    SQLiteSemanticIndex,
)


class CapturingProvider(OllamaEmbeddingProvider):
    """Records the wire request and replays a canned response."""

    def __init__(self, response=None) -> None:
        super().__init__()
        self.requests: list[dict] = []
        self.response = response

    def _post_json(self, request):
        self.requests.append(request)
        return self.response


# ------------------------------------------------------- URL derivation


def test_embed_url_is_derived_from_the_compatibility_base_url() -> None:
    assert (
        native_embed_url("http://127.0.0.1:11434/v1")
        == "http://127.0.0.1:11434/api/embed"
    )
    assert (
        native_embed_url("http://127.0.0.1:11434/v1/")
        == "http://127.0.0.1:11434/api/embed"
    )
    assert (
        native_embed_url("http://ollama.internal:11434")
        == "http://ollama.internal:11434/api/embed"
    )


# ------------------------------------------------------- wire format


def test_document_and_query_requests_carry_the_mandatory_prefixes() -> None:
    provider = CapturingProvider({"embeddings": [[0.5, 0.25]]})

    assert provider.embed("wifi password") == (0.5, 0.25)
    assert provider.embed("wifi password", query=True) == (0.5, 0.25)
    assert provider.requests == [
        {"model": "nomic-embed-text", "input": "search_document: wifi password"},
        {"model": "nomic-embed-text", "input": "search_query: wifi password"},
    ]


def test_configured_model_reaches_every_request() -> None:
    provider = CapturingProvider({"embeddings": [[1.0]]})
    provider.model = "custom-embedder"

    provider.embed("anything")
    assert provider.requests[0]["model"] == "custom-embedder"


def test_blank_text_never_reaches_the_server() -> None:
    provider = CapturingProvider({"embeddings": [[1.0]]})

    assert provider.embed("") == ()
    assert provider.embed("   ") == ()
    assert provider.embed(None) == ()
    assert provider.requests == []


def test_constructor_rejects_an_unnamed_model() -> None:
    try:
        OllamaEmbeddingProvider(model="  ")
    except ValueError as error:
        assert "non-empty" in str(error)
    else:
        raise AssertionError("an empty model name must not construct")


# ------------------------------------------------------- fail closed


def test_transport_and_payload_failures_all_yield_empty_vectors() -> None:
    failures = [
        None,  # transport or HTTP failure as recorded by _post_json
        {},
        {"embeddings": []},
        {"embeddings": "not-a-list"},
        {"embeddings": ["not-a-vector"]},
        {"embeddings": [[]]},
        {"embeddings": [[float("nan")]]},
        {"embeddings": [[True, 0.5]]},
    ]
    for response in failures:
        provider = CapturingProvider(response)
        assert provider.embed("probe") == (), response


def test_an_unreachable_server_degrades_the_index_not_its_vectors(
    tmp_path,
) -> None:
    index = SQLiteSemanticIndex(tmp_path / "ollama.db", MemoryScope.USER)
    provider = CapturingProvider(None)  # the server never answers
    retriever = SemanticRetriever(provider, index)
    item = MemoryItem(content="The user prefers tea.", id=1, scope=MemoryScope.USER)

    try:
        # Empty vectors are rejected by the shared validation: nothing
        # poisoned is ever stored, and searches simply find nothing.
        assert retriever.index_memory(item) is False
        assert retriever.retrieve("tea") == []
        # And the provider keeps its honest identity label regardless.
        assert provider.method == "ollama-embedding"
    finally:
        index.close()


def test_valid_ollama_rows_survive_alongside_other_providers(
    tmp_path,
) -> None:
    index = SQLiteSemanticIndex(tmp_path / "mixed.db", MemoryScope.USER)
    ollama = CapturingProvider({"embeddings": [[0.9, 0.1]]})
    hash_retriever = SemanticRetriever(LocalHashEmbeddingProvider(), index)
    ollama_retriever = SemanticRetriever(ollama, index)
    item = MemoryItem(content="The user prefers tea.", id=1, scope=MemoryScope.USER)

    try:
        assert ollama_retriever.index_memory(item) is True
        # The hash provider cannot see the Ollama-space row...
        assert hash_retriever.retrieve("tea") == []
        # ...while its own provider finds it at the stored dimension.
        assert [match.item for match in ollama_retriever.retrieve("tea")] == [item]
        stored = sqlite3.connect(tmp_path / "mixed.db").execute(
            "SELECT provider, dimension FROM semantic_vectors"
        ).fetchone()
        assert stored == ("ollama-embedding", 2)
    finally:
        index.close()


# ------------------------------------------------------- MiniLM


def test_minilm_provider_labels_itself() -> None:
    assert MiniLMEmbeddingProvider().method == "minilm-embedding"


def _install_fake_sentence_transformers(monkeypatch, model) -> None:
    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = lambda *args, **kwargs: model
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)


class FakeMiniLM:
    def __init__(self, vector) -> None:
        self.seen: list[list] = []
        self.vector = vector

    def encode(self, texts):
        self.seen.append(list(texts))
        return [self.vector]


def test_minilm_embeds_through_the_extra_on_first_use(monkeypatch) -> None:
    fake = FakeMiniLM([0.1, 0.2, 0.3])
    _install_fake_sentence_transformers(monkeypatch, fake)
    provider = MiniLMEmbeddingProvider()

    assert provider.embed("wifi password") == (0.1, 0.2, 0.3)
    assert provider.embed("wifi password", query=True) == (0.1, 0.2, 0.3)
    # MiniLM is prompt-free: queries embed exactly like documents.
    assert fake.seen == [["wifi password"], ["wifi password"]]


def test_minilm_blank_text_never_loads_a_model(monkeypatch) -> None:
    def explode(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("loading must be lazy")

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = explode
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)

    assert MiniLMEmbeddingProvider().embed("  ") == ()


def test_minilm_failures_yield_empty_vectors(monkeypatch) -> None:
    class Broken:
        def encode(self, texts):
            raise RuntimeError("the model refused")

    _install_fake_sentence_transformers(monkeypatch, Broken())
    assert MiniLMEmbeddingProvider().embed("probe") == ()

    module = types.ModuleType("sentence_transformers")

    def explode(*args, **kwargs):
        raise RuntimeError("the download failed")

    module.SentenceTransformer = explode
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    assert MiniLMEmbeddingProvider().embed("probe") == ()


def test_extra_availability_reflects_the_installed_environment() -> None:
    # sentence-transformers is not a base dependency; the probe must agree
    # with what an import attempt would actually find.
    try:
        import sentence_transformers  # noqa: F401

        installed = True
    except ImportError:
        installed = False
    assert minilm_extra_available() is installed
