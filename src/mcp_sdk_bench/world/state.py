"""Deterministic shared benchmark world — SPEC.md §5.

Single source of truth for all server variants and all graders.
Rules:
- No wall-clock anywhere in state; mutation ordering is a monotonic counter.
- All mutations go through World methods so op_log is complete and
  graders can verify correct_final_state without LLM judging.
- Fixture data lives in fixtures.py; reset.py rebuilds from seed.
"""
from __future__ import annotations

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


class World(BaseModel):
    employees: dict[str, Employee] = Field(default_factory=dict)
    tickets: dict[str, Ticket] = Field(default_factory=dict)
    documents: dict[str, Document] = Field(default_factory=dict)
    deployments: dict[str, Deployment] = Field(default_factory=dict)
    inventory: dict[str, InventoryItem] = Field(default_factory=dict)
    projects: dict[str, Project] = Field(default_factory=dict)
    op_log: list[OpRecord] = Field(default_factory=list)

    # ---- mutation surface (single implementation shared by all server variants) ----

    def _record(self, op: str, entity: str, entity_id: str, **payload: Any) -> None:
        self.op_log.append(
            OpRecord(seq=len(self.op_log), op=op, entity=entity, entity_id=entity_id, payload=payload)
        )

    def get_ticket(self, ticket_id: str) -> Ticket:
        if ticket_id not in self.tickets:
            raise WorldError(f"ticket {ticket_id} not found")
        return self.tickets[ticket_id]

    def create_ticket(self, title: str, team: str, description: str = "") -> Ticket:
        ticket_id = f"T-{len(self.op_log) + 1000}"
        ticket = Ticket(id=ticket_id, title=title, status=TicketStatus.OPEN, team=team, description=description)
        self.tickets[ticket_id] = ticket
        self._record("create_ticket", "ticket", ticket_id, title=title, team=team)
        return ticket

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

    def reserve_inventory(self, item: str, employee_id: str) -> InventoryItem:
        if item not in self.inventory:
            raise WorldError(f"unknown inventory item {item}")
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

    def deploy_service(
        self,
        service: str,
        target_version: str,
        environment: str,
    ) -> Deployment:
        dep = self.get_deployment(service)
        if environment == "production" and dep.environment != environment:
            raise WorldError(f"{service} is not deployed in {environment}")
        dep.version = target_version
        dep.environment = environment
        dep.status = DeploymentStatus.ACTIVE
        self._record("deploy_service", "deployment", service, version=target_version, environment=environment)
        return dep

    def search_documents(self, query: str) -> list[Document]:
        q = query.lower()
        return [d for d in self.documents.values() if q in d.title.lower() or q in d.body.lower()]

    def find_employee(self, employee_id: str) -> Employee:
        if employee_id not in self.employees:
            raise WorldError(f"unknown employee {employee_id}")
        return self.employees[employee_id]
