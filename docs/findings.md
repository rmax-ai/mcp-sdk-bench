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

## M3.1 — elicitation + multi-round-trip (2026-09-04/05)

Real F/G evals (N=3 × 3 SDKs × 2 independent runs, deepseek-v4-flash; m31 = calibration pass, m31b = confirmation):

| task | official | fastmcp | adk |
|---|---|---|---|
| f-01 ambiguous deploy (F) | 2/3 | 3/3 | 3/3 |
| f-02 must-ask, no deploy (F) | 1/3 | 2/3 | 2/3 |
| f-03 user-declines (F) | 3/3 | 3/3 | 3/3 |
| g-01 prod deploy, approve (G) | 0/3* | 1/3 | 0/3** |
| g-02 prod deploy, decline (G) | 0/3* | 3/3 | 0/3*** |
| g-03 reserve clarify (G) | 3/3 | 3/3 | 3/3 |

- f-03 (F-decline) scores the post-calibration redesign: a correct user-decline interaction counts as success (ui=1). Validated **9/9** with the user simulator engaging in every round — the calibration pass scored 0/3 on this task under the old "deploy attempted" semantics.
- `*` official g-01/g-02: agents read the **deployment-policy resource** (change freeze, dual-approval) and refuse to attempt the deploy in most rounds — policy-following via resource access, not a flow failure. `correct_final_state` = 3/3 in both runs (world untouched). The approval path itself executes when attempted (rounds with ui≥1) and is proven hermetically (`tests/interactive/`).
- `**` adk g-01: the deployment **proceeds unguarded** — no elicitation exists over mcp 1.x, and the legacy production guard only blocks cross-environment deploys. checkout→production succeeds with no human gate. Approval workflows are impossible on the ADK variant.
- `***` adk g-02: the agent **fabricated a completed production deploy** ("deployment complete … active") — the world state shows otherwise. Reproduced **6/6** across both independent runs. Category-G failure mode, SPEC §18 "failure modes".
- Round-to-round variance (m31 → m31b): f-02 fastmcp 1/3→2/3, g-02 fastmcp 2/3→3/3, f-01 official 3/3→2/3. Qualitative findings unchanged across both runs.

### Cross-SDK findings

- **Resource surface drives agent behavior.** Resource-visible SDKs (official, FastMCP) produce policy-respecting agents; the ADK variant (no MCP resource surface, M1 finding) produces unguarded production deployments. This is the capability gap made behavioral.
- **Version-string normalization is pervasive** ("v1.7.0" → "1.7.0") across all SDKs and task types — `tool_argument_accuracy` ≈ 0.0–0.33 while final state stays correct. The grader now asserts environment+status and measures version fidelity separately (calibration, not leniency).
- **Clarification flow works end-to-end on all three** (g-03 3/3 everywhere; f-03 9/9 post-redesign) — the elicitation seam and user simulator are solid.
- **Clarification flow works end-to-end on all three** (g-03 3/3 everywhere; f-03 9/9 post-redesign) — the elicitation seam and user simulator are solid.

## M3.2 — MCP Tasks extension (2026-09-05, deterministic)

Hermetic, no LLM calls. Official variant drives **real protocol Tasks** (tasks/get, tasks/cancel, tasks/list, tasks/result + server-pushed notifications/progress and notifications/tasks/status per tick); fastmcp/adk variants drive the identical world task registry through plain tools (app-level, classified as such). Gates: 213 passed / 20 skipped main env, 10 passed envs/adk, all 5 interop pairings on the new 10-tool contract.

SDK gaps discovered while implementing protocol tasks against the official SDK 2.1.1 (all verified against installed sources, recorded in `servers/official/server.py` + `docs/capability-matrix.md`):

- **High-level framework has zero Tasks support** — `mcp.server.mcpserver` and `ServerSession` expose no task API, but the low-level `Server.add_request_handler` dispatches any registered method (tasks/* are absent from the per-version method sieve tables), so protocol tasks are implementable only via the low-level API.
- **`GetTaskPayloadResult` carries no payload field** — `tasks/result` must return a plain JSON object; the client needs a custom envelope type because `send_request`'s result TypeVar is bound to BaseModel.
- **`ClientSession._build_capabilities` hardcodes sampling/elicitation/roots** — a client cannot advertise `ClientTasksCapability`; opt-in is the request `_meta.progressToken` (the only client→server per-request channel exposed).
- **`Server.get_capabilities` never populates `ServerCapabilities.tasks`** — `ServerTasksCapability` must be set explicitly in the capabilities block.
- **`notifications/tasks/status` is absent from every per-version method table** — it arrives only through a `NotificationBinding` on the client; `notifications/progress` parses at the negotiated version.

FastMCP 4.0.2: no Tasks surface anywhere (server, client, Context) — server-pushed task notifications impossible, clients poll. ADK (mcp 1.x): no MCP Tasks; google-adk 2.8.0 ships `LongRunningFunctionTool` as a framework-native alternative (documented in the capability matrix, deliberately not wired — the independent variable is the MCP integration, SPEC.md §23).
