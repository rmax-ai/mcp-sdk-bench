"""Official MCP Python SDK server variant — SPEC.md §6, Milestone 1.

Built with the SDK's low-level API (mcp.server.lowlevel.Server) so the variant
honestly represents the official SDK, not a fastmcp-style helper.

M1 surface: 5 tools (get_ticket, update_ticket, get_inventory, reserve_inventory,
deploy_service), 1 resource (company://policies/deployment), 1 prompt
(incident-triage). M2.1 adds probe_schema, a side-effect-free echo probe for
the SPEC.md §8 SCHEMA conformance category. M2.3a adds create_ticket (SPEC.md
§21 idempotent creation) and the shared deterministic fault layer
(mcp_sdk_bench.faults), driven by env vars read once at startup. One World
instance per server process, seeded at startup; no disk persistence.

M3.1 (SPEC.md §18) adds protocol elicitation on reserve_inventory (missing
employee -> clarification) and deploy_service (production -> approval). The
protocol mechanics here are exactly one seam: the world methods receive an
`elicit` callback that sends ``elicitation/create`` (form mode) via
``ServerSession.elicit_form`` and awaits the client's ElicitResult in-band,
with a timeout. Elicitation is a CLIENT-advertised capability in MCP
(ElicitationCapability lives on ClientCapabilities, not ServerCapabilities),
so there is nothing to declare server-side; the benchmark's official client
advertises it by registering an elicitation callback (see
adapters/official.py). reserve_inventory's employee_id becomes optional in
the input schema so the clarification flow is reachable from the wire
(identical schema change in all three variants — SPEC.md §23).

M3.2 (SPEC.md §17) adds REAL protocol Tasks on top of the low-level API.
mcp 2.1.1's high-level framework (mcp.server.mcpserver) and ServerSession
have zero task support, but the low-level Server dispatches any registered
method (tasks/* are not in the per-version SPEC_CLIENT_METHODS tables, so
they route straight to registered handlers with no version sieve), and
mcp.types carries the full Tasks vocabulary (Task, GetTaskRequest,
CancelTaskRequest, ListTasksRequest, GetTaskPayloadRequest,
TaskStatusNotification, ServerTasksCapability). This variant therefore:

(a) exposes generate_monthly_report as a plain tools/call that starts the
    world task and returns the task view as structuredContent (CreateTaskResult
    semantics — the handle + initial status), plus get_report_task /
    cancel_report_task app-level mirrors (present on ALL three variants for
    the SPEC.md §23 identical-tool-surface contract; the official ADAPTER
    drives the protocol tasks/* methods below, not these mirrors);
(b) registers low-level handlers for tasks/get, tasks/cancel, tasks/list and
    tasks/result (Server.add_request_handler) mapping to the world registry.
    tasks/result returns the task payload as a plain JSON object — the SDK's
    GetTaskPayloadResult type carries no payload field (SDK gap, recorded in
    docs/capability-matrix.md), so the handler returns a dict, which the
    low-level _serialize path passes through un-sieved for non-spec methods;
(c) emits notifications/progress (progressToken = task handle) and
    notifications/tasks/status on every tick/transition via
    ServerSession.send_progress_notification / send_notification. Gating:
    ClientSession 2.1.1 cannot advertise ClientTasksCapability (its
    _build_capabilities hardcodes sampling/elicitation/roots), so the opt-in
    is the request _meta progressToken on the start call (or on a tasks/get
    re-bind) — the only client→server per-request channel the SDK exposes.
    This is the documented _meta gate the M3.2 dispatch prompt allowed for;
(d) declares ServerTasksCapability(list/cancel/requests.tools.call) in the
    capabilities block (see initialization_options(); ServerCapabilities has
    a `tasks` field but Server.get_capabilities never populates it — set
    explicitly).

Fault-layer note: the three task tools BYPASS run_tool_with_faults. M3.2
failure injection is the world's ASYNCHRONOUS mid-task failure (one
FaultEngine.task_failure() draw at task start, applied at the first
progress tick); the synchronous per-call fault layer would instead fail the
start/poll calls themselves, which is not the SPEC.md §17 experiment.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import ServerRequestContext
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, Field, ValidationError

from mcp_sdk_bench.faults import (
    FaultEngine,
    InjectedToolFault,
    load_fault_config,
    run_tool_with_faults,
)
from mcp_sdk_bench.world import (
    PROBE_SCHEMA_ENUM_VALUES,
    REPORT_TASK_TTL_MS,
    Deployment,
    ElicitationUnavailable,
    ElicitFn,
    InventoryItem,
    ProbeNestedItem,
    ProbeNestedObject,
    ReportTask,
    ReportTaskStatus,
    ReportTaskView,
    Ticket,
    TicketStatus,
    World,
    WorldError,
    elicitation_response,
    load_task_tick_s,
    report_task_view,
    reset_world,
    task_timestamp,
)

SERVER_NAME = "mcp-sdk-bench-official"
SERVER_VERSION = "0.1.0"

#: Max wait for the client's elicitation response before the call fails loud
#: (WorldError) instead of hanging the tool handler.
ELICIT_TIMEOUT_S = 30.0

DEPLOYMENT_POLICY_URI = "company://policies/deployment"
DEPLOYMENT_POLICY_DOC_ID = "dep-policy"
INCIDENT_TRIAGE_PROMPT = "incident-triage"

# JSON-RPC error code for invalid params (unknown tool / resource / prompt).
INVALID_PARAMS = -32602


# ---- tool input models (schemas must stay equivalent across SDK variants) ----


class GetTicketInput(BaseModel):
    ticket_id: str = Field(description="Ticket identifier, e.g. PAY-123")


class UpdateTicketInput(BaseModel):
    ticket_id: str = Field(description="Ticket identifier, e.g. PAY-123")
    status: TicketStatus | None = Field(
        default=None, description="New ticket status; omit to leave unchanged"
    )
    assignee: str | None = Field(
        default=None, description="Employee id to assign; omit to leave unchanged"
    )


class GetInventoryInput(BaseModel):
    """No parameters — returns the full inventory snapshot."""


class ReserveInventoryInput(BaseModel):
    item: str = Field(description="Inventory item name, e.g. thinkpad-t14")
    employee_id: str | None = Field(
        default=None,
        description=(
            "Employee id making the reservation, e.g. alice; omit when unknown "
            "— the server will ask the user (elicitation)"
        ),
    )


class DeployServiceInput(BaseModel):
    service: str = Field(description="Service name, e.g. checkout")
    target_version: str = Field(description="Version to deploy, e.g. 1.8.3")
    environment: str = Field(description="Target environment, e.g. staging or production")


class CreateTicketInput(BaseModel):
    ticket_id: str = Field(description="Ticket identifier to create, e.g. T-1")
    title: str = Field(description="Ticket title")
    priority: str | None = Field(
        default=None, description="Ticket priority, e.g. high; omit for none"
    )
    idempotency_key: str = Field(
        description="Idempotency key; a retry with the same key returns the existing ticket"
    )


# probe_schema is registered with an EXPLICIT inputSchema (not a pydantic-
# generated one) so the wire schema states the §8 SCHEMA primitives exactly:
# nullable as type ["string", "null"], union as type ["string", "integer"].
# ProbeSchemaInput mirrors it and is the validation boundary inside
# on_call_tool; the two must stay in lockstep.
PROBE_SCHEMA_TOOL = "probe_schema"
PROBE_SCHEMA_DESCRIPTION = (
    "Side-effect-free echo probe exercising every JSON-schema primitive (SPEC.md §8 SCHEMA)."
)

PROBE_SCHEMA_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "string_field": {"type": "string"},
        "int_field": {"type": "integer"},
        "float_field": {"type": "number"},
        "bool_field": {"type": "boolean"},
        "enum_field": {"type": "string", "enum": list(PROBE_SCHEMA_ENUM_VALUES)},
        "nullable_field": {"type": ["string", "null"]},
        "union_field": {"type": ["string", "integer"]},
        "list_field": {"type": "array", "items": {"type": "string"}},
        "nested_field": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["id", "tags"],
        },
        "nested_list_field": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "count": {"type": "integer"},
                },
                "required": ["name", "count"],
            },
        },
    },
    "required": [
        "string_field",
        "int_field",
        "float_field",
        "bool_field",
        "enum_field",
        "nullable_field",
        "union_field",
        "list_field",
        "nested_field",
        "nested_list_field",
    ],
}


class ProbeSchemaInput(BaseModel):
    string_field: str
    int_field: int
    float_field: float
    bool_field: bool
    enum_field: Literal["alpha", "beta", "gamma"]
    nullable_field: str | None
    union_field: str | int
    list_field: list[str]
    nested_field: ProbeNestedObject
    nested_list_field: list[ProbeNestedItem]


# ---- M3.2 task tools (SPEC.md §17) ----

GENERATE_REPORT_TOOL = "generate_monthly_report"
GET_REPORT_TASK_TOOL = "get_report_task"
CANCEL_REPORT_TASK_TOOL = "cancel_report_task"

TASK_TOOL_DESCRIPTIONS = {
    GENERATE_REPORT_TOOL: (
        "Start a simulated monthly-report task; returns the task handle and initial status."
    ),
    GET_REPORT_TASK_TOOL: "Poll a report task by handle; returns status and progress.",
    CANCEL_REPORT_TASK_TOOL: "Cancel a running report task by handle.",
}


class GenerateMonthlyReportInput(BaseModel):
    """No parameters — starts the simulated monthly-report task."""


class ReportTaskHandleInput(BaseModel):
    handle: str = Field(description="Task handle returned by generate_monthly_report")


class ReportTaskOutput(BaseModel):
    task: ReportTaskView


def _wire_task_status(task: ReportTask) -> Literal["working", "completed", "failed", "cancelled"]:
    """World -> wire Task.status. The wire vocabulary has no queued/running
    split; both map to "working" and the world status rides statusMessage."""
    if task.status in (ReportTaskStatus.QUEUED, ReportTaskStatus.RUNNING):
        return "working"
    return task.status.value


def _task_result_kwargs(task: ReportTask) -> dict[str, Any]:
    return {
        "task_id": task.handle,
        "status": _wire_task_status(task),
        # statusMessage carries the world status (queued/running) or the
        # failure message — the only per-task text channel the wire Task has.
        "status_message": task.error if task.status == ReportTaskStatus.FAILED else task.status.value,
        "created_at": task_timestamp(task.created_seq),
        "last_updated_at": task_timestamp(task.updated_seq),
        # The wire Task types REQUIRE ttl and the session dump strips None
        # (exclude_none=True), so a fixed deterministic value is sent.
        "ttl": REPORT_TASK_TTL_MS,
    }


def _wire_task(task: ReportTask) -> types.Task:
    return types.Task(**_task_result_kwargs(task))


def _notification_opted_in(ctx: ServerRequestContext[Any]) -> bool:
    """Client opt-in for task notifications. SDK gap (documented, M3.2):
    ClientSession 2.1.1 cannot advertise ClientTasksCapability, so the gate
    is the request _meta progressToken — the base protocol's progress
    opt-in — instead of the declared capability."""
    return ctx.meta is not None and ctx.meta.get("progress_token") is not None


# ---- tool output models (drive both structuredContent and outputSchema) ----


class TicketOutput(BaseModel):
    ticket: Ticket


class InventoryOutput(BaseModel):
    items: dict[str, InventoryItem]


class ReserveInventoryOutput(BaseModel):
    item: InventoryItem


class DeploymentOutput(BaseModel):
    deployment: Deployment


class ProbeSchemaOutput(BaseModel):
    received: dict[str, Any]
    count: int


def _incident_triage_text(ticket_id: str) -> str:
    return (
        f"You are triaging incident ticket {ticket_id}. Follow these steps in order:\n"
        f"1. Inspect the ticket by calling the get_ticket tool with ticket_id \"{ticket_id}\".\n"
        f"2. Retrieve the deployment policy by reading the resource {DEPLOYMENT_POLICY_URI}.\n"
        "3. Identify the owning team from the ticket's team field.\n"
        "4. Inspect the owning team's deployment state for the services that team owns.\n"
        "5. Produce a recommendation for the next action, citing the policy constraints "
        "that apply."
    )


def create_server() -> Server[Any]:
    """Build the official-SDK M1 server. A fresh seeded World is created here,
    so each server process owns exactly one in-memory world instance. Fault
    config is read from the environment once, here, at startup (SPEC.md §21)."""
    world: World = reset_world()
    fault_engine = FaultEngine(load_fault_config())

    async def task_update_hook(task: ReportTask) -> None:
        """Protocol mechanics for the world notification seam (M3.2, SPEC.md
        §17): push notifications/progress (progressToken = task handle, so
        the client correlates notifications to tasks directly) and
        notifications/tasks/status on every tick/transition to the session
        bound to this task. The world invokes this only for opted-in tasks
        (see _notification_opted_in for the capability gate)."""
        session = world.task_session(task.handle)
        if session is None:
            # Rebound clients re-register on their first opt-in tasks/get; a
            # restarted process has no session until then.
            return
        await session.send_progress_notification(
            task.handle, task.progress, 1.0, task.status.value
        )
        # ServerSession.send_notification's type union predates the Tasks
        # vocabulary; runtime delivery is model_dump + channel write and
        # accepts TaskStatusNotification fine (verified against 2.1.1).
        await session.send_notification(
            cast(
                "Any",
                types.TaskStatusNotification(
                    params=types.TaskStatusNotificationParams(**_task_result_kwargs(task))
                ),
            )
        )

    world.set_task_runtime(tick_s=load_task_tick_s(), update_hook=task_update_hook)

    tool_specs: dict[str, tuple[str, type[BaseModel], type[BaseModel]]] = {
        "get_ticket": ("Fetch a single ticket by id.", GetTicketInput, TicketOutput),
        "update_ticket": (
            "Update a ticket's status and/or assignee.",
            UpdateTicketInput,
            TicketOutput,
        ),
        "create_ticket": (
            "Create a ticket; idempotent on idempotency_key (SPEC.md §21).",
            CreateTicketInput,
            TicketOutput,
        ),
        "get_inventory": (
            "List all inventory items with availability.",
            GetInventoryInput,
            InventoryOutput,
        ),
        "reserve_inventory": (
            "Reserve one unit of an inventory item for an employee.",
            ReserveInventoryInput,
            ReserveInventoryOutput,
        ),
        "deploy_service": (
            "Deploy a service at a target version to an environment.",
            DeployServiceInput,
            DeploymentOutput,
        ),
    }

    async def on_list_tools(
        _ctx: ServerRequestContext[Any], _params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                *(
                    types.Tool(
                        name=name,
                        description=description,
                        input_schema=input_model.model_json_schema(),
                        output_schema=output_model.model_json_schema(),
                    )
                    for name, (description, input_model, output_model) in tool_specs.items()
                ),
                types.Tool(
                    name=PROBE_SCHEMA_TOOL,
                    description=PROBE_SCHEMA_DESCRIPTION,
                    input_schema=PROBE_SCHEMA_INPUT_SCHEMA,
                    output_schema=ProbeSchemaOutput.model_json_schema(),
                ),
                *(
                    types.Tool(
                        name=name,
                        description=TASK_TOOL_DESCRIPTIONS[name],
                        input_schema=input_model.model_json_schema(),
                        output_schema=ReportTaskOutput.model_json_schema(),
                    )
                    for name, input_model in (
                        (GENERATE_REPORT_TOOL, GenerateMonthlyReportInput),
                        (GET_REPORT_TASK_TOOL, ReportTaskHandleInput),
                        (CANCEL_REPORT_TASK_TOOL, ReportTaskHandleInput),
                    )
                ),
            ]
        )

    def _ok(output_model: BaseModel) -> types.CallToolResult:
        payload = output_model.model_dump(mode="json")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(payload))],
            structured_content=payload,
        )

    def _world_error(err: WorldError) -> types.CallToolResult:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=str(err))],
            is_error=True,
        )

    def _validated_call(
        name: str, arguments: dict[str, Any] | None, elicit: ElicitFn
    ) -> tuple[
        Callable[[], types.CallToolResult | Awaitable[types.CallToolResult]],
        Callable[[], bool],
    ]:
        """Validate arguments and return (execute, is_replay) thunks.

        Validation runs BEFORE any fault draw so protocol-level errors
        (unknown tool / invalid params) are never masked by injected faults.
        `is_replay` reports whether the call is an idempotent replay
        (create_ticket with an already-used key — SPEC.md §21); replays
        bypass the fault layer because they execute no transaction.

        M3.1 (SPEC.md §18): `elicit` is the per-request elicitation callback
        (protocol mechanics owned here; policy owned by the world). Only
        reserve_inventory / deploy_service pass it on, and only their thunks
        are async — the fault layer awaits them (run_tool_with_faults).
        """
        if name == PROBE_SCHEMA_TOOL:
            try:
                probe = ProbeSchemaInput.model_validate(arguments or {})
            except ValidationError as err:
                raise MCPError(
                    INVALID_PARAMS, f"invalid arguments for {PROBE_SCHEMA_TOOL}: {err}"
                ) from err

            def execute_probe() -> types.CallToolResult:
                echo = world.probe_schema(
                    string_field=probe.string_field,
                    int_field=probe.int_field,
                    float_field=probe.float_field,
                    bool_field=probe.bool_field,
                    enum_field=probe.enum_field,
                    nullable_field=probe.nullable_field,
                    union_field=probe.union_field,
                    list_field=probe.list_field,
                    nested_field=probe.nested_field,
                    nested_list_field=probe.nested_list_field,
                )
                return _ok(ProbeSchemaOutput(**echo))

            return execute_probe, lambda: False

        spec = tool_specs.get(name)
        if spec is None:
            raise MCPError(INVALID_PARAMS, f"unknown tool {name}")
        _, input_model, _ = spec
        try:
            args = input_model.model_validate(arguments or {})
        except ValidationError as err:
            raise MCPError(INVALID_PARAMS, f"invalid arguments for {name}: {err}") from err
        match args:
            case GetTicketInput(ticket_id=ticket_id):
                return lambda: _ok(TicketOutput(ticket=world.get_ticket(ticket_id))), (
                    lambda: False
                )
            case UpdateTicketInput(ticket_id=ticket_id, status=status, assignee=assignee):
                return (
                    lambda: _ok(TicketOutput(ticket=world.update_ticket(ticket_id, status, assignee))),
                    lambda: False,
                )
            case CreateTicketInput(
                ticket_id=ticket_id, title=title, priority=priority, idempotency_key=key
            ):
                return (
                    lambda: _ok(
                        TicketOutput(
                            ticket=world.create_ticket(
                                ticket_id, title, priority, idempotency_key=key
                            )
                        )
                    ),
                    lambda: world.ticket_for_idempotency_key(key) is not None,
                )
            case GetInventoryInput():
                return lambda: _ok(InventoryOutput(items=world.get_inventory())), lambda: False
            case ReserveInventoryInput(item=item, employee_id=employee_id):
                async def execute_reserve() -> types.CallToolResult:
                    inv = await world.reserve_inventory(item, employee_id, elicit=elicit)
                    return _ok(ReserveInventoryOutput(item=inv))

                return execute_reserve, lambda: False
            case DeployServiceInput(service=service, target_version=version, environment=env):
                async def execute_deploy() -> types.CallToolResult:
                    dep = await world.deploy_service(service, version, env, elicit=elicit)
                    return _ok(DeploymentOutput(deployment=dep))

                return execute_deploy, lambda: False
        raise MCPError(INVALID_PARAMS, f"unknown tool {name}")  # pragma: no cover

    def _injected_fault(err: InjectedToolFault) -> types.CallToolResult:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=str(err))],
            is_error=True,
        )

    async def _on_task_tool(
        ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        """M3.2 app-level task tools (SPEC.md §17). Bypasses
        run_tool_with_faults by design — the task fault story is the world's
        asynchronous mid-task failure, not a synchronous call failure (see
        module docstring). The official ADAPTER drives the protocol tasks/*
        methods; these mirrors keep the tool surface identical across all
        three variants (SPEC.md §23)."""
        try:
            if params.name == GENERATE_REPORT_TOOL:
                GenerateMonthlyReportInput.model_validate(params.arguments or {})
                session = ctx.session if _notification_opted_in(ctx) else None
                task = await world.start_report_task(fault_engine, session=session)
            elif params.name == GET_REPORT_TASK_TOOL:
                args = ReportTaskHandleInput.model_validate(params.arguments or {})
                task = world.get_report_task(args.handle)
            else:
                args = ReportTaskHandleInput.model_validate(params.arguments or {})
                task = await world.cancel_report_task(args.handle)
        except ValidationError as err:
            raise MCPError(
                INVALID_PARAMS, f"invalid arguments for {params.name}: {err}"
            ) from err
        except WorldError as err:
            return _world_error(err)
        return _ok(ReportTaskOutput(task=report_task_view(task)))

    async def on_call_tool(
        _ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        if params.name in TASK_TOOL_DESCRIPTIONS:
            return await _on_task_tool(_ctx, params)
        async def elicit(payload: dict[str, Any]) -> dict[str, Any]:
            """M3.1 protocol mechanics (SPEC.md §18): send the world's
            elicitation request as ``elicitation/create`` (form mode) on this
            request's session and await the client's answer in-band.

            A client WITHOUT the elicitation capability answers
            INVALID_REQUEST "Elicitation not supported" — mapped to
            ElicitationUnavailable so the world falls back to its legacy
            no-channel policy (honest degradation, never a faked answer).
            Any other failure (a stalled client hitting the timeout, a real
            protocol error) fails loud as WorldError rather than hanging."""
            try:
                result = await asyncio.wait_for(
                    _ctx.session.elicit_form(payload["question"], payload["schema"]),
                    timeout=ELICIT_TIMEOUT_S,
                )
            except MCPError as err:
                if "not supported" in str(err).lower():
                    raise ElicitationUnavailable(str(err)) from err
                raise WorldError(f"elicitation failed: {err}") from err
            except TimeoutError as err:
                raise WorldError(f"elicitation timed out after {ELICIT_TIMEOUT_S}s") from err
            return elicitation_response(result.action, result.content, payload)

        execute, is_replay = _validated_call(params.name, params.arguments, elicit)

        async def settled() -> types.CallToolResult:
            # M3.1: eliciting tools are async thunks; settle explicitly so
            # the fault layer sees one uniform awaitable.
            value = execute()
            if inspect.isawaitable(value):
                return await cast("Awaitable[types.CallToolResult]", value)
            return cast("types.CallToolResult", value)

        try:
            return await run_tool_with_faults(fault_engine, settled, is_replay=is_replay)
        except InjectedToolFault as err:
            return _injected_fault(err)
        except WorldError as err:
            return _world_error(err)

    async def on_list_resources(
        _ctx: ServerRequestContext[Any], _params: types.PaginatedRequestParams | None
    ) -> types.ListResourcesResult:
        return types.ListResourcesResult(
            resources=[
                types.Resource(
                    uri=DEPLOYMENT_POLICY_URI,
                    name="deployment-policy",
                    title="Deployment Policy",
                    description="Company deployment policy (approvals, rollout plans, change freezes).",
                    mime_type="text/markdown",
                )
            ]
        )

    async def on_read_resource(
        _ctx: ServerRequestContext[Any], params: types.ReadResourceRequestParams
    ) -> types.ReadResourceResult:
        if str(params.uri) != DEPLOYMENT_POLICY_URI:
            raise MCPError(INVALID_PARAMS, f"unknown resource {params.uri}")
        doc = world.documents[DEPLOYMENT_POLICY_DOC_ID]
        return types.ReadResourceResult(
            contents=[
                types.TextResourceContents(
                    uri=DEPLOYMENT_POLICY_URI,
                    mime_type="text/markdown",
                    text=f"# {doc.title}\n\n{doc.body}",
                )
            ]
        )

    async def on_list_prompts(
        _ctx: ServerRequestContext[Any], _params: types.PaginatedRequestParams | None
    ) -> types.ListPromptsResult:
        return types.ListPromptsResult(
            prompts=[
                types.Prompt(
                    name=INCIDENT_TRIAGE_PROMPT,
                    description="Triage an incident ticket against the deployment policy.",
                    arguments=[
                        types.PromptArgument(
                            name="ticket_id",
                            description="Ticket identifier to triage, e.g. PAY-123",
                            required=True,
                        )
                    ],
                )
            ]
        )

    async def on_get_prompt(
        _ctx: ServerRequestContext[Any], params: types.GetPromptRequestParams
    ) -> types.GetPromptResult:
        if params.name != INCIDENT_TRIAGE_PROMPT:
            raise MCPError(INVALID_PARAMS, f"unknown prompt {params.name}")
        arguments = params.arguments or {}
        ticket_id = arguments.get("ticket_id")
        if not ticket_id:
            raise MCPError(INVALID_PARAMS, "incident-triage requires a ticket_id argument")
        return types.GetPromptResult(
            description=f"Incident triage workflow for {ticket_id}",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(type="text", text=_incident_triage_text(ticket_id)),
                )
            ],
        )

    # ---- M3.2 protocol Tasks handlers (SPEC.md §17) ----
    # tasks/* are absent from the per-version SPEC_CLIENT_METHODS tables in
    # mcp 2.1.1, so the runner routes them to these registered handlers with
    # no spec-version pre-validation or result sieve (verified against the
    # installed ServerRunner._on_request / _serialize).

    async def on_tasks_get(
        ctx: ServerRequestContext[Any], params: types.GetTaskRequestParams
    ) -> types.GetTaskResult:
        try:
            task = world.get_report_task(params.task_id)
        except WorldError as err:
            raise MCPError(INVALID_PARAMS, str(err)) from err
        if _notification_opted_in(ctx):
            # Re-bind the notification target (e.g. a reconnected client).
            world.register_task_session(task.handle, ctx.session)
        return types.GetTaskResult(**_task_result_kwargs(task))

    async def on_tasks_cancel(
        _ctx: ServerRequestContext[Any], params: types.CancelTaskRequestParams
    ) -> types.CancelTaskResult:
        try:
            task = await world.cancel_report_task(params.task_id)
        except WorldError as err:
            raise MCPError(INVALID_PARAMS, str(err)) from err
        return types.CancelTaskResult(**_task_result_kwargs(task))

    async def on_tasks_list(
        _ctx: ServerRequestContext[Any], _params: types.PaginatedRequestParams
    ) -> types.ListTasksResult:
        return types.ListTasksResult(
            tasks=[_wire_task(task) for task in world.list_report_tasks()]
        )

    async def on_tasks_result(
        _ctx: ServerRequestContext[Any], params: types.GetTaskPayloadRequestParams
    ) -> dict[str, Any]:
        # SDK gap (recorded in docs/capability-matrix.md): GetTaskPayloadResult
        # carries no payload field, so the payload returns as a plain JSON
        # object — the low-level _serialize path passes dicts through.
        try:
            task = world.get_report_task(params.task_id)
        except WorldError as err:
            raise MCPError(INVALID_PARAMS, str(err)) from err
        return {"task": report_task_view(task).model_dump(mode="json")}

    server = Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_read_resource=on_read_resource,
        on_list_prompts=on_list_prompts,
        on_get_prompt=on_get_prompt,
    )
    server.add_request_handler("tasks/get", types.GetTaskRequestParams, on_tasks_get)
    server.add_request_handler("tasks/cancel", types.CancelTaskRequestParams, on_tasks_cancel)
    server.add_request_handler("tasks/list", types.PaginatedRequestParams, on_tasks_list)
    server.add_request_handler(
        "tasks/result", types.GetTaskPayloadRequestParams, on_tasks_result
    )
    return server


def server_tasks_capability() -> types.ServerTasksCapability:
    """The Tasks capability this server serves (M3.2)."""
    return types.ServerTasksCapability(
        list=types.TasksListCapability(),
        cancel=types.TasksCancelCapability(),
        requests=types.ServerTasksRequestsCapability(
            tools=types.TasksToolsCapability(call=types.TasksCallCapability())
        ),
    )


def initialization_options(server: Server[Any]) -> Any:
    """server.create_initialization_options() PLUS the Tasks capability.

    SDK gap (documented, M3.2): ServerCapabilities HAS a `tasks` field, but
    Server.get_capabilities derives only prompts/resources/tools/logging/
    completions from the registered handlers and never populates `tasks`
    even when tasks/* handlers are registered — declared explicitly here.
    """
    options = server.create_initialization_options()
    options.capabilities.tasks = server_tasks_capability()
    return options
