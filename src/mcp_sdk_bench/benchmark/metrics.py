"""Per-task metric assembly (SPEC.md §10).

Joins the runner result with the deterministic grader verdict into the full
metric record. Fields no experiment measures yet (error_recovery_success,
protocol_errors) are emitted as null — honest absence, not a fabricated
zero. user_interactions is real since M3.1 (SPEC.md §18).
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

# M2.3b reliability counters (SPEC.md §21). Populated by
# benchmark.reliability for failure-injection runs; None on M1 eval runs
# (honest absence, never a fabricated 0).
_RELIABILITY_FIELDS = (
    "retry_count",
    "duplicate_side_effects",
    "recovery",
)


def retry_count(call_log: list[dict]) -> int:
    """Repeat calls to the same tool with the same arguments (SPEC.md §21).

    A retry is any call whose (name, args_hash) pair was already seen in this
    run: N identical calls contribute N-1 retries. Under fault injection
    retries are correct recovery behavior — a metric, never a failure.
    """
    seen: dict[tuple[str, str], int] = {}
    retries = 0
    for entry in call_log:
        key = (str(entry.get("name")), str(entry.get("args_hash")))
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            retries += 1
    return retries


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
    # M3.1 (SPEC.md §18): MCP round trips include the elicitation
    # pause/resume legs; pre-M3.1 run records lack the field and fall back
    # to the LLM-driven count (identical when no elicitation occurred).
    record["MCP_round_trips"] = run_result.get("mcp_round_trips")
    if record["MCP_round_trips"] is None:
        record["MCP_round_trips"] = run_result.get("round_trips")
    record["error"] = run_result.get("error") or verdict.get("error")
    record["final_answer"] = run_result.get("final_answer")
    record["tool_calls"] = run_result.get("tool_calls", [])
    # M2+: failure recovery and protocol errors remain unmeasured.
    record["error_recovery_success"] = None
    record["protocol_errors"] = None
    # M3.1: real user-interaction count (scripted simulator answers).
    # Records from pre-M3.1 runners lack the field -> None, honest absence.
    record["user_interactions"] = run_result.get("user_interactions")
    # M2.3b reliability counters: taken from the run result when the
    # reliability experiment populated them, else None (honest absence).
    for field in _RELIABILITY_FIELDS:
        record[field] = run_result.get(field)
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
        # M3.1 (SPEC.md §18): scripted-user interactions per task.
        "mean_user_interactions": mean("user_interactions"),
        # M2.3b reliability aggregates (None when unobserved — e.g. M1 runs).
        # `recovery` is bool; bools aggregate as 0/1 via isinstance(int).
        "mean_retry_count": mean("retry_count"),
        "mean_duplicate_side_effects": mean("duplicate_side_effects"),
        "recovery_rate": mean("recovery"),
    }
