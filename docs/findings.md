# Findings

Run-verified observations only (AGENTS.md rule 5). Each finding cites its evidence source: a test module, a results artifact, or a wire-level capture.

## M1 — vertical slice (2026-09-04)

- **ADK's agent-as-server collapses to one conversational tool** (`@experimental`). Evidence: `tests/conformance/test_adk_variant.py`; capability-matrix ADK rows.
- **ADK pins mcp 1.x** while the official SDK and FastMCP 4 track 2.x — three dependency universes forced the process-boundary adapters. Evidence: `src/mcp_sdk_bench/adapters/adk.py`; envs/adk (DECISIONS.md D1).
- **FastMCP 4 is runtime-decoupled from the official SDK** (no shared code path). Evidence: FastMCP server passes the full conformance suite through the FastMCP client with zero official-SDK imports.
- **Official SDK 2.1.1 ships no MCP Apps types.** Evidence: primary source (installed package surface); M4 deferred to its own milestone.

## M2 — conformance, interoperability, failures (2026-09-04)

### Version negotiation (wire-level, all 5 pairings)

Only the **FastMCP 4 client** negotiates protocol **2026-07-28**. The official SDK client and ADK's embedded mcp 1.x client both negotiate the legacy **2025-11-25** handshake — accepted by both 2.x servers, so no pairing breaks, but two of the three clients are running the legacy protocol. Evidence: `results/latest/interoperability.json` (run 20260904T074737Z); `tests/interoperability/test_version_negotiation.py`.

### Reliability (N=3, deepseek-v4-flash, per config)

| SDK | baseline | fail-before | fail-after | latency | recovery range |
|---|---|---|---|---|---|
| official | 0.80 | 0.87 | 0.80 | 0.87 | 0.60–0.87 |
| fastmcp | 0.80 | 0.73 | 0.67 | 0.80 | 0.40–0.80 |
| adk | 0.80 | 0.73 | 0.73 | 0.87 | 0.50–0.87 |

Duplicate side effects: **0.00 across all SDKs and configs.** Evidence: `results/latest/failures.matrix-n3.json` (run 20260904T123832Z). N=3 is directional, not conclusive — see idempotency verdict for the N=10 task.

### Idempotency verdict (N=10, FAIL_AFTER, task fail-01)

All three SDKs: **10/10 task success, 0 duplicate tickets**, with faults actually firing (official 1/10, fastmcp 4/10, adk 3/10 runs) and the agent retrying (mean 0.1–0.3 retries). The world's `create_ticket(idempotency_key=...)` replay semantics make retry-after-failure safe; each candidate surfaces the after-execution failure differently and the agent recovers through all three:

- **official**: fail-after surfaces as an `isError=True` tool result — distinguishable from a transport failure (which raises `MCPError`).
- **fastmcp**: raises `ToolError` by default; `isError` result value under `raise_on_error=False`.
- **adk**: via the benchmark adapter over `McpToolset`'s channel.

Evidence: `results/latest/failures.json` (run 20260904T125437Z); `tests/failures/test_idempotency.py`.

### Conformance classifications (honest labels)

- **ADK wire-level faults are not injectable** — `McpToolset` drives the server over an SDK-managed channel with no wire access; no per-call timeout parameter either. Classified harness limitation, skipped with reason (not "unsupported" — the ADK server handles the same fault config at the tool layer). Evidence: `tests/conformance/test_errors.py`, `tests/failures/test_injected_faults.py` skip reasons.
- **official SDK surfaces a malformed server frame** to `message_handler`; the pending request then fails via read timeout (`MCPError` REQUEST_TIMEOUT) — asserted as such. Evidence: `tests/conformance/test_errors.py`.
- **ADK resources/prompts are honestly empty** in discovery — `McpToolset` has no first-class resource/prompt surface (absence as absence, SPEC §7). Evidence: `tests/conformance/test_discovery.py`.

### Harness lesson (operational)

A model-backend outage mid-experiment (DeepSeek balance hit zero → 402 on every agent call) initially recorded plausible-looking **0.00 success / 0.60 bad_state rows**. The fail-loud guard `_raise_if_model_outage()` now aborts a cell when every run errored with zero tool calls. Evidence: `src/mcp_sdk_bench/benchmark/reliability.py`; `tests/failures/test_reliability.py`.
