"""Agent-loop elicitation hooks (SPEC.md §18, M3.1), hermetic.

A scripted fake model + a stub adapter with a synthetic pause/resume surface
drive the LangGraph loop without any server or network. Covers: the
category-F clarify hook (pre-first-tool-call user message), the category-G
pause/resume path through respond_to_elicitation, and the recorded counters
(user_interactions, elicitation_round_trips, mcp_round_trips).
"""
from __future__ import annotations

from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from mcp_sdk_bench.adapters.base import Discovery, MCPAdapter, ToolResult, ToolSpec
from mcp_sdk_bench.agent.graph import build_agent
from mcp_sdk_bench.agent.simulator import ScriptedUserSimulator
from mcp_sdk_bench.benchmark.runner import RECURSION_LIMIT, run_task

TOOL_SPECS = [
    ToolSpec(
        name="deploy_service",
        description="Deploy a service.",
        input_schema={
            "type": "object",
            "properties": {
                "service": {"type": "string"},
                "target_version": {"type": "string"},
                "environment": {"type": "string"},
            },
            "required": ["service", "target_version", "environment"],
        },
    )
]

DEPLOY_ARGS = {"service": "checkout", "target_version": "v1.8.3", "environment": "production"}


class BindableFakeChatModel(GenericFakeChatModel):
    """GenericFakeChatModel does not implement bind_tools; for the scripted
    loop the tool schema is irrelevant, so return self unchanged."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


class ElicitingStubAdapter(MCPAdapter):
    """Synthetic pause/resume surface: the first deploy_service call returns
    an approval elicitation request; respond_to_elicitation records the
    payload; the verbatim repeat call completes (approved) or errors
    (declined), mirroring the world seam."""

    def __init__(self) -> None:
        self.paused = False
        self.payloads: list[dict] = []
        self.calls: list[tuple[str, dict]] = []

    async def connect(self) -> Discovery:
        return Discovery(tools=TOOL_SPECS, resources=[], prompts=[])

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        self.calls.append((name, dict(arguments)))
        if not self.paused:
            self.paused = True
            return ToolResult(
                is_error=False,
                text="elicitation requested (approval): Approve deployment?",
                elicitation_request={
                    "kind": "approval",
                    "question": "Approve deployment of checkout v1.8.3 to production?",
                    "schema": {
                        "type": "object",
                        "title": "approval",
                        "properties": {"approved": {"type": "boolean"}},
                        "required": ["approved"],
                    },
                },
            )
        if self.payloads and self.payloads[-1].get("status") == "approved":
            return ToolResult(
                structured_content={
                    "deployment": {
                        "service": "checkout",
                        "version": "v1.8.3",
                        "environment": "production",
                        "status": "active",
                    }
                },
                text="deployed",
            )
        return ToolResult(is_error=True, text="deployment declined by user")

    async def respond_to_elicitation(self, payload: dict) -> None:
        self.payloads.append(payload)

    async def read_resource(self, uri: str) -> str:
        raise RuntimeError("stub adapter does not serve resources")

    async def get_prompt(self, name: str, arguments: dict) -> str:
        raise RuntimeError("stub adapter does not serve prompts")

    async def close(self) -> None:
        pass


def _deploy_call_message() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "deploy_service", "args": DEPLOY_ARGS, "id": "call-1"}],
    )


def _initial_state(prompt: str) -> dict:
    return {
        "messages": [HumanMessage(content=prompt)],
        "iterations": 0,
        "tool_calls": [],
        "mcp_latency_ms": 0.0,
        "user_interactions": 0,
        "elicitation_round_trips": 0,
    }


async def test_approval_pause_resume_auto_approve() -> None:
    model = BindableFakeChatModel(
        messages=iter([_deploy_call_message(), AIMessage(content="Deployed v1.8.3 to production.")])
    )
    adapter = ElicitingStubAdapter()
    graph = build_agent(
        TOOL_SPECS, adapter, model=model, user_simulator=ScriptedUserSimulator("auto-approve")
    )

    final = await graph.ainvoke(
        _initial_state("Deploy checkout to v1.8.3 in production."),
        config={"recursion_limit": RECURSION_LIMIT},
    )

    # The pause/resume went through the adapter seam, verbatim re-issue.
    assert adapter.payloads == [{"status": "approved"}]
    assert adapter.calls == [("deploy_service", DEPLOY_ARGS)] * 2
    assert final["user_interactions"] == 1
    assert final["elicitation_round_trips"] == 1
    # Message order: ToolMessage(s) first, then the user-side record.
    kinds = [type(m) for m in final["messages"]]
    tool_idx = kinds.index(ToolMessage)
    user_idx = max(i for i, m in enumerate(final["messages"]) if isinstance(m, HumanMessage))
    assert tool_idx < user_idx
    tool_message = final["messages"][tool_idx]
    assert tool_message.status == "success"
    # Structured content wins over text in the ToolMessage body.
    assert "v1.8.3" in tool_message.content


async def test_approval_pause_resume_auto_decline_surfaces_tool_error() -> None:
    model = BindableFakeChatModel(
        messages=iter(
            [_deploy_call_message(), AIMessage(content="The deployment declined by user request.")]
        )
    )
    adapter = ElicitingStubAdapter()
    graph = build_agent(
        TOOL_SPECS, adapter, model=model, user_simulator=ScriptedUserSimulator("auto-decline")
    )

    final = await graph.ainvoke(
        _initial_state("Deploy checkout to v1.8.3 in production."),
        config={"recursion_limit": RECURSION_LIMIT},
    )

    assert adapter.payloads == [{"status": "declined"}]
    assert final["user_interactions"] == 1
    assert final["elicitation_round_trips"] == 1
    tool_message = next(m for m in final["messages"] if isinstance(m, ToolMessage))
    assert tool_message.status == "error"
    assert "deployment declined by user" in tool_message.content


async def test_default_policy_none_declines_elicitation() -> None:
    """No simulator configured == "none" == M1/M2 behavior; an unexpected
    elicitation is declined, never auto-approved."""
    model = BindableFakeChatModel(
        messages=iter([_deploy_call_message(), AIMessage(content="Could not deploy.")])
    )
    adapter = ElicitingStubAdapter()
    graph = build_agent(TOOL_SPECS, adapter, model=model)

    final = await graph.ainvoke(
        _initial_state("Deploy checkout to v1.8.3 in production."),
        config={"recursion_limit": RECURSION_LIMIT},
    )

    assert adapter.payloads == [{"status": "declined"}]
    assert final["user_interactions"] == 1


async def test_clarify_hook_injects_user_message_before_first_tool_call() -> None:
    """Category F: the scripted user volunteers the missing environment and
    version; the model sees it in the conversation BEFORE acting."""
    model = BindableFakeChatModel(
        messages=iter([_deploy_call_message(), AIMessage(content="Deployed to staging.")])
    )
    adapter = ElicitingStubAdapter()
    graph = build_agent(
        TOOL_SPECS, adapter, model=model,
        user_simulator=ScriptedUserSimulator("clarify-with:staging v1.7.0"),
    )

    final = await graph.ainvoke(
        _initial_state("Deploy checkout."), config={"recursion_limit": RECURSION_LIMIT}
    )

    human_messages = [m for m in final["messages"] if isinstance(m, HumanMessage)]
    assert any(
        "Clarification from the user: staging v1.7.0" in str(m.content)
        for m in human_messages
    )
    # Two interactions: the clarify-hook injection AND the stub's approval
    # pause (a clarify-with user is cooperative and approves).
    assert final["user_interactions"] == 2
    assert final["elicitation_round_trips"] == 1
    assert adapter.payloads == [{"status": "approved"}]


async def test_no_policy_means_no_clarify_injection() -> None:
    model = BindableFakeChatModel(
        messages=iter([AIMessage(content="I need more information.")])
    )
    adapter = ElicitingStubAdapter()
    graph = build_agent(TOOL_SPECS, adapter, model=model, user_simulator=ScriptedUserSimulator())

    final = await graph.ainvoke(
        _initial_state("Deploy checkout."), config={"recursion_limit": RECURSION_LIMIT}
    )

    human_messages = [m for m in final["messages"] if isinstance(m, HumanMessage)]
    assert len(human_messages) == 1  # the task prompt only
    assert final["user_interactions"] == 0


async def test_run_task_records_m3_1_counters() -> None:
    model = BindableFakeChatModel(
        messages=iter([_deploy_call_message(), AIMessage(content="Deployed.")])
    )
    adapter = ElicitingStubAdapter()
    graph = build_agent(
        TOOL_SPECS, adapter, model=model, user_simulator=ScriptedUserSimulator("auto-approve")
    )

    result = await run_task(
        {"id": "g-01", "sdk": "stub", "prompt": "Deploy checkout to v1.8.3 in production."},
        adapter,
        graph,
    )

    assert result["error"] is None
    assert result["round_trips"] == 1  # one LLM-driven tool loop
    assert result["elicitation_round_trips"] == 1
    assert result["mcp_round_trips"] == 2  # the resumed call crosses the wire again
    assert result["user_interactions"] == 1
