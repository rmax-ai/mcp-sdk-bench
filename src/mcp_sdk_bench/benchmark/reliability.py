"""The reliability experiment (SPEC.md §21; methodology §23).

Runs the Category-E failure dataset (datasets/failures.jsonl) for one SDK
under one deterministic fault configuration, n_runs per task. Every run gets
a FRESH server session with the fault env applied; the per-run FAULT_SEED is
derived deterministically from (sdk, task_id, run_index) via run_seed, so an
entire experiment grid reproduces exactly (SPEC.md §23: same model, same
prompts, same task order, same agent loop across SDKs — only the MCP
integration under test changes).

Fault-aware grading differs from M1 in one essential way: under fault
injection RETRIES ARE CORRECT BEHAVIOR. Runs grade on OUTCOME
(expected_final_state, answer) and SIDE EFFECTS (no duplicate creates);
recovery, retries, and protocol errors are METRICS, never failures.
expected_trajectory graders are deliberately not used here (a retry would
spuriously fail a trajectory check); Category-E tasks set
expected_trajectory=null.

Wire-level fault knobs (DROP_CONNECTION_AFTER / MALFORMED_RESPONSE_RATE) are
NOT part of the default experiment: they kill sessions wholesale and require
routing the client through the StdioProxy (tests/conformance/helpers.py),
which the benchmark adapters do not do. The default session factory REJECTS
wire-level configs with ValueError rather than silently running a fault-free
server (honest absence, SPEC.md §7); M2.3a covers wire faults at the session
level.

Honest labels (SPEC.md §7): recovery_probability is null with a reason when
no fault fired in any run of a cell — never a fabricated 0.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any

from langchain_core.language_models import BaseChatModel

from mcp_sdk_bench.adapters import (
    ADAPTER_UNAVAILABLE_REASONS,
    AdkAdapter,
    FastMCPAdapter,
    MCPAdapter,
    OfficialAdapter,
)
from mcp_sdk_bench.adapters.base import ToolResult, ToolSpec
from mcp_sdk_bench.agent.graph import build_agent, build_model
from mcp_sdk_bench.benchmark.metrics import retry_count
from mcp_sdk_bench.benchmark.runner import AccessRecordingAdapter, run_task
from mcp_sdk_bench.benchmark.sweep import GET_PROMPT_SPEC, READ_RESOURCE_SPEC
from mcp_sdk_bench.evals.datasets import BenchmarkTask
from mcp_sdk_bench.faults import (
    INJECTED_FAULT,
    INJECTED_TASK_FAILURE,
    FaultConfig,
)
from mcp_sdk_bench.world import World

#: The default M2.3b experiment grid (SPEC.md §21). Wire-level configs
#: (drop/malformed) are deliberately excluded — see module docstring.
DEFAULT_FAULT_CONFIGS: dict[str, FaultConfig] = {
    "BASELINE": FaultConfig(),
    "FAIL_BEFORE": FaultConfig(fail_tool_call=0.3, fail_phase="before"),
    "FAIL_AFTER": FaultConfig(fail_tool_call=0.3, fail_phase="after"),
    "LATENCY": FaultConfig(latency_ms=300),
}

_ADAPTER_CLASSES: dict[str, Any] = {
    "official": OfficialAdapter,
    "fastmcp": FastMCPAdapter,
    "adk": AdkAdapter,
}


def faults_active(config: FaultConfig) -> bool:
    """True when any fault knob is on. Whether a fault FIRED in a given run
    is observed per run (seeded RNG may spare individual runs)."""
    return (
        config.fail_tool_call > 0
        or config.task_failure_rate > 0
        or config.latency_ms > 0
        or config.drop_connection_after is not None
        or config.malformed_response_rate > 0
    )


def fault_config_label(config: FaultConfig) -> str:
    """Config summary for records and reports: fail probability + phase,
    latency, drop, malformed, task-failure rate. Baseline configs are
    prefixed so report readers cannot mistake them for a fault run."""
    drop = (
        str(config.drop_connection_after)
        if config.drop_connection_after is not None
        else "-"
    )
    label = (
        f"fail={config.fail_tool_call}/phase={config.fail_phase}"
        f"/latency={config.latency_ms}ms/drop={drop}"
        f"/malformed={config.malformed_response_rate}"
        f"/task_fail={config.task_failure_rate}"
    )
    return label if faults_active(config) else f"baseline:{label}"


def run_seed(task_id: str, sdk: str, run_index: int) -> int:
    """Deterministic per-run FAULT_SEED derived from (sdk, task, run_index)
    (SPEC.md §23): the whole experiment grid reproduces byte-identically."""
    digest = hashlib.sha256(f"{sdk}:{task_id}:{run_index}".encode()).hexdigest()
    return int(digest[:8], 16)


def fault_env_for(config: FaultConfig, seed: int) -> dict[str, str]:
    """Map a FaultConfig + per-run seed onto the M2.3a fault env vars. Only
    non-default knobs are set, so BASELINE spawns an unconfigured server
    (plus FAULT_SEED, which is inert without active knobs)."""
    env: dict[str, str] = {}
    if config.fail_tool_call > 0:
        env["FAIL_TOOL_CALL"] = str(config.fail_tool_call)
        env["FAIL_PHASE"] = config.fail_phase
    if config.latency_ms > 0:
        env["LATENCY_MS"] = str(config.latency_ms)
    if config.task_failure_rate > 0:
        env["TASK_FAILURE_RATE"] = str(config.task_failure_rate)
    if config.drop_connection_after is not None:
        env["DROP_CONNECTION_AFTER"] = str(config.drop_connection_after)
    if config.malformed_response_rate > 0:
        env["MALFORMED_RESPONSE_RATE"] = str(config.malformed_response_rate)
    env["FAULT_SEED"] = str(seed)
    return env


@dataclass
class ReliabilitySession:
    """One fault-configured server session plus its agent tool list.

    `world` is the in-process World when (and only when) the harness runs the
    world in this process (hermetic tests). Subprocess server worlds are NOT
    observable from the runner, so world-derived metrics then fall back to
    what the tool surface reveals (see _duplicate_side_effects).
    """

    adapter: MCPAdapter
    tools: list[ToolSpec]
    world: World | None = None


#: (sdk, fault_env) -> async context manager yielding a connected session.
SessionFactory = Callable[[str, dict[str, str]], AbstractAsyncContextManager[ReliabilitySession]]

#: (task_id, run_index) -> the (scripted or real) model for that run.
ModelFactory = Callable[[str, int], BaseChatModel]


@contextlib.contextmanager
def _patched_environ(updates: dict[str, str]) -> Iterator[None]:
    """Temporarily overlay env vars (restored on exit, including unset)."""
    saved = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _reject_wire_configs(config: FaultConfig) -> None:
    if config.drop_connection_after is not None or config.malformed_response_rate > 0:
        raise ValueError(
            "wire-level fault knobs (drop_connection_after, "
            "malformed_response_rate) require the StdioProxy harness "
            "(tests/conformance/helpers.py) and are not part of the default "
            "M2.3b reliability experiment (SPEC.md §21)"
        )


@asynccontextmanager
async def default_session_factory(
    sdk: str, fault_env: dict[str, str]
) -> AsyncIterator[ReliabilitySession]:
    """Spawn a fresh server subprocess with the fault env applied and connect
    the benchmark adapter for `sdk` (SPEC.md §21/§23).

    official/fastmcp: the adapters merge the env over the SDK default
    subprocess environment. adk: AdkAdapter builds its server env from
    os.environ and the server reads the fault config once at startup, so
    scoping the overlay to connect() suffices (mirrors the M2.3a ADK tests).
    """
    if sdk not in _ADAPTER_CLASSES:
        raise ValueError(f"unknown sdk {sdk!r}; expected one of {sorted(_ADAPTER_CLASSES)}")
    adapter_cls = _ADAPTER_CLASSES[sdk]
    if adapter_cls is None:
        reason = ADAPTER_UNAVAILABLE_REASONS.get(f"{sdk.capitalize()}Adapter", "unknown")
        raise RuntimeError(
            f"{sdk} adapter unavailable in this environment: {reason} — see DECISIONS.md D1"
        )
    if sdk == "adk":
        with _patched_environ(fault_env):
            adapter: MCPAdapter = adapter_cls()
            discovery = await adapter.connect()
    else:
        adapter = adapter_cls(env=fault_env)
        discovery = await adapter.connect()
    try:
        # Same agent tool surface as the M1 sweep: discovered tools plus
        # host-mediated pseudo-tools where the candidate exposes the
        # resource/prompt primitives (SPEC.md §23 — same MCP schemas).
        agent_tools: list[ToolSpec] = list(discovery.tools)
        if discovery.resources:
            agent_tools.append(READ_RESOURCE_SPEC)
        if discovery.prompts:
            agent_tools.append(GET_PROMPT_SPEC)
        yield ReliabilitySession(adapter=adapter, tools=agent_tools)
    finally:
        await adapter.close()


# ---- fault-aware grading (outcome + side effects; SPEC.md §21) ----


def _plain(value: Any) -> Any:
    """Normalize enum statuses to their string value before comparing."""
    return value.value if isinstance(value, Enum) else value


def _answer_ok(task: BenchmarkTask, final_answer: str | None) -> bool:
    """M1 answer semantics (answer_contains / forbidden_contains, both
    case-insensitive substrings) as an outcome check — same rule as
    evals.graders._answer_quality, without the trajectory coupling."""
    required = task.answer_contains
    forbidden = task.forbidden_contains
    if not isinstance(final_answer, str):
        return not required and not forbidden
    low = final_answer.lower()
    return all(s.lower() in low for s in required) and not any(
        s.lower() in low for s in forbidden
    )


def _is_injected_fault(entry: dict) -> bool:
    """True when an errored call carries a canonical injected-fault message
    (INJECTED_FAULT is a prefix of the after-execution variant)."""
    text = entry.get("error_text") or ""
    return INJECTED_FAULT in text or INJECTED_TASK_FAILURE in text


def _protocol_errors(call_log: list[dict]) -> int:
    """Errored tool calls NOT attributable to the injected fault layer:
    transport failures, malformed frames, validation/domain rejections
    observed at the adapter boundary (SPEC.md §21 metric)."""
    return sum(
        1 for entry in call_log if entry["is_error"] and not _is_injected_fault(entry)
    )


async def _read_unfaulted(
    call: Callable[[], Any], *, attempts: int = 8
) -> ToolResult | None:
    """Idempotent grading read that retries through injected faults.

    Grading must not be flaked by the fault layer it is measuring: the
    grading reads (get_ticket / get_inventory) are side-effect-free, so
    retrying an injected failure is exact, and the extra fault draws stay
    deterministic because the per-run seed is fixed. A NON-injected error
    (e.g. ticket not found) is a real outcome and is returned as-is."""
    last: ToolResult | None = None
    for _ in range(attempts):
        try:
            result: ToolResult = await call()
        except Exception:  # noqa: BLE001 — adapter failure fails the check, never the grader
            return None
        if not result.is_error:
            return result
        last = result
        if not _is_injected_fault({"error_text": result.text}):
            return result
    return last


def _deployment_from_call_log(service: str, call_log: list[dict]) -> dict | None:
    """Final deployment state as observable through the tool surface: the
    LAST successful deploy_service result for the service (deploy_service
    returns the post-mutation Deployment, so the last success is the final
    state). Used only when the world is not observable in-process."""
    entity: dict | None = None
    for entry in call_log:
        if entry["name"] != "deploy_service" or entry["is_error"]:
            continue
        if (entry.get("arguments") or {}).get("service") != service:
            continue
        content = entry.get("structured_content") or {}
        entity = content.get("deployment")
    return entity


async def _read_entity(
    kind: str,
    key: str,
    adapter: MCPAdapter,
    world: World | None,
    call_log: list[dict],
) -> dict | None:
    """Read one final-state entity: exactly from the in-process world when
    observable, else through the tool surface (fault-tolerant reads)."""
    if world is not None:
        if kind == "ticket":
            ticket = world.tickets.get(key)
            return ticket.model_dump(mode="json") if ticket is not None else None
        if kind == "inventory":
            item = world.inventory.get(key)
            return item.model_dump(mode="json") if item is not None else None
        if kind == "deployment":
            deployment = world.deployments.get(key)
            return deployment.model_dump(mode="json") if deployment is not None else None
        return None
    if kind == "ticket":
        res = await _read_unfaulted(lambda: adapter.call_tool("get_ticket", {"ticket_id": key}))
        if res is None or res.is_error or res.structured_content is None:
            return None
        return res.structured_content.get("ticket")
    if kind == "inventory":
        res = await _read_unfaulted(lambda: adapter.call_tool("get_inventory", {}))
        if res is None or res.is_error or res.structured_content is None:
            return None
        items = res.structured_content.get("items") or {}
        return items.get(key)
    if kind == "deployment":
        # No read tool for deployments exists in the contract; the last
        # successful deploy_service result is the observable final state.
        return _deployment_from_call_log(key, call_log)
    return None


async def _final_state_ok(
    task: BenchmarkTask,
    adapter: MCPAdapter,
    world: World | None,
    call_log: list[dict],
) -> bool:
    """expected_final_state check (SPEC.md §10 outcome). Supports the M1
    ticket:/inventory: check kinds plus deployment: for Category E."""
    for check_id, fields in task.expected_final_state.items():
        kind, _, key = check_id.partition(":")
        entity = await _read_entity(kind, key, adapter, world, call_log)
        if not isinstance(entity, dict):
            return False
        for field, expected in fields.items():
            if _plain(entity.get(field)) != expected:
                return False
    return True


def _duplicate_side_effects(
    task: BenchmarkTask, world: World | None, call_log: list[dict]
) -> int:
    """Tickets created for the task's idempotency key minus the expected one
    (SPEC.md §21: a retried create must not create two tickets).

    0 for tasks without a create_ticket expectation. With an in-process
    world the count is exact; through a subprocess server the world is not
    observable, so count DISTINCT ticket ids across SUCCESSFUL create_ticket
    calls carrying the key — an idempotent replay returns the same id, so a
    second distinct id is a true duplicate side effect.
    """
    key = (task.expected_args.get("create_ticket") or {}).get("idempotency_key")
    if key is None:
        return 0
    if world is not None:
        count = sum(1 for t in world.tickets.values() if t.idempotency_key == key)
        return max(0, count - 1)
    ticket_ids = {
        (entry.get("arguments") or {}).get("ticket_id")
        for entry in call_log
        if entry["name"] == "create_ticket"
        and not entry["is_error"]
        and (entry.get("arguments") or {}).get("idempotency_key") == key
    }
    return max(0, len(ticket_ids) - 1)


async def grade_failure_run(
    task: BenchmarkTask,
    result: dict,
    call_log: list[dict],
    adapter: MCPAdapter,
    world: World | None,
    fault_config: FaultConfig,
) -> dict:
    """Grade one Category-E run on outcome + side effects (SPEC.md §21).

    Never raises — a failed run is data, not a crash. Unobservable metrics
    are null (honest absence), never a fabricated 0.
    """
    try:
        error = result.get("error")
        answer_ok = _answer_ok(task, result.get("final_answer"))
        final_state_ok = await _final_state_ok(task, adapter, world, call_log)
        duplicates = _duplicate_side_effects(task, world, call_log)
        injected = sum(
            1 for entry in call_log if entry["is_error"] and _is_injected_fault(entry)
        )
        # A fault FIRED when an injected fault was observed on the wire, or
        # the fixed latency knob was applied to at least one call.
        faults_fired = injected > 0 or (fault_config.latency_ms > 0 and len(call_log) > 0)
        task_success = bool(
            error is None and answer_ok and final_state_ok and duplicates == 0
        )
        return {
            "task_success": task_success,
            # recovery: faults were active (fired) AND the task succeeded.
            "recovery": bool(faults_fired and task_success),
            "faults_fired": faults_fired,
            "injected_faults": injected,
            "tool_call_count": len(call_log),
            "retry_count": retry_count(call_log),
            "duplicate_side_effects": duplicates,
            "incorrect_final_state": not final_state_ok,
            "protocol_errors": _protocol_errors(call_log),
            "answer_ok": answer_ok,
            "error": error,
        }
    except Exception as err:  # noqa: BLE001 — grading must be total
        return {
            "task_success": False,
            "recovery": None,
            "faults_fired": None,
            "injected_faults": None,
            "tool_call_count": len(call_log),
            "retry_count": None,
            "duplicate_side_effects": None,
            "incorrect_final_state": None,
            "protocol_errors": None,
            "answer_ok": None,
            "error": f"{type(err).__name__}: {err}",
        }


def aggregate_runs(records: list[dict]) -> dict[str, Any]:
    """Aggregate per-run records of one (task, sdk, config) cell into the
    SPEC.md §21 metrics. Nulls (unobserved) are skipped, never zeroed;
    recovery_probability is null with a reason when no fault fired."""
    n = len(records)
    if n == 0:
        return {"n": 0}

    def mean(field: str) -> float | None:
        values = [r[field] for r in records if isinstance(r.get(field), (int, float))]
        return round(sum(values) / len(values), 4) if values else None

    def positive_rate(field: str) -> float | None:
        observed = [r for r in records if r.get(field) is not None]
        if not observed:
            return None
        return round(sum(1 for r in observed if r[field] > 0) / len(observed), 4)

    fired = [r for r in records if r.get("faults_fired")]
    recovered = [r for r in fired if r.get("recovery")]
    recovery_probability: float | None = None
    recovery_reason: str | None = "no fault fired in any run (denominator 0)"
    if fired:
        recovery_probability = round(len(recovered) / len(fired), 4)
        recovery_reason = None
    return {
        "n": n,
        "success_rate": mean("task_success"),
        "faults_fired_runs": len(fired),
        "recovery_probability": recovery_probability,
        "recovery_probability_reason": recovery_reason,
        "mean_retries": mean("retry_count"),
        "duplicate_side_effect_rate": positive_rate("duplicate_side_effects"),
        "incorrect_final_state_rate": mean("incorrect_final_state"),
        "protocol_error_rate": positive_rate("protocol_errors"),
        "mean_tool_call_count": mean("tool_call_count"),
    }


async def run_reliability(
    tasks: list[BenchmarkTask],
    sdk: str,
    fault_config: FaultConfig,
    n_runs: int,
    *,
    session_factory: SessionFactory | None = None,
    model: BaseChatModel | None = None,
    model_factory: ModelFactory | None = None,
) -> list[dict]:
    """Run the reliability experiment for one (sdk, fault_config) cell
    (SPEC.md §21). Returns one record per (task, run_index).

    Task order and run order are fixed (SPEC.md §23); each run gets a fresh
    server session and a deterministic per-run FAULT_SEED. `session_factory`
    and `model_factory` are the hermetic-test seam: the default factory
    spawns real server subprocesses and the default model is build_model()
    (constructed once per experiment — agent identity pinned, §23); tests
    inject an in-process world and a scripted fake model instead.
    """
    if n_runs < 1:
        raise ValueError(f"n_runs must be >= 1, got {n_runs}")
    if session_factory is None:
        _reject_wire_configs(fault_config)
        session_factory = default_session_factory
    if model is None and model_factory is None:
        model = build_model()  # once per experiment — agent identity pinned (§23)
    label = fault_config_label(fault_config)
    records: list[dict] = []
    for task in tasks:
        for run_index in range(n_runs):
            seed = run_seed(task.id, sdk, run_index)
            env = fault_env_for(fault_config, seed)
            async with session_factory(sdk, env) as session:
                recording = AccessRecordingAdapter(session.adapter)
                run_model = (
                    model_factory(task.id, run_index) if model_factory is not None else model
                )
                graph = build_agent(session.tools, recording, model=run_model)
                task_dict = {**task.model_dump(), "sdk": sdk}
                result = await run_task(task_dict, recording, graph)
                record = await grade_failure_run(
                    task,
                    result,
                    recording.call_log,
                    session.adapter,
                    session.world,
                    fault_config,
                )
                record.update(
                    {
                        "task_id": task.id,
                        "sdk": sdk,
                        "fault_config_label": label,
                        "run_index": run_index,
                        "fault_seed": seed,
                    }
                )
                records.append(record)
    return records
