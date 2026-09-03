"""Run-result assembly and the report generator (SPEC.md §25, §27-lite for M1).

results/<run_id>/eval-<sdk>.json   — per-SDK task records + aggregate
results/<run_id>/trace-<sdk>.jsonl — normalized trace events
results/latest/*.json              — latest run, aggregated by `mcpbench report`
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from mcp_sdk_bench.benchmark.metrics import aggregate

RESULTS_DIR = Path(__file__).resolve().parents[3] / "results"
LATEST_DIR = RESULTS_DIR / "latest"


def new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def run_dir(run_id: str) -> Path:
    return RESULTS_DIR / run_id


def write_eval_result(run_id: str, sdk: str, records: list[dict], environment: dict) -> Path:
    path = run_dir(run_id) / f"eval-{sdk}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "run_id": run_id,
        "sdk": sdk,
        "environment": environment,
        "aggregate": aggregate(records),
        "tasks": records,
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def write_capabilities(run_id: str, snapshots: dict[str, dict], environment: dict) -> Path:
    """Discovery snapshot per SDK (honest subset of the full matrix — the
    full protocol-level matrix completes in M2/M5.5)."""
    path = run_dir(run_id) / "capabilities.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "run_id": run_id,
        "environment": environment,
        "note": "M1 discovery snapshot: what each variant's adapter exposed at connect time. Protocol-level matrix rows land in M2/M5.5.",
        "snapshots": snapshots,
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def report(run_id: str) -> list[Path]:
    """Aggregate the latest run into results/latest/*.json (SPEC §25)."""
    src = run_dir(run_id)
    eval_files = sorted(src.glob("eval-*.json"))
    if not eval_files:
        raise FileNotFoundError(f"no eval-*.json results under {src}")

    per_sdk: dict[str, Any] = {}
    all_tasks: list[dict] = []
    environment: dict = {}
    for path in eval_files:
        payload = json.loads(path.read_text())
        sdk = payload["sdk"]
        per_sdk[sdk] = payload["aggregate"]
        environment = payload.get("environment", environment)
        all_tasks.extend(payload["tasks"])

    LATEST_DIR.mkdir(parents=True, exist_ok=True)
    summary = {
        "run_id": run_id,
        "environment": environment,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sdks": sorted(per_sdk),
        "tasks_per_sdk": {sdk: len([t for t in all_tasks if t["sdk"] == sdk]) for sdk in per_sdk},
        "per_sdk": per_sdk,
    }
    written = []
    written.append(_write_json("summary.json", summary))
    written.append(_write_json("agent-evals.json", {"run_id": run_id, "tasks": all_tasks}))
    written.append(
        _write_json(
            "performance.json",
            {
                "run_id": run_id,
                "per_sdk": {
                    sdk: {k: v for k, v in agg.items() if "latency" in k or "token" in k or k == "n"}
                    for sdk, agg in per_sdk.items()
                },
            },
        )
    )
    caps = src / "capabilities.json"
    if caps.exists():
        written.append(_write_json("capabilities.json", json.loads(caps.read_text())))
    # Not yet run in M1 — explicit markers, never fabricated data (SPEC §25).
    written.append(
        _write_json(
            "interoperability.json",
            {"run_id": run_id, "status": "not-yet-run", "milestone": "M2"},
        )
    )
    written.append(
        _write_json(
            "dx.json",
            {"run_id": run_id, "status": "not-yet-run", "milestone": "M5"},
        )
    )
    return written


def _write_json(name: str, payload: dict) -> Path:
    path = LATEST_DIR / name
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path
