IMPLEMENT the M1.4 story now. Do NOT produce a research report. Write code and run the verification commands at the end.

WORKING DIRECTORY: /home/rmax-10/src/rmax-ai/mcp-sdk-bench
Python 3.13 via uv. FastMCP 4.0.2 already installed (package name: fastmcp; note FastMCP 4 is a metapackage over fastmcp-slim — imports stay `from fastmcp import ...`).

CONTEXT: This repo benchmarks FastMCP 4.x vs Google ADK vs the official MCP Python SDK v2. You are implementing the FastMCP server variant for Milestone 1. A sibling official-SDK server already exists at src/mcp_sdk_bench/servers/official/server.py — the CONTRACT must be identical (same 5 tools, same schemas, same resource, same prompt), only the framework differs. Read that file first and mirror its tool names, parameter names, field descriptions, resource URI/mime, and prompt template text. Do not modify src/mcp_sdk_bench/world/ or the official server or its tests.

WORLD API (already implemented, import from mcp_sdk_bench.world):
- reset_world() -> World  (fresh seeded state: employees alice/bob/carol; tickets PAY-123 OPEN payments, RISK-88 IN_PROGRESS risk, PAY-456 CLOSED; deployments checkout 1.8.2 production, payments-api 2.4.0 staging, risk-engine 3.1.0 production; inventory macbook-pro 0, thinkpad-t14 2, dell-xps-13 1, monitor-27 5; documents dep-policy, incident-runbook, onboarding-guide)
- world.get_ticket(ticket_id) -> Ticket | raises WorldError("ticket X not found")
- world.update_ticket(ticket_id, status: TicketStatus|None, assignee: str|None) -> Ticket
- world.get_inventory() -> dict[str, InventoryItem]
- world.reserve_inventory(item, employee_id) -> InventoryItem | raises WorldError (unknown item, unknown employee, no availability)
- world.deploy_service(service, target_version, environment) -> Deployment | raises WorldError (unknown service, prod mismatch)
- world.get_deployment(service) -> Deployment
- TicketStatus enum: OPEN IN_PROGRESS BLOCKED CLOSED

REQUIREMENTS (SPEC.md §6, M1 scope — identical contract to the official variant):
- 5 tools: get_ticket, update_ticket, get_inventory, reserve_inventory, deploy_service. Pydantic input models, same descriptions as the official variant. WorldError MUST surface as an MCP tool error (isError) with the WorldError message, never as a crash.
- 1 resource: URI company://policies/deployment, mimeType text/markdown, content = dep-policy document body (render identically to the official variant: "# Deployment Policy" title + body).
- 1 prompt: incident-triage, argument ticket_id, rendered text = the same 5-step instruction sequence as the official variant.
- One World instance per server process, created at startup from reset_world(). No disk persistence.
- Use FastMCP idioms (@server.tool, @server.resource, @server.prompt, return-type-driven output schemas). Before coding, probe the installed FastMCP 4 API for the correct current signatures: uv run python scripts/probe_api.py, plus inspect fastmcp.server.FastMCP.run(...) signature (transport arg), fastmcp client in-process usage (fastmcp.Client(<FastMCP instance>)), and how tool exceptions map to isError (mask_error_details behavior). Do not guess API names from older FastMCP 2.x/3.x docs.

FILE 1: src/mcp_sdk_bench/servers/fastmcp/__init__.py
Export create_server() -> the FastMCP instance.

FILE 2: src/mcp_sdk_bench/servers/fastmcp/server.py
The server per the requirements above.

FILE 3: src/mcp_sdk_bench/servers/fastmcp/__main__.py
Stdio entrypoint: python -m mcp_sdk_bench.servers.fastmcp starts the server on stdio. Must run cleanly under uv run.

FILE 4: tests/conformance/test_fastmcp_server.py
Same 10-case shape as tests/conformance/test_official_server.py (read it and mirror):
1. tools/list contains exactly the 5 expected tool names
2. resources/list contains company://policies/deployment
3. prompts/list contains incident-triage
4. call get_ticket(PAY-123) -> status OPEN
5. call reserve_inventory(thinkpad-t14, alice) -> available becomes 1; then get_inventory reflects it
6. call get_ticket(NOPE) -> isError with "not found"
7. call reserve_inventory(macbook-pro, alice) -> isError (no availability)
8. read resource company://policies/deployment -> content contains "Deployment Policy"
9. get prompt incident-triage with ticket_id=PAY-123 -> text mentions get_ticket and policy retrieval
10. unknown resource URI -> clear error, not a crash
Prefer an in-process FastMCP client for tests if the probed API supports it cleanly; otherwise a stdio subprocess per test (fresh process = fresh world, no state leaks between tests).

Hygiene: do not copy any real-world values, keys, or host paths into code, tests, or fixtures. No new runtime dependencies. Do not edit pyproject.toml.

AFTER writing all files, run these verification commands and make them pass:
1. uv run ruff check src tests
2. uv run ty check src tests
3. uv run pytest -q

Then commit with message "M1.4: FastMCP server (4.0.2) — identical contract to official variant, conformance tests" and push origin main.
