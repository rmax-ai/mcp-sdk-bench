"""CONCURRENCY conformance (SPEC.md §8 CONCURRENCY, M2.1).

1, 10, and 100 concurrent probe_schema calls on ONE session via
asyncio.gather. Every call uses distinct argument values, so a mismatched or
cross-wired response fails the echo assertion. probe_schema is
side-effect-free by design (no op_log entry); world-mutation concurrency is
M2.3, not this suite.

Driving surfaces (no faked symmetry):
- official: official SDK ClientSession over stdio (mcp 2.x dispatcher,
  concurrent pending requests).
- fastmcp: fastmcp.client.Client over stdio.
- adk: the benchmark AdkAdapter — ADK 2.8 ships no standalone protocol
  client, so the adapter IS the canonical driving surface. Any serialization
  inside McpToolset or the adapter is an adapter/harness property and is NOT
  asserted here as an SDK claim; what is asserted is correctness (all results
  correct, no exceptions, session usable afterwards).

Assertion messages are classified: "SDK DEFECT" vs "HARNESS ISSUE".
"""
from __future__ import annotations

import asyncio

import pytest
from helpers import (
    ADK_ADAPTER_SESSION,
    FAST_MCP_SESSION,
    OFFICIAL_SESSION,
    PROBE_TOOL,
    expected_probe_echo,
    probe_arguments,
    sdk_defect,
)

BURST_SIZES = [1, 10, 100]


def _args_for(i: int) -> dict:
    return probe_arguments(
        string_field=f"burst-{i}",
        int_field=i,
        union_field=i,
        list_field=[f"item-{i}"],
        nested_field={"id": f"n{i}", "tags": [f"t{i}"]},
        nested_list_field=[{"name": f"item-{i}", "count": i}],
    )


# ---- official ----


@pytest.mark.parametrize("n", BURST_SIZES, ids=lambda n: f"burst-{n}")
async def test_official_concurrent_probes(n: int) -> None:
    candidate = "official"
    async with OFFICIAL_SESSION() as session:
        args_list = [_args_for(i) for i in range(n)]
        results = await asyncio.gather(
            *(session.call_tool(PROBE_TOOL, args) for args in args_list)
        )
        for args, result in zip(args_list, results, strict=True):
            assert not result.is_error, sdk_defect(
                candidate, f"burst of {n}: call errored: {result}"
            )
            assert result.structured_content == expected_probe_echo(args), sdk_defect(
                candidate,
                f"burst of {n}: echo mismatch for int_field={args['int_field']}",
            )
        # Session usable after the burst.
        result = await session.call_tool(PROBE_TOOL, probe_arguments())
        assert not result.is_error, sdk_defect(candidate, "session unusable after burst")


# ---- fastmcp ----


@pytest.mark.parametrize("n", BURST_SIZES, ids=lambda n: f"burst-{n}")
async def test_fastmcp_concurrent_probes(n: int) -> None:
    candidate = "fastmcp"
    async with FAST_MCP_SESSION() as client:
        args_list = [_args_for(i) for i in range(n)]
        results = await asyncio.gather(
            *(client.call_tool(PROBE_TOOL, args) for args in args_list)
        )
        for args, result in zip(args_list, results, strict=True):
            assert not result.is_error, sdk_defect(
                candidate, f"burst of {n}: call errored: {result}"
            )
            assert result.structured_content == expected_probe_echo(args), sdk_defect(
                candidate,
                f"burst of {n}: echo mismatch for int_field={args['int_field']}",
            )
        result = await client.call_tool(PROBE_TOOL, probe_arguments())
        assert not result.is_error, sdk_defect(candidate, "session unusable after burst")


# ---- adk (adapter driving surface) ----


@pytest.mark.parametrize("n", BURST_SIZES, ids=lambda n: f"burst-{n}")
async def test_adk_concurrent_probes(n: int) -> None:
    """Same burst sizes through the adapter. If McpToolset serializes
    internally, that is an adapter/harness property — this test asserts
    correctness only, not parallelism."""
    candidate = "adk"
    async with ADK_ADAPTER_SESSION() as (adapter, _):
        args_list = [_args_for(i) for i in range(n)]
        results = await asyncio.gather(
            *(adapter.call_tool(PROBE_TOOL, args) for args in args_list)
        )
        for args, result in zip(args_list, results, strict=True):
            assert not result.is_error, sdk_defect(
                candidate, f"burst of {n}: call errored: {result.text}"
            )
            assert result.structured_content == expected_probe_echo(args), sdk_defect(
                candidate,
                f"burst of {n}: echo mismatch for int_field={args['int_field']}",
            )
        result = await adapter.call_tool(PROBE_TOOL, probe_arguments())
        assert not result.is_error, sdk_defect(candidate, "session unusable after burst")
