"""Embedding backends.

`fastembed` runs a small ONNX model locally, so the project needs no second API
key and no GPU. `hash` is a deterministic offline stand-in that lets the test
suite and CI exercise the full ingestion and retrieval path without downloading
a model — it is not semantically meaningful and must never be used in anger.
"""

import hashlib
import math
import struct
from functools import lru_cache
from typing import Protocol

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class Embedder(Protocol):
    dimension: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class FastEmbedEmbedder:
    """Local ONNX embeddings via fastembed (BAAI/bge-small-en-v1.5 by default)."""

    def __init__(self, model_name: str, dimension: int) -> None:
        from fastembed import TextEmbedding

        self.dimension = dimension
        self._model = TextEmbedding(model_name=model_name)
        logger.info("embedder_loaded", provider="fastembed", model=model_name, dim=dimension)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [vec.tolist() for vec in self._model.embed(texts)]

    def embed_query(self, text: str) -> list[float]:
        # bge models are trained with an asymmetric query prefix; `query_embed`
        # applies it, so queries and passages land in the same space.
        return next(iter(self._model.query_embed([text]))).tolist()


class HashEmbedder:
    """Deterministic hash-based vectors. Offline test double, not a real model."""

    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def _vector(self, text: str) -> list[float]:
        # Hash whole words into buckets: a crude bag-of-words signature that at
        # least makes lexically-similar texts land near each other.
        vec = [0.0] * self.dimension
        for token in text.lower().split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            bucket = struct.unpack("<I", digest[:4])[0] % self.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[bucket] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0:
            # An all-zero vector has undefined cosine distance; use a fixed unit vector.
            vec[0] = 1.0
            return vec
        return [v / norm for v in vec]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    """Process-wide embedder. Cached because loading the ONNX model is slow."""
    settings = get_settings()
    if settings.embedding_provider == "hash":
        logger.warning("embedder_hash_mode", reason="deterministic test embeddings in use")
        return HashEmbedder(settings.embedding_dim)
    return FastEmbedEmbedder(settings.embedding_model, settings.embedding_dim)
