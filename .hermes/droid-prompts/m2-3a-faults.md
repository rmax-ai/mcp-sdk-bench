IMPLEMENT the following changes in the mcp-sdk-bench repo. Write code now and run verification at the end. Do not produce a research report. Do not ask questions.

WORKING DIRECTORY: /home/rmax-10/src/rmax-ai/mcp-sdk-bench
PYTHON: 3.12, uv-managed. NEVER edit pyproject.toml or uv.lock. No new dependencies.

TASK: Milestone 2.3 part A — deterministic failure injection infrastructure + the idempotency experiment (SPEC.md §21). Closes the first half of issue #25. This batch is DETERMINISTIC ONLY: no LLM calls, no agent evals (part B does the reliability runs).

CONTEXT: M2.1 shipped tests/conformance/helpers.py with StdioProxy (deterministic corrupt/delay/drop modes, Nth-frame triggers) and session factories per candidate. M2.2 adds tests/interoperability/. The world (src/mcp_sdk_bench/world/state.py) has 6 tools (get_ticket, update_ticket, get_inventory, reserve_inventory, deploy_service, probe_schema), 1 resource, 1 prompt. Fault injection is env-var-driven and seeded (SPEC §21: FAIL_TOOL_CALL, LATENCY_MS, DROP_CONNECTION_AFTER, MALFORMED_RESPONSE_RATE, TASK_FAILURE_RATE). The world currently has NO create_ticket tool — this batch adds it (SPEC §21 idempotency: "An agent retrying a failed MCP call must not accidentally create two tickets"), which extends the contract to 7 tools.

DESIGN (one independent variable — identical fault semantics across all three candidates):
- TOOL-LEVEL faults (FAIL_TOOL_CALL, LATENCY_MS, TASK_FAILURE_RATE) are applied by a shared deterministic fault layer in the tool-dispatch path of all three server variants, configured from env vars read once at server startup. Same code path in all three servers (a shared module), so the SDK difference is the only variable.
- WIRE-LEVEL faults (DROP_CONNECTION_AFTER, MALFORMED_RESPONSE_RATE) are applied by extending the existing StdioProxy in tests/conformance/helpers.py with probabilistic modes driven by the same env vars + a fixed seed.
- Fault RNG: a module-level seeded generator (seed from env var FAULT_SEED, default 42). Same seed + same config = byte-identical fault sequence across SDKs. No wall-clock randomness anywhere.
- FAIL_TOOL_CALL semantics: probability p that a tool call fails. Two sub-modes, chosen per fault configuration: fail-BEFORE-execution (no side effect) and fail-AFTER-execution (side effect applied, error returned). The AFTER mode is the idempotency probe. Expose both via env (FAIL_PHASE=before|after, default before).
- LATENCY_MS: fixed added latency to every tool call (deterministic delay, no jitter).
- TASK_FAILURE_RATE: probability that a task-level operation (the world transaction) raises a WorldError-like failure after execution.

CHANGES:

FILE 1: src/mcp_sdk_bench/world/state.py
Add tool method `create_ticket(ticket_id: str, title: str, priority: str | None = None, idempotency_key: str) -> Ticket` with idempotency semantics: if a ticket with this idempotency_key was already created in this world session, return the EXISTING ticket unchanged (no duplicate, no error); otherwise create the ticket (status OPEN, id recorded) and record the key. Store the key map on the world state. Add an internal helper the fault layer can use: a way to know whether a create_ticket call DID execute (side-effect applied) vs was rejected — used by tests to assert duplicates. Ticket model gains the idempotency_key field. Update fixtures (src/mcp_sdk_bench/world/fixtures.py) only if seed data must stay consistent — do not change seeded ticket IDs.

FILE 2: src/mcp_sdk_bench/faults.py
New shared module `FaultConfig` (pydantic model or dataclass): fail_tool_call: float, latency_ms: int, drop_connection_after: int | None, malformed_response_rate: float, task_failure_rate: float, fail_phase: "before"|"after", seed: int. `load_fault_config()` reads env vars FAIL_TOOL_CALL, LATENCY_MS, DROP_CONNECTION_AFTER, MALFORMED_RESPONSE_RATE, TASK_FAILURE_RATE, FAIL_PHASE, FAULT_SEED (all optional, sensible defaults, validation: probabilities in [0,1], latency >= 0). `FaultEngine` class: seeded RNG, methods `should_fail_call() -> bool`, `task_failure() -> bool`, `latency() -> int`, `next_malformed() -> bool`, `drop_after() -> int | None` — deterministic given the seed. Also `apply_latency()` async helper (asyncio.sleep). Docstring cites SPEC §21 and the seeded-determinism requirement.

FILE 3: src/mcp_sdk_bench/servers/official/server.py
Wire FaultEngine into the tool-dispatch path: before executing a tool, if fail_phase=before and should_fail_call() → return an error result (isError, message "injected fault"); await apply_latency(); execute the tool via the world; if fail_phase=after and should_fail_call() → the world side effect has happened, return an error result claiming failure (isError, message "injected fault after execution"); if task_failure() → raise WorldError("injected task failure"). Register create_ticket with inputSchema matching FILE 1 (idempotency_key required string). Keep the injection in a small shared helper if possible, but do NOT restructure the server beyond this.

FILE 4: src/mcp_sdk_bench/servers/fastmcp/server.py
Same fault semantics on the FastMCP tool functions (before/after/latency/task-failure), registered via @tool. create_ticket registered via @tool with type hints.

FILE 5: src/mcp_sdk_bench/servers/adk/server.py
Same fault semantics in the ADK tool wrappers (FunctionTool), create_ticket added.

FILE 6: tests/conformance/helpers.py
Extend StdioProxy with probabilistic modes: when env MALFORMED_RESPONSE_RATE is set, corrupt response frames at that rate (seeded via FAULT_SEED); when DROP_CONNECTION_AFTER=N, drop after the Nth response frame (existing mode, keep). The proxy must accept the same env config as FaultEngine and produce identical fault sequences for the same seed. Add a session factory variant `OFFICIAL_SESSION_FAULTY` / `FAST_MCP_SESSION_FAULTY` that spawns the server subprocess WITH fault env vars set (via the existing spawn mechanism, passing an env override).

FILE 7: tests/failures/test_fault_engine.py
Unit tests for FaultEngine: determinism (two engines, same seed+config → identical 100-call fault sequences), probability bounds (0.0 → never fails, 1.0 → always fails), fail_phase parsing, env loading (set env vars, load, assert config), invalid values rejected.

FILE 8: tests/failures/test_injected_faults.py
Per candidate (official + fastmcp via faulty session factories; adk via adapter where tool-level faults apply — wire-level drops do not apply to adk, classify as harness limitation): FAIL_TOOL_CALL=1.0 fail_phase=before → every probe_schema call returns error, world state UNCHANGED; FAIL_TOOL_CALL=1.0 fail_phase=after on create_ticket → error returned but ticket EXISTS (side effect applied); LATENCY_MS=200 → a call takes >= 200ms (assert elapsed >= 200ms); TASK_FAILURE_RATE=1.0 → deploy_service raises WorldError("injected task failure"). For official+fastmcp via proxy: MALFORMED_RESPONSE_RATE=1.0 → client surfaces protocol error; DROP_CONNECTION_AFTER=2 → third call fails with connection error. Same-seed determinism assertion: two sessions with same seed hit the same fault pattern on the same call sequence.

FILE 9: tests/failures/test_idempotency.py
The SPEC §21 experiment, deterministic: (a) agent-free — create_ticket(T-1, idempotency_key=K) succeeds; create_ticket(T-1, idempotency_key=K) again returns the SAME ticket, ticket count unchanged (assert world state: exactly one ticket T-1); same idempotency_key with different ticket_id returns the existing ticket without creating a second one. (b) fail-after retry simulation per candidate: FAIL_TOOL_CALL=1.0 fail_phase=after + create_ticket(K) → error; the "retry" (second create_ticket with same K) returns the existing ticket; assert exactly ONE ticket exists (this is the idempotency verdict mechanism). Run (b) per candidate and record which candidates let the client distinguish the after-failure (is_error) from a transport failure — assert the error SHAPE per candidate (error result vs exception) and document in the test docstring which candidate surfaces what (this feeds the per-SDK idempotency verdict in part B).

FILE 10: UPDATE the tool-count contract from 6 to 7 tools in exactly these files and nowhere else:
tests/conformance/test_official_server.py
tests/conformance/test_fastmcp_server.py
tests/conformance/test_adk_variant.py
tests/conformance/test_adk_adapter.py
tests/conformance/test_adapters.py
tests/conformance/helpers.py (DISCOVERY_CONTRACT)
tests/interoperability/test_pairings.py (only its contract references)
Do not change any other assertion semantics.

STYLE: mirror existing test style. All tests deterministic (no sleeps except the latency assertion window; use generous but bounded asserts). Docstrings cite SPEC §21. No LLM calls.

AFTER writing all files, run these verification commands in order and fix failures until all pass:
1. uv run ruff check src/ tests/
2. uv run ty check
3. uv run pytest -q (full suite must stay green; the new failures/ tests must be collected and pass)

Then commit with message "M2.3a: fault injection engine + create_ticket idempotency + failure tests" including ONLY the files you changed, and push with `git push origin main`.
