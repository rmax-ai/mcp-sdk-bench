"""DISCOVERY conformance (SPEC.md §8 DISCOVERY, M2.1).

Per candidate: connect, capabilities negotiation succeeds, list tools returns
exactly the contract set (names + descriptions), list resources, list prompts,
and protocolVersion is present in the initialize result.

Driving surfaces (no faked symmetry):
- official: official SDK ClientSession over stdio.
- fastmcp: fastmcp.client.Client over stdio.
- adk: the benchmark AdkAdapter (src/mcp_sdk_bench/adapters/adk.py) — ADK 2.8
  ships no standalone protocol client, so the adapter IS the canonical
  driving surface. Its discovery honestly reports EMPTY resources/prompts
  (McpToolset has no first-class resource/prompt surface; M1 finding,
  SPEC.md §7 absence-as-absence), and it exposes no initialize result
  (McpToolset hides the handshake), so the protocolVersion assertion is a
  documented harness limitation for this candidate.

Assertion messages are classified: "SDK DEFECT" means the candidate violates
its documented contract; "HARNESS ISSUE" means the benchmark harness is at
fault.
"""
from __future__ import annotations

from helpers import (
    ADK_ADAPTER_SESSION,
    DISCOVERY_CONTRACT,
    FAST_MCP_SESSION,
    OFFICIAL_SESSION,
    harness_issue,
    sdk_defect,
)


async def test_official_connect_and_negotiate() -> None:
    candidate = "official"
    async with OFFICIAL_SESSION() as session:
        # Reaching here means spawn + initialize handshake succeeded.
        assert session.initialize_result is not None, harness_issue(
            candidate, "initialize returned no result object"
        )
        assert session.protocol_version, sdk_defect(
            candidate, "initialize result carries no protocolVersion"
        )
        capabilities = session.initialize_result.capabilities
        assert capabilities.tools is not None, sdk_defect(
            candidate, "server capabilities do not advertise tools"
        )


async def test_official_tools_resources_prompts_match_contract() -> None:
    candidate = "official"
    contract = DISCOVERY_CONTRACT[candidate]
    async with OFFICIAL_SESSION() as session:
        tools = await session.list_tools()
        assert {tool.name for tool in tools.tools} == contract.tools, sdk_defect(
            candidate,
            f"tool set {{{', '.join(sorted(t.name for t in tools.tools))}}} != contract",
        )
        for tool in tools.tools:
            assert tool.description, sdk_defect(
                candidate, f"tool {tool.name} has no description"
            )
        resources = await session.list_resources()
        assert sorted(str(r.uri) for r in resources.resources) == sorted(
            contract.resources
        ), sdk_defect(candidate, "resource list != contract")
        prompts = await session.list_prompts()
        assert {p.name for p in prompts.prompts} == set(contract.prompts), sdk_defect(
            candidate, "prompt list != contract"
        )


async def test_fastmcp_connect_and_negotiate() -> None:
    candidate = "fastmcp"
    async with FAST_MCP_SESSION() as client:
        assert client.protocol_version, sdk_defect(
            candidate, "negotiated session carries no protocolVersion"
        )
        assert client.server_capabilities.tools is not None, sdk_defect(
            candidate, "server capabilities do not advertise tools"
        )


async def test_fastmcp_tools_resources_prompts_match_contract() -> None:
    candidate = "fastmcp"
    contract = DISCOVERY_CONTRACT[candidate]
    async with FAST_MCP_SESSION() as client:
        tools = await client.list_tools()
        assert {tool.name for tool in tools} == contract.tools, sdk_defect(
            candidate,
            f"tool set {{{', '.join(sorted(t.name for t in tools))}}} != contract",
        )
        for tool in tools:
            assert tool.description, sdk_defect(
                candidate, f"tool {tool.name} has no description"
            )
        resources = await client.list_resources()
        assert sorted(str(r.uri) for r in resources) == sorted(
            contract.resources
        ), sdk_defect(candidate, "resource list != contract")
        prompts = await client.list_prompts()
        assert {p.name for p in prompts} == set(contract.prompts), sdk_defect(
            candidate, "prompt list != contract"
        )


async def test_adk_tools_match_contract_and_gaps_are_honest() -> None:
    """ADK candidate via the benchmark adapter. Resources/prompts MUST be
    empty lists (honest absence); protocolVersion is not exposed by
    McpToolset — harness limitation, not an SDK claim."""
    candidate = "adk"
    contract = DISCOVERY_CONTRACT[candidate]
    async with ADK_ADAPTER_SESSION() as (_, discovery):
        assert {tool.name for tool in discovery.tools} == contract.tools, sdk_defect(
            candidate,
            f"tool set {{{', '.join(sorted(t.name for t in discovery.tools))}}} != contract",
        )
        for tool in discovery.tools:
            assert tool.description, sdk_defect(
                candidate, f"tool {tool.name} has no description"
            )
        # Honest absence (M1 finding): empty lists, never emulation.
        assert discovery.resources == [], sdk_defect(
            candidate, "resources must be reported as EMPTY (McpToolset has no surface)"
        )
        assert discovery.prompts == [], sdk_defect(
            candidate, "prompts must be reported as EMPTY (McpToolset has no surface)"
        )
        assert not contract.exposes_protocol_version, harness_issue(
            candidate, "contract drift: ADK adapter exposes no initialize result"
        )
