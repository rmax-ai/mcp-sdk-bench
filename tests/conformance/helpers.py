"""Shared conformance harness for SPEC.md §8 (M2.1).

Contents:
- DISCOVERY_CONTRACT: the per-candidate discovery contract (tools, resources,
  prompts). The ADK variant's resources/prompts are EMPTY lists by design:
  ADK's McpToolset has no first-class MCP resource/prompt surface, so the
  adapter reports absence as absence (M1 finding, SPEC.md §7) — never emulated.
- OFFICIAL_SESSION / FAST_MCP_SESSION / ADK_ADAPTER_SESSION: async
  context-manager factories. Each yields a fresh session against a freshly
  spawned server subprocess, so every test sees a freshly seeded world
  (mirrors tests/conformance/test_official_server.py). The ADK factory yields
  the benchmark adapter (src/mcp_sdk_bench/adapters/adk.py): ADK 2.8 ships no
  standalone protocol client, so the adapter IS the canonical driving surface.
- StdioProxy: a deterministic stdio JSON-RPC proxy with corrupt/delay/drop
  fault modes, run as a subprocess between client and server. Wire-level
  faults apply only to the official and FastMCP candidates; the ADK adapter
  drives its server through an SDK-managed channel with no wire access.

This module doubles as the proxy executable:
    python tests/conformance/helpers.py --mode corrupt --nth 2 -- \
        python -m mcp_sdk_bench.servers.official
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_sdk_bench.adapters.adk import ADK_ENV_MESSAGE, AdkAdapter
from mcp_sdk_bench.adapters.base import Discovery

try:  # mcp 2.x (main env)
    from mcp.shared.exceptions import MCPError
except ImportError:  # mcp 1.x (ADK env) spells it McpError
    from mcp.shared.exceptions import (
        McpError as _McpError,  # ty: ignore[unresolved-import]
    )

    # Re-exported under the mcp 2.x spelling for test modules.
    MCPError = _McpError

# ---- contract constants (SPEC.md §8 DISCOVERY) ----

DEPLOYMENT_POLICY_URI = "company://policies/deployment"
INCIDENT_TRIAGE_PROMPT = "incident-triage"

#: JSON-RPC invalid-params code; mirrors INVALID_PARAMS in
#: mcp_sdk_bench.servers.official.server (duplicated so this module stays
#: importable in the mcp 1.x ADK env, where the mcp 2.x-only official server
#: module must not be imported).
INVALID_PARAMS = -32602

#: The M2.1 six-tool contract: the five M1 world tools plus probe_schema.
EXPECTED_TOOLS = frozenset(
    {
        "get_ticket",
        "update_ticket",
        "get_inventory",
        "reserve_inventory",
        "deploy_service",
        "probe_schema",
    }
)


@dataclasses.dataclass(frozen=True)
class CandidateContract:
    tools: frozenset[str]
    resources: tuple[str, ...]
    prompts: tuple[str, ...]
    #: Whether the driving surface exposes the initialize handshake's
    #: protocolVersion. The ADK adapter's handshake is SDK-internal
    #: (McpToolset exposes no initialize result) — harness limitation.
    exposes_protocol_version: bool


DISCOVERY_CONTRACT: dict[str, CandidateContract] = {
    "official": CandidateContract(
        tools=EXPECTED_TOOLS,
        resources=(DEPLOYMENT_POLICY_URI,),
        prompts=(INCIDENT_TRIAGE_PROMPT,),
        exposes_protocol_version=True,
    ),
    "fastmcp": CandidateContract(
        tools=EXPECTED_TOOLS,
        resources=(DEPLOYMENT_POLICY_URI,),
        prompts=(INCIDENT_TRIAGE_PROMPT,),
        exposes_protocol_version=True,
    ),
    # Honest absence (M1 finding): ADK McpToolset has no resource/prompt
    # surface, so the ADK contract lists EMPTY resources/prompts.
    "adk": CandidateContract(
        tools=EXPECTED_TOOLS,
        resources=(),
        prompts=(),
        exposes_protocol_version=False,
    ),
}

# ---- probe_schema fixtures (SPEC.md §8 SCHEMA) ----

PROBE_TOOL = "probe_schema"


def probe_arguments(**overrides: Any) -> dict[str, Any]:
    """Canonical valid probe_schema arguments (JSON round-trippable)."""
    args: dict[str, Any] = {
        "string_field": "hello",
        "int_field": 42,
        "float_field": 2.5,
        "bool_field": True,
        "enum_field": "beta",
        "nullable_field": None,
        "union_field": "seven",
        "list_field": ["a", "b"],
        "nested_field": {"id": "n1", "tags": ["x", "y"]},
        "nested_list_field": [
            {"name": "first", "count": 1},
            {"name": "second", "count": 2},
        ],
    }
    args.update(overrides)
    return args


def expected_probe_echo(args: dict[str, Any]) -> dict[str, Any]:
    """The canonical result every server variant must produce for `args`."""
    return {"received": dict(args), "count": len(args)}


def sdk_defect(candidate: str, what: str) -> str:
    """Assertion-message prefix: the SDK violates its documented behavior."""
    return f"SDK DEFECT [{candidate}]: {what}"


def harness_issue(candidate: str, what: str) -> str:
    """Assertion-message prefix: the harness/setup is at fault, not the SDK."""
    return f"HARNESS ISSUE [{candidate}]: {what}"


# ---- stdio fault-injection proxy (SPEC.md §8 ERRORS / LIFECYCLE) ----

PROXY_MODES = ("corrupt", "delay", "drop")

HELPERS_PATH = Path(__file__).resolve()

OFFICIAL_SERVER_ARGS = ["-m", "mcp_sdk_bench.servers.official"]
FASTMCP_SERVER_ARGS = ["-m", "mcp_sdk_bench.servers.fastmcp"]

#: Replacement body for corrupt mode: bytes that are not valid JSON.
CORRUPT_FRAME = b'{"jsonrpc": "2.0", "id": 0, "result": {CORRUPTED-BY-PROXY\n'


@dataclasses.dataclass(frozen=True)
class ProxyFault:
    """Fault configuration for StdioProxy.

    mode="corrupt": replace the Nth response frame's JSON-RPC body with
        invalid JSON.
    mode="delay": sleep delay_ms milliseconds before forwarding EVERY
        response frame.
    mode="drop": forward the Nth response frame, then close both pipes
        (the proxy exits, killing the server child).

    "Nth response frame" counts only server-to-client JSON-RPC responses
    (frames carrying "id" plus "result" or "error"); notifications and
    requests are not counted. Counters are deterministic; no randomness.
    """

    mode: str
    nth: int = 1
    delay_ms: int = 250

    def __post_init__(self) -> None:
        if self.mode not in PROXY_MODES:
            raise ValueError(f"unknown proxy mode {self.mode!r}; expected one of {PROXY_MODES}")
        if self.nth < 1:
            raise ValueError("nth is 1-based and must be >= 1")


class StdioProxy:
    """Transparent newline-delimited JSON-RPC stdio proxy with deterministic
    fault injection. Runs as a subprocess between the candidate client and the
    real server child it spawns. Forwards stdin/stdout verbatim until the
    configured trigger (see ProxyFault) fires."""

    def __init__(self, command: list[str], fault: ProxyFault) -> None:
        self.command = command
        self.fault = fault
        self.responses_seen = 0
        self._child: asyncio.subprocess.Process | None = None

    @staticmethod
    def _is_response_frame(line: bytes) -> bool:
        try:
            frame = json.loads(line)
        except ValueError:
            return False
        return (
            isinstance(frame, dict)
            and "id" in frame
            and ("result" in frame or "error" in frame)
        )

    async def run(self) -> int:
        self._child = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # stderr inherits: server logs flow through to the test's stderr.
        )
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
        )
        pump = asyncio.create_task(self._forward_requests(reader))
        try:
            await self._forward_responses()
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            await self._terminate_child()
        return 0

    async def _forward_requests(self, reader: asyncio.StreamReader) -> None:
        assert self._child is not None and self._child.stdin is not None
        stdin = self._child.stdin
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                stdin.write(line)
                await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # child already gone (drop mode)
        finally:
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                stdin.close()

    async def _forward_responses(self) -> None:
        assert self._child is not None and self._child.stdout is not None
        out = sys.stdout.buffer
        while True:
            line = await self._child.stdout.readline()
            if not line:
                return  # server exited on its own; pass EOF through by exiting
            is_response = self._is_response_frame(line)
            if is_response:
                self.responses_seen += 1
                if self.fault.mode == "delay":
                    # The delay proxy is the ONLY sanctioned sleep in the
                    # conformance suite (AGENTS.md: deterministic fault
                    # injection; delay_ms is an explicit parameter).
                    await asyncio.sleep(self.fault.delay_ms / 1000)
                if self.fault.mode == "corrupt" and self.responses_seen == self.fault.nth:
                    line = CORRUPT_FRAME
            out.write(line)
            out.flush()
            if self.fault.mode == "drop" and is_response and self.responses_seen == self.fault.nth:
                return  # exit closes both pipes; run()'s finally kills the child

    async def _terminate_child(self) -> None:
        if self._child is None or self._child.returncode is not None:
            return
        self._child.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._child.wait(), timeout=5)


async def eof_shutdown_returncode(
    server_args: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 20.0,
) -> int | None:
    """SPEC.md §8 LIFECYCLE clean-shutdown probe: spawn a server, close its
    stdin immediately (EOF), and return its exit code. Returns None if the
    server failed to exit within `timeout` (killed)."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        *server_args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    assert proc.stdin is not None
    proc.stdin.close()
    try:
        return await asyncio.wait_for(proc.wait(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None


def proxy_args(server_args: list[str], fault: ProxyFault | None) -> list[str]:
    """Build the subprocess args for a (possibly proxied) stdio server."""
    if fault is None:
        return list(server_args)
    return [
        str(HELPERS_PATH),
        "--mode",
        fault.mode,
        "--nth",
        str(fault.nth),
        "--delay-ms",
        str(fault.delay_ms),
        "--",
        sys.executable,
        *server_args,
    ]


# ---- session factories (fresh server subprocess per session) ----


@asynccontextmanager
async def OFFICIAL_SESSION(
    *,
    fault: ProxyFault | None = None,
    read_timeout_seconds: float | None = None,
    message_handler: Callable[..., Any] | None = None,
) -> AsyncIterator[ClientSession]:
    """Official SDK ClientSession over stdio against a fresh official server
    (optionally behind a StdioProxy fault)."""
    params = StdioServerParameters(
        command=sys.executable,
        args=proxy_args(OFFICIAL_SERVER_ARGS, fault),
    )
    session_kwargs: dict[str, Any] = {}
    if read_timeout_seconds is not None:
        session_kwargs["read_timeout_seconds"] = read_timeout_seconds
    if message_handler is not None:
        session_kwargs["message_handler"] = message_handler
    async with (
        stdio_client(params) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream, **session_kwargs) as session,
    ):
        await session.initialize()
        yield session


@asynccontextmanager
async def FAST_MCP_SESSION(
    *,
    fault: ProxyFault | None = None,
    message_handler: Callable[..., Any] | None = None,
) -> AsyncIterator[Any]:
    """FastMCP Client (fastmcp.client.Client) over stdio against a fresh
    FastMCP server subprocess (optionally behind a StdioProxy fault)."""
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command=sys.executable,
        args=proxy_args(FASTMCP_SERVER_ARGS, fault),
        keep_alive=False,
    )
    client_kwargs: dict[str, Any] = {}
    if message_handler is not None:
        client_kwargs["message_handler"] = message_handler
    async with Client(transport, **client_kwargs) as client:
        yield client


@asynccontextmanager
async def ADK_ADAPTER_SESSION() -> AsyncIterator[tuple[AdkAdapter, Discovery]]:
    """The ADK candidate's canonical driving surface: the benchmark's
    AdkAdapter (ADK 2.8 ships no standalone protocol client). Yields
    (adapter, discovery). Skips when the ADK env is unavailable (main env
    pins mcp 2.x; google-adk[mcp] needs 1.x).
    """
    adapter = AdkAdapter()
    try:
        discovery = await adapter.connect()
    except RuntimeError as err:
        if ADK_ENV_MESSAGE in str(err):
            pytest.skip(f"ADK env unavailable ({ADK_ENV_MESSAGE})")
        raise
    try:
        yield adapter, discovery
    finally:
        await adapter.close()


def _main(argv: list[str]) -> int:
    if "--" not in argv:
        print(
            "usage: helpers.py --mode {corrupt,delay,drop} [--nth N] [--delay-ms MS]"
            " -- <server command...>",
            file=sys.stderr,
        )
        return 2
    sep = argv.index("--")
    parser = argparse.ArgumentParser(prog="mcp-sdk-bench-stdio-proxy")
    parser.add_argument("--mode", choices=PROXY_MODES, required=True)
    parser.add_argument("--nth", type=int, default=1)
    parser.add_argument("--delay-ms", type=int, default=250)
    args = parser.parse_args(argv[:sep])
    command = argv[sep + 1 :]
    if not command:
        print("proxy requires a server command after '--'", file=sys.stderr)
        return 2
    proxy = StdioProxy(
        command=command,
        fault=ProxyFault(mode=args.mode, nth=args.nth, delay_ms=args.delay_ms),
    )
    return asyncio.run(proxy.run())


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
