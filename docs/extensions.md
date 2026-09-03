# Extensions & Modern Capabilities — mcp-sdk-bench

> **STATUS: skeleton.** Research in Phase 1, experiments in M3–M5. Nothing here is asserted as fact until `docs/research/` findings land and executable tests pass.

Sections below mirror SPEC.md experiments. Each will be expanded with: current spec/SDK status (primary sources), what was implemented, measurements, and verdicts.

## 1. MCP Apps (SPEC.md §12) — M4

`inventory_dashboard` app: display inventory, select item, reserve item, refresh state. Minimal benchmark host for render/structural validation.

Open items: Apps extension spec state; negotiation flow; UI resource discovery; tool→UI association; host message contract; security metadata; fallback behavior when client lacks support.

## 2. Custom extension `io.mcpbench.audit` (SPEC.md §16) — M4

Capability advertisement → negotiation → extension-specific metadata → extension request/result → unsupported-client behavior. Implemented on official facilities first, then FastMCP/ADK cost comparison.

## 3. Tasks (SPEC.md §17) — M3

`generate_monthly_report()`: task handle → running → progress → completed; cancellation, failure, reconnect-during-task, concurrency. Three-way distinction recorded: spec supports / SDK implements / framework-native equivalent.

## 4. Elicitation & multi-round-trip (SPEC.md §18) — M3

`reserve_inventory` clarification flow; `deploy_service` approval flow. Measures: application code vs protocol-specific code, agent success, round trips, failure modes.

## 5. Auth & identity (SPEC.md §19) — M5

Local OAuth-like environment: human → agent → client → server → backend. Identity propagation without model-visible credentials. Investigate: OAuth, client credentials, ID-JAG/identity assertions (if applicable), per-request metadata, DI, scopes, authz failures.

## 6. Multi-server (SPEC.md §20) — M5

people-mcp / ticket-mcp / inventory-mcp. Compare ClientGroup (FastMCP), ADK toolsets, official client composition. Measure config complexity, lifecycle, name collisions, provenance, routing, failure isolation.

## 7. Skills over MCP (SPEC.md §13–15) — M5

Maturity classification first (standard / official extension / draft SEP / experimental convention / ecosystem-specific — SPEC.md §29). `incident_triage` skill + three-way experiment (no skill / prompt-embedded / dynamic discovery) + progressive disclosure comparison.
