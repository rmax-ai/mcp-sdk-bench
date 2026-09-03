"""World determinism + mutation-recording tests."""
from mcp_sdk_bench.world import TicketStatus, WorldError, reset_world, seed_world


def test_reset_is_fresh_copy() -> None:
    a = reset_world()
    b = reset_world()
    assert a == b
    a.tickets["PAY-123"].status = TicketStatus.CLOSED
    assert b.tickets["PAY-123"].status == TicketStatus.OPEN  # no shared mutable state


def test_fixture_integrity() -> None:
    w = seed_world()
    assert w.tickets["PAY-123"].title == "Payment timeout"
    assert w.tickets["PAY-123"].status == TicketStatus.OPEN
    assert w.deployments["checkout"].version == "1.8.2"
    assert w.deployments["checkout"].environment == "production"
    assert w.inventory["macbook-pro"].available == 0
    assert w.employees["alice"].team == "payments"


def test_create_ticket_records_op() -> None:
    w = reset_world()
    t = w.create_ticket(title="Onboard new engineer", team="payments")
    assert t.status == TicketStatus.OPEN
    ops = [o for o in w.op_log if o.op == "create_ticket"]
    assert len(ops) == 1
    assert ops[0].entity_id == t.id
    assert ops[0].seq == len(w.op_log) - 1


def test_reserve_inventory_mutates_and_records() -> None:
    w = reset_world()
    inv = w.reserve_inventory("thinkpad-t14", "alice")
    assert inv.available == 1
    assert inv.reserved_by == ["alice"]
    assert w.op_log[-1].op == "reserve_inventory"


def test_reserve_unknown_or_empty_fails() -> None:
    w = reset_world()
    try:
        w.reserve_inventory("macbook-pro", "alice")
        raise AssertionError("expected WorldError")
    except WorldError:
        pass
    try:
        w.reserve_inventory("hoverboard", "alice")
        raise AssertionError("expected WorldError")
    except WorldError:
        pass


def test_search_documents() -> None:
    w = reset_world()
    hits = w.search_documents("deployment policy")
    assert any(d.id == "dep-policy" for d in hits)
    assert w.search_documents("zzz-not-there") == []


def test_deploy_service_updates_version() -> None:
    w = reset_world()
    dep = w.deploy_service("payments-api", "2.5.0", "staging")
    assert dep.version == "2.5.0"
    assert w.op_log[-1].op == "deploy_service"
