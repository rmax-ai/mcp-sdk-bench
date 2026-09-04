"""Google ADK adapter (google-adk 2.8.0, McpToolset over stdio).

Runs only under envs/adk (google-adk[mcp] pins mcp 1.x, incompatible with the
main env's mcp 2.x); google.adk is imported lazily inside connect() and a
clear RuntimeError is raised when it is unavailable.

Honest capability surface (SPEC.md §7 — absence shown as absence, never
emulated): ADK's McpToolset is a *tool* integration. It has no first-class
client surface for MCP resources or prompts (resources can only be surfaced
to a Gemini agent as extra tools via load_mcp_resource_tool, which is an
agent-framework abstraction, not the MCP resource protocol). This adapter
therefore reports Discovery(resources=[], prompts=[]) and read_resource /
get_prompt raise RuntimeError stating the gap.

Probed against the installed ADK 2.8.0: McpToolset.get_tools() returns McpTool
objects whose .raw_mcp_tool is an mcp 1.x Tool (camelCase .inputSchema);
McpTool.run_async(args=..., tool_context=...) returns the CallToolResult as a
dict with camelCase keys (isError, structuredContent, content).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from mcp_sdk_bench.adapters.base import (
    Discovery,
    MCPAdapter,
    ToolResult,
    ToolSpec,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

ADK_ENV_MESSAGE = "ADK adapter requires the envs/adk environment"

RESOURCE_GAP = (
    "ADK McpToolset exposes no first-class MCP resource surface "
    "(resources only via load_mcp_resource_tool, an agent-side abstraction)"
)
PROMPT_GAP = "ADK McpToolset exposes no MCP prompt surface"


def _server_env() -> dict[str, str]:
    """mcp 1.x stdio_client defaults to a minimal env that drops PYTHONPATH;
    forward ours (plus repo src) so the subprocess can import mcp_sdk_bench."""
    env = dict(os.environ)
    src = str(REPO_ROOT / "src")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{src}{os.pathsep}{existing}" if existing else src
    return env


def adk_env_ok() -> bool:
    """True only when the real ADK imports EXECUTE (not just resolve).

    google.adk is installed in the main env too, but importing mcp_toolset
    there raises ImportError because google-adk[mcp] needs mcp 1.x while the
    main env pins mcp 2.x (DECISIONS.md D1). find_spec-based checks miss
    this; execute the exact import chain connect() needs instead."""
    try:
        from google.adk.agents.invocation_context import InvocationContext  # noqa: F401
        from google.adk.sessions import InMemorySessionService  # noqa: F401
        from google.adk.tools.mcp_tool.mcp_toolset import (  # noqa: F401
            McpToolset,
            StdioConnectionParams,
        )
        from google.adk.tools.tool_context import ToolContext  # noqa: F401
        from mcp import StdioServerParameters  # noqa: F401

        return True
    except ImportError:
        return False


class AdkAdapter(MCPAdapter):
    def __init__(self) -> None:
        self._toolset: Any = None
        self._tools_by_name: dict[str, Any] = {}
        self._context_factory: Any = None

    async def connect(self) -> Discovery:
        if not adk_env_ok():
            raise RuntimeError(ADK_ENV_MESSAGE)

        from google.adk.agents.invocation_context import InvocationContext
        from google.adk.sessions import InMemorySessionService
        from google.adk.tools.mcp_tool.mcp_toolset import (
            McpToolset,
            StdioConnectionParams,
        )
        from google.adk.tools.tool_context import ToolContext
        from mcp import StdioServerParameters

        self._toolset = McpToolset(
            connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "mcp_sdk_bench.servers.adk"],
                    env=_server_env(),
                ),
                timeout=30.0,
            )
        )
        try:
            tools = await self._toolset.get_tools()
        except Exception:
            await self._toolset.close()
            self._toolset = None
            raise

        self._tools_by_name = {tool.name: tool for tool in tools}

        # McpTool.run_async needs a ToolContext; build one minimal invocation
        # (in-memory session) to reuse across calls.
        session_service = InMemorySessionService()
        session = await session_service.create_session(
            app_name="mcp_sdk_bench", user_id="bench"
        )
        invocation = InvocationContext(
            session_service=session_service,
            session=session,
            invocation_id="mcp-sdk-bench-adk-adapter",
        )

        def _context_factory() -> Any:
            return ToolContext(invocation)

        self._context_factory = _context_factory

        return Discovery(
            tools=[
                ToolSpec(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=dict(tool.raw_mcp_tool.inputSchema),
                )
                for tool in tools
            ],
            # Honest gap: McpToolset has no first-class resource/prompt
            # surface (see module docstring). Empty lists, not emulation.
            resources=[],
            prompts=[],
        )

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        tool = self._tools_by_name.get(name)
        if tool is None:
            return ToolResult(is_error=True, text=f"unknown tool {name}")
        try:
            result = await tool.run_async(
                args=dict(arguments), tool_context=self._context_factory()
            )
        except Exception as err:  # noqa: BLE001 — any candidate-side failure maps to a tool error, never crashes the agent loop
            return ToolResult(is_error=True, text=str(err))
        # mcp 1.x dumps CallToolResult with camelCase keys; read both
        # spellings so a future mcp 2.x-based ADK keeps working.
        is_error = result.get("isError", result.get("is_error", False))
        structured = result.get(
            "structuredContent", result.get("structured_content")
        )
        content = result.get("content") or []
        text = "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        return ToolResult(
            is_error=bool(is_error),
            structured_content=structured,
            text=text or None,
        )

    async def read_resource(self, uri: str) -> str:
        raise RuntimeError(RESOURCE_GAP)

    async def get_prompt(self, name: str, arguments: dict) -> str:
        raise RuntimeError(PROMPT_GAP)

    async def close(self) -> None:
        if self._toolset is not None:
            await self._toolset.close()
            self._toolset = None
            self._tools_by_name = {}
