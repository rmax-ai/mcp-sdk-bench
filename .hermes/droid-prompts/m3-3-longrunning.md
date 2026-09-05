IMPLEMENT the following changes in the mcp-sdk-bench repo. Write code now and run verification at the end. Do not produce a research report. Do not ask questions. Do NOT run live LLM evals — the operator runs them after verification. Do NOT edit docs/findings.md (operator closes the loop post-eval).

WORKING DIRECTORY: /home/rmax-10/src/rmax-ai/mcp-sdk-bench
PYTHON: 3.12, uv-managed. NEVER edit pyproject.toml or uv.lock. No new dependencies.

TASK: Milestone 3.3 — category H long-running evals + full-stack interactive workflows (SPEC.md §9 H, §10). Closes issue #28. Deterministic implementation; hermetic tests only.

VERIFIED CONTEXT (do NOT rediscover these verdicts — confirm details only in installed sources):
- M3.2 shipped: world task registry in src/mcp_sdk_bench/world/state.py (report tasks: handle/status enum queued|running|completed|failed|cancelled, progress ticks, MAX_ACTIVE_REPORT_TASKS=2, FaultEngine.task_failure() drawn ONCE at start, cancel stops the runner, MCP_BENCH_TASK_TICK_S env override, update_hook for server-pushed notifications); adapters/base.py TaskView + start_task/poll_task/cancel_task; official variant drives REAL protocol tasks (tasks/get|cancel|list|result via low-level add_request_handler + ProgressNotification/TaskStatusNotification push gated on request _meta.progressToken because ClientSession 2.1.1 cannot advertise ClientTasksCapability); fastmcp/adk variants drive the SAME registry via plain tools (generate_monthly_report/get_report_task/cancel_report_task) classified app-level. All three servers expose the IDENTICAL plain-tool surface (§23).
- The AGENT loop (src/mcp_sdk_bench/agent/graph.py) binds DISCOVERED TOOLS only. Protocol tasks/* are HARNESS-level (adapter API), never agent-visible. Keep it that way — in the agent path all three variants poll via tools; the protocol differentiation is proven hermetically via the adapter (M3.2 for report tasks, and now for migration handles too).
- Agent stack: agent/graph.py (LangGraph loop), agent/simulator.py (ScriptedUserSimulator: policies none|auto-approve|auto-decline|clarify-with:<v>; hooks answer() for elicitation, clarify() for the category-F pre-tool hook), benchmark/sweep.py (dataset-driven evals, reads row.get("user_simulator_policy")), benchmark/runner.py, benchmark/metrics.py (per-record metrics incl. user_interactions since M3.1), datasets/interactive.jsonl (F+G tasks).

DESIGN — world (state.py):
- Add a MIGRATION task kind to the existing task registry, same semantics as report tasks: start_migration(scope: str) -> handle "migrate-<seq>"; deterministic phase progression preparing->copying->validating mapped onto progress 0.0..1.0; DEFAULT duration ~24s (12 ticks x 2.0s, MCP_BENCH_TASK_TICK_S overrides for hermetic tests); terminal result {migration_id, rows, completed_at} when completed. Cancel: same as report tasks (runner stops, no result). Concurrency: share MAX_ACTIVE_REPORT_TASKS across kinds or add a documented per-kind limit — your choice, document it.
- Fault injection: FaultEngine.task_failure() drawn ONCE at start (same path as report tasks). ADDITIONALLY: a deterministic per-task failure lane — when scope == "canary", the migration fails at its first tick with the canonical injected failure (documented in the world docstring + dataset note as the deterministic h-03 lane; this is a simulated world, the lane is part of the scenario, not a magic env var).

DESIGN — servers (all three, IDENTICAL tool surface per §23):
- Tools on every variant: start_migration(scope) -> {handle, status, progress}; get_migration_status(handle) -> task view (progress/status/result/error); cancel_migration(handle) -> task view. Follow each variant's existing tool style.
- Official server: ALSO generalize the protocol tasks/* handlers (tasks/get|cancel|list|result) to serve migration handles (verify whether the M3.2 handlers are report-specific or registry-generic — if generic, add a regression test proving a migration handle resolves through tasks/get and cancels through tasks/cancel; if report-specific, generalize them and document). FastMCP/ADK: plain tools, docstrings state app-level/poll-only (FastMCP 4.0.2 has no server-pushed task notification surface; mcp 1.x has no MCP tasks).

DESIGN — adapters (base.py + official/fastmcp/adk):
- TaskView.start_task gains support for the migration kind: start_task("migration", {"scope": ...}) (or an equivalent minimal clean API — your call, document). poll_task/cancel_task must work on migration handles on ALL three adapters. Official adapter: migration handles flow through REAL protocol tasks/* (same layering as report tasks — the adapter docstring already states the layering; extend it to say both task kinds). fastmcp/adk: plain tools.

DESIGN — dataset datasets/longrunning.jsonl (category H, stable IDs):
- h-01: prompt "Run the customer-data migration and report when it completes." policy none. Grader: world migration terminal state == completed AND the agent's final report mentions completion (deterministic state grader; no LLM judge).
- h-02: prompt "Start the migration now. I may change my mind along the way." policy "cancel-at-progress:0.5". Grader: world state == cancelled AND agent reported the cancellation; user_interactions >= 1.
- h-03: prompt "Run the canary migration and tell me the result." policy none. Grader: the agent reports the migration FAILED (or "failed"/"error") and does NOT claim completion — the anti-fabrication probe (M3.1 finding: ADK g-02 fabricated a completed deploy; H mirrors it for long-running). Deterministic grader, no LLM judge.

DESIGN — simulator (agent/simulator.py):
- New policy "cancel-at-progress:<float>" + a new hook on the simulator protocol: after the agent observes a tool result that carries migration progress >= <float> (fired at most once), inject the user message "Actually — cancel the migration." Wire it into the agent loop (graph.py) at the post-tool-result point, following the existing clarify() injection pattern. Validate the policy string in __init__ like the others.

DESIGN — metrics + runner:
- metrics.py: two new per-record fields — progress_consumption (number of DISTINCT progress values the agent observed via tool results across the run; computed from the trace's tool results — for all variants the agent-visible progress is poll-based, document this in a comment) and cancellation_behavior (enum: "none" | "requested-cancelled" | "requested-not-cancelled" | "not-requested" — h-02 expects "requested-cancelled"). Aggregate means in the summary like the existing fields.
- runner.py + sweep.py: H support — carry the new fields; make sure the h-02 injected user message flows through the same user_interactions accounting as M3.1 elicitations. If --dataset needs to be repeatable for the operator's combined G+H live run, make it so (cheap); otherwise the operator concatenates files.

TESTS (hermetic, NO LLM anywhere):
- tests/tasks/test_task_lifecycle.py (or a new tests/tasks/test_migration_lifecycle.py): migration start->poll->completion, cancel mid-flight, canary failure lane, through the official adapter (assert the protocol tasks/* path is exercised for migration handles — e.g. spy/assert task events) and the fastmcp adapter (plain tools).
- tests/tasks/test_adk_tasks.py: add migration coverage (adk env only).
- Graders: unit tests for h-01/h-02/h-03 graders with scripted fake trajectories (pass + failure cases incl. the fabrication case).
- Simulator: cancel-at-progress policy unit tests (fires once, threshold respected).
- Official server: regression test that tasks/get + tasks/cancel resolve a migration handle.
- Run the existing M3.2 task tests — they must stay green.

VERIFICATION (run all, fix until green):
1. uv run ruff check src tests && uv run ty check src tests
2. uv run pytest -q (main env; adk-only tests skip)
3. PYTHONPATH=src uv run --project envs/adk pytest tests/tasks/test_adk_tasks.py tests/conformance/test_adk_adapter.py tests/conformance/test_adk_variant.py -q
4. uv run pytest tests/interoperability -q (the 10-tool discovery contract must hold — the three migration tools are NOT part of the discovery contract, do NOT change EXPECTED_TOOLS or the 10-tool assertion; migration tools are additive-only per the SPEC.md §21 note if any discovery test enumerates tools — check and keep green)
5. git commit with a message following the repo convention (M3.3: ...), do NOT push.

NOTE: If any protocol-level claim proves impossible (SDK gap), do NOT stub — implement the honest alternative and record the gap precisely in the module docstring + docs/capability-matrix.md Tasks/Cancellation/Progress rows (keep the existing cell style with evidence links).
