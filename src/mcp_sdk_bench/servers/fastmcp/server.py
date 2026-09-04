"""FastMCP 4.x server variant — SPEC.md §6, Milestone 1.

Built with FastMCP's decorator API (@server.tool / @server.resource /
@server.prompt). Tool input schemas are pydantic-generated from annotated
signatures; output schemas are return-type-driven. The contract is identical
to the official-SDK variant: same 5 tools, same parameter names and field
descriptions, same resource, same prompt — only the framework differs.

M1 surface: 5 tools (get_ticket, update_ticket, get_inventory,
reserve_inventory, deploy_service), 1 resource (company://policies/deployment),
1 prompt (incident-triage). M2.1 adds probe_schema, a side-effect-free echo
probe for the SPEC.md §8 SCHEMA conformance category (schema generated from
the annotated signature; enum via the SchemaEnum class). M2.3a adds
create_ticket (SPEC.md §21 idempotent creation) and the shared deterministic
fault layer (mcp_sdk_bench.faults), driven by env vars read once at startup.
One World instance per server process, seeded at startup; no disk persistence.
WorldError and InjectedToolFault surface as an MCP tool error (isError)
carrying the error message, via fastmcp.exceptions.ToolError.

M3.1 (SPEC.md §18) adds elicitation on reserve_inventory (missing employee
-> clarification) and deploy_service (production -> approval). The FastMCP
client negotiates the modern 2026-07-28 protocol with this server, where
imperative ``ctx.elicit()`` is deliberately unavailable (SEP-2577 removed
the back-channel), so the protocol mechanics are the SEP-2322 guard pattern:
the world's elicit callback raises _ElicitationPending when the current
request leg carries no answer, the tool returns an
``InputRequiredResult(input_requests={...: ElicitRequest(...)})``, the client
fulfills it and RE-CALLS the tool with ``input_responses`` populated, and the
re-entered callback resolves the answer from ``ctx.input_responses``. The
world code is byte-identical to the official variant — only this callback
and the InputRequiredResult translation are protocol-specific. Output schemas
are pinned explicitly so the guard-leg return type never leaks into the wire
contract. On handshake-era connections (< 2026-07-28) the guard return is
rejected with FastMCP's era error (documented limitation; the benchmark's
FastMCP client is always modern).
"""
from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from enum import Enum
from functools import partial
from typing import Annotated, Any, TypeVar, cast, overload

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp import types
from pydantic import BaseModel, Field

from mcp_sdk_bench.faults import (
    FaultEngine,
    InjectedToolFault,
    load_fault_config,
    run_tool_with_faults,
)
from mcp_sdk_bench.world import (
    Deployment,
    ElicitationUnavailable,
    ElicitFn,
    InventoryItem,
    ProbeNestedItem,
    ProbeNestedObject,
    Ticket,
    TicketStatus,
    World,
    WorldError,
    elicitation_response,
    reset_world,
)

SERVER_NAME = "mcp-sdk-bench-fastmcp"
SERVER_VERSION = "0.1.0"


class _ElicitationPending(Exception):
    """Leg-1 signal (SPEC.md §18, SEP-2322): the world's elicit policy fired
    but this request leg carries no client answer yet. Caught by the tool
    and translated into an InputRequiredResult; never a user-facing error."""

    def __init__(self, key: str, payload: dict[str, Any]) -> None:
        super().__init__(f"elicitation pending: {key}")
        self.key = key
        self.payload = payload


def _elicit_key(payload: dict[str, Any]) -> str:
    """Server-assigned input_requests key; the client echoes it on retry."""
    return f"elicitation:{payload['kind']}"


def _input_required(pending: _ElicitationPending) -> types.InputRequiredResult:
    """Translate the world payload into a SEP-2322 form-mode elicitation
    request embedded in an InputRequiredResult (2026-07-28 wire shape)."""
    return types.InputRequiredResult(
        input_requests={
            pending.key: types.ElicitRequest(
                params=types.ElicitRequestFormParams(
                    message=pending.payload["question"],
                    requested_schema=pending.payload["schema"],
                )
            )
        }
    )


async def _elicit_via_ctx(ctx: Context, payload: dict[str, Any]) -> dict[str, Any]:
    """FastMCP guard-pattern elicit callback (protocol mechanics, SPEC.md
    §18): on the first leg there is no answer — signal _ElicitationPending
    so the tool returns an InputRequiredResult; on the re-entered leg read
    the client's ElicitResult from ctx.input_responses and normalize it into
    the world seam's response dict.

    A client WITHOUT the elicitation capability could not answer the
    embedded ElicitRequest (its driver would fail the whole call with
    "Elicitation not supported"); checked up front so the world falls back
    to its legacy no-channel policy instead (honest degradation)."""
    rc = ctx.request_context
    capabilities = rc.session.client_capabilities if rc is not None else None
    if capabilities is None or capabilities.elicitation is None:
        raise ElicitationUnavailable("client did not advertise elicitation")
    key = _elicit_key(payload)
    responses = ctx.input_responses
    if responses is None or key not in responses:
        raise _ElicitationPending(key, payload)
    result = responses[key]
    if not isinstance(result, types.ElicitResult):
        raise WorldError(f"unexpected input response for {key}: {type(result).__name__}")
    return elicitation_response(result.action, result.content, payload)

DEPLOYMENT_POLICY_URI = "company://policies/deployment"
DEPLOYMENT_POLICY_DOC_ID = "dep-policy"
INCIDENT_TRIAGE_PROMPT = "incident-triage"


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


class SchemaEnum(str, Enum):
    """Enum for probe_schema.enum_field; FastMCP generates the schema
    constraint from this class (verified against installed FastMCP 4.0.2:
    emits {"type": "string", "enum": [...]}, and nullable / union /
    list-of-object fields all generate correct anyOf/object schemas — no
    FastMCP 4 deviation to record for probe_schema)."""

    ALPHA = "alpha"
    BETA = "beta"
    GAMMA = "gamma"


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


_T = TypeVar("_T")


def create_server() -> FastMCP:
    """Build the FastMCP M1 server. A fresh seeded World is created here,
    so each server process owns exactly one in-memory world instance. Fault
    config is read from the environment once, here, at startup (SPEC.md §21)."""
    world: World = reset_world()
    fault_engine = FaultEngine(load_fault_config())
    server = FastMCP(SERVER_NAME, version=SERVER_VERSION)

    @overload
    async def _run(
        execute: Callable[[], Awaitable[_T]],
        *,
        is_replay: Callable[[], bool] = ...,
    ) -> _T: ...

    @overload
    async def _run(
        execute: Callable[[], _T],
        *,
        is_replay: Callable[[], bool] = ...,
    ) -> _T: ...

    async def _run(
        execute: Callable[[], _T | Awaitable[_T]],
        *,
        is_replay: Callable[[], bool] = lambda: False,
    ) -> _T:
        """Shared dispatch through the deterministic fault layer (SPEC.md
        §21); identical semantics to the other two server variants. Execute
        thunks may be async (M3.1 eliciting tools). _ElicitationPending is
        NOT an error — it propagates to the tool body, which returns the
        SEP-2322 InputRequiredResult for the leg."""
        async def settled() -> _T:
            value = execute()
            if inspect.isawaitable(value):
                return await cast("Awaitable[_T]", value)
            return cast("_T", value)

        try:
            return await run_tool_with_faults(fault_engine, settled, is_replay=is_replay)
        except (InjectedToolFault, WorldError) as err:
            raise ToolError(str(err)) from err

    @server.tool(description="Fetch a single ticket by id.")
    async def get_ticket(
        ticket_id: Annotated[str, Field(description="Ticket identifier, e.g. PAY-123")],
    ) -> TicketOutput:
        return await _run(lambda: TicketOutput(ticket=world.get_ticket(ticket_id)))

    @server.tool(description="Update a ticket's status and/or assignee.")
    async def update_ticket(
        ticket_id: Annotated[str, Field(description="Ticket identifier, e.g. PAY-123")],
        status: Annotated[
            TicketStatus | None,
            Field(description="New ticket status; omit to leave unchanged"),
        ] = None,
        assignee: Annotated[
            str | None,
            Field(description="Employee id to assign; omit to leave unchanged"),
        ] = None,
    ) -> TicketOutput:
        return await _run(
            lambda: TicketOutput(ticket=world.update_ticket(ticket_id, status, assignee))
        )

    @server.tool(description="Create a ticket; idempotent on idempotency_key (SPEC.md §21).")
    async def create_ticket(
        ticket_id: Annotated[str, Field(description="Ticket identifier to create, e.g. T-1")],
        title: Annotated[str, Field(description="Ticket title")],
        idempotency_key: Annotated[
            str,
            Field(
                description="Idempotency key; a retry with the same key returns the existing ticket"
            ),
        ],
        priority: Annotated[
            str | None, Field(description="Ticket priority, e.g. high; omit for none")
        ] = None,
    ) -> TicketOutput:
        return await _run(
            lambda: TicketOutput(
                ticket=world.create_ticket(
                    ticket_id, title, priority, idempotency_key=idempotency_key
                )
            ),
            is_replay=lambda: world.ticket_for_idempotency_key(idempotency_key) is not None,
        )

    @server.tool(description="List all inventory items with availability.")
    async def get_inventory() -> InventoryOutput:
        return await _run(lambda: InventoryOutput(items=world.get_inventory()))

    @server.tool(
        description="Reserve one unit of an inventory item for an employee.",
        # Pinned: the M3.1 guard leg returns InputRequiredResult, which must
        # not leak into the wire outputSchema (SPEC.md §23 schema parity).
        output_schema=ReserveInventoryOutput.model_json_schema(),
    )
    async def reserve_inventory(
        item: Annotated[str, Field(description="Inventory item name, e.g. thinkpad-t14")],
        ctx: Context,
        employee_id: Annotated[
            str | None,
            Field(
                description=(
                    "Employee id making the reservation, e.g. alice; omit when unknown "
                    "— the server will ask the user (elicitation)"
                )
            ),
        ] = None,
    ) -> ReserveInventoryOutput | types.InputRequiredResult:
        async def execute() -> ReserveInventoryOutput:
            elicit: ElicitFn = partial(_elicit_via_ctx, ctx)
            inv = await world.reserve_inventory(item, employee_id, elicit=elicit)
            return ReserveInventoryOutput(item=inv)

        try:
            return await _run(execute)
        except _ElicitationPending as pending:
            return _input_required(pending)

    @server.tool(
        description="Deploy a service at a target version to an environment.",
        output_schema=DeploymentOutput.model_json_schema(),
    )
    async def deploy_service(
        service: Annotated[str, Field(description="Service name, e.g. checkout")],
        target_version: Annotated[str, Field(description="Version to deploy, e.g. 1.8.3")],
        environment: Annotated[
            str, Field(description="Target environment, e.g. staging or production")
        ],
        ctx: Context,
    ) -> DeploymentOutput | types.InputRequiredResult:
        async def execute() -> DeploymentOutput:
            elicit: ElicitFn = partial(_elicit_via_ctx, ctx)
            dep = await world.deploy_service(service, target_version, environment, elicit=elicit)
            return DeploymentOutput(deployment=dep)

        try:
            return await _run(execute)
        except _ElicitationPending as pending:
            return _input_required(pending)

    @server.tool(
        description=(
            "Side-effect-free echo probe exercising every JSON-schema primitive "
            "(SPEC.md §8 SCHEMA)."
        )
    )
    async def probe_schema(
        string_field: str,
        int_field: int,
        float_field: float,
        bool_field: bool,
        enum_field: SchemaEnum,
        nullable_field: str | None,
        union_field: str | int,
        list_field: list[str],
        nested_field: ProbeNestedObject,
        nested_list_field: list[ProbeNestedItem],
    ) -> ProbeSchemaOutput:
        return await _run(
            lambda: ProbeSchemaOutput(
                **world.probe_schema(
                    string_field=string_field,
                    int_field=int_field,
                    float_field=float_field,
                    bool_field=bool_field,
                    enum_field=enum_field.value,
                    nullable_field=nullable_field,
                    union_field=union_field,
                    list_field=list_field,
                    nested_field=nested_field,
                    nested_list_field=nested_list_field,
                )
            )
        )

    @server.resource(
        DEPLOYMENT_POLICY_URI,
        name="deployment-policy",
        title="Deployment Policy",
        description="Company deployment policy (approvals, rollout plans, change freezes).",
        mime_type="text/markdown",
    )
    def deployment_policy() -> str:
        doc = world.documents[DEPLOYMENT_POLICY_DOC_ID]
        return f"# {doc.title}\n\n{doc.body}"

    @server.prompt(
        name=INCIDENT_TRIAGE_PROMPT,
        description="Triage an incident ticket against the deployment policy.",
    )
    def incident_triage(
        ticket_id: Annotated[
            str, Field(description="Ticket identifier to triage, e.g. PAY-123")
        ],
    ) -> str:
        return _incident_triage_text(ticket_id)

    return server
