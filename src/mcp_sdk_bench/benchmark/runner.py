"""Benchmark runner skeleton (SPEC.md §2, layer L2).

Runs one task against one adapter + one compiled agent graph and returns the
per-task result record. Full metric/trace/grader plumbing (SPEC.md §10/§22)
lands in later M1.x stories; this is the run-shape the agent evaluations use.
"""
from __future__ import annotations

import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from mcp_sdk_bench.adapters.base import MCPAdapter
from mcp_sdk_bench.agent.graph import MAX_TOOL_ITERATIONS

# Each tool iteration costs two supersteps (llm + tools); allow the cap plus
# entry/final steps so the graph never hits langgraph's recursion limit first.
RECURSION_LIMIT = 2 * MAX_TOOL_ITERATIONS + 4


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

    return {
        "task_id": task["id"],
        "sdk": task["sdk"],
        "tool_calls": state.get("tool_calls", []),
        "round_trips": _round_trips(state.get("messages", [])),
        "total_latency_ms": total_latency_ms,
        "mcp_latency_ms": state.get("mcp_latency_ms", 0.0),
        "final_answer": _final_answer(state.get("messages", [])),
        "error": error,
    }
