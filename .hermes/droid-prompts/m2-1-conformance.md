IMPLEMENT the following changes in the mcp-sdk-bench repo. Write code now and run verification at the end. Do not produce a research report. Do not ask questions.

WORKING DIRECTORY: /home/rmax-10/src/rmax-ai/mcp-sdk-bench
PYTHON: 3.12, uv-managed. NEVER edit pyproject.toml or uv.lock. No new dependencies.

TASK: Milestone 2.1 — the full deterministic protocol conformance suite (SPEC.md §8): discovery, schema, errors, concurrency, lifecycle, for all three server variants (official mcp 2.1.1, FastMCP 4.0.2, ADK 2.8.0). Closes issue #23.

CONTEXT (already shipped, all gates green): M1 vertical slice. Three server variants implement one identical contract: 5 tools (get_ticket, update_ticket, get_inventory, reserve_inventory, deploy_service), 1 resource (deployment policy), 1 prompt (incident triage). Adapters (src/mcp_sdk_bench/adapters/) normalize the three into a common protocol view; src/mcp_sdk_bench/adapters/base.py defines MCPAdapter. Existing per-server tests live in tests/conformance/test_official_server.py, test_fastmcp_server.py, test_adk_variant.py, test_adk_adapter.py, test_adapters.py. World logic lives in src/mcp_sdk_bench/world/state.py (World class methods are the tool functions; servers register them). 66 tests green baseline.

DRIVING SURFACE per candidate (do not fake symmetry): official server → official SDK ClientSession over stdio (see tests/conformance/test_official_server.py for the exact spawn pattern). FastMCP server → FastMCP Client (fastmcp.client.Client). ADK variant → the benchmark adapter src/mcp_sdk_bench/adapters/adk.py (ADK ships no standalone protocol client; the adapter is the canonical driving surface — state this in test module docstrings).

BEFORE CODING, verify current installed API shapes from the repo's own venv (AGENTS.md rule 1 — never rely on training knowledge): inspect signatures via `uv run python -c "import fastmcp, mcp, inspect; ..."` for the client constructors, timeout parameters, and concurrency behavior you plan to use. Record nothing in results/; this is test code only.

CHANGES:

FILE 1: src/mcp_sdk_bench/world/state.py
Add one new world tool method `probe_schema` — a side-effect-free echo probe whose input exercises every JSON-schema primitive SPEC §8 requires. Signature (type hints ARE the schema source for FastMCP; the official server and ADK FunctionTool need an explicit JSON schema mirroring these exact semantics):
- string_field: str (required)
- int_field: int (required)
- float_field: float (required)
- bool_field: bool (required)
- enum_field: str, constrained to one of ["alpha", "beta", "gamma"] (required; reject other values)
- nullable_field: str | None (required parameter, explicit null allowed)
- union_field: str | int (required; accept both branches)
- list_field: list[str] (required)
- nested_field: dict with keys id: str and tags: list[str] (required)
- nested_list_field: list[dict] with keys name: str and count: int (required)
Return a normalized canonical dict: {"received": {<every field echoed exactly, including nulls>}, "count": <int, total number of fields received>}. The echo must be JSON-serializable and identical across the three server variants.

FILE 2: src/mcp_sdk_bench/servers/official/server.py
Register probe_schema as a Tool with an explicit inputSchema JSON schema matching FILE 1 semantics (required array lists all ten fields; nullable_field uses type ["string","null"]; union_field uses oneOf or type ["string","integer"] — use whatever the official SDK supports correctly). Mirror the existing get_ticket registration pattern exactly. Validate enum/union at the tool boundary or inside the world method (either is fine, but invalid enum MUST produce an MCP error, not a silent default).

FILE 3: src/mcp_sdk_bench/servers/fastmcp/server.py
Register probe_schema via the FastMCP @tool decorator so the schema is generated from the type hints. Enum as a Python enum class named SchemaEnum. Confirm generated schema exposes nullable/union/list-of-object fields correctly; if FastMCP 4 mis-handles any of them, record the deviation in a comment AND report it as a capability observation in the test docstring (do not weaken the schema).

FILE 4: src/mcp_sdk_bench/servers/adk/server.py
Register probe_schema as a FunctionTool following the existing add_tool pattern. Same schema semantics.

FILE 5: tests/conformance/helpers.py
Shared fixtures and utilities:
(a) OFFICIAL_SESSION / FAST_MCP_SESSION / ADK_ADAPTER_SESSION async context-manager factories — each yields a fresh session against a freshly spawned server subprocess (fresh world per test, mirrors tests/conformance/test_official_server.py pattern).
(b) A stdio corrupting proxy class (PROXY_MODES: "corrupt" — replace the Nth response frame's JSON-RPC body with invalid JSON; "delay" — sleep N ms before forwarding every response frame; "drop" — close both pipes after the Nth response frame). The proxy spawns the real server subprocess and transparently forwards stdin/stdout until the trigger fires. Deterministic counters, no randomness. Used only for the official and FastMCP candidates (ADK variant is driven through an SDK-managed channel — no wire access; skip with an explicit harness-limitation reason there).
(c) DISCOVERY_CONTRACT constants: expected tool set (the 6 tools including probe_schema), resource URI list, prompt names, per candidate, with an explicit note that the ADK variant's resources/prompts are EMPTY lists (honest absence, M1 finding).

FILE 6: tests/conformance/test_discovery.py
Per candidate: connect; capabilities negotiation succeeds; list tools returns exactly the contract set with names+descriptions; list resources returns the deployment-policy resource for official+fastmcp, empty for adk; list prompts returns the incident-triage prompt for official+fastmcp, empty for adk. Assert protocolVersion is present in initialize result for official+fastmcp. Every assertion failure must be classified in the message: SDK defect vs harness issue.

FILE 7: tests/conformance/test_schema.py
Per candidate, via probe_schema: round-trip all primitives; nested object round-trip; enum valid values accepted; enum invalid value → error result (is_error or raised MCP error depending on candidate client surface — assert whichever is the canonical error shape for that candidate); explicit null in nullable_field round-trips; union both branches (str then int); empty list; nested_list_field with two objects; result is structured (structuredContent / structured result per candidate surface). If a candidate's client cannot express structured results, classify: partially_supported, and assert on the text fallback instead — record why in the docstring.

FILE 8: tests/conformance/test_errors.py
Per candidate where wire-level injection is possible (official + fastmcp, via helpers proxy; adk adapter-level only where feasible):
- invalid parameter type (int_field="not-an-int") → protocol error, session still usable afterward
- missing required parameter → error mentioning the missing field
- application exception: deploy_service to a production guard that raises WorldError → error result with the WorldError message, world state unchanged
- timeout: proxy delay mode with a client-side timeout configured on the session (use each SDK's real timeout parameter — verify it exists first); assert timeout surfaces as an error/timed-out result, not a hang. If a candidate's client exposes no timeout parameter, skip that case with an explicit harness-limitation reason (honest classification).
- connection loss: proxy drop mode → subsequent call raises/errors; assert the candidate client surfaces the failure (never silently returns a fake success).
- malformed response: proxy corrupt mode → the candidate client must surface a protocol/parse error, not a hang, not a fake result.

FILE 9: tests/conformance/test_concurrency.py
Per candidate: 1, 10, and 100 concurrent probe_schema calls on ONE session (asyncio.gather). All results correct; no exceptions; session usable after the burst. Use probe_schema only — it is side-effect-free; world-mutation concurrency belongs to M2.3. For the ADK variant, run the concurrency numbers the adapter supports honestly; classify anything the adapter serializes internally as an adapter/harness property, not an SDK claim.

FILE 10: tests/conformance/test_lifecycle.py
Per candidate: clean startup (spawn → initialize → discovery succeeds); clean shutdown (graceful close with no errors, server subprocess exits); reconnect (new session over a new subprocess against the same world seed file works); restart (kill the subprocess, spawn again, initialize again — no stale state); cancellation (asyncio task wrapping an in-flight probe_schema call is cancelled mid-flight; the session must remain usable for a subsequent call — for official+fastmcp via delay-mode proxy; for adk, cancel an in-flight adapter call and assert the adapter session remains usable, else classify harness limitation).

FILE 11: UPDATE existing tool-count assertions to the new 6-tool contract, in exactly these files and nowhere else:
tests/conformance/test_official_server.py
tests/conformance/test_fastmcp_server.py
tests/conformance/test_adk_variant.py
tests/conformance/test_adk_adapter.py
tests/conformance/test_adapters.py
tests/regression/test_datasets.py (only if it asserts the tool set; if it merely references tools by name, leave it untouched)
Do not change any other assertion semantics in these files.

STYLE: mirror existing test style (async, pytest-asyncio auto mode, stdlib typing, ruff-clean, ty-clean). Module docstrings cite SPEC §8 categories. No LLM calls, no network beyond spawning local subprocesses. Deterministic only — no random seeds, no sleeps except in the delay proxy.

AFTER writing all files, run these verification commands in order and fix failures until all pass:
1. uv run ruff check src/ tests/
2. uv run ty check
3. uv run pytest -q (must grow from 66 passed to well above 100; the two existing skips may remain)

Then commit with message "M2.1: full conformance suite (discovery/schema/errors/concurrency/lifecycle)" including ONLY the files you changed, and push with `git push origin main`.
