"""Cross-implementation interoperability runner — SPEC.md §8 INTEROPERABILITY (M2.2).

Runs the five meaningful client/server pairings, each a REAL client from one
SDK against a REAL server from another SDK, in a fresh subprocess world:

    fastmcp -> fastmcp   (self-pair baseline)
    fastmcp -> official
    official -> fastmcp
    adk -> fastmcp       (driven inside envs/adk via interop_adk_driver)
    adk -> official      (driven inside envs/adk via interop_adk_driver)

Every pairing is routed through the M2.1 stdio proxy in "log" mode
(tests/conformance/helpers.py StdioProxy), so both protocol versions are
captured from the WIRE (client-announced on the initialize request,
server-accepted on the response) rather than from SDK self-reporting.

Constructor shapes probed against the installed SDKs (AGENTS.md rule 1):

- fastmcp 4.0.2: ``Client(transport)`` as an async context manager;
  stdio via ``fastmcp.client.transports.StdioTransport(command, args,
  keep_alive=False)``. ``client.protocol_version`` (property) reports the
  negotiated version while connected; list_tools/list_resources/list_prompts
  return mcp 2.x types; ``call_tool(name, args, raise_on_error=False)``
  returns a CallToolResult with ``.structured_content``.
- mcp 2.1.1 (official): ``stdio_client(StdioServerParameters(command, args))``
  yields (read, write) streams; ``ClientSession(read, write)`` +
  ``await session.initialize()`` returns ``types.InitializeResult`` with
  ``.protocol_version`` (server-accepted). The client offers
  ``mcp_types.version.LATEST_HANDSHAKE_VERSION`` ("2025-11-25") on the wire.
- google-adk 2.8.0 (envs/adk only, embeds mcp 1.29.1): see
  interop_adk_driver.py — McpToolset exposes no initialize result, so the
  ADK pairing's protocol versions come from the proxy wire log only.

Deterministic only: no LLM calls, no network beyond local subprocesses.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any

from mcp_sdk_bench.benchmark.result import LATEST_DIR, new_run_id
from mcp_sdk_bench.benchmark.sweep import REPO_ROOT

#: The ten-tool contract (M2.1 six + M2.3a create_ticket + M3.2
#: generate_monthly_report/get_report_task/cancel_report_task, SPEC.md §21;
#: mirrors tests/conformance/helpers.py EXPECTED_TOOLS — duplicated because
#: src must not import from tests).
EXPECTED_TOOLS = frozenset(
    {
        "get_ticket",
        "update_ticket",
        "create_ticket",
        "get_inventory",
        "reserve_inventory",
        "deploy_service",
        "probe_schema",
        "generate_monthly_report",
        "get_report_task",
        "cancel_report_task",
    }
)

DEPLOYMENT_POLICY_URI = "company://policies/deployment"
INCIDENT_TRIAGE_PROMPT = "incident-triage"

#: probe_schema arguments for the interop round-trip: one primitive
#: (string_field) and one nested field (nested_field) are the asserted
#: echo targets; the rest keep the schema's required fields satisfied.
#: Mirrors tests/conformance/helpers.py probe_arguments().
PROBE_ARGS: dict[str, Any] = {
    "string_field": "interop-probe",
    "int_field": 42,
    "float_field": 2.5,
    "bool_field": True,
    "enum_field": "beta",
    "nullable_field": None,
    "union_field": "seven",
    "list_field": ["a", "b"],
    "nested_field": {"id": "n1", "tags": ["x", "y"]},
    "nested_list_field": [{"name": "first", "count": 1}],
}

#: The M2.1 stdio proxy executable (D8 addendum: the repo's wire instrument).
HELPERS_PATH = REPO_ROOT / "tests" / "conformance" / "helpers.py"

#: The ADK env interpreter (DECISIONS.md D1: google-adk[mcp] pins mcp 1.x and
#: cannot coexist with the main env's mcp 2.x).
ADK_ENV_PYTHON = REPO_ROOT / "envs" / "adk" / ".venv" / "bin" / "python"

SERVER_MODULE = {
    "fastmcp": "mcp_sdk_bench.servers.fastmcp",
    "official": "mcp_sdk_bench.servers.official",
}


@dataclasses.dataclass(frozen=True)
class Pairing:
    """One client->server cell of the §8 INTEROPERABILITY matrix."""

    name: str
    client_sdk: str
    server_sdk: str


PAIRINGS: tuple[Pairing, ...] = (
    Pairing("fastmcp->fastmcp", "fastmcp", "fastmcp"),
    Pairing("fastmcp->official", "fastmcp", "official"),
    Pairing("official->fastmcp", "official", "fastmcp"),
    Pairing("adk->fastmcp", "adk", "fastmcp"),
    Pairing("adk->official", "adk", "official"),
)


@dataclasses.dataclass
class PairingResult:
    """One pairing's outcome. Classification vocabulary (SPEC.md §7 honesty):
    "pass" | "sdk_defect" | "negotiation_failure" | "harness_limitation"."""

    pairing: str
    client_sdk: str
    server_sdk: str
    connected: bool = False
    protocol_version_client: str | None = None  # client-announced (wire)
    protocol_version_server: str | None = None  # server-accepted (wire)
    discovery_ok: bool = False
    tools_seen: int = 0
    resources_seen: int = 0
    prompts_seen: int = 0
    roundtrip_ok: bool = False
    error: str | None = None
    classification: str = "pass"

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class AdkEnvUnavailable(RuntimeError):
    """envs/adk interpreter missing — the ADK pairings cannot run here."""


# ---- wire log parsing (proxy "log" mode output) ----


def read_wire_log(path: Path) -> list[dict[str, Any]]:
    """Parse a proxy log-mode JSONL file into [{"direction", "frame"}]."""
    if not path.exists():
        return []
    frames = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            frames.append(json.loads(line))
    return frames


#: mcp_types._types.PROTOCOL_VERSION_META_KEY — duplicated literally so this
#: module stays importable in envs/adk (mcp 1.x lacks the constant).
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"


def _meta_version(frame: dict[str, Any]) -> str | None:
    meta = frame.get("params", {}).get("_meta")
    if not isinstance(meta, dict):
        return None
    version = meta.get(PROTOCOL_VERSION_META_KEY)
    return version if isinstance(version, str) else None


def initialize_exchange(frames: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the (initialize request, initialize response) wire frames.

    Raises ValueError with the observed frame count when either side is
    absent — that absence IS the negotiation evidence.
    """
    request = next(
        (
            f["frame"]
            for f in frames
            if f.get("direction") == "c2s"
            and isinstance(f.get("frame"), dict)
            and f["frame"].get("method") == "initialize"
        ),
        None,
    )
    if request is None:
        raise ValueError(f"no initialize request in wire log ({len(frames)} frames)")
    response = next(
        (
            f["frame"]
            for f in frames
            if f.get("direction") == "s2c"
            and isinstance(f.get("frame"), dict)
            and f["frame"].get("id") == request.get("id")
            and "result" in f["frame"]
        ),
        None,
    )
    if response is None:
        raise ValueError(
            f"no initialize response for id={request.get('id')!r} ({len(frames)} frames)"
        )
    return request, response


def discover_exchange(frames: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the (server/discover request, response) wire frames — the
    modern-era (2026-07-28) replacement for the initialize handshake that
    FastMCP 4.x clients perform against discover-capable servers."""
    request = next(
        (
            f["frame"]
            for f in frames
            if f.get("direction") == "c2s"
            and isinstance(f.get("frame"), dict)
            and f["frame"].get("method") == "server/discover"
        ),
        None,
    )
    if request is None:
        raise ValueError(f"no server/discover request in wire log ({len(frames)} frames)")
    response = next(
        (
            f["frame"]
            for f in frames
            if f.get("direction") == "s2c"
            and isinstance(f.get("frame"), dict)
            and f["frame"].get("id") == request.get("id")
            and "result" in f["frame"]
        ),
        None,
    )
    if response is None:
        raise ValueError(
            f"no server/discover response for id={request.get('id')!r} ({len(frames)} frames)"
        )
    return request, response


def negotiated_version_from_wire(frames: list[dict[str, Any]]) -> str | None:
    """The protocol version the client actually STAMPS on its first
    post-handshake request — wire proof of which version won, independent of
    any SDK self-reporting."""
    for record in frames:
        frame = record.get("frame")
        if (
            record.get("direction") == "c2s"
            and isinstance(frame, dict)
            and frame.get("method") not in ("initialize", "server/discover")
        ):
            version = _meta_version(frame)
            if version is not None:
                return version
    return None


def wire_versions(log_file: Path) -> tuple[str | None, str | None]:
    """(client-announced, negotiated) protocol versions from the wire.

    Legacy era: initialize request params.protocolVersion (client offer) and
    initialize response result.protocolVersion (server pick). Modern era
    (FastMCP 4.x client): server/discover request _meta stamp (client
    announcement) and the version stamped on the client's first subsequent
    request (the negotiated version in use).
    """
    frames = read_wire_log(log_file)
    try:
        request, response = initialize_exchange(frames)
        offered = request.get("params", {}).get("protocolVersion")
        accepted = response.get("result", {}).get("protocolVersion")
        return (
            offered if isinstance(offered, str) else None,
            accepted if isinstance(accepted, str) else None,
        )
    except ValueError:
        pass
    try:
        request, _ = discover_exchange(frames)
    except ValueError:
        return None, None
    return _meta_version(request), negotiated_version_from_wire(frames)


def classify_connect_error(err: BaseException) -> str:
    """negotiation_failure when the failure text names the protocol version
    handshake; anything else on connect is an SDK defect."""
    text = str(err).lower()
    if "protocol" in text and "version" in text:
        return "negotiation_failure"
    return "sdk_defect"


# ---- shared pairing primitives ----


def log_proxy_command(python: str, server_args: list[str], log_file: Path) -> list[str]:
    """argv for a server behind the log-mode proxy (pass-through + record)."""
    return [
        python,
        str(HELPERS_PATH),
        "--mode",
        "log",
        "--log-file",
        str(log_file),
        "--",
        python,
        *server_args,
    ]


def check_probe_echo(structured: dict[str, Any] | None) -> bool:
    """Round-trip assertion: one primitive and one nested field echoed."""
    if not isinstance(structured, dict):
        return False
    received = structured.get("received")
    if not isinstance(received, dict):
        return False
    return (
        received.get("string_field") == PROBE_ARGS["string_field"]
        and received.get("nested_field") == PROBE_ARGS["nested_field"]
    )


def check_ticket(structured: dict[str, Any] | None) -> bool:
    """get_ticket("PAY-123") returns the seeded ticket."""
    if not isinstance(structured, dict):
        return False
    ticket = structured.get("ticket")
    return (
        isinstance(ticket, dict)
        and ticket.get("id") == "PAY-123"
        and ticket.get("status") == "OPEN"
    )


async def run_pairing(pairing: Pairing, log_dir: Path) -> PairingResult:
    """Execute one pairing end-to-end (connect/initialize, discovery,
    round-trip, teardown) and return the classified result."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{pairing.name.replace('->', '_to_')}.wire.jsonl"
    if pairing.client_sdk == "adk":
        return await _run_adk_pairing(pairing, log_file)
    if pairing.client_sdk == "fastmcp":
        return await _run_fastmcp_client_pairing(pairing, log_file)
    return await _run_official_client_pairing(pairing, log_file)


async def _run_fastmcp_client_pairing(pairing: Pairing, log_file: Path) -> PairingResult:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    result = PairingResult(pairing.name, pairing.client_sdk, pairing.server_sdk)
    argv = log_proxy_command(
        sys.executable, ["-m", SERVER_MODULE[pairing.server_sdk]], log_file
    )
    transport = StdioTransport(command=argv[0], args=argv[1:], keep_alive=False)
    try:
        async with Client(transport) as client:
            result.connected = True
            try:
                tools = await client.list_tools()
                resources = await client.list_resources()
                prompts = await client.list_prompts()
                result.tools_seen = len(tools)
                result.resources_seen = len(resources)
                result.prompts_seen = len(prompts)
                result.discovery_ok = (
                    {t.name for t in tools} == EXPECTED_TOOLS
                    and DEPLOYMENT_POLICY_URI in {str(r.uri) for r in resources}
                    and INCIDENT_TRIAGE_PROMPT in {p.name for p in prompts}
                )
                if not result.discovery_ok:
                    result.error = (
                        f"discovery mismatch: tools={sorted(t.name for t in tools)} "
                        f"resources={[str(r.uri) for r in resources]} "
                        f"prompts={[p.name for p in prompts]}"
                    )
                    result.classification = "sdk_defect"
            except Exception as err:  # noqa: BLE001 — failure is the evidence
                result.error = f"discovery: {err}"
                result.classification = "sdk_defect"
                return result
            try:
                ticket = await client.call_tool(
                    "get_ticket", {"ticket_id": "PAY-123"}, raise_on_error=False
                )
                probe = await client.call_tool(
                    "probe_schema", PROBE_ARGS, raise_on_error=False
                )
                result.roundtrip_ok = (
                    not ticket.is_error
                    and not probe.is_error
                    and check_ticket(ticket.structured_content)
                    and check_probe_echo(probe.structured_content)
                )
                if not result.roundtrip_ok:
                    result.error = (
                        f"round-trip mismatch: ticket={ticket.structured_content!r} "
                        f"probe={probe.structured_content!r}"
                    )
                    result.classification = "sdk_defect"
            except Exception as err:  # noqa: BLE001 — failure is the evidence
                result.error = f"round-trip: {err}"
                result.classification = "sdk_defect"
    except Exception as err:  # noqa: BLE001 — connect/teardown failure is the evidence
        result.error = f"connect/teardown: {err}"
        result.classification = classify_connect_error(err)
    finally:
        offered, accepted = wire_versions(log_file)
        result.protocol_version_client = offered
        result.protocol_version_server = accepted
    return result


async def _run_official_client_pairing(pairing: Pairing, log_file: Path) -> PairingResult:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    result = PairingResult(pairing.name, pairing.client_sdk, pairing.server_sdk)
    argv = log_proxy_command(
        sys.executable, ["-m", SERVER_MODULE[pairing.server_sdk]], log_file
    )
    params = StdioServerParameters(command=argv[0], args=argv[1:])
    try:
        async with (
            stdio_client(params) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            await session.initialize()
            result.connected = True
            try:
                tools = await session.list_tools()
                resources = await session.list_resources()
                prompts = await session.list_prompts()
                result.tools_seen = len(tools.tools)
                result.resources_seen = len(resources.resources)
                result.prompts_seen = len(prompts.prompts)
                result.discovery_ok = (
                    {t.name for t in tools.tools} == EXPECTED_TOOLS
                    and DEPLOYMENT_POLICY_URI in {str(r.uri) for r in resources.resources}
                    and INCIDENT_TRIAGE_PROMPT in {p.name for p in prompts.prompts}
                )
                if not result.discovery_ok:
                    result.error = (
                        f"discovery mismatch: tools={sorted(t.name for t in tools.tools)} "
                        f"resources={[str(r.uri) for r in resources.resources]} "
                        f"prompts={[p.name for p in prompts.prompts]}"
                    )
                    result.classification = "sdk_defect"
            except Exception as err:  # noqa: BLE001 — failure is the evidence
                result.error = f"discovery: {err}"
                result.classification = "sdk_defect"
                return result
            try:
                ticket = await session.call_tool("get_ticket", {"ticket_id": "PAY-123"})
                probe = await session.call_tool("probe_schema", PROBE_ARGS)
                result.roundtrip_ok = (
                    not ticket.is_error
                    and not probe.is_error
                    and check_ticket(ticket.structured_content)
                    and check_probe_echo(probe.structured_content)
                )
                if not result.roundtrip_ok:
                    result.error = (
                        f"round-trip mismatch: ticket={ticket.structured_content!r} "
                        f"probe={probe.structured_content!r}"
                    )
                    result.classification = "sdk_defect"
            except Exception as err:  # noqa: BLE001 — failure is the evidence
                result.error = f"round-trip: {err}"
                result.classification = "sdk_defect"
    except Exception as err:  # noqa: BLE001 — connect/teardown failure is the evidence
        result.error = f"connect/teardown: {err}"
        result.classification = classify_connect_error(err)
    finally:
        offered, accepted = wire_versions(log_file)
        result.protocol_version_client = offered
        result.protocol_version_server = accepted
    return result


async def _run_adk_pairing(pairing: Pairing, log_file: Path) -> PairingResult:
    """ADK client pairings run inside envs/adk (D1): interop_adk_driver is
    re-executed there and writes a PairingResult JSON the parent reads back."""
    if not ADK_ENV_PYTHON.exists():
        raise AdkEnvUnavailable(
            f"ADK env interpreter missing ({ADK_ENV_PYTHON}) — "
            "create it with `uv sync --project envs/adk` (DECISIONS.md D1)"
        )
    out_file = log_file.with_suffix(".result.json")
    env = dict(os.environ)
    src = str(REPO_ROOT / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{src}{os.pathsep}{existing}" if existing else src
    proc = await asyncio.create_subprocess_exec(
        str(ADK_ENV_PYTHON),
        "-m",
        "mcp_sdk_bench.benchmark.interop_adk_driver",
        "--server",
        pairing.server_sdk,
        "--log-file",
        str(log_file),
        "--out",
        str(out_file),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=REPO_ROOT,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        result = PairingResult(pairing.name, pairing.client_sdk, pairing.server_sdk)
        result.error = "ADK driver subprocess timed out after 180s"
        result.classification = "harness_limitation"
        return result
    if out_file.exists():
        data = json.loads(out_file.read_text())
        return PairingResult(**data)
    result = PairingResult(pairing.name, pairing.client_sdk, pairing.server_sdk)
    tail = stderr.decode("utf-8", errors="replace")[-500:]
    result.error = f"ADK driver exited {proc.returncode} without a result: {tail}"
    result.classification = (
        "negotiation_failure" if "protocol" in tail.lower() and "version" in tail.lower()
        else "harness_limitation"
    )
    return result


def run_interop(log_dir: Path | None = None) -> list[dict[str, Any]]:
    """Run all five §8 INTEROPERABILITY pairings (sequentially — deterministic
    ordering on the 2-core benchmark host) and write
    results/latest/interoperability.json. Returns the pairing result dicts."""
    log_dir = log_dir or (LATEST_DIR / "interop-wire")

    async def _run_all() -> list[PairingResult]:
        results = []
        for pairing in PAIRINGS:
            try:
                results.append(await run_pairing(pairing, log_dir))
            except AdkEnvUnavailable as err:
                result = PairingResult(pairing.name, pairing.client_sdk, pairing.server_sdk)
                result.error = str(err)
                result.classification = "harness_limitation"
                results.append(result)
        return results

    results = asyncio.run(_run_all())
    payload = {
        "run_id": new_run_id(),
        "milestone": "M2.2",
        "pairings": [r.to_dict() for r in results],
    }
    LATEST_DIR.mkdir(parents=True, exist_ok=True)
    (LATEST_DIR / "interoperability.json").write_text(json.dumps(payload, indent=2))
    return payload["pairings"]
