"""Deterministic shared benchmark world — SPEC.md §5.

Single source of truth for all server variants and all graders.
Rules:
- No wall-clock anywhere in state; mutation ordering is a monotonic counter.
- All mutations go through World methods so op_log is complete and
  graders can verify correct_final_state without LLM judging.
- Fixture data lives in fixtures.py; reset.py rebuilds from seed.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TicketStatus(str, Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    CLOSED = "CLOSED"


class DeploymentStatus(str, Enum):
    ACTIVE = "active"
    ROLLING_OUT = "rolling_out"
    FAILED = "failed"


class Employee(BaseModel):
    id: str
    name: str
    team: str
    title: str = ""


class Ticket(BaseModel):
    id: str
    title: str
    status: TicketStatus
    team: str = ""
    assignee: str | None = None
    description: str = ""
    priority: str | None = None
    #: Set only for tickets created via create_ticket (SPEC.md §21
    #: idempotency); seeded fixture tickets have no key.
    idempotency_key: str | None = None


class Document(BaseModel):
    id: str
    title: str
    body: str
    tags: list[str] = Field(default_factory=list)


class Deployment(BaseModel):
    service: str
    version: str
    environment: str
    status: DeploymentStatus = DeploymentStatus.ACTIVE


class InventoryItem(BaseModel):
    name: str
    available: int = 0
    reserved_by: list[str] = Field(default_factory=list)


class Project(BaseModel):
    id: str
    name: str
    team: str


class OpRecord(BaseModel):
    seq: int
    op: str
    entity: str
    entity_id: str
    payload: dict[str, Any] = Field(default_factory=dict)


class WorldError(Exception):
    """Domain error raised by world mutations (mapped to MCP errors by servers)."""


class ElicitationUnavailable(Exception):
    """Raised by a server's elicit fn when the CLIENT has no elicitation
    capability (M3.1, SPEC.md §18). The world catches it and falls back to
    its legacy no-channel policy, so capability-free clients keep the
    pre-M3.1 behavior exactly (honest degradation, not a faked answer)."""


# ---- elicitation seam (SPEC.md §18, M3.1) ----

#: The world's elicitation callback contract. The WORLD owns the policy
#: (which situations require clarification/approval and what the answer
#: means); the SERVER VARIANT owns the protocol mechanics behind the
#: callback (official SDK: imperative ``elicitation/create`` via
#: ``ServerSession.elicit_form``; FastMCP 4 on 2026-07-28: the SEP-2322
#: ``InputRequiredResult`` guard pattern). The callback receives the
#: normalized request ``{kind, question, schema}`` and returns the
#: normalized response ``{status: approved|declined|clarified, answer: ...}``.
#:
#: The seam methods are async precisely so this measurement stays honest:
#: the application code (this module) is identical across SDK variants and
#: only the callback — the protocol-specific code — differs.
ElicitFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

#: Kinds of elicitation the world can request.
ELICIT_CLARIFICATION = "clarification"
ELICIT_APPROVAL = "approval"

#: Canonical decline message for a rejected production-deployment approval.
DEPLOYMENT_DECLINED = "deployment declined by user"


def clarification_payload(field: str, question: str) -> dict[str, Any]:
    """Normalized clarification request: ask the user for one string field."""
    return {
        "kind": ELICIT_CLARIFICATION,
        "question": question,
        "schema": {
            "type": "object",
            # Root "title" doubles as the machine-readable kind marker; the
            # restricted requestedSchema subset ignores unknown root keys,
            # so clients also recover kind from the schema shape.
            "title": ELICIT_CLARIFICATION,
            "properties": {field: {"type": "string", "title": field}},
            "required": [field],
        },
    }


def approval_payload(question: str) -> dict[str, Any]:
    """Normalized approval request: ask the user for a boolean decision."""
    return {
        "kind": ELICIT_APPROVAL,
        "question": question,
        "schema": {
            "type": "object",
            "title": ELICIT_APPROVAL,
            "properties": {"approved": {"type": "boolean", "title": "Approved"}},
            "required": ["approved"],
        },
    }


def requested_field(payload: dict[str, Any]) -> str:
    """The single required property of a clarification payload's schema."""
    required = (payload.get("schema") or {}).get("required") or []
    return str(required[0]) if required else "value"


def elicitation_response(
    action: str, content: dict[str, Any] | None, payload: dict[str, Any]
) -> dict[str, Any]:
    """Normalize a wire-level elicitation outcome (the MCP ElicitResult
    action/content shape) into the world seam's response dict.

    accept + approval payload -> approved iff content.approved is truthy;
    accept + clarification payload -> clarified with the requested field's
    value; decline/cancel -> declined (the world's policy maps that to a
    WorldError).
    """
    if action != "accept":
        return {"status": "declined"}
    data = content or {}
    if payload.get("kind") == ELICIT_APPROVAL:
        return {"status": "approved" if data.get("approved") else "declined"}
    return {"status": "clarified", "answer": data.get(requested_field(payload))}


# ---- schema probe (SPEC.md §8 SCHEMA) ----

#: Valid values for the probe's enum_field. Kept as data (not an Enum) so the
#: world method signature stays ``enum_field: str``; each server variant maps
#: this constraint onto its own schema mechanism.
PROBE_SCHEMA_ENUM_VALUES: tuple[str, str, str] = ("alpha", "beta", "gamma")


class ProbeNestedObject(BaseModel):
    """Nested object shape for probe_schema.nested_field."""

    id: str
    tags: list[str]


class ProbeNestedItem(BaseModel):
    """Array element shape for probe_schema.nested_list_field."""

    name: str
    count: int


class World(BaseModel):
    employees: dict[str, Employee] = Field(default_factory=dict)
    tickets: dict[str, Ticket] = Field(default_factory=dict)
    documents: dict[str, Document] = Field(default_factory=dict)
    deployments: dict[str, Deployment] = Field(default_factory=dict)
    inventory: dict[str, InventoryItem] = Field(default_factory=dict)
    projects: dict[str, Project] = Field(default_factory=dict)
    op_log: list[OpRecord] = Field(default_factory=list)
    #: idempotency_key -> ticket_id for tickets created via create_ticket
    #: (SPEC.md §21). Populated on first execution only; a retry with a known
    #: key is a replay and never creates a second ticket.
    ticket_idempotency_keys: dict[str, str] = Field(default_factory=dict)

    # ---- mutation surface (single implementation shared by all server variants) ----

    def _record(self, op: str, entity: str, entity_id: str, **payload: Any) -> None:
        self.op_log.append(
            OpRecord(seq=len(self.op_log), op=op, entity=entity, entity_id=entity_id, payload=payload)
        )

    def get_ticket(self, ticket_id: str) -> Ticket:
        if ticket_id not in self.tickets:
            raise WorldError(f"ticket {ticket_id} not found")
        return self.tickets[ticket_id]

    def create_ticket(
        self,
        ticket_id: str,
        title: str,
        priority: str | None = None,
        *,
        idempotency_key: str,
    ) -> Ticket:
        """Create a ticket with idempotency semantics (SPEC.md §21).

        If `idempotency_key` was already used in this world session, return
        the EXISTING ticket unchanged (no duplicate, no error, no op_log
        entry) — a retried call must never create two tickets. Otherwise
        create the ticket (status OPEN) and record the key.
        """
        if idempotency_key in self.ticket_idempotency_keys:
            return self.tickets[self.ticket_idempotency_keys[idempotency_key]]
        if ticket_id in self.tickets:
            raise WorldError(f"ticket {ticket_id} already exists")
        ticket = Ticket(
            id=ticket_id,
            title=title,
            status=TicketStatus.OPEN,
            priority=priority,
            idempotency_key=idempotency_key,
        )
        self.tickets[ticket_id] = ticket
        self.ticket_idempotency_keys[idempotency_key] = ticket_id
        self._record(
            "create_ticket",
            "ticket",
            ticket_id,
            title=title,
            priority=priority,
            idempotency_key=idempotency_key,
        )
        return ticket

    def ticket_for_idempotency_key(self, idempotency_key: str) -> Ticket | None:
        """Return the ticket previously created under `idempotency_key`, or
        None. This is how the fault layer and the tests tell a create_ticket
        call that DID execute (side effect applied, key recorded) from a
        rejected/replayed one."""
        ticket_id = self.ticket_idempotency_keys.get(idempotency_key)
        return self.tickets.get(ticket_id) if ticket_id is not None else None

    def update_ticket(
        self,
        ticket_id: str,
        status: TicketStatus | None = None,
        assignee: str | None = None,
    ) -> Ticket:
        ticket = self.get_ticket(ticket_id)
        if status is not None:
            ticket.status = status
        if assignee is not None:
            ticket.assignee = assignee
        self._record("update_ticket", "ticket", ticket_id, status=status, assignee=assignee)
        return ticket

    def get_inventory(self) -> dict[str, InventoryItem]:
        return dict(self.inventory)

    async def reserve_inventory(
        self,
        item: str,
        employee_id: str | None = None,
        *,
        elicit: ElicitFn | None = None,
    ) -> InventoryItem:
        """Reserve one unit of an item for an employee (SPEC.md §18).

        Clarification policy (world-owned): a missing employee_id requires
        asking the user. With an `elicit` callback the world requests a
        clarification and the answer becomes the employee id; a decline (or
        empty answer) raises WorldError. Without a callback the call raises
        on the missing employee — the pre-M3.1 behavior for a request the
        server cannot clarify.
        """
        if item not in self.inventory:
            raise WorldError(f"unknown inventory item {item}")
        if employee_id is None:
            if elicit is None:
                raise WorldError(f"reserve_inventory for {item} requires an employee id")
            try:
                response = await elicit(
                    clarification_payload(
                        "employee_id",
                        f"Which employee should the {item} reservation be recorded for?",
                    )
                )
            except ElicitationUnavailable:
                # Client cannot answer elicitations: legacy behavior.
                raise WorldError(
                    f"reserve_inventory for {item} requires an employee id"
                ) from None
            if response.get("status") != "clarified" or not response.get("answer"):
                raise WorldError("reservation declined by user")
            employee_id = str(response["answer"])
        if employee_id not in self.employees:
            raise WorldError(f"unknown employee {employee_id}")
        inv = self.inventory[item]
        if inv.available <= 0:
            raise WorldError(f"{item} has no available units")
        inv.available -= 1
        inv.reserved_by.append(employee_id)
        self._record("reserve_inventory", "inventory", item, employee_id=employee_id)
        return inv

    def get_deployment(self, service: str) -> Deployment:
        if service not in self.deployments:
            raise WorldError(f"unknown deployment {service}")
        return self.deployments[service]

    async def deploy_service(
        self,
        service: str,
        target_version: str,
        environment: str,
        *,
        elicit: ElicitFn | None = None,
    ) -> Deployment:
        """Deploy a service (SPEC.md §18 approval flow).

        Approval policy (world-owned): a production deploy requires explicit
        user approval when an elicitation channel exists; a decline raises
        WorldError(DEPLOYMENT_DECLINED) and the world is left unchanged.
        Without a callback the pre-M3.1 guard applies unchanged (production
        deploys of services not already in production are rejected).
        """
        dep = self.get_deployment(service)
        if environment == "production":
            if elicit is not None:
                try:
                    response = await elicit(
                        approval_payload(
                            f"Approve deployment of {service} {target_version} to production?"
                        )
                    )
                except ElicitationUnavailable:
                    # Client cannot answer elicitations: legacy guard below.
                    elicit = None
                else:
                    if response.get("status") != "approved":
                        raise WorldError(DEPLOYMENT_DECLINED)
            # Legacy pre-M3.1 guard (no elicitation channel): production
            # deploys of services not already in production are rejected.
            if elicit is None and dep.environment != environment:
                raise WorldError(f"{service} is not deployed in {environment}")
        dep.version = target_version
        dep.environment = environment
        dep.status = DeploymentStatus.ACTIVE
        self._record("deploy_service", "deployment", service, version=target_version, environment=environment)
        return dep

    def probe_schema(
        self,
        string_field: str,
        int_field: int,
        float_field: float,
        bool_field: bool,
        enum_field: str,
        nullable_field: str | None,
        union_field: str | int,
        list_field: list[str],
        nested_field: ProbeNestedObject,
        nested_list_field: list[ProbeNestedItem],
    ) -> dict[str, Any]:
        """Side-effect-free echo probe exercising every JSON-schema primitive
        the SPEC.md §8 SCHEMA category requires. NOT recorded in op_log (no
        mutation), so it is safe for concurrency bursts. Returns a canonical
        dict that must be identical across all three server variants."""
        if enum_field not in PROBE_SCHEMA_ENUM_VALUES:
            raise WorldError(
                f"enum_field must be one of {list(PROBE_SCHEMA_ENUM_VALUES)}; got {enum_field!r}"
            )
        received: dict[str, Any] = {
            "string_field": string_field,
            "int_field": int_field,
            "float_field": float_field,
            "bool_field": bool_field,
            "enum_field": enum_field,
            "nullable_field": nullable_field,
            "union_field": union_field,
            "list_field": list_field,
            "nested_field": nested_field.model_dump(mode="json"),
            "nested_list_field": [item.model_dump(mode="json") for item in nested_list_field],
        }
        return {"received": received, "count": len(received)}

    def search_documents(self, query: str) -> list[Document]:
        q = query.lower()
        return [d for d in self.documents.values() if q in d.title.lower() or q in d.body.lower()]

    def find_employee(self, employee_id: str) -> Employee:
        if employee_id not in self.employees:
            raise WorldError(f"unknown employee {employee_id}")
        return self.employees[employee_id]
