"""Result assembly + report generation tests (deterministic, fixture-driven)."""
import json

import pytest

from mcp_sdk_bench.benchmark import metrics
from mcp_sdk_bench.benchmark import result as result_mod


@pytest.fixture()
def isolate_results(tmp_path, monkeypatch):
    monkeypatch.setattr(result_mod, "RESULTS_DIR", tmp_path / "results")
    monkeypatch.setattr(result_mod, "LATEST_DIR", tmp_path / "results" / "latest")
    return tmp_path / "results"


def test_assemble_maps_all_fields() -> None:
    task = {"id": "basic-001", "category": "A"}
    run = {
        "sdk": "official",
        "round_trips": 1,
        "total_latency_ms": 5000.0,
        "mcp_latency_ms": 40.0,
        "model_latency_ms": 4960.0,
        "input_tokens": 1200,
        "output_tokens": 80,
        "final_answer": "PAY-123 is OPEN",
        "error": None,
    }
    verdict = {
        "task_success": 1.0,
        "correct_final_state": 1.0,
        "tool_selection_accuracy": 1.0,
        "tool_argument_accuracy": 1.0,
        "trajectory_correctness": None,
        "unnecessary_tool_calls": 0,
        "tool_call_count": 1,
    }
    record = metrics.assemble(task, run, verdict)
    assert record["LLM_turn_count"] == 2
    assert record["MCP_round_trips"] == 1
    assert record["model_latency_ms"] == 4960.0
    assert record["error_recovery_success"] is None  # honest absence, M2+
    assert record["task_success"] == 1.0


def test_aggregate_means() -> None:
    records = [
        {"task_success": 1.0, "correct_final_state": 1.0, "total_latency_ms": 100.0, "mcp_latency_ms": 10.0},
        {"task_success": 0.0, "correct_final_state": 1.0, "total_latency_ms": 200.0, "mcp_latency_ms": 20.0},
    ]
    agg = metrics.aggregate(records)
    assert agg["n"] == 2
    assert agg["task_success_rate"] == 0.5
    assert agg["mean_total_latency_ms"] == 150.0


def test_report_generates_all_artifacts(isolate_results) -> None:
    run_id = "testrun"
    run_dir = isolate_results / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "eval-official.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "sdk": "official",
                "environment": {"model_name": "fake"},
                "aggregate": metrics.aggregate([{"task_success": 1.0, "total_latency_ms": 10.0}]),
                "tasks": [{"task_id": "basic-001", "sdk": "official", "task_success": 1.0}],
            }
        )
    )
    (run_dir / "capabilities.json").write_text(json.dumps({"run_id": run_id, "snapshots": {}}))

    written = result_mod.report(run_id)
    names = {p.name for p in written}
    assert {"summary.json", "agent-evals.json", "performance.json", "capabilities.json",
            "interoperability.json", "dx.json"} <= names
    interop = json.loads((isolate_results / "latest" / "interoperability.json").read_text())
    assert interop["status"] == "not-yet-run"  # honest marker, not fabricated data


def test_report_requires_eval_files(isolate_results) -> None:
    with pytest.raises(FileNotFoundError):
        result_mod.report("missing-run")
