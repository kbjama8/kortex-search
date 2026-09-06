"""Off-loop model inference shim (sweep 2026-09-03) + load tracking (v0.9).

The cross-encoder re-ranker and the bi-encoder embedders are CPU-bound and
lazy-loaded. Running them synchronously inside an async tool handler froze
the MCP event loop for tens of seconds (imports + model loads + batches).
Phase-0 reproduction measured a single 16.9 s blocking stretch; concurrent
requests queued behind the freeze and the MCP stdio session unwound,
killing the whole server process with a clean rc=0 mid-request.

Every heavy call therefore runs on dedicated executors:

  * the event loop stays live — pings, cancellations, and other tool calls
    are serviced while a batch runs (client timeouts become per-request
    errors, never process death);
  * two SINGLE-worker executors, split by job kind (v0.9): embeds
    (~0.2 s) ride their own worker so cheap dedup/MMR stages never queue
    behind a multi-second rerank. Each executor serializes its kind, which
    also singleflights lazy model loads (concurrent first calls queue on
    the worker instead of loading the same model N times);
  * reranks are NOT parallelized across workers on purpose — measured
    2026-09-05: concurrent ONNX predicts contend on the CPU pool and the
    queue drains SLOWER than serialized (see quality.py);
  * on client cancellation the executor thread keeps running and its result
    is discarded — safe, since the models are process singletons anyway.

v0.9 adds load accounting so the adaptive quality controller (quality.py)
can size its work against the queue: every submitted job registers its kind
and estimated cost; actual durations feed per-kind rolling averages.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

_embed_executor: ThreadPoolExecutor | None = None
_rerank_executor: ThreadPoolExecutor | None = None

DEFAULT_COST = {"embed": 0.2, "rerank": 2.0}
DURATION_WINDOW = 64

_state_lock = threading.Lock()
_pending: list[tuple[int, str, float]] = []  # (job_id, kind, est_cost)
_inflight: dict[int, tuple[str, float]] = {}  # job_id -> (kind, started_at)
_durations: dict[str, deque[float]] = {
    "embed": deque(maxlen=DURATION_WINDOW),
    "rerank": deque(maxlen=DURATION_WINDOW),
}
_job_seq = itertools.count()


def _get_executor(kind: str) -> ThreadPoolExecutor:
    global _embed_executor, _rerank_executor
    if kind == "rerank":
        if _rerank_executor is None:
            _rerank_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ks-rerank")
        return _rerank_executor
    if _embed_executor is None:
        _embed_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ks-embed")
    return _embed_executor


async def run_inference(fn, *args, kind: str = "embed", cost: float | None = None, **kwargs):
    """Run a CPU-bound model call off the event loop (serialized per kind).

    `kind` selects the executor ("embed" or "rerank"). `cost` is the caller's
    estimate in seconds (feeds load_estimate's pending_seconds); omitted
    calls fall back to the kind's rolling average, then DEFAULT_COST.
    Model loads happen inside the worker on first use, so the import +
    download/load cost never blocks the loop either.
    """
    loop = asyncio.get_running_loop()
    job_id = next(_job_seq)
    with _state_lock:
        est = cost
        if est is None:
            d = _durations[kind]
            est = (sum(d) / len(d)) if d else DEFAULT_COST[kind]
        _pending.append((job_id, kind, est))

    def _run():
        with _state_lock:
            for i, (jid, _, _) in enumerate(_pending):
                if jid == job_id:
                    _pending.pop(i)
                    break
            _inflight[job_id] = (kind, time.monotonic())
        try:
            return fn(*args, **kwargs)
        finally:
            with _state_lock:
                started = _inflight.pop(job_id, (kind, time.monotonic()))[1]
                _durations[kind].append(time.monotonic() - started)

    return await loop.run_in_executor(_get_executor(kind), _run)


def load_estimate() -> dict:
    """Pending/inflight inference work — the adaptive controller's load signal.

    Returns a dict with: pending_jobs (queued, not yet executing),
    pending_seconds (sum of queued cost estimates), inflight_jobs,
    avg_embed_s / avg_rerank_s (rolling means of completed jobs), and
    n_embed / n_rerank (completed-job counts in the window).
    """
    with _state_lock:
        pending_jobs = len(_pending)
        pending_seconds = sum(est for _, _, est in _pending)
        inflight_jobs = len(_inflight)
        avg_embed = (
            sum(_durations["embed"]) / len(_durations["embed"]) if _durations["embed"] else 0.0
        )
        avg_rerank = (
            sum(_durations["rerank"]) / len(_durations["rerank"]) if _durations["rerank"] else 0.0
        )
        n_embed = len(_durations["embed"])
        n_rerank = len(_durations["rerank"])
    return {
        "pending_jobs": pending_jobs,
        "pending_seconds": round(pending_seconds, 3),
        "inflight_jobs": inflight_jobs,
        "avg_embed_s": round(avg_embed, 3),
        "avg_rerank_s": round(avg_rerank, 3),
        "n_embed": n_embed,
        "n_rerank": n_rerank,
    }
