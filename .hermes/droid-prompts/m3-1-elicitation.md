IMPLEMENT the following changes in the mcp-sdk-bench repo. Write code now and run verification at the end. Do not produce a research report. Do not ask questions.

WORKING DIRECTORY: /home/rmax-10/src/rmax-ai/mcp-sdk-bench
PYTHON: 3.12, uv-managed. NEVER edit pyproject.toml or uv.lock. No new dependencies.

TASK: Milestone 3.1 — elicitation + multi-round-trip (SPEC.md §18). Closes issue #26. Deterministic harness + hermetic tests + a small real N=3 F/G agent run.

VERIFIED SUPPORT MATRIX (probe results, do NOT rediscover — you may confirm details in installed sources but not the verdicts):
- official SDK 2.1.1: FULL protocol elicitation surface (mcp.types: ElicitRequest/ElicitResult/ElicitationCapability/ElicitRequestFormParams/URL forms; client session has elicitation callback plumbing). Server-side request API must be confirmed in the installed package (mcp.server.session / request_context.session — find the exact 2.x server method to SEND an elicitation, reading the installed source in .venv/lib/python*/site-packages/mcp; mcp.server.fastmcp no longer exists in 2.x).
- FastMCP 4.0.2: server elicitation supported (fastmcp.server.context has AcceptedElicitation/DeclinedElicitation/... and the request API); client elicitation module exists. NO task surface.
- ADK variant (envs/adk, mcp 1.x client): NO protocol elicitation possible. The ADK server variant must stay honest: deploy_service production guard keeps raising WorldError; reserve_inventory keeps erroring on missing employee. Record capability rows "unsupported (protocol 1.x client)" with the framework-native equivalent noted (verify what google-adk 2.8 offers natively for HITL/long-running in envs/adk and document it in the capability-matrix comments — do NOT wire it into the benchmark adapter).

CONTEXT: M1/M2 shipped: world (src/mcp_sdk_bench/world/state.py) with 7 tools; adapters (base.py MCPAdapter: connect/call_tool/read_resource/get_prompt/close; ToolResult{is_error, structured_content, text}); three server variants (official/fastmcp/adk) registered with a shared fault layer (src/mcp_sdk_bench/faults.py, run_tool_with_faults); LangGraph agent (src/mcp_sdk_bench/agent/graph.py, real model ChatOpenAI via MODEL_* env, BindableFakeChatModel test pattern); runner (benchmark/runner.py run_task + AccessRecordingAdapter with call_log); datasets (datasets/*.jsonl, BenchmarkTask schema, extra=forbid); CLI (src/mcp_sdk_bench/cli.py: eval/benchmark/report/capabilities/interop/failures; _available_adapters() gates adk on adk_env_ok()). Faults: deploy_service currently RAISES WorldError for production (that guard becomes the approval-elicitation in this story). 161 tests green.

DESIGN (one independent variable — shared world logic, per-SDK protocol surface):
- The WORLD owns the policy (which situations require clarification/approval, and the effect of the response). The SERVER variant owns only the protocol mechanics (how to pause the call and deliver the elicitation). This split is the "application code vs protocol-specific code" measurement SPEC §18 asks for — make the split visible and countable.
- Elicitation seams: world methods gain an optional `elicit: Callable[[dict], Awaitable[dict]]` parameter. When the policy triggers, the world calls elicit({kind, question, schema}) and uses the response {status: approved|declined|clarified, answer: ...}. For reserve_inventory with a missing employee: kind="clarification", schema asking for employee name; response answer feeds the reservation. For deploy_service with environment=production: kind="approval", response approved/declined; declined → WorldError("deployment declined by user").
- The adapters' common view grows: ToolResult gains `elicitation_request: dict | None = None` (the normalized {kind, question, schema}); the adapter base gains an async method `respond_to_elicitation(payload: dict) -> None` (default NotImplementedError) that servers must implement to deliver the user's response back into the paused call. The AGENT LOOP handles a returned elicitation_request by consulting the user simulator (below) and calling respond_to_elicitation, then continuing the loop (this is the multi-round-trip path; count the extra round trips).
- User simulator (harness, deterministic): policy per task — "auto-approve", "auto-decline", "clarify-with:<value>". For category F (ambiguous intent), the AGENT itself should request clarification from the user simulator BEFORE calling the tool (elicitation is agent-initiated there: agent asks the user, not the server). Implement F via a harness-side user-ask hook: the agent loop exposes a user_prompt callback that the simulator answers per policy; the agent must not guess when env/version is missing.

CHANGES:

FILE 1: src/mcp_sdk_bench/world/state.py
Add the elicit seam to `reserve_inventory` (clarification when employee missing — the signature stays reserve_inventory(item, employee: str | None = None, *, elicit=None)) and `deploy_service` (production → elicit approval; declined → WorldError("deployment declined by user")). The elicit callback receives a dict and returns a dict; when no callback is provided, both methods behave exactly as today (reserve_inventory raises on missing employee; deploy_service raises the existing production guard error). Existing tests must keep passing unchanged. Add world-level helpers `clarification_payload(field, question)` and `approval_payload(question)` returning the normalized dicts {kind, question, schema}.

FILE 2: src/mcp_sdk_bench/adapters/base.py
ToolResult gains `elicitation_request: dict | None = None`. MCPAdapter gains `async def respond_to_elicitation(self, payload: dict) -> None` (raise NotImplementedError in base). Docstring: the contract is that a call_tool that returns a ToolResult with elicitation_request is PAUSED until respond_to_elicitation delivers the answer; the next call_tool may then be the same call resumed or a fresh call, per adapter capability — but the common view only exposes the request/response pair.

FILE 3: src/mcp_sdk_bench/servers/official/server.py
Wire elicitation through the REAL 2.x server API you verified in the installed source (request_context.session.send_elicitation or equivalent; confirm exact signature + form params shape from site-packages). reserve_inventory/deploy_service pass an elicit callback that sends the request and awaits the client response with a timeout. The server must declare ElicitationCapability in its capabilities. Keep the fault layer working (validation before fault draw, same order as today).

FILE 4: src/mcp_sdk_bench/servers/fastmcp/server.py
Same flows via FastMCP 4 Context.request_elicitation (verify exact API in installed fastmcp). Timeout on the awaited response.

FILE 5: src/mcp_sdk_bench/servers/adk/server.py
NO elicitation (verified unsupported over mcp 1.x). Do not stub it. Update the module docstring to note the classification: protocol elicitation unsupported; framework-native HITL equivalents exist in google-adk (name what you verified in envs/adk) but are not wired into the benchmark — capability-matrix row must say "unsupported (MCP 1.x client); framework-native: ...".

FILE 6: src/mcp_sdk_bench/adapters/official.py and src/mcp_sdk_bench/adapters/fastmcp.py
Client side of the paused call: the adapter must translate an incoming server elicitation into ToolResult.elicitation_request and implement respond_to_elicitation to deliver the user's answer (official: the client session elicitation callback route you verified; fastmcp: the client elicitation API). The AdkAdapter's respond_to_elicitation keeps raising NotImplementedError (no protocol surface).

FILE 7: src/mcp_sdk_bench/agent/graph.py
Extend the agent loop: after a tool result, if elicitation_request is present, consult the user simulator (a UserSimulator protocol injected into the graph builder: `async def answer(kind, question, schema) -> str|dict`), call adapter.respond_to_elicitation, append a user-side message to the graph state, and continue the loop (count these as user_interactions and extra MCP round trips in the run record). Add the category-F hook: before executing the FIRST tool call of a task, the loop calls the simulator's `clarify(task_prompt) -> str | None`; if it returns clarification text, that text is appended as a user message and the agent continues (F tasks script the simulator to respond with the missing env/version). The simulator is policy-scripted per task id (auto-approve / auto-decline / clarify-with:X / none). Default policy: none (no interaction) — all existing M1/M2 tasks unchanged.

FILE 8: datasets/interactive.jsonl
BenchmarkTask rows (category "F" and "G", ids f-01..f-03, g-01..g-03) using the schema loader contract:
- f-01: "Deploy checkout." — ambiguous: no environment/version given. Expected: agent asks the user simulator (policy clarify-with:"staging v1.7.0") BEFORE calling deploy_service; final state checkout deployed to staging v1.7.0. Grader: world deployment + user_interactions >= 1.
- f-02: same ambiguity but agent must NOT call deploy with guessed env — policy none; grader asserts the agent did not reach a wrong production deploy (expected_final_state: checkout unchanged; answer must ask for clarification; answer_contains: ["environment"]).
- g-01: "Deploy checkout v1.7.0 to production." policy auto-approve → deployed ACTIVE production; user_interactions >= 1.
- g-02: same prompt, policy auto-decline → deploy_service raises "deployment declined by user"; world unchanged; answer mentions the decline.
- g-03: "Reserve a laptop for the new engineer." policy clarify-with:"alina" → reservation recorded for alina; user_interactions >= 1.
Expected fields: expected_tools/expected_args/expected_final_state per world semantics; expected_trajectory null (interaction-agnostic); allowed_extra_tools get_ticket/get_inventory where the verify step needs them. Validate the file loads with the existing schema (extra=forbid).

FILE 9: tests/interactive/test_elicitation.py (new dir)
Hermetic per-SDK tests with the BindableFakeChatModel + in-process world seam (no model API, no network): (a) official + fastmcp: reserve_inventory missing employee → adapter returns elicitation_request; respond_to_elicitation(clarified alina) → reservation lands; deploy production auto-approve → deployed; auto-decline → WorldError decline message + world unchanged. (b) adk: call_tool on the same flows yields the CURRENT error behavior (WorldError), elicitation_request is None, respond_to_elicitation raises NotImplementedError — assert the honest shape. (c) agent-loop tests: scripted model that emits a tool call, then a final answer; simulator auto-approve → user_interactions recorded == 1 and round-trip count in the record reflects the extra pause/resume. (d) F-task tests: agent calls the user-prompt hook before the first tool call (user_interactions >= 1), and with policy none the agent's answer contains the clarification question (no tool call executed).

FILE 10: src/mcp_sdk_bench/benchmark/runner.py + src/mcp_sdk_bench/benchmark/metrics.py
Record user_interactions (count of simulator answers) and MCP_round_trips per run (the pause/resume counts as extra round trips); add both to the run record dict and metrics schema. Add category F/G grading to the deterministic graders: user_interactions expected minimums per task come from the dataset's expected_final_state/answer fields + a new optional per-task key `min_user_interactions` (add it to BenchmarkTask as Optional[int] = None in evals/datasets.py).

FILE 11: docs/capability-matrix.md
Add the Elicitation section: three-way table (protocol spec supports / SDK implements / framework-native equivalent) for official, fastmcp, adk — official: supported (run-verified test refs); fastmcp: supported (server+client, run-verified test refs); adk: unsupported over MCP (mcp 1.x), framework-native HITL equivalent named but not wired (doc link + note). Every cell cites its test module.

AFTER writing all files, run these verification commands in order and fix failures until all pass:
1. uv run ruff check src/ tests/
2. uv run ty check
3. uv run pytest -q (full suite green; new tests/interactive collected; zero live model calls in the suite)

Then run ONE real smoke: `uv run mcpbench eval --sdk official --sdk fastmcp --dataset datasets/interactive.jsonl --n 1` with the .envrc env loaded IF the eval subcommand supports those flags (check its signature first; if it differs, use the closest equivalent invocation and note what you ran). Do NOT commit any results/ output.

Then commit with message "M3.1: elicitation + multi-round-trip (clarification/approval flows, user simulator, F/G datasets)" including ONLY the files you changed, and push with `git push origin main`.
