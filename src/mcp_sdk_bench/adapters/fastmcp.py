"""FastMCP 4.x adapter (fastmcp 4.0.2).

Drives ``python -m mcp_sdk_bench.servers.fastmcp`` as a stdio subprocess via
fastmcp.Client + StdioTransport (probed against the installed fastmcp 4.0.2:
Client.list_tools/list_resources/list_prompts, call_tool(raise_on_error=False)
returning a CallToolResult with is_error/structured_content, read_resource
returning a list of contents, get_prompt returning a GetPromptResult).

M3.1 (SPEC.md §18): the client is created with an elicitation handler. The
FastMCP client negotiates the modern 2026-07-28 protocol with the benchmark
server, so elicitation arrives as a SEP-2322 InputRequiredResult which the
client's input-required driver resolves through the SAME callback table —
our handler fires mid-call, the ElicitationBridge pauses the adapter call
(ToolResult.elicitation_request) until respond_to_elicitation delivers the
user's payload, then the driver retries the original call with
input_responses and the paused call completes. The resume surfaces on the
next call_tool for the same (name, arguments); the extra wire leg is the
adapter's protocol mechanics, invisible to the agent loop beyond the
recorded round trip.

M3.2 (SPEC.md §17): this adapter exercises the APP-LEVEL task surface —
FastMCP 4.0.2 has no protocol Tasks API (verified: no task methods on the
server, Client, or Context), so start/poll/cancel map to the plain tools
generate_monthly_report / get_report_task / cancel_report_task. Classified
as app-level (never protocol tasks) in docs/capability-matrix.md.
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult as FastMCPElicitResult
from fastmcp.client.transports import StdioTransport
from mcp import types
from mcp.client.stdio import get_default_environment

from mcp_sdk_bench.adapters.base import (
    Discovery,
    ElicitationBridge,
    MCPAdapter,
    PromptSpec,
    ResourceSpec,
    TaskView,
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
    """An in-flight call paused on a server elicitation (M3.1)."""

    task: asyncio.Task[ToolResult]
    name: str
    arguments: dict[str, Any]


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
        self._elicit_bridge = ElicitationBridge()
        self._paused: _PausedCall | None = None
        self._client: Client = Client(transport, elicitation_handler=self._on_elicitation)

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

    async def _on_elicitation(
        self,
        message: str,
        _response_type: type | None,
        params: types.ElicitRequestParams,
        _context: Any,
    ) -> dict[str, Any] | FastMCPElicitResult:
        """Client-side elicitation handler (M3.1, SPEC.md §18). Fires inside
        the client's input-required driver during an in-flight call_tool:
        normalize the request, pause the adapter call, and block until
        respond_to_elicitation delivers the user's payload. A returned dict
        means "accept" with that content (fastmcp convention)."""
        if not isinstance(params, types.ElicitRequestFormParams):
            # URL mode is out of scope for M3.1 — decline honestly.
            return FastMCPElicitResult(action="decline")
        schema = dict(params.requested_schema)
        request = {
            "kind": infer_elicitation_kind(schema),
            "question": message,
            "schema": schema,
        }
        self._elicit_bridge.post_request(request)
        payload = await self._elicit_bridge.wait_answer()
        action, content = elicitation_wire_content(request, payload)
        if action == "accept":
            return content or {}
        return FastMCPElicitResult(action="decline")

    async def respond_to_elicitation(self, payload: dict) -> None:
        self._elicit_bridge.deliver(payload)

    async def _call(self, name: str, arguments: dict) -> ToolResult:
        """One logical tools/call (the client drives any input-required
        retries internally), exceptions mapped to ToolResult."""
        try:
            result = await self._client.call_tool(name, arguments, raise_on_error=False)
        except Exception as err:  # noqa: BLE001 — transport/protocol failure maps to a tool error, never crashes the agent loop
            return ToolResult(is_error=True, text=str(err))
        return ToolResult(
            is_error=bool(result.is_error),
            structured_content=result.structured_content,
            text=_text_of(result.content),
        )

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        if self._paused is not None:
            # Resume path: the driver's retry completed once
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
            # The server asked for user input (SPEC.md §18).
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

    # ---- M3.2 app-level task tools (SPEC.md §17) — NOT protocol tasks ----

    async def _task_call(self, name: str, arguments: dict) -> TaskView:
        result = await self.call_tool(name, arguments)
        if result.is_error or not result.structured_content:
            raise RuntimeError(result.text or f"{name} failed")
        return TaskView(**result.structured_content["task"])

    async def start_task(self, name: str) -> TaskView:
        """Plain tools/call on generate_monthly_report (app-level layer)."""
        return await self._task_call(name, {})

    async def poll_task(self, handle: str) -> TaskView:
        """Plain tools/call on get_report_task (app-level layer)."""
        return await self._task_call("get_report_task", {"handle": handle})

    async def cancel_task(self, handle: str) -> TaskView:
        """Plain tools/call on cancel_report_task (app-level layer)."""
        return await self._task_call("cancel_report_task", {"handle": handle})

    async def close(self) -> None:
        await self._client.__aexit__(None, None, None)
