"""SCHEMA conformance (SPEC.md §8 SCHEMA, M2.1) via the probe_schema tool.

Exercises: primitive parameters, nested objects, enums (valid + invalid),
nullable values (explicit null), unions (both branches), arrays (empty +
nested list of two objects), and structured results.

Driving surfaces (no faked symmetry):
- official: official SDK ClientSession over stdio. Canonical error shape for
  argument validation failures is a RAISED MCPError (the official server
  maps pydantic ValidationError to JSON-RPC invalid-params, -32602).
- fastmcp: fastmcp.client.Client over stdio. Canonical error shape is an
  isError=True CallToolResult (FastMCP server-side validation failure is a
  tool error result; the client raises only with raise_on_error=True).
- adk: the benchmark AdkAdapter — ADK 2.8 ships no standalone protocol
  client, so the adapter IS the canonical driving surface. Its canonical
  error shape is ToolResult(is_error=True) (the adapter maps every
  candidate-side failure to a tool error, never a crash).

Structured results: all three surfaces express structuredContent for
probe_schema (mcp 2.x CallToolResult.structured_content; FastMCP
CallToolResult.structured_content; AdkAdapter ToolResult.structured_content
read from the mcp 1.x camelCase payload), so no candidate needs the
partially_supported text-fallback classification here.

Assertion messages are classified: "SDK DEFECT" vs "HARNESS ISSUE".
"""
from __future__ import annotations

import pytest
from helpers import (
    ADK_ADAPTER_SESSION,
    FAST_MCP_SESSION,
    OFFICIAL_SESSION,
    PROBE_TOOL,
    MCPError,
    expected_probe_echo,
    probe_arguments,
    sdk_defect,
)

ENUM_VALUES = ("alpha", "beta", "gamma")


# ---- official ----


async def test_official_probe_round_trip_all_primitives() -> None:
    candidate = "official"
    async with OFFICIAL_SESSION() as session:
        args = probe_arguments()
        result = await session.call_tool(PROBE_TOOL, args)
        assert not result.is_error, sdk_defect(candidate, f"valid probe errored: {result}")
        assert result.structured_content is not None, sdk_defect(
            candidate, "probe result has no structuredContent"
        )
        assert result.structured_content == expected_probe_echo(args), sdk_defect(
            candidate, f"echo mismatch: {result.structured_content}"
        )


async def test_official_probe_enum_accepts_valid_values() -> None:
    candidate = "official"
    async with OFFICIAL_SESSION() as session:
        for value in ENUM_VALUES:
            args = probe_arguments(enum_field=value)
            result = await session.call_tool(PROBE_TOOL, args)
            assert not result.is_error, sdk_defect(
                candidate, f"valid enum {value!r} rejected"
            )
            assert result.structured_content is not None
            assert result.structured_content["received"]["enum_field"] == value


async def test_official_probe_enum_rejects_invalid_value() -> None:
    candidate = "official"
    async with OFFICIAL_SESSION() as session:
        # Canonical official-SDK shape: raised MCPError (invalid params).
        with pytest.raises(MCPError, match="enum_field"):
            await session.call_tool(PROBE_TOOL, probe_arguments(enum_field="delta"))
        # Session still usable after the protocol error.
        result = await session.call_tool(PROBE_TOOL, probe_arguments())
        assert not result.is_error, harness_usable(candidate)


async def test_official_probe_nullable_explicit_null_and_string() -> None:
    candidate = "official"
    async with OFFICIAL_SESSION() as session:
        # Explicit null round-trips.
        args = probe_arguments(nullable_field=None)
        result = await session.call_tool(PROBE_TOOL, args)
        assert result.structured_content is not None
        assert result.structured_content["received"]["nullable_field"] is None, sdk_defect(
            candidate, "explicit null did not round-trip"
        )
        # Non-null string also round-trips.
        args = probe_arguments(nullable_field="present")
        result = await session.call_tool(PROBE_TOOL, args)
        assert result.structured_content is not None
        assert result.structured_content["received"]["nullable_field"] == "present"


async def test_official_probe_union_both_branches() -> None:
    candidate = "official"
    async with OFFICIAL_SESSION() as session:
        for branch in ("seven", 7):
            args = probe_arguments(union_field=branch)
            result = await session.call_tool(PROBE_TOOL, args)
            assert result.structured_content is not None
            assert result.structured_content["received"]["union_field"] == branch, (
                sdk_defect(candidate, f"union branch {branch!r} did not round-trip")
            )


async def test_official_probe_arrays_empty_and_nested() -> None:
    candidate = "official"
    async with OFFICIAL_SESSION() as session:
        # Empty list.
        args = probe_arguments(list_field=[])
        result = await session.call_tool(PROBE_TOOL, args)
        assert result.structured_content is not None
        assert result.structured_content["received"]["list_field"] == [], sdk_defect(
            candidate, "empty list did not round-trip"
        )
        # Nested list with two objects round-trips exactly.
        args = probe_arguments()
        result = await session.call_tool(PROBE_TOOL, args)
        assert result.structured_content is not None
        assert (
            result.structured_content["received"]["nested_list_field"]
            == args["nested_list_field"]
        ), sdk_defect(candidate, "nested list of objects did not round-trip")


# ---- fastmcp ----


async def test_fastmcp_probe_round_trip_all_primitives() -> None:
    candidate = "fastmcp"
    async with FAST_MCP_SESSION() as client:
        args = probe_arguments()
        result = await client.call_tool(PROBE_TOOL, args)
        assert not result.is_error, sdk_defect(candidate, f"valid probe errored: {result}")
        assert result.structured_content is not None, sdk_defect(
            candidate, "probe result has no structuredContent"
        )
        assert result.structured_content == expected_probe_echo(args), sdk_defect(
            candidate, f"echo mismatch: {result.structured_content}"
        )


async def test_fastmcp_probe_enum_accepts_valid_values() -> None:
    candidate = "fastmcp"
    async with FAST_MCP_SESSION() as client:
        for value in ENUM_VALUES:
            args = probe_arguments(enum_field=value)
            result = await client.call_tool(PROBE_TOOL, args)
            assert not result.is_error, sdk_defect(
                candidate, f"valid enum {value!r} rejected"
            )
            assert result.structured_content is not None
            assert result.structured_content["received"]["enum_field"] == value


async def test_fastmcp_probe_enum_rejects_invalid_value() -> None:
    candidate = "fastmcp"
    async with FAST_MCP_SESSION() as client:
        # Canonical FastMCP shape: isError=True result (validation happens
        # server-side inside the tool call, not as a protocol error).
        result = await client.call_tool(
            PROBE_TOOL, probe_arguments(enum_field="delta"), raise_on_error=False
        )
        assert result.is_error, sdk_defect(
            candidate, "invalid enum silently accepted (or produced no error result)"
        )
        assert "enum_field" in _result_text(result), sdk_defect(
            candidate, "enum error text does not name the field"
        )
        # Session still usable after the error result.
        ok = await client.call_tool(PROBE_TOOL, probe_arguments())
        assert not ok.is_error, harness_usable(candidate)


async def test_fastmcp_probe_nullable_explicit_null_and_string() -> None:
    candidate = "fastmcp"
    async with FAST_MCP_SESSION() as client:
        args = probe_arguments(nullable_field=None)
        result = await client.call_tool(PROBE_TOOL, args)
        assert result.structured_content is not None
        assert result.structured_content["received"]["nullable_field"] is None, sdk_defect(
            candidate, "explicit null did not round-trip"
        )
        args = probe_arguments(nullable_field="present")
        result = await client.call_tool(PROBE_TOOL, args)
        assert result.structured_content is not None
        assert result.structured_content["received"]["nullable_field"] == "present"


async def test_fastmcp_probe_union_both_branches() -> None:
    candidate = "fastmcp"
    async with FAST_MCP_SESSION() as client:
        for branch in ("seven", 7):
            args = probe_arguments(union_field=branch)
            result = await client.call_tool(PROBE_TOOL, args)
            assert result.structured_content is not None
            assert result.structured_content["received"]["union_field"] == branch, (
                sdk_defect(candidate, f"union branch {branch!r} did not round-trip")
            )


async def test_fastmcp_probe_arrays_empty_and_nested() -> None:
    candidate = "fastmcp"
    async with FAST_MCP_SESSION() as client:
        args = probe_arguments(list_field=[])
        result = await client.call_tool(PROBE_TOOL, args)
        assert result.structured_content is not None
        assert result.structured_content["received"]["list_field"] == [], sdk_defect(
            candidate, "empty list did not round-trip"
        )
        args = probe_arguments()
        result = await client.call_tool(PROBE_TOOL, args)
        assert result.structured_content is not None
        assert (
            result.structured_content["received"]["nested_list_field"]
            == args["nested_list_field"]
        ), sdk_defect(candidate, "nested list of objects did not round-trip")


# ---- adk (adapter driving surface) ----


async def test_adk_probe_schema_round_trip() -> None:
    """All schema cases for the ADK candidate in one adapter session (the
    adapter's per-session spawn cost dominates; the cases are independent
    calls over one world because probe_schema is side-effect-free)."""
    candidate = "adk"
    async with ADK_ADAPTER_SESSION() as (adapter, _):
        # Round-trip all primitives (incl. explicit null, empty list,
        # nested object, nested list of two objects).
        args = probe_arguments()
        result = await adapter.call_tool(PROBE_TOOL, args)
        assert not result.is_error, sdk_defect(candidate, f"valid probe errored: {result.text}")
        assert result.structured_content is not None, sdk_defect(
            candidate, "probe result has no structuredContent"
        )
        assert result.structured_content == expected_probe_echo(args), sdk_defect(
            candidate, f"echo mismatch: {result.structured_content}"
        )

        # Enum: every valid value accepted.
        for value in ENUM_VALUES:
            ok = await adapter.call_tool(PROBE_TOOL, probe_arguments(enum_field=value))
            assert not ok.is_error, sdk_defect(candidate, f"valid enum {value!r} rejected")
            assert ok.structured_content is not None
            assert ok.structured_content["received"]["enum_field"] == value

        # Enum: invalid value -> canonical adapter shape ToolResult(is_error=True).
        bad = await adapter.call_tool(PROBE_TOOL, probe_arguments(enum_field="delta"))
        assert bad.is_error, sdk_defect(candidate, "invalid enum silently accepted")
        assert bad.text is not None and "enum" in bad.text, sdk_defect(
            candidate, "enum error text does not name the field"
        )

        # Union: both branches.
        for union_value in ("seven", 7):
            ok = await adapter.call_tool(PROBE_TOOL, probe_arguments(union_field=union_value))
            assert not ok.is_error
            assert ok.structured_content is not None
            assert ok.structured_content["received"]["union_field"] == union_value

        # Explicit null + empty list.
        ok = await adapter.call_tool(
            PROBE_TOOL, probe_arguments(nullable_field=None, list_field=[])
        )
        assert ok.structured_content is not None
        assert ok.structured_content["received"]["nullable_field"] is None
        assert ok.structured_content["received"]["list_field"] == []


def _result_text(result: object) -> str:
    content = getattr(result, "content", None) or []
    return "\n".join(
        block.text for block in content if getattr(block, "type", None) == "text"
    )


def harness_usable(candidate: str) -> str:
    return sdk_defect(candidate, "session unusable after a per-request error")
