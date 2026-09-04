"""LIFECYCLE conformance (SPEC.md §8 LIFECYCLE, M2.1).

Per candidate: clean startup, clean shutdown, reconnect, server restart,
client cancellation.

Driving surfaces (no faked symmetry):
- official: official SDK ClientSession over stdio.
- fastmcp: fastmcp.client.Client over stdio.
- adk: the benchmark AdkAdapter — ADK 2.8 ships no standalone protocol
  client, so the adapter IS the canonical driving surface. "Subprocess"
  control for the ADK candidate is SDK-internal (McpToolset owns the
  channel); restart is exercised as close() + a fresh adapter session.

Cancellation tests cancel an asyncio task wrapping an in-flight probe_schema
call. For official+fastmcp the delay-mode StdioProxy (1000 ms per response
frame) guarantees the response cannot arrive before the cancel lands — the
cancel is deterministically mid-flight; the only sleep involved is the delay
proxy's own. For ADK there is no wire access (harness limitation), so the
cancel lands wherever the SDK-managed call happens to be; what is asserted
is that the adapter session stays usable afterwards.

Assertion messages are classified: "SDK DEFECT" vs "HARNESS ISSUE".
"""
from __future__ import annotations

import asyncio
import os
from collections.abc import Coroutine
from typing import Any

import pytest
from helpers import (
    ADK_ADAPTER_SESSION,
    DISCOVERY_CONTRACT,
    FAST_MCP_SESSION,
    FASTMCP_SERVER_ARGS,
    OFFICIAL_SERVER_ARGS,
    OFFICIAL_SESSION,
    PROBE_TOOL,
    MCPError,
    ProxyFault,
    eof_shutdown_returncode,
    harness_issue,
    probe_arguments,
    sdk_defect,
)

from mcp_sdk_bench.adapters.adk import REPO_ROOT

SEED_THINKPAD_AVAILABLE = 2
RESERVE_ARGS = {"item": "thinkpad-t14", "employee_id": "alice"}

CANCEL_START_YIELDS = 5


async def _cancel_in_flight(call: Coroutine[Any, Any, Any]) -> None:
    """Run `call` (a coroutine) in a task and cancel it in-flight; the
    CancelledError must propagate to the awaiting caller."""
    task = asyncio.create_task(call)
    # Pure scheduling yields (no wall-clock sleep): let the task start the
    # request. The delay proxy guarantees the response cannot beat the cancel.
    for _ in range(CANCEL_START_YIELDS):
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ---- official ----


async def test_official_clean_startup() -> None:
    candidate = "official"
    async with OFFICIAL_SESSION() as session:
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == DISCOVERY_CONTRACT[candidate].tools, (
            sdk_defect(candidate, "startup discovery != contract")
        )


async def test_official_clean_shutdown() -> None:
    candidate = "official"
    # Graceful close of a live session: no error propagates.
    async with OFFICIAL_SESSION() as session:
        result = await session.call_tool(PROBE_TOOL, probe_arguments())
        assert not result.is_error
    # The server subprocess exits cleanly (rc 0) on stdin EOF.
    rc = await eof_shutdown_returncode(OFFICIAL_SERVER_ARGS)
    assert rc == 0, sdk_defect(
        candidate, f"server did not exit cleanly on stdin EOF (rc={rc})"
    )


async def test_official_reconnect_fresh_world() -> None:
    candidate = "official"
    async with OFFICIAL_SESSION() as session_a:
        reserved = await session_a.call_tool("reserve_inventory", RESERVE_ARGS)
        assert not reserved.is_error, harness_issue(
            candidate, "baseline reserve_inventory failed before reconnect step"
        )
        assert reserved.structured_content is not None
        assert reserved.structured_content["item"]["available"] == SEED_THINKPAD_AVAILABLE - 1
    # New session over a NEW subprocess: same seed, mutation must not leak.
    async with OFFICIAL_SESSION() as session_b:
        inventory = await session_b.call_tool("get_inventory", {})
        assert inventory.structured_content is not None
        assert (
            inventory.structured_content["items"]["thinkpad-t14"]["available"]
            == SEED_THINKPAD_AVAILABLE
        ), sdk_defect(candidate, "stale world state across reconnect")


async def test_official_restart_after_kill() -> None:
    candidate = "official"
    # The drop proxy kills the server child after the initialize response.
    fault = ProxyFault(mode="drop", nth=1)
    async with OFFICIAL_SESSION(fault=fault) as session:
        with pytest.raises(MCPError, match="Connection closed"):
            await session.call_tool(PROBE_TOOL, probe_arguments())
    # Spawn again: initializes cleanly, world is the fresh seed.
    async with OFFICIAL_SESSION() as session:
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == DISCOVERY_CONTRACT[candidate].tools, (
            sdk_defect(candidate, "restarted server discovery != contract")
        )
        inventory = await session.call_tool("get_inventory", {})
        assert inventory.structured_content is not None
        assert (
            inventory.structured_content["items"]["thinkpad-t14"]["available"]
            == SEED_THINKPAD_AVAILABLE
        ), sdk_defect(candidate, "stale world state after restart")


async def test_official_cancellation_keeps_session_usable() -> None:
    candidate = "official"
    fault = ProxyFault(mode="delay", delay_ms=1000)
    async with OFFICIAL_SESSION(fault=fault) as session:
        await _cancel_in_flight(session.call_tool(PROBE_TOOL, probe_arguments()))
        result = await session.call_tool(PROBE_TOOL, probe_arguments())
        assert not result.is_error, sdk_defect(
            candidate, "session unusable after client-side cancellation"
        )
        assert result.structured_content is not None


# ---- fastmcp ----


async def test_fastmcp_clean_startup() -> None:
    candidate = "fastmcp"
    async with FAST_MCP_SESSION() as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools} == DISCOVERY_CONTRACT[candidate].tools, (
            sdk_defect(candidate, "startup discovery != contract")
        )


async def test_fastmcp_clean_shutdown() -> None:
    candidate = "fastmcp"
    async with FAST_MCP_SESSION() as client:
        result = await client.call_tool(PROBE_TOOL, probe_arguments())
        assert not result.is_error
    rc = await eof_shutdown_returncode(FASTMCP_SERVER_ARGS)
    assert rc == 0, sdk_defect(
        candidate, f"server did not exit cleanly on stdin EOF (rc={rc})"
    )


async def test_fastmcp_reconnect_fresh_world() -> None:
    candidate = "fastmcp"
    async with FAST_MCP_SESSION() as client_a:
        reserved = await client_a.call_tool("reserve_inventory", RESERVE_ARGS)
        assert not reserved.is_error, harness_issue(
            candidate, "baseline reserve_inventory failed before reconnect step"
        )
        assert reserved.structured_content is not None
        assert reserved.structured_content["item"]["available"] == SEED_THINKPAD_AVAILABLE - 1
    async with FAST_MCP_SESSION() as client_b:
        inventory = await client_b.call_tool("get_inventory", {})
        assert inventory.structured_content is not None
        assert (
            inventory.structured_content["items"]["thinkpad-t14"]["available"]
            == SEED_THINKPAD_AVAILABLE
        ), sdk_defect(candidate, "stale world state across reconnect")


async def test_fastmcp_restart_after_kill() -> None:
    candidate = "fastmcp"
    fault = ProxyFault(mode="drop", nth=1)
    async with FAST_MCP_SESSION(fault=fault) as client:
        with pytest.raises(MCPError, match="Connection closed"):
            await client.call_tool(PROBE_TOOL, probe_arguments())
    async with FAST_MCP_SESSION() as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools} == DISCOVERY_CONTRACT[candidate].tools, (
            sdk_defect(candidate, "restarted server discovery != contract")
        )
        inventory = await client.call_tool("get_inventory", {})
        assert inventory.structured_content is not None
        assert (
            inventory.structured_content["items"]["thinkpad-t14"]["available"]
            == SEED_THINKPAD_AVAILABLE
        ), sdk_defect(candidate, "stale world state after restart")


async def test_fastmcp_cancellation_keeps_session_usable() -> None:
    candidate = "fastmcp"
    fault = ProxyFault(mode="delay", delay_ms=1000)
    async with FAST_MCP_SESSION(fault=fault) as client:
        await _cancel_in_flight(client.call_tool(PROBE_TOOL, probe_arguments()))
        result = await client.call_tool(PROBE_TOOL, probe_arguments())
        assert not result.is_error, sdk_defect(
            candidate, "session unusable after client-side cancellation"
        )
        assert result.structured_content is not None


# ---- adk (adapter driving surface) ----

ADK_SERVER_ARGS = ["-m", "mcp_sdk_bench.servers.adk"]


def _adk_server_env() -> dict[str, str]:
    env = dict(os.environ)
    src = str(REPO_ROOT / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{src}{os.pathsep}{existing}" if existing else src
    return env


async def test_adk_lifecycle_startup_shutdown_reconnect_restart() -> None:
    """ADK candidate lifecycle through the adapter: connect (startup),
    close (clean shutdown), a second adapter session (reconnect) sees the
    fresh seed, and close()+new adapter again (restart) shows no stale
    state. The server subprocess is SDK-managed (McpToolset owns it).
    """
    candidate = "adk"
    async with ADK_ADAPTER_SESSION() as (adapter, discovery):
        assert {tool.name for tool in discovery.tools} == DISCOVERY_CONTRACT[
            candidate
        ].tools, sdk_defect(candidate, "startup discovery != contract")
        reserved = await adapter.call_tool("reserve_inventory", RESERVE_ARGS)
        assert not reserved.is_error, harness_issue(
            candidate, "baseline reserve_inventory failed before reconnect step"
        )
        assert reserved.structured_content is not None
        assert reserved.structured_content["item"]["available"] == SEED_THINKPAD_AVAILABLE - 1
    # Exiting the factory closed the adapter cleanly (no error propagated).

    # Reconnect: fresh subprocess, fresh world seed.
    async with ADK_ADAPTER_SESSION() as (adapter, _):
        inventory = await adapter.call_tool("get_inventory", {})
        assert inventory.structured_content is not None
        assert (
            inventory.structured_content["items"]["thinkpad-t14"]["available"]
            == SEED_THINKPAD_AVAILABLE
        ), sdk_defect(candidate, "stale world state across reconnect")

    # Restart: another fresh session still works.
    async with ADK_ADAPTER_SESSION() as (adapter, _):
        result = await adapter.call_tool(PROBE_TOOL, probe_arguments())
        assert not result.is_error, sdk_defect(candidate, "restart session unusable")


async def test_adk_clean_shutdown_eof() -> None:
    """The ADK server subprocess exits cleanly on stdin EOF. Runs only in the
    ADK env (the server needs google-adk importable)."""
    pytest.importorskip(
        "google.adk.tools.mcp_tool.mcp_toolset",
        reason="adk env only (main env pins mcp 2.x; google-adk[mcp] needs mcp 1.x)",
    )
    rc = await eof_shutdown_returncode(ADK_SERVER_ARGS, env=_adk_server_env())
    assert rc == 0, sdk_defect(
        "adk", f"server did not exit cleanly on stdin EOF (rc={rc})"
    )


async def test_adk_cancellation_keeps_adapter_usable() -> None:
    """Cancel an in-flight adapter call. No wire access for delay injection
    (harness limitation), so the cancel lands wherever the SDK-managed call
    is; what is asserted is that the adapter stays usable afterwards."""
    candidate = "adk"
    async with ADK_ADAPTER_SESSION() as (adapter, _):
        await _cancel_in_flight(adapter.call_tool(PROBE_TOOL, probe_arguments()))
        result = await adapter.call_tool(PROBE_TOOL, probe_arguments())
        assert not result.is_error, sdk_defect(
            candidate, "adapter unusable after client-side cancellation"
        )
