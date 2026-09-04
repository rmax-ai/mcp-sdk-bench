IMPLEMENT the following changes in the mcp-sdk-bench repo. Write code now and run verification at the end. Do not produce a research report. Do not ask questions.

WORKING DIRECTORY: /home/rmax-10/src/rmax-ai/mcp-sdk-bench
PYTHON: 3.12, uv-managed. NEVER edit pyproject.toml or uv.lock. No new dependencies.

TASK: Milestone 2.3 part B — the failure-recovery dataset + the reliability experiment runner (SPEC.md §21). Completes issue #25. This batch is HARNESS CODE ONLY: hermetic tests with a fake model. You must NOT run any live LLM experiment (the operator runs the real reliability numbers afterward); your pytest suite must pass with zero network calls.

CONTEXT: M2.3a shipped FaultConfig/FaultEngine (src/mcp_sdk_bench/faults.py), env-driven (FAIL_TOOL_CALL, LATENCY_MS, DROP_CONNECTION_AFTER, MALFORMED_RESPONSE_RATE, TASK_FAILURE_RATE, FAIL_PHASE, FAULT_SEED), wired into all three server variants, plus create_ticket with idempotency_key (contract now 7 tools) and tests/failures/ deterministic tests. M1 shipped the LangGraph agent (src/mcp_sdk_bench/agent/graph.py, real model via ChatOpenAI + MODEL_* env), the task runner (src/mcp_sdk_bench/benchmark/runner.py, run_task(task, adapter, agent_graph)), the dataset schema (src/mcp_sdk_bench/evals/datasets.py, BenchmarkTask, JSONL, extra=forbid), deterministic graders, and metrics (src/mcp_sdk_bench/benchmark/metrics.py). The M1 fake-model test pattern is tests/regression/test_graph.py (BindableFakeChatModel(GenericFakeChatModel) with a scripted tool-call sequence). CLI: typer app in src/mcp_sdk_bench/cli.py (subcommands eval/benchmark/report/capabilities/interop).

DESIGN: fault-aware grading differs from M1 in one essential way — under fault injection, RETRIES ARE CORRECT BEHAVIOR. Failure tasks grade on OUTCOME (expected_final_state, answer) and SIDE EFFECTS (no duplicates), while recovery/retries/protocol errors are METRICS, not failures. Do not reuse expected_trajectory-based graders for failure tasks (a retry would spuriously fail trajectory checks).

CHANGES:

FILE 1: datasets/failures.jsonl
Five Category-E tasks, same BenchmarkTask schema as basic.jsonl (id, category="E", prompt, expected_tools, expected_args, expected_final_state, answer_contains, forbidden_contains, expected_trajectory, allowed_extra_tools). Stable ids fail-01..fail-05. Tasks (world tools: get_ticket, update_ticket, get_inventory, reserve_inventory, deploy_service, probe_schema, create_ticket):
- fail-01 (idempotency, for FAIL_PHASE=after configs): "Create ticket T-901 titled 'Recovery incident' with idempotency key IDEM-01 and report the ticket id." expected_final_state: exactly one ticket with id T-901 exists (use the create_ticket idempotency_key as the world-dedup mechanism; the grader asserts world ticket count for this key == 1).
- fail-02 (fail-before recovery): "What is the status of PAY-123? If the first attempt fails, retry." expected: get_ticket, final answer contains status.
- fail-03 (update under failure): "Set ticket PAY-124 to status IN_PROGRESS with assignee alina. Confirm the change." expected_final_state: PAY-124 status IN_PROGRESS, assignee alina. allowed_extra_tools: get_ticket (verify after write).
- fail-04 (task failure recovery): "Deploy checkout version v1.7.0 to staging and confirm deployment is active." expected_final_state: checkout deployment ACTIVE version v1.7.0 in staging. allowed_extra_tools: get_ticket (not needed — leave as needed).
- fail-05 (latency tolerance): "Check laptop inventory availability and report the count." expected: get_inventory, answer contains the seeded count. allowed_extra_tools: none.
Set expected_trajectory to null for all five (retry-agnostic); forbidden_contains [] except fail-04 forbids "production".

FILE 2: src/mcp_sdk_bench/benchmark/reliability.py
The reliability experiment: `run_reliability(tasks, sdk: str, fault_config: FaultConfig, n_runs: int) -> list[dict]`. For each task: spawn a fresh server subprocess WITH the fault env applied (same mechanism as the M2.3a faulty session factories — import/reuse from tests where sensible, otherwise a small private helper here; do NOT duplicate the proxy, import StdioProxy if needed), drive the LangGraph agent through the benchmark adapter for that SDK, and grade per task. Per-run record: task_id, sdk, fault_config_label, run_index, task_success, recovery (bool: faults_were_active AND task_success), tool_call_count, retry_count (repeat calls to the same tool with same args), duplicate_side_effects (world count of created tickets for the task's idempotency key minus expected 1 — for fail-01; 0 elsewhere), incorrect_final_state (bool), protocol_errors, answer_ok. Aggregate per (task, sdk, config): success_rate, recovery_probability (recovered runs / runs where faults fired), mean retries, duplicate_side_effect_rate, incorrect_final_state_rate, protocol_error_rate. Fault configs: read from FaultConfig env vars; the label must include the config summary (fail p, phase, latency, drop, malformed). Seeds: FAULT_SEED per run derived deterministically from (task, run_index, sdk) so a whole experiment is reproducible. Docstring cites SPEC §21 + §23.

FILE 3: src/mcp_sdk_bench/benchmark/metrics.py
Add the new counters (retry_count, duplicate_side_effects, recovery) to the metrics dataclass/schema used by results, keeping existing M1 fields untouched. Ensure run_task in runner.py records per-tool-call history so retries/duplicates can be counted (extend AccessRecordingAdapter in runner.py with a call log: tool name + args hash per call; retry = same (name, args-hash) seen more than once). Do not change grading semantics of existing M1 tasks.

FILE 4: src/mcp_sdk_bench/cli.py
Add a `failures` subcommand: runs the reliability experiment over datasets/failures.jsonl for all three SDKs across the defined fault config set — BASELINE (no faults), FAIL_BEFORE (FAIL_TOOL_CALL=0.3, FAIL_PHASE=before), FAIL_AFTER (FAIL_TOOL_CALL=0.3, FAIL_PHASE=after), LATENCY (LATENCY_MS=300). Wire-level configs (drop/malformed) are NOT part of the default experiment (they kill sessions wholesale; documented in the module docstring). Options: --n (default 3), --sdk (repeatable, default all three), --config (repeatable, default the four above). Writes results/latest/failures.json: {run_id, n_runs, model, per-sdk {config -> {per-task aggregates, overall recovery_probability, duplicate_side_effect_rate, incorrect_final_state_rate}}}. Prints a compact per-SDK table. Exit 1 on any experiment-level error.

FILE 5: tests/failures/test_reliability.py
Hermetic tests using the BindableFakeChatModel pattern from tests/regression/test_graph.py (script the exact tool-call sequences including a retry): (a) fail-01 scripted with a failed-after first create_ticket + one retry → task_success true, duplicate_side_effects 0 (world has exactly one T-901); (b) a scripted agent that retries get_ticket 3 times then succeeds → retry_count 2, recovery true; (c) metrics aggregation: given two runs, one recovered, one not → recovery_probability 0.5; (d) dataset loads: datasets/failures.jsonl validates against BenchmarkTask, 5 rows, ids fail-01..fail-05; (e) fault config label includes the active knobs. All hermetic: no model API, no network, no subprocess servers with faults that could flake — use the adapter directly with the fake model and in-process world where the existing M1 graph tests do the same.

FILE 6: tests/conformance/helpers.py
Only if FILE 2 needs a shared spawn-with-fault-env helper that M2.3a did not already provide — reuse what exists; do not restructure.

STYLE: mirror existing conventions. Deterministic seeds; no randomness beyond FaultEngine. Docstrings cite SPEC §21/§23. Honest labels: if a metric cannot be observed for a candidate, mark it null with a reason, never 0.

AFTER writing all files, run these verification commands in order and fix failures until all pass:
1. uv run ruff check src/ tests/
2. uv run ty check
3. uv run pytest -q (full suite green, new tests collected; ZERO live model calls)

Then commit with message "M2.3b: failures dataset + reliability experiment runner + metrics" including ONLY the files you changed, and push with `git push origin main`.
