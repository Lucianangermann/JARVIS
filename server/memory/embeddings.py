"""Text → vector embeddings via sentence-transformers (all-MiniLM-L6-v2).

Why all-MiniLM-L6-v2:
- 80 MB model, 384-dim embeddings — small enough to live entirely in
  RAM on a 2014 Intel laptop.
- Runs on CPU comfortably; ~5-10 ms per short string after warm-up.
- Solid general-purpose semantic similarity, multilingual-ish (good
  enough for German + English mixed assistant conversations).
- Local — no API roundtrips, no cost, no privacy footprint.

Lazy-loaded: the first :func:`encode` call triggers the model download
(~80 MB, cached under ``~/.cache/huggingface``) + load. Subsequent
calls reuse the loaded model. If the import or download fails we set
``_AVAILABLE = False`` and callers can fall back to text-only memory
(no semantic search) instead of crashing.
"""
from __future__ import annotations

import logging
import threading
from typing import Iterable

log = logging.getLogger("jarvis.memory.embeddings")

_MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"

_model = None
_model_lock = threading.Lock()
_AVAILABLE: bool | None = None      # None = not probed yet
_LOAD_ERROR: str = ""


def is_available() -> bool:
    """True iff sentence-transformers loads + we can build embeddings.

    Probes lazily on first call; cached thereafter. Safe to call from
    any thread."""
    global _AVAILABLE, _LOAD_ERROR
    if _AVAILABLE is not None:
        return _AVAILABLE
    try:
        import sentence_transformers  # noqa: F401
        _AVAILABLE = True
    except Exception as exc:  # noqa: BLE001
        _LOAD_ERROR = repr(exc)
        log.warning("sentence-transformers unavailable: %s", _LOAD_ERROR)
        _AVAILABLE = False
    return _AVAILABLE


def load_error() -> str:
    """Last import / load error, for diagnostics in /memory/stats."""
    return _LOAD_ERROR


def _get_model():
    """Load the model once; subsequent calls return the cached instance.

    The first call can take several seconds (model download or load
    from cache). Wrap in a lock so concurrent first-callers don't
    duplicate the work."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        from sentence_transformers import SentenceTransformer

        log.info("loading embedding model %s (first call may take ~10s)", _MODEL_NAME)
        _model = SentenceTransformer(_MODEL_NAME)
        # The dimension getter was renamed in sentence-transformers 5.x;
        # accept either name to stay compatible with the torch-2.2-pinned
        # transformers we're running on Intel Macs.
        dim_fn = getattr(_model, "get_embedding_dimension", None) \
              or getattr(_model, "get_sentence_embedding_dimension", None)
        log.info("embedding model ready (dim=%d)", dim_fn() if dim_fn else 384)
        return _model


def encode(text: str) -> list[float]:
    """Return a 384-dim embedding for ``text``. Empty / whitespace
    inputs return a zero vector so callers don't need to special-case
    them. Always returns a Python ``list[float]`` for portability."""
    if not is_available():
        raise RuntimeError(f"embeddings unavailable: {_LOAD_ERROR}")
    text = (text or "").strip()
    if not text:
        return [0.0] * 384
    vec = _get_model().encode(text, normalize_embeddings=True)
    return vec.tolist()


def encode_batch(texts: Iterable[str]) -> list[list[float]]:
    """Batch variant for bulk-ingest paths. Faster than calling
    :func:`encode` in a loop because the model amortises per-call
    setup costs."""
    if not is_available():
        raise RuntimeError(f"embeddings unavailable: {_LOAD_ERROR}")
    cleaned = [(t or "").strip() for t in texts]
    keep_mask = [bool(t) for t in cleaned]
    if not any(keep_mask):
        return [[0.0] * 384 for _ in cleaned]
    model = _get_model()
    non_empty = [t for t in cleaned if t]
    vecs = model.encode(non_empty, normalize_embeddings=True, show_progress_bar=False)
    vecs_iter = iter(vecs.tolist())
    return [next(vecs_iter) if keep else [0.0] * 384 for keep in keep_mask]


def dim() -> int:
    """Embedding dimensionality. Constant for a given model."""
    return 384
