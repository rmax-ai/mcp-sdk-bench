"""cancel-at-progress policy tests (SPEC.md §9 H, M3.3) — hermetic, NO LLM.

Unit-tests the new ScriptedUserSimulator policy (fires the user's
"Actually — cancel the migration." exactly once, at the first tool result
whose migration progress reaches the threshold) and proves the agent-loop
wiring (agent/graph.py tools_node observe hook): the injected message lands
as a user message AFTER the tool messages and increments user_interactions
via the same accounting channel as M3.1 elicitations.
"""
from __future__ import annotations

from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from mcp_sdk_bench.adapters.base import Discovery, MCPAdapter, ToolResult, ToolSpec
from mcp_sdk_bench.agent.graph import build_agent
from mcp_sdk_bench.agent.simulator import (
    CANCEL_MIGRATION_MESSAGE,
    ScriptedUserSimulator,
)
from mcp_sdk_bench.benchmark.runner import RECURSION_LIMIT

MIGRATION_TOOL_SPECS = [
    ToolSpec(
        name="start_migration",
        description="Start a migration for a scope.",
        input_schema={
            "type": "object",
            "properties": {"scope": {"type": "string"}},
            "required": ["scope"],
        },
    ),
    ToolSpec(
        name="get_migration_status",
        description="Poll a migration by handle.",
        input_schema={
            "type": "object",
            "properties": {"handle": {"type": "string"}},
            "required": ["handle"],
        },
    ),
    ToolSpec(
        name="cancel_migration",
        description="Cancel a running migration by handle.",
        input_schema={
            "type": "object",
            "properties": {"handle": {"type": "string"}},
            "required": ["handle"],
        },
    ),
]


# ---- simulator unit tests ----


def test_policy_parse_accepts_in_range_threshold() -> None:
    sim = ScriptedUserSimulator("cancel-at-progress:0.5")
    assert sim.cancel_threshold == 0.5
    assert sim.policy == "cancel-at-progress:0.5"


def test_policy_parse_rejects_bad_thresholds() -> None:
    with pytest.raises(ValueError, match="threshold must be within 0.0..1.0"):
        ScriptedUserSimulator("cancel-at-progress:1.5")
    with pytest.raises(ValueError, match="threshold must be within 0.0..1.0"):
        ScriptedUserSimulator("cancel-at-progress:-0.1")
    with pytest.raises(ValueError, match="invalid cancel-at-progress threshold"):
        ScriptedUserSimulator("cancel-at-progress:abc")


async def test_observe_fires_at_or_above_threshold_exactly_once() -> None:
    sim = ScriptedUserSimulator("cancel-at-progress:0.5")

    # Below threshold -> nothing.
    assert (
        await sim.observe_tool_result(
            "get_migration_status",
            {"task": {"status": "running", "progress": 0.25}},
            None,
        )
        is None
    )
    # Exactly at threshold -> the cancellation fires.
    message = await sim.observe_tool_result(
        "get_migration_status",
        {"task": {"status": "running", "progress": 0.5}},
        None,
    )
    assert message == CANCEL_MIGRATION_MESSAGE
    assert message == "Actually — cancel the migration."
    # Above threshold again -> fired already, nothing more.
    assert (
        await sim.observe_tool_result(
            "get_migration_status",
            {"task": {"status": "running", "progress": 0.75}},
            None,
        )
        is None
    )


async def test_observe_ignores_non_task_views_and_text_only_results() -> None:
    sim = ScriptedUserSimulator("cancel-at-progress:0.0")  # any progress qualifies
    # Text-only result (e.g. an errored call, or a non-task tool).
    assert await sim.observe_tool_result("get_migration_status", None, "boom") is None
    # Structured content without the task envelope.
    assert (
        await sim.observe_tool_result(
            "get_ticket", {"ticket": {"status": "OPEN"}}, None
        )
        is None
    )
    # The envelope key must carry a numeric progress.
    assert (
        await sim.observe_tool_result(
            "get_migration_status", {"task": {"status": "running"}}, None
        )
        is None
    )


async def test_other_policies_never_inject() -> None:
    for policy in ("none", "auto-approve", "auto-decline", "clarify-with:staging v1.7.0"):
        sim = ScriptedUserSimulator(policy)
        assert (
            await sim.observe_tool_result(
                "get_migration_status", {"task": {"status": "running", "progress": 1.0}}, None
            )
            is None
        )


# ---- agent-loop wiring (observe hook in tools_node) ----


class BindableFakeChatModel(GenericFakeChatModel):
    """GenericFakeChatModel does not implement bind_tools; for the loop the
    tool schema is irrelevant, so return self unchanged (regression pattern)."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


class MigrationStubAdapter(MCPAdapter):
    """Stub MCP adapter that answers migration poll/cancel calls with task
    views carrying progress, so the observe hook has real structured content
    to parse."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def connect(self) -> Discovery:
        return Discovery(tools=MIGRATION_TOOL_SPECS, resources=[], prompts=[])

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        self.calls.append((name, arguments))
        handle = arguments.get("handle", "migrate-001")
        if name == "get_migration_status":
            return ToolResult(
                structured_content={
                    "task": {"handle": handle, "status": "running", "progress": 0.75}
                },
                text="running",
            )
        if name == "cancel_migration":
            return ToolResult(
                structured_content={
                    "task": {
                        "handle": handle,
                        "status": "cancelled",
                        "progress": 0.75,
                        "result": None,
                        "error": None,
                    }
                },
                text="cancelled",
            )
        return ToolResult(is_error=True, text=f"unknown tool {name}")

    async def read_resource(self, uri: str) -> str:
        raise RuntimeError("stub adapter does not serve resources")

    async def get_prompt(self, name: str, arguments: dict) -> str:
        raise RuntimeError("stub adapter does not serve prompts")

    async def close(self) -> None:
        pass


def _tool_call(name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


async def test_agent_loop_injects_cancel_message_and_counts_interaction() -> None:
    """Full-graph proof: poll reaches the threshold, the scripted user's
    cancellation is injected between the tool messages and the model's next
    turn, and the run records one user interaction (the h-02 lane)."""
    model = BindableFakeChatModel(
        messages=iter(
            [
                _tool_call("get_migration_status", {"handle": "migrate-001"}, "call-1"),
                _tool_call("cancel_migration", {"handle": "migrate-001"}, "call-2"),
                AIMessage(content="The migration was cancelled as you asked."),
            ]
        )
    )
    adapter = MigrationStubAdapter()
    graph = build_agent(
        MIGRATION_TOOL_SPECS,
        adapter,
        model=model,
        user_simulator=ScriptedUserSimulator("cancel-at-progress:0.5"),
    )

    final = await graph.ainvoke(
        {
            "messages": [
                HumanMessage(content="Start the migration now. I may change my mind.")
            ],
            "iterations": 0,
            "tool_calls": [],
            "mcp_latency_ms": 0.0,
            "user_interactions": 0,
            "elicitation_round_trips": 0,
        },
        config={"recursion_limit": RECURSION_LIMIT},
    )

    # The injected user message is present, in order, exactly once.
    messages = final["messages"]
    injected = [
        m for m in messages if isinstance(m, HumanMessage) and m.content.startswith("User message:")
    ]
    assert [m.content for m in injected] == [
        "User message: Actually — cancel the migration."
    ]
    # It landed after the poll ToolMessage and before the final answer
    # (message order: [ToolMessage(poll), ToolMessage(cancel),
    # HumanMessage(cancel), AIMessage(final)]).
    poll_pos = next(
        i
        for i, m in enumerate(messages)
        if isinstance(m, ToolMessage) and m.tool_call_id == "call-1"
    )
    inject_pos = next(
        i
        for i, m in enumerate(messages)
        if isinstance(m, HumanMessage) and m.content.startswith("User message:")
    )
    final_pos = next(
        i
        for i, m in enumerate(messages)
        if isinstance(m, AIMessage) and m.content == "The migration was cancelled as you asked."
    )
    assert poll_pos < inject_pos < final_pos
    # Same user_interactions accounting channel as M3.1 elicitations.
    assert final["user_interactions"] == 1
    # The agent proceeded to cancel after the injected user turn.
    assert ("cancel_migration", {"handle": "migrate-001"}) in adapter.calls


async def test_agent_loop_without_cancel_policy_injects_nothing() -> None:
    """Policy "none" (the h-01/h-03 lane): identical tool results but no
    injected user message and no user interaction."""
    model = BindableFakeChatModel(
        messages=iter(
            [
                _tool_call("get_migration_status", {"handle": "migrate-001"}, "call-1"),
                AIMessage(content="The migration is progressing."),
            ]
        )
    )
    adapter = MigrationStubAdapter()
    graph = build_agent(
        MIGRATION_TOOL_SPECS,
        adapter,
        model=model,
        user_simulator=ScriptedUserSimulator("none"),
    )

    final = await graph.ainvoke(
        {
            "messages": [HumanMessage(content="Run the customer-data migration.")],
            "iterations": 0,
            "tool_calls": [],
            "mcp_latency_ms": 0.0,
            "user_interactions": 0,
            "elicitation_round_trips": 0,
        },
        config={"recursion_limit": RECURSION_LIMIT},
    )

    assert not any(
        isinstance(m, HumanMessage) and m.content.startswith("User message:")
        for m in final["messages"]
    )
    assert final["user_interactions"] == 0
