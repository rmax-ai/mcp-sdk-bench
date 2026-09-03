IMPLEMENT the M1.6 story now. Do NOT produce a research report. Write code and run the verification commands at the end.

WORKING DIRECTORY: /home/rmax-10/src/rmax-ai/mcp-sdk-bench
Main env: Python 3.13, mcp 2.1.1, fastmcp 4.0.2, langgraph 1.2.11, langchain-core 1.6.1. ADK env: envs/adk (google-adk 2.8.0 + mcp 1.29.1).

CONTEXT: This repo benchmarks FastMCP 4.x vs Google ADK vs the official MCP Python SDK v2. M1 = vertical slice. Three servers exist and pass conformance:
- python -m mcp_sdk_bench.servers.official (stdio, official SDK, runs in main env)
- python -m mcp_sdk_bench.servers.fastmcp (stdio, FastMCP, main env)
- python -m mcp_sdk_bench.servers.adk (stdio, ADK-hosted, MUST run under envs/adk with PYTHONPATH=<repo>/src: `PYTHONPATH=src uv run --project envs/adk python -m mcp_sdk_bench.servers.adk`)
All three expose the same contract: 5 tools (get_ticket, update_ticket, get_inventory, reserve_inventory, deploy_service), resource company://policies/deployment, prompt incident-triage. Tool errors arrive as isError with the WorldError message.

You are building the adapter boundary + LangGraph agent + runner skeleton. Read SPEC.md §2 and docs/architecture.md first — the adapter normalizes HOW candidates are driven, never WHAT they express. Capability gaps must be surfaced honestly (empty discovery + a note), never faked or hidden (SPEC §7).

THE COMMON PROTOCOL VIEW (adapters/base.py) — define these exact names:
- pydantic models: ToolSpec(name: str, description: str, input_schema: dict), ResourceSpec(uri: str, name: str, mime_type: str, description: str), PromptSpec(name: str, description: str, arguments: list[dict]), ToolResult(is_error: bool = False, structured_content: dict | None = None, text: str | None = None), Discovery(tools: list[ToolSpec], resources: list[ResourceSpec], prompts: list[PromptSpec])
- class MCPAdapter (abstract base): async connect() -> Discovery; async call_tool(name: str, arguments: dict) -> ToolResult; async read_resource(uri: str) -> str (content text; raise RuntimeError with the server's message on error); async get_prompt(name: str, arguments: dict) -> str; async close() -> None. Use asyncio; no sync wrappers.

FILE 1: src/mcp_sdk_bench/adapters/__init__.py
Export base classes + the three adapters (adk import must be lazy/guarded so the main env can import the package without google.adk present — a module-level try/except or importlib-based lazy factory returning None with a clear reason when google.adk is unavailable).

FILE 2: src/mcp_sdk_bench/adapters/base.py
The common protocol view per the spec above.

FILE 3: src/mcp_sdk_bench/adapters/official.py
OfficialAdapter: official SDK client over stdio subprocess (mcp.client.stdio.stdio_client + ClientSession). Spawn: [sys.executable, "-m", "mcp_sdk_bench.servers.official"] with cwd = repo root (derive from __file__: repo root = Path(__file__).parents[2]). Probe the installed mcp 2.1.1 API (uv run python scripts/probe_api.py; inspect mcp.client.stdio + mcp.shared.session.ClientSession) — do not guess. Map isError results to ToolResult(is_error=True, text=message); map structuredContent to structured_content.

FILE 4: src/mcp_sdk_bench/adapters/fastmcp.py
FastMCPAdapter: fastmcp.Client over stdio against the fastmcp server (probe fastmcp 4.0.2 Client stdio API: transport="stdio", command/args; call_tool raise_on_error=False semantics; list_tools/list_resources/list_prompts/read_resource/get_prompt equivalents — verify the real method names on the installed package). Same mapping rules as official.

FILE 5: src/mcp_sdk_bench/adapters/adk.py
AdkAdapter: wraps google.adk McpToolset (current class name McpToolset, not the deprecated alias) with StdioConnectionParams pointing at the ADK server command. Lazy-import google.adk inside connect() and raise RuntimeError("ADK adapter requires the envs/adk environment") if unavailable. Discovery: tools from McpToolset (map to ToolSpec; probe how to read the underlying tool schemas — McpToolset tools wrap mcp 1.x types). resources and prompts: ADK's McpToolset has no first-class resource/prompt surface (resources become tools via load_mcp_resource_tool) — return empty lists and document the gap in the docstring; this honesty rule is REQUIRED, do not invent fake resource support. call_tool: run the tool via the probed async run API; errors → ToolResult(is_error=True, text=message).

FILE 6: src/mcp_sdk_bench/agent/__init__.py
Empty marker.

FILE 7: src/mcp_sdk_bench/agent/prompts.py
SYSTEM_PROMPT: str constant. A concise operational-assistant instruction: answer by inspecting state via tools, prefer facts from tool results, one tool at a time where sensible, report findings plainly, never invent data. No placeholders, no f-strings.

FILE 8: src/mcp_sdk_bench/agent/graph.py
LangGraph tool-calling loop (langgraph 1.2.11 + langchain-core 1.6.1). State = messages list. build_agent(tools: list[ToolSpec]) -> compiled graph: llm node binds tools converted to langchain tool schema (dict {"name","description","parameters"} from ToolSpec), tools node executes MCPAdapter.call_tool and appends ToolMessage results, conditional edge loops while tool_calls exist, hard cap MAX_TOOL_ITERATIONS = 12. Model construction (add uv dep langchain-openai, use langchain_openai.ChatOpenAI): read env MODEL_PROVIDER, MODEL_NAME, MODEL_API_KEY, MODEL_BASE_URL (optional), BENCH_TEMPERATURE (default "0"); support MODEL_PROVIDER values "deepseek" (base_url https://api.deepseek.com/v1) and "openai_compat" (requires MODEL_BASE_URL). If MODEL_API_KEY unset, raise RuntimeError with a clear message. The model is constructed ONCE per agent build.

FILE 9: src/mcp_sdk_bench/benchmark/__init__.py
Empty marker.

FILE 10: src/mcp_sdk_bench/benchmark/runner.py
run_task(task: dict, adapter: MCPAdapter, agent_graph) -> dict skeleton returning: task_id (from task["id"]), sdk (task["sdk"]), tool_calls (list of {"name","arguments"}), round_trips (count of agent iterations that produced tool calls), total_latency_ms, mcp_latency_ms (sum of call_tool durations), final_answer (last AIMessage content), error (str|None). Invoke the graph with the task's prompt as a HumanMessage; agent must run with recursion safe (graph compiled with no recursion issues — probe the langgraph 1.2 API if needed: create_react_agent exists but implement the explicit loop per the state description above so tool execution routes through OUR adapter, not langchain internals).

FILE 11: tests/conformance/test_adapters.py
For official and fastmcp adapters (main env, asyncio): connect+discover (exactly 5 tools, resource company://policies/deployment present, prompt incident-triage present), call get_ticket PAY-123 -> not is_error and structured_content has status OPEN, call get_ticket NOPE -> is_error with "not found", read_resource returns text containing "Deployment Policy", get_prompt(incident-triage, ticket_id=PAY-123) contains "get_ticket". Each test creates its own adapter and closes it.

FILE 12: tests/regression/test_graph.py
Deterministic loop test with no network: langchain_core.language_models.fake_chat_models.FakeListChatModel — build the agent graph with an injected fake model that first returns a tool call for get_ticket(PAY-123) then a final text answer; verify the tools node routed through a stub adapter and the final answer plus tool_calls are recorded. Also a test that MAX_TOOL_ITERATIONS caps an infinite tool-call loop.

FILE 13: tests/conformance/test_adk_adapter.py
AdkAdapter smoke (adk env only): pytest.importorskip("google.adk.tools.mcp_tool.mcp_toolset", reason="adk env only") — connect+discover against the ADK server (subprocess), assert 5 tools, call get_ticket(PAY-123). Skips in main env; runnable for real with: PYTHONPATH=src uv run --project envs/adk pytest tests/conformance/test_adk_adapter.py

ENV CHANGES: uv add langchain-openai (main env only — required by agent/graph.py). Also add langchain-core, langgraph, langchain-openai to envs/adk/pyproject.toml dependencies and re-lock envs/adk (uv lock --project envs/adk) so the agent code can run under the adk env later. Do not touch main pyproject beyond the langchain-openai add.

CONSTRAINTS: do not modify servers/, world/, existing tests. Hygiene: no real-world values, keys, or host paths in code/tests/fixtures. No new deps beyond those named.

AFTER writing all files, run these verification commands and make them pass:
1. uv run ruff check src tests
2. uv run ty check src tests
3. uv run pytest -q

Then commit with message "M1.6: adapter boundary (official/fastmcp/adk), LangGraph agent, runner skeleton" and push origin main.
