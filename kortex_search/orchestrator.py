"""Search orchestrator — fan-out → fusion → dedup → re-rank → diversity → cache.

Pipeline: per-source cache → (optional) LLM query expansion → concurrent fan-out
(asyncio.wait, keeps completed sources even on timeout) → weighted RRF fusion →
dedup (URL + title + embedding) → cross-encoder re-rank → MMR diversity →
freshness filter → cache.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import math
import re
import time
from typing import Any

import numpy as np

from . import cache, llm, quality, ratelimit, stats
from .config import (
    ADAPTIVE_QUALITY,
    ADAPTIVE_TIMEOUT,
    ADAPTIVE_TIMEOUT_FACTOR,
    ADAPTIVE_TIMEOUT_MAX,
    ADAPTIVE_TIMEOUT_MIN,
    AUTH_GATED_SOURCES,
    DEFAULT_LIMIT,
    DEFAULT_SOURCES,
    EMBEDDING_DEDUP,
    EXPANSION_GATE_RESULTS,
    EXPANSION_LLM_TIMEOUT,
    FRESHNESS_FILTER,
    GLOBAL_TIMEOUT,
    MMR_ENABLED,
    MMR_LAMBDA,
    MMR_LAMBDA_BY_CATEGORY,
    PER_SOURCE_TIMEOUT,
    QUERY_EXPANSION,
    RATE_LIMIT_INTERVAL,
    RATE_LIMITED_SOURCES,
    RERANK_CANDIDATES,
    RERANK_MAX_SNIPPET,
    SEARCH_TOTAL_TIMEOUT,
    SEMANTIC_RERANK,
)
from .dedup import canonical_url, dedup
from .diversity import mmr_select
from .embeddings import cjk_dominant, encode_async
from .extract.router import tiers_for
from .fusion import rrf_fuse
from .inference import load_estimate
from .models import Result
from .rerank import rerank_async
from .sources import get_sources
from .sources.base import SourceError

logger = logging.getLogger("kortex_search.orchestrator")

_BLOCKED_RE = re.compile(r"blocked \(([^/]+)/([^)]+)\)")
_AUTH_RE = re.compile(r"auth: ([^)]*)$")

# Singleflight: concurrent searches for the same (source, query) share one
# in-flight task instead of hammering the backend N times.
_inflight: dict[tuple[str, str], asyncio.Task] = {}


async def _singleflight(source, query: str, limit: int, category: str,
                        freshness: str | None, year_from: int | None,
                        open_access_only: bool) -> tuple[str, Any]:
    """Run `_run_one` under a per-(source, query, params) in-flight dedup.

    The key covers EVERY input that shapes the outcome — the old
    (source, query) key let concurrent requests with different limits/
    categories share one task and return the wrong result count
    (bug-sweep discovery 2026-08-26).
    """
    key = (source.name, query.lower().strip(), limit, category, freshness,
           year_from, open_access_only)
    task = _inflight.get(key)
    if task is not None and not task.done():
        with contextlib.suppress(Exception):  # fall through to a fresh run
            return await asyncio.shield(task)
    task = asyncio.ensure_future(
        _run_one(source, query, limit, category, freshness, year_from,
                 open_access_only))
    _inflight[key] = task
    try:
        return await task
    finally:
        _inflight.pop(key, None)

_FRESHNESS_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}

_DATE_PATTERNS = [
    (re.compile(r"(\d{4})-(\d{2})-(\d{2})"), "%Y-%m-%d"),
    (re.compile(r"\w{3} \w{3} \d{1,2} \d{2}:\d{2}:\d{2} [+-]\d{4} (\d{4})"), "%Y"),
    # anchored: compact dates are exactly 8 digits — an epoch-millis string
    # (13 digits) must NOT match (sweep 2026-08-31: '8796090100000' parsed
    # as year 8796; epoch strings are deliberately unparseable per I10)
    (re.compile(r"^(\d{4})(\d{2})(\d{2})$"), "%Y%m%d"),
]


def _parse_date(s: str) -> dt.datetime | None:
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):  # ISO first
        # slice by the SAMPLE length, not len(fmt): the %-directives are
        # longer than the text they match ("%Y" vs "2000") — slicing by
        # len(fmt) truncated the last seconds digit (sweep 2026-08-31:
        # 'T00:00:59' parsed as 5 seconds).
        sample_len = len(dt.datetime(2000, 1, 1, 0, 0, 0).strftime(fmt))  # noqa: DTZ001 — sample text, not a real instant
        try:
            return dt.datetime.strptime(s[:sample_len], fmt)  # noqa: DTZ007 — naive by design; callers normalize tz
        except ValueError:
            continue
    for pat, _ in _DATE_PATTERNS:
        m = pat.search(s)
        if m:
            groups = m.groups()
            try:
                if len(groups) == 1:
                    return dt.datetime(int(groups[0]), 1, 1)  # noqa: DTZ001 — naive; callers normalize tz
                parsed = dt.datetime(*[int(g) for g in groups])  # noqa: DTZ001 — naive; callers normalize tz
            except (ValueError, TypeError):
                return None
            # compact dates must look like plausible published dates — an
            # 8-digit epoch-seconds value (e.g. '38300701' ≈ 1971-03) must
            # read as unparseable (I10), not as year 3830 "fresh"
            # (sweep 2026-08-31)
            if not 1970 <= parsed.year <= 2100:
                return None
            return parsed
    return None


def _filter_fresh(results: list[Result], freshness: str | None) -> list[Result]:
    if not freshness or freshness not in _FRESHNESS_DAYS or not FRESHNESS_FILTER:
        return results
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=_FRESHNESS_DAYS[freshness])
    out = []
    for r in results:
        d = _parse_date(r.published or "")
        if d is None:
            out.append(r)  # unparseable → keep exactly once (don't drop)
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.UTC)
        if d >= cutoff:
            out.append(r)
    return out


def _filter_year(results: list[Result], year_from: int | None) -> list[Result]:
    if not year_from:
        return results
    return [r for r in results
            if (r.meta.get("year") is None or r.meta.get("year") >= year_from)]


def _filter_oa(results: list[Result]) -> list[Result]:
    # keep known-OA or unknown; drop only known-closed-access
    return [r for r in results if r.meta.get("is_oa") is not False]


def _filter_key(freshness: str | None, year_from: int | None,
                open_access_only: bool) -> str:
    parts = []
    if freshness:
        parts.append(f"fr:{freshness}")
    if year_from:
        parts.append(f"yr:{year_from}")
    if open_access_only:
        parts.append("oa")
    return "|".join(parts)


async def _expand_query(query: str, budget: float = EXPANSION_LLM_TIMEOUT
                        ) -> list[str]:
    """LLM query expansion → up to 2 alternative search phrasings.

    `budget` bounds the LLM leg: expansion is a latency luxury, so a slow
    completion degrades to "no variants" instead of holding the pipeline
    (was: unbounded behind the 60s client timeout — sweep 2026-09-03).
    """
    if not QUERY_EXPANSION or not llm.available():
        return []
    try:
        prompt = (
            "Generate 2 alternative search queries that would surface different "
            "relevant information about the SAME topic as the query below. "
            "Each query must be a complete meaningful phrase of 3 or more words. "
            "Do NOT output single keywords, fragments, or the original query. "
            "Output exactly 2 lines, nothing else.\n\n"
            f"Query: {query}"
        )
        out = await asyncio.wait_for(
            llm.complete([{"role": "user", "content": prompt}],
                         max_tokens=150, temperature=0.2, thinking=False),
            timeout=budget,
        )
        variants = []
        for ln in out.splitlines():
            v = ln.strip().lstrip("-•1234567890. ").strip()
            words = v.split()
            # require a meaningful multi-word phrase
            if len(words) >= 3 and v.lower() != query.lower():
                variants.append(v)
        return variants[:2]
    except TimeoutError:
        logger.warning("query expansion timed out after %.0fs", budget)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("query expansion failed: %s", exc)
        return []


def _adaptive_timeout(name: str, fallback: float = PER_SOURCE_TIMEOUT) -> float:
    """min(p95(source) x factor, cap) with a floor; fallback when unknown.

    Non-finite percentiles (poisoned reservoir) fall back too — a nan
    timeout would flow into wait_for/sleep with undefined behavior.
    """
    if not ADAPTIVE_TIMEOUT:
        return fallback
    p95 = stats.latency_percentiles(name).get("p95_s", 0.0)
    if not math.isfinite(p95) or p95 <= 0:
        return fallback
    return max(ADAPTIVE_TIMEOUT_MIN, min(p95 * ADAPTIVE_TIMEOUT_FACTOR,
                                         ADAPTIVE_TIMEOUT_MAX))


def _expansion_needed(base_total: int, gate: int = EXPANSION_GATE_RESULTS) -> bool:
    return base_total < gate


def _extract_signals(statuses: dict[str, Any]) -> tuple[dict, dict]:
    """Parse outcome strings into envelope signals (v0.4).

    Source outcomes already carry the machine-readable facts: `blocked
    (vendor/level)` and `auth: reason`. This turns them into structured
    envelope fields — the envelope names the state, never guesses it.

    Block telemetry: raising sources record the event in
    `sources.base._blocked_error` (the raise site). Outcomes here that are
    blocked strings but NOT error-prefixed came from a non-raising path, so
    they are recorded here — the two sites never record the same event twice.
    """
    blocked: list[dict] = []
    auth: dict[str, str] = {}
    for name, outcome in statuses.items():
        if isinstance(outcome, list):
            if name in AUTH_GATED_SOURCES:
                auth[name] = "ok"
            continue
        if not isinstance(outcome, str):
            continue
        m = _BLOCKED_RE.search(outcome)
        if m:
            blocked.append({"source": name, "vendor": m.group(1),
                            "level": m.group(2)})
            if not outcome.startswith("error:"):
                stats.record_block(name, m.group(1), m.group(2))
        if outcome.startswith("auth:"):
            auth[name] = "missing"
        elif name in AUTH_GATED_SOURCES:
            if outcome.startswith("ok"):
                auth[name] = "ok"
            elif outcome.startswith("error") or outcome == "pending (timeout)":
                auth[name] = "unknown"
    return blocked, auth


def _extract_tiers(source_names: list[str]) -> dict[str, dict]:
    """Which extraction tier each requested source declares (api/cli/browser)."""
    return {n: {"tier": tiers_for(n)[0]} for n in source_names}


def _category_lambda(category: str) -> float:
    return MMR_LAMBDA_BY_CATEGORY.get(category, MMR_LAMBDA)


async def _run_one(source, query: str, limit: int, category: str,
                   freshness: str | None, year_from: int | None = None,
                   open_access_only: bool = False) -> tuple[str, Any]:
    """Run a single source: rate-limit → per-source cache → search → stats.

    The search call is wrapped in an ADAPTIVE per-source timeout:
    `min(p95(source) * factor, cap)` — stragglers die early, healthy sources
    get headroom. Unknown sources use the static PER_SOURCE_TIMEOUT.
    """
    name = source.name
    fkey = _filter_key(freshness, year_from, open_access_only)
    try:
        if cache.source_recently_failed(name, query, category):
            return name, "skipped (recent failure)"

        if name in RATE_LIMITED_SOURCES:
            await ratelimit.wait_if_needed(name, RATE_LIMIT_INTERVAL)
        await ratelimit.enforce_daily_budget(name)

        cached = cache.get_source(name, query, category, limit=limit, filters=fkey)
        if cached is not None:
            return name, [Result(**d) for d in cached]

        timeout = _adaptive_timeout(name)

        t0 = time.monotonic()
        if name == "searxng":
            coro = source.search(query, limit=limit, category=category,
                                 freshness=freshness)
        elif name == "openalex":
            coro = source.search(query, limit=limit, year_from=year_from,
                                 open_access_only=open_access_only)
        elif name == "crossref":
            coro = source.search(query, limit=limit, year_from=year_from)
        else:
            coro = source.search(query, limit=limit)
        try:
            results = await asyncio.wait_for(coro, timeout=timeout)
        except TimeoutError as exc:
            raise SourceError(
                f"timeout ({timeout:.1f}s, adaptive): {name}"
            ) from exc
        elapsed = time.monotonic() - t0

        if not isinstance(results, list):
            results = []
        # enforce the standardized source_type on every result (backward-compat)
        for r in results:
            if isinstance(r, Result):
                r.meta.setdefault("source_type", source.source_type)
        if freshness:
            results = _filter_fresh(results, freshness)
        if year_from and name not in ("openalex", "crossref"):
            results = _filter_year(results, year_from)
        if open_access_only and name != "openalex":
            results = _filter_oa(results)

        cache.set_source(name, query, category, [r.to_dict() for r in results],
                         limit=limit, filters=fkey)
        stats.record(name, True, elapsed)
        return name, results
    except SourceError as exc:
        stats.record_error(name)
        cache.mark_source_failed(name, query, category)
        return name, f"error: {exc}"
    except Exception as exc:  # noqa: BLE001
        stats.record_error(name)
        cache.mark_source_failed(name, query, category)
        return name, f"error: {type(exc).__name__}: {exc}"


async def search(query: str, sources: list[str] | None, category: str = "general",
                 limit: int = DEFAULT_LIMIT, freshness: str | None = None,
                 expand: bool = QUERY_EXPANSION, year_from: int | None = None,
                 open_access_only: bool = False, *,
                 deadline: float | None = None) -> dict[str, Any]:
    start = time.monotonic()
    # Every leg runs under one end-to-end deadline so a search fits inside
    # the MCP client's request budget (sweep 2026-09-03: the historical
    # worst case ran 150s+ → client timeouts and, under concurrency,
    # process death from the frozen event loop). v0.9 extends the deadline
    # to the POST-fanout stages: inference legs are bounded too, degrading
    # instead of queueing (see quality.py). A caller-supplied absolute
    # `deadline` (monotonic seconds) overrides the default budget —
    # research_answer uses it to fit search + synthesis into one tool call.
    end_deadline = deadline if deadline is not None else start + SEARCH_TOTAL_TIMEOUT
    end_deadline = max(end_deadline, start + 1.0)

    def _remaining(floor: float = 1.0) -> float:
        return max(floor, end_deadline - time.monotonic())

    source_names = list(sources or DEFAULT_SOURCES)  # defensive copy of the default
    fkey = _filter_key(freshness, year_from, open_access_only)

    cached = cache.get(query, source_names, category, limit, filters=fkey)
    if cached is not None:
        return {"query": query, "results": cached, "count": len(cached),
                "sources": {}, "cached": True,
                "elapsed_ms": int((time.monotonic() - start) * 1000),
                "extract": _extract_tiers(source_names),
                "blocked": [], "auth": {},
                "quality": {"tier": 0, "label": "cached",
                            "rerank_candidates": 0, "snippet_cap": 0},
                "degraded": []}

    objs = get_sources(source_names)
    statuses: dict[str, Any] = {}
    ranked_lists: list[list[Result]] = []

    # Phase 1: base fan-out (original query on all requested sources).
    tasks = {asyncio.ensure_future(
        _singleflight(s, query, limit, category, freshness, year_from,
                      open_access_only)
    ): (s.name, s.name)
             for s in objs}
    done, pending = await asyncio.wait(
        tasks, timeout=min(GLOBAL_TIMEOUT, _remaining()))

    for fut in done:
        label, name = tasks[fut]
        try:
            _, outcome = fut.result()
        except Exception as exc:  # noqa: BLE001
            outcome = f"error: {type(exc).__name__}: {exc}"
        if isinstance(outcome, list):
            if outcome:
                ranked_lists.append(outcome)
            if label:
                statuses[name] = f"ok ({len(outcome)})"
        else:
            if label:
                statuses[name] = outcome

    pending_names = []
    for fut in pending:
        label, name = tasks[fut]
        if label:
            pending_names.append(name)
            statuses[name] = "pending (timeout)"
        fut.cancel()

    # Phase 2: expansion fan-out ONLY when the base results are weak AND the
    # caller did not pin sources. Expanding into searxng/exa for an explicit
    # `sources=[...]` request would silently lie about provenance — the
    # envelope's `sources`/`extract` fields must name every source that
    # contributed (smoke-test discovery 2026-08-25). The expansion legs also
    # stop once the end-to-end deadline is near: variants are worth ~10s of
    # extra latency, not the full fan-out budget.
    base_total = sum(len(rl) for rl in ranked_lists)
    expansion_deadline = end_deadline - 15.0
    if (expand and sources is None and _expansion_needed(base_total)
            and time.monotonic() < expansion_deadline):
        variants = await _expand_query(
            query, budget=min(EXPANSION_LLM_TIMEOUT,
                              max(1.0, end_deadline - time.monotonic())))
        if variants:
            tasks2 = {asyncio.ensure_future(
                _singleflight(s, v, limit, category, freshness, year_from,
                              open_access_only)
            ): ("", s.name)
                      for v in variants for s in get_sources(["searxng", "exa"])}
            done2, pending2 = await asyncio.wait(
                tasks2, timeout=min(GLOBAL_TIMEOUT, _remaining()))
            for fut in done2:
                _, outcome = fut.result()
                if isinstance(outcome, list) and outcome:
                    ranked_lists.append(outcome)
            for fut in pending2:
                fut.cancel()

    # fusion (weighted RRF + exact dedup) → near-dup dedup (embedding)
    t_fusion = time.monotonic()
    fused = rrf_fuse(ranked_lists)
    # snippet may be None on malformed source output — never let fusion crash
    dedup_docs = [(r.title + " " + (r.snippet or "")[:200]) for r in fused]
    multilingual = cjk_dominant(dedup_docs)
    degraded: list[str] = []

    emb_for_dedup = None
    if EMBEDDING_DEDUP and len(fused) > 1:
        try:
            # encode runs off-loop on the embed executor; bounded by the
            # per-search deadline (v0.9) — on expiry dedup degrades to
            # URL/title layers instead of holding the pipeline hostage.
            emb_for_dedup = await asyncio.wait_for(
                encode_async(dedup_docs, multilingual=multilingual),
                timeout=_remaining())
        except TimeoutError:
            degraded.append("dedup_embed")
    # keep doc vectors keyed by dedup identity so MMR can REUSE them instead
    # of running a second encode pass (v0.9: halves per-search embed work)
    vec_by_ckey: dict[str, np.ndarray] = {}
    if emb_for_dedup is not None and len(emb_for_dedup) == len(fused):
        for r, v in zip(fused, emb_for_dedup, strict=False):
            ckey = canonical_url(r.identity())
            if ckey:
                vec_by_ckey[ckey] = v
    fused = dedup(fused, embeddings=emb_for_dedup)
    t_dedup = time.monotonic()

    # semantic re-rank the top candidates (adaptive quality ladder, v0.9):
    # full tier when idle, cheaper tiers as the inference queue backs up,
    # RRF order when saturated. Every path is bounded by the deadline.
    reranked = None
    tier = quality.SKIP_TIER
    if SEMANTIC_RERANK and len(fused) > limit:
        load = load_estimate()
        if ADAPTIVE_QUALITY:
            tier = quality.pick_tier(
                remaining=_remaining(), fused=len(fused), limit=limit,
                candidates_ceiling=RERANK_CANDIDATES,
                snippet_ceiling=RERANK_MAX_SNIPPET,
                pending_seconds=load.get("pending_seconds", 0.0))
        else:
            tier = quality.Tier(0, min(RERANK_CANDIDATES, len(fused)),
                                RERANK_MAX_SNIPPET, "full")
        if tier.candidates:
            cands = fused[:tier.candidates]
            est = quality.estimate_rerank(len(cands), tier.snippet_cap)
            t_rr = time.monotonic()
            try:
                reranked = await asyncio.wait_for(
                    rerank_async(query, cands, snippet_cap=tier.snippet_cap,
                                 cost=est),
                    timeout=_remaining())
                quality.observe_rerank(len(cands), tier.snippet_cap,
                                       time.monotonic() - t_rr)
            except TimeoutError:
                degraded.append("rerank")
        else:
            degraded.append("rerank")
    if reranked is None:
        reranked = fused
    t_rerank = time.monotonic()

    # MMR diversity on the re-ranked candidates (per-category λ). Embeddings
    # are reused from the dedup pass (v0.9) — no encode, nothing to bound.
    if MMR_ENABLED and len(reranked) > limit:
        emb_for_mmr = None
        if vec_by_ckey:
            vecs = [vec_by_ckey.get(canonical_url(r.identity()))
                    for r in reranked]
            if all(v is not None for v in vecs):
                emb_for_mmr = np.stack(vecs)
        final = mmr_select(reranked, emb_for_mmr, limit,
                           lam=_category_lambda(category))
    else:
        final = reranked[:limit]
    t_mmr = time.monotonic()

    result_dicts = [r.to_dict() for r in final]
    cache.set(query, source_names, category, limit, result_dicts, filters=fkey)

    blocked, auth = _extract_signals(statuses)

    if not SEMANTIC_RERANK:
        quality_block = {"tier": 0, "label": "disabled",
                         "rerank_candidates": 0, "snippet_cap": 0}
    elif tier is quality.SKIP_TIER and len(fused) <= limit:
        quality_block = {"tier": 0, "label": "not-needed",
                         "rerank_candidates": 0, "snippet_cap": 0}
    else:
        quality_block = {"tier": tier.level, "label": tier.label,
                         "rerank_candidates": tier.candidates,
                         "snippet_cap": tier.snippet_cap}

    return {
        "query": query,
        "results": result_dicts,
        "count": len(result_dicts),
        "sources": statuses,
        "cached": False,
        "reranked": SEMANTIC_RERANK,
        "partial": bool(pending_names),
        "pending": pending_names,
        "elapsed_ms": int((time.monotonic() - start) * 1000),
        "stage_ms": {
            "fanout": int((t_fusion - start) * 1000),
            "fusion_dedup": int((t_dedup - t_fusion) * 1000),
            "rerank": int((t_rerank - t_dedup) * 1000),
            "mmr": int((t_mmr - t_rerank) * 1000),
            "total": int((time.monotonic() - start) * 1000),
        },
        "quality": quality_block,
        "degraded": degraded,
        # v0.4 extraction-layer signals (additive, optional for clients)
        "extract": _extract_tiers(source_names),
        "blocked": blocked,
        "auth": auth,
    }
