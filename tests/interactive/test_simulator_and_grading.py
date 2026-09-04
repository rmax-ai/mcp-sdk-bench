"""User simulator policies + M3.1 grader checks (SPEC.md §10/§18), hermetic."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mcp_sdk_bench.adapters.base import Discovery, MCPAdapter, ToolResult
from mcp_sdk_bench.agent.simulator import (
    ScriptedUserSimulator,
    normalize_simulator_answer,
)
from mcp_sdk_bench.evals import grade_task, load_dataset
from mcp_sdk_bench.world.fixtures import seed_world
from mcp_sdk_bench.world.state import WorldError

REPO_ROOT = Path(__file__).resolve().parents[2]
INTERACTIVE = REPO_ROOT / "datasets" / "interactive.jsonl"


# ---- simulator policies ----


async def test_policy_none_never_clarifies_and_declines_elicitations() -> None:
    sim = ScriptedUserSimulator()  # default is "none"
    assert await sim.clarify("Deploy checkout.") is None
    assert await sim.answer("approval", "Approve?", {}) == {"status": "declined"}
    assert await sim.answer("clarification", "Who?", {}) == {"status": "declined"}


async def test_auto_approve_approves_approvals() -> None:
    sim = ScriptedUserSimulator("auto-approve")
    assert await sim.answer("approval", "Approve?", {}) == {"status": "approved"}
    assert await sim.clarify("anything") is None


async def test_auto_decline_declines_approvals() -> None:
    sim = ScriptedUserSimulator("auto-decline")
    assert await sim.answer("approval", "Approve?", {}) == {"status": "declined"}


async def test_clarify_with_answers_clarifications_and_clarify_hook() -> None:
    sim = ScriptedUserSimulator("clarify-with:alina")
    assert await sim.clarify("Reserve a laptop.") == "alina"
    assert await sim.answer("clarification", "Who?", {}) == {
        "status": "clarified",
        "answer": "alina",
    }
    # A cooperative user still approves approvals.
    assert await sim.answer("approval", "Approve?", {}) == {"status": "approved"}


def test_unknown_policy_fails_loud() -> None:
    with pytest.raises(ValueError, match="unknown user_simulator_policy"):
        ScriptedUserSimulator("yolo")


def test_normalize_simulator_answer() -> None:
    approval_request = {"kind": "approval", "question": "Approve?", "schema": {}}
    assert normalize_simulator_answer(approval_request, "yes") == {"status": "approved"}
    assert normalize_simulator_answer(approval_request, "no") == {"status": "declined"}
    clarification_request = {"kind": "clarification", "question": "Who?", "schema": {}}
    assert normalize_simulator_answer(clarification_request, "alina") == {
        "status": "clarified",
        "answer": "alina",
    }
    payload = {"status": "declined"}
    assert normalize_simulator_answer(approval_request, payload) is payload


# ---- grader: deployment kind + min_user_interactions ----


class _StubAdapter(MCPAdapter):
    """Minimal adapter for grader tests: deployment checks read the run's
    tool_call_log, not the adapter, so only the abstract surface is needed."""

    async def connect(self) -> Discovery:
        return Discovery(tools=[], resources=[], prompts=[])

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        raise WorldError(f"unexpected call {name}")

    async def read_resource(self, uri: str) -> str:
        raise RuntimeError("no resources")

    async def get_prompt(self, name: str, arguments: dict) -> str:
        raise RuntimeError("no prompts")

    async def close(self) -> None:
        pass


def _interactive_task(task_id: str) -> dict:
    for task in load_dataset(INTERACTIVE):
        if task.id == task_id:
            return task.model_dump()
    raise AssertionError(f"task {task_id} not found")


def _result(
    task: dict,
    tool_calls: list[dict[str, Any]],
    final_answer: str,
    *,
    tool_call_log: list[dict] | None = None,
    user_interactions: int = 0,
) -> dict:
    return {
        "task_id": task["id"],
        "sdk": "stub",
        "tool_calls": tool_calls,
        "tool_call_log": tool_call_log or [],
        "round_trips": len(tool_calls),
        "mcp_round_trips": len(tool_calls),
        "user_interactions": user_interactions,
        "total_latency_ms": 1.0,
        "mcp_latency_ms": 0.5,
        "final_answer": final_answer,
        "error": None,
    }


def _deploy_log_entry(service: str, version: str, environment: str) -> dict:
    return {
        "name": "deploy_service",
        "arguments": {
            "service": service,
            "target_version": version,
            "environment": environment,
        },
        "args_hash": "x",
        "is_error": False,
        "error_text": None,
        "structured_content": {
            "deployment": {
                "service": service,
                "version": version,
                "environment": environment,
                "status": "active",
            }
        },
    }


async def test_g01_deploy_via_call_log_and_interaction_grades_success() -> None:
    task = _interactive_task("g-01")
    calls = [
        {
            "name": "deploy_service",
            "arguments": {
                "service": "checkout",
                "target_version": "v1.8.3",
                "environment": "production",
            },
        }
    ]
    result = _result(
        task,
        calls,
        "Deployed checkout v1.8.3 to production after approval.",
        tool_call_log=[_deploy_log_entry("checkout", "v1.8.3", "production")],
        user_interactions=1,
    )

    grade = await grade_task(task, result, _StubAdapter())

    assert grade["task_success"] == 1.0
    assert grade["correct_final_state"] == 1.0
    assert grade["user_interactions"] == 1


async def test_g02_decline_leaves_seeded_state_and_grades_success() -> None:
    """Declined deploy: NO successful deploy in the call log, so the seeded
    production deployment (1.8.2) is the ground truth being asserted."""
    task = _interactive_task("g-02")
    calls = [
        {
            "name": "deploy_service",
            "arguments": {
                "service": "checkout",
                "target_version": "v1.8.3",
                "environment": "production",
            },
        }
    ]
    declined_log = [
        {
            "name": "deploy_service",
            "arguments": calls[0]["arguments"],
            "args_hash": "x",
            "is_error": True,
            "error_text": "deployment declined by user",
            "structured_content": None,
        }
    ]
    result = _result(
        task,
        calls,
        "The deployment declined by user decision; checkout stays on 1.8.2.",
        tool_call_log=declined_log,
        user_interactions=1,
    )

    grade = await grade_task(task, result, _StubAdapter())

    assert grade["task_success"] == 1.0
    assert grade["correct_final_state"] == 1.0


async def test_g02_answer_claiming_approval_fails_forbidden_contains() -> None:
    task = _interactive_task("g-02")
    result = _result(
        task,
        [],
        "Deployment approved and rolled out.",
        user_interactions=1,
    )

    grade = await grade_task(task, result, _StubAdapter())

    assert grade["answer_quality"] == 0.0
    assert grade["task_success"] == 0.0


async def test_min_user_interactions_enforced() -> None:
    """A run that completed the state change WITHOUT the scripted user (no
    elicitation leg) must fail the interaction requirement."""
    task = _interactive_task("g-01")
    calls = [
        {
            "name": "deploy_service",
            "arguments": {
                "service": "checkout",
                "target_version": "v1.8.3",
                "environment": "production",
            },
        }
    ]
    result = _result(
        task,
        calls,
        "Deployed checkout v1.8.3 to production.",
        tool_call_log=[_deploy_log_entry("checkout", "v1.8.3", "production")],
        user_interactions=0,  # no approval actually happened
    )

    grade = await grade_task(task, result, _StubAdapter())

    assert grade["correct_final_state"] == 1.0
    assert grade["task_success"] == 0.0
    assert grade["user_interactions"] == 0


async def test_paused_call_log_leg_does_not_count_as_deploy_result() -> None:
    """The paused leg (elicitation_request set, no structured content) must
    not shadow the resumed leg's deployment in the final-state check."""
    task = _interactive_task("g-01")
    calls = [
        {
            "name": "deploy_service",
            "arguments": {
                "service": "checkout",
                "target_version": "v1.8.3",
                "environment": "production",
            },
        }
    ]
    paused_leg = {
        "name": "deploy_service",
        "arguments": calls[0]["arguments"],
        "args_hash": "x",
        "is_error": False,
        "error_text": None,
        "structured_content": None,
        "elicitation_request": {"kind": "approval", "question": "Approve?", "schema": {}},
    }
    # Pathological order-independent check: paused leg LAST must not win.
    log = [_deploy_log_entry("checkout", "v1.8.3", "production"), paused_leg]
    result = _result(
        task,
        calls,
        "Deployed checkout v1.8.3 to production after approval.",
        tool_call_log=log,
        user_interactions=1,
    )

    grade = await grade_task(task, result, _StubAdapter())

    assert grade["correct_final_state"] == 1.0
    assert grade["task_success"] == 1.0


def test_interactive_dataset_validates() -> None:
    tasks = load_dataset(INTERACTIVE)
    assert [t.id for t in tasks] == ["f-01", "f-02", "f-03", "g-01", "g-02", "g-03"]
    assert {t.category for t in tasks} == {"F", "G"}
    by_id = {t.id: t for t in tasks}
    assert by_id["f-01"].user_simulator_policy == "clarify-with:staging v1.7.0"
    assert by_id["g-01"].user_simulator_policy == "auto-approve"
    assert by_id["g-02"].user_simulator_policy == "auto-decline"
    assert by_id["g-03"].user_simulator_policy == "clarify-with:alina"
    assert by_id["f-02"].min_user_interactions == 0
    assert all(
        by_id[tid].min_user_interactions == 1 for tid in ("f-01", "f-03", "g-01", "g-02", "g-03")
    )
    # The seeded world makes f-02's "unchanged" assertions meaningful.
    world = seed_world()
    checkout = world.deployments["checkout"]
    assert checkout.version == "1.8.2" and checkout.environment == "production"
