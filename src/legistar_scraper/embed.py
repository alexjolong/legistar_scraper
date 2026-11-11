"""Text embeddings behind a small interface, so the model host can change.

Locally the host is Ollama. On Databricks the same interface would wrap a model
serving endpoint; the three models named here are all served there, so vectors
built locally are a fair preview of what the workspace would produce. (Ollama
runs a quantised build, so re-embed after a move rather than mixing vectors.)
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

import httpx


class EmbeddingError(RuntimeError):
    pass


class Embedder(Protocol):
    model: str

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


# Retrieval models are trained with a prefix on the query side only. Keys are
# matched against the lower-cased model name with separators removed.
_QUERY_TASK = "Given a question about county board legislation, retrieve passages from resolutions and staff background that answer it"
_QUERY_PREFIXES = {
    "qwen3embedding": f"Instruct: {_QUERY_TASK}\nQuery: ",
    "bgelarge": "Represent this sentence for searching relevant passages: ",
    "gtelarge": "",
}


def query_prefix(model: str) -> str:
    key = "".join(ch for ch in model.lower() if ch.isalnum())
    return next((prefix for family, prefix in _QUERY_PREFIXES.items() if family in key), "")


def normalize(vector: Sequence[float]) -> list[float]:
    """Unit length, so cosine similarity is a plain dot product downstream."""
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


class OllamaEmbedder:
    def __init__(self, model: str, host: str = "http://127.0.0.1:11434", *, transport: httpx.BaseTransport | None = None) -> None:
        self.model = model
        self._host = host
        self._http = httpx.Client(base_url=host, timeout=300.0, transport=transport)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            # truncate=False: an over-long passage should fail loudly, not be cut short.
            response = self._http.post("/api/embed", json={"model": self.model, "input": list(texts), "truncate": False})
        except httpx.TransportError as exc:
            raise EmbeddingError(f"Cannot reach Ollama at {self._host}. Is it running? ({exc})") from exc
        if response.status_code == 404:
            raise EmbeddingError(f"Ollama does not have the model {self.model!r}. Run: ollama pull {self.model}")
        if response.status_code != 200:
            raise EmbeddingError(f"Ollama embedding request failed ({response.status_code}): {response.text[:300]}")
        vectors = response.json()["embeddings"]
        if len(vectors) != len(texts):
            raise EmbeddingError(f"Asked for {len(texts)} embeddings, got {len(vectors)}")
        return [normalize(vector) for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([query_prefix(self.model) + text])[0]
