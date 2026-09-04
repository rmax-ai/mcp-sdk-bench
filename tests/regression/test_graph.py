"""Deterministic agent-loop regression tests (no network, no real model).

Uses langchain_core's GenericFakeChatModel with a bind_tools shim so the loop
is fully reproducible, and a stub MCPAdapter to prove the tools node routes
through the adapter boundary (SPEC.md §2), not langchain internals.
"""
from __future__ import annotations

from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from mcp_sdk_bench.adapters.base import Discovery, MCPAdapter, ToolResult, ToolSpec
from mcp_sdk_bench.agent.graph import MAX_TOOL_ITERATIONS, build_agent
from mcp_sdk_bench.benchmark.runner import RECURSION_LIMIT, run_task

TOOL_SPECS = [
    ToolSpec(
        name="get_ticket",
        description="Fetch a single ticket by id.",
        input_schema={
            "type": "object",
            "properties": {"ticket_id": {"type": "string"}},
            "required": ["ticket_id"],
        },
    )
]


class BindableFakeChatModel(GenericFakeChatModel):
    """GenericFakeChatModel does not implement bind_tools; for the regression
    loop the tool schema is irrelevant, so return self unchanged."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        return self


class StubAdapter(MCPAdapter):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def connect(self) -> Discovery:
        return Discovery(tools=TOOL_SPECS, resources=[], prompts=[])

    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        self.calls.append((name, arguments))
        return ToolResult(
            structured_content={"ticket": {"id": arguments["ticket_id"], "status": "OPEN"}},
            text='{"ticket": {"status": "OPEN"}}',
        )

    async def read_resource(self, uri: str) -> str:
        raise RuntimeError("stub adapter does not serve resources")

    async def get_prompt(self, name: str, arguments: dict) -> str:
        raise RuntimeError("stub adapter does not serve prompts")

    async def close(self) -> None:
        pass


def _tool_call_message() -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "get_ticket", "args": {"ticket_id": "PAY-123"}, "id": "call-1"}
        ],
    )


def _initial_state(prompt: str) -> dict:
    return {
        "messages": [HumanMessage(content=prompt)],
        "iterations": 0,
        "tool_calls": [],
        "mcp_latency_ms": 0.0,
    }


async def test_tool_call_round_trips_through_adapter_then_final_answer() -> None:
    model = BindableFakeChatModel(
        messages=iter([_tool_call_message(), AIMessage(content="PAY-123 is OPEN.")])
    )
    adapter = StubAdapter()
    graph = build_agent(TOOL_SPECS, adapter, model=model)

    final = await graph.ainvoke(
        _initial_state("What is the status of PAY-123?"),
        config={"recursion_limit": RECURSION_LIMIT},
    )

    assert adapter.calls == [("get_ticket", {"ticket_id": "PAY-123"})]
    assert final["tool_calls"] == [
        {"name": "get_ticket", "arguments": {"ticket_id": "PAY-123"}}
    ]
    assert final["iterations"] == 1
    assert final["mcp_latency_ms"] >= 0.0
    last = final["messages"][-1]
    assert isinstance(last, AIMessage)
    assert last.content == "PAY-123 is OPEN."


def _tool_calls_forever() -> Any:
    while True:
        yield _tool_call_message()


async def test_max_tool_iterations_caps_an_infinite_tool_call_loop() -> None:
    # A model that never stops asking for tools must be cut off by
    # MAX_TOOL_ITERATIONS.
    model = BindableFakeChatModel(messages=_tool_calls_forever())
    adapter = StubAdapter()
    graph = build_agent(TOOL_SPECS, adapter, model=model)

    final = await graph.ainvoke(
        _initial_state("loop forever"),
        config={"recursion_limit": RECURSION_LIMIT},
    )

    assert final["iterations"] == MAX_TOOL_ITERATIONS
    assert len(final["tool_calls"]) == MAX_TOOL_ITERATIONS
    assert len(adapter.calls) == MAX_TOOL_ITERATIONS


async def test_host_mediated_read_resource_dispatch() -> None:
    """read_resource pseudo-tool must route to adapter.read_resource (host role)."""

    class ResourceStubAdapter(StubAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.resource_reads: list[str] = []

        async def read_resource(self, uri: str) -> str:
            self.resource_reads.append(uri)
            return "# Deployment Policy\ncheckout is under change freeze."

    model = BindableFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "read_resource", "args": {"uri": "company://policies/deployment"}, "id": "call-r1"}
                    ],
                ),
                AIMessage(content="checkout is under change freeze."),
            ]
        )
    )
    adapter = ResourceStubAdapter()
    graph = build_agent(TOOL_SPECS, adapter, model=model)

    final = await graph.ainvoke(
        _initial_state("Can checkout be deployed?"),
        config={"recursion_limit": RECURSION_LIMIT},
    )

    assert adapter.resource_reads == ["company://policies/deployment"]
    assert adapter.calls == []  # no MCP tool call was made
    assert final["tool_calls"] == [
        {"name": "read_resource", "arguments": {"uri": "company://policies/deployment"}}
    ]
    assert final["messages"][-1].content == "checkout is under change freeze."


async def test_host_mediated_read_resource_error_is_tool_error() -> None:
    """A failing resource read must surface as a tool error, not a crash."""

    class FailingResourceAdapter(StubAdapter):
        async def read_resource(self, uri: str) -> str:
            raise RuntimeError("unknown resource URI")

    model = BindableFakeChatModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "read_resource", "args": {"uri": "company://nope"}, "id": "call-r2"}
                    ],
                ),
                AIMessage(content="I could not read that resource."),
            ]
        )
    )
    adapter = FailingResourceAdapter()
    graph = build_agent(TOOL_SPECS, adapter, model=model)

    final = await graph.ainvoke(
        _initial_state("read the policy"),
        config={"recursion_limit": RECURSION_LIMIT},
    )

    tool_message = final["messages"][-2]
    assert "unknown resource URI" in tool_message.content


async def test_run_task_records_result_shape() -> None:
    model = BindableFakeChatModel(
        messages=iter([_tool_call_message(), AIMessage(content="PAY-123 is OPEN.")])
    )
    adapter = StubAdapter()
    graph = build_agent(TOOL_SPECS, adapter, model=model)

    result = await run_task(
        {"id": "basic-001", "sdk": "stub", "prompt": "What is the status of PAY-123?"},
        adapter,
        graph,
    )

    assert result["task_id"] == "basic-001"
    assert result["sdk"] == "stub"
    assert result["tool_calls"] == [
        {"name": "get_ticket", "arguments": {"ticket_id": "PAY-123"}}
    ]
    assert result["round_trips"] == 1
    assert result["final_answer"] == "PAY-123 is OPEN."
    assert result["error"] is None
    assert result["total_latency_ms"] >= result["mcp_latency_ms"] >= 0.0
