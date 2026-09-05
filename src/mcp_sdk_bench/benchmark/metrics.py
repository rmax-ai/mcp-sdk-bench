"""Per-task metric assembly (SPEC.md §10).

Joins the runner result with the deterministic grader verdict into the full
metric record. Fields no experiment measures yet (error_recovery_success,
protocol_errors) are emitted as null — honest absence, not a fabricated
zero. user_interactions is real since M3.1 (SPEC.md §18).

M3.3 (SPEC.md §9 H) adds two per-record long-running metrics computed from
the run's tool-call log, never from an LLM:

- progress_consumption: how many DISTINCT migration-progress values the
  agent actually observed via tool results across the run (0 when the run
  touched no long-running task). For all three variants the agent-visible
  progress is POLL-BASED (get_migration_status plain tool results — the
  protocol differentiation lives in the harness adapters, not the agent
  loop), so the count is a fair measure of sampling behavior across SDKs.
- cancellation_behavior: "none" | "requested-cancelled" |
  "requested-not-cancelled" | "not-requested" — whether the scripted user
  requested a cancellation (cancel-at-progress policy fired) and whether a
  cancel tool call actually produced the cancelled world state. h-02
  expects "requested-cancelled".
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

#: App-level cancel tool names (report + migration kinds); a successful
#: cancel call reports the view's status as "cancelled".
_CANCEL_TOOL_NAMES = frozenset({"cancel_report_task", "cancel_migration"})

#: The cancel-at-progress scripted-user policy prefix (agent/simulator.py).
_CANCEL_AT_PROGRESS_POLICY = "cancel-at-progress:"


def _entry_progress(entry: dict) -> float | None:
    """Migration progress carried by one tool-call-log entry (the task-view
    envelope {"task": {"progress": ...}}), or None when the entry is not a
    task-tool result."""
    structured = entry.get("structured_content")
    if not isinstance(structured, dict):
        return None
    task = structured.get("task")
    if not isinstance(task, dict):
        return None
    progress = task.get("progress")
    return progress if isinstance(progress, (int, float)) else None


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


def progress_consumption(call_log: list[dict]) -> int:
    """Number of DISTINCT progress values the agent observed via tool
    results (M3.3, SPEC.md §9 H). Every task-tool result that carries a
    progress value counts — the start view (0.0) and every polled snapshot.
    Poll-based by construction on all three variants (see module docstring);
    never an LLM judgment."""
    observed = {
        _entry_progress(entry)
        for entry in call_log
        if _entry_progress(entry) is not None
    }
    return len(observed)


def cancellation_behavior(task: dict, run_result: dict) -> str:
    """Classify the run's cancellation lane (M3.3, SPEC.md §9 H).

    - "requested-cancelled": the scripted user asked to cancel
      (cancel-at-progress policy + the injected user interaction is counted)
      AND a cancel tool call produced the cancelled world state.
    - "requested-not-cancelled": the user asked, but no cancel call
      succeeded (the agent ignored the request or the cancel errored).
    - "not-requested": a cancel succeeded WITHOUT a user request (agent-
      initiated), or the user asked but never got to inject (agent
      cancelled/answered first — treated as not user-requested).
    - "none": no cancellation activity and no cancellation policy.
    """
    policy = str(task.get("user_simulator_policy") or "")
    user_requested = policy.startswith(_CANCEL_AT_PROGRESS_POLICY) and int(
        run_result.get("user_interactions") or 0
    ) >= 1
    cancelled = any(
        str(entry.get("name")) in _CANCEL_TOOL_NAMES
        and not entry.get("is_error")
        and (entry.get("structured_content") or {}).get("task", {}).get("status")
        == "cancelled"
        for entry in run_result.get("tool_call_log") or []
    )
    if user_requested:
        return "requested-cancelled" if cancelled else "requested-not-cancelled"
    return "not-requested" if cancelled else "none"


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
    # M3.3 long-running metrics (computed from the tool-call log, never an
    # LLM judge — see module docstring).
    call_log = run_result.get("tool_call_log") or []
    record["progress_consumption"] = progress_consumption(call_log)
    record["cancellation_behavior"] = cancellation_behavior(task, run_result)
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
        # M3.3 (SPEC.md §9 H): mean distinct progress values the agent
        # sampled per task, plus the cancellation-behavior distribution
        # (categorical — aggregated as counts, not a mean).
        "mean_progress_consumption": mean("progress_consumption"),
        "cancellation_behavior_counts": {
            label: sum(1 for r in records if r.get("cancellation_behavior") == label)
            for label in sorted(
                {
                    str(value)
                    for value in (r.get("cancellation_behavior") for r in records)
                    if value is not None
                }
            )
        },
        # M2.3b reliability aggregates (None when unobserved — e.g. M1 runs).
        # `recovery` is bool; bools aggregate as 0/1 via isinstance(int).
        "mean_retry_count": mean("retry_count"),
        "mean_duplicate_side_effects": mean("duplicate_side_effects"),
        "recovery_rate": mean("recovery"),
    }
