"""Conformance tests for the FastMCP server variant (SPEC.md §8, M1).

Each test builds a fresh server via create_server() and connects an in-process
FastMCP client, so every session sees a freshly seeded world (mutations never
leak between tests). Mirrors test_official_server.py case-for-case.
"""
from __future__ import annotations

import pytest
from fastmcp import Client
from mcp import types
from mcp.shared.exceptions import MCPError

from mcp_sdk_bench.servers.fastmcp.server import (
    DEPLOYMENT_POLICY_URI,
    INCIDENT_TRIAGE_PROMPT,
    create_server,
)

EXPECTED_TOOLS = {
    "get_ticket",
    "update_ticket",
    "get_inventory",
    "reserve_inventory",
    "deploy_service",
    "probe_schema",
}


def _client() -> Client:
    return Client(create_server())


def _text(result: types.CallToolResult) -> str:
    return "\n".join(
        block.text for block in result.content if isinstance(block, types.TextContent)
    )


async def test_tools_list_has_exactly_the_six_m21_tools() -> None:
    async with _client() as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools} == EXPECTED_TOOLS


async def test_resources_list_contains_deployment_policy() -> None:
    async with _client() as client:
        resources = await client.list_resources()
        uris = {str(resource.uri) for resource in resources}
        assert DEPLOYMENT_POLICY_URI in uris


async def test_prompts_list_contains_incident_triage() -> None:
    async with _client() as client:
        prompts = await client.list_prompts()
        assert INCIDENT_TRIAGE_PROMPT in {prompt.name for prompt in prompts}


async def test_get_ticket_returns_open_pay_123() -> None:
    async with _client() as client:
        result = await client.call_tool("get_ticket", {"ticket_id": "PAY-123"})
        assert not result.is_error
        assert result.structured_content is not None
        ticket = result.structured_content["ticket"]
        assert ticket["id"] == "PAY-123"
        assert ticket["status"] == "OPEN"


async def test_reserve_inventory_decrements_and_world_reflects_it() -> None:
    async with _client() as client:
        reserved = await client.call_tool(
            "reserve_inventory", {"item": "thinkpad-t14", "employee_id": "alice"}
        )
        assert not reserved.is_error
        assert reserved.structured_content is not None
        assert reserved.structured_content["item"]["available"] == 1
        assert "alice" in reserved.structured_content["item"]["reserved_by"]

        inventory = await client.call_tool("get_inventory", {})
        assert not inventory.is_error
        assert inventory.structured_content is not None
        assert inventory.structured_content["items"]["thinkpad-t14"]["available"] == 1


async def test_get_ticket_unknown_id_is_tool_error_not_crash() -> None:
    async with _client() as client:
        result = await client.call_tool(
            "get_ticket", {"ticket_id": "NOPE"}, raise_on_error=False
        )
        assert result.is_error
        assert "not found" in _text(result)


async def test_reserve_inventory_without_availability_is_tool_error() -> None:
    async with _client() as client:
        result = await client.call_tool(
            "reserve_inventory",
            {"item": "macbook-pro", "employee_id": "alice"},
            raise_on_error=False,
        )
        assert result.is_error
        assert "no available units" in _text(result)


async def test_read_deployment_policy_resource() -> None:
    async with _client() as client:
        contents = await client.read_resource(DEPLOYMENT_POLICY_URI)
        assert len(contents) == 1
        content = contents[0]
        assert isinstance(content, types.TextResourceContents)
        assert content.mime_type == "text/markdown"
        assert "Deployment Policy" in content.text


async def test_incident_triage_prompt_renders_instructions() -> None:
    async with _client() as client:
        result = await client.get_prompt(INCIDENT_TRIAGE_PROMPT, {"ticket_id": "PAY-123"})
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
    async with _client() as client:
        with pytest.raises(MCPError, match="not found"):
            await client.read_resource("company://policies/nonexistent")
        # Session still works after the error.
        tools = await client.list_tools()
        assert {tool.name for tool in tools} == EXPECTED_TOOLS
