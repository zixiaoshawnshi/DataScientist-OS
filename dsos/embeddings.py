"""Semantic search backbone.

Real embeddings when `sentence-transformers` is installed (`pip install
dsos[embeddings]`); otherwise a dependency-free hashing fallback, so layer 1
runs with zero network calls or model downloads. Swap the fallback for the
real model before the demo if search quality matters (it will).
"""

from __future__ import annotations

import re
import zlib

import numpy as np

DIM = 384  # matches all-MiniLM-L6-v2, so swapping the backend needs no migration

_model = None
_model_checked = False


def _get_model():
    global _model, _model_checked
    if not _model_checked:
        _model_checked = True
        try:
            from sentence_transformers import SentenceTransformer

            _model = SentenceTransformer("all-MiniLM-L6-v2")
        except Exception:
            _model = None
    return _model


def embed(text: str) -> np.ndarray:
    """Return a unit-normalized float32 embedding vector for `text`."""
    model = _get_model()
    if model is not None:
        vec = np.asarray(model.encode(text), dtype=np.float32)
    else:
        vec = _hash_embed(text)
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else vec


def _hash_embed(text: str, dim: int = DIM) -> np.ndarray:
    """Deterministic bag-of-words hashing embedding. No model, no network —
    good enough to make search_artifacts work end-to-end during development.

    Uses zlib.crc32, not Python's built-in hash(): str hashing is randomized
    per-process (PYTHONHASHSEED) unless disabled, which would silently make
    stored embeddings incomparable across runs.
    """
    vec = np.zeros(dim, dtype=np.float32)
    for tok in re.findall(r"[a-z0-9]+", text.lower()):
        vec[zlib.crc32(tok.encode()) % dim] += 1.0
    return vec


def cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom else 0.0
