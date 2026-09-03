"""Adapter conformance tests (SPEC.md §8 DISCOVERY, M1.6).

Drives the official and fastmcp adapters against their stdio server
subprocesses through the common protocol view. The ADK adapter is covered
separately in test_adk_adapter.py (adk env only). Each test creates its own
adapter and closes it, so world mutations never leak between tests.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from mcp_sdk_bench.adapters import FastMCPAdapter, MCPAdapter, OfficialAdapter
from mcp_sdk_bench.adapters.base import Discovery

EXPECTED_TOOLS = {
    "get_ticket",
    "update_ticket",
    "get_inventory",
    "reserve_inventory",
    "deploy_service",
}
DEPLOYMENT_POLICY_URI = "company://policies/deployment"
INCIDENT_TRIAGE_PROMPT = "incident-triage"

ADAPTER_CLASSES = [OfficialAdapter, FastMCPAdapter]


@asynccontextmanager
async def _connected(cls: type[MCPAdapter]) -> AsyncIterator[tuple[MCPAdapter, Discovery]]:
    adapter = cls()
    discovery = await adapter.connect()
    try:
        yield adapter, discovery
    finally:
        await adapter.close()


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_discovery_five_tools_one_resource_one_prompt(cls) -> None:
    async with _connected(cls) as (_, discovery):
        assert {tool.name for tool in discovery.tools} == EXPECTED_TOOLS
        assert DEPLOYMENT_POLICY_URI in {r.uri for r in discovery.resources}
        assert INCIDENT_TRIAGE_PROMPT in {p.name for p in discovery.prompts}


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_get_ticket_returns_open_pay_123(cls) -> None:
    async with _connected(cls) as (adapter, _):
        result = await adapter.call_tool("get_ticket", {"ticket_id": "PAY-123"})
        assert not result.is_error
        assert result.structured_content is not None
        assert result.structured_content["ticket"]["status"] == "OPEN"


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_get_ticket_unknown_id_is_tool_error(cls) -> None:
    async with _connected(cls) as (adapter, _):
        result = await adapter.call_tool("get_ticket", {"ticket_id": "NOPE"})
        assert result.is_error
        assert result.text is not None
        assert "not found" in result.text


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_read_deployment_policy_resource(cls) -> None:
    async with _connected(cls) as (adapter, _):
        text = await adapter.read_resource(DEPLOYMENT_POLICY_URI)
        assert "Deployment Policy" in text


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_incident_triage_prompt_renders_instructions(cls) -> None:
    async with _connected(cls) as (adapter, _):
        text = await adapter.get_prompt(
            INCIDENT_TRIAGE_PROMPT, {"ticket_id": "PAY-123"}
        )
        assert "PAY-123" in text
        assert "get_ticket" in text
