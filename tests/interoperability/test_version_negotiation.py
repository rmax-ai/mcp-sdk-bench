"""Wire-level protocol version negotiation evidence — SPEC.md §8 (M2.2).

Drives the two real cross-SDK wire paths (fastmcp client -> official server,
official client -> fastmcp server) through the M2.1 stdio proxy in "log"
mode (tests/conformance/helpers.py StdioProxy) and asserts on the recorded
JSON-RPC frames, not on SDK self-reporting:

- the handshake REQUEST carries a protocolVersion (legacy `initialize`
  params for the mcp 2.x official client; the modern `server/discover`
  params._meta stamp for the FastMCP 4.x client — probed against the
  installed fastmcp 4.0.2 / mcp 2.1.1),
- the handshake RESPONSE carries the server's answer (initialize
  result.protocolVersion, resp. discover result.supportedVersions),
- the version the client REPORTS as negotiated (session.protocol_version /
  client.protocol_version) equals the version observed on the wire — the
  client actually uses the negotiated version, no hardcoded assumption.

Failure classification: a missing/mismatched version on the wire is a
VERSION-NEGOTIATION FAILURE; a client self-report contradicting the wire is
an SDK DEFECT.
"""
from __future__ import annotations

from helpers import (  # ty: ignore[unresolved-import] — tests/conformance/helpers.py, on sys.path via conftest.py
    FAST_MCP_SESSION,
    FASTMCP_SERVER_ARGS,
    OFFICIAL_SERVER_ARGS,
    OFFICIAL_SESSION,
    ProxyFault,
)
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS, LATEST_HANDSHAKE_VERSION

from mcp_sdk_bench.benchmark.interop import (
    PROTOCOL_VERSION_META_KEY,
    discover_exchange,
    initialize_exchange,
    negotiated_version_from_wire,
    read_wire_log,
)


async def test_wire_negotiation_fastmcp_client_official_server(tmp_path) -> None:
    """FastMCP 4.0.2 client -> official mcp 2.1.1 server: the client opens
    with the modern `server/discover` probe (no legacy initialize on the
    wire); the server's supportedVersions answers, and the client stamps the
    negotiated version on every subsequent request."""
    log_file = tmp_path / "wire.jsonl"
    async with FAST_MCP_SESSION(
        fault=ProxyFault(mode="log", log_file=log_file),
        server_args=OFFICIAL_SERVER_ARGS,
    ) as client:
        # A real post-handshake request, so the client's negotiated-version
        # stamp is observable on the wire (not just in SDK state).
        await client.list_tools()
        reported = client.protocol_version

    frames = read_wire_log(log_file)
    assert frames, f"HARNESS LIMITATION: empty wire log at {log_file}"

    # The handshake the FastMCP 4.x client actually performs is
    # server/discover; a legacy initialize exchange is equally valid evidence.
    try:
        request, response = discover_exchange(frames)
    except ValueError:
        request, response = initialize_exchange(frames)
        announced = request["params"].get("protocolVersion")
        assert isinstance(announced, str), (
            f"VERSION-NEGOTIATION FAILURE [fastmcp->official]: initialize "
            f"request carries no protocolVersion: {request}"
        )
        accepted = response["result"].get("protocolVersion")
        assert isinstance(accepted, str), (
            f"VERSION-NEGOTIATION FAILURE [fastmcp->official]: initialize "
            f"response carries no protocolVersion: {response}"
        )
    else:
        announced = request["params"]["_meta"].get(PROTOCOL_VERSION_META_KEY)
        assert isinstance(announced, str), (
            f"VERSION-NEGOTIATION FAILURE [fastmcp->official]: server/discover "
            f"request carries no {PROTOCOL_VERSION_META_KEY} stamp: {request}"
        )
        supported = response["result"].get("supportedVersions")
        assert isinstance(supported, list) and supported, (
            f"VERSION-NEGOTIATION FAILURE [fastmcp->official]: server/discover "
            f"response carries no supportedVersions: {response}"
        )
        # The server picked: the negotiated version must come from its list.
        accepted = negotiated_version_from_wire(frames)
        assert accepted in supported, (
            f"VERSION-NEGOTIATION FAILURE [fastmcp->official]: client stamped "
            f"{accepted!r} but the official server offered {supported}"
        )

    # The client actually uses the negotiated version: SDK self-report must
    # equal the wire evidence.
    assert reported == accepted, (
        f"SDK DEFECT [fastmcp->official]: client reports negotiated "
        f"{reported!r} but the wire shows {accepted!r}"
    )


async def test_wire_negotiation_official_client_fastmcp_server(tmp_path) -> None:
    """Official mcp 2.1.1 client -> FastMCP 4.0.2 server: legacy initialize
    handshake — the client offers LATEST_HANDSHAKE_VERSION (2025-11-25), the
    FastMCP server accepts it, and the session reports the wire-accepted
    version."""
    log_file = tmp_path / "wire.jsonl"
    async with OFFICIAL_SESSION(
        fault=ProxyFault(mode="log", log_file=log_file),
        server_args=FASTMCP_SERVER_ARGS,
    ) as session:
        reported = session.protocol_version

    frames = read_wire_log(log_file)
    assert frames, f"HARNESS LIMITATION: empty wire log at {log_file}"

    request, response = initialize_exchange(frames)
    offered = request["params"].get("protocolVersion")
    assert isinstance(offered, str), (
        f"VERSION-NEGOTIATION FAILURE [official->fastmcp]: initialize request "
        f"carries no protocolVersion: {request}"
    )
    assert offered == LATEST_HANDSHAKE_VERSION, (
        f"SDK DEFECT [official->fastmcp]: client offered {offered!r}, expected "
        f"its newest handshake version {LATEST_HANDSHAKE_VERSION!r}"
    )
    accepted = response["result"].get("protocolVersion")
    assert isinstance(accepted, str), (
        f"VERSION-NEGOTIATION FAILURE [official->fastmcp]: initialize response "
        f"carries no protocolVersion: {response}"
    )
    # The server's pick must be a version the client can actually speak.
    assert accepted in HANDSHAKE_PROTOCOL_VERSIONS, (
        f"VERSION-NEGOTIATION FAILURE [official->fastmcp]: server picked "
        f"{accepted!r}, outside the client's handshake set {HANDSHAKE_PROTOCOL_VERSIONS}"
    )

    # The client actually uses the negotiated version: SDK self-report must
    # equal the wire evidence.
    assert reported == accepted, (
        f"SDK DEFECT [official->fastmcp]: session reports negotiated "
        f"{reported!r} but the wire shows {accepted!r}"
    )
