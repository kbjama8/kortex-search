"""Gateway configuration — env-overridable, machine-specific defaults."""

import os

# v0.5.0 rename (Search Gateway -> Kortex Search): KORTEX_SEARCH_* is the
# canonical env prefix. The SEARCH_GATEWAY_* fallback bridge was removed in
# 0.6.0 — the old prefix is now ignored entirely.
_PREFIX = "KORTEX_SEARCH_"


def _read_env(name: str) -> str | None:
    """Read the canonical KORTEX_SEARCH_* env var."""
    return os.environ.get(name)


def _env(name: str, default: str) -> str:
    v = _read_env(name)
    return default if v is None else v


def _env_bool(name: str, default: bool) -> bool:
    v = _read_env(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    v = _read_env(name)
    try:
        return int(v) if v is not None else default
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    v = _read_env(name)
    try:
        return float(v) if v is not None else default
    except ValueError:
        return default


# --- infrastructure ---
SEARXNG_BASE = _env("SEARXNG_BASE", "http://127.0.0.1:8888")
REDIS_URL = _env("KORTEX_SEARCH_REDIS_URL", "redis://127.0.0.1:6379/0")
GITHUB_TOKEN = _env("GITHUB_TOKEN", "")  # optional, raises API rate limit

# --- search behaviour ---
# Fast, non-browser sources are the default fan-out (opencli sources are
# serialized through one browser bridge, so they're opt-in via `sources`).
DEFAULT_SOURCES = ["searxng", "exa", "github", "youtube", "bilibili", "v2ex"]
WEB_SOURCES = ["searxng", "exa"]
SOCIAL_SOURCES = ["twitter", "reddit", "facebook", "instagram"]
ACADEMIC_SOURCES = ["arxiv", "openalex", "crossref"]  # semantic_scholar optional
# Semantic Scholar's free API rate-limits hard (429). A 429 sets a
# process-wide fast-fail cooldown instead of the old multi-retry burn;
# citation/reference chains are OpenAlex-first (sweep 2026-09-03).
S2_COOLDOWN = _env_int("KORTEX_SEARCH_S2_COOLDOWN", 900)
# polite-pool email for OpenAlex/Crossref (not an API key — just courteous)
MAILTO = _env("KORTEX_SEARCH_MAILTO", "kaichen.research@proton.me")
DEFAULT_LIMIT = 10
GLOBAL_TIMEOUT = _env_int("KORTEX_SEARCH_TIMEOUT", 50)  # seconds per search
PER_SOURCE_TIMEOUT = _env_int("KORTEX_SEARCH_SOURCE_TIMEOUT", 18)

# --- end-to-end budget discipline (sweep 2026-09-03) ---
# A single search tool call must fit inside the MCP client's request
# budget. The historical worst case — fanout 50s + unbounded expansion LLM
# (60s) + expansion fanout (50s) + CPU stages — exceeded 150s, which
# clients read as -32001 timeouts; concurrent overruns froze the event
# loop and killed the process. These knobs bound every leg of the pipeline.
SEARCH_TOTAL_TIMEOUT = _env_int("KORTEX_SEARCH_TOTAL_TIMEOUT", 45)
# The expansion LLM leg gets its own short budget (was: unbounded behind a
# 60s client timeout). On expiry, expansion degrades to "no variants".
EXPANSION_LLM_TIMEOUT = _env_float("KORTEX_SEARCH_EXPANSION_LLM_TIMEOUT", 12.0)
# research_answer's synthesis leg (on top of the search itself).
ANSWER_LLM_TIMEOUT = _env_float("KORTEX_SEARCH_ANSWER_LLM_TIMEOUT", 25.0)

# --- adaptive quality (v0.9) ---
# The rerank stage scales its work with load (see quality.py): full quality
# idle, graceful degradation when the inference queue is busy. These are the
# CEILINGS the ladder can never exceed; the existing RERANK_CANDIDATES knob
# doubles as the candidate ceiling.
ADAPTIVE_QUALITY = _env_bool("KORTEX_SEARCH_ADAPTIVE_QUALITY", True)
RERANK_MAX_SNIPPET = _env_int("KORTEX_SEARCH_RERANK_MAX_SNIPPET", 512)
# research_answer's whole-tool budget (search + synthesis legs together) —
# must fit inside the MCP client's request timeout.
RESEARCH_ANSWER_TOTAL_TIMEOUT = _env_int(
    "KORTEX_SEARCH_ANSWER_TOTAL_TIMEOUT", 50)

# --- retry (Phase 3) ---
RETRY_COUNT = _env_int("KORTEX_SEARCH_RETRY_COUNT", 1)
RETRY_BACKOFF = _env_float("KORTEX_SEARCH_RETRY_BACKOFF", 1.5)  # seconds, x2 per retry
RETRYABLE_EXIT_CODES = (1, 8, 52, 56)  # curl-style transient codes; skip on auth/404

# --- rate limiting for cookie-logged sources (Phase 3) ---
RATE_LIMITED_SOURCES = {"twitter", "reddit", "facebook", "instagram"}
# Min seconds between queries to a cookie-logged source. Research (sweep
# 2026-09-03): fixed short intervals read as automation; pacing must be
# conservative AND jittered (see RATE_LIMIT_JITTER in the extraction layer).
RATE_LIMIT_INTERVAL = _env_float("KORTEX_SEARCH_RATE_LIMIT", 5.0)

# --- fusion / re-rank / diversity (Phases 1-2) ---
RRF_K = 60
WEIGHTED_RRF = _env_bool("KORTEX_SEARCH_WEIGHTED_RRF", True)
RERANK_MODEL = _env("KORTEX_SEARCH_RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
EMBED_MODEL = _env("KORTEX_SEARCH_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
# Multilingual embedding model for CJK-heavy runs (bilibili/v2ex/XHS dominate).
# Lazy-loaded only when a run's fused results are CJK-dominant; MiniLM stays the
# fast default for English-dominant text.
EMBED_MODEL_CJK = _env("KORTEX_SEARCH_EMBED_MODEL_CJK", "BAAI/bge-m3")
# Pinned HF revisions (empty string = unpinned). Fixes the observed bge-m3
# commit-churn re-download bug: without `revision=`, snapshot_download /
# SentenceTransformer re-fetch whenever the model repo's HEAD moves.
RERANK_REVISION = _env("KORTEX_SEARCH_RERANK_REVISION",
                       "953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e")
EMBED_REVISION = _env("KORTEX_SEARCH_EMBED_REVISION",
                      "1110a243fdf4706b3f48f1d95db1a4f5529b4d41")
EMBED_CJK_REVISION = _env("KORTEX_SEARCH_EMBED_CJK_REVISION",
                          "5617a9f61b028005a4858fdac845db406aefb181")
EMBED_CJK = _env_bool("KORTEX_SEARCH_EMBED_CJK", True)

# --- inference backend (ONNX, ~2x faster rerank) ---
# onnx_int8 | onnx | torch. Default onnx_int8 (dynamic-quantized ONNX) — ~2x
# faster + smaller RSS than torch, ranking quality preserved (Spearman ≈ 0.96).
# optimum + onnxruntime are core dependencies; if the ONNX model cannot load,
# the reranker falls back to torch automatically.
INFERENCE_BACKEND = _env("KORTEX_SEARCH_INFERENCE_BACKEND", "onnx_int8")
RERANK_ONNX_MODEL = _env("KORTEX_SEARCH_RERANK_ONNX_MODEL",
                         "onnx-community/bge-reranker-v2-m3-ONNX")
RERANK_ONNX_REVISION = _env("KORTEX_SEARCH_RERANK_ONNX_REVISION",
                            "6f5ff65298512715a1e669753bc754d2bc8f367b")
# onnx -> model.onnx (fp32); onnx_int8 -> model_int8.onnx (dynamic-quantized).
_ONNX_FILE = {"onnx": "model.onnx", "onnx_int8": "model_int8.onnx"}
CJK_SHARE_THRESHOLD = _env_float("KORTEX_SEARCH_CJK_SHARE_THRESHOLD", 0.25)
SEMANTIC_RERANK = _env_bool("KORTEX_SEARCH_SEMANTIC_RERANK",
                            _env_bool("SEMANTIC_RERANK", True))  # legacy alias honored
RERANK_CANDIDATES = _env_int("KORTEX_SEARCH_RERANK_CANDIDATES", 30)
MMR_ENABLED = _env_bool("KORTEX_SEARCH_MMR", True)
MMR_LAMBDA = _env_float("KORTEX_SEARCH_MMR_LAMBDA", 0.75)  # relevance vs diversity
# Per-category λ (B6 calibration): news/social value diversity more (0.7),
# science/general value relevance more (0.8). Swept 0.6-0.9 in scripts/rerank_eval.py.
MMR_LAMBDA_BY_CATEGORY = {
    "general": _env_float("KORTEX_SEARCH_MMR_LAMBDA_GENERAL", 0.75),
    "news": _env_float("KORTEX_SEARCH_MMR_LAMBDA_NEWS", 0.7),
    "science": _env_float("KORTEX_SEARCH_MMR_LAMBDA_SCIENCE", 0.8),
    "social": _env_float("KORTEX_SEARCH_MMR_LAMBDA_SOCIAL", 0.7),
}
EMBEDDING_DEDUP = _env_bool("KORTEX_SEARCH_EMBEDDING_DEDUP", True)

# --- freshness (Phase 3) ---
FRESHNESS_FILTER = _env_bool("KORTEX_SEARCH_FRESHNESS", True)

# --- two-tier / opencli parallelism (Phase 4) ---
TWO_TIER = _env_bool("KORTEX_SEARCH_TWO_TIER", True)
OPENCLI_PROFILES = _env_int("KORTEX_SEARCH_OPENCLI_PROFILES", 1)  # N Chromium profiles

# --- cache (Phase 1) ---
CACHE_TTL = _env_int("KORTEX_SEARCH_CACHE_TTL", 3600)  # final-result TTL (1h)
SOURCE_CACHE_TTL = _env_int("KORTEX_SEARCH_SOURCE_CACHE_TTL", 900)  # per-source TTL (15m)
# Negative caching: how long a failed source is skipped for that query (bounded
# so recovery stays fast).
NEGATIVE_CACHE_TTL = _env_int("KORTEX_SEARCH_NEGATIVE_CACHE_TTL", 60)

# --- LLM / answer synthesis (Phase 5) ---
DEEPSEEK_API_KEY = _env("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = _env("KORTEX_SEARCH_LLM_MODEL", "deepseek-v4-flash")
LLM_ENABLED = _env_bool("KORTEX_SEARCH_LLM", True)
LLM_TIMEOUT = _env_int("KORTEX_SEARCH_LLM_TIMEOUT", 60)
QUERY_EXPANSION = _env_bool("KORTEX_SEARCH_QUERY_EXPANSION", True)
# Query-rewrite gating: expansion only runs when the base fan-out returns
# fewer results than this (weak base → variants are worth the latency).
EXPANSION_GATE_RESULTS = _env_int("KORTEX_SEARCH_EXPANSION_GATE", 6)

# --- fallback-chain matrix (B6 #8) ---
# Per-source degradation semantics: when a channel fails (429/500/timeout/
# empty/garbage), the fan-out records the failure in `sources` statuses and
# the pipeline continues with the remaining sources. This matrix documents the
# intended degrade order per channel class — enforced by the source adapters'
# error semantics + the negative cache. "silent-throttle" = empty-but-200.
FALLBACK_CHAINS: dict[str, list[str]] = {
    # browser-backed channels: managed profile farm first (self-hosted
    # persistent Chrome over CDP), OpenCLI bridge second (operator's own
    # browser), then skip (ADR-0009, sweep 2026-09-04)
    "twitter": ["twitter-cli", "farm-cdp", "opencli-twitter", "skip"],
    "reddit": ["farm-cdp", "opencli-reddit", "skip"],
    "facebook": ["opencli-facebook", "skip"],
    "instagram": ["opencli-instagram", "skip"],
    "xiaohongshu": ["opencli-xiaohongshu", "skip"],
    "linkedin": ["mcporter-linkedin", "skip"],
    # API channels: fail → the fan-out's remaining sources absorb the gap
    "searxng": ["searxng-json", "exa"],
    "exa": ["exa-mcporter", "searxng"],
    "github": ["github-rest", "skip"],
    "youtube": ["yt-dlp", "skip"],
    "bilibili": ["bilibili-api", "skip"],
    "v2ex": ["sov2ex-api", "skip"],
    "stackoverflow": ["stackexchange-api", "skip"],
    # academic: dedicated sources cross-cover each other
    "arxiv": ["arxiv-atom", "openalex"],
    "openalex": ["openalex-rest", "crossref"],
    "crossref": ["crossref-rest", "openalex"],
    "semantic_scholar": ["s2-rest", "openalex"],
    # reader channel
    "web": ["jina-reader", "skip"],
    # CN tier (v0.4): API first, browser as the last-resort tier
    "zhihu": ["zhihu-v4", "browser", "skip"],
    "zhihu_hot": ["zhihu-hot-api", "skip"],
    "weibo": ["weibo-ajax", "weibo-hot", "browser", "skip"],
    "baidu": ["baidu-board", "skip"],
    "toutiao": ["toutiao-board", "skip"],
}

# --- auth (v0.4.1: secrets vault, per-persona, 0600) ---
# Persona + vault root drive the env-file defaults. `vault.resolve_env_file`
# honors the legacy flat paths (deprecated, removed 0.4.3) and the systemd
# `$CREDENTIALS_DIRECTORY` bridge (KORTEX_SEARCH_CREDENTIALS_DIR).
PERSONA = _env("KORTEX_SEARCH_PERSONA", "kaiser")
VAULT_DIR = _env("KORTEX_SEARCH_VAULT_DIR",
                 os.path.expanduser("~/.agent-reach/profiles"))
CREDENTIALS_DIR = _env("KORTEX_SEARCH_CREDENTIALS_DIR", "")
TWITTER_ENV_FILE = _env("TWITTER_AUTH_FILE",
                        os.path.expanduser(f"{VAULT_DIR}/{PERSONA}/twitter.env"))
DEEPSEEK_ENV_FILE = _env("DEEPSEEK_AUTH_FILE",
                         os.path.expanduser(f"{VAULT_DIR}/{PERSONA}/deepseek.env"))

# --- observability / serving (Phase D) ---
LOG_FMT = _env("KORTEX_SEARCH_LOG_FMT", "text")  # text | json (always stderr)
LOG_LEVEL = _env("KORTEX_SEARCH_LOG_LEVEL", "INFO")
# Bounded per-source latency reservoir (last N successes) for p50/p95 — feeds
# the adaptive-timeout budget and stats_report.
STATS_RESERVOIR_SIZE = _env_int("KORTEX_SEARCH_STATS_RESERVOIR", 60)
# Adaptive per-source timeout: min(p95 x factor, cap) — stragglers die early,
# healthy sources get headroom. Bounded by PER_SOURCE_TIMEOUT semantics.
ADAPTIVE_TIMEOUT = _env_bool("KORTEX_SEARCH_ADAPTIVE_TIMEOUT", True)
ADAPTIVE_TIMEOUT_FACTOR = _env_float("KORTEX_SEARCH_ADAPTIVE_TIMEOUT_FACTOR", 1.5)
ADAPTIVE_TIMEOUT_MIN = _env_float("KORTEX_SEARCH_ADAPTIVE_TIMEOUT_MIN", 3.0)
ADAPTIVE_TIMEOUT_MAX = _env_float("KORTEX_SEARCH_ADAPTIVE_TIMEOUT_MAX", 25.0)
MCP_HOST = _env("KORTEX_SEARCH_HOST", "127.0.0.1")  # bind host for http/sse serve
MCP_PORT = _env_int("KORTEX_SEARCH_PORT", 8765)     # bind port for http/sse serve
# Required for the http/sse transports: an unauthenticated network endpoint
# lets anyone spend the DeepSeek budget + trigger ban-rate queries against the
# cookie-logged burner accounts. The stdio transport needs no token.
HTTP_TOKEN = _env("KORTEX_SEARCH_HTTP_TOKEN", "")
# Per-source daily query budget (Redis-backed, 24h window) — the outer guard
# against runaway/abusive fan-out on top of the per-query rate limit.
DAILY_QUERY_LIMIT = _env_int("KORTEX_SEARCH_DAILY_QUERY_LIMIT", 300)
# Cap on `doctor` total probe wall time — the MCP client request timeout is
# far smaller than the slowest probe (linkedin uvx first-run, opencli doctor),
# so an unbounded report made the MCP `doctor` tool time out (-32001).
DOCTOR_TIMEOUT = _env_int("KORTEX_SEARCH_DOCTOR_TIMEOUT", 12)
# Per-probe budget inside the doctor report (opencli doctor alone ≈ 9s).
DOCTOR_PROBE_TIMEOUT = _env_int("KORTEX_SEARCH_DOCTOR_PROBE_TIMEOUT", 6)
# Probe results are cached so repeated doctor calls are instant (states don't
# change every second — opencli doctor ≈ 9s, uvx cold start ≈ 2.5s).
DOCTOR_CACHE_TTL = _env_int("KORTEX_SEARCH_DOCTOR_CACHE_TTL", 120)

# --- ledger health (Phase 4.6) ---
# Where deep-research run directories (containing ledger.json) live. Used by
# `doctor` / `stats_report` to report run/claim/open-claim health. Read-only.
LEDGER_DIR = _env("KORTEX_SEARCH_LEDGER_DIR", os.path.expanduser("~/research_runs"))

# --- extraction layer (v0.4 / Project Gatekeeper) ---
# The extraction overhaul's knobs. Risky capabilities ship disabled; the
# existing behavior is preserved when every flag below stays at its default.
# Doctrine: public-content research, consistency-first stealth, pacing as the
# cheapest anti-detection. See docs/extraction/PLAN.md.

# Profile farm (Phase 2): per-(platform, persona) browser sessions with a
# health state machine. Reuses the Phase-4 OPENCLI_PROFILES knob for count.
PROFILE_DIR = _env("KORTEX_SEARCH_PROFILE_DIR",
                   os.path.expanduser("~/.agent-reach/profiles"))
PROFILE_HEALTH_TTL = _env_int("KORTEX_SEARCH_PROFILE_HEALTH_TTL", 3600)

# Browser scheduler (Phase 1): global budget replaces the single OPENCLI_LOCK.
# Default 1 preserves the old single-bridge behavior; the profile farm raises
# it once parallel browser profiles exist (KORTEX_SEARCH_OPENCLI_PROFILES).
BROWSER_BUDGET = _env_int("KORTEX_SEARCH_BROWSER_BUDGET", 1)
RATE_LIMIT_JITTER = _env_float("KORTEX_SEARCH_RATE_LIMIT_JITTER", 0.3)  # ± fraction

# Stealth tier (Phase 3): Camoufox anonymous extraction, experimental.
STEALTH_ENABLED = _env_bool("KORTEX_SEARCH_STEALTH", False)
STEALTH_PROFILE = _env("KORTEX_SEARCH_STEALTH_PROFILE", "")  # fingerprint preset name

# --- Managed profile farm (sweep 2026-09-04, ADR-0009) ---
# Persistent per-profile Chrome instances driven over raw CDP by
# agent-browser (Apache-2.0). Login state lives in a pinned user-data-dir
# and survives browser restarts — the tier no longer depends on the
# operator's own browser running (the OpenCLI bridge's failure mode).
# Research: direct-CDP control planes are the 2026 benchmark's only
# zero-block class; see docs/extraction/BROWSER-TIER-2026.md.
FARM_ENABLED = _env_bool("KORTEX_SEARCH_FARM_ENABLED", True)
FARM_BROWSER_BIN = _env("KORTEX_SEARCH_FARM_BROWSER_BIN", "agent-browser")
# Virtual display for headed-but-invisible launches. ":99" = Xvfb (needs
# xorg-x11-server-Xvfb); "" = agent-browser default (headless-new — more
# detectable on hard targets, use only for tolerant ones).
FARM_DISPLAY = _env("KORTEX_SEARCH_FARM_DISPLAY", ":99")
FARM_LAUNCH_TIMEOUT = _env_int("KORTEX_SEARCH_FARM_LAUNCH_TIMEOUT", 60)
FARM_COMMAND_TIMEOUT = _env_int("KORTEX_SEARCH_FARM_COMMAND_TIMEOUT", 45)
# Idle profiles (no exec in this many seconds) are eligible for
# `kortex-search farm reap` shutdown — memory discipline on small hosts.
FARM_IDLE_TTL = _env_int("KORTEX_SEARCH_FARM_IDLE_TTL", 1800)

# HTTP impersonation (Phase 3): curl_cffi TLS/JA3/HTTP2 for fingerprinted APIs.
IMPERSONATE = _env_bool("KORTEX_SEARCH_IMPERSONATE", False)
IMPERSONATE_SOURCES = {"bilibili", "zhihu", "weibo"}  # allowlist; searxng etc. never

# Proxy subsystem (Phase 3.5): env-gated, default OFF (zero-cost doctrine).
PROXY_ENABLED = _env_bool("KORTEX_SEARCH_PROXY_ENABLED", False)
PROXY_PROTOCOL = _env("KORTEX_SEARCH_PROXY_PROTOCOL", "http")  # http|socks5
PROXY_GATEWAY = _env("KORTEX_SEARCH_PROXY_GATEWAY", "")       # host:port
PROXY_USERNAME = _env("KORTEX_SEARCH_PROXY_USERNAME", "")     # carries geo grammar
PROXY_PASSWORD = _env("KORTEX_SEARCH_PROXY_PASSWORD", "")
PROXY_ENV_FILE = _env("KORTEX_SEARCH_PROXY_AUTH_FILE",
                      os.path.expanduser(f"{VAULT_DIR}/{PERSONA}/proxy.env"))
PROXY_COUNTRY = _env("KORTEX_SEARCH_PROXY_COUNTRY", "")       # "" = provider default
# Sticky-session lifetime per profile. Research (sweep 2026-09-03): IP
# rotation per account is one of the strongest bot signals — account-bearing
# profiles want lifetime-sticky sessions. 24h is the compromise default;
# operators with account farms should raise it further.
PROXY_STICKY_TTL = _env("KORTEX_SEARCH_PROXY_STICKY_TTL", "24h")
# Geo-consistency (Phase 3.5): derive TZ/locale bundle from egress geo.
PROXY_GEO_ALIGN = _env_bool("KORTEX_SEARCH_PROXY_GEO_ALIGN", True)

# Block & challenge intelligence (Phase 4).
BLOCK_DETECTION = _env_bool("KORTEX_SEARCH_BLOCK_DETECTION", True)
PLATFORM_BLOCK_LIMIT = _env_int("KORTEX_SEARCH_PLATFORM_BLOCK_LIMIT", 3)
PLATFORM_COOLDOWN_TTL = _env_int("KORTEX_SEARCH_PLATFORM_COOLDOWN_TTL", 900)

# Parser intelligence (Phase 5): LLM-assisted extraction for degenerate shapes.
LLM_PARSE = _env_bool("KORTEX_SEARCH_LLM_PARSE", False)  # gated, validated
READ_URL_STAGES = _env("KORTEX_SEARCH_READ_URL_STAGES", "jina,trafilatura,readability")
TRAFILATURA_MIN_LEN = _env_int("KORTEX_SEARCH_TRAFILATURA_MIN_LEN", 300)

# CN sources (Phase 6): opt-in per D3 — bilibili wbi upgrade is always on.
BILIBILI_WBI = _env_bool("KORTEX_SEARCH_BILIBILI_WBI", True)
BILIBILI_WBI_KEY_TTL = _env_int("KORTEX_SEARCH_BILIBILI_WBI_KEY_TTL", 82800)  # 23h
CN_SOURCES = _env_bool("KORTEX_SEARCH_CN_SOURCES", False)  # zhihu/weibo/baidu/toutiao
ZHIHU_COOKIE = _env("ZHIHU_COOKIE", "")   # d_c0 + z_c0 (required; 401 without)
WEIBO_SUB = _env("WEIBO_SUB", "")         # logged-in SUB cookie (keyword search only)

# Cookie-gated sources: the envelope reports their auth state on every search
# (auth: missing|ok|unknown) so callers never wonder why a source is silent.
AUTH_GATED_SOURCES = frozenset({"zhihu", "weibo", "twitter", "reddit",
                                "facebook", "instagram", "xiaohongshu",
                                "linkedin"})

# YouTube PO tokens (Phase 4/6): externally provisioned; default off.
YOUTUBE_PO_PLUGIN = _env("KORTEX_SEARCH_YOUTUBE_PO_PLUGIN", "")
YOUTUBE_PO_SERVER = _env("KORTEX_SEARCH_YOUTUBE_PO_SERVER", "http://127.0.0.1:4416")

# --- containment & observability (v0.4.1+, Phase 7) ---
# L1 egress floor: always-blocked private/link-local/metadata ranges, checked
# pre-nav AND post-redirect. Default ON by design (it is the containment
# floor, not a risky capability). Local-infra exemption = the gateway's own
# loopback deps (SEARXNG_BASE/REDIS_URL) + the operator allowlist below.
EGRESS_FLOOR = _env_bool("KORTEX_SEARCH_EGRESS_FLOOR", True)
FLOOR_EXEMPT = _env("KORTEX_SEARCH_FLOOR_EXEMPT", "")
# Block-event telemetry reservoir (bounded, 24h TTL) — feeds doctor `blocks`.
BLOCK_RESERVOIR = _env_int("KORTEX_SEARCH_BLOCK_RESERVOIR", 120)

# --- L2 forced-proxy (v0.4.2, D7.2) ---
# Loopback CONNECT proxy in front of the anonymous browser tier. Every target
# passes the floor (deny 403 + telemetry); allowed targets chain through the
# residential tier when it is enabled. Anonymous engines only — the
# authenticated OpenCLI tier keeps L1+L3 (a bench live-test gate must pass
# before that policy ever changes).
EGRESS_PROXY = _env_bool("KORTEX_SEARCH_EGRESS_PROXY", True)

# --- L3 kernel filter (v0.4.2, D7.1) ---
# nftables per-cgroup egress DROP rules (no suid binary, no kernel module).
# Browser-tier ops REFUSE to launch without it: `required` (default) makes
# run_opencli/Camoufox fail with the explicit "egress-unhardened" message;
# `permissive` is the explicit opt-out for sandboxed CI that cannot run nft.
HARDEN = _env("KORTEX_SEARCH_HARDEN", "required")  # required | permissive
HARDEN_SUDO = _env_bool("KORTEX_SEARCH_HARDEN_SUDO", False)


def load_env_file(path: str, keys: set[str]) -> dict[str, str]:
    """Load `KEY=VALUE` lines from a file (export-aware, quote-stripped)."""
    out: dict[str, str] = {}
    if not path or not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            k = k.removeprefix("export ")
            if k in keys:
                out[k] = v.strip().strip('"').strip("'")
    return out


def load_env_file_credential(name: str, keys: set[str],
                             fallback_path: str = "") -> dict[str, str]:
    """Credentials-dir-aware env loader (systemd $CREDENTIALS_DIRECTORY bridge).

    When `KORTEX_SEARCH_CREDENTIALS_DIR` is set (systemd `LoadCredential=`
    puts files there as `<name>`), read `<dir>/<name>` first — the systemd
    way: secrets arrive as files, never as env vars (LESSONS.md §1.5).
    Both `<name>` and `<name>.env` are tried (the vault files carry the
    suffix; a unit may bind either name). Falls back to `fallback_path`
    (the vault file) when the bridge is off.
    """
    if CREDENTIALS_DIR:
        for candidate in (name, f"{name}.env"):
            path = os.path.join(CREDENTIALS_DIR, candidate)
            if os.path.exists(path):
                return load_env_file(path, keys)
    return load_env_file(fallback_path, keys)
