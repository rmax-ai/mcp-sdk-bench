"""FastMCP 4.x adapter (fastmcp 4.0.2).

Drives ``python -m mcp_sdk_bench.servers.fastmcp`` as a stdio subprocess via
fastmcp.Client + StdioTransport (probed against the installed fastmcp 4.0.2:
Client.list_tools/list_resources/list_prompts, call_tool(raise_on_error=False)
returning a CallToolResult with is_error/structured_content, read_resource
returning a list of contents, get_prompt returning a GetPromptResult).
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from mcp import types
from mcp.client.stdio import get_default_environment

from mcp_sdk_bench.adapters.base import (
    Discovery,
    MCPAdapter,
    PromptSpec,
    ResourceSpec,
    ToolResult,
    ToolSpec,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _text_of(content: list) -> str:
    return "\n".join(
        block.text for block in content if isinstance(block, types.TextContent)
    )


class FastMCPAdapter(MCPAdapter):
    def __init__(self, env: dict[str, str] | None = None) -> None:
        # `env`: extra env vars merged over the SDK default subprocess
        # environment (M2.3b fault injection, SPEC.md §21).
        transport = StdioTransport(
            command=sys.executable,
            args=["-m", "mcp_sdk_bench.servers.fastmcp"],
            env=({**get_default_environment(), **env} if env else None),
            cwd=str(REPO_ROOT),
        )
        self._client: Client = Client(transport)

    async def connect(self) -> Discovery:
        await self._client.__aenter__()
        tools = await self._client.list_tools()
        resources = await self._client.list_resources()
        prompts = await self._client.list_prompts()
        return Discovery(
            tools=[
                ToolSpec(
                    name=t.name,
                    description=t.description or "",
                    input_schema=dict(t.input_schema),
                )
                for t in tools
            ],
            resources=[
                ResourceSpec(
                    uri=str(r.uri),
                    name=r.name,
                    mime_type=r.mime_type or "",
                    description=r.description or "",
                )
                for r in resources
            ],
            prompts=[
                PromptSpec(
                    name=p.name,
                    description=p.description or "",
                    arguments=[
                        a.model_dump(mode="json", exclude_none=True)
                        for a in (p.arguments or [])
                    ],
                )
                for p in prompts
            ],
        )

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        try:
            result = await self._client.call_tool(name, arguments, raise_on_error=False)
        except Exception as err:  # noqa: BLE001 — transport/protocol failure maps to a tool error, never crashes the agent loop
            return ToolResult(is_error=True, text=str(err))
        return ToolResult(
            is_error=bool(result.is_error),
            structured_content=result.structured_content,
            text=_text_of(result.content),
        )

    async def read_resource(self, uri: str) -> str:
        try:
            contents = await self._client.read_resource(uri)
        except Exception as err:
            raise RuntimeError(str(err)) from err
        return "\n".join(
            block.text
            for block in contents
            if isinstance(block, types.TextResourceContents)
        )

    async def get_prompt(self, name: str, arguments: dict) -> str:
        try:
            result = await self._client.get_prompt(name, arguments)
        except Exception as err:
            raise RuntimeError(str(err)) from err
        return "\n".join(
            message.content.text
            for message in result.messages
            if isinstance(message.content, types.TextContent)
        )

    async def close(self) -> None:
        await self._client.__aexit__(None, None, None)
