"""World elicitation seam unit tests (SPEC.md §18, M3.1).

Hermetic: the world methods are driven with in-memory elicit fns — no
server, no model, no network. The seam contract: the world owns the
clarification/approval POLICY; the elicit fn (server-owned protocol
mechanics) is exercised here with fakes.
"""
from __future__ import annotations

import pytest

from mcp_sdk_bench.adapters.base import infer_elicitation_kind
from mcp_sdk_bench.world import (
    DEPLOYMENT_DECLINED,
    ElicitationUnavailable,
    WorldError,
    approval_payload,
    clarification_payload,
    elicitation_response,
    reset_world,
)

# ---- clarification flow (reserve_inventory) ----


async def test_reserve_without_employee_clarifies_then_reserves() -> None:
    w = reset_world()
    seen: list[dict] = []

    async def elicit(payload: dict) -> dict:
        seen.append(payload)
        return {"status": "clarified", "answer": "alina"}

    inv = await w.reserve_inventory("thinkpad-t14", None, elicit=elicit)

    assert inv.available == 1
    assert inv.reserved_by == ["alina"]
    assert len(seen) == 1
    assert seen[0]["kind"] == "clarification"
    assert seen[0]["schema"]["required"] == ["employee_id"]
    assert "thinkpad-t14" in seen[0]["question"]


async def test_reserve_clarification_declined_raises_and_does_not_mutate() -> None:
    w = reset_world()

    async def elicit(payload: dict) -> dict:
        return {"status": "declined"}

    with pytest.raises(WorldError, match="reservation declined by user"):
        await w.reserve_inventory("thinkpad-t14", None, elicit=elicit)
    assert w.inventory["thinkpad-t14"].available == 2
    assert w.op_log == []


async def test_reserve_without_callback_keeps_legacy_error() -> None:
    w = reset_world()
    with pytest.raises(WorldError, match="requires an employee id"):
        await w.reserve_inventory("thinkpad-t14")


async def test_reserve_falls_back_to_legacy_error_when_channel_unavailable() -> None:
    """A client without the elicitation capability gets the pre-M3.1
    behavior, not a faked answer (honest degradation)."""
    w = reset_world()

    async def elicit(payload: dict) -> dict:
        raise ElicitationUnavailable("Elicitation not supported")

    with pytest.raises(WorldError, match="requires an employee id"):
        await w.reserve_inventory("thinkpad-t14", None, elicit=elicit)


async def test_reserve_with_employee_never_elicits() -> None:
    w = reset_world()

    async def elicit(payload: dict) -> dict:
        raise AssertionError("elicit must not fire when employee_id is given")

    inv = await w.reserve_inventory("thinkpad-t14", "alice", elicit=elicit)
    assert inv.reserved_by == ["alice"]


# ---- approval flow (deploy_service) ----


async def test_production_deploy_approved_proceeds() -> None:
    """Approval REPLACES the legacy guard: payments-api is staging-only, so
    pre-M3.1 this deploy was rejected; with an approved elicitation it
    proceeds."""
    w = reset_world()
    seen: list[dict] = []

    async def elicit(payload: dict) -> dict:
        seen.append(payload)
        return {"status": "approved"}

    dep = await w.deploy_service("payments-api", "2.5.0", "production", elicit=elicit)

    assert dep.environment == "production"
    assert dep.version == "2.5.0"
    assert len(seen) == 1
    assert seen[0]["kind"] == "approval"
    assert "payments-api" in seen[0]["question"]


async def test_production_deploy_declined_raises_and_leaves_world_unchanged() -> None:
    w = reset_world()

    async def elicit(payload: dict) -> dict:
        return {"status": "declined"}

    with pytest.raises(WorldError, match=DEPLOYMENT_DECLINED):
        await w.deploy_service("checkout", "v1.8.3", "production", elicit=elicit)
    assert w.deployments["checkout"].version == "1.8.2"
    assert w.op_log == []


async def test_production_deploy_without_callback_keeps_legacy_guard() -> None:
    w = reset_world()
    with pytest.raises(WorldError, match="not deployed in production"):
        await w.deploy_service("payments-api", "2.5.0", "production")


async def test_production_deploy_falls_back_to_legacy_guard_when_channel_unavailable() -> None:
    w = reset_world()

    async def elicit(payload: dict) -> dict:
        raise ElicitationUnavailable("Elicitation not supported")

    with pytest.raises(WorldError, match="not deployed in production"):
        await w.deploy_service("payments-api", "2.5.0", "production", elicit=elicit)


async def test_staging_deploy_never_elicits() -> None:
    w = reset_world()

    async def elicit(payload: dict) -> dict:
        raise AssertionError("elicit must not fire for staging deploys")

    dep = await w.deploy_service("checkout", "v1.7.0", "staging", elicit=elicit)
    assert dep.environment == "staging"


# ---- payload helpers ----


def test_kind_inference_survives_title_stripping() -> None:
    """The wire requestedSchema subset ignores root `title`; kind must still
    be recoverable from the schema shape."""
    for payload, kind in (
        (approval_payload("approve?"), "approval"),
        (clarification_payload("employee_id", "who?"), "clarification"),
    ):
        assert infer_elicitation_kind(payload["schema"]) == kind
        stripped = {k: v for k, v in payload["schema"].items() if k != "title"}
        assert infer_elicitation_kind(stripped) == kind


def test_elicitation_response_normalization() -> None:
    approval = approval_payload("approve?")
    assert elicitation_response("accept", {"approved": True}, approval) == {"status": "approved"}
    assert elicitation_response("accept", {"approved": False}, approval) == {"status": "declined"}
    assert elicitation_response("decline", None, approval) == {"status": "declined"}
    assert elicitation_response("cancel", None, approval) == {"status": "declined"}

    clarification = clarification_payload("employee_id", "who?")
    assert elicitation_response("accept", {"employee_id": "alina"}, clarification) == {
        "status": "clarified",
        "answer": "alina",
    }
    assert elicitation_response("decline", None, clarification) == {"status": "declined"}
