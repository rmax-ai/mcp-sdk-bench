# DECISIONS.md — mcp-sdk-bench

Design rationale. Each entry: decision, why, what was rejected.

## D1 — Three dependency universes, adapter boundary = process boundary

**Decision:** each SDK variant runs in its own uv environment; server variants are subprocesses; the harness + official client live in the main env.

**Evidence (docs/research/findings.md §10):** `google-adk[mcp]` pins `mcp>=1.24,<2` — its MCP toolset imports `mcp.shared.session`, which does not exist in mcp 2.1.1 (import failure verified against installed packages). FastMCP 4 is decoupled (depends only on `mcp-types`). One venv cannot hold all three.

**Rejected:** a single shared venv (hard incompatibility); Docker isolation (no docker on the benchmark host, memory-constrained); monkeypatching ADK internals (fragile, pollutes the experiment).

**Consequence:** the SPEC §2 adapter boundary doubles as the process boundary. Interop pairings become genuine cross-implementation tests (FastMCP client ↔ official server, etc.).

## D2 — Harness stack: LangGraph + thin adapter, official client in main env

**Decision:** LangGraph 1.2.11 agent; benchmark-owned adapter; ADK never the common benchmark agent (SPEC §2 — ADK is a candidate).

**Rejected:** ADK as benchmark agent (candidate bias); any framework's native MCP abstraction as the main experiment surface (hides candidate-specific behavior).

## D3 — Python 3.13 pin

**Decision:** `.python-version = 3.13`, `requires-python >=3.13`.

**Why:** `uv init` resolved the venv to CPython 3.14.5 (uv-managed). 3.14 is too new for benchmark reproducibility (wheel availability across the three ecosystems); 3.13 matches the host's system interpreter. All candidates support ≥3.10.

## D4 — uv.lock committed

**Decision:** commit `uv.lock`. This is an app (benchmark), not a library — reproducibility across runs and machines is the point.

## D5 — ty for type checking

**Decision:** ruff (lint) → ty (types) → pytest, pinned (0.16.6 / 0.0.78 / 9.1.1).

**Rejected:** mypy (slower, more setup), pyright (node runtime). ty pairs with ruff and is fast enough for a 2-core host.

## D6 — Relative latency deltas only

**Decision:** all performance numbers are reported as per-SDK relative deltas on the benchmark host (2 cores / ~4 GiB). Absolute numbers are machine-specific noise.

## D7 — Skills over MCP classified EXPERIMENTAL

**Decision:** SEP-2640 is Draft (declined once by core maintainers 2026-06-24, vote pending as of 2026-08-11). The M5 experiment implements against the draft's resource-based model and reports honestly where SDK support is absent. No capability claim above EXPERIMENTAL without a passing test.

## D8 — Reuse rmax-ai prior art

**Decision:** build the conformance layer on `rmax-ai/mcp-conformance` (scenario-driven runner) and/or `rmax-ai/mcph` (MCP-Hurl DSL); the §19 auth experiment on `rmax-ai/mcp-auth-test-server` (OAuth 1.0a/2.0/2.1/Bearer test endpoints). Reuse over reinvention; cite, don't fork silently.

**M2.1 addendum (2026-09-04):** the conformance suite landed as native pytest in `tests/conformance/` with a deterministic stdio corrupting/delaying/dropping proxy (`helpers.py StdioProxy`), not by embedding mcph/mcp-conformance. Rationale: the world is stdio-subprocess servers driven through each SDK's own client — mcph targets stdio but its DSL+runner would add a cross-repo runtime dep for assertions the native suite already encodes per-candidate; mcp-conformance's scenario runner is HTTP-partner oriented and a poor fit. Prior art consulted and credited in module docstrings; M2.1 keeps the wire-level checks (malformed frames, connection loss, timeouts) that motivated D8.

## D9 — Results policy

**Decision:** `results/environment.json` is committed (environment pin record). `results/latest/*` is generated output — regenerated per run, not hand-edited.

## D10 — Public + MIT

**Decision:** repo public, MIT (SPEC says open-source research project; org default). LICENSE added Phase 2.

## D11 — Grading policy

**Decision:** deterministic state graders first (correct final state > textual answer); outcome and trajectory scored independently (SPEC §11); LLM judge only where state inspection genuinely cannot grade.

## D12 — Model for benchmark runs (decided)

**Decision:** main runs on `deepseek-v4-flash`, smoke subset on `deepseek-v4-pro` (user directive 2026-09-03). Configured via `.envrc` (MODEL_PROVIDER=deepseek, MODEL_NAME, MODEL_API_KEY from pass). Model identity is pinned per run and recorded in `results/latest/summary.json` environment block — never a variable.
