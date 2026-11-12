"""Text embeddings behind a small interface, so the model host can change.

Locally the host is Ollama; on Databricks it is a model serving endpoint. The three
model families named here are served in both places, so vectors built locally are a
fair preview of what the workspace produces. (Ollama runs a quantised build, and the
index records the model's name, so an index is rebuilt after a move, never mixed.)
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol

import httpx

from .config import Settings


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


class DatabricksEmbedder:
    """An embedding model behind a Databricks model serving endpoint; ``model`` is the endpoint's name."""

    def __init__(self, endpoint: str, *, transport: httpx.BaseTransport | None = None, attempts: int = 5) -> None:
        from . import databricks

        self.model = endpoint
        self._databricks = databricks
        self._attempts = attempts
        try:
            self._http = databricks.serving_client(transport=transport)
        except databricks.DatabricksError as exc:
            raise EmbeddingError(str(exc)) from exc

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        for attempt in range(1, self._attempts + 1):
            try:
                response = self._http.post(f"{self.model}/invocations", json={"input": list(texts)})
            except httpx.TransportError as exc:
                raise EmbeddingError(f"Cannot reach the Databricks workspace at {self._http.base_url.host}. ({exc})") from exc
            if response.status_code not in self._databricks.RETRY_STATUSES or attempt == self._attempts:
                break
            self._databricks.wait_before_retry(response, attempt)
        if response.status_code != 200:
            raise EmbeddingError(self._databricks.problem(response, self.model))
        rows = sorted(response.json()["data"], key=lambda row: row.get("index", 0))
        if len(rows) != len(texts):
            raise EmbeddingError(f"Asked for {len(texts)} embeddings, got {len(rows)}")
        return [normalize(row["embedding"]) for row in rows]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([query_prefix(self.model) + text])[0]


def embedder(settings: Settings) -> Embedder:
    if settings.provider == "databricks":
        return DatabricksEmbedder(settings.embed_model)
    return OllamaEmbedder(settings.embed_model, settings.ollama_host)
