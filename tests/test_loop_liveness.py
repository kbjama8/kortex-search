"""Regression tests for the 2026-09-03 event-loop sweep.

Before the fix, CPU-bound model work (imports, lazy loads, batches) ran
synchronously inside async tool handlers. Reproduction (Phase 0) measured a
16.9s single-block event-loop freeze; any concurrent traffic queued behind
the freeze and the MCP stdio session unwound — the process exited cleanly
(rc=0) mid-request and every in-flight call died with -32000 / -32001,
forcing agents onto WebFetch fallbacks.

These tests pin the fixes:
  1. encode/rerank run off-loop: a slow model call must not stall the loop;
  2. the shared model worker serializes calls (load singleflight by
     construction);
  3. the expansion LLM leg is budget-bounded (degrade, never hang);
  4. the end-to-end search deadline bounds fan-out;
  5. research_answer synthesis is budget-bounded.
"""

from __future__ import annotations

import asyncio
import time

from tests.test_pipeline import FakeSource, _mk


def _search_ctx(monkeypatch):
    """Standard off-model pipeline flags for orchestrator tests."""
    import kortex_search.orchestrator as omod

    monkeypatch.setattr(omod, "SEMANTIC_RERANK", False)
    monkeypatch.setattr(omod, "MMR_ENABLED", False)
    monkeypatch.setattr(omod, "QUERY_EXPANSION", False)


# --------------------------------------------------------------------------
# 1. loop liveness: a slow model call must NOT freeze the event loop
# --------------------------------------------------------------------------

def test_encode_runs_off_event_loop(monkeypatch, rds):
    """A 1.2s embed call must not stall a concurrent probe task (it would
    before the sweep: encode ran synchronously in the handler)."""
    import numpy as np

    import kortex_search.embeddings as emb
    import kortex_search.orchestrator as orch

    _search_ctx(monkeypatch)
    calls = []

    def slow_encode(texts, multilingual=False):
        calls.append(1)
        time.sleep(1.2)  # blocking on purpose — must happen off-loop
        return np.eye(len(texts))

    monkeypatch.setattr(emb, "encode", slow_encode)

    s1 = FakeSource("s1", [_mk("A", "https://a.com/1")])
    s2 = FakeSource("s2", [_mk("B", "https://b.com/1")])
    monkeypatch.setattr(orch, "get_sources", lambda names: [s1, s2])

    async def run():
        gaps = []
        in_flight = asyncio.Event()

        async def probe():
            last = time.monotonic()
            while not in_flight.is_set():
                await asyncio.sleep(0.02)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        ptask = asyncio.create_task(probe())
        out = await orch.search("loop liveness", ["s1", "s2"], limit=50)
        in_flight.set()
        await ptask
        return out, gaps

    out, gaps = asyncio.run(run())
    assert out["count"] == 2
    assert calls, "encode was never invoked — test is vacuous"
    # probe ticked every ~20ms; a loop-blocked encode (1.2s) would leave a
    # gap >= 1.2s. Allow generous CI headroom: 0.5s.
    assert max(gaps) < 0.5, f"event loop stalled: max probe gap {max(gaps):.2f}s"


def test_rerank_runs_off_event_loop(monkeypatch, rds):
    """rerank_async must also execute off-loop (same executor path)."""
    import kortex_search.orchestrator as orch
    import kortex_search.rerank as rr

    _search_ctx(monkeypatch)
    # many results so the rerank branch (len(fused) > limit) triggers
    monkeypatch.setattr(orch, "SEMANTIC_RERANK", True)
    monkeypatch.setattr(orch, "EMBEDDING_DEDUP", False)
    monkeypatch.setattr(rr, "rerank",
                        lambda query, cands, top_k=None, snippet_cap=512: cands)

    from tests.test_adaptive_quality import FatSource
    from tests.test_pipeline import _mk

    s1 = FatSource("s1", [_mk(f"T{i}", f"https://a.com/{i}") for i in range(20)])
    monkeypatch.setattr(orch, "get_sources", lambda names: [s1])

    async def run():
        gaps = []
        done = asyncio.Event()

        async def probe():
            last = time.monotonic()
            while not done.is_set():
                await asyncio.sleep(0.02)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        ptask = asyncio.create_task(probe())
        out = await orch.search("rerank liveness", ["s1"], limit=5)
        done.set()
        await ptask
        return out, gaps

    out, gaps = asyncio.run(run())
    assert out["count"] == 5
    assert max(gaps) < 0.5, f"event loop stalled: max probe gap {max(gaps):.2f}s"


# --------------------------------------------------------------------------
# 2. single shared worker → calls serialize (load singleflight)
# --------------------------------------------------------------------------

def test_inference_calls_serialize_on_one_worker(monkeypatch):
    """Concurrent encode_async calls must not overlap: max_workers=1 means
    the first (cold) call loads the model while waiters queue — no
    duplicate loads, no concurrent CPU bursts."""
    import kortex_search.embeddings as emb

    active = 0
    max_active = 0
    calls = 0

    def slow_encode(texts, multilingual=False):
        nonlocal active, max_active, calls
        calls += 1
        active += 1
        max_active = max(max_active, active)
        time.sleep(0.15)
        active -= 1
        return None

    monkeypatch.setattr(emb, "encode", slow_encode)

    async def run():
        await asyncio.gather(*[emb.encode_async([f"doc{i}"])
                               for i in range(6)])

    asyncio.run(run())
    assert calls == 6
    assert max_active == 1, f"{max_active} model calls overlapped"


# --------------------------------------------------------------------------
# 3. expansion LLM leg is budget-bounded
# --------------------------------------------------------------------------

def test_expansion_llm_timeout_degrades(monkeypatch, rds):
    """A slow expansion LLM must not hold the pipeline: it degrades to 'no
    variants' within its budget and the search still returns results."""
    import kortex_search.llm as llm
    import kortex_search.orchestrator as orch

    _search_ctx(monkeypatch)
    monkeypatch.setattr(orch, "QUERY_EXPANSION", True)
    monkeypatch.setattr(orch, "EXPANSION_LLM_TIMEOUT", 0.3)
    monkeypatch.setattr(orch, "EMBEDDING_DEDUP", False)
    monkeypatch.setattr(llm, "available", lambda: True)

    async def slow_complete(messages, **kwargs):
        await asyncio.sleep(10)
        return "one two three\nfour five six"

    monkeypatch.setattr(llm, "complete", slow_complete)

    # weak base (1 result) → expansion is eligible; no `sources` arg → allowed
    s1 = FakeSource("s1", [_mk("Only Result", "https://a.com/1")])
    monkeypatch.setattr(orch, "get_sources", lambda names: [s1])

    async def run():
        t0 = time.monotonic()
        out = await orch.search("weak base expansion", None, limit=5)
        return out, time.monotonic() - t0

    out, elapsed = asyncio.run(run())
    assert elapsed < 5.0, f"expansion held the search {elapsed:.1f}s"
    assert out["count"] >= 1


# --------------------------------------------------------------------------
# 4. end-to-end deadline bounds the fan-out
# --------------------------------------------------------------------------

def test_search_total_deadline_bounds_fanout(monkeypatch, rds):
    """The per-search deadline caps the fan-out even when sources are slow
    and GLOBAL_TIMEOUT is far away."""
    import kortex_search.orchestrator as orch

    _search_ctx(monkeypatch)
    monkeypatch.setattr(orch, "SEARCH_TOTAL_TIMEOUT", 1.5)
    monkeypatch.setattr(orch, "GLOBAL_TIMEOUT", 60)
    monkeypatch.setattr(orch, "EMBEDDING_DEDUP", False)

    slow = FakeSource("slow", [_mk("Late", "https://l.com/1")], delay=10.0)
    fast = FakeSource("fast", [_mk("Quick", "https://q.com/1")])
    monkeypatch.setattr(orch, "get_sources", lambda names: [slow, fast])

    async def run():
        t0 = time.monotonic()
        out = await orch.search("deadline", ["slow", "fast"], limit=5)
        return out, time.monotonic() - t0

    out, elapsed = asyncio.run(run())
    assert elapsed < 4.0, f"search ignored the deadline ({elapsed:.1f}s)"
    assert out["partial"] is True
    assert "slow" in out["pending"] or "slow" in out["sources"]
    assert out["count"] == 1  # the fast source's result still returned


# --------------------------------------------------------------------------
# 5. research_answer synthesis is budget-bounded
# --------------------------------------------------------------------------

def test_research_answer_synthesis_timeout(monkeypatch, rds):
    """A slow DeepSeek completion must degrade to an explicit timeout
    answer instead of pushing the tool past the client budget."""
    import kortex_search.llm as llm
    import kortex_search.orchestrator as orch
    import kortex_search.server as srv

    monkeypatch.setattr(srv, "ANSWER_LLM_TIMEOUT", 0.3)

    async def fake_search(query, sources, limit, **kwargs):
        return {"results": [
            {"title": "Source One", "url": "https://one.example/",
             "snippet": "the first source snippet"},
        ], "sources": {"searxng": "ok (1)"}}

    monkeypatch.setattr(orch, "search", fake_search)

    async def slow_complete(messages, **kwargs):
        await asyncio.sleep(10)
        return '{"answer_md": "never", "citations": []}'

    monkeypatch.setattr(llm, "complete", slow_complete)

    async def run():
        t0 = time.monotonic()
        out = await srv.research_answer("synthesis budget", limit=4)
        return out, time.monotonic() - t0

    out, elapsed = asyncio.run(run())
    assert elapsed < 5.0, f"synthesis held the tool {elapsed:.1f}s"
    assert "timed out" in out["answer"]
    assert out["results"], "degraded answer must still carry the search hits"


# --------------------------------------------------------------------------
# 6. subprocess fd isolation: children must NEVER inherit the MCP stdin
# --------------------------------------------------------------------------

def test_run_cmd_children_do_not_inherit_server_stdin():
    """run_cmd spawns children with stdin=DEVNULL. Before the 2026-09-03
    fix, a child chain that reads its own stdin (mcporter → npx → the exa
    MCP server) became a second reader on the client's protocol pipe and
    stole MCP messages — the session unwound and the server exited cleanly
    mid-request (pipe-inode forensics: two processes holding fd 0 of the
    same pipe)."""
    import asyncio

    from kortex_search.sources.base import run_cmd

    # A child that reads stdin: with DEVNULL it sees instant EOF (''); if it
    # inherited the server's protocol pipe it would block or steal bytes.
    code, out = asyncio.run(run_cmd([
        "python3", "-c",
        "import sys; print('EOF' if sys.stdin.read(1) == '' else 'STOLEN')",
    ], retries=0))
    assert code == 0
    assert "EOF" in out, f"child stdin was NOT isolated: {out!r}"


def test_research_answer_retries_when_reasoning_starves_budget(monkeypatch, rds):
    """deepseek-v4 reasoning tokens count toward max_tokens — json_mode +
    thinking=high can return an EMPTY answer. research_answer must retry
    once with thinking disabled instead of serving an empty string
    (smoke-test discovery 2026-09-04)."""
    import kortex_search.llm as llm
    import kortex_search.orchestrator as orch
    import kortex_search.server as srv

    monkeypatch.setattr(srv, "ANSWER_LLM_TIMEOUT", 10)

    async def fake_search(query, sources, limit, **kwargs):
        return {"results": [
            {"title": "Source One", "url": "https://one.example/",
             "snippet": "the first source snippet"},
        ], "sources": {"searxng": "ok (1)"}}

    monkeypatch.setattr(orch, "search", fake_search)

    calls = []

    async def fake_complete(messages, **kwargs):
        calls.append(kwargs.get("thinking", "default"))
        if len(calls) == 1:
            return ""  # reasoning ate the whole budget
        return ('{"answer_md": "Retry worked [1]", '
                '"citations": [{"id": 1, "quote": "the first source snippet"}],'
                ' "insufficient_evidence": false}')

    monkeypatch.setattr(llm, "complete", fake_complete)

    async def run():
        return await srv.research_answer("synthesis retry", limit=4)

    out = asyncio.run(run())
    assert len(calls) == 2, calls
    assert calls[1] is False, calls  # retry runs with thinking disabled
    assert "Retry worked" in out["answer"]
    assert out["verification"]["status"] == "verified"
