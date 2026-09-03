IMPLEMENT the M1.5 story now. Do NOT produce a research report. Write code and run the verification commands at the end.

WORKING DIRECTORY: /home/rmax-10/src/rmax-ai/mcp-sdk-bench
Main env: Python 3.13, mcp 2.1.1, fastmcp 4.0.2. ADK variant env: envs/adk (Python 3.13, google-adk 2.8.0 + mcp 1.29.1 — pinned by envs/adk/uv.lock, run with `uv run --project envs/adk ...`).

CONTEXT: This repo benchmarks FastMCP 4.x vs Google ADK vs the official MCP Python SDK v2. You are implementing the ADK variant for Milestone 1. The shared world and two sibling servers already exist:
- src/mcp_sdk_bench/world/ — deterministic world (import from mcp_sdk_bench.world; see API below)
- src/mcp_sdk_bench/servers/official/ — official-SDK server (contract reference)
- src/mcp_sdk_bench/servers/fastmcp/ — FastMCP server
The CONTRACT (5 tools, 1 resource, 1 prompt) must match the official variant: same tool names, parameter names, field descriptions, error messages. Read src/mcp_sdk_bench/servers/official/server.py first.

WORLD API (import from mcp_sdk_bench.world; pydantic models only, no mcp import — safe to import from the adk env via PYTHONPATH):
- reset_world() -> World (seeded state: tickets PAY-123 OPEN payments, RISK-88 IN_PROGRESS risk; deployments checkout 1.8.2 production; inventory thinkpad-t14 2, macbook-pro 0; documents dep-policy)
- world.get_ticket(id), world.update_ticket(id, status, assignee), world.get_inventory(), world.reserve_inventory(item, employee_id), world.deploy_service(service, target_version, environment), world.get_deployment(service)
- WorldError raised on unknown/empty/not-found; TicketStatus enum OPEN IN_PROGRESS BLOCKED CLOSED

REQUIREMENTS — two deliverables, both ADK-native:

PART A — ADK-hosted MCP server (google-adk 2.8.0):
Wrap the world operations as ADK tools and expose them over MCP stdio. Probe the INSTALLED ADK first (it changed in 2.8 — old docs are wrong):
- uv run --project envs/adk python -c "import pkgutil, google.adk; print([m.name for m in pkgutil.walk_packages(google.adk.__path__, 'google.adk.') if 'mcp' in m.name.lower()])"
- Inspect google.adk.tools.mcp_tool (McpTool, mcp_toolset), google.adk.tools._remote_mcp_server, google.adk.tools.load_mcp_resource_tool — determine the REAL way ADK 2.8 hosts an agent (or a toolset) as an MCP server over stdio. If ADK provides a supported agent-as-MCP-server path, use it; if only internal APIs exist (_remote_mcp_server), use the minimal supported public surface and note the deviation in a docstring.
Files: src/mcp_sdk_bench/servers/adk/server.py + __main__.py + __init__.py (create_server equivalent). The server subprocess runs under envs/adk with PYTHONPATH=<repo>/src (document exact launch command in the module docstring; envs/adk already declares pydantic — verify and add it to envs/adk/pyproject.toml if missing, then re-lock with `uv lock --project envs/adk`).

PART B — ADK McpToolset client demo:
A test that proves the ADK client side works: instantiate McpToolset with stdio connection params against the ADK-hosted server (or the official server), list tools, and confirm the 5 tool names are present. Probe the real McpToolset class name (mcp_toolset module has both McpToolset and a deprecated MCPToolset alias — use the current name) and its constructor/config shape (stdio_server_params vs stdio_connection_params). This is a M1 smoke only — deep interop is M2.
Files: tests/conformance/test_adk_variant.py — but note this test needs the adk env: write it so pytest in the MAIN env skips it (pytest.importorskip("google.adk.tools.mcp_tool.mcp_toolset", reason="adk env only")) and document the adk-env command to run it for real: `uv run --project envs/adk pytest tests/conformance/test_adk_variant.py`.

CONSTRAINTS:
- Do not modify src/mcp_sdk_bench/world/, the official/fastmcp servers, or their tests.
- Do not edit the MAIN pyproject.toml. You MAY edit envs/adk/pyproject.toml (add pydantic if missing) and re-lock envs/adk.
- ADK-hosted server must map WorldError to an MCP tool error with the WorldError message, never a crash.
- Hygiene: no real-world values, keys, or host paths in code/tests/fixtures. No new main-env dependencies.

AFTER writing all files, run these verification commands and make them pass:
1. uv run ruff check src tests
2. uv run ty check src tests
3. uv run pytest -q   (main env; the adk test skips)
4. uv run --project envs/adk python -m mcp_sdk_bench.servers.adk --smoke   (if you implement a --smoke flag that starts the server, lists tools, exits 0 — implement it as a fast self-check)

Then commit with message "M1.5: ADK variant (2.8.0) — agent-hosted MCP server + McpToolset client demo" and push origin main.
