"""Common protocol view (SPEC.md §2, layer L4) + adapter boundary (L3).

The adapter normalizes HOW a candidate is driven — connection bootstrap, call
dispatch, error mapping, lifecycle teardown — never WHAT it can express. If a
candidate cannot express resources or prompts, the Discovery projection must
show empty lists (absence as absence), never silent emulation.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any, Literal

from pydantic import BaseModel

from mcp_sdk_bench.world.state import MIGRATION_TASK_PREFIX


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict


class ResourceSpec(BaseModel):
    uri: str
    name: str
    mime_type: str
    description: str


class PromptSpec(BaseModel):
    name: str
    description: str
    arguments: list[dict]


class ToolResult(BaseModel):
    is_error: bool = False
    structured_content: dict | None = None
    text: str | None = None
    #: Normalized server-initiated elicitation (SPEC.md §18, M3.1):
    #: ``{kind: "clarification"|"approval", question: str, schema: dict}``.
    #: When set, the call is PAUSED — see MCPAdapter.respond_to_elicitation.
    elicitation_request: dict | None = None


class Discovery(BaseModel):
    tools: list[ToolSpec]
    resources: list[ResourceSpec]
    prompts: list[PromptSpec]


class TaskView(BaseModel):
    """SDK-agnostic view of one registered task (SPEC.md §17 report kind M3.2,
    SPEC.md §9 H migration kind M3.3).

    `status` uses the WORLD vocabulary (queued/running/completed/failed/
    cancelled); the official adapter maps the wire Task.status ("working")
    back to it. `progress` is 0.0..1.0. `result` carries the completed
    task's payload — {"report_id", "rows", "generated_at"} for reports,
    {"migration_id", "rows", "completed_at"} for migrations; `error` the
    failure message. Migration handles start with ``migrate-`` and report
    handles with ``report-`` (canonical prefixes in world/state.py)."""

    handle: str
    status: str
    progress: float = 0.0
    result: dict | None = None
    error: str | None = None


#: The variant-shared app-level task tools (SPEC.md §23 identical surface).
#: Report kind (M3.2): generate_monthly_report / get_report_task /
#: cancel_report_task. Migration kind (M3.3): start_migration /
#: get_migration_status / cancel_migration.
REPORT_START_TOOL = "generate_monthly_report"
MIGRATION_START_TOOL = "start_migration"
MIGRATION_POLL_TOOL = "get_migration_status"
MIGRATION_CANCEL_TOOL = "cancel_migration"


def resolve_start_tool(name: str) -> str:
    """Map a start_task() `name` argument to the wire tool name.

    Accepts both the kind token ("report" | "migration") and the app-level
    start-tool name (generate_monthly_report | start_migration) so the M3.2
    adapter tests and the M3.3 migration tests share one surface. Unknown
    names fail loud rather than silently dispatching a bogus tool call.
    """
    resolved = {
        "report": REPORT_START_TOOL,
        REPORT_START_TOOL: REPORT_START_TOOL,
        "migration": MIGRATION_START_TOOL,
        MIGRATION_START_TOOL: MIGRATION_START_TOOL,
    }.get(name)
    if resolved is None:
        raise ValueError(
            f"unknown task start name {name!r} (expected 'report' | "
            f"'migration' | {REPORT_START_TOOL} | {MIGRATION_START_TOOL})"
        )
    return resolved


def is_migration_handle(handle: str) -> bool:
    """True when a task handle belongs to the migration kind. The canonical
    prefixes are minted by the world registry (world/state.py)."""
    return handle.startswith(MIGRATION_TASK_PREFIX)


class MCPAdapter(ABC):
    """Async-only adapter boundary. One instance == one server session."""

    @abstractmethod
    async def connect(self) -> Discovery:
        """Open the connection, run protocol handshake, return discovery."""

    @abstractmethod
    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        """Invoke one tool. isError results map to ToolResult(is_error=True,
        text=message); structuredContent maps to structured_content.

        M3.1 (SPEC.md §18): when the server pauses the call for user input,
        this returns a ToolResult with `elicitation_request` set. The call is
        then PAUSED until respond_to_elicitation delivers the user's payload;
        the NEXT call_tool for the same (name, arguments) returns the resumed
        call's final result. Whether "resumed" is the same wire call
        completing (official SDK: the server handler awaits the
        elicitation/create response in-band) or a fresh wire call carrying
        the answer (FastMCP 4 on 2026-07-28: SEP-2322 end-and-reenter) is the
        adapter's own protocol mechanics — the common view exposes only the
        request/response pair."""

    @abstractmethod
    async def read_resource(self, uri: str) -> str:
        """Return resource content text. Raise RuntimeError with the server's
        message when the server reports an error."""

    @abstractmethod
    async def get_prompt(self, name: str, arguments: dict) -> str:
        """Render a prompt to its concatenated text content."""

    async def respond_to_elicitation(self, payload: dict) -> None:
        """Deliver the user's answer to a paused elicitation (SPEC.md §18).

        `payload` is the normalized response dict
        ``{status: approved|declined|clarified, answer: ...}``. Only valid
        after a call_tool returned a ToolResult with `elicitation_request`
        set. Adapters without a protocol elicitation surface (ADK, M3.1
        finding) keep the base implementation: NotImplementedError, never a
        stubbed success.
        """
        raise NotImplementedError(
            f"{type(self).__name__} has no protocol elicitation surface"
        )

    async def start_task(self, name: str, arguments: dict | None = None) -> TaskView:
        """Start a long-running task and return its initial view (SPEC.md
        §17, M3.2; SPEC.md §9 H, M3.3).

        `name` is the task kind token OR the variant's app-level start-tool
        name: ``"report"`` / ``"generate_monthly_report"`` start the
        monthly-report task; ``"migration"`` / ``"start_migration"`` start a
        customer-data migration with ``arguments={"scope": ...}`` (the
        world's migration scenario; scope "canary" is the deterministic
        failure lane). Both spellings are accepted so the M3.2 adapter tests
        (which pass the report tool name) and the M3.3 migration tests (which
        pass the kind token) share one surface.

        Layering contract (the honesty that feeds the capability matrix):
        the OFFICIAL adapter exercises the real MCP Tasks protocol surface
        (tools/call returning CreateTaskResult semantics, protocol
        tasks/get | tasks/cancel | tasks/list, server-pushed
        notifications/progress + notifications/tasks/status) for BOTH task
        kinds; the FASTMCP and ADK adapters exercise APP-LEVEL plain tools
        (report: generate_monthly_report / get_report_task /
        cancel_report_task; migration: start_migration / get_migration_status
        / cancel_migration) because neither SDK ships a Tasks surface. Each
        adapter's docstring states which layer it drives. Raises RuntimeError
        when the server rejects the start (e.g. the 2-concurrent-task limit,
        shared across kinds)."""
        raise NotImplementedError(
            f"{type(self).__name__} has no task surface (protocol or app-level)"
        )

    async def poll_task(self, handle: str) -> TaskView:
        """Poll one task (report or migration handle). Official: a real
        tasks/get request, with server-pushed progress merged in; fastmcp/
        adk: the get_report_task / get_migration_status plain tool, selected
        by the handle's kind prefix."""
        raise NotImplementedError(
            f"{type(self).__name__} has no task surface (protocol or app-level)"
        )

    async def cancel_task(self, handle: str) -> TaskView:
        """Cancel one task (report or migration handle). Official: a real
        tasks/cancel request; fastmcp/adk: the cancel_report_task /
        cancel_migration plain tool, selected by the handle's kind prefix."""
        raise NotImplementedError(
            f"{type(self).__name__} has no task surface (protocol or app-level)"
        )

    @abstractmethod
    async def close(self) -> None:
        """Tear down the connection. Safe to call once; never twice-required."""


# ---- elicitation client plumbing (SPEC.md §18, M3.1) ----


def infer_elicitation_kind(schema: dict[str, Any]) -> str:
    """Recover the world's elicitation kind from a wire requestedSchema.

    The world marks payloads with a root ``title`` (a spec-ignored key on
    the restricted requestedSchema subset); when a transport strips it, the
    shape is decisive for the two kinds the world can mint: an approval asks
    for exactly one boolean ``approved`` property, anything else is a
    clarification.
    """
    title = schema.get("title")
    if title in ("approval", "clarification"):
        return str(title)
    props = schema.get("properties") or {}
    if set(props) == {"approved"} and (props.get("approved") or {}).get("type") == "boolean":
        return "approval"
    return "clarification"


def elicitation_wire_content(
    request: dict[str, Any], payload: dict[str, Any]
) -> tuple[Literal["accept", "decline", "cancel"], dict[str, Any] | None]:
    """Map the normalized user payload onto the MCP ElicitResult shape:
    approved -> ("accept", {"approved": True}); clarified ->
    ("accept", {<requested field>: answer}); declined -> ("decline", None).
    """
    status = payload.get("status")
    if status == "approved":
        return "accept", {"approved": True}
    if status == "clarified":
        schema = request.get("schema") or {}
        required = schema.get("required") or []
        field = str(required[0]) if required else "value"
        return "accept", {field: payload.get("answer")}
    return "decline", None


class ElicitationBridge:
    """Pause/resume rendezvous shared by the elicitation-capable adapters.

    The SDK's elicitation callback runs inside the client session's receive
    loop while `call_tool` is still awaiting the response. The callback
    `post_request`s the normalized request and blocks in `wait_answer`;
    `call_tool` races the in-flight call against `wait_request` and returns
    the paused ToolResult when the elicitation fires first;
    `respond_to_elicitation` `deliver`s the user's payload, unblocking the
    callback so the paused call can complete.
    """

    def __init__(self) -> None:
        self._request: dict[str, Any] | None = None
        self._answer: dict[str, Any] | None = None
        self._request_event = asyncio.Event()
        self._answer_event = asyncio.Event()

    def post_request(self, request: dict[str, Any]) -> None:
        """Called by the SDK elicitation callback (receive-loop task)."""
        if self._request is not None:
            raise RuntimeError("concurrent elicitations are unsupported")
        self._request = request
        self._answer = None
        self._answer_event.clear()
        self._request_event.set()

    async def wait_request(self) -> dict[str, Any]:
        """Await the next incoming elicitation request (adapter call_tool)."""
        await self._request_event.wait()
        assert self._request is not None
        return self._request

    async def wait_answer(self) -> dict[str, Any]:
        """Await the user's payload (SDK callback, after post_request)."""
        await self._answer_event.wait()
        assert self._answer is not None
        answer = self._answer
        self._request = None
        self._request_event.clear()
        return answer

    def deliver(self, payload: dict[str, Any]) -> None:
        """Deliver the user's payload (adapter respond_to_elicitation)."""
        if self._request is None:
            raise RuntimeError("respond_to_elicitation with no pending elicitation")
        self._answer = payload
        self._answer_event.set()
