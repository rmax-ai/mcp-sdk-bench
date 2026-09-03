# ROADMAP — mcp-sdk-bench

Implements SPEC.md §32 milestone plan, mapped to the full-project-development orchestration phases.

## Phase 0 — Scope extraction ✅ (this commit)

- SPEC.md preserved verbatim as ground truth
- docs/architecture.md, docs/methodology.md, capability-matrix skeleton, extensions skeleton
- GitHub repo + label system + phase issues + Plan epic

## Phase 1 — Research (current state → evidence)

**Deliverables:**
- `docs/research/` findings document covering, from primary sources only:
  - Current PyPI/GitHub releases: fastmcp, google-adk, official `mcp` SDK (v2 line), protocol version support
  - MCP spec 2026-07-28 status + negotiation/back-compat story
  - MCP Apps extension — spec state, reference implementation, host requirements
  - Tasks extension — spec vs SDK implementation status (SPEC.md §17's three-way distinction)
  - Skills over MCP — working group, SEPs, reference impls, registry/packaging conventions; maturity classification per SPEC.md §29
  - Elicitation/sampling/structured content status in each SDK
  - Prior-art scan: `rmax-ai/mcp-conformance`, `rmax-ai/mcp-auth-test-server`, `rmax-ai/mcph` for reusable harness pieces
- `results/environment.json` version pinning

**Acceptance:** every capability-matrix row has a primary-source link; SDK versions pinned in `uv.lock`; maturity classifications recorded.

## Phase 2 — Supporting files

- Full README.md, ARCHITECTURE.md companion sections, DECISIONS.md (recorded research decisions), `<PY>` companion docs as needed

## Phase 3 — GitHub setup completion

- Epic/story breakdown for M1–M5 (milestone labels, story issues per milestone)
- SPEC.md cross-check against issue bodies

## Phase 4 — Implementation (Droid, sequential background sessions)

### M1 — Vertical slice (SPEC.md §32)
Three comparable integrations → shared world → 5 tools / 1 resource / 1 prompt → LangGraph agent → 10 eval tasks → deterministic graders → normalized traces → comparison report.

**Acceptance:** `uv run mcpbench benchmark` + `uv run mcpbench report` produce results for all three SDKs end-to-end.

### M2 — Failure injection + interoperability
Full conformance suite (§8: discovery/schema/errors/concurrency/lifecycle) + cross-implementation pairings + fault injection (§21) + idempotency study.

### M3 — Elicitation + multi-round-trip + tasks
§18 elicitation flows, Tasks experiment (§17: run/progress/cancel/failure/reconnect/concurrency).

### M4 — MCP Apps + extensions
inventory_dashboard app + minimal benchmark host (§12), custom `io.mcpbench.audit` extension (§16), capability negotiation tests.

### M5 — Skills over MCP + progressive disclosure
incident_triage skill (§13), three-way skills experiment (§14), progressive disclosure comparison (§15), multi-server experiment (§20), auth/identity experiment (§19).

## Phase 5 — Verification

- Hard gates: ruff → type check → pytest
- `validate-project-docs` claims audit
- Report generation (SPEC.md §27 structure) → `docs/findings.md` + `results/latest/*.json`
- Close epic, tag release

## Phase 6 — Website

Landing page for the project (gemini → codex → droid pipeline).

## Open questions (decide in Phase 1)

- LangGraph version pinning; which OpenAI-compatible model as the pinned default (cost: N=10 × 40 tasks × 3 SDKs ≈ 1,200+ agent runs — budget impact assessed in Phase 1)
- Model API key provisioning path for benchmark runs (`.envrc` + direnv pattern)
- Whether ADK MCP client variant exists standalone (ADK toolsets) for the interop matrix, vs server-only
