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
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal

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
    Deployment,
    InventoryItem,
    ProbeNestedItem,
    ProbeNestedObject,
    Ticket,
    TicketStatus,
    World,
    WorldError,
    reset_world,
)

SERVER_NAME = "mcp-sdk-bench-official"
SERVER_VERSION = "0.1.0"

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
    employee_id: str = Field(description="Employee id making the reservation, e.g. alice")


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
        name: str, arguments: dict[str, Any] | None
    ) -> tuple[Callable[[], types.CallToolResult], Callable[[], bool]]:
        """Validate arguments and return (execute, is_replay) thunks.

        Validation runs BEFORE any fault draw so protocol-level errors
        (unknown tool / invalid params) are never masked by injected faults.
        `is_replay` reports whether the call is an idempotent replay
        (create_ticket with an already-used key — SPEC.md §21); replays
        bypass the fault layer because they execute no transaction.
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
                return (
                    lambda: _ok(ReserveInventoryOutput(item=world.reserve_inventory(item, employee_id))),
                    lambda: False,
                )
            case DeployServiceInput(service=service, target_version=version, environment=env):
                return (
                    lambda: _ok(DeploymentOutput(deployment=world.deploy_service(service, version, env))),
                    lambda: False,
                )
        raise MCPError(INVALID_PARAMS, f"unknown tool {name}")  # pragma: no cover

    def _injected_fault(err: InjectedToolFault) -> types.CallToolResult:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=str(err))],
            is_error=True,
        )

    async def on_call_tool(
        _ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        execute, is_replay = _validated_call(params.name, params.arguments)
        try:
            return await run_tool_with_faults(fault_engine, execute, is_replay=is_replay)
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

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
        on_list_resources=on_list_resources,
        on_read_resource=on_read_resource,
        on_list_prompts=on_list_prompts,
        on_get_prompt=on_get_prompt,
    )
