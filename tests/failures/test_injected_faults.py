"""Injected-fault behavior per candidate — SPEC.md §21 (M2.3a).

Deterministic only: no LLM calls. Every server subprocess is configured via
the SPEC.md §21 env vars (read once at startup) through the M2.3a faulty
session factories; wire-level faults go through the StdioProxy "pass" mode
driven by the same env config + FAULT_SEED.

Candidate coverage (one independent variable — identical fault semantics):
- official: OFFICIAL_SESSION_FAULTY (tool-level) + pass-proxy (wire-level).
- fastmcp: FAST_MCP_SESSION_FAULTY (tool-level) + pass-proxy (wire-level).
- adk: ADK_ADAPTER_SESSION with monkeypatched env (the adapter's spawned
  server inherits os.environ). Wire-level faults do NOT apply: McpToolset
  manages the channel with no wire access — harness limitation, classified
  and skipped as such.

World-state assertions: each session owns a world inside its server
subprocess, so exact post-fault world state is verified through the SAME
shared dispatch helper (run_tool_with_faults) against a real World
in-process, while the sessions assert the client-visible error shapes.

Error-shape findings (verified against mcp 2.1.1 / fastmcp 4.0.2):
- official: injected faults arrive as isError=True CallToolResult; a
  malformed wire frame surfaces via the message_handler plus a read-timeout
  MCPError on the pending call; a dropped connection raises MCPError
  "Connection closed".
- fastmcp: same wire behavior (its client inherits the mcp 2.x session);
  call_tool uses raise_on_error=False to observe isError results.
"""
from __future__ import annotations

import time
from typing import Any

import pytest
from helpers import (  # ty: ignore[unresolved-import] — tests/conformance/helpers.py, on sys.path via conftest.py
    ADK_ADAPTER_SESSION,
    FAST_MCP_SESSION_FAULTY,
    OFFICIAL_SESSION_FAULTY,
    PROBE_TOOL,
    MCPError,
    ProxyFault,
    probe_arguments,
    sdk_defect,
)

from mcp_sdk_bench.faults import (
    FaultConfig,
    FaultEngine,
    InjectedToolFault,
    load_fault_config,
    run_tool_with_faults,
)
from mcp_sdk_bench.world import reset_world

#: SPEC.md §21 env configs (FAULT_SEED pinned everywhere; determinism).
FAIL_BEFORE_ENV = {"FAIL_TOOL_CALL": "1.0", "FAIL_PHASE": "before", "FAULT_SEED": "42"}
FAIL_AFTER_ENV = {"FAIL_TOOL_CALL": "1.0", "FAIL_PHASE": "after", "FAULT_SEED": "42"}
LATENCY_ENV = {"LATENCY_MS": "200", "FAULT_SEED": "42"}
TASK_FAILURE_ENV = {"TASK_FAILURE_RATE": "1.0", "FAULT_SEED": "42"}
MALFORMED_ALL_ENV = {"MALFORMED_RESPONSE_RATE": "1.0", "FAULT_SEED": "42"}
MALFORMED_HALF_ENV = {"MALFORMED_RESPONSE_RATE": "0.5", "FAULT_SEED": "123"}
DROP_ENV = {"DROP_CONNECTION_AFTER": "2", "FAULT_SEED": "42"}

CREATE_ARGS = {"ticket_id": "T-1", "title": "Fault probe", "idempotency_key": "K-1"}

#: A deploy that would succeed without injection (payments-api is seeded in
#: staging), so the error can only come from TASK_FAILURE_RATE.
STAGING_DEPLOY_ARGS = {
    "service": "payments-api",
    "target_version": "2.4.1",
    "environment": "staging",
}

#: Seed availability of thinkpad-t14 (fixtures.py); proves the world is
#: unchanged after a fault-rejected mutation.
SEED_THINKPAD_AVAILABLE = 2

LATENCY_SECONDS = 0.2


def _text(result: Any) -> str:
    content = getattr(result, "content", None) or []
    return "\n".join(
        block.text for block in content if getattr(block, "type", None) == "text"
    )


def _exception_recorder() -> tuple[Any, list[Exception]]:
    """FastMCP MessageHandler subclass recording transport-level exceptions
    (mirrors tests/conformance/test_errors.py)."""
    from fastmcp.client.messages import MessageHandler

    class _ExceptionRecorder(MessageHandler):
        def __init__(self) -> None:
            self.exceptions: list[Exception] = []

        async def on_exception(self, message: Exception) -> None:
            self.exceptions.append(message)

    recorder = _ExceptionRecorder()
    return recorder, recorder.exceptions


async def _assert_fail_before_world_semantics() -> None:
    """World-level half of the fail-BEFORE verdict, run through the same
    shared dispatch helper the three servers call: a rejected mutation
    applies NO side effect (SPEC.md §21)."""
    world = reset_world()
    engine = FaultEngine(load_fault_config(FAIL_BEFORE_ENV))
    with pytest.raises(InjectedToolFault, match="injected fault"):
        await run_tool_with_faults(
            engine, lambda: world.reserve_inventory("thinkpad-t14", "alice")
        )
    assert world.inventory["thinkpad-t14"].available == SEED_THINKPAD_AVAILABLE
    assert world.op_log == []


# ---- official ----


async def test_official_fail_before_every_call_errors_world_unchanged() -> None:
    candidate = "official"
    async with OFFICIAL_SESSION_FAULTY(fault_env=FAIL_BEFORE_ENV) as session:
        probe = await session.call_tool(PROBE_TOOL, probe_arguments())
        assert probe.is_error, sdk_defect(candidate, "fail-before probe did not error")
        assert "injected fault" in _text(probe), sdk_defect(
            candidate, f"unexpected fail-before message: {_text(probe)!r}"
        )
        reserved = await session.call_tool(
            "reserve_inventory", {"item": "thinkpad-t14", "employee_id": "alice"}
        )
        assert reserved.is_error, sdk_defect(candidate, "fail-before reserve did not error")
        assert "injected fault" in _text(reserved)
    await _assert_fail_before_world_semantics()


async def test_official_fail_after_applies_side_effect() -> None:
    candidate = "official"
    async with OFFICIAL_SESSION_FAULTY(fault_env=FAIL_AFTER_ENV) as session:
        first = await session.call_tool("create_ticket", CREATE_ARGS)
        assert first.is_error, sdk_defect(candidate, "fail-after create did not error")
        assert "injected fault after execution" in _text(first), sdk_defect(
            candidate, f"unexpected fail-after message: {_text(first)!r}"
        )
        # The idempotent replay bypasses the fault layer (no transaction), so
        # its success PROVES the first call's side effect was applied.
        replay = await session.call_tool("create_ticket", CREATE_ARGS)
        assert not replay.is_error, sdk_defect(candidate, "idempotent replay errored")
        assert replay.structured_content is not None
        assert replay.structured_content["ticket"]["id"] == "T-1"


async def test_official_latency_ms_adds_deterministic_delay() -> None:
    candidate = "official"
    async with OFFICIAL_SESSION_FAULTY(fault_env=LATENCY_ENV) as session:
        start = time.monotonic()
        result = await session.call_tool(PROBE_TOOL, probe_arguments())
        elapsed = time.monotonic() - start
        assert not result.is_error, sdk_defect(candidate, "latent call errored")
        assert elapsed >= LATENCY_SECONDS, sdk_defect(
            candidate, f"LATENCY_MS=200 not applied (call took {elapsed:.3f}s)"
        )
        assert elapsed < 10.0, f"HARNESS ISSUE [{candidate}]: call hung {elapsed:.3f}s"


async def test_official_task_failure_rate_raises_world_error() -> None:
    candidate = "official"
    async with OFFICIAL_SESSION_FAULTY(fault_env=TASK_FAILURE_ENV) as session:
        result = await session.call_tool("deploy_service", STAGING_DEPLOY_ARGS)
        assert result.is_error, sdk_defect(candidate, "task-failure deploy did not error")
        assert "injected task failure" in _text(result), sdk_defect(
            candidate, f"unexpected task-failure message: {_text(result)!r}"
        )


async def test_official_malformed_response_rate_1_surfaces_protocol_error() -> None:
    candidate = "official"
    collected: list[Exception] = []

    async def handler(message: object) -> None:
        if isinstance(message, Exception):
            collected.append(message)

    fault = ProxyFault(mode="pass")
    async with OFFICIAL_SESSION_FAULTY(
        fault_env=MALFORMED_ALL_ENV, fault=fault, message_handler=handler
    ) as session:
        with pytest.raises(MCPError, match="timed out"):
            await session.call_tool(
                PROBE_TOOL, probe_arguments(), read_timeout_seconds=2.0
            )
        assert collected, sdk_defect(
            candidate, "malformed frame never surfaced to the message_handler"
        )


async def test_official_drop_connection_after_2_third_call_fails() -> None:
    fault = ProxyFault(mode="pass")
    async with OFFICIAL_SESSION_FAULTY(fault_env=DROP_ENV, fault=fault) as session:
        first = await session.call_tool("get_ticket", {"ticket_id": "PAY-123"})
        assert not first.is_error
        second = await session.call_tool("get_ticket", {"ticket_id": "PAY-123"})
        assert not second.is_error
        with pytest.raises(MCPError, match="Connection closed"):
            await session.call_tool("get_ticket", {"ticket_id": "PAY-123"})


async def _official_malformed_pattern(calls: int) -> list[bool]:
    """True per call whose tools/call response frame was corrupted."""
    fault = ProxyFault(mode="pass")
    pattern: list[bool] = []
    async with OFFICIAL_SESSION_FAULTY(fault_env=MALFORMED_HALF_ENV, fault=fault) as session:
        for _ in range(calls):
            try:
                result = await session.call_tool(
                    PROBE_TOOL, probe_arguments(), read_timeout_seconds=1.0
                )
            except MCPError:
                pattern.append(True)
            else:
                assert not result.is_error
                pattern.append(False)
    return pattern


async def test_official_same_seed_same_fault_sequence() -> None:
    candidate = "official"
    calls = 5
    first = await _official_malformed_pattern(calls)
    second = await _official_malformed_pattern(calls)
    assert first == second, sdk_defect(
        candidate, f"same seed gave different fault sequences: {first} vs {second}"
    )
    # The proxy's FaultEngine must reproduce the in-process prediction.
    engine = FaultEngine(FaultConfig(malformed_response_rate=0.5, seed=123))
    predicted = [engine.next_malformed() for _ in range(calls)]
    assert first == predicted, (
        f"HARNESS ISSUE [{candidate}]: proxy sequence {first} != FaultEngine "
        f"prediction {predicted}"
    )
    assert any(first) and not all(first), (
        f"HARNESS ISSUE [{candidate}]: rate 0.5 sequence is degenerate: {first}"
    )


# ---- fastmcp ----


async def test_fastmcp_fail_before_every_call_errors_world_unchanged() -> None:
    candidate = "fastmcp"
    async with FAST_MCP_SESSION_FAULTY(fault_env=FAIL_BEFORE_ENV) as client:
        probe = await client.call_tool(PROBE_TOOL, probe_arguments(), raise_on_error=False)
        assert probe.is_error, sdk_defect(candidate, "fail-before probe did not error")
        assert "injected fault" in _text(probe), sdk_defect(
            candidate, f"unexpected fail-before message: {_text(probe)!r}"
        )
        reserved = await client.call_tool(
            "reserve_inventory",
            {"item": "thinkpad-t14", "employee_id": "alice"},
            raise_on_error=False,
        )
        assert reserved.is_error, sdk_defect(candidate, "fail-before reserve did not error")
        assert "injected fault" in _text(reserved)
    await _assert_fail_before_world_semantics()


async def test_fastmcp_fail_after_applies_side_effect() -> None:
    candidate = "fastmcp"
    async with FAST_MCP_SESSION_FAULTY(fault_env=FAIL_AFTER_ENV) as client:
        first = await client.call_tool("create_ticket", CREATE_ARGS, raise_on_error=False)
        assert first.is_error, sdk_defect(candidate, "fail-after create did not error")
        assert "injected fault after execution" in _text(first), sdk_defect(
            candidate, f"unexpected fail-after message: {_text(first)!r}"
        )
        replay = await client.call_tool("create_ticket", CREATE_ARGS, raise_on_error=False)
        assert not replay.is_error, sdk_defect(candidate, "idempotent replay errored")
        assert replay.structured_content is not None
        assert replay.structured_content["ticket"]["id"] == "T-1"


async def test_fastmcp_latency_ms_adds_deterministic_delay() -> None:
    candidate = "fastmcp"
    async with FAST_MCP_SESSION_FAULTY(fault_env=LATENCY_ENV) as client:
        start = time.monotonic()
        result = await client.call_tool(PROBE_TOOL, probe_arguments())
        elapsed = time.monotonic() - start
        assert not result.is_error, sdk_defect(candidate, "latent call errored")
        assert elapsed >= LATENCY_SECONDS, sdk_defect(
            candidate, f"LATENCY_MS=200 not applied (call took {elapsed:.3f}s)"
        )
        assert elapsed < 10.0, f"HARNESS ISSUE [{candidate}]: call hung {elapsed:.3f}s"


async def test_fastmcp_task_failure_rate_raises_world_error() -> None:
    candidate = "fastmcp"
    async with FAST_MCP_SESSION_FAULTY(fault_env=TASK_FAILURE_ENV) as client:
        result = await client.call_tool(
            "deploy_service", STAGING_DEPLOY_ARGS, raise_on_error=False
        )
        assert result.is_error, sdk_defect(candidate, "task-failure deploy did not error")
        assert "injected task failure" in _text(result), sdk_defect(
            candidate, f"unexpected task-failure message: {_text(result)!r}"
        )


async def test_fastmcp_malformed_response_rate_1_surfaces_protocol_error() -> None:
    candidate = "fastmcp"
    recorder, recorded = _exception_recorder()
    fault = ProxyFault(mode="pass")
    async with FAST_MCP_SESSION_FAULTY(
        fault_env=MALFORMED_ALL_ENV, fault=fault, message_handler=recorder
    ) as client:
        with pytest.raises(MCPError, match="timed out"):
            await client.call_tool(PROBE_TOOL, probe_arguments(), timeout=2.0)
        assert recorded, sdk_defect(
            candidate, "malformed frame never surfaced to MessageHandler.on_exception"
        )


async def test_fastmcp_drop_connection_after_2_third_call_fails() -> None:
    fault = ProxyFault(mode="pass")
    async with FAST_MCP_SESSION_FAULTY(fault_env=DROP_ENV, fault=fault) as client:
        first = await client.call_tool("get_ticket", {"ticket_id": "PAY-123"})
        assert not first.is_error
        second = await client.call_tool("get_ticket", {"ticket_id": "PAY-123"})
        assert not second.is_error
        with pytest.raises(MCPError, match="Connection closed"):
            await client.call_tool("get_ticket", {"ticket_id": "PAY-123"})


async def _fastmcp_malformed_pattern(calls: int) -> list[bool]:
    fault = ProxyFault(mode="pass")
    pattern: list[bool] = []
    async with FAST_MCP_SESSION_FAULTY(fault_env=MALFORMED_HALF_ENV, fault=fault) as client:
        for _ in range(calls):
            try:
                result = await client.call_tool(
                    PROBE_TOOL, probe_arguments(), timeout=1.0, raise_on_error=False
                )
            except MCPError:
                pattern.append(True)
            else:
                assert not result.is_error
                pattern.append(False)
    return pattern


async def test_fastmcp_same_seed_same_fault_sequence() -> None:
    candidate = "fastmcp"
    calls = 5
    first = await _fastmcp_malformed_pattern(calls)
    second = await _fastmcp_malformed_pattern(calls)
    assert first == second, sdk_defect(
        candidate, f"same seed gave different fault sequences: {first} vs {second}"
    )
    engine = FaultEngine(FaultConfig(malformed_response_rate=0.5, seed=123))
    predicted = [engine.next_malformed() for _ in range(calls)]
    assert first == predicted, (
        f"HARNESS ISSUE [{candidate}]: proxy sequence {first} != FaultEngine "
        f"prediction {predicted}"
    )
    assert any(first) and not all(first), (
        f"HARNESS ISSUE [{candidate}]: rate 0.5 sequence is degenerate: {first}"
    )


# ---- adk (adapter driving surface; tool-level faults only) ----


async def test_adk_fail_before_every_call_errors_world_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = "adk"
    for key, value in FAIL_BEFORE_ENV.items():
        monkeypatch.setenv(key, value)
    async with ADK_ADAPTER_SESSION() as (adapter, _):
        probe = await adapter.call_tool(PROBE_TOOL, probe_arguments())
        assert probe.is_error, sdk_defect(candidate, "fail-before probe did not error")
        assert probe.text is not None and "injected fault" in probe.text, sdk_defect(
            candidate, f"unexpected fail-before message: {probe.text!r}"
        )
        reserved = await adapter.call_tool(
            "reserve_inventory", {"item": "thinkpad-t14", "employee_id": "alice"}
        )
        assert reserved.is_error, sdk_defect(candidate, "fail-before reserve did not error")
        assert reserved.text is not None and "injected fault" in reserved.text
    await _assert_fail_before_world_semantics()


async def test_adk_fail_after_applies_side_effect(monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = "adk"
    for key, value in FAIL_AFTER_ENV.items():
        monkeypatch.setenv(key, value)
    async with ADK_ADAPTER_SESSION() as (adapter, _):
        first = await adapter.call_tool("create_ticket", CREATE_ARGS)
        assert first.is_error, sdk_defect(candidate, "fail-after create did not error")
        assert first.text is not None and "injected fault after execution" in first.text, (
            sdk_defect(candidate, f"unexpected fail-after message: {first.text!r}")
        )
        replay = await adapter.call_tool("create_ticket", CREATE_ARGS)
        assert not replay.is_error, sdk_defect(candidate, "idempotent replay errored")
        assert replay.structured_content is not None
        assert replay.structured_content["ticket"]["id"] == "T-1"


async def test_adk_latency_ms_adds_deterministic_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = "adk"
    for key, value in LATENCY_ENV.items():
        monkeypatch.setenv(key, value)
    async with ADK_ADAPTER_SESSION() as (adapter, _):
        start = time.monotonic()
        result = await adapter.call_tool(PROBE_TOOL, probe_arguments())
        elapsed = time.monotonic() - start
        assert not result.is_error, sdk_defect(candidate, "latent call errored")
        assert elapsed >= LATENCY_SECONDS, sdk_defect(
            candidate, f"LATENCY_MS=200 not applied (call took {elapsed:.3f}s)"
        )
        assert elapsed < 30.0, f"HARNESS ISSUE [{candidate}]: call hung {elapsed:.3f}s"


async def test_adk_task_failure_rate_raises_world_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = "adk"
    for key, value in TASK_FAILURE_ENV.items():
        monkeypatch.setenv(key, value)
    async with ADK_ADAPTER_SESSION() as (adapter, _):
        result = await adapter.call_tool("deploy_service", STAGING_DEPLOY_ARGS)
        assert result.is_error, sdk_defect(candidate, "task-failure deploy did not error")
        assert result.text is not None and "injected task failure" in result.text, (
            sdk_defect(candidate, f"unexpected task-failure message: {result.text!r}")
        )


async def test_adk_wire_level_faults_are_a_harness_limitation() -> None:
    pytest.skip(
        "HARNESS LIMITATION [adk]: McpToolset drives the server over an "
        "SDK-managed channel with no wire access, so MALFORMED_RESPONSE_RATE "
        "and DROP_CONNECTION_AFTER injection is impossible for this "
        "candidate (SPEC.md §21 wire-level faults are official/fastmcp only)"
    )
