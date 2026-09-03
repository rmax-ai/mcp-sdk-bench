"""Deterministic task graders (SPEC.md §10/§11).

No LLM judging anywhere in this module: tool selection, arguments, final
state, and answer checks are all exact, reproducible comparisons. Trajectory
correctness is reported independently of task_success (§11 outcome vs
trajectory separation). grade_task never raises — a malformed result grades
0 and carries an "error" field, so one bad result cannot kill a sweep.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from mcp_sdk_bench.adapters.base import MCPAdapter

# Resource/prompt access is recorded in the trace as pseudo tool calls
# (see benchmark.runner.AccessRecordingAdapter). They are excluded from the
# tool-selection comparison but their arguments are still graded.
PSEUDO_TOOLS = frozenset({"read_resource", "get_prompt"})


def _used_names(result: dict) -> list[str]:
    calls = result.get("tool_calls") or []
    return [str(call.get("name")) for call in calls]


def _tool_selection(task: dict, used_names: list[str]) -> tuple[float, int]:
    expected = {n for n in task["expected_tools"] if n not in PSEUDO_TOOLS}
    used = {n for n in used_names if n not in PSEUDO_TOOLS}
    unnecessary = sum(
        1 for n in used_names if n not in PSEUDO_TOOLS and n not in expected
    )
    return (1.0 if used == expected else 0.0), unnecessary


def _argument_accuracy(task: dict, result: dict) -> float:
    calls = result.get("tool_calls") or []
    for name, expected_args in task["expected_args"].items():
        matched = any(
            call.get("name") == name
            and all(
                (call.get("arguments") or {}).get(arg) == value
                for arg, value in expected_args.items()
            )
            for call in calls
        )
        if not matched:
            return 0.0
    return 1.0


def _trajectory_correctness(task: dict, used_names: list[str]) -> float | None:
    expected_trajectory = task.get("expected_trajectory")
    if expected_trajectory is None:
        return None
    expected_names = set(task["expected_tools"])
    filtered = [n for n in used_names if n in expected_names]
    return 1.0 if filtered == expected_trajectory else 0.0


def _plain(value: Any) -> Any:
    """Normalize enum statuses to their string value before comparing."""
    return value.value if isinstance(value, Enum) else value


async def _correct_final_state(task: dict, adapter: MCPAdapter) -> float:
    for check_id, fields in task["expected_final_state"].items():
        kind, _, key = check_id.partition(":")
        try:
            if kind == "ticket":
                res = await adapter.call_tool("get_ticket", {"ticket_id": key})
                if res.is_error or res.structured_content is None:
                    return 0.0
                entity = res.structured_content.get("ticket")
            elif kind == "inventory":
                res = await adapter.call_tool("get_inventory", {})
                if res.is_error or res.structured_content is None:
                    return 0.0
                items = res.structured_content.get("items") or {}
                entity = items.get(key)
            else:
                return 0.0
        except Exception:  # noqa: BLE001 — any adapter error fails the check, never the grader
            return 0.0
        if not isinstance(entity, dict):
            return 0.0
        for field, expected_value in fields.items():
            if _plain(entity.get(field)) != expected_value:
                return 0.0
    return 1.0


def _answer_quality(task: dict, result: dict) -> float:
    answer = result.get("final_answer")
    required = task["answer_contains"]
    forbidden = task.get("forbidden_contains", [])
    if not isinstance(answer, str):
        return 1.0 if not required and not forbidden else 0.0
    low = answer.lower()
    if any(substr.lower() not in low for substr in required):
        return 0.0
    if any(substr.lower() in low for substr in forbidden):
        return 0.0
    return 1.0


async def grade_task(task: dict, result: dict, adapter: MCPAdapter) -> dict:
    """Grade one task result deterministically. Never raises."""
    base = {
        "task_id": task.get("id", "unknown"),
        "category": task.get("category", "?"),
    }
    try:
        used_names = _used_names(result)
        tool_selection, unnecessary = _tool_selection(task, used_names)
        argument_accuracy = _argument_accuracy(task, result)
        trajectory = _trajectory_correctness(task, used_names)
        final_state = await _correct_final_state(task, adapter)
        answer_quality = _answer_quality(task, result)
        task_success = (
            1.0
            if final_state == 1.0 and answer_quality == 1.0 and tool_selection == 1.0
            else 0.0
        )
        return {
            **base,
            "task_success": task_success,
            "correct_final_state": final_state,
            "tool_selection_accuracy": tool_selection,
            "tool_argument_accuracy": argument_accuracy,
            "trajectory_correctness": trajectory,
            "answer_quality": answer_quality,
            "unnecessary_tool_calls": unnecessary,
            "tool_call_count": len(result.get("tool_calls") or []),
            "round_trips": int(result.get("round_trips") or 0),
        }
    except Exception as err:  # noqa: BLE001 — grading must be total; a malformed result is data
        return {
            **base,
            "task_success": 0.0,
            "correct_final_state": 0.0,
            "tool_selection_accuracy": 0.0,
            "tool_argument_accuracy": 0.0,
            "trajectory_correctness": None,
            "answer_quality": 0.0,
            "unnecessary_tool_calls": 0,
            "tool_call_count": 0,
            "round_trips": 0,
            "error": f"{type(err).__name__}: {err}",
        }
