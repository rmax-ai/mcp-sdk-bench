"""Unit tests for the deterministic fault engine — SPEC.md §21 (M2.3a).

Covers FaultConfig validation, env loading, and the FaultEngine determinism
contract: same seed + same config = identical fault sequence, with no
wall-clock randomness anywhere.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_sdk_bench.faults import (
    DEFAULT_FAULT_SEED,
    FaultConfig,
    FaultEngine,
    load_fault_config,
)

DRAWS = 100


def _fail_sequence(engine: FaultEngine) -> list[bool]:
    return [engine.should_fail_call() for _ in range(DRAWS)]


def test_same_seed_same_config_gives_identical_sequences() -> None:
    config = FaultConfig(fail_tool_call=0.37, task_failure_rate=0.11, seed=7)
    first = FaultEngine(config)
    second = FaultEngine(config)
    assert _fail_sequence(first) == _fail_sequence(second)
    first = FaultEngine(config)
    second = FaultEngine(config)
    assert [first.task_failure() for _ in range(DRAWS)] == [
        second.task_failure() for _ in range(DRAWS)
    ]


def test_different_seeds_give_different_sequences() -> None:
    a = FaultEngine(FaultConfig(fail_tool_call=0.5, seed=1))
    b = FaultEngine(FaultConfig(fail_tool_call=0.5, seed=2))
    assert _fail_sequence(a) != _fail_sequence(b)


def test_probability_zero_never_fails() -> None:
    engine = FaultEngine(FaultConfig(fail_tool_call=0.0, task_failure_rate=0.0))
    assert not any(_fail_sequence(engine))
    assert not any(engine.task_failure() for _ in range(DRAWS))


def test_probability_one_always_fails() -> None:
    engine = FaultEngine(FaultConfig(fail_tool_call=1.0, task_failure_rate=1.0))
    assert all(_fail_sequence(engine))
    assert all(engine.task_failure() for _ in range(DRAWS))


def test_malformed_rate_bounds() -> None:
    never = FaultEngine(FaultConfig(malformed_response_rate=0.0))
    always = FaultEngine(FaultConfig(malformed_response_rate=1.0))
    assert not any(never.next_malformed() for _ in range(DRAWS))
    assert all(always.next_malformed() for _ in range(DRAWS))


def test_latency_is_fixed_and_deterministic() -> None:
    engine = FaultEngine(FaultConfig(latency_ms=250))
    assert engine.latency() == 250
    assert engine.latency() == 250  # no jitter, no draw consumed


def test_drop_after_passthrough() -> None:
    assert FaultEngine(FaultConfig()).drop_after() is None
    assert FaultEngine(FaultConfig(drop_connection_after=3)).drop_after() == 3


async def test_apply_latency_zero_is_a_noop() -> None:
    engine = FaultEngine(FaultConfig())
    await engine.apply_latency()  # must return immediately, no sleep


def test_default_config_is_inert() -> None:
    config = FaultConfig()
    assert config.fail_tool_call == 0.0
    assert config.fail_phase == "before"
    assert config.latency_ms == 0
    assert config.drop_connection_after is None
    assert config.malformed_response_rate == 0.0
    assert config.task_failure_rate == 0.0
    assert config.seed == DEFAULT_FAULT_SEED


def test_load_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAIL_TOOL_CALL", "0.25")
    monkeypatch.setenv("FAIL_PHASE", "after")
    monkeypatch.setenv("LATENCY_MS", "150")
    monkeypatch.setenv("DROP_CONNECTION_AFTER", "3")
    monkeypatch.setenv("MALFORMED_RESPONSE_RATE", "0.05")
    monkeypatch.setenv("TASK_FAILURE_RATE", "0.1")
    monkeypatch.setenv("FAULT_SEED", "99")
    config = load_fault_config()
    assert config.fail_tool_call == 0.25
    assert config.fail_phase == "after"
    assert config.latency_ms == 150
    assert config.drop_connection_after == 3
    assert config.malformed_response_rate == 0.05
    assert config.task_failure_rate == 0.1
    assert config.seed == 99


def test_load_defaults_when_env_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "FAIL_TOOL_CALL",
        "FAIL_PHASE",
        "LATENCY_MS",
        "DROP_CONNECTION_AFTER",
        "MALFORMED_RESPONSE_RATE",
        "TASK_FAILURE_RATE",
        "FAULT_SEED",
    ):
        monkeypatch.delenv(var, raising=False)
    assert load_fault_config() == FaultConfig()


def test_fail_phase_parsing() -> None:
    assert load_fault_config({"FAIL_PHASE": "before"}).fail_phase == "before"
    assert load_fault_config({"FAIL_PHASE": "after"}).fail_phase == "after"
    with pytest.raises(ValidationError):
        load_fault_config({"FAIL_PHASE": "during"})


def test_out_of_range_probabilities_rejected() -> None:
    with pytest.raises(ValidationError):
        load_fault_config({"FAIL_TOOL_CALL": "1.5"})
    with pytest.raises(ValidationError):
        load_fault_config({"TASK_FAILURE_RATE": "-0.1"})
    with pytest.raises(ValidationError):
        load_fault_config({"MALFORMED_RESPONSE_RATE": "2"})
    with pytest.raises(ValidationError):
        FaultConfig(fail_tool_call=-0.01)


def test_negative_latency_rejected() -> None:
    with pytest.raises(ValidationError):
        load_fault_config({"LATENCY_MS": "-5"})


def test_unparseable_values_rejected() -> None:
    with pytest.raises(ValueError, match="FAIL_TOOL_CALL"):
        load_fault_config({"FAIL_TOOL_CALL": "often"})
    with pytest.raises(ValueError, match="LATENCY_MS"):
        load_fault_config({"LATENCY_MS": "soon"})
    with pytest.raises(ValueError, match="FAULT_SEED"):
        load_fault_config({"FAULT_SEED": "abc"})
