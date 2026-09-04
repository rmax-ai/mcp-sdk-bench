"""ADK elicitation honest-gap check (SPEC.md §7/§18, M3.1) — ADK env only.

Skips in the main env (mcp 2.x) like the other ADK conformance modules. Run
it for real with:

    PYTHONPATH=src uv run --project envs/adk pytest tests/interactive/test_adk_elicitation.py

The ADK variant has NO protocol elicitation surface: respond_to_elicitation
must raise NotImplementedError (absence as absence, never a stubbed success),
and a missing-employee reservation must surface the world's honest WorldError
rather than an elicitation pause.
"""
from __future__ import annotations

import pytest

pytest.importorskip(
    "google.adk.tools.mcp_tool.mcp_toolset",
    reason="adk env only (main env pins mcp 2.x; google-adk[mcp] needs mcp 1.x)",
)

from mcp_sdk_bench.adapters.adk import AdkAdapter


async def test_adk_adapter_has_no_protocol_elicitation_surface() -> None:
    adapter = AdkAdapter()
    with pytest.raises(NotImplementedError, match="no protocol elicitation surface"):
        await adapter.respond_to_elicitation({"status": "approved"})


async def test_adk_missing_employee_is_honest_world_error_not_elicitation() -> None:
    adapter = AdkAdapter()
    try:
        await adapter.connect()
        result = await adapter.call_tool("reserve_inventory", {"item": "thinkpad-t14"})
        assert result.is_error
        assert result.elicitation_request is None
        assert result.text is not None
        assert "requires an employee id" in result.text
    finally:
        await adapter.close()
