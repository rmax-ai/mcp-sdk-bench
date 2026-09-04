"""Deterministic fault injection — SPEC.md §21 (M2.3a).

One shared fault layer used identically by all three server variants, so the
MCP integration under test is the only independent variable (SPEC.md §23):
same env-var configuration, same seeded RNG, same decision points in the
tool-dispatch path.

Configuration is read ONCE at server startup from env vars (all optional):

    FAIL_TOOL_CALL            probability a tool call fails       (default 0)
    FAIL_PHASE                before|after                        (default before)
                              before: fail without executing (no side effect)
                              after:  execute, THEN report failure (the
                                      idempotency probe — side effect applied,
                                      error returned)
    LATENCY_MS                fixed latency added to every call   (default 0)
    TASK_FAILURE_RATE         probability the world transaction
                              raises WorldError after execution   (default 0)
    DROP_CONNECTION_AFTER     drop the connection after the Nth   (default None)
                              tools/call response (wire level)
    MALFORMED_RESPONSE_RATE   probability a tools/call response   (default 0)
                              frame is corrupted (wire level)
    FAULT_SEED                RNG seed                            (default 42)

Seeded determinism: one module-seeded ``random.Random`` per engine; same seed
+ same config + same call sequence = byte-identical fault sequence across
SDKs. No wall-clock randomness anywhere (the only sleep is the explicit
LATENCY_MS delay).

Tool-level faults (FAIL_TOOL_CALL / LATENCY_MS / TASK_FAILURE_RATE) are
applied server-side via run_tool_with_faults. Wire-level faults
(DROP_CONNECTION_AFTER / MALFORMED_RESPONSE_RATE) are applied by the
StdioProxy in tests/conformance/helpers.py, driven by the same env config and
the same FaultEngine, so identical seeds give identical sequences.
"""
from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Callable, Mapping
from typing import Literal

from pydantic import BaseModel, Field

from mcp_sdk_bench.world import WorldError

#: Canonical injected-fault messages (asserted verbatim by tests/failures).
INJECTED_FAULT = "injected fault"
INJECTED_FAULT_AFTER = "injected fault after execution"
INJECTED_TASK_FAILURE = "injected task failure"

DEFAULT_FAULT_SEED = 42


class InjectedToolFault(Exception):
    """A FAIL_TOOL_CALL injection (before- or after-execution phase).

    Distinct from WorldError: it simulates an infrastructure/transport-grade
    failure of the call itself, not a domain rejection."""


class FaultConfig(BaseModel):
    """Validated fault-injection configuration (SPEC.md §21)."""

    fail_tool_call: float = Field(default=0.0, ge=0.0, le=1.0)
    fail_phase: Literal["before", "after"] = "before"
    latency_ms: int = Field(default=0, ge=0)
    drop_connection_after: int | None = Field(default=None, ge=1)
    malformed_response_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    task_failure_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    seed: int = DEFAULT_FAULT_SEED


def _read(env: Mapping[str, str], name: str, parse: Callable[[str], object]) -> object | None:
    raw = env.get(name)
    if raw is None or raw == "":
        return None
    try:
        return parse(raw)
    except ValueError as err:
        raise ValueError(f"invalid value for {name}: {raw!r}") from err


def load_fault_config(env: Mapping[str, str] | None = None) -> FaultConfig:
    """Build a FaultConfig from env vars (defaults: os.environ). Unknown or
    missing vars fall back to FaultConfig defaults; malformed values raise
    ValueError, out-of-range values raise pydantic ValidationError."""
    source = os.environ if env is None else env
    values: dict[str, object] = {}
    parsed = {
        "fail_tool_call": _read(source, "FAIL_TOOL_CALL", float),
        "fail_phase": _read(source, "FAIL_PHASE", str),
        "latency_ms": _read(source, "LATENCY_MS", int),
        "drop_connection_after": _read(source, "DROP_CONNECTION_AFTER", int),
        "malformed_response_rate": _read(source, "MALFORMED_RESPONSE_RATE", float),
        "task_failure_rate": _read(source, "TASK_FAILURE_RATE", float),
        "seed": _read(source, "FAULT_SEED", int),
    }
    values = {key: value for key, value in parsed.items() if value is not None}
    return FaultConfig.model_validate(values)


class FaultEngine:
    """Seeded, deterministic fault decision-maker (SPEC.md §21).

    Every decision method consumes exactly one RNG draw per call, in the
    fixed order the dispatch path invokes them, so two engines built from
    the same config produce identical sequences.
    """

    def __init__(self, config: FaultConfig) -> None:
        self.config = config
        self._rng = random.Random(config.seed)

    def should_fail_call(self) -> bool:
        """FAIL_TOOL_CALL draw for one tool call."""
        return self._rng.random() < self.config.fail_tool_call

    def task_failure(self) -> bool:
        """TASK_FAILURE_RATE draw for one executed world transaction."""
        return self._rng.random() < self.config.task_failure_rate

    def latency(self) -> int:
        """Fixed added latency in ms (deterministic, no jitter, no draw)."""
        return self.config.latency_ms

    def next_malformed(self) -> bool:
        """MALFORMED_RESPONSE_RATE draw for one wire response frame."""
        return self._rng.random() < self.config.malformed_response_rate

    def drop_after(self) -> int | None:
        """Configured tools/call response count after which to drop."""
        return self.config.drop_connection_after

    async def apply_latency(self) -> None:
        """Sleep for the configured LATENCY_MS (the only sanctioned sleep in
        the fault layer; the delay is an explicit deterministic parameter)."""
        if self.config.latency_ms > 0:
            await asyncio.sleep(self.config.latency_ms / 1000)


async def run_tool_with_faults[T](
    engine: FaultEngine,
    execute: Callable[[], T],
    *,
    is_replay: Callable[[], bool] = lambda: False,
) -> T:
    """Run one tool execution through the shared fault layer (SPEC.md §21).

    Decision order (identical in all three server variants):

    1. is_replay(): an idempotent replay (create_ticket with an already-used
       idempotency_key) executes NO transaction, so no transaction fault
       applies — the replay returns the existing record unfaulted. This is
       what lets a client retry after an after-phase failure converge.
    2. FAIL_PHASE=before + should_fail_call() -> raise InjectedToolFault
       BEFORE execution (no side effect).
    3. apply_latency().
    4. execute() — the world transaction.
    5. FAIL_PHASE=after + should_fail_call() -> raise InjectedToolFault AFTER
       execution (side effect applied, failure reported — the SPEC.md §21
       idempotency probe).
    6. task_failure() -> raise WorldError(INJECTED_TASK_FAILURE) (the
       transaction executed, then failed at the task level).
    """
    if is_replay():
        return execute()  # replay: no transaction, no fault draws
    if engine.config.fail_phase == "before" and engine.should_fail_call():
        raise InjectedToolFault(INJECTED_FAULT)
    await engine.apply_latency()
    result = execute()
    if engine.config.fail_phase == "after" and engine.should_fail_call():
        raise InjectedToolFault(INJECTED_FAULT_AFTER)
    if engine.task_failure():
        raise WorldError(INJECTED_TASK_FAILURE)
    return result
