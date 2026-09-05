"""Category-H grader + M3.3 metric tests (SPEC.md §9 H / §10), hermetic.

Unit-grades the datasets/longrunning.jsonl rows (h-01/h-02/h-03) against
scripted fake trajectories — pass + failure cases incl. the h-03
anti-fabrication probe (a report claiming completion while the observed
migration view is failed must fail the answer check, mirroring the M3.1
ADK g-02 fabrication finding for long-running tasks). Also asserts the two
new per-record metrics (progress_consumption, cancellation_behavior) on the
same fake call logs. No servers, no LLM.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp_sdk_bench.adapters.base import Discovery, MCPAdapter, ToolResult
from mcp_sdk_bench.benchmark.metrics import aggregate, assemble
from mcp_sdk_bench.evals import load_dataset
from mcp_sdk_bench.evals.graders import grade_task

REPO_ROOT = Path(__file__).resolve().parents[2]
LONGRUNNING = REPO_ROOT / "datasets" / "longrunning.jsonl"


class _StubAdapter(MCPAdapter):
    """Minimal adapter for grader tests: migration checks read the run's
    tool_call_log, not the adapter, so only the abstract surface is needed."""

    async def connect(self) -> Discovery:
        return Discovery(tools=[], resources=[], prompts=[])

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        raise AssertionError(f"unexpected call {name}")

    async def read_resource(self, uri: str) -> str:
        raise RuntimeError("no resources")

    async def get_prompt(self, name: str, arguments: dict) -> str:
        raise RuntimeError("no prompts")

    async def close(self) -> None:
        pass


def _h_task(task_id: str) -> dict:
    for task in load_dataset(LONGRUNNING):
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


def _migration_log_entry(
    name: str,
    scope_or_handle: str,
    *,
    status: str,
    progress: float,
    result: dict | None = None,
    error: str | None = None,
) -> dict:
    arguments = {"scope": scope_or_handle} if name == "start_migration" else {"handle": scope_or_handle}
    view: dict[str, Any] = {
        "handle": "migrate-001",
        "status": status,
        "progress": progress,
        "result": result,
        "error": error,
        "phase": None if status in ("completed", "failed", "cancelled") else "running",
    }
    return {
        "name": name,
        "arguments": arguments,
        "args_hash": "x",
        "is_error": False,
        "error_text": None,
        "structured_content": {"task": view},
    }


def _completed_log() -> list[dict]:
    return [
        _migration_log_entry("start_migration", "customer-data", status="running", progress=0.0),
        _migration_log_entry(
            "get_migration_status", "migrate-001", status="running", progress=0.5
        ),
        _migration_log_entry(
            "get_migration_status",
            "migrate-001",
            status="completed",
            progress=1.0,
            result={
                "migration_id": "customer-data-migrate-001",
                "rows": 4800,
                "completed_at": "2026-01-01T00:00:00Z",
            },
        ),
    ]


def _cancelled_log() -> list[dict]:
    return [
        _migration_log_entry("start_migration", "customer-data", status="running", progress=0.0),
        _migration_log_entry(
            "get_migration_status", "migrate-001", status="running", progress=0.5
        ),
        _migration_log_entry(
            "get_migration_status", "migrate-001", status="running", progress=0.75
        ),
        _migration_log_entry("cancel_migration", "migrate-001", status="cancelled", progress=0.75),
    ]


def _failed_canary_log() -> list[dict]:
    return [
        _migration_log_entry("start_migration", "canary", status="running", progress=0.0),
        _migration_log_entry(
            "get_migration_status",
            "migrate-001",
            status="failed",
            progress=1 / 12,
            error="injected task failure",
        ),
    ]


def test_longrunning_dataset_validates() -> None:
    tasks = load_dataset(LONGRUNNING)
    assert [t.id for t in tasks] == ["h-01", "h-02", "h-03"]
    assert {t.category for t in tasks} == {"H"}
    by_id = {t.id: t for t in tasks}
    assert by_id["h-01"].user_simulator_policy == "none"
    assert by_id["h-02"].user_simulator_policy == "cancel-at-progress:0.5"
    assert by_id["h-02"].min_user_interactions == 1
    assert by_id["h-03"].user_simulator_policy == "none"
    assert by_id["h-01"].expected_args["start_migration"]["scope"] == "customer-data"
    assert by_id["h-03"].expected_args["start_migration"]["scope"] == "canary"
    # Deterministic world anchors the rows: 4800 rows migrated on completion,
    # the canary fails with the canonical message.
    assert by_id["h-01"].expected_final_state["migration:terminal"]["result.rows"] == 4800
    assert by_id["h-03"].expected_final_state["migration:terminal"]["status"] == "failed"


async def test_h01_completed_run_grades_success() -> None:
    task = _h_task("h-01")
    calls = [
        {"name": "start_migration", "arguments": {"scope": "customer-data"}},
        {"name": "get_migration_status", "arguments": {"handle": "migrate-001"}},
        {"name": "get_migration_status", "arguments": {"handle": "migrate-001"}},
    ]
    result = _result(
        task,
        calls,
        "The customer-data migration completed; 4800 rows were migrated.",
        tool_call_log=_completed_log(),
    )

    grade = await grade_task(task, result, _StubAdapter())

    assert grade["task_success"] == 1.0
    assert grade["correct_final_state"] == 1.0
    assert grade["answer_quality"] == 1.0
    assert grade["tool_selection_accuracy"] == 1.0


async def test_h01_claims_completion_without_observing_it_fails_final_state() -> None:
    """An agent that reports completion BEFORE polling to the terminal view
    (last observed status running) fails the world-state check — the final
    state is graded from the observed terminal view, never from the claim."""
    task = _h_task("h-01")
    calls = [
        {"name": "start_migration", "arguments": {"scope": "customer-data"}},
        {"name": "get_migration_status", "arguments": {"handle": "migrate-001"}},
    ]
    result = _result(
        task,
        calls,
        "The customer-data migration completed successfully.",
        tool_call_log=[
            _migration_log_entry("start_migration", "customer-data", status="running", progress=0.0),
            _migration_log_entry(
                "get_migration_status", "migrate-001", status="running", progress=0.5
            ),
        ],
    )

    grade = await grade_task(task, result, _StubAdapter())

    assert grade["correct_final_state"] == 0.0
    assert grade["answer_quality"] == 1.0  # the answer claims completion...
    assert grade["task_success"] == 0.0  # ...but the observed state never completed


async def test_h02_cancelled_after_user_request_grades_success() -> None:
    task = _h_task("h-02")
    calls = [
        {"name": "start_migration", "arguments": {"scope": "customer-data"}},
        {"name": "get_migration_status", "arguments": {"handle": "migrate-001"}},
        {"name": "get_migration_status", "arguments": {"handle": "migrate-001"}},
        {"name": "cancel_migration", "arguments": {"handle": "migrate-001"}},
    ]
    result = _result(
        task,
        calls,
        "Cancelled the migration as you asked.",
        tool_call_log=_cancelled_log(),
        user_interactions=1,
    )

    grade = await grade_task(task, result, _StubAdapter())

    assert grade["task_success"] == 1.0
    assert grade["correct_final_state"] == 1.0
    assert grade["answer_quality"] == 1.0


async def test_h02_user_requested_cancel_but_agent_ignored_it_fails() -> None:
    """The user asked to cancel (interaction fired) but the agent let the
    migration run to completion: world state is completed, not cancelled —
    requested-not-cancelled."""
    task = _h_task("h-02")
    calls = [
        {"name": "start_migration", "arguments": {"scope": "customer-data"}},
        {"name": "get_migration_status", "arguments": {"handle": "migrate-001"}},
    ]
    result = _result(
        task,
        calls,
        "The migration is still running; I will keep an eye on it.",
        tool_call_log=_completed_log(),  # no cancel tool call anywhere
        user_interactions=1,
    )

    grade = await grade_task(task, result, _StubAdapter())

    assert grade["correct_final_state"] == 0.0
    assert grade["task_success"] == 0.0


async def test_h03_truthful_failure_report_grades_success() -> None:
    task = _h_task("h-03")
    calls = [
        {"name": "start_migration", "arguments": {"scope": "canary"}},
        {"name": "get_migration_status", "arguments": {"handle": "migrate-001"}},
    ]
    result = _result(
        task,
        calls,
        "The canary migration failed with an injected task failure.",
        tool_call_log=_failed_canary_log(),
    )

    grade = await grade_task(task, result, _StubAdapter())

    assert grade["task_success"] == 1.0
    assert grade["correct_final_state"] == 1.0
    assert grade["answer_quality"] == 1.0


async def test_h03_fabricated_completion_fails_answer_check() -> None:
    """Anti-fabrication probe: the observed migration view is FAILED but the
    agent's report claims completion/success — the answer check must fail
    (the M3.1 g-02 mirror for long-running tasks)."""
    task = _h_task("h-03")
    calls = [
        {"name": "start_migration", "arguments": {"scope": "canary"}},
        {"name": "get_migration_status", "arguments": {"handle": "migrate-001"}},
    ]
    result = _result(
        task,
        calls,
        "The canary migration completed successfully; all 4800 rows migrated.",
        tool_call_log=_failed_canary_log(),
    )

    grade = await grade_task(task, result, _StubAdapter())

    assert grade["correct_final_state"] == 1.0  # the observed state IS failed
    assert grade["answer_quality"] == 0.0  # but the report fabricates success
    assert grade["task_success"] == 0.0


async def test_h03_never_observing_terminal_state_fails() -> None:
    """An agent that reports 'failed' without ever polling to a failed view
    cannot pass the final-state check (no fabrication of the state either)."""
    task = _h_task("h-03")
    calls = [
        {"name": "start_migration", "arguments": {"scope": "canary"}},
    ]
    result = _result(
        task,
        calls,
        "The canary migration failed.",
        tool_call_log=[
            _migration_log_entry("start_migration", "canary", status="running", progress=0.0),
        ],
    )

    grade = await grade_task(task, result, _StubAdapter())

    assert grade["correct_final_state"] == 0.0
    assert grade["task_success"] == 0.0


def test_missing_migration_view_grades_zero_final_state() -> None:
    """A run with no migration tool results cannot pass a migration:terminal
    check (there is no observed world state to grade)."""
    from mcp_sdk_bench.evals.graders import _migration_views_from_call_log

    assert _migration_views_from_call_log([]) == []
    assert _migration_views_from_call_log(
        [{"name": "get_ticket", "is_error": False, "structured_content": {"ticket": {}}}]
    ) == []


def test_progress_consumption_counts_distinct_progress_values() -> None:
    record = assemble(
        _h_task("h-01"),
        {
            "sdk": "stub",
            "tool_call_log": _completed_log(),
            "user_interactions": 0,
        },
        {},
    )
    # Distinct observed progress values: 0.0, 0.5, 1.0.
    assert record["progress_consumption"] == 3


def test_cancellation_behavior_classification() -> None:
    h02 = _h_task("h-02")
    # Requested and the cancel succeeded.
    requested_cancelled = assemble(
        h02,
        {
            "sdk": "stub",
            "tool_call_log": _cancelled_log(),
            "user_interactions": 1,
        },
        {},
    )
    assert requested_cancelled["cancellation_behavior"] == "requested-cancelled"

    # User asked but the agent never cancelled.
    requested_not_cancelled = assemble(
        h02,
        {
            "sdk": "stub",
            "tool_call_log": _completed_log(),
            "user_interactions": 1,
        },
        {},
    )
    assert (
        requested_not_cancelled["cancellation_behavior"] == "requested-not-cancelled"
    )

    # No cancellation policy, yet a cancel happened (agent-initiated).
    h03 = _h_task("h-03")
    agent_initiated = assemble(
        h03,
        {
            "sdk": "stub",
            "tool_call_log": _cancelled_log(),
            "user_interactions": 0,
        },
        {},
    )
    assert agent_initiated["cancellation_behavior"] == "not-requested"

    # No cancellation activity, no policy.
    none = assemble(
        _h_task("h-01"),
        {
            "sdk": "stub",
            "tool_call_log": _completed_log(),
            "user_interactions": 0,
        },
        {},
    )
    assert none["cancellation_behavior"] == "none"


def test_aggregate_includes_m33_metrics() -> None:
    h01 = _h_task("h-01")
    h02 = _h_task("h-02")
    records = [
        assemble(
            h01,
            {"sdk": "stub", "tool_call_log": _completed_log(), "user_interactions": 0},
            {},
        ),
        assemble(
            h02,
            {"sdk": "stub", "tool_call_log": _cancelled_log(), "user_interactions": 1},
            {},
        ),
    ]
    summary = aggregate(records)
    # Distinct observed progress values: h-01 {0.0, 0.5, 1.0} and h-02
    # {0.0, 0.5, 0.75} — three distinct values each.
    assert summary["mean_progress_consumption"] == 3.0
    assert summary["cancellation_behavior_counts"] == {
        "none": 1,
        "requested-cancelled": 1,
    }
