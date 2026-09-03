# PY_DEVELOPMENT.md — Python engineering idioms for mcp-sdk-bench

Day-to-day conventions for harness and delegated implementation code. Companion to AGENTS.md (hub).

## Async model

- All MCP server implementations are `async def` (all three candidates are anyio/asyncio native).
- The harness uses `asyncio` + `anyio` (already a transitive dep of the SDKs). One event loop per benchmark run; no threads for MCP I/O.
- Server subprocess lifecycle: spawn → health-check via stdio handshake (or HTTP readiness for Streamable HTTP) → supervise → terminate. Never `shell=True`.

## Process boundaries (D1)

- Each variant env is spawned with `uv run --project <variant-env> python -m mcp_sdk_bench.servers.<variant>.main` — the child process's environment must NOT inherit `MODEL_API_KEY` (servers don't need it; least privilege per SPEC §19).
- ADK variant env: `google-adk[mcp]` pins mcp 1.x — its lockfile is separate (`envs/adk/pyproject.toml` + lock).

## Determinism

- Every RNG is seeded (`random.seed`, `numpy` if used). World state resets per task from fixtures (`src/world/reset.py`).
- Fault injection is env-var driven with a seeded PRNG: `FAIL_TOOL_CALL`, `LATENCY_MS`, `DROP_CONNECTION_AFTER`, `MALFORMED_RESPONSE_RATE`, `TASK_FAILURE_RATE` (SPEC §21).
- Task order is fixed by dataset file order; datasets are JSONL with stable task IDs.

## Errors

- Tool handlers raise domain errors that the SDK layers map to MCP error responses — never bare `except:`.
- The harness records protocol errors per SPEC §10 (`protocol_errors` metric); an SDK mis-mapping (e.g. application error surfaced as transport error) is a finding, not a test failure.

## Testing

- `tests/conformance/` — protocol-level, no LLM (SPEC §8 layers: discovery, schema, errors, concurrency, lifecycle).
- `tests/interoperability/` — cross-implementation pairings; self-pairing is weak evidence (SPEC §8).
- `tests/failures/` — fault injection + idempotency (a retried create_ticket must not create two tickets).
- `tests/regression/` — everything else.
- Gates, in order: `ruff check .` → `ty check` → `pytest`. Stop at first failure.

## Traces

- Normalized format per SPEC §22: `{run_id, sdk, task, events[]}` with event types `model_call`, `mcp.discover`, `mcp.tool_call`, etc.
- Exported as JSONL under `results/<run_id>/trace.jsonl`; the report aggregates from traces, never from ad-hoc print statements.

## SDK code

- Never rely on training knowledge for API shapes — probe with `scripts/probe_api.py` after any version bump.
- Capability-matrix claims are written by tests (`mcpbench capabilities`), not by hand.
