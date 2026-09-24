"""MiniLM embedding provider: a real local model behind an optional extra.

The base installation never imports sentence-transformers (or torch); this
module only checks that the ``stella[embed]`` extra is present and defers
the actual model load to the first embedding call.
"""

import importlib.util
import math
from typing import Any

from stella.semantic_memory import EmbeddingProvider, SemanticVector

MINILM_MODEL_NAME = "all-MiniLM-L6-v2"
# The CPU device is deliberate: MiniLM measures 24 ms per embedding on CPU
# and this keeps the whole GPU free for the chat model.
MINILM_DEVICE = "cpu"
_EXTRA_INSTALL_HINT = (
    "semantic provider 'minilm' needs the optional extra; install "
    "stella[embed] (CPU-only torch is sufficient) or choose another "
    "STELLA_SEMANTIC_PROVIDER"
)


def minilm_extra_available() -> bool:
    """Report whether sentence-transformers is importable, without importing.

    A cheap spec lookup avoids paying torch's import cost at application
    startup on installs that never select this provider path.
    """

    return importlib.util.find_spec("sentence_transformers") is not None


class MiniLMEmbeddingProvider(EmbeddingProvider):
    """all-MiniLM-L6-v2 sentence embeddings, loaded lazily on first use.

    Fails closed like the other optional providers: an unloadable model
    or a failed encoding returns an empty vector, which the index
    validation rejects, degrading the turn to keyword recall plus honest
    reporting.
    """

    method = "minilm-embedding"

    def __init__(self, model_name: str = MINILM_MODEL_NAME) -> None:
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("model_name must be a non-empty string")
        self.model_name = model_name
        self._model: Any = None

    def embed(self, text: str, *, query: bool = False) -> SemanticVector:
        # all-MiniLM-L6-v2 is prompt-free: queries and documents embed
        # identically, so the query flag is accepted and ignored.
        if not isinstance(text, str) or not text.strip():
            return ()
        model = self._load_model()
        if model is None:
            return ()
        try:
            encoded = model.encode([text])
            vector = tuple(float(value) for value in encoded[0])
        except Exception:  # noqa: BLE001 - see below
            # Deliberately broad: torch/HF failure modes are many and all
            # mean the same thing here — no embedding for this call.
            return ()
        if not vector or not all(math.isfinite(value) for value in vector):
            return ()
        return vector

    def _load_model(self) -> Any:
        """Import and load the model once; None when unavailable."""

        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(
                self.model_name, device=MINILM_DEVICE
            )
        except Exception:  # noqa: BLE001 - unavailable means empty vector
            return None
        return self._model
