# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.0] - 2026-09-06

"Adaptive quality ladder" — parallel-agent bursts no longer time out.

### Root cause fixed

Four agents x 3-4 concurrent MCP calls (12-16 in-flight searches) queued on
the single inference worker thread: every search burns ~5s of serialized
CPU inference (rerank 4.7s @ 30 pairs x 512 chars + 2 embeds), the
post-fanout stages had NO deadline (only the fan-out was bounded), so the
queue ran 60-80s deep against the client's 30s request budget — every call
timed out, and aborted calls still burned their queued worker time
(queue thrash). Measured live: a 4-way burst degraded walls 7.1s →
16.0/16.1/16.3s. Parallel rerank threads were ruled out by measurement —
concurrent ONNX predicts contend on the CPU pool (4-parallel = 16.1s wall
for 4 x 4.5s jobs), so the fix is work REDUCTION, not more workers.

### Added
- **`quality.py` — adaptive quality controller.** The rerank stage scales
  down a 5-rung ladder with the inference queue's pending work and the
  per-search remaining budget: full (30 x 512 chars) idle → light (20 x
  384) → moderate (15 x 256) → busy (10 x 160) → saturated (skip rerank,
  RRF order). Hysteresis: downgrade fast on a load spike, upgrade only
  after the load window clears. The linear cost model (k x pairs x chars +
  overhead) self-calibrates from observed predict durations.
- **Deadline enforcement on ALL stages (v0.9):** dedup-embed, rerank, and
  the fan-out now run under the per-search end-to-end deadline; on expiry
  the stage degrades (URL/title dedup, RRF order) instead of queueing.
  `orchestrator.search` accepts an explicit caller `deadline`.
- **Envelope additions (additive):** `quality` block (tier/label/
  candidates/snippet cap) and `degraded` list — degradation is reported,
  never silent.
- **Two inference executors** (embed / rerank): cheap embed jobs no longer
  wait behind multi-second reranks. Load accounting (`inference.load_estimate`,
  surfaced in `stats_report`) feeds the controller.
- **MMR embeds reuse the dedup pass** — the second encode per search is gone.
- **`research_answer` whole-tool budget** (`KORTEX_SEARCH_ANSWER_TOTAL_TIMEOUT`,
  default 50s): search + synthesis fit one client request; the synthesis
  budget shrinks to whatever the search leg leaves.
- **`--warm` preloads the CJK embed model** — a mid-burst CJK-dominant run
  can no longer stall the queue on a cold load.

### Fixed
- The 2026-09-03 deadline knob only bounded the fan-out; a loaded pipeline
  sailed past `KORTEX_SEARCH_TOTAL_TIMEOUT` in the CPU stages (now bounded).

### Changed
- `KORTEX_SEARCH_RERANK_CANDIDATES` (30) is now the CEILING of the adaptive
  ladder; `KORTEX_SEARCH_RERANK_MAX_SNIPPET` (512) bounds pair length.
- Client-side (opencode): the MCP request timeout for kortex-search is
  raised 30s → 60s to align with the server budgets.

### Tests
- `tests/test_adaptive_quality.py` (17 tests): tier picking + hysteresis,
  cost-model calibration, executor split, load reporting, per-stage
  deadline degradation, saturated-queue skip, a 16-search burst all within
  deadline, caller deadline, `research_answer` total budget.
- `tests/test_loop_liveness.py::test_rerank_runs_off_event_loop` now
  actually exercises the rerank branch (the old FakeSource truncation made
  it vacuous).

## [0.8.3] - 2026-09-04

### Added
- **Fresh-agent journey smoke** integrated into the suite
  (`tests/test_smoke_agent.py`): the full MCP tool surface driven like a
  research agent over stdio — doctor → search_web → CN search →
  search_academic → get_paper → research_answer → read_url → stats_report
  → saved_queries → warm — asserting tool CONTRACTS (envelope shapes,
  non-empty/graceful answers), never specific results. Pins the two bugs
  the original live smoke caught (empty research_answer, missing envelope
  fields). A `slow` social-ladder journey guards honest degradation for
  reddit/twitter. A `gateway`-marked test wraps the live smoke.
- `scripts/smoke_gateway.py`: the live gateway journey (expanded from the
  one-off run) with exit codes — `python3 scripts/smoke_gateway.py` or
  `KORTEX_SEARCH_SMOKE_GATEWAY=1 pytest -m gateway -s`.

## [0.8.2] - 2026-09-04

### Fixed
- `research_answer` could return an EMPTY answer: deepseek-v4 reasoning
  tokens count toward `max_tokens`, so json_mode + thinking=high sometimes
  consumed the whole budget (smoke-test discovery). An empty first
  completion now retries once with thinking disabled; regression test
  added.
- `kortex-search serve --warm`: preloads the rerank+embed models in-process
  after startup (background thread, off the event loop) so a gateway
  restart no longer makes the first search pay the full cold-load cost.
  The systemd unit enables it (`--warm` on ExecStart).

## [0.8.1] - 2026-09-04

### Fixed
- `kortex-search farm login` now works end-to-end: agent-browser keeps ONE
  daemon per user (launch flags are honored only by a fresh daemon), so
  login stops the daemon and settles until it is really gone before
  relaunching headed on the desktop; systemd-run transient units do not
  inherit the caller's env — DISPLAY/XAUTHORITY/TMPDIR/XDG_RUNTIME_DIR are
  forwarded via `--setenv=` (the two-token `--setenv K=V` form was wrong);
  Chrome needs XAUTHORITY to reach the desktop X server (added to the
  allowlists); `input()` moved off the async path with an EOFError guard
  for non-interactive stdin.
- xvfb.service now runs with `-ac` (the gateway unit has no xauth cookie;
  display is loopback-only).
- agent-browser only honors daemon-level flags in the PRE-command position:
  `--headed` after `open` silently kept `headless=new`; both farm launch
  sites now pass `--headed` before the subcommand (pinned empirically).

## [0.8.0] - 2026-09-04

"Managed profile farm" — the browser-backed social tier no longer depends
on the operator's own browser running.

### Added
- **`extract/browserfarm.py`** — profile-farm supervisor: idempotent
  per-profile Chrome launches driven by agent-browser (raw CDP, no
  Playwright shim — the 2026 benchmark's only zero-block class), Redis
  CDP registry shared across gateway processes, per-profile launch locks,
  TCP liveness probes (CLI probes would auto-resurrect browsers), stale
  egress-scope cleanup, `reap_idle`, `status`. Launches are gated by the
  L3 filter and scoped under `ks-egress-<profile>` transient scopes.
- **`sources/base.py::run_profile`** — jittered per-profile pacing +
  browser-budget lease + ensure/exec + block detection.
- **Farm tiers in ladders**: reddit = farm (old.reddit DOM eval) →
  opencli; twitter = twitter-cli → farm (x.com DOM eval) → opencli.
  Login walls are NOT counted as profile failures (no quarantine for
  healthy-but-unauthenticated profiles). `FALLBACK_CHAINS` updated.
- **CLI**: `kortex-search farm status|login <platform>|reap`; doctor gains
  a `farm` section.
- **`infra/systemd/xvfb.service`** — headed-but-invisible rendering
  (headless-new is a measured detection vector). Knobs:
  `KORTEX_SEARCH_FARM_*` (enabled/browser-bin/display/launch+command
  timeouts/idle TTL).
- 9 hermetic farm tests (`tests/test_browserfarm.py`).

### Changed
- Docs: `docs/extraction/BROWSER-TIER-2026.md` (evidence spine +
  architecture), ADR-0009, deployment notes.

### Notes
- Reddit now login-walls all of old.reddit (even /r/rust) — `kortex-search
  farm login reddit` is a hard prerequisite for that source; until then
  the ladder degrades honestly (farm → opencli).
- Xvfb installed + enabled via the user unit on this host (DISPLAY=:99).

## [0.7.2] - 2026-09-03

"OpenAlex-first academics, pacing discipline, social-strategy research."

### Changed
- **Academic fallbacks are OpenAlex-first with a cooldown-gated Semantic
  Scholar**: a 429 now fails FAST and sets a process-wide cooldown
  (`KORTEX_SEARCH_S2_COOLDOWN`, 900s default) instead of the old ~12s
  retry burn per call; the cooldown surfaces in `doctor`
  (`academic.semantic_scholar.cooldown_s`) and gates `available()` probes
  too.
- **Pacing discipline** (social-strategy research, sweep 2026-09-03):
  `RATE_LIMIT_INTERVAL` default 2.5s → 5.0s and the Redis pacing gate now
  jitters the interval at production scale (fixed intervals are themselves
  a fingerprint); `PROXY_STICKY_TTL` default 30m → 24h (IP rotation per
  account is one of the strongest bot signals).

### Fixed
- `read_url` readability stage: `Document` now imports from
  `readability.readability` (the package `__init__` does not re-export it
  on this install — the fallback stage was dead).

### Added
- `docs/extraction/SOCIAL-STRATEGY-2026.md` — evidence-backed social-tier
  strategy from a deep-research run across CN forums (52pojie/V2EX/
  bilibili), RU forums (zenno.club/lolz/habr), stealth-engineering sources
  (Camoufox, rebrowser-patches), and red-team counter-sources. Ledger:
  `~/research_runs/social-strategy-2026-09-03/`.
- S2 429 fast-fail/cooldown regression tests + per-test cooldown reset
  fixture.

## [0.7.1] - 2026-09-03

"Event-loop sweep" — root-caused the recurring MCP crash/timeout the whole
ecosystem kept hitting, and fixed the gateway's silently-broken model
loading.

### Fixed
- **The crash**: `run_cmd` spawned sources with inherited stdio, so a child
  chain that reads its own stdin (mcporter → npx → the exa MCP server)
  became a *second reader on the client's MCP protocol pipe* and stole
  client messages. The session's receive loop starved and unwound, the
  server exited cleanly (rc=0) mid-request, and every in-flight call died
  with -32000/-32001 — the exact "agents fall back to WebFetch" failure.
  All source subprocesses now spawn with `stdin=DEVNULL` (root-caused via
  pipe-inode forensics; reproduced with the official MCP client).
- **The freeze**: CPU-bound model work (imports, lazy loads, batches) ran
  synchronously on the asyncio event loop — a single measured 16.9s
  blocking stretch. Encode/rerank now run off-loop on a single-worker
  inference executor (`kortex_search/inference.py`); `max_workers=1` also
  singleflights cold model loads.
- **The gateway's silent model failure**: `ProtectSystem=strict` made /tmp
  read-only inside the systemd unit; optimum's ONNX load failed
  (`No usable temporary directory found`) and its half-finished torch
  artifact registration then poisoned *every* subsequent model load in the
  process. The unit now sets `TMPDIR` inside its `ReadWritePaths`.
- **Budget discipline**: unbounded legs (50s fan-out + 60s expansion LLM +
  50s expansion fan-out + CPU) could reach 150s+ per search. New
  `KORTEX_SEARCH_TOTAL_TIMEOUT` (45s), `KORTEX_SEARCH_EXPANSION_LLM_TIMEOUT`
  (12s), `KORTEX_SEARCH_ANSWER_LLM_TIMEOUT` (25s) bound every leg; the
  expansion fan-out gates on remaining budget.
- Sync redis-py clients across cache/ratelimit/stats/saved_queries/bilibili/
  profiles/proxies now carry socket timeouts (1s connect / 2s socket) so a
  stalled Redis can never hang the event loop.

### Added
- `warm` MCP tool — preload the rerank + embed models off-loop (15th tool;
  contract + handshake tests updated).
- Regression suite `tests/test_loop_liveness.py`: loop liveness under slow
  model calls, worker serialization, expansion budget, end-to-end deadline,
  synthesis budget, and subprocess-stdin isolation.
- `docs/adrs/0008-serving-topology.md` — single gateway deployment for MCP
  clients; stdio for direct dev.

### Changed
- Recommended opencode registration switches from per-session stdio
  children to the persistent HTTP gateway (single warm process, no
  per-spawn cold-model cost, no RSS duplication across concurrent
  sessions); see `docs/mcp-registration.md`.

## [0.7.0] - 2026-09-01

"Scale, ground, and pool" — two new sources, a shared-HTTP-pool
architecture, and grounded answer synthesis with deterministic citation
verification.

### Added
- **hackernews source** (Algolia API, no auth): stories with points/
  comment engagement, item-link synthesis, live-verified.
- **wikipedia source** (MediaWiki CirrusSearch, no auth): full-text
  search, HTML-snippet stripping, URL-quoted titles. Registry 23 → **25**.
- **Grounded `research_answer`**: strict cite-during-write synthesis
  (inline `[N]` markers, JSON mode on DeepSeek) + a deterministic
  verification pass — id-space enforcement, URL membership, verbatim
  quote-substring checks. Unverifiable citations are dropped and counted
  (`verification{}` block); JSON degradation is reported, never silent.
- **Nightly property sweep** CI job (`HYPOTHESIS_PROFILE=sweep`,
  non-deterministic bug hunt) on schedule.

### Changed
- **Shared HTTP pools** (perf): the facade, the read_url scrape tier, and
  the LLM client each own ONE pooled client (per-origin keep-alive,
  `keepalive_expiry=60s`, tier-scoped cookie jars, `trust_env=False`) —
  the fan-out no longer pays TCP+TLS handshakes per source call.
  Graceful `aclose()` on shutdown; tests reset the pools per test.

### Fixed
- `_verify_citations` marker handling (markers of verified citations
  were stripped — found by the new grounding tests before ship).

## [0.6.1] - 2026-08-31


"Deep sweep" — property-based testing layer + the bugs it caught + a
security pass across containment, SSRF, and the vault.

### Added
- **Hypothesis property suite** (`tests/test_properties.py`): date-parser
  round-trips, canonicalization idempotence, fusion differential oracle,
  nearest-rank percentile reference, extraction-ladder + challenge
  classifier no-crash/shape contracts, empty-string env contract.
  dev/ci/sweep profiles via `HYPOTHESIS_PROFILE`.
- **Linting & security tooling**: ruff rules extended (C4, PT, ASYNC),
  bandit + pip-audit + gitleaks CI `security` job, per-code skip
  rationales in `docs/security.md`.
- **Root loader unit** `ks-egress.service`: validates (`nft -c`) then
  applies the root-owned `/etc/kortex-search/ks-egress.nft` at boot.
- `harden --status` reports `system_rules` (is the root-owned copy in place).

### Fixed
- **Date parsing**: ISO-T timestamps lost the last seconds digit
  (slice-by-`len(fmt)` trap) — `T12:30:59` parsed as 12:30:05; the
  compact-date regex was unanchored (13-digit epoch-millis parsed as
  year-8796 "fresh"); compact dates now carry a 1970–2100 sanity range.
- **Challenge classifier** crashed on non-string bodies/headers; hostile
  adapters now read as no-body and headers still classify.
- **Percentiles** used floor instead of ceil for nearest-rank — p95
  under-picked for non-integer products.
- **nan/inf poisoning**: Redis-backed pacing, latency reservoirs, and
  adaptive timeouts now treat non-finite values as absent (poisoned
  slots reclaimed, never slept on).
- **read_url SSRF**: jina targets percent-encoded into one path segment;
  per-hop pre-connection egress guards on the redirect-following stages
  (previously only the final URL was checked).
- **Vault TOCTOU**: symlink-refusing mkdir + O_EXCL/O_NOFOLLOW atomic
  0600 writes; migration refusals report per-kind without touching
  sources.
- **Dependency CVEs**: torch→2.13 (PYSEC-2025-194); idna/soupsieve/
  lxml-html-clean/gitpython/pillow bumped; transformers pinned <4.58
  (optimum-onnx ceiling) with SHA-pinned-revision mitigation documented
  for the four 5.x-only advisories.

### Security
- **Removed the sudoers escalation seam**: the NOPASSWD `nft -f` grant on
  a user-writable ruleset could let any kbj-uid process (including the
  browser automation) inject rules parsed by root into the nf_tables
  kernel surface. The old companion user loader + sudoers drop-in are
  retired; boot loading happens via the root unit on a root-owned file.

## [0.6.0] - 2026-08-29

"Rename bridge removal" — the 0.5.x `SEARCH_GATEWAY_*` fallback is gone.

### Removed
- **`SEARCH_GATEWAY_*` env fallback** (`config._read_env` legacy branch +
  the once-per-variable deprecation warning). Only `KORTEX_SEARCH_*` is
  read now. Stale env files, units, and shells still carrying the old
  prefix silently lose those settings — migrate to the new prefix.

### Fixed
- **CI red since 0.4.2**: `test_vault_status` passed locally only by
  accident (the machine's real vault files tripped the out-of-vault
  hygiene finding); in CI's clean HOME the assertion flipped. The fixture
  is now hermetic — VAULT_DIR points at a nonexistent path and
  `_CONFIG_PATHS` is patched too, asserting the intended
  "missing vault → warn" contract.
- **Time-bomb freshness fixtures**: two tests hardcoded
  `published=2026-08-20` against `freshness=week` and aged out of their
  own window two days later; fixtures are now date-relative.
- **Residual `sg-` doc stragglers** from the rename (deployment.md
  sudoers heredoc pointed at the old ruleset filename — a broken drop-in
  waiting to happen).

## [0.5.0] - 2026-08-26

"Rename to Kortex Search" — product rebrand from Search Gateway.

### Changed (breaking)
- **Package** `search_gateway` → `kortex_search` (imports, loggers, module paths).
- **Console script / binary** `search-gateway` → `kortex-search`
  (`[project.scripts]` entry, CLI `prog`, FastMCP server name).
- **Env prefix** `SEARCH_GATEWAY_*` → `KORTEX_SEARCH_*`. The old prefix is
  still read as a deprecated fallback (warns once per variable; removal in
  0.6.0). Unprefixed vars (`SEARXNG_*`, `GITHUB_TOKEN`, `DEEPSEEK_*`,
  `ZHIHU_COOKIE`, `WEIBO_SUB`, `TWITTER_AUTH_FILE`, `DEEPSEEK_AUTH_FILE`)
  are unchanged.
- **Redis key prefix** `sg:` → `ks:` (cache, rate-limit, stats, saved
  queries). `sg:sq:*` saved queries are migrated on upgrade; TTL-bound
  caches/stats expire naturally.
- **Systemd units** `search-gateway@.service` → `kortex-search@.service`,
  `search-gateway-harden.service` → `kortex-search-harden.service`; config
  dir `~/.config/search-gateway/` → `~/.config/kortex-search/`.
- **Egress filter** nft table/scope `sg_egress`/`sg-egress` →
  `ks_egress`/`ks-egress`; ruleset file `sg-egress.nft` → `ks-egress.nft`.
- Repo renamed (local + GitHub `kbjama8/kortex-search`); opencode MCP key
  `mcp.kortex-search` (tool prefix `kortex-search_*`).

## [0.4.3] - 2026-08-23

"CN truth + vault finalization" (PHASE8 Plan 1/2/3/5 + D7.3).

### Fixed
- **Baidu hot board was silently broken** (R12, live-verified 2026-08-23):
  the endpoint's JSON structure changed (`cards[]` with `word`/`hotScore` →
  `tabTextList` with nested `content[].content[]` items carrying
  `word`/`url`/`index`/`hotTag`/`newHotName`), so the parser returned
  empty results. Dual-shape parser now handles both shapes, and a
  **shape-drift guard** raises `SourceError` on any 200-with-cards-but-
  unparseable response — the silent-empty failure mode is closed for good.

### Added
- **Zhihu hot-list source** (`zhihu_hot`, R11): the anonymous
  `api.zhihu.com/topstory/hot-list` endpoint (verified live, 30-item cap)
  gives zhihu a zero-cookie presence alongside the cookie-gated v4 search;
  URLs rewritten to the human surface; registry 22 → **23 sources** (contract
  + `check` gate updated in the same commit).
- Fixtures for both CN fixes under `tests/fixtures/platforms/` (recorded
  live captures).
- `docs/extraction/stealth-matrix.md` (R2): nodriver vs Patchright vs
  Camoufox capability matrix + adoption triggers — insurance, not adoption.
- `docs/adrs/0007-cn-signing-deferred.md` (Plan 5): Douyin/XHS stay deferred
  with explicit revival triggers (XHS drift + 406 since Mar 2026; Douyin
  x-gorgon emulation cost).
- `docs/extraction/PHASE8-PLAN.md`: the approved consolidation plan.

### Changed
- **D7.3 lands**: legacy flat secret paths (`~/.agent-reach/twitter-auth.env`
  etc.) are **removed** — `env_file_for` resolves the vault path only;
  `migrate()` remains as the migration path for never-migrated machines
  (migrate before upgrading past 0.4.2). Doctor `vault` section drops
  `legacy_in_use`.

## [0.4.2] - 2026-08-23

Phase 7 (containment & observability), release 2 of 2 — the sharp end:
L2 forced-proxy for the anonymous tier, the **mandatory** L3 kernel egress
filter, bench browser tier, and the hardened systemd unit
(`docs/extraction/PHASE7-HANDOFF.md`, decisions D7.1/D7.2).

### Added
- **L3 kernel egress filter** (`extract/harden.py`, decision D7.1):
  nftables `socket cgroupv2` per-cgroup DROP rules for private/link-local/
  metadata ranges (pam_authnft pattern — no suid binary, no kernel module,
  LESSONS.md §1.5). `search-gateway harden --install|--status|--uninstall|
  --check [--sudo]`; `build_rules()` is a pure function with a golden
  ruleset test; all kernel probes mocked in CI.
- **Mandatory enforcement (D7.1)**: browser-tier ops (`run_opencli` +
  Camoufox launch) refuse to run until the filter is installed — the
  explicit `blocked (egress-unhardened): run 'search-gateway harden
  --install --sudo'` message reaches the envelope and negative cache.
  `SEARCH_GATEWAY_HARDEN=required` (default) | `permissive` (sandboxed-CI
  opt-out). OpenCLI children are wrapped in a `systemd-run --user --scope
  --unit=sg-egress` transient scope when the gateway itself is not inside
  the scoped cgroup; Camoufox requires the gateway to be scoped (its
  children cannot be wrapped).
- **L2 forced-proxy** (`egress.EgressProxy`, decision D7.2): loopback
  CONNECT/absolute-URI HTTP proxy fronting the anonymous tier; every target
  re-passes the floor (403 + telemetry), chains through the residential
  tier when enabled, origin-form rewriting, lazy singleton.
  `SEARCH_GATEWAY_EGRESS_PROXY=1` (default ON for the anonymous tier).
  Camoufox launches with `--proxy-server` + `--host-resolver-rules="MAP *
  0.0.0.0, EXCLUDE 127.0.0.1"` (remote DNS through the proxy too).
- **bench browser tier** (`scripts/bench.py browser`, slow): egress-floor
  overhead, L2 proxy roundtrip vs direct, Camoufox cold/warm launch,
  navigate→extract→teardown, profile rotation, RSS delta — each measurement
  SKIPs with a reason when its dependency is absent.
- **Hardened systemd unit** (`infra/systemd/search-gateway@.service`):
  `LoadCredential=` for the vault (`$CREDENTIALS_DIRECTORY` bridge),
  `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict` + `ReadWritePaths`
  for vault/HF-cache, `RestrictAddressFamilies`, service-level `IPAddressDeny`
  (loopback stays open for Redis/SearXNG), `ExecStartPre` installs + reports
  the L3 filter for the unit's cgroup. `systemd-analyze verify` exits 0.

### Changed
- `doctor`'s `egress` section is now fully populated: floor + proxy + kernel
  state (`mode`/`installed`/`covered`) + denial counters.
- `.env.example` / `docs/config-reference.md`: 3 new variables
  (`SEARCH_GATEWAY_EGRESS_PROXY`, `SEARCH_GATEWAY_HARDEN`,
  `SEARCH_GATEWAY_HARDEN_SUDO`) → 82 documented.
- Deployment docs: §3.2 kernel-filter walkthrough (ad-hoc scope, systemd
  unit, `sudo nft -f` one-time load).

## [0.4.1] - 2026-08-23

Phase 7 (containment & observability), release 1 of 2 — the zero-risk floor:
L1 egress floor, per-persona secrets vault with full migration, and block
telemetry. L2/L3 land in 0.4.2 (`docs/extraction/PHASE7-HANDOFF.md`, decisions
D7.1–D7.4).

### Added
- **L1 egress floor** (`extract/egress.py`): always-blocked private/link-local/
  metadata ranges (RFC1918, CGNAT, IMDS across AWS/GCP/Azure/Alibaba, loopback
  v4+v6) checked **pre-nav and post-redirect** on every extraction path — API
  tier (`extract/http.py`), all `read_url` stages (`sources/web.py`), the
  Camoufox adapter, and the `read_url` server tool. Pure IP/hostname matching,
  no DNS in the floor; the L3 kernel filter catches what the floor can't see.
  Local-infra exemption only (`SEARXNG_BASE`/`REDIS_URL` hosts +
  `SEARCH_GATEWAY_FLOOR_EXEMPT`). Default **ON** by design.
- **Per-persona secrets vault** (`extract/vault.py`): secrets move to
  `~/.agent-reach/profiles/<persona>/{twitter,deepseek,proxy}.env` (0600,
  decision D7.3). `search-gateway vault migrate [--dry-run]` / `vault status`;
  hygiene checks (mode enforcement, symlink traps, stale files, out-of-vault
  config) warn-not-fail but color doctor red. Legacy flat paths honored one
  release with a doctor deprecation warning (removed 0.4.3).
- **systemd credentials bridge**: `SEARCH_GATEWAY_CREDENTIALS_DIR`
  (`$CREDENTIALS_DIRECTORY`) — secrets arrive as files, never env vars
  (LESSONS.md §1.5).
- **Block-event telemetry** (`stats.record_block` / `blocks_snapshot`): bounded
  24h reservoir `sg:bl:<source>:<vendor>` recorded from the subprocess raise
  site, the envelope chokepoint (non-raising blocked strings only — no double
  counting), and egress denials.
- **Doctor sections**: `egress{floor,proxy,kernel,denied_count,last_denial}`,
  `vault{profiles,hygiene,findings}`, `blocks`, `profiles` (Phase 2 profile
  farm status). `stats_report` gains `blocks`.

### Changed
- `TWITTER_AUTH_FILE` / `DEEPSEEK_AUTH_FILE` / `SEARCH_GATEWAY_PROXY_AUTH_FILE`
  defaults now point into the vault; consumers resolve via the vault chain
  (credentials bridge → vault → legacy).
- `.env.example` / `docs/config-reference.md`: 6 new variables (containment +
  vault + telemetry).

### Security
- SSRF floor: the whole link-local range is blocked, not just the well-known
  IMDS address — a post-redirect jump to any private/metadata host now fails
  with an explicit `blocked (egress-floor/…)` envelope signal.

## [0.4.0] - 2026-08-22

Project Gatekeeper — the extraction-architecture overhaul
(`docs/extraction/PLAN.md`).

### Added
- `search_gateway/extract/` package: multi-shape parsing (`parse.py`),
  block & challenge intelligence (`detectors.py`), extraction tiering
  (`router.py`), browser budget + jittered pacing (`scheduler.py`), browser
  profile farm + health state machine (`profiles.py`), fingerprint bundles +
  coherence lint + geo alignment (`fingerprints.py`), env-gated proxy
  subsystem with sticky sessions (`proxies.py`), HTTP facade with optional
  curl_cffi TLS/JA3 impersonation (`http.py`), Camoufox anonymous-tier
  adapter, experimental (`camoufox.py`).
- Envelope v0.4 signals (additive): `extract{tier}`, `blocked[]`, `auth{}`.
- `read_url` multi-stage extraction: Jina (SPA) → Trafilatura (precision) →
  readability (recall), configurable via `SEARCH_GATEWAY_READ_URL_STAGES`.
- Bilibili **wbi signing** (w_rid/wts, Redis-cached daily keys) — verified
  against the canonical bilibili-API-collect worked example.
- Chinese-ecosystem tier (gated by `SEARCH_GATEWAY_CN_SOURCES=1`):
  **zhihu** (v4 search API, cookie-gated), **weibo** (hot search + SUB-gated
  keyword search), **baidu** + **toutiao** hot boards (public, no auth).
  Registry 18 → **22 sources**.
- Block detection in the subprocess layer: challenge walls fail immediately
  (`blocked (vendor/level)`) instead of burning retries; Cloudflare's
  official `cf-mitigated: challenge` header is the primary signal.
- `docs/extraction/`: PLAN (assignment), LESSONS (research journal),
  `proxy-funding-guide.md`, `camoufox-migration.md`, `project-map.md` vision
  & goals section.

### Changed
- `OPENCLI_LOCK` replaced by the configurable browser budget
  (`SEARCH_GATEWAY_BROWSER_BUDGET`, default 1 — old single-bridge behavior
  preserved).
- Core deps: `trafilatura`, `readability-lxml` (read_url stages).
- `search-gateway check` gate: 18 → 22 sources.
- Golden contract test: 22 sources + 14 tools + `Result` shape.

### Fixed
- Bilibili search silently degrading without wbi signing (unsigned requests
  returned `v_voucher` risk-control payloads).

## [0.3.0] - 2026-08-15

### Added
- ONNX Runtime inference backend for the cross-encoder, on by default
  (`SEARCH_GATEWAY_INFERENCE_BACKEND=onnx_int8`): ~2× faster re-rank and ~1GB
  smaller RSS than torch at Spearman ≈ 0.96 ranking agreement. `onnx`
  (fp32) and `torch` remain available; the reranker falls back to torch
  automatically if the ONNX model can't load.
- `scripts/bench.py` — micro/model/search benchmark harness (p50/p90, RSS,
  subprocess cold-start measurement).

### Changed
- `optimum[onnxruntime]` + `onnxruntime` are now core dependencies (was the
  `.[onnx]` optional extra).
- Docs rewritten in the hybrid answer-synthesis voice with Mermaid diagrams;
  six ADRs, a FAQ, and a CONTRIBUTING guide added.

### Fixed
- `saved_queries` MCP tool crashed with `AttributeError` (module shadowed by
  the same-named tool function) — now aliased and regression-guarded.
- `diversity._domain` misused `str.lstrip("www.")` (character-set semantics,
  mangling domains like `worldwide.com`) — now `removeprefix`.
- Embedding loader cold-start hits the Hugging Face API even on a warm cache —
  now `local_files_only=True` fast path with `snapshot_download` on miss.
- `get_paper` awaited its independent sub-lookups sequentially — now
  `asyncio.gather`.

## [0.2.0] - 2026-08-14

Standalone, client-agnostic release: the gateway decouples from OpenCode into
its own repo (public, MIT) while the orchestration skills ship alongside it.

### Added
- Console entry point `search-gateway` with `serve` (default, stdio),
  `doctor`, `check`, `version`, and `warm` subcommands (`cli.py`, `health.py`).
- Optional HTTP/SSE transports (`serve --transport http|sse --host --port`) for
  a long-running host process.
- Structured logging (`SEARCH_GATEWAY_LOG_FMT=json|text`, `SEARCH_GATEWAY_LOG_LEVEL`)
  — always to stderr; SIGTERM/SIGINT graceful shutdown.
- Repo hygiene: MIT `LICENSE`, `.gitignore`, `.env.example`, `mcp.json`,
  `install.sh` (idempotent skill symlinks), `CHANGELOG.md`.
- Canonical docs: `api/tools.md`, `meta-schema.md`, `config-reference.md`,
  `deployment.md`, `architecture.md`, `mcp-registration.md`, `security.md`,
  plus `docs/history/` (project archaeology).
- Infra: `docker-compose.yml` (Redis AOF + SearXNG JSON), headless tier-1
  `Dockerfile`, `systemd/search-gateway@.service`.
- CI (`ci.yml`, Python 3.12/3.13, `pytest -m "not slow"`) and a golden
  contract test (18 sources + 14 tools + `Result` surface).
- Regression tests for the bare `search-gateway` stdio handshake and the
  `saved_queries` tool (`tests/test_mcp_handshake.py`); the fast suite is now
  16 tests.
- `docs/voice.md` — the hybrid answer-synthesis voice contract; the docs were
  rewritten to it (narrative voice-card register for README/architecture,
  reference research_answer register for the rest, with Mermaid diagrams).

### Changed
- Orchestration skills (`deep-research`, `master-router`, `report`, `monitor`,
  `research-rubric`) now ship in this repo and are symlinked into client skill
  dirs; docs de-pathed to `$SKILL_DIR`-relative.
- `diagram-design` is now a **git submodule** (pinned commit) instead of a
  vendored copy.
- Version is sourced from `search_gateway/__version__` (`dynamic` in
  `pyproject.toml`).

### Fixed
- Declared `huggingface-hub` and `numpy` as direct dependencies (previously
  transitive-only).
- Pinned Hugging Face model revisions (`bge-reranker-v2-m3`, `all-MiniLM-L6-v2`,
  `bge-m3`) via `*_REVISION` env vars to stop the commit-churn re-download bug.
- `saved_queries` MCP tool crashed with `AttributeError`: the same-named tool
  function shadowed the module import. Now aliased (`sq`), verified end-to-end,
  and regression-guarded.
- `diversity._domain` misused `str.lstrip("www.")` (which strips a *character
  set*, mangling domains like `worldwide.com`); now `removeprefix`.
- File-handle leaks in `load_env_file` and the twitter env loader (`open()`
  without a context manager).
- Removed unused variables/imports; `ruff` F-rule checks are clean across the
  package and tests.

## [0.1.0] - 2026-08-14

### Added
- Unified web-search & research MCP server (FastMCP, stdio) fusing 18 sources
  behind one `search` tool: weighted RRF fusion, de-duplication, cross-encoder
  re-rank, MMR diversity, freshness filtering, Redis cache.
- 14 MCP tools: `search`, `search_web`, `search_news`, `search_science`,
  `search_social`, `search_academic`, `get_paper`, `get_citations`,
  `get_references`, `research_answer`, `read_url`, `doctor`, `stats_report`,
  `saved_queries`.
- Orchestration skills (deep-research, master-router, report, monitor,
  research-rubric) and a pytest regression guard.
