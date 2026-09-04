"""Stdio entrypoint: python -m mcp_sdk_bench.servers.adk [--smoke]

Runs only in the ADK env (envs/adk pins mcp 1.x for google-adk[mcp]):

    PYTHONPATH=src uv run --project envs/adk python -m mcp_sdk_bench.servers.adk
    PYTHONPATH=src uv run --project envs/adk python -m mcp_sdk_bench.servers.adk --smoke

``--smoke`` spawns this same module as a stdio MCP server subprocess, lists
its tools, verifies the seven tool names (M2.1 six + M2.3a create_ticket),
prints them, and exits 0.
"""
from __future__ import annotations

import os
import sys

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_sdk_bench.servers.adk.server import create_server

EXPECTED_TOOLS = (
    "create_ticket",
    "deploy_service",
    "get_inventory",
    "get_ticket",
    "probe_schema",
    "reserve_inventory",
    "update_ticket",
)


async def _serve() -> None:
    server = create_server()
    await server.run_stdio_async()


async def _smoke() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_sdk_bench.servers.adk"],
        # mcp 1.x stdio_client defaults to a minimal env that drops
        # PYTHONPATH; forward ours so the subprocess can import this package.
        env=dict(os.environ),
    )
    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        result = await session.list_tools()
        names = sorted(tool.name for tool in result.tools)
        if tuple(names) != EXPECTED_TOOLS:
            print(f"smoke FAILED: tools {names} != {list(EXPECTED_TOOLS)}", file=sys.stderr)
            raise SystemExit(1)
        print(f"smoke OK: {names}")


def main() -> None:
    if "--smoke" in sys.argv[1:]:
        anyio.run(_smoke)
        return
    anyio.run(_serve)


if __name__ == "__main__":
    main()
