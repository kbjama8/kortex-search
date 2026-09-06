"""Adaptive quality controller (v0.9).

Ranks the expensive post-fanout stage (cross-encoder rerank) down a quality
ladder based on the remaining per-search budget and the inference queue's
pending work:

    tier 0  idle       30 candidates x 512-char snippets  (~4.7s measured)
    tier 1  light      20 x 384                            (~2.3s)
    tier 2  moderate   15 x 256                            (~1.2s)
    tier 3  busy       10 x 160                            (~0.5s)
    tier 4  saturated  skip rerank entirely → RRF order    (0s)

Decisions use a linear cost model (k x pairs x chars + fixed overhead)
self-calibrated from observed predict durations, so tier boundaries track
the actual machine. Hysteresis keeps tiers stable: a load spike downgrades
immediately; an upgrade only happens once the load window has fully cleared
(a single quiet sample must not yoyo the pipeline back to expensive full
reranks during a burst).

Design note: parallel rerank THREADS are deliberately not used to absorb
bursts — measured 2026-09-05: concurrent ONNX predicts contend on the CPU
pool (4-parallel wall 16.1s for 4 x 4.5s jobs). The valve is work REDUCTION,
not more workers.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

_TIER_TABLE = (
    (0, 30, 512, "full"),
    (1, 20, 384, "light"),
    (2, 15, 256, "moderate"),
    (3, 10, 160, "busy"),
    (4, 0, 0, "saturated"),
)

SAFETY_FACTOR = 1.2  # queue-drain + cost margin
UPGRADE_HEADROOM = 0.75  # upgrades need cost <= remaining x headroom
UPGRADE_LOAD_CLEAR_S = 2.0  # pessimistic queue must be this small to upgrade
HISTORY_WINDOW = 6  # samples kept for pessimistic/optimistic load


@dataclass(frozen=True)
class Tier:
    """One rung of the quality ladder (candidates/snippet_cap already clamped
    by the caller's ceilings and the fused result count)."""

    level: int
    candidates: int  # 0 = skip rerank
    snippet_cap: int
    label: str


TIERS: tuple[Tier, ...] = tuple(Tier(*row) for row in _TIER_TABLE)
SKIP_TIER: Tier = TIERS[-1]


class CostModel:
    """Linear rerank cost model with EMA self-calibration.

    cost(n, slen) = c0 + k * n * slen — the measured cost table (2026-09-05,
    onnx_int8, this CPU) is linear in pairs x chars with no batch
    amortization, so two parameters capture it. `observe_rerank` pulls the
    constants toward reality on every completed predict.
    """

    def __init__(
        self, k: float = 0.00025, c0: float = 0.5, embed_s: float = 0.2, learn_rate: float = 0.25
    ):
        self.k = k
        self.c0 = c0
        self.embed_s = embed_s
        self.lr = learn_rate

    def rerank_cost(self, n: int, slen: int) -> float:
        if not n:
            return 0.0
        return self.c0 + self.k * n * slen

    def embed_cost(self, n_docs: int = 1) -> float:
        return self.embed_s * max(0, n_docs)

    def observe_rerank(self, n: int, slen: int, actual: float) -> None:
        """Fold one real predict duration into the model (EMA)."""
        if not n or not slen or actual <= 0:
            return
        k_obs = max(0.0, (actual - self.c0) / (n * slen))
        self.k = (1 - self.lr) * self.k + self.lr * k_obs
        c0_obs = max(0.0, actual - self.k * n * slen)
        self.c0 = (1 - self.lr) * self.c0 + self.lr * c0_obs


_model = CostModel()


def estimate_rerank(n: int, slen: int) -> float:
    """Estimated cost in seconds for a rerank of `n` pairs at `slen` chars."""
    return _model.rerank_cost(n, slen)


def observe_rerank(n: int, slen: int, actual: float) -> None:
    _model.observe_rerank(n, slen, actual)


class LoadHistory:
    """Sliding window of pending-inference load samples.

    pessimistic() = max(window) — the downgrade signal (a spike is honored
    immediately and remembered until it ages out).
    optimistic()  = min(window) — kept for symmetry; upgrades additionally
    require the pessimistic window to clear, so a single quiet sample can
    never upgrade past a remembered spike.
    """

    def __init__(self, window: int = HISTORY_WINDOW):
        self._q: deque[float] = deque(maxlen=window)

    def push(self, x: float) -> None:
        self._q.append(max(0.0, float(x)))

    def pessimistic(self) -> float:
        return max(self._q) if self._q else 0.0

    def optimistic(self) -> float:
        return min(self._q) if self._q else 0.0

    def clear(self) -> None:
        self._q.clear()


_history = LoadHistory()
_current_tier = 0


def reset() -> None:
    """Reset tier state + load window (tests)."""
    global _current_tier
    _history.clear()
    _current_tier = 0


def _clamped(tier: Tier, fused: int, ceil_c: int, ceil_s: int) -> Tier:
    cand = min(tier.candidates, ceil_c, fused) if tier.candidates else 0
    slen = min(tier.snippet_cap, ceil_s) if tier.candidates else 0
    return Tier(tier.level, cand, slen, tier.label)


def _best_tier(remaining: float, load_seconds: float, fused: int, ceil_c: int, ceil_s: int) -> Tier:
    """Highest-quality tier whose estimated completion fits `remaining`."""
    for tier in TIERS:
        cand = min(tier.candidates, ceil_c, fused) if tier.candidates else 0
        slen = min(tier.snippet_cap, ceil_s) if tier.candidates else 0
        wait = load_seconds * SAFETY_FACTOR
        if wait + _model.rerank_cost(cand, slen) <= remaining:
            return Tier(tier.level, cand, slen, tier.label)
    return SKIP_TIER


def pick_tier(
    *,
    remaining: float,
    fused: int,
    limit: int,
    candidates_ceiling: int,
    snippet_ceiling: int,
    pending_seconds: float,
) -> Tier:
    """Choose the rerank tier for one search.

    Downgrade fast (pessimistic window), upgrade slow (window must clear AND
    the stricter headroom budget must allow it). Returns a tier whose
    candidate/snippet values are already clamped.
    """
    global _current_tier
    if fused <= limit:
        return SKIP_TIER
    ceil_c = max(1, int(candidates_ceiling))
    ceil_s = max(1, int(snippet_ceiling))
    _history.push(pending_seconds)
    pess = _history.pessimistic()

    tier_pess = _best_tier(remaining, pess, fused, ceil_c, ceil_s)
    current = min(max(_current_tier, 0), len(TIERS) - 1)

    if tier_pess.level > current:
        _current_tier = tier_pess.level
        return tier_pess
    if pess * SAFETY_FACTOR <= UPGRADE_LOAD_CLEAR_S:
        tier_up = _best_tier(remaining * UPGRADE_HEADROOM, pess, fused, ceil_c, ceil_s)
        if tier_up.level < current:
            _current_tier = tier_up.level
            return tier_up
    return _clamped(TIERS[current], fused, ceil_c, ceil_s)
