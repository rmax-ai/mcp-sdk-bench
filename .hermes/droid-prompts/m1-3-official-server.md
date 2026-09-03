IMPLEMENT the M1.3 story now. Do NOT produce a research report. Write code and run the verification commands at the end.

WORKING DIRECTORY: /home/rmax-10/src/rmax-ai/mcp-sdk-bench
Python 3.13 via uv. Official MCP Python SDK 2.1.1 already installed (package name: mcp, protocol 2026-07-28).

CONTEXT: This repo benchmarks FastMCP 4.x vs Google ADK vs the official MCP Python SDK v2. You are implementing the OFFICIAL SDK server variant for Milestone 1: 5 tools, 1 resource, 1 prompt over a shared deterministic world. The world module already exists and is tested — use it as-is, do not modify src/mcp_sdk_bench/world/ or tests/test_world.py. Use the SDK's LOW-LEVEL server API (mcp.server.lowlevel), NOT the SDK's fastmcp-style helper, so the variant honestly represents the official SDK.

WORLD API (already implemented, import from mcp_sdk_bench.world):
- reset_world() -> World  (fresh seeded state: employees alice/bob/carol; tickets PAY-123 OPEN payments, RISK-88 IN_PROGRESS risk, PAY-456 CLOSED; deployments checkout 1.8.2 production, payments-api 2.4.0 staging, risk-engine 3.1.0 production; inventory macbook-pro 0, thinkpad-t14 2, dell-xps-13 1, monitor-27 5; documents dep-policy, incident-runbook, onboarding-guide)
- world.get_ticket(ticket_id) -> Ticket | raises WorldError("ticket X not found")
- world.update_ticket(ticket_id, status: TicketStatus|None, assignee: str|None) -> Ticket
- world.get_inventory() -> dict[str, InventoryItem]
- world.reserve_inventory(item, employee_id) -> InventoryItem | raises WorldError (unknown item, unknown employee, no availability)
- world.deploy_service(service, target_version, environment) -> Deployment | raises WorldError (unknown service, prod mismatch)
- world.get_deployment(service) -> Deployment
- world.search_documents(query) -> list[Document]
- TicketStatus enum: OPEN IN_PROGRESS BLOCKED CLOSED
- Model fields: Ticket(id,title,status,team,assignee,description), InventoryItem(name,available,reserved_by), Deployment(service,version,environment,status), Document(id,title,body,tags), Employee(id,name,team,title)

REQUIREMENTS (SPEC.md §6, M1 scope):
- 5 tools: get_ticket, update_ticket, get_inventory, reserve_inventory, deploy_service. Pydantic input models with clear field descriptions; JSON schemas must be equivalent in spirit across variants (no fabricated fields). WorldError MUST surface as an MCP tool error (isError) with the WorldError message, never as a crash.
- 1 resource: URI company://policies/deployment, mimeType text/markdown, content = the dep-policy document body. Return it via a resource handler; reject unknown URIs with a clear error.
- 1 prompt: incident-triage. Takes ticket_id argument; template text instructs: 1. inspect the ticket (get_ticket), 2. retrieve deployment policy resource, 3. identify owning team, 4. inspect the owning team's deployment state, 5. produce a recommendation. Keep it as instructions to the agent, not fabricated data.
- One World instance per server process, created at server startup from reset_world(). Do not persist to disk.
- Before coding, probe the installed SDK for the correct 2.1.1 API surface: uv run python scripts/probe_api.py and inspect mcp.server.lowlevel, mcp.server.stdio, mcp.client.stdio exports (stdio runner name, handler decorators, read_resource patterns). Do not guess API names from older docs.

FILE 1: src/mcp_sdk_bench/servers/__init__.py
Empty package marker.

FILE 2: src/mcp_sdk_bench/servers/official/__init__.py
Export create_server().

FILE 3: src/mcp_sdk_bench/servers/official/server.py
The server. create_server() -> mcp.server.lowlevel.Server configured with the 5 tools, 1 resource, 1 prompt. Handlers close over a World instance. Tool results return structured dicts; use the SDK's structured output facility if the probed API supports outputSchema/structuredContent cleanly — otherwise plain dict results are acceptable for M1.

FILE 4: src/mcp_sdk_bench/servers/official/__main__.py
Stdio entrypoint: python -m mcp_sdk_bench.servers.official starts the server on stdio using the SDK's stdio runner. Must run cleanly under uv run.

FILE 5: tests/conformance/test_official_server.py
Asyncio tests using the SDK's in-process or stdio_client transport:
1. tools/list contains exactly the 5 expected tool names
2. resources/list contains company://policies/deployment
3. prompts/list contains incident-triage
4. call get_ticket(PAY-123) -> status OPEN
5. call reserve_inventory(thinkpad-t14, alice) -> available becomes 1; then the world reflects it (call get_inventory)
6. call get_ticket(NOPE) -> isError with "not found" message
7. call reserve_inventory(macbook-pro, alice) -> isError (no availability)
8. read resource company://policies/deployment -> content contains "Deployment Policy"
9. get prompt incident-triage with ticket_id=PAY-123 -> rendered text mentions get_ticket and policy retrieval
10. unknown resource URI -> clear error, not a crash

Hygiene: do not copy any real-world values, keys, or host paths into code, tests, or fixtures. No new runtime dependencies — stdio transport only in M1 (Streamable HTTP comes later). Do not edit pyproject.toml unless a probe proves a dependency is missing.

AFTER writing all files, run these verification commands and make them pass:
1. uv run ruff check src tests
2. uv run ty check src tests
3. uv run pytest -q

Then commit with message "M1.3: official SDK server (mcp 2.1.1) — 5 tools, deployment-policy resource, incident-triage prompt, conformance tests" and push origin main.
