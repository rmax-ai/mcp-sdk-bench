"""Eval sweep orchestration: one SDK, one adapter per task, graders, traces.

Per-task world isolation (SPEC.md §23): a fresh adapter + server process per
task gives every task an untouched world. The model is constructed once per
sweep (agent identity stays pinned); the graph is rebuilt per task because it
closes over the adapter. Resources/prompts are surfaced as host-mediated
pseudo-tools (the benchmark agent plays the MCP host role, SPEC.md §2).
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from mcp_sdk_bench.adapters import (
    ADAPTER_UNAVAILABLE_REASONS,
    AdkAdapter,
    FastMCPAdapter,
    MCPAdapter,
    OfficialAdapter,
)
from mcp_sdk_bench.adapters.base import ToolSpec
from mcp_sdk_bench.agent.graph import build_agent, build_model
from mcp_sdk_bench.agent.simulator import ScriptedUserSimulator
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

REPO_ROOT = Path(__file__).resolve().parents[3]
DATASET_PATHS = [REPO_ROOT / "datasets" / "basic.jsonl", REPO_ROOT / "datasets" / "composition.jsonl"]

_ADAPTER_CLASSES: dict[str, Any] = {
    "official": OfficialAdapter,
    "fastmcp": FastMCPAdapter,
    "adk": AdkAdapter,
}

READ_RESOURCE_SPEC = ToolSpec(
    name="read_resource",
    description=(
        "Read an MCP resource by URI (host-mediated resource access). "
        "Use for company policies and documents."
    ),
    input_schema={
        "type": "object",
        "properties": {"uri": {"type": "string", "description": "Resource URI, e.g. company://policies/deployment"}},
        "required": ["uri"],
    },
)

GET_PROMPT_SPEC = ToolSpec(
    name="get_prompt",
    description="Render an MCP prompt template by name (host-mediated prompt access).",
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}, "arguments": {"type": "object"}},
        "required": ["name"],
    },
)


def environment_snapshot() -> dict:
    env: dict[str, Any] = {"model_name": os.environ.get("MODEL_NAME"), "model_provider": os.environ.get("MODEL_PROVIDER")}
    env_file = REPO_ROOT / "results" / "environment.json"
    if env_file.exists():
        env["pinned"] = json.loads(env_file.read_text())
    return env


async def run_sweep(
    sdk: str, run_id: str, dataset_paths: list[Path] | None = None
) -> tuple[list[dict], dict]:
    if sdk not in _ADAPTER_CLASSES:
        raise ValueError(f"unknown sdk {sdk!r}; expected one of {sorted(_ADAPTER_CLASSES)}")
    adapter_cls = _ADAPTER_CLASSES[sdk]
    if adapter_cls is None:  # unavailable in this env
        reason = ADAPTER_UNAVAILABLE_REASONS.get(f"{sdk.capitalize()}Adapter", "unknown")
        raise RuntimeError(f"{sdk} adapter unavailable in this environment: {reason} — see DECISIONS.md D1")

    tasks: list[dict] = []
    # Default stays basic+composition so `mcpbench benchmark` / `eval`
    # without --dataset is unchanged; interactive.jsonl is opt-in (M3.1).
    for path in dataset_paths or DATASET_PATHS:
        tasks.extend(row.model_dump() for row in load_dataset(path))

    model = build_model()  # once per sweep — agent identity pinned (SPEC §23)
    trace = TraceRecorder(run_id, sdk)
    trace.record("run.start", model=os.environ.get("MODEL_NAME", "unset"))

    # Discovery once (probe), snapshot for capabilities + agent tool list.
    probe = adapter_cls()
    try:
        discovery = await probe.connect()
    finally:
        await probe.close()
    trace.record(
        "mcp.discover",
        tools=[t.name for t in discovery.tools],
        resources=[r.uri for r in discovery.resources],
        prompts=[p.name for p in discovery.prompts],
    )
    agent_tools: list[ToolSpec] = list(discovery.tools)
    if discovery.resources:
        agent_tools.append(READ_RESOURCE_SPEC)
    if discovery.prompts:
        agent_tools.append(GET_PROMPT_SPEC)

    records: list[dict] = []
    for row in tasks:
        task = {**row, "sdk": sdk}
        adapter: MCPAdapter = adapter_cls()
        recording = AccessRecordingAdapter(adapter)
        trace.record("task.start", task_id=row["id"], category=row.get("category"))
        connect_start = time.perf_counter()
        try:
            await adapter.connect()
            # M3.1 (SPEC.md §18): the per-task scripted user. Default
            # (policy None) is "none" — no interaction, M1/M2 unchanged.
            graph = build_agent(
                agent_tools,
                recording,
                model=model,
                user_simulator=ScriptedUserSimulator(row.get("user_simulator_policy")),
            )
            result = await run_task(task, recording, graph)
            for call in result["tool_calls"]:
                trace.record("mcp.tool_call", task_id=row["id"], tool=call.get("name"), arguments=call.get("arguments"))
            if result.get("error"):
                trace.record("error", task_id=row["id"], message=result["error"])
            verdict = await grade_task(task, result, adapter)
            record = assemble(task, result, verdict)
            record["total_latency_ms"] = float(record["total_latency_ms"]) + (time.perf_counter() - connect_start) * 1000
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


def sweep_sync(sdk: str, run_id: str, dataset_paths: list[Path] | None = None) -> None:
    records, snapshot = asyncio.run(run_sweep(sdk, run_id, dataset_paths))
    environment = environment_snapshot()
    write_eval_result(run_id, sdk, records, environment)
    write_capabilities(run_id, {sdk: snapshot}, environment)
