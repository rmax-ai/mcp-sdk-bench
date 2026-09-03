"""Deterministic grader regression tests (SPEC.md §10/§11; M1.7).

No network, no model: a stub adapter backed by the in-memory seeded world
stands in for the real MCP servers, and runner results are synthetic.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp_sdk_bench.adapters.base import Discovery, MCPAdapter, ToolResult
from mcp_sdk_bench.evals import grade_task, load_dataset
from mcp_sdk_bench.world.fixtures import seed_world
from mcp_sdk_bench.world.state import TicketStatus, World, WorldError

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT_POLICY_URI = "company://policies/deployment"


class WorldAdapter(MCPAdapter):
    """In-memory world-backed stub adapter with the M1 server contract."""

    def __init__(self, world: World | None = None) -> None:
        self.world = world or seed_world()

    async def connect(self) -> Discovery:
        return Discovery(tools=[], resources=[], prompts=[])

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        try:
            if name == "get_ticket":
                ticket = self.world.get_ticket(arguments["ticket_id"])
                return ToolResult(
                    structured_content={"ticket": ticket.model_dump(mode="json")}
                )
            if name == "update_ticket":
                ticket = self.world.update_ticket(
                    arguments["ticket_id"],
                    status=(
                        TicketStatus(arguments["status"])
                        if "status" in arguments
                        else None
                    ),
                    assignee=arguments.get("assignee"),
                )
                return ToolResult(
                    structured_content={"ticket": ticket.model_dump(mode="json")}
                )
            if name == "get_inventory":
                return ToolResult(
                    structured_content={
                        "items": {
                            item_name: item.model_dump()
                            for item_name, item in self.world.get_inventory().items()
                        }
                    }
                )
            if name == "reserve_inventory":
                item = self.world.reserve_inventory(
                    arguments["item"], arguments["employee_id"]
                )
                return ToolResult(structured_content={"item": item.model_dump()})
        except WorldError as err:
            return ToolResult(is_error=True, text=str(err))
        return ToolResult(is_error=True, text=f"unknown tool {name}")

    async def read_resource(self, uri: str) -> str:
        if uri == DEPLOYMENT_POLICY_URI:
            return self.world.documents["dep-policy"].body
        raise RuntimeError(f"unknown resource: {uri}")

    async def get_prompt(self, name: str, arguments: dict) -> str:
        raise RuntimeError("stub adapter does not serve prompts")

    async def close(self) -> None:
        pass


def _task(task_id: str) -> dict:
    for dataset in ("basic.jsonl", "composition.jsonl"):
        for task in load_dataset(REPO_ROOT / "datasets" / dataset):
            if task.id == task_id:
                return task.model_dump()
    raise AssertionError(f"task {task_id} not found in datasets")


def _result(
    task: dict,
    tool_calls: list[dict[str, Any]],
    final_answer: str | None,
    *,
    error: str | None = None,
) -> dict:
    return {
        "task_id": task["id"],
        "sdk": "stub",
        "tool_calls": tool_calls,
        "round_trips": len(tool_calls),
        "total_latency_ms": 1.0,
        "mcp_latency_ms": 0.5,
        "final_answer": final_answer,
        "error": error,
    }


async def test_perfect_result_on_basic_001_grades_full_success() -> None:
    task = _task("basic-001")
    result = _result(
        task,
        [{"name": "get_ticket", "arguments": {"ticket_id": "PAY-123"}}],
        "PAY-123 is OPEN.",
    )

    grade = await grade_task(task, result, WorldAdapter())

    assert grade["task_id"] == "basic-001"
    assert grade["category"] == "A"
    assert grade["task_success"] == 1.0
    assert grade["correct_final_state"] == 1.0
    assert grade["tool_selection_accuracy"] == 1.0
    assert grade["tool_argument_accuracy"] == 1.0
    assert grade["trajectory_correctness"] is None
    assert grade["answer_quality"] == 1.0
    assert grade["unnecessary_tool_calls"] == 0
    assert grade["tool_call_count"] == 1
    assert "error" not in grade


async def test_wrong_tool_fails_selection_and_success() -> None:
    task = _task("basic-001")
    result = _result(
        task,
        [{"name": "get_inventory", "arguments": {}}],
        "PAY-123 is OPEN.",
    )

    grade = await grade_task(task, result, WorldAdapter())

    assert grade["tool_selection_accuracy"] == 0.0
    assert grade["unnecessary_tool_calls"] == 1
    assert grade["task_success"] == 0.0


async def test_right_tool_wrong_arguments_fails_argument_accuracy() -> None:
    task = _task("basic-001")
    result = _result(
        task,
        [{"name": "get_ticket", "arguments": {"ticket_id": "PAY-456"}}],
        "PAY-123 is OPEN.",
    )

    grade = await grade_task(task, result, WorldAdapter())

    assert grade["tool_selection_accuracy"] == 1.0
    assert grade["tool_argument_accuracy"] == 0.0


async def test_final_state_check_catches_unmutated_state() -> None:
    task = _task("basic-006")
    # The agent read the ticket but never called update_ticket.
    result = _result(
        task,
        [{"name": "get_ticket", "arguments": {"ticket_id": "PAY-123"}}],
        "PAY-123 is CLOSED.",
    )

    grade = await grade_task(task, result, WorldAdapter())

    assert grade["correct_final_state"] == 0.0
    assert grade["task_success"] == 0.0


async def test_trajectory_violation_is_independent_of_task_success() -> None:
    task = _task("basic-006")
    adapter = WorldAdapter()
    # Simulate the mutations the (mis-ordered) run actually performed.
    await adapter.call_tool(
        "update_ticket", {"ticket_id": "PAY-123", "status": "CLOSED"}
    )
    result = _result(
        task,
        [
            {"name": "update_ticket", "arguments": {"ticket_id": "PAY-123", "status": "CLOSED"}},
            {"name": "get_ticket", "arguments": {"ticket_id": "PAY-123"}},
        ],
        "PAY-123 is now CLOSED.",
    )

    grade = await grade_task(task, result, adapter)

    # Outcome is right: state mutated, answer fine, tool set exact.
    assert grade["task_success"] == 1.0
    # Trajectory is wrong: update before read (SPEC.md §11 separation).
    assert grade["trajectory_correctness"] == 0.0


async def test_missing_required_answer_substring_fails_answer_quality() -> None:
    task = _task("basic-001")
    result = _result(
        task,
        [{"name": "get_ticket", "arguments": {"ticket_id": "PAY-123"}}],
        "I could not determine the status.",
    )

    grade = await grade_task(task, result, WorldAdapter())

    assert grade["answer_quality"] == 0.0
    assert grade["task_success"] == 0.0


async def test_forbidden_contains_violation_on_basic_003() -> None:
    task = _task("basic-003")
    result = _result(
        task,
        [{"name": "get_inventory", "arguments": {}}],
        "Yes, thinkpad-t14 is available, and macbook-pro is available too.",
    )

    grade = await grade_task(task, result, WorldAdapter())

    assert grade["answer_quality"] == 0.0
    assert grade["task_success"] == 0.0


async def test_resource_read_pseudo_tool_grades_successfully() -> None:
    task = _task("comp-003")
    result = _result(
        task,
        [{"name": "read_resource", "arguments": {"uri": DEPLOYMENT_POLICY_URI}}],
        "No: checkout is under a change freeze until the incident is closed.",
    )

    grade = await grade_task(task, result, WorldAdapter())

    assert grade["task_success"] == 1.0
    assert grade["tool_selection_accuracy"] == 1.0
    assert grade["tool_argument_accuracy"] == 1.0


async def test_malformed_result_grades_zero_without_raising() -> None:
    task = _task("basic-001")

    grade = await grade_task(task, {"task_id": "basic-001"}, WorldAdapter())

    assert grade["task_success"] == 0.0
    assert grade["answer_quality"] == 0.0
    assert grade["tool_call_count"] == 0


async def test_all_dataset_rows_load_and_validate() -> None:
    basic = load_dataset(REPO_ROOT / "datasets" / "basic.jsonl")
    composition = load_dataset(REPO_ROOT / "datasets" / "composition.jsonl")

    assert len(basic) == 7
    assert len(composition) == 4
    assert len(basic) + len(composition) == 11
