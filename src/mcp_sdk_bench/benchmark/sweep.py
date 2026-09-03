"""Eval sweep orchestration: one SDK, one adapter, full dataset, graders, traces.

Wired into `mcpbench eval --sdk <sdk>` and `mcpbench benchmark`.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from mcp_sdk_bench.adapters import (
    ADAPTER_UNAVAILABLE_REASONS,
    AdkAdapter,
    FastMCPAdapter,
    MCPAdapter,
    OfficialAdapter,
)
from mcp_sdk_bench.agent.graph import build_agent
from mcp_sdk_bench.benchmark.metrics import assemble
from mcp_sdk_bench.benchmark.result import (
    run_dir,
    write_capabilities,
    write_eval_result,
)
from mcp_sdk_bench.benchmark.runner import AccessRecordingAdapter, run_task
from mcp_sdk_bench.benchmark.traces import TraceRecorder
from mcp_sdk_bench.evals.datasets import load_dataset
from mcp_sdk_bench.evals.graders import grade_task

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_PATHS = [REPO_ROOT / "datasets" / "basic.jsonl", REPO_ROOT / "datasets" / "composition.jsonl"]

_ADAPTER_CLASSES: dict[str, Any] = {
    "official": OfficialAdapter,
    "fastmcp": FastMCPAdapter,
    "adk": AdkAdapter,
}


def environment_snapshot() -> dict:
    env: dict[str, Any] = {"model_name": os.environ.get("MODEL_NAME"), "model_provider": os.environ.get("MODEL_PROVIDER")}
    env_file = REPO_ROOT / "results" / "environment.json"
    if env_file.exists():
        import json

        env["pinned"] = json.loads(env_file.read_text())
    return env


async def run_sweep(sdk: str, run_id: str) -> tuple[list[dict], dict]:
    if sdk not in _ADAPTER_CLASSES:
        raise ValueError(f"unknown sdk {sdk!r}; expected one of {sorted(_ADAPTER_CLASSES)}")
    adapter_cls = _ADAPTER_CLASSES[sdk]
    if adapter_cls is None:  # unavailable in this env
        reason = ADAPTER_UNAVAILABLE_REASONS.get(f"{sdk.capitalize()}Adapter", "unknown")
        raise RuntimeError(f"{sdk} adapter unavailable in this environment: {reason} — see DECISIONS.md D1")

    tasks: list[dict] = []
    for path in DATASET_PATHS:
        tasks.extend(row.model_dump() for row in load_dataset(path))

    adapter: MCPAdapter = adapter_cls()
    recording = AccessRecordingAdapter(adapter)
    trace = TraceRecorder(run_id, sdk)
    trace.record("run.start", model=os.environ.get("MODEL_NAME", "unset"))

    discovery = await adapter.connect()
    trace.record(
        "mcp.discover",
        tools=[t.name for t in discovery.tools],
        resources=[r.uri for r in discovery.resources],
        prompts=[p.name for p in discovery.prompts],
    )
    graph = build_agent(list(discovery.tools), recording)

    records: list[dict] = []
    try:
        for row in tasks:
            task = {**row, "sdk": sdk}
            trace.record("task.start", task_id=row["id"], category=row.get("category"))
            result = await run_task(task, recording, graph)
            for call in result["tool_calls"]:
                trace.record("mcp.tool_call", task_id=row["id"], tool=call.get("name"), arguments=call.get("arguments"))
            if result.get("error"):
                trace.record("error", task_id=row["id"], message=result["error"])
            verdict = await grade_task(task, result, adapter)
            record = assemble(task, result, verdict)
            records.append(record)
            trace.record("task.end", task_id=row["id"], task_success=record["task_success"])
    finally:
        await adapter.close()

    trace.write(run_dir(run_id) / f"trace-{sdk}.jsonl")

    snapshot: dict[str, Any] = {
        "tools": [t.name for t in discovery.tools],
        "resources": [r.uri for r in discovery.resources],
        "prompts": [p.name for p in discovery.prompts],
    }
    return records, snapshot


def sweep_sync(sdk: str, run_id: str) -> None:
    records, snapshot = asyncio.run(run_sweep(sdk, run_id))
    environment = environment_snapshot()
    write_eval_result(run_id, sdk, records, environment)
    write_capabilities(run_id, {sdk: snapshot}, environment)
