"""Cross-implementation pairing matrix — SPEC.md §8 INTEROPERABILITY (M2.2).

Each of the five pairings runs a REAL client from one SDK against a REAL
server from another SDK, in a fresh subprocess world, behind the M2.1
log-mode stdio proxy so both protocol versions are captured from the wire
(client-announced on the initialize/server-discover request, server-accepted
on the response / the client's negotiated stamp):

    fastmcp -> fastmcp   (self-pair baseline)
    fastmcp -> official
    official -> fastmcp
    adk -> fastmcp       (driven inside envs/adk, DECISIONS.md D1)
    adk -> official      (driven inside envs/adk, DECISIONS.md D1)

The per-pairing logic is shared with the `mcpbench interop` runner
(src/mcp_sdk_bench/benchmark/interop.py) — these tests assert the same
PairingResult the runner reports.

Failures are CLASSIFIED, never weakened or silently skipped (AGENTS.md rule
4): every assertion message carries the classification — SDK DEFECT (with
repro), VERSION-NEGOTIATION FAILURE (with the wire versions), or HARNESS
LIMITATION. A pairing that cannot connect fails at the connected assertion
with its wire evidence attached.
"""
from __future__ import annotations

import pytest

from mcp_sdk_bench.benchmark.interop import (
    PAIRINGS,
    AdkEnvUnavailable,
    Pairing,
    PairingResult,
    run_pairing,
)

CLASSIFICATION_LABELS = {
    "sdk_defect": "SDK DEFECT",
    "negotiation_failure": "VERSION-NEGOTIATION FAILURE",
    "harness_limitation": "HARNESS LIMITATION",
    "pass": "UNCLASSIFIED FAILURE",
}


def _classified(result: PairingResult, stage: str) -> str:
    label = CLASSIFICATION_LABELS.get(result.classification, result.classification.upper())
    return (
        f"{label} [{result.pairing}] at {stage}: {result.error} "
        f"(wire protocolVersion: client-announced={result.protocol_version_client} "
        f"server-accepted={result.protocol_version_server}; "
        f"repro: uv run pytest tests/interoperability -k {result.pairing} "
        f"or uv run mcpbench interop)"
    )


@pytest.mark.parametrize("pairing", PAIRINGS, ids=[p.name for p in PAIRINGS])
async def test_pairing(pairing: Pairing, tmp_path) -> None:
    try:
        result = await run_pairing(pairing, tmp_path)
    except AdkEnvUnavailable as err:
        # Explicit, reasoned skip (never silent): the ADK client stack cannot
        # exist in the main env (DECISIONS.md D1), so without envs/adk the
        # pairing is unrunnable on this host — not a pairing verdict.
        pytest.skip(str(err))

    # connect + initialize (+ clean teardown: run_pairing exiting without a
    # connect/teardown error means the client context and the proxied server
    # subprocess shut down cleanly)
    assert result.connected, _classified(result, "connect/initialize")

    # protocolVersion captured in BOTH directions, from the wire
    assert result.protocol_version_client is not None, (
        f"HARNESS LIMITATION [{result.pairing}]: proxy wire log has no "
        f"client-announced protocolVersion — {result.error}"
    )
    assert result.protocol_version_server is not None, (
        f"VERSION-NEGOTIATION FAILURE [{result.pairing}]: no server-accepted "
        f"protocolVersion on the wire (client announced "
        f"{result.protocol_version_client}) — {result.error}"
    )

    # discovery: the 10-tool contract on every pairing (M2.1 six + M2.3a
    # create_ticket + M3.2 generate_monthly_report/get_report_task/
    # cancel_report_task); resources + prompts asserted through the client
    # surfaces that expose them
    assert result.tools_seen == 10, _classified(result, "discovery: tools")
    if pairing.client_sdk == "adk":
        # Honest absence (M1 finding, SPEC.md §7): ADK's McpToolset has no
        # first-class resource/prompt surface, so the ADK client cannot
        # enumerate them. Absence is reported, not emulated — this says
        # nothing about the server, which serves both (proven by the
        # fastmcp/official client pairings above).
        assert result.resources_seen == 0 and result.prompts_seen == 0, _classified(
            result, "discovery: ADK client surface"
        )
    else:
        assert result.resources_seen == 1, _classified(result, "discovery: resources")
        assert result.prompts_seen == 1, _classified(result, "discovery: prompts")
    assert result.discovery_ok, _classified(result, "discovery")

    # round-trip: get_ticket("PAY-123") seeded state + probe_schema echo of
    # one primitive and one nested field
    assert result.roundtrip_ok, _classified(result, "round-trip")

    assert result.classification == "pass", _classified(result, "overall")
