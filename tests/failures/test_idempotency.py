"""The SPEC.md §21 idempotency experiment (M2.3a) — deterministic, agent-free.

"An agent retrying a failed MCP call must not accidentally create two
tickets." The verdict mechanism: after a fail-AFTER fault (side effect
applied, error returned), a retry with the same idempotency_key must return
the EXISTING ticket and the world must hold exactly ONE ticket.

Part (a) is world-level: create_ticket idempotency semantics on a real World,
plus the shared fault dispatch (run_tool_with_faults) that all three server
variants execute.

Part (b) runs the fail-after retry simulation per candidate over real client
surfaces. Idempotent replays bypass the fault layer by design (a replay
executes no transaction, so no transaction fault applies — see
mcp_sdk_bench.faults.run_tool_with_faults), which is what lets the retry
converge even at FAIL_TOOL_CALL=1.0.

Error SHAPE per candidate — whether the client can distinguish an
after-failure (call executed, then reported failed) from a transport failure
(feeds the per-SDK idempotency verdict in M2.3b):
- official: after-failure is an isError=True CallToolResult VALUE carrying
  "injected fault after execution"; transport failure is a RAISED MCPError.
  Fully distinguishable by shape.
- fastmcp: with raise_on_error=False, same isError result value; with the
  client default (raise_on_error=True) it is a RAISED ToolError, while a
  transport failure raises MCPError — distinguishable by exception type.
- adk: the adapter maps EVERY candidate-side failure to ToolResult(
  is_error=True) — including transport failures — so shape alone does NOT
  distinguish the two; only the message text does. Recorded as an adapter
  finding, not emulated away (SPEC.md §7).
"""
from __future__ import annotations

from typing import Any

import pytest
from helpers import (  # ty: ignore[unresolved-import] — tests/conformance/helpers.py, on sys.path via conftest.py
    ADK_ADAPTER_SESSION,
    FAST_MCP_SESSION_FAULTY,
    OFFICIAL_SESSION_FAULTY,
    sdk_defect,
)

from mcp_sdk_bench.faults import (
    INJECTED_FAULT_AFTER,
    FaultEngine,
    InjectedToolFault,
    load_fault_config,
    run_tool_with_faults,
)
from mcp_sdk_bench.world import Ticket, WorldError, reset_world

FAIL_AFTER_ENV = {"FAIL_TOOL_CALL": "1.0", "FAIL_PHASE": "after", "FAULT_SEED": "42"}

CREATE_ARGS = {"ticket_id": "T-1", "title": "Onboard new engineer", "idempotency_key": "K-1"}
SEEDED_TICKETS = 4  # fixtures.py: PAY-123, RISK-88, PAY-456, PAY-124 (M2.3b)


def _text(result: Any) -> str:
    content = getattr(result, "content", None) or []
    return "\n".join(
        block.text for block in content if getattr(block, "type", None) == "text"
    )


# ---- (a) world-level idempotency semantics ----


def test_create_ticket_replay_returns_existing_ticket() -> None:
    world = reset_world()
    created = world.create_ticket("T-1", "Onboard new engineer", idempotency_key="K-1")
    assert created.id == "T-1"
    assert len(world.tickets) == SEEDED_TICKETS + 1

    replay = world.create_ticket("T-1", "Onboard new engineer", idempotency_key="K-1")
    assert replay is created  # EXISTING ticket, unchanged
    assert len(world.tickets) == SEEDED_TICKETS + 1

    # Same key with a DIFFERENT ticket_id still returns the existing ticket;
    # no second ticket is created.
    other = world.create_ticket("T-2", "Duplicate attempt", idempotency_key="K-1")
    assert other.id == "T-1"
    assert "T-2" not in world.tickets
    assert len(world.tickets) == SEEDED_TICKETS + 1

    # Exactly one creation was recorded (replays are not transactions).
    ops = [o for o in world.op_log if o.op == "create_ticket"]
    assert len(ops) == 1
    assert ops[0].entity_id == "T-1"


def test_create_ticket_new_key_duplicate_id_rejected() -> None:
    world = reset_world()
    world.create_ticket("T-1", "Onboard new engineer", idempotency_key="K-1")
    with pytest.raises(WorldError, match="already exists"):
        world.create_ticket("T-1", "Other title", idempotency_key="K-2")
    assert len(world.tickets) == SEEDED_TICKETS + 1


def test_ticket_for_idempotency_key_reports_execution() -> None:
    world = reset_world()
    assert world.ticket_for_idempotency_key("K-1") is None
    world.create_ticket("T-1", "Onboard new engineer", idempotency_key="K-1")
    ticket = world.ticket_for_idempotency_key("K-1")
    assert ticket is not None and ticket.id == "T-1"


async def test_fault_dispatch_fail_after_then_replay_keeps_one_ticket() -> None:
    """The shared dispatch layer (what every server variant runs): fail-after
    applies the side effect and reports failure; the replay bypasses the
    fault layer and returns the existing ticket; exactly one ticket exists."""
    world = reset_world()
    engine = FaultEngine(load_fault_config(FAIL_AFTER_ENV))
    # Thunks mirror the per-tool closures the server variants build.
    def execute() -> Ticket:
        return world.create_ticket("T-1", "Onboard new engineer", idempotency_key="K-1")

    def is_replay() -> bool:
        return world.ticket_for_idempotency_key("K-1") is not None

    with pytest.raises(InjectedToolFault, match=INJECTED_FAULT_AFTER):
        await run_tool_with_faults(engine, execute, is_replay=is_replay)
    # Side effect WAS applied despite the reported failure.
    assert world.ticket_for_idempotency_key("K-1") is not None
    assert len(world.tickets) == SEEDED_TICKETS + 1

    # The "agent retry": replay returns the existing ticket, no duplicate.
    retried = await run_tool_with_faults(engine, execute, is_replay=is_replay)
    assert retried.id == "T-1"
    assert len(world.tickets) == SEEDED_TICKETS + 1
    ops = [o for o in world.op_log if o.op == "create_ticket"]
    assert len(ops) == 1


# ---- (b) per-candidate fail-after retry simulation ----


async def test_official_fail_after_retry_creates_exactly_one_ticket() -> None:
    """official: error SHAPE is an isError=True result value (a transport
    failure would raise MCPError instead) — distinguishable."""
    candidate = "official"
    async with OFFICIAL_SESSION_FAULTY(fault_env=FAIL_AFTER_ENV) as session:
        failed = await session.call_tool("create_ticket", CREATE_ARGS)
        assert failed.is_error, sdk_defect(candidate, "fail-after create did not error")
        assert INJECTED_FAULT_AFTER in _text(failed), sdk_defect(
            candidate, f"unexpected fail-after message: {_text(failed)!r}"
        )

        # The retry: same idempotency_key returns the existing ticket.
        retried = await session.call_tool("create_ticket", CREATE_ARGS)
        assert not retried.is_error, sdk_defect(candidate, "idempotent retry errored")
        assert retried.structured_content is not None
        ticket = retried.structured_content["ticket"]
        assert ticket["id"] == "T-1"
        assert ticket["idempotency_key"] == "K-1"

        # A retry with the same key but a different id returns the existing
        # ticket instead of creating a second one.
        other = await session.call_tool(
            "create_ticket", {**CREATE_ARGS, "ticket_id": "T-2"}
        )
        assert not other.is_error
        assert other.structured_content is not None
        assert other.structured_content["ticket"]["id"] == "T-1"


async def test_fastmcp_fail_after_retry_creates_exactly_one_ticket() -> None:
    """fastmcp: error SHAPE is an isError=True result value under
    raise_on_error=False (default True raises ToolError; a transport failure
    raises MCPError) — distinguishable."""
    candidate = "fastmcp"
    async with FAST_MCP_SESSION_FAULTY(fault_env=FAIL_AFTER_ENV) as client:
        failed = await client.call_tool("create_ticket", CREATE_ARGS, raise_on_error=False)
        assert failed.is_error, sdk_defect(candidate, "fail-after create did not error")
        assert INJECTED_FAULT_AFTER in _text(failed), sdk_defect(
            candidate, f"unexpected fail-after message: {_text(failed)!r}"
        )

        retried = await client.call_tool("create_ticket", CREATE_ARGS, raise_on_error=False)
        assert not retried.is_error, sdk_defect(candidate, "idempotent retry errored")
        assert retried.structured_content is not None
        ticket = retried.structured_content["ticket"]
        assert ticket["id"] == "T-1"
        assert ticket["idempotency_key"] == "K-1"

        other = await client.call_tool(
            "create_ticket", {**CREATE_ARGS, "ticket_id": "T-2"}, raise_on_error=False
        )
        assert not other.is_error
        assert other.structured_content is not None
        assert other.structured_content["ticket"]["id"] == "T-1"


async def test_adk_fail_after_retry_creates_exactly_one_ticket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """adk: error SHAPE is ToolResult(is_error=True) for BOTH the after-
    failure and transport failures (the adapter never raises) — the two are
    distinguishable only by message text. Adapter finding, recorded."""
    candidate = "adk"
    for key, value in FAIL_AFTER_ENV.items():
        monkeypatch.setenv(key, value)
    async with ADK_ADAPTER_SESSION() as (adapter, _):
        failed = await adapter.call_tool("create_ticket", CREATE_ARGS)
        assert failed.is_error, sdk_defect(candidate, "fail-after create did not error")
        assert failed.text is not None and INJECTED_FAULT_AFTER in failed.text, sdk_defect(
            candidate, f"unexpected fail-after message: {failed.text!r}"
        )

        retried = await adapter.call_tool("create_ticket", CREATE_ARGS)
        assert not retried.is_error, sdk_defect(candidate, "idempotent retry errored")
        assert retried.structured_content is not None
        ticket = retried.structured_content["ticket"]
        assert ticket["id"] == "T-1"
        assert ticket["idempotency_key"] == "K-1"

        other = await adapter.call_tool(
            "create_ticket", {**CREATE_ARGS, "ticket_id": "T-2"}
        )
        assert not other.is_error
        assert other.structured_content is not None
        assert other.structured_content["ticket"]["id"] == "T-1"
