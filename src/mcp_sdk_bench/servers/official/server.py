"""Official MCP Python SDK server variant — SPEC.md §6, Milestone 1.

Built with the SDK's low-level API (mcp.server.lowlevel.Server) so the variant
honestly represents the official SDK, not a fastmcp-style helper.

M1 surface: 5 tools (get_ticket, update_ticket, get_inventory, reserve_inventory,
deploy_service), 1 resource (company://policies/deployment), 1 prompt
(incident-triage). M2.1 adds probe_schema, a side-effect-free echo probe for
the SPEC.md §8 SCHEMA conformance category. One World instance per server
process, seeded at startup; no disk persistence.
"""
from __future__ import annotations

import json
from typing import Any, Literal

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.server import ServerRequestContext
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, Field, ValidationError

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
    so each server process owns exactly one in-memory world instance."""
    world: World = reset_world()

    tool_specs: dict[str, tuple[str, type[BaseModel], type[BaseModel]]] = {
        "get_ticket": ("Fetch a single ticket by id.", GetTicketInput, TicketOutput),
        "update_ticket": (
            "Update a ticket's status and/or assignee.",
            UpdateTicketInput,
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

    def _call_probe(arguments: dict[str, Any] | None) -> types.CallToolResult:
        try:
            args = ProbeSchemaInput.model_validate(arguments or {})
        except ValidationError as err:
            raise MCPError(
                INVALID_PARAMS, f"invalid arguments for {PROBE_SCHEMA_TOOL}: {err}"
            ) from err
        try:
            echo = world.probe_schema(
                string_field=args.string_field,
                int_field=args.int_field,
                float_field=args.float_field,
                bool_field=args.bool_field,
                enum_field=args.enum_field,
                nullable_field=args.nullable_field,
                union_field=args.union_field,
                list_field=args.list_field,
                nested_field=args.nested_field,
                nested_list_field=args.nested_list_field,
            )
        except WorldError as err:
            return _world_error(err)
        return _ok(ProbeSchemaOutput(**echo))

    async def on_call_tool(
        _ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        if params.name == PROBE_SCHEMA_TOOL:
            return _call_probe(params.arguments)
        spec = tool_specs.get(params.name)
        if spec is None:
            raise MCPError(INVALID_PARAMS, f"unknown tool {params.name}")
        _, input_model, _ = spec
        try:
            args = input_model.model_validate(params.arguments or {})
        except ValidationError as err:
            raise MCPError(INVALID_PARAMS, f"invalid arguments for {params.name}: {err}") from err
        try:
            match args:
                case GetTicketInput(ticket_id=ticket_id):
                    return _ok(TicketOutput(ticket=world.get_ticket(ticket_id)))
                case UpdateTicketInput(ticket_id=ticket_id, status=status, assignee=assignee):
                    return _ok(TicketOutput(ticket=world.update_ticket(ticket_id, status, assignee)))
                case GetInventoryInput():
                    return _ok(InventoryOutput(items=world.get_inventory()))
                case ReserveInventoryInput(item=item, employee_id=employee_id):
                    return _ok(ReserveInventoryOutput(item=world.reserve_inventory(item, employee_id)))
                case DeployServiceInput(service=service, target_version=version, environment=env):
                    return _ok(
                        DeploymentOutput(deployment=world.deploy_service(service, version, env))
                    )
        except WorldError as err:
            return _world_error(err)
        raise MCPError(INVALID_PARAMS, f"unknown tool {params.name}")  # pragma: no cover

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
