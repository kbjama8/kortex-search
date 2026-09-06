"""Semantic re-ranking with a local cross-encoder (CPU).

Lazily loads BAAI/bge-reranker-v2-m3 (multilingual, EN+ZH) — the best
cross-encoder that fits a CPU-only 16 GB machine. The model is a singleton so
it is loaded once per process; re-ranking is applied only to the top RRF
candidates (never the full result set) to bound CPU latency.
"""

from __future__ import annotations

import logging

from .config import (
    _ONNX_FILE,
    INFERENCE_BACKEND,
    RERANK_MODEL,
    RERANK_ONNX_MODEL,
    RERANK_ONNX_REVISION,
    RERANK_REVISION,
    SEMANTIC_RERANK,
)
from .inference import run_inference
from .models import Result

logger = logging.getLogger("kortex_search.rerank")

_model: object | None = None
_model_error: str | None = None
_effective_model: str = (RERANK_ONNX_MODEL if INFERENCE_BACKEND in _ONNX_FILE
                         else RERANK_MODEL)


def _get_model():
    global _model, _model_error, _effective_model
    if _model is not None or _model_error is not None:
        return _model

    def _load_torch() -> object:
        from sentence_transformers import CrossEncoder
        kwargs = {"revision": RERANK_REVISION} if RERANK_REVISION else {}
        return CrossEncoder(RERANK_MODEL, **kwargs)

    try:
        from sentence_transformers import CrossEncoder
        if INFERENCE_BACKEND in _ONNX_FILE:
            fname = _ONNX_FILE[INFERENCE_BACKEND]
            _effective_model = RERANK_ONNX_MODEL
            logger.info("loading cross-encoder %s (backend=%s, %s) ...",
                        RERANK_ONNX_MODEL, INFERENCE_BACKEND, fname)
            kwargs = {"revision": RERANK_ONNX_REVISION} if RERANK_ONNX_REVISION else {}
            _model = CrossEncoder(RERANK_ONNX_MODEL, backend="onnx",
                                  model_kwargs={"file_name": fname}, **kwargs)
        else:
            logger.info("loading cross-encoder %s ...", RERANK_MODEL)
            _model = _load_torch()
        logger.info("cross-encoder loaded")
    except Exception as exc:  # noqa: BLE001 — never crash the search
        if INFERENCE_BACKEND in _ONNX_FILE:
            # ONNX unavailable (missing `.[onnx]` extra, model not cached) —
            # fall back to torch rather than silently dropping re-rank.
            logger.warning("ONNX cross-encoder load failed (%s); falling back to torch", exc)
            try:
                _effective_model = RERANK_MODEL
                _model = _load_torch()
                logger.info("cross-encoder loaded (torch fallback)")
            except Exception as exc2:  # noqa: BLE001
                _model_error = f"{type(exc2).__name__}: {exc2}"
                logger.error("cross-encoder load failed: %s", _model_error)
                _model = None
        else:
            _model_error = f"{type(exc).__name__}: {exc}"
            logger.error("cross-encoder load failed: %s", _model_error)
            _model = None
    return _model


def rerank(query: str, candidates: list[Result], top_k: int | None = None,
           snippet_cap: int = 512) -> list[Result]:
    """Re-rank candidates by query-document relevance. Falls back to input
    order (RRF) if the model is unavailable or disabled. `top_k` optionally
    truncates (else returns the full re-ranked list, so MMR can diversify).
    `snippet_cap` bounds each pair's document text — the adaptive quality
    ladder scales rerank cost with it (cost is linear in pairs x chars)."""
    if not candidates or not SEMANTIC_RERANK:
        out = list(candidates)
        return out[:top_k] if top_k else out

    model = _get_model()
    if model is None:
        out = list(candidates)
        return out[:top_k] if top_k else out

    try:
        pairs = [(query, (r.snippet or r.title)[:snippet_cap]) for r in candidates]
        scores = model.predict(pairs)
        for r, s in zip(candidates, scores, strict=False):
            r.score = float(s)
        ranked = sorted(candidates, key=lambda r: r.score, reverse=True)
        return ranked[:top_k] if top_k else ranked
    except Exception as exc:  # noqa: BLE001
        logger.error("re-rank failed: %s", exc)
        out = list(candidates)
        return out[:top_k] if top_k else out


async def rerank_async(query: str, candidates: list[Result],
                       top_k: int | None = None, snippet_cap: int = 512,
                       cost: float | None = None) -> list[Result]:
    """Event-loop-safe `rerank`: lazy model load + predict run on the
    dedicated rerank executor (see inference.py). `cost` feeds the adaptive
    quality controller's load estimate."""
    return await run_inference(rerank, query, candidates, top_k,
                               kind="rerank", cost=cost,
                               snippet_cap=snippet_cap)


def status() -> dict:
    return {"enabled": SEMANTIC_RERANK, "backend": INFERENCE_BACKEND,
            "model": _effective_model, "loaded": _model is not None,
            "error": _model_error}
