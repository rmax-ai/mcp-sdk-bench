"""Protocol elicitation pause/resume tests (SPEC.md §18, M3.1).

Drives the official and fastmcp adapters against their real stdio server
subprocesses — the same hermetic pattern as tests/conformance/test_adapters.py
(no model, no network). These are the executable evidence for the capability
matrix's elicitation rows: each variant's adapter must surface the normalized
{kind, question, schema} request, pause the call, and complete it after
respond_to_elicitation — the official SDK in-band (elicitation/create), the
FastMCP variant via the SEP-2322 InputRequiredResult guard (2026-07-28).

The ADK variant is covered in test_adk_elicitation.py (adk env only).
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from mcp_sdk_bench.adapters import FastMCPAdapter, MCPAdapter, OfficialAdapter

ADAPTER_CLASSES = [OfficialAdapter, FastMCPAdapter]


@asynccontextmanager
async def _connected(cls: type[MCPAdapter]) -> AsyncIterator[MCPAdapter]:
    adapter = cls()
    await adapter.connect()
    try:
        yield adapter
    finally:
        await adapter.close()


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_clarification_pauses_and_resume_completes(cls) -> None:
    async with _connected(cls) as adapter:
        first = await adapter.call_tool("reserve_inventory", {"item": "thinkpad-t14"})

        assert first.elicitation_request is not None
        assert first.elicitation_request["kind"] == "clarification"
        assert "employee" in first.elicitation_request["question"].lower()
        assert first.elicitation_request["schema"]["required"] == ["employee_id"]

        await adapter.respond_to_elicitation({"status": "clarified", "answer": "alina"})
        resumed = await adapter.call_tool("reserve_inventory", {"item": "thinkpad-t14"})

        assert not resumed.is_error, resumed.text
        assert resumed.structured_content is not None
        item = resumed.structured_content["item"]
        assert item["reserved_by"] == ["alina"]
        assert item["available"] == 1


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_approval_approve_completes_production_deploy(cls) -> None:
    async with _connected(cls) as adapter:
        first = await adapter.call_tool(
            "deploy_service",
            {"service": "payments-api", "target_version": "2.5.0", "environment": "production"},
        )

        assert first.elicitation_request is not None
        assert first.elicitation_request["kind"] == "approval"
        assert "payments-api" in first.elicitation_request["question"]

        await adapter.respond_to_elicitation({"status": "approved"})
        resumed = await adapter.call_tool(
            "deploy_service",
            {"service": "payments-api", "target_version": "2.5.0", "environment": "production"},
        )

        assert not resumed.is_error, resumed.text
        assert resumed.structured_content is not None
        deployment = resumed.structured_content["deployment"]
        assert deployment["environment"] == "production"
        assert deployment["version"] == "2.5.0"


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_approval_decline_fails_with_world_error(cls) -> None:
    async with _connected(cls) as adapter:
        first = await adapter.call_tool(
            "deploy_service",
            {"service": "checkout", "target_version": "v1.8.3", "environment": "production"},
        )
        assert first.elicitation_request is not None
        assert first.elicitation_request["kind"] == "approval"

        await adapter.respond_to_elicitation({"status": "declined"})
        resumed = await adapter.call_tool(
            "deploy_service",
            {"service": "checkout", "target_version": "v1.8.3", "environment": "production"},
        )

        assert resumed.is_error
        assert resumed.text is not None
        assert "deployment declined by user" in resumed.text
        # World-unchanged on decline is proven hermetically in
        # test_world_elicitation.py (op_log empty, version untouched).


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_no_elicitation_when_arguments_complete(cls) -> None:
    """A fully-specified call must not pause (M1/M2 behavior unchanged)."""
    async with _connected(cls) as adapter:
        result = await adapter.call_tool(
            "reserve_inventory", {"item": "thinkpad-t14", "employee_id": "alice"}
        )
        assert result.elicitation_request is None
        assert not result.is_error
        assert result.structured_content is not None
        assert result.structured_content["item"]["reserved_by"] == ["alice"]


async def test_base_adapter_respond_to_elicitation_is_honest_not_implemented() -> None:
    """The common view never stubs elicitation (SPEC.md §7): the base
    implementation raises NotImplementedError."""
    from mcp_sdk_bench.adapters.base import MCPAdapter

    class Bare(MCPAdapter):
        async def connect(self):
            raise NotImplementedError

        async def call_tool(self, name, arguments):
            raise NotImplementedError

        async def read_resource(self, uri):
            raise NotImplementedError

        async def get_prompt(self, name, arguments):
            raise NotImplementedError

        async def close(self):
            pass

    with pytest.raises(NotImplementedError, match="no protocol elicitation surface"):
        await Bare().respond_to_elicitation({"status": "approved"})
