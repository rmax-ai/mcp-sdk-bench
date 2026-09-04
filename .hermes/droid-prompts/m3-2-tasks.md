IMPLEMENT the following changes in the mcp-sdk-bench repo. Write code now and run verification at the end. Do not produce a research report. Do not ask questions.

WORKING DIRECTORY: /home/rmax-10/src/rmax-ai/mcp-sdk-bench
PYTHON: 3.12, uv-managed. NEVER edit pyproject.toml or uv.lock. No new dependencies.

TASK: Milestone 3.2 — the MCP Tasks extension experiment (SPEC.md §17). Closes issue #27. Deterministic, protocol-level, NO LLM calls anywhere in this story.

VERIFIED SUPPORT MATRIX (probe results — do NOT rediscover the verdicts, confirm details only in installed sources):
- official SDK 2.1.1: mcp.types carries the FULL protocol task types (Task, CreateTaskResult, GetTaskRequest, CancelTaskRequest, ListTasksRequest, TaskStatusNotification, ProgressNotification, ServerTasksCapability, TaskStatus). Client-side task requests exist. SERVER-side: mcp.server.mcpserver (the 2.x framework) has ZERO task support, and mcp.server.session has none — BUT the project's official variant already builds on the LOW-LEVEL API (mcp.server.lowlevel.Server + ServerRequestContext), which exposes register_request_handler(method, handler) and derives capabilities from registered handlers. Therefore: implement REAL protocol Tasks for the official variant by registering low-level handlers for the Tasks methods (confirm the exact method strings and params types from mcp.types / the dispatcher — likely tasks/get, tasks/cancel, tasks/list) and declaring/deriving ServerTasksCapability.
- FastMCP 4.0.2: NO task surface at all (server, client, and context all empty). Classification: protocol-unsupported in SDK; app-level equivalent via plain tools (run-verified). Do not fake protocol tasks.
- ADK variant (envs/adk, mcp 1.x): NO MCP tasks. App-level equivalent via plain tools; framework-native long-running (verify in envs/adk what google-adk 2.8 offers — e.g. LongRunningFunctionTool / long_running_function) is documented in the capability-matrix but NOT wired into the benchmark adapter.

CONTEXT: M1-M3.1 shipped: world (src/mcp_sdk_bench/world/state.py, 7 tools + elicit seams); adapters (base.py MCPAdapter + ToolResult; official/fastmcp/adk variants); three servers (official low-level, fastmcp, adk) with shared fault layer (src/mcp_sdk_bench/faults.py FaultEngine env-driven); CLI (src/mcp_sdk_bench/cli.py: eval/benchmark/report/capabilities/interop/failures + _available_adapters gating adk on adk_env_ok()); deterministic graders; results/latest/* artifacts per run_id. 200 tests green.

DESIGN:
- The WORLD owns a task registry: generate_monthly_report starts a simulated 15-second report task (asyncio, deterministic progress ticks every 2s: 0.0, 0.2, ..., 1.0; result {"report_id", "rows", "generated_at"}), with handle, status enum (queued/running/completed/failed/cancelled), and fault integration (FaultEngine.task_failure() decides an injected mid-task failure — reuse the env-driven config so failure is deterministic per seed). Task registry supports 2 concurrent tasks.
- Per-SDK surface: official = protocol Tasks (tool call returns the task handle via CreateTaskResult semantics; server emits ProgressNotification + TaskStatusNotification to the requesting client when it declares the capability; client polls via real tasks/get, cancels via tasks/cancel, lists via tasks/list). fastmcp = plain tools (generate_monthly_report / get_report_task / cancel_report_task). adk = same plain tools, documented as app-level.
- The adapter common view grows task methods so the runner and tests are SDK-agnostic: TaskView {handle, status, progress, result, error}; start_task(name) -> TaskView; poll_task(handle) -> TaskView; cancel_task(handle) -> TaskView. Official adapter maps these to REAL protocol task requests; fastmcp/adk adapters map them to the plain tools (each adapter docstring states which layer it exercises — the honesty that feeds the capability matrix).

CHANGES:

FILE 1: src/mcp_sdk_bench/world/state.py
Task registry + generate_monthly_report simulation as specified in DESIGN. Cancellation: cancelling a running task marks it cancelled, the asyncio task stops ticking, no result. Failure injection: when FaultEngine.task_failure() fires at task start, the task fails at the first progress tick (status failed, error "injected task failure"). No elicit seams needed here.

FILE 2: src/mcp_sdk_bench/servers/official/server.py
(a) Add tool generate_monthly_report that starts the world task and returns the handle+initial status (the tool result's structured content = task view). (b) Register low-level request handlers for the protocol Tasks methods (tasks/get, tasks/cancel, tasks/list — confirm exact strings) using the Server's register_request_handler API; each maps to the world registry. (c) Emit ProgressNotification and TaskStatusNotification to the requesting client (confirm the exact notification send path on the low-level Server/ServerRequestContext — session.send_progress_notification exists; find the task-status send equivalent in the installed source) whenever a task ticks or transitions, for clients that declared ClientTasksCapability (guard by capability — if the installed SDK does not expose the declared capability at request time, gate on the request's _meta params instead, and document what you did in a comment). (d) Declare ServerTasksCapability in the server's capabilities block (confirm the exact capabilities field shape in the installed Server). If ANY of (b)/(c)/(d) proves genuinely impossible in 2.1.1 (SDK gap, not convenience), do NOT stub it — implement the client-side real requests against a documented minimal handler you CAN register, and record the gap precisely in the module docstring + capability-matrix as "server-side partial: <what is missing>".

FILE 3: src/mcp_sdk_bench/servers/fastmcp/server.py
Plain tools: generate_monthly_report (returns handle+status), get_report_task(handle), cancel_report_task(handle) — FastMCP @tool style. Docstring: FastMCP 4.0.2 has no Tasks surface; these are the app-level equivalent, classified as such.

FILE 4: src/mcp_sdk_bench/servers/adk/server.py
Same plain-tool trio in the ADK tool style. Docstring: mcp 1.x has no Tasks; app-level equivalent; google-adk native long-running (verified name) documented, not wired.

FILE 5: src/mcp_sdk_bench/adapters/base.py
TaskView model + three async methods start_task/poll_task/cancel_task (base raises NotImplementedError). Docstring states the layering contract: official exercises protocol tasks; fastmcp/adk exercise app-level tools — each adapter docstring must say which.

FILE 6: src/mcp_sdk_bench/adapters/official.py, fastmcp.py, adk.py
Implement the three task methods per the DESIGN layering. For official, use the real client-side task requests (verify exact client session API for GetTaskRequest/CancelTaskRequest/ListTasksRequest in installed mcp.client.session; wire task status/progress notifications to the poll result — the client must surface the server-pushed progress, not just polled snapshots, if the SDK supports receiving them; if it does not, document it).

FILE 7: tests/tasks/test_task_lifecycle.py (new dir)
Hermetic, NO LLM: per candidate (official + fastmcp; adk via its adapter): start → poll until completed (assert progress increases monotonically and reaches 1.0, result fields present); cancel mid-run → status cancelled, no result, no further progress; failure injection (fault env) → status failed with "injected task failure"; reconnect: start a task, close the session, open a NEW session (fresh server subprocess sharing the same world seed file — the registry must persist in the world store, not the process; use the world's existing persistence if there is one, otherwise make the task registry live in the same store the world already persists via its seed/reset mechanism — do not introduce a new store format) and poll: status still running then completes; concurrency: two tasks concurrently, both reach completed, independent progress. For official ONLY: wire-level assertions — a real tasks/list shows both tasks; the client receives at least one ProgressNotification during a run (assert via the adapter's poll surfacing server-pushed progress if available, else assert the raw client path you verified).

FILE 8: src/mcp_sdk_bench/cli.py
Add `tasks` subcommand: deterministic runner (no model) that for each SDK runs the lifecycle matrix (start/complete, cancel, failure, concurrent) through the adapters and writes results/latest/tasks.json: per-sdk {layer: protocol|app-level, lifecycle rows with observed statuses+progress, failure row, concurrency row} + a compact table print. Exit 1 on any lifecycle mismatch vs expectation.

FILE 9: docs/capability-matrix.md
Add Tasks section: three-way table — protocol spec supports (2026-07-28 Tasks, SEP-2663) / SDK implements (official: partial-low-level, note what works; fastmcp: none; adk: none) / framework-native equivalent (fastmcp: plain tools run-verified; adk: plain tools run-verified + native long-running named-not-wired). Every cell cites its test module or the tasks.json run. No docs-only claims.

AFTER writing all files, run these verification commands in order and fix failures until all pass:
1. uv run ruff check src/ tests/
2. uv run ty check
3. uv run pytest -q (full suite green, tests/tasks collected)
4. uv run mcpbench tasks (must exit 0 and write results/latest/tasks.json)

Then commit with message "M3.2: Tasks extension experiment (protocol tasks official, app-level fastmcp/adk)" including ONLY the files you changed, and push with `git push origin main`.
