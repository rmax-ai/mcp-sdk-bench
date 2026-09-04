"""Official MCP Python SDK v2 adapter (mcp 2.1.1).

Drives ``python -m mcp_sdk_bench.servers.official`` as a stdio subprocess via
mcp.client.stdio.stdio_client + ClientSession (probed against the installed
mcp 2.1.1: ClientSession.list_tools/call_tool/read_resource/get_prompt,
CallToolResult fields meta/content/structured_content/is_error).
"""
from __future__ import annotations

import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.shared.exceptions import MCPError

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


class OfficialAdapter(MCPAdapter):
    def __init__(self, env: dict[str, str] | None = None) -> None:
        #: Extra env vars merged over the SDK default subprocess environment
        #: (M2.3b fault injection, SPEC.md §21). StdioServerParameters env
        #: REPLACES the default env, so the merge happens here.
        self._env = env
        self._stdio_cm = None
        self._session_cm = None
        self._session: ClientSession | None = None

    async def connect(self) -> Discovery:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_sdk_bench.servers.official"],
            cwd=str(REPO_ROOT),
            env=({**get_default_environment(), **self._env} if self._env else None),
        )
        self._stdio_cm = stdio_client(params)
        read_stream, write_stream = await self._stdio_cm.__aenter__()
        self._session_cm = ClientSession(read_stream, write_stream)
        self._session = await self._session_cm.__aenter__()
        await self._session.initialize()
        return await self._discover()

    async def _discover(self) -> Discovery:
        assert self._session is not None
        tools = await self._session.list_tools()
        resources = await self._session.list_resources()
        prompts = await self._session.list_prompts()
        return Discovery(
            tools=[
                ToolSpec(
                    name=t.name,
                    description=t.description or "",
                    input_schema=dict(t.input_schema),
                )
                for t in tools.tools
            ],
            resources=[
                ResourceSpec(
                    uri=str(r.uri),
                    name=r.name,
                    mime_type=r.mime_type or "",
                    description=r.description or "",
                )
                for r in resources.resources
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
                for p in prompts.prompts
            ],
        )

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        assert self._session is not None
        try:
            result = await self._session.call_tool(name, arguments)
        except MCPError as err:
            return ToolResult(is_error=True, text=str(err))
        if not isinstance(result, types.CallToolResult):
            return ToolResult(
                is_error=True,
                text=f"unexpected result type {type(result).__name__}",
            )
        return ToolResult(
            is_error=bool(result.is_error),
            structured_content=result.structured_content,
            text=_text_of(result.content),
        )

    async def read_resource(self, uri: str) -> str:
        assert self._session is not None
        try:
            result = await self._session.read_resource(uri)
        except MCPError as err:
            raise RuntimeError(str(err)) from err
        if not isinstance(result, types.ReadResourceResult):
            raise TypeError(f"unexpected result type {type(result).__name__}")
        return "\n".join(
            block.text
            for block in result.contents
            if isinstance(block, types.TextResourceContents)
        )

    async def get_prompt(self, name: str, arguments: dict) -> str:
        assert self._session is not None
        try:
            result = await self._session.get_prompt(name, arguments)
        except MCPError as err:
            raise RuntimeError(str(err)) from err
        if not isinstance(result, types.GetPromptResult):
            raise TypeError(f"unexpected result type {type(result).__name__}")
        return "\n".join(
            message.content.text
            for message in result.messages
            if isinstance(message.content, types.TextContent)
        )

    async def close(self) -> None:
        if self._session_cm is not None:
            await self._session_cm.__aexit__(None, None, None)
            self._session_cm = None
            self._session = None
        if self._stdio_cm is not None:
            await self._stdio_cm.__aexit__(None, None, None)
            self._stdio_cm = None
