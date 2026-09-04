"""ADK-client interop pairing driver — runs ONLY inside envs/adk (DECISIONS.md D1).

google-adk 2.8.0 pins mcp 1.29.1, so the ADK-client pairings
(adk -> fastmcp, adk -> official) cannot execute in the main env (mcp 2.x).
The parent (mcp_sdk_bench.benchmark.interop) re-executes this module under
envs/adk/.venv with PYTHONPATH=<repo>/src; the driver connects an ADK
McpToolset to the requested MAIN-env server through the log-mode stdio
proxy and writes one PairingResult JSON to --out.

Constructor shapes probed against the installed ADK env (AGENTS.md rule 1):

- ``McpToolset(connection_params=StdioConnectionParams(server_params=
  StdioServerParameters(command, args, env), timeout=30.0))`` — ADK 2.8.0
  embeds the mcp 1.29.1 client; there is NO standalone ADK protocol client
  and McpToolset exposes no initialize result, so protocol versions are read
  from the proxy wire log, not from the SDK surface (harness limitation,
  recorded in DISCOVERY_CONTRACT.adk.exposes_protocol_version).
- ``await toolset.get_tools()`` returns McpTool objects; ``await
  tool.run_async(args=..., tool_context=ToolContext(invocation))`` returns
  the CallToolResult as a camelCase dict (isError / structuredContent), the
  mcp 1.x spelling.
- ``await toolset.close()`` terminates the proxied server subprocess.

Honest absence (M1 finding): McpToolset has no first-class MCP
resource/prompt surface, so resources_seen / prompts_seen stay 0 for ADK
client pairings — the client cannot enumerate them, which says nothing about
the server (see docs/capability-matrix.md Interoperability).

Usage (from interop.py only):
    envs/adk/.venv/bin/python -m mcp_sdk_bench.benchmark.interop_adk_driver \
        --server fastmcp --log-file wire.jsonl --out result.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp_sdk_bench.benchmark.interop import (
    EXPECTED_TOOLS,
    PROBE_ARGS,
    PairingResult,
    check_probe_echo,
    check_ticket,
    classify_connect_error,
    log_proxy_command,
    wire_versions,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The MAIN env interpreter — the pairing servers (fastmcp 4.0.2 / mcp 2.1.1)
#: live there; only the ADK client runs in this (envs/adk) interpreter.
MAIN_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

SERVER_MODULE = {
    "fastmcp": "mcp_sdk_bench.servers.fastmcp",
    "official": "mcp_sdk_bench.servers.official",
}


async def run(server_sdk: str, log_file: Path) -> PairingResult:
    from google.adk.agents.invocation_context import InvocationContext
    from google.adk.sessions import InMemorySessionService
    from google.adk.tools.mcp_tool.mcp_toolset import (
        McpToolset,
        StdioConnectionParams,
    )
    from google.adk.tools.tool_context import ToolContext
    from mcp import StdioServerParameters

    result = PairingResult(f"adk->{server_sdk}", "adk", server_sdk)
    proxy_cmd = log_proxy_command(
        str(MAIN_PYTHON), ["-m", SERVER_MODULE[server_sdk]], log_file
    )
    # mcp 1.x stdio_client defaults to a minimal env; forward ours (mirrors
    # AdkAdapter._server_env).
    toolset = McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=proxy_cmd[0],
                args=proxy_cmd[1:],
                env=dict(os.environ),
            ),
            timeout=30.0,
        )
    )
    try:
        try:
            tools = await toolset.get_tools()
        except Exception as err:  # noqa: BLE001 — connect failure is the evidence
            result.error = f"connect/initialize: {err}"
            result.classification = classify_connect_error(err)
            return result
        result.connected = True
        result.tools_seen = len(tools)
        # ADK client surface: tools only (M1 finding — no resource/prompt
        # surface, so discovery_ok covers the 7-tool contract only).
        result.discovery_ok = {tool.name for tool in tools} == EXPECTED_TOOLS
        if not result.discovery_ok:
            result.error = f"discovery mismatch: tools={sorted(tool.name for tool in tools)}"
            result.classification = "sdk_defect"

        session_service = InMemorySessionService()
        session = await session_service.create_session(
            app_name="mcp_sdk_bench", user_id="bench"
        )
        invocation = InvocationContext(
            session_service=session_service,
            session=session,
            invocation_id="mcp-sdk-bench-interop-adk",
        )
        tools_by_name = {tool.name: tool for tool in tools}

        async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            tool = tools_by_name[name]
            return await tool.run_async(
                args=dict(arguments), tool_context=ToolContext(invocation)
            )

        try:
            ticket = await call("get_ticket", {"ticket_id": "PAY-123"})
            probe = await call("probe_schema", PROBE_ARGS)
            result.roundtrip_ok = (
                not ticket.get("isError", False)
                and not probe.get("isError", False)
                and check_ticket(ticket.get("structuredContent"))
                and check_probe_echo(probe.get("structuredContent"))
            )
            if not result.roundtrip_ok:
                result.error = (
                    f"round-trip mismatch: ticket={ticket.get('structuredContent')!r} "
                    f"probe={probe.get('structuredContent')!r}"
                )
                result.classification = "sdk_defect"
        except Exception as err:  # noqa: BLE001 — failure is the evidence
            result.error = f"round-trip: {err}"
            result.classification = "sdk_defect"
    finally:
        try:
            await toolset.close()
        except Exception as err:  # noqa: BLE001 — teardown failure is evidence too
            if result.error is None:
                result.error = f"teardown: {err}"
                result.classification = "sdk_defect"
        offered, accepted = wire_versions(log_file)
        result.protocol_version_client = offered
        result.protocol_version_server = accepted
    return result


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="mcp-sdk-bench-interop-adk-driver")
    parser.add_argument("--server", choices=sorted(SERVER_MODULE), required=True)
    parser.add_argument("--log-file", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    result = asyncio.run(run(args.server, args.log_file))
    args.out.write_text(json.dumps(result.to_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
