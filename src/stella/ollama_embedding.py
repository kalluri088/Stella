"""Ollama-backed embedding provider for semantic memory recall."""

import http.client
import json
import math
import urllib.parse
from typing import Any

from stella.ollama_client import DEFAULT_OLLAMA_BASE_URL
from stella.semantic_memory import EmbeddingProvider, SemanticVector

EMBEDDING_REQUEST_TIMEOUT_SECONDS = 30.0
# nomic-embed-text is instruction-prompted: Ollama passes ``input`` through
# verbatim (verified on this machine; see STELLA-BENCHMARK-REPORT Part 4),
# so the client must apply the same prefixes the model was trained with,
# symmetrically for documents and queries.
_DOCUMENT_PREFIX = "search_document: "
_QUERY_PREFIX = "search_query: "


def native_embed_url(base_url: str) -> str:
    """Derive the native /api/embed endpoint from a compatibility base URL."""

    trimmed = base_url.rstrip("/")
    return f"{trimmed.removesuffix('/v1')}/api/embed"


class OllamaEmbeddingProvider(EmbeddingProvider):
    """Embeds text through Ollama's native /api/chat sibling endpoint.

    Fails closed: any transport, HTTP or payload problem returns an empty
    vector, which the index validation rejects. An unreachable server
    therefore degrades to no semantic supplements plus honest sync-failure
    reporting — never to poisoned or fabricated vectors.
    """

    method = "ollama-embedding"

    def __init__(
        self,
        model: str = "nomic-embed-text",
        base_url: str = DEFAULT_OLLAMA_BASE_URL,
        timeout_seconds: float = EMBEDDING_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.embed_url = native_embed_url(base_url)

    def embed(self, text: str, *, query: bool = False) -> SemanticVector:
        if not isinstance(text, str) or not text.strip():
            return ()
        prompted = f"{_QUERY_PREFIX if query else _DOCUMENT_PREFIX}{text}"
        payload = self._post_json({"model": self.model, "input": prompted})
        return self._parse_embedding(payload)

    def _post_json(self, request: dict[str, object]) -> Any:
        """POST one embedding request; None on any failure, fail-closed."""

        url = urllib.parse.urlsplit(self.embed_url)
        secure = url.scheme == "https"
        connection_class = (
            http.client.HTTPSConnection if secure else http.client.HTTPConnection
        )
        body = json.dumps(request).encode("utf-8")
        try:
            connection = connection_class(
                url.hostname or "127.0.0.1",
                url.port or (443 if secure else 80),
                timeout=self.timeout_seconds,
            )
            try:
                connection.request(
                    "POST",
                    url.path or "/api/embed",
                    body=body,
                    headers={"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                if response.status >= 400:
                    return None
                return json.loads(response.read().decode("utf-8"))
            finally:
                connection.close()
        except (OSError, ValueError):
            # ValueError covers json.JSONDecodeError: a malformed body is
            # an unavailable embedding, not a crash in the middle of a
            # conversation turn.
            return None

    @staticmethod
    def _parse_embedding(payload: Any) -> SemanticVector:
        if not isinstance(payload, dict):
            return ()
        embeddings = payload.get("embeddings")
        if not isinstance(embeddings, list) or not embeddings:
            return ()
        vector = embeddings[0]
        if (
            not isinstance(vector, list)
            or not vector
            or not all(
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                for value in vector
            )
        ):
            return ()
        return tuple(float(value) for value in vector)
