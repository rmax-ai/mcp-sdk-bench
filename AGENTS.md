# AGENTS.md — mcp-sdk-bench

Project DNA: an evidence-first benchmark comparing **FastMCP 4.x**, **Google ADK's MCP integration**, and the **official MCP Python SDK v2**. Not a product, not a tutorial, not a feature-comparison article. The deliverable is falsifiable experimental evidence (SPEC.md §31–33).

## Non-negotiables

1. **Version discipline.** Check live PyPI/GitHub releases before writing any SDK code. Record everything in `results/environment.json` (SPEC.md §28). Never rely on training knowledge for API shapes.
2. **One independent variable.** Same model, temperature, prompts, task order, MCP schemas, world state, timeout policy, agent loop across SDK variants (SPEC.md §23). Only the MCP integration under test changes.
3. **Deterministic graders first.** Correct final state > textual answer. LLM judges only when state inspection is genuinely insufficient (SPEC.md §10).
4. **Honest capability labels.** `supported / partially supported / via-other-abstraction / experimental / unsupported / not-applicable`. Never fake symmetry to complete a table (SPEC.md §7).
5. **Evidence-linked claims.** Every capability-matrix cell needs (a) a primary documentation link and (b) an executable test. Docs-only ≠ verified (SPEC.md §7).
6. **Cross-implementation interop.** A framework working against itself is weak evidence. All meaningful client/server pairings must run (SPEC.md §8).
7. **Provenance classification.** Every feature tagged PROVEN / IMPLEMENTED-BUT-OPTIONAL / EMERGING / EXPERIMENTAL / SPECULATIVE (SPEC.md §29).
8. **No credentials in tool schemas.** Identity experiments never expose secrets in model-visible parameters (SPEC.md §19).

## Repo conventions

- `uv` + src layout. `uv.lock` is **committed** (benchmark reproducibility — this is an app, not a library).
- Lint/type/test: ruff, pyright (or mypy — pinned at bootstrap), pytest. Deterministic seeds everywhere.
- Tests mirror SPEC.md §8: `tests/conformance/` (discovery, schema, errors, concurrency, lifecycle), `tests/interoperability/`, `tests/failures/`, `tests/regression/`.
- Datasets are JSONL with stable task IDs. Results keyed by `run_id` + `sdk`. `results/` is gitignored except `.gitkeep`; `results/latest/` is the canonical report artifact.
- Traces follow SPEC.md §22 format, exported as JSONL.
- World state lives in `src/world/` — single source of truth shared by all server variants and graders.
- Fault injection is deterministic: seeded RNG, env-var-driven parameters (SPEC.md §21).

## Execution conventions

- Milestones M1→M5 strictly in order (SPEC.md §32). The M1 vertical slice must work before any extension experiments exist.
- Python implementation is delegated to Droid with explicit prompts; harness/verification is deterministic-first. Fix build gates directly — they are infrastructure prerequisites, not implementation work.
- Prompts for delegated agents must reference SPEC.md sections and this file.
- Machine constraint: benchmark runs on a low-resource host (2 cores / ~4 GiB). Absolute latency numbers are machine-specific — always report relative deltas between SDKs, never absolute values as portable claims.
- Network-bound experiments are forbidden (SPEC.md §5) — the world is in-memory/SQLite only. Model API calls are the single permitted network dependency.
