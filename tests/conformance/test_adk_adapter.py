"""AdkAdapter conformance smoke — ADK env only.

Skips in the main env (mcp 2.x) because google-adk[mcp] pins mcp 1.x. Run it
for real with:

    PYTHONPATH=src uv run --project envs/adk pytest tests/conformance/test_adk_adapter.py

Honest-gap checks: discovery returns empty resources/prompts (SPEC.md §7 —
absence as absence), and read_resource/get_prompt raise RuntimeError naming
the gap.
"""
from __future__ import annotations

import pytest

pytest.importorskip(
    "google.adk.tools.mcp_tool.mcp_toolset",
    reason="adk env only (main env pins mcp 2.x; google-adk[mcp] needs mcp 1.x)",
)

from mcp_sdk_bench.adapters.adk import AdkAdapter

EXPECTED_TOOLS = {
    "get_ticket",
    "update_ticket",
    "get_inventory",
    "reserve_inventory",
    "deploy_service",
    "probe_schema",
}


async def test_adk_adapter_discovers_the_six_m21_tools() -> None:
    adapter = AdkAdapter()
    try:
        discovery = await adapter.connect()
    finally:
        await adapter.close()
    assert {tool.name for tool in discovery.tools} == EXPECTED_TOOLS
    # Honest capability labels: McpToolset has no resource/prompt surface.
    assert discovery.resources == []
    assert discovery.prompts == []


async def test_adk_adapter_call_tool_round_trip() -> None:
    adapter = AdkAdapter()
    try:
        await adapter.connect()
        result = await adapter.call_tool("get_ticket", {"ticket_id": "PAY-123"})
        assert not result.is_error
        assert result.structured_content is not None
        assert result.structured_content["ticket"]["status"] == "OPEN"

        bad = await adapter.call_tool("get_ticket", {"ticket_id": "NOPE"})
        assert bad.is_error
        assert bad.text is not None
        assert "not found" in bad.text
    finally:
        await adapter.close()


async def test_adk_adapter_resource_and_prompt_gaps_are_explicit() -> None:
    adapter = AdkAdapter()
    try:
        await adapter.connect()
        with pytest.raises(RuntimeError, match="resource"):
            await adapter.read_resource("company://policies/deployment")
        with pytest.raises(RuntimeError, match="prompt"):
            await adapter.get_prompt("incident-triage", {"ticket_id": "PAY-123"})
    finally:
        await adapter.close()
