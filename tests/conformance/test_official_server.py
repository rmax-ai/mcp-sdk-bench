"""Conformance tests for the official MCP SDK server variant (SPEC.md §8, M1).

Each test spawns a fresh server subprocess over stdio, so every session sees a
freshly seeded world (mutations never leak between tests).
"""
from __future__ import annotations

import sys
from collections.abc import AsyncIterator

import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import MCPError

from mcp_sdk_bench.servers.official.server import (
    DEPLOYMENT_POLICY_URI,
    INCIDENT_TRIAGE_PROMPT,
)

EXPECTED_TOOLS = {
    "get_ticket",
    "update_ticket",
    "create_ticket",
    "get_inventory",
    "reserve_inventory",
    "deploy_service",
    "probe_schema",
}

SERVER_PARAMS = StdioServerParameters(
    command=sys.executable,
    args=["-m", "mcp_sdk_bench.servers.official"],
)


async def _connect() -> AsyncIterator[ClientSession]:
    async with (
        stdio_client(SERVER_PARAMS) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        yield session


def _text(result: types.CallToolResult) -> str:
    return "\n".join(
        block.text for block in result.content if isinstance(block, types.TextContent)
    )


async def test_tools_list_has_exactly_the_seven_m23a_tools() -> None:
    async for session in _connect():
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS


async def test_resources_list_contains_deployment_policy() -> None:
    async for session in _connect():
        resources = await session.list_resources()
        uris = {str(resource.uri) for resource in resources.resources}
        assert DEPLOYMENT_POLICY_URI in uris


async def test_prompts_list_contains_incident_triage() -> None:
    async for session in _connect():
        prompts = await session.list_prompts()
        assert INCIDENT_TRIAGE_PROMPT in {prompt.name for prompt in prompts.prompts}


async def test_get_ticket_returns_open_pay_123() -> None:
    async for session in _connect():
        result = await session.call_tool("get_ticket", {"ticket_id": "PAY-123"})
        assert not result.is_error
        assert result.structured_content is not None
        ticket = result.structured_content["ticket"]
        assert ticket["id"] == "PAY-123"
        assert ticket["status"] == "OPEN"


async def test_reserve_inventory_decrements_and_world_reflects_it() -> None:
    async for session in _connect():
        reserved = await session.call_tool(
            "reserve_inventory", {"item": "thinkpad-t14", "employee_id": "alice"}
        )
        assert not reserved.is_error
        assert reserved.structured_content is not None
        assert reserved.structured_content["item"]["available"] == 1
        assert "alice" in reserved.structured_content["item"]["reserved_by"]

        inventory = await session.call_tool("get_inventory", {})
        assert not inventory.is_error
        assert inventory.structured_content is not None
        assert inventory.structured_content["items"]["thinkpad-t14"]["available"] == 1


async def test_get_ticket_unknown_id_is_tool_error_not_crash() -> None:
    async for session in _connect():
        result = await session.call_tool("get_ticket", {"ticket_id": "NOPE"})
        assert result.is_error
        assert "not found" in _text(result)


async def test_reserve_inventory_without_availability_is_tool_error() -> None:
    async for session in _connect():
        result = await session.call_tool(
            "reserve_inventory", {"item": "macbook-pro", "employee_id": "alice"}
        )
        assert result.is_error
        assert "no available units" in _text(result)


async def test_read_deployment_policy_resource() -> None:
    async for session in _connect():
        result = await session.read_resource(DEPLOYMENT_POLICY_URI)
        assert len(result.contents) == 1
        content = result.contents[0]
        assert isinstance(content, types.TextResourceContents)
        assert content.mime_type == "text/markdown"
        assert "Deployment Policy" in content.text


async def test_incident_triage_prompt_renders_instructions() -> None:
    async for session in _connect():
        result = await session.get_prompt(INCIDENT_TRIAGE_PROMPT, {"ticket_id": "PAY-123"})
        assert result.messages
        text = "\n".join(
            message.content.text
            for message in result.messages
            if isinstance(message.content, types.TextContent)
        )
        assert "PAY-123" in text
        assert "get_ticket" in text
        assert DEPLOYMENT_POLICY_URI in text


async def test_unknown_resource_uri_is_clear_error_not_crash() -> None:
    async for session in _connect():
        with pytest.raises(MCPError, match="unknown resource"):
            await session.read_resource("company://policies/nonexistent")
        # Session still works after the error.
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == EXPECTED_TOOLS
