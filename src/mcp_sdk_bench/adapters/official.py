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

M3.2 (SPEC.md §17): this adapter exercises the REAL MCP Tasks protocol
surface — the PROTOCOL layer, unlike fastmcp/adk (app-level plain tools).
ClientSession 2.1.1 ships no task methods (verified: no get_task /
cancel_task / list_tasks on mcp.client.session.ClientSession), so the task
requests go through send_request with the mcp.types Tasks vocabulary
(GetTaskRequest / CancelTaskRequest / ListTasksRequest /
GetTaskPayloadRequest — real wire methods tasks/get, tasks/cancel,
tasks/list, tasks/result against the server's registered low-level
handlers). Server-pushed notifications: notifications/progress parses at
the negotiated 2025-11-25 version and tees to message_handler;
notifications/tasks/status is absent from every per-version method table
(SDK gap), so it arrives through a NotificationBinding. Both feed the poll
result: poll_task merges server-pushed progress with the tasks/get
snapshot (and fetches the completed payload via tasks/result). The client
cannot advertise ClientTasksCapability (_build_capabilities hardcodes
sampling/elicitation/roots), so it opts into notifications with a request
_meta progressToken on start/poll — documented in servers/official.
"""
from __future__ import annotations

import asyncio
import contextlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.extension import NotificationBinding
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, ConfigDict

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
    """An in-flight wire call paused on a server elicitation (M3.1)."""

    task: asyncio.Task[ToolResult]
    name: str
    arguments: dict[str, Any]


@dataclass
class _TaskPush:
    """Server-pushed task notification cache (M3.2, SPEC.md §17).

    progress: handle -> max pushed progress (notifications/progress,
    progressToken = task handle); statuses: handle -> latest pushed wire
    status (notifications/tasks/status, via NotificationBinding); events:
    ordered (handle, progress) log for wire-level assertions."""

    progress: dict[str, float] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)
    events: list[tuple[str, float]] = field(default_factory=list)


class _TaskResultPayload(BaseModel):
    """tasks/result response envelope: the server returns the task view as a
    plain JSON object (SDK gap: GetTaskPayloadResult carries no payload
    field), and send_request's result TypeVar is bound to BaseModel, so a
    typed envelope is needed here."""

    model_config = ConfigDict(extra="allow")

    task: dict[str, Any] | None = None


def _world_status(wire_status: str, status_message: str | None) -> str:
    """Wire Task.status -> world vocabulary. The wire has no queued/running
    split ("working" covers both); the server carries the world status in
    statusMessage."""
    if wire_status == "working":
        return status_message if status_message in ("queued", "running") else "running"
    return wire_status


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
        #: M3.2 task plumbing (SPEC.md §17).
        self._task_push = _TaskPush()
        self._task_token_seq = 0

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
            read_stream,
            write_stream,
            elicitation_callback=self._on_elicitation,
            # M3.2: message_handler receives notifications/progress (core
            # table parses it at 2025-11-25); the binding receives
            # notifications/tasks/status (absent from every version table).
            message_handler=self._on_message,
            notification_bindings=[
                NotificationBinding(
                    method="notifications/tasks/status",
                    params_type=types.TaskStatusNotificationParams,
                    handler=self._on_task_status,
                )
            ],
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

    # ---- M3.2 protocol Tasks (SPEC.md §17) — the PROTOCOL layer ----

    async def _on_message(self, message: Any) -> None:
        """ClientSession message_handler tee: capture server-pushed
        notifications/progress (progressToken = task handle)."""
        if isinstance(message, types.ProgressNotification):
            handle = str(message.params.progress_token)
            progress = float(message.params.progress)
            self._task_push.progress[handle] = max(
                self._task_push.progress.get(handle, 0.0), progress
            )
            self._task_push.events.append((handle, progress))

    async def _on_task_status(self, params: types.TaskStatusNotificationParams) -> None:
        """NotificationBinding handler for notifications/tasks/status."""
        self._task_push.statuses[params.task_id] = params.status

    def pushed_progress(self, handle: str) -> list[float]:
        """Ordered server-pushed progress values for `handle` (wire-level
        assertion surface for tests/tasks/)."""
        return [progress for h, progress in self._task_push.events if h == handle]

    def pushed_status(self, handle: str) -> str | None:
        """Latest server-pushed wire status for `handle`
        (notifications/tasks/status via the NotificationBinding), or None."""
        return self._task_push.statuses.get(handle)

    def _next_task_token(self, kind: str) -> str:
        self._task_token_seq += 1
        return f"mcp-sdk-bench-task-{kind}-{self._task_token_seq}"

    async def start_task(self, name: str) -> TaskView:
        """tools/call on the task-starting tool with CreateTaskResult
        semantics (handle + initial status in structuredContent). The _meta
        progressToken opts this client into the task's server-pushed
        notifications (the SDK cannot advertise ClientTasksCapability — see
        module docstring)."""
        assert self._session is not None
        result = await self._session.call_tool(
            name,
            {},
            meta=types.RequestParamsMeta(progress_token=self._next_task_token("start")),
        )
        if not isinstance(result, types.CallToolResult):
            raise TypeError(f"unexpected result type {type(result).__name__}")
        if result.is_error or not result.structured_content:
            raise RuntimeError(_text_of(result.content) or "task start failed")
        return TaskView(**result.structured_content["task"])

    async def poll_task(self, handle: str) -> TaskView:
        """Real tasks/get request; server-pushed progress merged in. On
        completion the payload is fetched with a real tasks/result request
        (the wire Task carries no result payload; the server's tasks/result
        returns the view as a JSON object — SDK gap documented server-side).
        The poll carries a progressToken so a RECONNECTED client re-binds
        the task's notification target to this session."""
        assert self._session is not None
        wire = await self._session.send_request(
            types.GetTaskRequest(
                params=types.GetTaskRequestParams(
                    task_id=handle,
                    meta=types.RequestParamsMeta(
                        progress_token=self._next_task_token("poll")
                    ),
                )
            ),
            types.GetTaskResult,
        )
        status = _world_status(wire.status, wire.status_message)
        progress = self._task_push.progress.get(handle, 0.0)
        result: dict[str, Any] | None = None
        error: str | None = None
        if status == "completed":
            progress = 1.0
            payload = await self._session.send_request(
                types.GetTaskPayloadRequest(
                    params=types.GetTaskPayloadRequestParams(task_id=handle)
                ),
                _TaskResultPayload,
            )
            result = (payload.task or {}).get("result")
        elif status == "failed":
            error = wire.status_message
        return TaskView(
            handle=handle, status=status, progress=progress, result=result, error=error
        )

    async def cancel_task(self, handle: str) -> TaskView:
        """Real tasks/cancel request."""
        assert self._session is not None
        wire = await self._session.send_request(
            types.CancelTaskRequest(
                params=types.CancelTaskRequestParams(task_id=handle)
            ),
            types.CancelTaskResult,
        )
        return TaskView(
            handle=handle,
            status=_world_status(wire.status, wire.status_message),
            progress=self._task_push.progress.get(handle, 0.0),
        )

    async def list_tasks(self) -> list[TaskView]:
        """Real tasks/list request (wire-level assertion surface; not part
        of the common adapter view)."""
        assert self._session is not None
        wire = await self._session.send_request(types.ListTasksRequest(), types.ListTasksResult)
        return [
            TaskView(
                handle=task.task_id,
                status=_world_status(task.status, task.status_message),
                progress=self._task_push.progress.get(task.task_id, 0.0),
            )
            for task in wire.tasks
        ]

    async def close(self) -> None:
        if self._session_cm is not None:
            await self._session_cm.__aexit__(None, None, None)
            self._session_cm = None
            self._session = None
        if self._stdio_cm is not None:
            await self._stdio_cm.__aexit__(None, None, None)
            self._stdio_cm = None
