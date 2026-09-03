"""MCP adapter boundary (SPEC.md §2, layer L3).

Exports the common protocol view (base.py) and one adapter per candidate.
Every candidate import is guarded: the main env has no google.adk, the adk
env has no fastmcp (and pins mcp 1.x), so neither env can import all three.
A missing candidate exports None plus its reason instead of breaking import.
"""
from __future__ import annotations

from mcp_sdk_bench.adapters.base import (
    Discovery,
    MCPAdapter,
    PromptSpec,
    ResourceSpec,
    ToolResult,
    ToolSpec,
)

_UNAVAILABLE: dict[str, str] = {}

try:
    from mcp_sdk_bench.adapters.official import OfficialAdapter
except ImportError as err:  # mcp 2.x not installed in this env
    OfficialAdapter = None  # type: ignore[assignment, misc]
    _UNAVAILABLE["OfficialAdapter"] = str(err)

try:
    from mcp_sdk_bench.adapters.fastmcp import FastMCPAdapter
except ImportError as err:  # fastmcp not installed in this env (e.g. envs/adk)
    FastMCPAdapter = None  # type: ignore[assignment, misc]
    _UNAVAILABLE["FastMCPAdapter"] = str(err)

try:
    from mcp_sdk_bench.adapters.adk import AdkAdapter
except ImportError as err:  # google.adk not installed in this env
    AdkAdapter = None  # type: ignore[assignment, misc]
    _UNAVAILABLE["AdkAdapter"] = str(err)

ADAPTER_UNAVAILABLE_REASONS = _UNAVAILABLE

__all__ = [
    "ADAPTER_UNAVAILABLE_REASONS",
    "AdkAdapter",
    "Discovery",
    "FastMCPAdapter",
    "MCPAdapter",
    "OfficialAdapter",
    "PromptSpec",
    "ResourceSpec",
    "ToolResult",
    "ToolSpec",
]
