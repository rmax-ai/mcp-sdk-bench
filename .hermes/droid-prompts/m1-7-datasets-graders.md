IMPLEMENT the M1.7 story now. Do NOT produce a research report. Write code and run the verification commands at the end.

WORKING DIRECTORY: /home/rmax-10/src/rmax-ai/mcp-sdk-bench
Python 3.13 via uv. This is the dataset + grader story for the M1 vertical slice.

CONTEXT: M1 vertical slice benchmark. Existing pieces (read them before coding):
- src/mcp_sdk_bench/adapters/base.py — common protocol view: ToolSpec, ResourceSpec, PromptSpec, ToolResult(is_error, structured_content, text), Discovery, MCPAdapter (connect/call_tool/read_resource/get_prompt/close)
- src/mcp_sdk_bench/adapters/{official,fastmcp,adk}.py — working adapters over stdio subprocesses
- src/mcp_sdk_bench/benchmark/runner.py — run_task(task, adapter, agent_graph) -> dict with task_id, sdk, tool_calls (list of {"name","arguments"}), round_trips, total_latency_ms, mcp_latency_ms, final_answer, error
- src/mcp_sdk_bench/servers/* — three servers, same contract: 5 tools (get_ticket, update_ticket, get_inventory, reserve_inventory, deploy_service), resource company://policies/deployment, prompt incident-triage
- Read tests/conformance/test_official_server.py to learn the EXACT structured_content shapes the servers return for get_ticket / update_ticket / get_inventory / reserve_inventory / deploy_service (field names are authoritative for graders).
- World seed: PAY-123 OPEN payments assigned alice; RISK-88 IN_PROGRESS risk assigned bob; PAY-456 CLOSED; checkout 1.8.2 production; thinkpad-t14 available 2; dell-xps-13 available 1; macbook-pro available 0; monitor-27 available 5; dep-policy document: production deploys need two approvals + rollout plan; checkout under change freeze until the payment-timeout incident is closed.

REQUIREMENTS (SPEC.md §9 categories A–D, §10, §11):

FILE 1: datasets/basic.jsonl
FILE 2: datasets/composition.jsonl
JSONL, one task per line, exactly this schema (pydantic-validated; no extra fields):
{"id": str, "category": str, "prompt": str, "expected_tools": [str], "expected_args": {tool: {arg: value}}, "expected_final_state": {check_id: {field: value}}, "answer_contains": [str], "expected_trajectory": [str] | null, "allowed_extra_tools": [str]}
check_id format: "ticket:<id>" resolves via adapter.call_tool(get_ticket, ticket_id) ; "inventory:<name>" resolves via adapter.call_tool(get_inventory) then item lookup.
Author exactly these 10 tasks (ids stable, referenced by tests):

basic.jsonl:
1. basic-001, A: "What is the status of PAY-123?" — expected_tools [get_ticket], expected_args {get_ticket: {ticket_id: PAY-123}}, expected_final_state {}, answer_contains [OPEN], trajectory null, allowed_extra []
2. basic-002, A: "Who is assigned to RISK-88?" — expected_tools [get_ticket], expected_args {get_ticket: {ticket_id: RISK-88}}, answer_contains [bob]
3. basic-003, B: "Do we have any laptops available for a new engineer?" — expected_tools [get_inventory], expected_args {get_inventory: {}}, answer_contains [thinkpad-t14] (and must NOT claim macbook-pro is available; grader checks answer_contains + a forbidden_contains field: add "forbidden_contains": ["macbook-pro"] to the schema for this task)
4. basic-004, B: "Can we give a new hire a machine to work on today?" — expected_tools [get_inventory], answer_contains [yes]
5. basic-005, C: "Reserve a thinkpad-t14 for alice and tell me how many remain." — expected_tools [reserve_inventory], expected_args {reserve_inventory: {item: thinkpad-t14, employee_id: alice}}, expected_final_state {inventory:thinkpad-t14: {available: 1}}, answer_contains [1]
6. basic-006, C: "Close PAY-123." — expected_tools [get_ticket, update_ticket], expected_args {update_ticket: {ticket_id: PAY-123, status: CLOSED}}, expected_final_state {ticket:PAY-123: {status: CLOSED}}, expected_trajectory [get_ticket, update_ticket]
7. basic-007, C: "Mark RISK-88 as BLOCKED." — expected_tools [get_ticket, update_ticket], expected_args {update_ticket: {ticket_id: RISK-88, status: BLOCKED}}, expected_final_state {ticket:RISK-88: {status: BLOCKED}}, expected_trajectory [get_ticket, update_ticket]

composition.jsonl:
8. comp-001, C: "A new engineer is joining the payments team and needs a macbook-pro. Check laptop availability and create an onboarding ticket if none are available." — expected_tools [get_inventory, create_ticket]... NOTE: create_ticket is NOT in the M1 5-tool contract. Use update_ticket or a different M1-tool composition. REWRITE this task to compose only M1 tools: "Check whether thinkpad-t14 laptops are available, and reserve one for alice if any remain." — expected_tools [get_inventory, reserve_inventory], expected_args {reserve_inventory: {item: thinkpad-t14, employee_id: alice}}, expected_final_state {inventory:thinkpad-t14: {available: 1}}, expected_trajectory [get_inventory, reserve_inventory], answer_contains [reserved]
9. comp-002, C: "Bob needs a new machine. Check inventory and reserve a dell-xps-13 for bob." — expected_tools [get_inventory, reserve_inventory], expected_args {reserve_inventory: {item: dell-xps-13, employee_id: bob}}, expected_final_state {inventory:dell-xps-13: {available: 0}}, expected_trajectory [get_inventory, reserve_inventory]
10. comp-003, D: "Check our deployment policy and tell me whether checkout can be deployed right now." — expected_tools [read resource via adapter.read_resource] — represent resource reads in the trace: expected_tools uses a pseudo-tool "read_resource" and expected_args {read_resource: {uri: company://policies/deployment}}; the runner records resource reads as tool_calls entries {"name": "read_resource", "arguments": {"uri": ...}} — EXTEND runner.py to record resource/prompt access as tool_calls entries with name "read_resource"/"get_prompt". answer_contains [freeze]
11. comp-004, D: "What does our deployment policy require for production deployments?" — expected_tools [read_resource], expected_args {read_resource: {uri: company://policies/deployment}}, answer_contains [approvals]... policy says "two independent approvals and a written rollout plan" — answer_contains [two] or [approvals]. Use answer_contains ["approvals", "rollout plan"] (both).

(That is 11 tasks total — 7 basic + 4 composition; the extra one is fine, SPEC requires at least 40 by the end but M1 ships 10+.)

FILE 3: src/evals/__init__.py
Export grade_task.

FILE 4: src/evals/graders.py
grade_task(task: dict, result: dict, adapter: MCPAdapter) -> dict — async. Deterministic grading only, NO LLM judging:
- tool_selection_accuracy: 1.0 if set(used tool names) == expected set (ignoring read_resource/get_prompt pseudo-tools), else 0.0. Counts unexpected calls as unnecessary_tool_calls.
- tool_argument_accuracy: 1.0 if for every tool in expected_args there is a call with matching argument values (exact equality for strings; enum statuses compared as strings), else 0.0.
- trajectory_correctness: if expected_trajectory set: 1.0 if the used-tool sequence (filtered to expected_tools) matches in order, else 0.0; null when no trajectory given.
- correct_final_state: 1.0 iff every expected_final_state check passes via adapter reads (ticket:<id> -> call_tool get_ticket and compare field; inventory:<name> -> call_tool get_inventory and compare item field). Any adapter error -> 0.0.
- answer_quality: 1.0 iff final_answer contains every answer_contains substring (case-insensitive) and, when present, none of forbidden_contains.
- task_success: 1.0 iff correct_final_state AND answer_quality AND tool_selection_accuracy are all 1.0.
- trajectory correctness is recorded independently of task_success (SPEC §11 outcome vs trajectory separation).
Return dict: {task_id, category, task_success, correct_final_state, tool_selection_accuracy, tool_argument_accuracy, trajectory_correctness, answer_quality, unnecessary_tool_calls (int), tool_call_count, round_trips}. Never raise on grading; malformed results grade 0 with an "error" field.

FILE 5: tests/regression/test_graders.py
Pure-deterministic grader tests (no network, no model): build a stub adapter (in-memory world-backed) and synthetic runner results, assert:
- perfect result on basic-001 grades task_success 1.0 with correct tool metrics
- wrong tool (get_inventory instead of get_ticket) -> tool_selection_accuracy 0, task_success 0
- right tool wrong args -> tool_argument_accuracy 0
- final state check catches unmutated state (update_ticket never called) -> correct_final_state 0
- trajectory order violation (update_ticket then get_ticket) -> trajectory_correctness 0 while task_success can remain 1.0 (outcome/trajectory independence)
- answer missing required substring -> answer_quality 0
- forbidden_contains violation on basic-003 -> answer_quality 0
- all 11 dataset rows load and validate against the schema

FILE 6: tests/regression/test_datasets.py
Load datasets/*.jsonl, validate every row against the schema (pydantic model), assert ids unique and stable (spot-check basic-001 and comp-003 exist), assert every expected tool name is in the M1 contract set {get_ticket, update_ticket, get_inventory, reserve_inventory, deploy_service} or is read_resource.

FILE 7: src/mcp_sdk_bench/benchmark/runner.py (EXTEND ONLY)
Add resource/prompt access recording: agent resource reads and prompt gets surface as tool_calls entries {"name": "read_resource", "arguments": {"uri": ...}} and {"name": "get_prompt", "arguments": {...}} appended to the returned tool_calls list. Do not break existing behavior/tests.

CONSTRAINTS: do not modify servers/, adapters/ (except no changes), agent/, world/, existing tests. No new dependencies. Hygiene: no real-world values. This story does NOT wire the CLI or run live evals — that is M1.8.

AFTER writing all files, run these verification commands and make them pass:
1. uv run ruff check src tests
2. uv run ty check src tests
3. uv run pytest -q

Then commit with message "M1.7: 11-task datasets (basic+composition) + deterministic graders + runner resource/prompt recording" and push origin main.
