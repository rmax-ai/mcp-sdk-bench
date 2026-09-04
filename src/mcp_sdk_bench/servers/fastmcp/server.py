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
the annotated signature; enum via the SchemaEnum class). One World instance
per server process, seeded at startup; no disk persistence. WorldError
surfaces as an MCP tool error (isError) carrying the WorldError message, via
fastmcp.exceptions.ToolError.
"""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from mcp_sdk_bench.world import (
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

SERVER_NAME = "mcp-sdk-bench-fastmcp"
SERVER_VERSION = "0.1.0"

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


def create_server() -> FastMCP:
    """Build the FastMCP M1 server. A fresh seeded World is created here,
    so each server process owns exactly one in-memory world instance."""
    world: World = reset_world()
    server = FastMCP(SERVER_NAME, version=SERVER_VERSION)

    @server.tool(description="Fetch a single ticket by id.")
    def get_ticket(
        ticket_id: Annotated[str, Field(description="Ticket identifier, e.g. PAY-123")],
    ) -> TicketOutput:
        try:
            return TicketOutput(ticket=world.get_ticket(ticket_id))
        except WorldError as err:
            raise ToolError(str(err)) from err

    @server.tool(description="Update a ticket's status and/or assignee.")
    def update_ticket(
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
        try:
            return TicketOutput(ticket=world.update_ticket(ticket_id, status, assignee))
        except WorldError as err:
            raise ToolError(str(err)) from err

    @server.tool(description="List all inventory items with availability.")
    def get_inventory() -> InventoryOutput:
        return InventoryOutput(items=world.get_inventory())

    @server.tool(description="Reserve one unit of an inventory item for an employee.")
    def reserve_inventory(
        item: Annotated[str, Field(description="Inventory item name, e.g. thinkpad-t14")],
        employee_id: Annotated[
            str, Field(description="Employee id making the reservation, e.g. alice")
        ],
    ) -> ReserveInventoryOutput:
        try:
            return ReserveInventoryOutput(item=world.reserve_inventory(item, employee_id))
        except WorldError as err:
            raise ToolError(str(err)) from err

    @server.tool(description="Deploy a service at a target version to an environment.")
    def deploy_service(
        service: Annotated[str, Field(description="Service name, e.g. checkout")],
        target_version: Annotated[str, Field(description="Version to deploy, e.g. 1.8.3")],
        environment: Annotated[
            str, Field(description="Target environment, e.g. staging or production")
        ],
    ) -> DeploymentOutput:
        try:
            return DeploymentOutput(
                deployment=world.deploy_service(service, target_version, environment)
            )
        except WorldError as err:
            raise ToolError(str(err)) from err

    @server.tool(
        description=(
            "Side-effect-free echo probe exercising every JSON-schema primitive "
            "(SPEC.md §8 SCHEMA)."
        )
    )
    def probe_schema(
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
        try:
            return ProbeSchemaOutput(
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
        except WorldError as err:
            raise ToolError(str(err)) from err

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
