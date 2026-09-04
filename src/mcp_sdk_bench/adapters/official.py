"""Official MCP Python SDK v2 adapter (mcp 2.1.1).

Drives ``python -m mcp_sdk_bench.servers.official`` as a stdio subprocess via
mcp.client.stdio.stdio_client + ClientSession (probed against the installed
mcp 2.1.1: ClientSession.list_tools/call_tool/read_resource/get_prompt,
CallToolResult fields meta/content/structured_content/is_error).

M3.1 (SPEC.md §18): the ClientSession is created with an elicitation
callback, which advertises ElicitationCapability (form mode) at initialize —
elicitation is a client-advertised capability in MCP. The callback runs
inside the session receive loop while call_tool is in flight; the
ElicitationBridge pauses the adapter call (ToolResult.elicitation_request)
until respond_to_elicitation delivers the user's payload, then the callback
returns the ElicitResult and the paused wire call completes. The resume
surfaces on the next call_tool for the same (name, arguments).
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.shared.exceptions import MCPError

from mcp_sdk_bench.adapters.base import (
    Discovery,
    ElicitationBridge,
    MCPAdapter,
    PromptSpec,
    ResourceSpec,
    ToolResult,
    ToolSpec,
    elicitation_wire_content,
    infer_elicitation_kind,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _text_of(content: list) -> str:
    return "\n".join(
        block.text for block in content if isinstance(block, types.TextContent)
    )


@dataclass
class _PausedCall:
    """An in-flight wire call paused on a server elicitation (M3.1)."""

    task: asyncio.Task[ToolResult]
    name: str
    arguments: dict[str, Any]


class OfficialAdapter(MCPAdapter):
    def __init__(self, env: dict[str, str] | None = None) -> None:
        #: Extra env vars merged over the SDK default subprocess environment
        #: (M2.3b fault injection, SPEC.md §21). StdioServerParameters env
        #: REPLACES the default env, so the merge happens here.
        self._env = env
        self._stdio_cm = None
        self._session_cm = None
        self._session: ClientSession | None = None
        #: M3.1 elicitation plumbing (SPEC.md §18).
        self._elicit_bridge = ElicitationBridge()
        self._paused: _PausedCall | None = None

    async def connect(self) -> Discovery:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_sdk_bench.servers.official"],
            cwd=str(REPO_ROOT),
            env=({**get_default_environment(), **self._env} if self._env else None),
        )
        self._stdio_cm = stdio_client(params)
        read_stream, write_stream = await self._stdio_cm.__aenter__()
        # Registering the elicitation callback advertises
        # ElicitationCapability (form mode) at initialize (mcp 2.1.1:
        # ClientSession advertises it iff a non-default callback is set).
        self._session_cm = ClientSession(
            read_stream, write_stream, elicitation_callback=self._on_elicitation
        )
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

    async def _on_elicitation(
        self,
        context: Any,
        params: types.ElicitRequestParams,
    ) -> types.ElicitResult | types.ErrorData:
        """Client-side elicitation callback (M3.1, SPEC.md §18). Runs inside
        the session receive loop during an in-flight call_tool: normalize the
        request, pause the adapter call, and block until
        respond_to_elicitation delivers the user's payload."""
        if not isinstance(params, types.ElicitRequestFormParams):
            # The world only mints form-mode payloads; URL mode is out of
            # scope for M3.1 — decline honestly rather than fabricate.
            return types.ErrorData(
                code=types.INVALID_REQUEST,
                message="mcp-sdk-bench harness supports form-mode elicitation only",
            )
        schema = dict(params.requested_schema)
        request = {
            "kind": infer_elicitation_kind(schema),
            "question": params.message,
            "schema": schema,
        }
        self._elicit_bridge.post_request(request)
        payload = await self._elicit_bridge.wait_answer()
        action, content = elicitation_wire_content(request, payload)
        return types.ElicitResult(action=action, content=content)

    async def respond_to_elicitation(self, payload: dict) -> None:
        self._elicit_bridge.deliver(payload)

    async def _call(self, name: str, arguments: dict) -> ToolResult:
        """One wire-level tools/call, exceptions mapped to ToolResult."""
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

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        assert self._session is not None
        if self._paused is not None:
            # Resume path: the paused wire call completed once
            # respond_to_elicitation delivered the answer.
            paused = self._paused
            self._paused = None
            if name != paused.name or dict(arguments) != paused.arguments:
                raise RuntimeError(
                    "elicitation resume must re-issue the paused call verbatim "
                    f"(paused on {paused.name}, got {name})"
                )
            return await paused.task
        call_task = asyncio.create_task(self._call(name, arguments))
        elicitation_waiter = asyncio.create_task(self._elicit_bridge.wait_request())
        done, _ = await asyncio.wait(
            {call_task, elicitation_waiter}, return_when=asyncio.FIRST_COMPLETED
        )
        if elicitation_waiter in done and not call_task.done():
            # The server paused the call for user input (SPEC.md §18).
            request = elicitation_waiter.result()
            self._paused = _PausedCall(task=call_task, name=name, arguments=dict(arguments))
            return ToolResult(
                is_error=False,
                text=f"elicitation requested ({request['kind']}): {request['question']}",
                elicitation_request=request,
            )
        elicitation_waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await elicitation_waiter
        return await call_task

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
