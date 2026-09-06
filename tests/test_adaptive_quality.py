"""Adaptive quality controller + inference queue split (v0.9).

Pins the burst-timeout fix:
  1. quality.pick_tier picks the highest rerank tier affordable within the
     remaining search budget — full quality idle, graceful degradation busy;
  2. hysteresis: downgrade fast on a load spike, upgrade only after the load
     window clears (no oscillation);
  3. the rerank cost model self-calibrates from observed predict durations;
  4. inference runs on TWO serialized executors (embed / rerank) so cheap
     embed jobs never wait behind a multi-second rerank;
  5. load_estimate reports pending/inflight inference work for both the
     controller and stats_report;
  6. orchestrator: every inference stage is deadline-bounded and degrades
     honestly (RRF order / URL dedup / top-N) with `degraded` reporting;
  7. orchestrator: tier selection skips rerank entirely when the queue is
     saturated;
  8. search accepts an explicit caller deadline;
  9. research_answer fits a total tool budget (search + synthesis).
"""

from __future__ import annotations

import asyncio
import threading
import time

import kortex_search.inference as inference
import kortex_search.quality as quality

# --------------------------------------------------------------------------
# 1. tier picking
# --------------------------------------------------------------------------


def test_pick_tier_idle_returns_full_quality():
    quality.reset()
    t = quality.pick_tier(
        remaining=40.0,
        fused=25,
        limit=10,
        candidates_ceiling=30,
        snippet_ceiling=512,
        pending_seconds=0.0,
    )
    assert t.level == 0
    assert t.candidates == 25  # clamped by fused
    assert t.snippet_cap == 512
    assert t.label == "full"


def test_pick_tier_degrades_monotonically_with_load():
    quality.reset()
    prev = -1
    for load in (0.0, 2.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0):
        t = quality.pick_tier(
            remaining=25.0,
            fused=25,
            limit=10,
            candidates_ceiling=30,
            snippet_ceiling=512,
            pending_seconds=load,
        )
        assert t.level >= prev, f"load {load}: tier went UP ({prev} -> {t.level})"
        prev = t.level
    assert prev == 4  # saturated by the end


def test_pick_tier_saturated_skips_rerank():
    quality.reset()
    t = quality.pick_tier(
        remaining=3.0,
        fused=25,
        limit=10,
        candidates_ceiling=30,
        snippet_ceiling=512,
        pending_seconds=20.0,
    )
    assert t.level == 4
    assert t.candidates == 0


def test_pick_tier_respects_ceilings():
    quality.reset()
    t = quality.pick_tier(
        remaining=40.0,
        fused=25,
        limit=10,
        candidates_ceiling=12,
        snippet_ceiling=200,
        pending_seconds=0.0,
    )
    assert t.level == 0
    assert t.candidates == 12
    assert t.snippet_cap == 200


def test_pick_tier_skips_when_fused_not_above_limit():
    quality.reset()
    t = quality.pick_tier(
        remaining=40.0,
        fused=5,
        limit=10,
        candidates_ceiling=30,
        snippet_ceiling=512,
        pending_seconds=0.0,
    )
    assert t.candidates == 0


def test_hysteresis_downgrades_fast_and_upgrades_slow():
    quality.reset()
    # idle → full quality
    t = quality.pick_tier(
        remaining=15.0,
        fused=25,
        limit=10,
        candidates_ceiling=30,
        snippet_ceiling=512,
        pending_seconds=0.0,
    )
    assert t.level == 0
    # one load spike → immediate downgrade
    t = quality.pick_tier(
        remaining=15.0,
        fused=25,
        limit=10,
        candidates_ceiling=30,
        snippet_ceiling=512,
        pending_seconds=15.0,
    )
    assert t.level > 0
    # load clears for ONE call → must NOT jump straight back to full
    t = quality.pick_tier(
        remaining=15.0,
        fused=25,
        limit=10,
        candidates_ceiling=30,
        snippet_ceiling=512,
        pending_seconds=0.0,
    )
    assert t.level > 0
    # sustained idle (window ages out) → recovers to full quality
    for _ in range(12):
        t = quality.pick_tier(
            remaining=15.0,
            fused=25,
            limit=10,
            candidates_ceiling=30,
            snippet_ceiling=512,
            pending_seconds=0.0,
        )
    assert t.level == 0


# --------------------------------------------------------------------------
# 2. cost model self-calibration
# --------------------------------------------------------------------------


def test_cost_model_calibrates_from_observation():
    m = quality.CostModel(k=0.00025, c0=0.5, learn_rate=0.25)
    k0, _c00 = m.k, m.c0
    # measured: 30 pairs x 512 chars took 6.6s — slower than the seed predicts
    m.observe_rerank(30, 512, 6.6)
    assert m.k > k0, "k must move toward the observed slope"
    # after calibration the model must roughly match the observation
    est = m.rerank_cost(30, 512)
    assert abs(est - 6.6) < 3.0


def test_cost_model_ignores_degenerate_observations():
    m = quality.CostModel(k=0.00025, c0=0.5)
    m.observe_rerank(0, 512, 10.0)
    m.observe_rerank(30, 0, 10.0)
    m.observe_rerank(30, 512, -1.0)
    assert m.k == 0.00025 and m.c0 == 0.5


# --------------------------------------------------------------------------
# 3. two executors + load estimate
# --------------------------------------------------------------------------


def test_embed_job_not_blocked_by_slow_rerank():
    """An embed job must not wait behind a multi-second rerank (the old
    single shared worker serialized both — the burst's cheap stages queued
    behind expensive reranks)."""

    def slow_rerank():
        time.sleep(1.5)
        return "r"

    async def run():
        rt = asyncio.create_task(inference.run_inference(slow_rerank, kind="rerank"))
        await asyncio.sleep(0.3)  # rerank is executing on its worker now
        t0 = time.monotonic()
        e = await inference.run_inference(lambda: "e", kind="embed")
        el = time.monotonic() - t0
        r = await rt
        return el, r, e

    el, r, e = asyncio.run(run())
    assert e == "e" and r == "r"
    assert el < 1.0, f"embed waited {el:.2f}s behind the rerank"


def test_load_estimate_reports_pending_and_inflight():
    started = threading.Event()
    release = threading.Event()

    def slow_rerank():
        started.set()
        release.wait(2.0)
        return 1

    async def run():
        job = asyncio.create_task(inference.run_inference(slow_rerank, kind="rerank", cost=5.0))
        await asyncio.sleep(0.05)  # let the loop submit the job first
        assert started.wait(2.0), "worker never picked up the job"
        load = inference.load_estimate()
        assert load["inflight_jobs"] >= 1, load
        assert load["pending_jobs"] == 0, load
        job2 = asyncio.create_task(inference.run_inference(lambda: 2, kind="rerank", cost=3.0))
        await asyncio.sleep(0.2)
        load2 = inference.load_estimate()
        assert load2["pending_jobs"] >= 1, load2
        assert load2["pending_seconds"] >= 2.0, load2
        release.set()
        await asyncio.gather(job, job2)
        final = inference.load_estimate()
        return load, load2, final

    _load, _load2, final = asyncio.run(run())
    assert final["pending_jobs"] == 0 and final["inflight_jobs"] == 0, final


# --------------------------------------------------------------------------
# 4. orchestrator: deadline-bounded stages + tier-driven skip
# --------------------------------------------------------------------------


def _search_ctx(monkeypatch):
    import kortex_search.orchestrator as omod

    monkeypatch.setattr(omod, "QUERY_EXPANSION", False)
    monkeypatch.setattr(omod, "MMR_ENABLED", False)
    monkeypatch.setattr(omod, "EMBEDDING_DEDUP", False)


class FatSource:
    """FakeSource variant that ignores `limit` — real sources return up to
    their own caps, so fused counts routinely exceed the request limit and
    trigger the rerank branch (the stock FakeSource truncates to `limit`,
    which silently disables rerank in pipeline tests)."""

    def __init__(self, name, results, source_type="web", delay=0.0):
        from kortex_search.models import Result

        self.name = name
        self._results = results
        self.source_type = source_type
        self._delay = delay
        self._Result = Result

    async def search(self, query, limit=10, **kwargs):
        if self._delay:
            await asyncio.sleep(self._delay)
        return [self._Result(**{**r, "source": self.name}) for r in self._results]


def test_rerank_stage_deadline_degrades_to_rrf(monkeypatch, rds):
    """A rerank slower than the remaining budget must be skipped — the
    search returns RRF-ordered results instead of hanging past the deadline."""
    import kortex_search.orchestrator as orch
    from tests.test_pipeline import _mk

    _search_ctx(monkeypatch)
    monkeypatch.setattr(orch, "SEMANTIC_RERANK", True)
    monkeypatch.setattr(orch, "SEARCH_TOTAL_TIMEOUT", 1.5)
    monkeypatch.setattr(orch, "GLOBAL_TIMEOUT", 60)

    async def slow_rerank_async(query, candidates, top_k=None, snippet_cap=512, cost=None):
        await asyncio.sleep(5.0)
        return candidates

    monkeypatch.setattr(orch, "rerank_async", slow_rerank_async)

    results = [_mk(f"T{i}", f"https://a.com/{i}") for i in range(20)]
    s1 = FatSource("s1", results)
    monkeypatch.setattr(orch, "get_sources", lambda names: [s1])

    async def run():
        t0 = time.monotonic()
        out = await orch.search("slow rerank", ["s1"], limit=5)
        return out, time.monotonic() - t0

    out, elapsed = asyncio.run(run())
    assert elapsed < 4.0, f"search ignored the deadline ({elapsed:.1f}s)"
    assert out["count"] == 5
    assert "rerank" in out["degraded"]


def test_dedup_embed_deadline_degrades_to_url_dedup(monkeypatch, rds):
    """A slow dedup embed must degrade to URL/title dedup — results still
    flow, the search returns within its deadline, and the skip is reported."""
    import kortex_search.orchestrator as orch
    from tests.test_pipeline import FakeSource, _mk

    _search_ctx(monkeypatch)
    monkeypatch.setattr(orch, "EMBEDDING_DEDUP", True)
    monkeypatch.setattr(orch, "SEARCH_TOTAL_TIMEOUT", 1.5)
    monkeypatch.setattr(orch, "GLOBAL_TIMEOUT", 60)

    async def slow_encode_async(texts, multilingual=False):
        await asyncio.sleep(5.0)
        return None

    monkeypatch.setattr(orch, "encode_async", slow_encode_async)

    # distinct URLs so fusion keeps both (exact-URL dupes collapse in
    # rrf_fuse, before the embedding-dedup stage this test targets)
    s1 = FakeSource("s1", [_mk("Alpha", "https://x.com/1")])
    s2 = FakeSource("s2", [_mk("Beta", "https://y.com/2")])
    monkeypatch.setattr(orch, "get_sources", lambda names: [s1, s2])

    async def run():
        t0 = time.monotonic()
        out = await orch.search("slow dedup", ["s1", "s2"], limit=5)
        return out, time.monotonic() - t0

    out, elapsed = asyncio.run(run())
    assert elapsed < 4.0, f"search ignored the deadline ({elapsed:.1f}s)"
    assert out["count"] == 2  # both results survived the embed-less dedup
    assert "dedup_embed" in out["degraded"]


def test_tier_selection_skips_rerank_under_saturated_load(monkeypatch, rds):
    """When the inference queue is saturated, rerank must be skipped BEFORE
    submission — no queueing, honest degradation, RRF order served."""
    import kortex_search.orchestrator as orch
    from tests.test_pipeline import _mk

    _search_ctx(monkeypatch)
    quality.reset()
    monkeypatch.setattr(orch, "SEMANTIC_RERANK", True)
    monkeypatch.setattr(orch, "ADAPTIVE_QUALITY", True)
    monkeypatch.setattr(orch, "SEARCH_TOTAL_TIMEOUT", 45)
    monkeypatch.setattr(
        orch,
        "load_estimate",
        lambda: {"pending_jobs": 8, "pending_seconds": 40.0, "inflight_jobs": 1},
    )

    calls = []

    async def fake_rerank_async(query, candidates, top_k=None, snippet_cap=512, cost=None):
        calls.append(len(candidates))
        return candidates

    monkeypatch.setattr(orch, "rerank_async", fake_rerank_async)

    results = [_mk(f"T{i}", f"https://a.com/{i}") for i in range(20)]
    s1 = FatSource("s1", results)
    monkeypatch.setattr(orch, "get_sources", lambda names: [s1])

    async def run():
        return await orch.search("saturated queue", ["s1"], limit=5)

    out = asyncio.run(run())
    assert calls == [], f"rerank ran despite saturation: {calls}"
    assert out["count"] == 5
    assert out["quality"]["tier"] == 4
    assert "rerank" in out["degraded"]


def test_tier_selection_full_quality_when_idle(monkeypatch, rds):
    """Idle gateway → full tier: rerank gets max candidates and full-length
    snippets."""
    import kortex_search.orchestrator as orch
    from tests.test_pipeline import _mk

    _search_ctx(monkeypatch)
    quality.reset()
    monkeypatch.setattr(orch, "SEMANTIC_RERANK", True)
    monkeypatch.setattr(orch, "ADAPTIVE_QUALITY", True)
    monkeypatch.setattr(orch, "SEARCH_TOTAL_TIMEOUT", 45)
    monkeypatch.setattr(
        orch,
        "load_estimate",
        lambda: {"pending_jobs": 0, "pending_seconds": 0.0, "inflight_jobs": 0},
    )

    seen = {}

    async def fake_rerank_async(query, candidates, top_k=None, snippet_cap=512, cost=None):
        seen["n"] = len(candidates)
        seen["slen"] = snippet_cap
        return candidates

    monkeypatch.setattr(orch, "rerank_async", fake_rerank_async)

    results = [_mk(f"T{i}", f"https://a.com/{i}") for i in range(20)]
    s1 = FatSource("s1", results)
    monkeypatch.setattr(orch, "get_sources", lambda names: [s1])

    async def run():
        return await orch.search("idle full quality", ["s1"], limit=5)

    out = asyncio.run(run())
    assert seen["n"] == 20, seen  # fused count caps candidates
    assert seen["slen"] == 512, seen
    assert out["quality"]["tier"] == 0
    assert "rerank" not in out["degraded"]


def test_burst_16_searches_all_within_deadline(monkeypatch, rds):
    """16 concurrent searches against a SERIALIZED 0.4s rerank worker (the
    real executor + a slow sync rerank): the 3s per-search deadline must
    hold for EVERY search — the adaptive tier ladder and the deadline valve
    degrade late arrivals instead of queueing them forever."""
    import kortex_search.orchestrator as orch
    import kortex_search.rerank as rr
    from tests.test_pipeline import _mk

    _search_ctx(monkeypatch)
    quality.reset()
    monkeypatch.setattr(orch, "SEMANTIC_RERANK", True)
    monkeypatch.setattr(orch, "ADAPTIVE_QUALITY", True)
    monkeypatch.setattr(orch, "SEARCH_TOTAL_TIMEOUT", 3.0)
    monkeypatch.setattr(orch, "GLOBAL_TIMEOUT", 60)

    def slow_sync_rerank(query, candidates, top_k=None, snippet_cap=512):
        time.sleep(0.4)  # serialized by the single rerank executor
        return candidates

    monkeypatch.setattr(rr, "rerank", slow_sync_rerank)

    results = [_mk(f"T{i}", f"https://a.com/{i}") for i in range(20)]
    s1 = FatSource("s1", results)
    s2 = FatSource("s2", results)
    monkeypatch.setattr(orch, "get_sources", lambda names: [s1, s2])

    async def run():
        t0 = time.monotonic()
        outs = await asyncio.gather(
            *[orch.search(f"burst query {i}", ["s1", "s2"], limit=5) for i in range(16)]
        )
        return outs, time.monotonic() - t0

    outs, elapsed = asyncio.run(run())
    assert elapsed < 3.0 + 1.5, f"burst wall {elapsed:.1f}s blew the deadline"
    for i, out in enumerate(outs):
        assert out["count"] == 5, f"search {i}: count={out['count']}"
        assert "quality" in out, f"search {i} missing quality block"
    # the serialized worker cannot serve 16 reranks in time — some searches
    # must have degraded (deadline skip or load skip), proving the valve works
    assert any("rerank" in o["degraded"] for o in outs), (
        "no search degraded — the burst valve never opened"
    )


def test_search_respects_explicit_deadline(monkeypatch, rds):
    """A caller-supplied deadline must bound the whole pipeline (search +
    post-fanout stages)."""
    import kortex_search.orchestrator as orch
    from tests.test_pipeline import FakeSource, _mk

    _search_ctx(monkeypatch)
    monkeypatch.setattr(orch, "SEMANTIC_RERANK", False)
    monkeypatch.setattr(orch, "GLOBAL_TIMEOUT", 60)

    slow = FakeSource("slow", [_mk("Late", "https://l.com/1")], delay=10.0)
    fast = FakeSource("fast", [_mk("Quick", "https://q.com/1")])
    monkeypatch.setattr(orch, "get_sources", lambda names: [slow, fast])

    async def run():
        t0 = time.monotonic()
        out = await orch.search("caller deadline", ["slow", "fast"], limit=5, deadline=t0 + 1.5)
        return out, time.monotonic() - t0

    out, elapsed = asyncio.run(run())
    assert elapsed < 4.0, f"search ignored the caller deadline ({elapsed:.1f}s)"
    assert out["count"] == 1
    assert out["partial"] is True


# --------------------------------------------------------------------------
# 5. research_answer total tool budget
# --------------------------------------------------------------------------


def test_research_answer_total_tool_budget(monkeypatch, rds):
    """The whole research_answer tool call (search + synthesis) must fit its
    total budget — a slow synthesis degrades instead of blowing the client's
    request timeout."""
    import kortex_search.llm as llm
    import kortex_search.orchestrator as orch
    import kortex_search.server as srv

    monkeypatch.setattr(srv, "RESEARCH_ANSWER_TOTAL_TIMEOUT", 2.0)
    monkeypatch.setattr(srv, "ANSWER_LLM_TIMEOUT", 25.0)

    async def fake_search(query, sources, limit, **kwargs):
        return {
            "results": [
                {
                    "title": "Source One",
                    "url": "https://one.example/",
                    "snippet": "the first source snippet",
                },
            ],
            "sources": {"searxng": "ok (1)"},
        }

    monkeypatch.setattr(orch, "search", fake_search)

    async def slow_complete(messages, **kwargs):
        await asyncio.sleep(10)
        return '{"answer_md": "never", "citations": []}'

    monkeypatch.setattr(llm, "complete", slow_complete)

    async def run():
        t0 = time.monotonic()
        out = await srv.research_answer("total budget", limit=4)
        return out, time.monotonic() - t0

    out, elapsed = asyncio.run(run())
    assert elapsed < 4.0, f"tool blew its total budget ({elapsed:.1f}s)"
    assert "timed out" in out["answer"]
    assert out["results"], "degraded answer must still carry the search hits"
