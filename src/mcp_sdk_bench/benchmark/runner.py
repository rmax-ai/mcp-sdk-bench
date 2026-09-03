"""Benchmark runner skeleton (SPEC.md §2, layer L2).

Runs one task against one adapter + one compiled agent graph and returns the
per-task result record. Full metric/trace/grader plumbing (SPEC.md §10/§22)
lands in later M1.x stories; this is the run-shape the agent evaluations use.
"""
from __future__ import annotations

import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from mcp_sdk_bench.adapters.base import Discovery, MCPAdapter, ToolResult
from mcp_sdk_bench.agent.graph import MAX_TOOL_ITERATIONS

# Each tool iteration costs two supersteps (llm + tools); allow the cap plus
# entry/final steps so the graph never hits langgraph's recursion limit first.
RECURSION_LIMIT = 2 * MAX_TOOL_ITERATIONS + 4


class AccessRecordingAdapter(MCPAdapter):
    """Adapter proxy that records resource reads and prompt gets as pseudo
    tool calls so they land in the result's tool_calls trace (SPEC.md §22).

    Pass the wrapped adapter to build_agent AND to run_task; the graph's
    tool calls still flow through the inner adapter unchanged, and every
    read_resource / get_prompt is appended to `recorded_access` with the
    shapes {"name": "read_resource", "arguments": {"uri": ...}} and
    {"name": "get_prompt", "arguments": {"name": ..., ...}}.
    """

    def __init__(self, inner: MCPAdapter) -> None:
        self._inner = inner
        self.recorded_access: list[dict] = []

    async def connect(self) -> Discovery:
        return await self._inner.connect()

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        return await self._inner.call_tool(name, arguments)

    async def read_resource(self, uri: str) -> str:
        self.recorded_access.append({"name": "read_resource", "arguments": {"uri": uri}})
        return await self._inner.read_resource(uri)

    async def get_prompt(self, name: str, arguments: dict) -> str:
        self.recorded_access.append(
            {"name": "get_prompt", "arguments": {"name": name, **arguments}}
        )
        return await self._inner.get_prompt(name, arguments)

    async def close(self) -> None:
        await self._inner.close()


def _final_answer(messages: list) -> str | None:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            if isinstance(message.content, str):
                return message.content
            return "".join(
                block.get("text", "")
                for block in message.content
                if isinstance(block, dict) and block.get("type") == "text"
            )
    return None


def _round_trips(messages: list) -> int:
    """Agent iterations that produced at least one tool call."""
    return sum(
        1
        for message in messages
        if isinstance(message, AIMessage) and message.tool_calls
    )


def _token_totals(messages: list) -> tuple[int, int]:
    """Sum usage_metadata across model messages (langchain AIMessage shape)."""
    in_tok = out_tok = 0
    for message in messages:
        if isinstance(message, AIMessage):
            usage = getattr(message, "usage_metadata", None) or {}
            in_tok += int(usage.get("input_tokens") or 0)
            out_tok += int(usage.get("output_tokens") or 0)
    return in_tok, out_tok


async def run_task(task: dict, adapter: MCPAdapter, agent_graph: Any) -> dict:
    """Execute one task. Returns the skeleton result record; errors are
    captured in `error` rather than raised so one task cannot kill a sweep."""
    start = time.perf_counter()
    error: str | None = None
    state: dict = {"messages": [], "tool_calls": [], "mcp_latency_ms": 0.0}
    try:
        state = await agent_graph.ainvoke(
            {
                "messages": [HumanMessage(content=task["prompt"])],
                "iterations": 0,
                "tool_calls": [],
                "mcp_latency_ms": 0.0,
            },
            config={"recursion_limit": RECURSION_LIMIT},
        )
    except Exception as err:  # noqa: BLE001 — a failed task is data, not a crash; one task must not kill a sweep
        error = str(err)
    total_latency_ms = (time.perf_counter() - start) * 1000
    mcp_latency_ms = float(state.get("mcp_latency_ms", 0.0))
    input_tokens, output_tokens = _token_totals(state.get("messages", []))

    tool_calls = list(state.get("tool_calls", []))
    # Resource/prompt access recorded by AccessRecordingAdapter surfaces as
    # pseudo tool calls, appended after the graph's own tool calls.
    recorded = getattr(adapter, "recorded_access", None)
    if recorded:
        tool_calls.extend(recorded)

    return {
        "task_id": task["id"],
        "sdk": task["sdk"],
        "tool_calls": tool_calls,
        "round_trips": _round_trips(state.get("messages", [])),
        "total_latency_ms": total_latency_ms,
        "mcp_latency_ms": mcp_latency_ms,
        "model_latency_ms": total_latency_ms - mcp_latency_ms,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "final_answer": _final_answer(state.get("messages", [])),
        "error": error,
    }
