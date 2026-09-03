"""ADK variant M1 smoke — ADK client side (McpToolset) against the ADK server.

Runs only in the ADK env: the main env pins mcp 2.x, which is incompatible
with google-adk[mcp]'s mcp 1.x client stack (``mcp.shared.session`` no longer
exists), so the importorskip below skips the whole module there. Run it for
real with:

    uv run --project envs/adk pytest tests/conformance/test_adk_variant.py

This is the M1 client smoke only — deep cross-implementation interop is M2.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

mcp_toolset = pytest.importorskip(
    "google.adk.tools.mcp_tool.mcp_toolset",
    reason="adk env only (main env pins mcp 2.x; google-adk[mcp] needs mcp 1.x)",
)
from mcp import StdioServerParameters

EXPECTED_TOOLS = {
    "get_ticket",
    "update_ticket",
    "get_inventory",
    "reserve_inventory",
    "deploy_service",
}

REPO_ROOT = Path(__file__).resolve().parents[2]


def _server_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return env


async def test_adk_mcptoolset_lists_the_five_m1_tools() -> None:
    """McpToolset (ADK client) over stdio against the ADK-hosted server."""
    toolset = mcp_toolset.McpToolset(
        connection_params=mcp_toolset.StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=["-m", "mcp_sdk_bench.servers.adk"],
                env=_server_env(),
            ),
            timeout=30.0,
        )
    )
    try:
        tools = await toolset.get_tools()
    finally:
        await toolset.close()
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
