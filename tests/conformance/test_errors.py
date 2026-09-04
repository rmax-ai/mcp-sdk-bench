"""ERRORS conformance (SPEC.md §8 ERRORS, M2.1).

Per candidate: invalid parameters, application exception, timeout, connection
loss, malformed server response.

Wire-level faults use the deterministic StdioProxy (tests/conformance/
helpers.py) — delay/drop/corrupt modes — which applies only to the official
and FastMCP candidates. The ADK candidate is driven through the benchmark
AdkAdapter (ADK 2.8 ships no standalone protocol client; the adapter is the
canonical driving surface), and McpToolset manages the wire channel itself:
there is NO wire access for delay/drop/corrupt injection, and the adapter
exposes no per-call timeout parameter. Those three cases are therefore
skipped for ADK with an explicit harness-limitation classification (see
test_adk_wire_level_faults_are_a_harness_limitation).

SDK behavior observations recorded here (verified against the installed
versions, mcp 2.1.1 / fastmcp 4.0.2):
- mcp 2.x delivers a malformed server frame to the session's message_handler
  as a stream-exception value; the pending request is NOT failed by it and
  surfaces via its read timeout (MCPError, REQUEST_TIMEOUT). Never a hang
  (with a configured timeout), never a fake result.
- mcp 2.x raises MCPError(code=CONNECTION_CLOSED) for pending requests when
  the transport drops.
- FastMCP 4's client inherits both behaviors from its mcp 2.x session; its
  MessageHandler.on_exception hook observes the malformed frame.

Assertion messages are classified: "SDK DEFECT" vs "HARNESS ISSUE".
"""
from __future__ import annotations

from typing import Any

import pytest
from helpers import (
    ADK_ADAPTER_SESSION,
    FAST_MCP_SESSION,
    INVALID_PARAMS,
    OFFICIAL_SESSION,
    PROBE_TOOL,
    MCPError,
    ProxyFault,
    probe_arguments,
    sdk_defect,
)

#: deploy_service guard: payments-api is seeded in staging, so a production
#: deploy raises WorldError("payments-api is not deployed in production").
PROD_GUARD_ARGS = {
    "service": "payments-api",
    "target_version": "9.9.9",
    "environment": "production",
}
PROD_GUARD_MESSAGE = "not deployed in production"

#: Seed availability of thinkpad-t14 (fixtures.py); used to prove the world
#: is unchanged after a rejected mutation.
SEED_THINKPAD_AVAILABLE = 2


def _exception_recorder() -> tuple[Any, list[Exception]]:
    """FastMCP MessageHandler subclass recording transport-level exceptions.

    Built lazily inside a factory so this module stays importable in the
    ADK env (mcp 1.x, no fastmcp installed) — the ADK candidate's tests in
    this module run there.
    """
    from fastmcp.client.messages import MessageHandler

    class _ExceptionRecorder(MessageHandler):
        def __init__(self) -> None:
            self.exceptions: list[Exception] = []

        async def on_exception(self, message: Exception) -> None:
            self.exceptions.append(message)

    recorder = _ExceptionRecorder()
    return recorder, recorder.exceptions


# ---- official ----


async def test_official_invalid_parameter_type() -> None:
    candidate = "official"
    async with OFFICIAL_SESSION() as session:
        with pytest.raises(MCPError) as excinfo:
            await session.call_tool(PROBE_TOOL, probe_arguments(int_field="not-an-int"))
        assert isinstance(excinfo.value, MCPError)
        assert excinfo.value.error.code == INVALID_PARAMS, sdk_defect(
            candidate, f"invalid type mapped to code {excinfo.value.error.code}, want -32602"
        )
        assert "int_field" in str(excinfo.value), sdk_defect(
            candidate, "invalid-type error does not name the field"
        )
        # Session still usable after the protocol error.
        result = await session.call_tool(PROBE_TOOL, probe_arguments())
        assert not result.is_error, sdk_defect(candidate, "session unusable after error")


async def test_official_missing_required_parameter() -> None:
    candidate = "official"
    async with OFFICIAL_SESSION() as session:
        args = probe_arguments()
        del args["int_field"]
        with pytest.raises(MCPError) as excinfo:
            await session.call_tool(PROBE_TOOL, args)
        assert isinstance(excinfo.value, MCPError)
        assert excinfo.value.error.code == INVALID_PARAMS, sdk_defect(
            candidate, f"missing-param mapped to code {excinfo.value.error.code}"
        )
        assert "int_field" in str(excinfo.value), sdk_defect(
            candidate, "missing-param error does not name the missing field"
        )


async def test_official_application_exception_and_world_unchanged() -> None:
    candidate = "official"
    async with OFFICIAL_SESSION() as session:
        result = await session.call_tool("deploy_service", PROD_GUARD_ARGS)
        assert result.is_error, sdk_defect(candidate, "production guard did not error")
        text = "\n".join(
            block.text for block in result.content if block.type == "text"
        )
        assert PROD_GUARD_MESSAGE in text, sdk_defect(
            candidate, f"WorldError message missing from error result: {text!r}"
        )
        # World state unchanged by the rejected mutation.
        inventory = await session.call_tool("get_inventory", {})
        assert inventory.structured_content is not None
        assert (
            inventory.structured_content["items"]["thinkpad-t14"]["available"]
            == SEED_THINKPAD_AVAILABLE
        ), sdk_defect(candidate, "world state changed after rejected deploy")


async def test_official_timeout_via_delay_proxy() -> None:
    candidate = "official"
    fault = ProxyFault(mode="delay", delay_ms=1000)
    async with OFFICIAL_SESSION(fault=fault) as session:
        # read_timeout_seconds is the official SDK's real per-call timeout.
        with pytest.raises(MCPError, match="timed out"):
            await session.call_tool(
                PROBE_TOOL, probe_arguments(), read_timeout_seconds=0.3
            )
        # Timeout must not poison the session.
        result = await session.call_tool("get_ticket", {"ticket_id": "PAY-123"})
        assert not result.is_error, sdk_defect(candidate, "session unusable after timeout")


async def test_official_connection_loss_via_drop_proxy() -> None:
    # nth=1: the initialize response is the last frame the client ever sees.
    fault = ProxyFault(mode="drop", nth=1)
    async with OFFICIAL_SESSION(fault=fault) as session:
        with pytest.raises(MCPError, match="Connection closed"):
            await session.call_tool(PROBE_TOOL, probe_arguments())


async def test_official_malformed_response_via_corrupt_proxy() -> None:
    candidate = "official"
    collected: list[Exception] = []

    async def handler(message: object) -> None:
        if isinstance(message, Exception):
            collected.append(message)

    # nth=2: initialize (frame 1) succeeds; the first tools/call response is
    # replaced with invalid JSON.
    fault = ProxyFault(mode="corrupt", nth=2)
    async with OFFICIAL_SESSION(fault=fault, message_handler=handler) as session:
        with pytest.raises(MCPError, match="timed out"):
            await session.call_tool(
                PROBE_TOOL, probe_arguments(), read_timeout_seconds=2.0
            )
        assert collected, sdk_defect(
            candidate, "malformed frame never surfaced to the message_handler"
        )
        # Session still usable: subsequent (uncorrupted) calls succeed.
        result = await session.call_tool("get_ticket", {"ticket_id": "PAY-123"})
        assert not result.is_error, sdk_defect(
            candidate, "session unusable after malformed frame"
        )


# ---- fastmcp ----


async def test_fastmcp_invalid_parameter_type() -> None:
    candidate = "fastmcp"
    async with FAST_MCP_SESSION() as client:
        result = await client.call_tool(
            PROBE_TOOL, probe_arguments(int_field="not-an-int"), raise_on_error=False
        )
        assert result.is_error, sdk_defect(candidate, "invalid type silently accepted")
        assert "int_field" in _result_text(result), sdk_defect(
            candidate, "invalid-type error does not name the field"
        )
        ok = await client.call_tool(PROBE_TOOL, probe_arguments())
        assert not ok.is_error, sdk_defect(candidate, "session unusable after error")


async def test_fastmcp_missing_required_parameter() -> None:
    candidate = "fastmcp"
    async with FAST_MCP_SESSION() as client:
        args = probe_arguments()
        del args["int_field"]
        result = await client.call_tool(PROBE_TOOL, args, raise_on_error=False)
        assert result.is_error, sdk_defect(candidate, "missing param silently accepted")
        assert "int_field" in _result_text(result), sdk_defect(
            candidate, "missing-param error does not name the missing field"
        )


async def test_fastmcp_application_exception_and_world_unchanged() -> None:
    candidate = "fastmcp"
    async with FAST_MCP_SESSION() as client:
        result = await client.call_tool(
            "deploy_service", PROD_GUARD_ARGS, raise_on_error=False
        )
        assert result.is_error, sdk_defect(candidate, "production guard did not error")
        assert PROD_GUARD_MESSAGE in _result_text(result), sdk_defect(
            candidate, "WorldError message missing from error result"
        )
        inventory = await client.call_tool("get_inventory", {})
        assert inventory.structured_content is not None
        assert (
            inventory.structured_content["items"]["thinkpad-t14"]["available"]
            == SEED_THINKPAD_AVAILABLE
        ), sdk_defect(candidate, "world state changed after rejected deploy")


async def test_fastmcp_timeout_via_delay_proxy() -> None:
    candidate = "fastmcp"
    fault = ProxyFault(mode="delay", delay_ms=1000)
    async with FAST_MCP_SESSION(fault=fault) as client:
        # timeout= is the FastMCP client's real per-call timeout parameter.
        with pytest.raises(MCPError, match="timed out"):
            await client.call_tool(PROBE_TOOL, probe_arguments(), timeout=0.3)
        result = await client.call_tool("get_ticket", {"ticket_id": "PAY-123"})
        assert not result.is_error, sdk_defect(candidate, "session unusable after timeout")


async def test_fastmcp_connection_loss_via_drop_proxy() -> None:
    fault = ProxyFault(mode="drop", nth=1)
    async with FAST_MCP_SESSION(fault=fault) as client:
        with pytest.raises(MCPError, match="Connection closed"):
            await client.call_tool(PROBE_TOOL, probe_arguments())


async def test_fastmcp_malformed_response_via_corrupt_proxy() -> None:
    candidate = "fastmcp"
    recorder, recorded = _exception_recorder()
    fault = ProxyFault(mode="corrupt", nth=2)
    async with FAST_MCP_SESSION(fault=fault, message_handler=recorder) as client:
        with pytest.raises(MCPError, match="timed out"):
            await client.call_tool(PROBE_TOOL, probe_arguments(), timeout=2.0)
        assert recorded, sdk_defect(
            candidate, "malformed frame never surfaced to MessageHandler.on_exception"
        )
        result = await client.call_tool("get_ticket", {"ticket_id": "PAY-123"})
        assert not result.is_error, sdk_defect(
            candidate, "session unusable after malformed frame"
        )


# ---- adk (adapter driving surface; adapter-level cases only) ----


async def test_adk_adapter_level_errors() -> None:
    """ADK candidate: parameter and application errors through the adapter.

    The adapter maps every candidate-side failure to ToolResult(is_error=True)
    — never a crash — so that is the canonical error shape asserted here.
    """
    candidate = "adk"
    async with ADK_ADAPTER_SESSION() as (adapter, _):
        # Invalid parameter type.
        bad = await adapter.call_tool(PROBE_TOOL, probe_arguments(int_field="not-an-int"))
        assert bad.is_error, sdk_defect(candidate, "invalid type silently accepted")
        assert bad.text is not None and "int_field" in bad.text, sdk_defect(
            candidate, f"invalid-type error does not name the field: {bad.text!r}"
        )
        # Missing required parameter.
        args = probe_arguments()
        del args["int_field"]
        bad = await adapter.call_tool(PROBE_TOOL, args)
        assert bad.is_error, sdk_defect(candidate, "missing param silently accepted")
        assert bad.text is not None and "int_field" in bad.text, sdk_defect(
            candidate, f"missing-param error does not name the field: {bad.text!r}"
        )
        # Application exception: production deploy guard raises WorldError.
        bad = await adapter.call_tool("deploy_service", PROD_GUARD_ARGS)
        assert bad.is_error, sdk_defect(candidate, "production guard did not error")
        assert bad.text is not None and PROD_GUARD_MESSAGE in bad.text, sdk_defect(
            candidate, f"WorldError message missing: {bad.text!r}"
        )
        # World state unchanged by the rejected mutation.
        inventory = await adapter.call_tool("get_inventory", {})
        assert inventory.structured_content is not None
        assert (
            inventory.structured_content["items"]["thinkpad-t14"]["available"]
            == SEED_THINKPAD_AVAILABLE
        ), sdk_defect(candidate, "world state changed after rejected deploy")
        # Adapter session still usable after the errors above.
        ok = await adapter.call_tool(PROBE_TOOL, probe_arguments())
        assert not ok.is_error, sdk_defect(candidate, "session unusable after errors")


async def test_adk_wire_level_faults_are_a_harness_limitation() -> None:
    pytest.skip(
        "HARNESS LIMITATION [adk]: McpToolset drives the server over an "
        "SDK-managed channel with no wire access, so delay/drop/corrupt "
        "injection is impossible, and AdkAdapter exposes no per-call timeout "
        "parameter (SPEC.md §8 ERRORS: timeout / connection-loss / "
        "malformed-response are not exercisable for this candidate)"
    )


def _result_text(result: object) -> str:
    content = getattr(result, "content", None) or []
    return "\n".join(
        block.text for block in content if getattr(block, "type", None) == "text"
    )
