"""Seed fixtures — SPEC.md §5 example state, extended for the M1 task set.

Every dataset task must be answerable from this seed. Keep IDs stable:
tests, datasets, and graders all reference them.
"""
from __future__ import annotations

from mcp_sdk_bench.world.state import (
    Deployment,
    DeploymentStatus,
    Document,
    Employee,
    InventoryItem,
    Project,
    Ticket,
    TicketStatus,
    World,
)


def seed_world() -> World:
    return World(
        employees={
            "alice": Employee(id="alice", name="Alice", team="payments", title="Staff Engineer"),
            "bob": Employee(id="bob", name="Bob", team="risk", title="Analyst"),
            "carol": Employee(id="carol", name="Carol", team="platform", title="SRE"),
        },
        tickets={
            "PAY-123": Ticket(
                id="PAY-123",
                title="Payment timeout",
                status=TicketStatus.OPEN,
                team="payments",
                assignee="alice",
                description="Checkout payments time out intermittently after the 1.8.2 release.",
            ),
            "RISK-88": Ticket(
                id="RISK-88",
                title="Fraud alert triage",
                status=TicketStatus.IN_PROGRESS,
                team="risk",
                assignee="bob",
                description="Unusual chargeback pattern on EU cards.",
            ),
            "PAY-456": Ticket(
                id="PAY-456",
                title="Gateway 502s",
                status=TicketStatus.CLOSED,
                team="payments",
                assignee="alice",
                description="Resolved by rolling back the gateway config.",
            ),
            # M2.3b: fail-03 updates PAY-124 under fault injection.
            "PAY-124": Ticket(
                id="PAY-124",
                title="Refund backlog spike",
                status=TicketStatus.OPEN,
                team="payments",
                description="Refund queue grew 3x after the 1.8.2 release; needs triage.",
            ),
        },
        documents={
            "dep-policy": Document(
                id="dep-policy",
                title="Deployment Policy",
                body=(
                    "Production deployments require two independent approvals and a written rollout plan. "
                    "Staging deploys require one approval. The checkout service is under change freeze "
                    "until the payment-timeout incident is closed."
                ),
                tags=["policy", "deployment"],
            ),
            "incident-runbook": Document(
                id="incident-runbook",
                title="Incident Triage Runbook",
                body=(
                    "1. Inspect the ticket. 2. Retrieve the relevant operational policy. "
                    "3. Identify the owning team. 4. Inspect deployment state. 5. Produce a recommendation."
                ),
                tags=["runbook", "incident"],
            ),
            "onboarding-guide": Document(
                id="onboarding-guide",
                title="Onboarding Guide",
                body=(
                    "New engineers need a laptop and an onboarding ticket assigned to their team "
                    "before their first day."
                ),
                tags=["onboarding"],
            ),
        },
        deployments={
            "checkout": Deployment(
                service="checkout", version="1.8.2", environment="production", status=DeploymentStatus.ACTIVE
            ),
            "payments-api": Deployment(
                service="payments-api", version="2.4.0", environment="staging", status=DeploymentStatus.ACTIVE
            ),
            "risk-engine": Deployment(
                service="risk-engine", version="3.1.0", environment="production", status=DeploymentStatus.ACTIVE
            ),
        },
        inventory={
            "macbook-pro": InventoryItem(name="macbook-pro", available=0),
            "thinkpad-t14": InventoryItem(name="thinkpad-t14", available=2),
            "dell-xps-13": InventoryItem(name="dell-xps-13", available=1),
            "monitor-27": InventoryItem(name="monitor-27", available=5),
        },
        projects={
            "payments-migration": Project(id="payments-migration", name="Payments Migration", team="payments"),
            "risk-scoring": Project(id="risk-scoring", name="Risk Scoring", team="risk"),
        },
    )
