"""Hermetic reliability-experiment tests (M2.3b, SPEC.md §21).

No model API, no network, no subprocess servers: the LangGraph agent runs
against an IN-PROCESS world through a stub MCPAdapter (the M1 fake-model
pattern from tests/regression/test_graph.py), with the shared fault dispatch
(run_tool_with_faults) standing in for the server variants' fault layer.

(a) fail-01 scripted with a failed-after first create_ticket + one retry:
    task succeeds, duplicate_side_effects 0, world holds exactly one T-901.
(b) a scripted agent that calls get_ticket 3 times (two injected failures,
    then success): retry_count 2, recovery true.
(c) aggregate_runs: one recovered run + one unrecovered -> 0.5.
(d) datasets/failures.jsonl validates against BenchmarkTask (5 rows).
(e) fault_config_label includes the active knobs.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from mcp_sdk_bench.adapters.base import Discovery, MCPAdapter, ToolResult, ToolSpec
from mcp_sdk_bench.benchmark.reliability import (
    ReliabilitySession,
    aggregate_runs,
    fault_config_label,
    run_reliability,
)
from mcp_sdk_bench.evals.datasets import BenchmarkTask, load_dataset
from mcp_sdk_bench.faults import (
    INJECTED_FAULT,
    FaultConfig,
    FaultEngine,
    InjectedToolFault,
    load_fault_config,
    run_tool_with_faults,
    settle,
)
from mcp_sdk_bench.world import Ticket, TicketStatus, World, WorldError, seed_world

REPO_ROOT = Path(__file__).resolve().parents[2]
FAILURES_DATASET = REPO_ROOT / "datasets" / "failures.jsonl"

WORLD_TOOL_SPECS = [
    ToolSpec(
        name="get_ticket",
        description="Fetch a single ticket by id.",
        input_schema={
            "type": "object",
            "properties": {"ticket_id": {"type": "string"}},
            "required": ["ticket_id"],
        },
    ),
    ToolSpec(
        name="create_ticket",
        description="Create a ticket; idempotent on idempotency_key (SPEC.md §21).",
        input_schema={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
                "title": {"type": "string"},
                "priority": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "required": ["ticket_id", "title", "idempotency_key"],
        },
    ),
    ToolSpec(
        name="update_ticket",
        description="Update a ticket's status and/or assignee.",
        input_schema={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
                "status": {"type": "string"},
                "assignee": {"type": "string"},
            },
            "required": ["ticket_id"],
        },
    ),
    ToolSpec(
        name="get_inventory",
        description="List all inventory items with availability.",
        input_schema={"type": "object", "properties": {}},
    ),
    ToolSpec(
        name="deploy_service",
        description="Deploy a service at a target version to an environment.",
        input_schema={
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "target_version": {"type": "string"},
                "environment": {"type": "string"},
            },
            "required": ["service", "target_version", "environment"],
        },
    ),
]


class BindableFakeChatModel(GenericFakeChatModel):
    """GenericFakeChatModel does not implement bind_tools; for the scripted
    loop the tool schema is irrelevant, so return self unchanged."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


def _ticket_result(ticket: Ticket) -> ToolResult:
    return ToolResult(structured_content={"ticket": ticket.model_dump(mode="json")})


class InProcessWorldAdapter(MCPAdapter):
    """In-process world adapter for hermetic reliability tests: same tool
    surface and result shapes as the server variants, same shared fault
    dispatch (run_tool_with_faults) when an engine is configured."""

    def __init__(self, world: World, engine: FaultEngine | None = None) -> None:
        self.world = world
        self.engine = engine

    async def connect(self) -> Discovery:
        return Discovery(tools=WORLD_TOOL_SPECS, resources=[], prompts=[])

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        execute, is_replay = self._bind(name, arguments)
        try:
            if self.engine is not None:
                return await run_tool_with_faults(self.engine, execute, is_replay=is_replay)
            return await settle(execute())
        except (InjectedToolFault, WorldError) as err:
            return ToolResult(is_error=True, text=str(err))

    def _bind(self, name: str, arguments: dict) -> tuple[Any, Any]:
        world = self.world
        if name == "get_ticket":
            return lambda: _ticket_result(world.get_ticket(arguments["ticket_id"])), (
                lambda: False
            )
        if name == "create_ticket":
            key = arguments["idempotency_key"]
            return (
                lambda: _ticket_result(
                    world.create_ticket(
                        arguments["ticket_id"],
                        arguments["title"],
                        arguments.get("priority"),
                        idempotency_key=key,
                    )
                ),
                lambda: world.ticket_for_idempotency_key(key) is not None,
            )
        if name == "update_ticket":
            status = arguments.get("status")
            return (
                lambda: _ticket_result(
                    world.update_ticket(
                        arguments["ticket_id"],
                        TicketStatus(status) if status is not None else None,
                        arguments.get("assignee"),
                    )
                ),
                lambda: False,
            )
        if name == "get_inventory":
            return (
                lambda: ToolResult(
                    structured_content={
                        "items": {
                            k: v.model_dump(mode="json")
                            for k, v in world.get_inventory().items()
                        }
                    }
                ),
                lambda: False,
            )
        if name == "deploy_service":

            async def deploy() -> ToolResult:
                deployment = await world.deploy_service(
                    arguments["service"],
                    arguments["target_version"],
                    arguments["environment"],
                )
                return ToolResult(
                    structured_content={"deployment": deployment.model_dump(mode="json")}
                )

            return deploy, lambda: False
        raise WorldError(f"unknown tool {name}")

    async def read_resource(self, uri: str) -> str:
        raise RuntimeError("in-process adapter does not serve resources")

    async def get_prompt(self, name: str, arguments: dict) -> str:
        raise RuntimeError("in-process adapter does not serve prompts")

    async def close(self) -> None:
        pass


class ScriptedlyFailingAdapter(InProcessWorldAdapter):
    """Fails the first N calls per named tool with the canonical injected-
    fault message, then delegates to the world (deterministic, engine-free —
    the script IS the fault sequence)."""

    def __init__(self, world: World, failures: dict[str, int]) -> None:
        super().__init__(world, None)
        self._failures = dict(failures)

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        if self._failures.get(name, 0) > 0:
            self._failures[name] -= 1
            return ToolResult(is_error=True, text=INJECTED_FAULT)
        return await super().call_tool(name, arguments)


def _tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id}]
    )


def _tasks(task_id: str) -> list[BenchmarkTask]:
    return [t for t in load_dataset(FAILURES_DATASET) if t.id == task_id]


# ---- (a) fail-01: failed-after first create_ticket + one idempotent retry ----

FAIL_AFTER_CONFIG = FaultConfig(fail_tool_call=1.0, fail_phase="after")
CREATE_ARGS = {"ticket_id": "T-901", "title": "Recovery incident", "idempotency_key": "IDEM-01"}


async def test_fail01_fail_after_retry_succeeds_without_duplicates() -> None:
    worlds: list[World] = []

    @asynccontextmanager
    async def session_factory(sdk: str, fault_env: dict[str, str]) -> AsyncIterator[ReliabilitySession]:
        world = seed_world()
        worlds.append(world)
        engine = FaultEngine(load_fault_config(fault_env))
        adapter = InProcessWorldAdapter(world, engine)
        await adapter.connect()
        yield ReliabilitySession(adapter=adapter, tools=WORLD_TOOL_SPECS, world=world)

    def model_factory(task_id: str, run_index: int) -> BindableFakeChatModel:
        # The first create_ticket fails AFTER execution; the retry (same
        # idempotency key) is a replay and returns the existing ticket.
        return BindableFakeChatModel(
            messages=iter(
                [
                    _tool_call("create_ticket", CREATE_ARGS, "call-1"),
                    _tool_call("create_ticket", CREATE_ARGS, "call-2"),
                    AIMessage(content="Created ticket T-901."),
                ]
            )
        )

    records = await run_reliability(
        _tasks("fail-01"),
        "inprocess",
        FAIL_AFTER_CONFIG,
        1,
        session_factory=session_factory,
        model_factory=model_factory,
    )

    assert len(records) == 1
    record = records[0]
    assert record["task_id"] == "fail-01"
    assert record["task_success"] is True
    assert record["recovery"] is True  # an injected fault fired AND the task succeeded
    assert record["duplicate_side_effects"] == 0
    assert record["retry_count"] == 1  # one repeat of (create_ticket, same args)
    assert record["incorrect_final_state"] is False
    assert record["answer_ok"] is True
    assert record["fault_config_label"] == fault_config_label(FAIL_AFTER_CONFIG)

    # The world holds exactly ONE ticket for the idempotency key.
    world = worlds[0]
    created = [t for t in world.tickets.values() if t.idempotency_key == "IDEM-01"]
    assert len(created) == 1
    assert created[0].id == "T-901"


# ---- (b) scripted get_ticket retried 3x: retry_count 2, recovery true ----


async def test_fail02_retries_counted_and_recovery_true() -> None:
    @asynccontextmanager
    async def session_factory(sdk: str, fault_env: dict[str, str]) -> AsyncIterator[ReliabilitySession]:
        world = seed_world()
        adapter = ScriptedlyFailingAdapter(world, {"get_ticket": 2})
        await adapter.connect()
        yield ReliabilitySession(adapter=adapter, tools=WORLD_TOOL_SPECS, world=world)

    def model_factory(task_id: str, run_index: int) -> BindableFakeChatModel:
        return BindableFakeChatModel(
            messages=iter(
                [
                    _tool_call("get_ticket", {"ticket_id": "PAY-123"}, "call-1"),
                    _tool_call("get_ticket", {"ticket_id": "PAY-123"}, "call-2"),
                    _tool_call("get_ticket", {"ticket_id": "PAY-123"}, "call-3"),
                    AIMessage(content="PAY-123 is OPEN."),
                ]
            )
        )

    records = await run_reliability(
        _tasks("fail-02"),
        "inprocess",
        FaultConfig(fail_tool_call=0.3, fail_phase="before"),
        1,
        session_factory=session_factory,
        model_factory=model_factory,
    )

    assert len(records) == 1
    record = records[0]
    assert record["tool_call_count"] == 3
    assert record["retry_count"] == 2  # 3 identical calls -> 2 retries
    assert record["injected_faults"] == 2
    assert record["faults_fired"] is True
    assert record["task_success"] is True
    assert record["recovery"] is True
    assert record["protocol_errors"] == 0


# ---- (c) aggregation: one recovered run + one not -> recovery_probability 0.5 ----


def test_aggregate_runs_recovery_probability() -> None:
    records = [
        {
            "task_success": True,
            "recovery": True,
            "faults_fired": True,
            "retry_count": 1,
            "duplicate_side_effects": 0,
            "incorrect_final_state": False,
            "protocol_errors": 0,
            "tool_call_count": 2,
        },
        {
            "task_success": False,
            "recovery": False,
            "faults_fired": True,
            "retry_count": 3,
            "duplicate_side_effects": 1,
            "incorrect_final_state": True,
            "protocol_errors": 1,
            "tool_call_count": 4,
        },
    ]
    agg = aggregate_runs(records)
    assert agg["n"] == 2
    assert agg["success_rate"] == 0.5
    assert agg["recovery_probability"] == 0.5
    assert agg["recovery_probability_reason"] is None
    assert agg["mean_retries"] == 2.0
    assert agg["duplicate_side_effect_rate"] == 0.5
    assert agg["incorrect_final_state_rate"] == 0.5
    assert agg["protocol_error_rate"] == 0.5


def test_aggregate_runs_baseline_recovery_is_null_with_reason() -> None:
    """Honest label: with no faults fired, recovery_probability is null —
    never a fabricated 0 (SPEC.md §7)."""
    records = [
        {
            "task_success": True,
            "recovery": False,
            "faults_fired": False,
            "retry_count": 0,
            "duplicate_side_effects": 0,
            "incorrect_final_state": False,
            "protocol_errors": 0,
            "tool_call_count": 1,
        }
    ]
    agg = aggregate_runs(records)
    assert agg["recovery_probability"] is None
    assert agg["recovery_probability_reason"] is not None


# ---- (d) the failure dataset validates against BenchmarkTask ----


def test_failures_dataset_validates() -> None:
    tasks = load_dataset(FAILURES_DATASET)
    assert [t.id for t in tasks] == ["fail-01", "fail-02", "fail-03", "fail-04", "fail-05"]
    assert all(t.category == "E" for t in tasks)
    # Retry-agnostic by design: no trajectory grading for failure tasks.
    assert all(t.expected_trajectory is None for t in tasks)
    by_id = {t.id: t for t in tasks}
    assert by_id["fail-04"].forbidden_contains == ["production"]
    assert by_id["fail-03"].allowed_extra_tools == ["get_ticket"]
    assert "deployment:checkout" in by_id["fail-04"].expected_final_state


# ---- (e) the fault config label includes the active knobs ----


def test_fault_config_label_includes_active_knobs() -> None:
    label = fault_config_label(FaultConfig(fail_tool_call=0.3, fail_phase="before"))
    assert "fail=0.3" in label
    assert "phase=before" in label
    assert "baseline" not in label

    latency = fault_config_label(FaultConfig(latency_ms=300))
    assert "latency=300ms" in latency

    wire = fault_config_label(FaultConfig(drop_connection_after=3, malformed_response_rate=0.05))
    assert "drop=3" in wire
    assert "malformed=0.05" in wire

    baseline = fault_config_label(FaultConfig())
    assert baseline.startswith("baseline:")


def test_model_outage_guard_raises_on_all_error_records() -> None:
    """A cell where every run errored with zero tool calls is a model-backend
    outage, not data — the guard must raise (regression, 2026-09-04:
    DeepSeek balance hit zero mid-experiment and cells recorded 0.00 rows)."""
    from mcp_sdk_bench.benchmark.reliability import _raise_if_model_outage

    with pytest.raises(RuntimeError, match="model backend failure in fastmcp/FAIL_BEFORE"):
        _raise_if_model_outage(
            [{"error": "Error code: 402 - Insufficient Balance", "tool_call_count": 0}] * 2,
            "fastmcp",
            "FAIL_BEFORE",
        )


def test_model_outage_guard_passes_with_any_completed_run() -> None:
    from mcp_sdk_bench.benchmark.reliability import _raise_if_model_outage

    # One healthy run in the cell → legitimate data, no raise.
    _raise_if_model_outage(
        [
            {"error": None, "tool_call_count": 3},
            {"error": "Error code: 402 - Insufficient Balance", "tool_call_count": 0},
        ],
        "official",
        "FAIL_AFTER",
    )
