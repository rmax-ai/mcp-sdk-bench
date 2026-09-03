"""Per-task metric assembly (SPEC.md §10).

Joins the runner result with the deterministic grader verdict into the full
metric record. Fields that M2–M5 experiments fill (error_recovery_success,
protocol_errors, user_interactions) are emitted as null — honest absence,
not a fabricated zero.
"""
from __future__ import annotations

from typing import Any

# SPEC.md §10 metric names grouped by source:
_VERDICT_FIELDS = (
    "task_success",
    "correct_final_state",
    "tool_selection_accuracy",
    "tool_argument_accuracy",
    "trajectory_correctness",
    "unnecessary_tool_calls",
    "tool_call_count",
)
_RUN_FIELDS = (
    "round_trips",
    "total_latency_ms",
    "mcp_latency_ms",
    "model_latency_ms",
    "input_tokens",
    "output_tokens",
)


def assemble(task: dict, run_result: dict, verdict: dict) -> dict[str, Any]:
    record: dict[str, Any] = {
        "task_id": task["id"],
        "category": task.get("category"),
        "sdk": run_result.get("sdk"),
    }
    for field in _RUN_FIELDS:
        record[field] = run_result.get(field)
    for field in _VERDICT_FIELDS:
        record[field] = verdict.get(field)
    record["LLM_turn_count"] = (run_result.get("round_trips") or 0) + 1
    record["MCP_round_trips"] = run_result.get("round_trips")
    record["error"] = run_result.get("error") or verdict.get("error")
    record["final_answer"] = run_result.get("final_answer")
    record["tool_calls"] = run_result.get("tool_calls", [])
    # M2+: failure recovery, protocol errors, user interactions.
    record["error_recovery_success"] = None
    record["protocol_errors"] = None
    record["user_interactions"] = None
    return record


def aggregate(records: list[dict]) -> dict[str, Any]:
    """Per-SDK aggregate over task records. Means over successful fields."""
    n = len(records)
    if n == 0:
        return {"n": 0}
    def mean(field: str) -> float | None:
        values = [r[field] for r in records if isinstance(r.get(field), (int, float))]
        return round(sum(values) / len(values), 2) if values else None

    return {
        "n": n,
        "task_success_rate": mean("task_success"),
        "correct_final_state_rate": mean("correct_final_state"),
        "tool_selection_accuracy": mean("tool_selection_accuracy"),
        "tool_argument_accuracy": mean("tool_argument_accuracy"),
        "trajectory_correctness": mean("trajectory_correctness"),
        "unnecessary_tool_calls_sum": sum(r.get("unnecessary_tool_calls") or 0 for r in records),
        "mean_tool_call_count": mean("tool_call_count"),
        "mean_LLM_turns": mean("LLM_turn_count"),
        "mean_MCP_round_trips": mean("MCP_round_trips"),
        "mean_total_latency_ms": mean("total_latency_ms"),
        "mean_mcp_latency_ms": mean("mcp_latency_ms"),
        "mean_model_latency_ms": mean("model_latency_ms"),
        "mean_input_tokens": mean("input_tokens"),
        "mean_output_tokens": mean("output_tokens"),
    }
