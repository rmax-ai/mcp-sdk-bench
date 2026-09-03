"""LangGraph tool-calling loop (SPEC.md §2, layer L1).

One explicit graph: llm node → conditional edge → tools node → llm node.
Tool execution routes through OUR MCPAdapter (L3), never through a
framework-native MCP abstraction. The model, temperature, and prompt are
controlled variables (§23): same values across all SDK variants.

Probed against langgraph 1.2.11 + langchain-core 1.6.1: StateGraph /
add_messages / ToolNode all present; the loop below is hand-rolled (not
create_react_agent) so tool calls go through the adapter boundary.
"""
from __future__ import annotations

import json
import operator
import os
import time
from typing import Annotated, Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from mcp_sdk_bench.adapters.base import MCPAdapter, ToolSpec
from mcp_sdk_bench.agent.prompts import SYSTEM_PROMPT

MAX_TOOL_ITERATIONS = 12


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    iterations: int
    tool_calls: Annotated[list[dict], operator.add]
    mcp_latency_ms: Annotated[float, operator.add]


def build_model() -> BaseChatModel:
    """Construct the benchmark model from env config (SPEC.md §3).

    MODEL_PROVIDER: "deepseek" | "openai_compat" (default "deepseek").
    MODEL_NAME, MODEL_API_KEY: required. MODEL_BASE_URL: required for
    openai_compat, ignored otherwise. BENCH_TEMPERATURE: default "0".
    """
    from langchain_openai import ChatOpenAI

    provider = os.environ.get("MODEL_PROVIDER", "deepseek")
    model_name = os.environ.get("MODEL_NAME")
    api_key = os.environ.get("MODEL_API_KEY")
    base_url = os.environ.get("MODEL_BASE_URL")
    temperature = float(os.environ.get("BENCH_TEMPERATURE", "0"))

    if not api_key:
        raise RuntimeError(
            "MODEL_API_KEY is not set; the benchmark agent needs a model "
            "credential (see SPEC.md §3). Never hardcode keys in the repo."
        )
    if not model_name:
        raise RuntimeError("MODEL_NAME is not set; pin the model for reproducibility.")

    if provider == "deepseek":
        resolved_base_url = "https://api.deepseek.com/v1"
    elif provider == "openai_compat":
        if not base_url:
            raise RuntimeError("MODEL_PROVIDER=openai_compat requires MODEL_BASE_URL.")
        resolved_base_url = base_url
    else:
        raise RuntimeError(f"unsupported MODEL_PROVIDER {provider!r}")

    return ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=resolved_base_url,
        temperature=temperature,
    )


def _tool_message_content(result_text: str | None, structured: dict | None) -> str:
    if structured is not None:
        return json.dumps(structured)
    return result_text or ""


def build_agent(
    tools: list[ToolSpec],
    adapter: MCPAdapter,
    *,
    model: BaseChatModel | None = None,
) -> Any:
    """Build the compiled LangGraph agent. The model is constructed once
    here; tool execution closes over `adapter` so every call crosses the
    adapter boundary."""
    chat_model = model if model is not None else build_model()
    model_with_tools = chat_model.bind_tools(
        [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            }
            for tool in tools
        ]
    )

    async def llm_node(state: AgentState) -> dict:
        response = await model_with_tools.ainvoke(
            [SystemMessage(content=SYSTEM_PROMPT), *state["messages"]]
        )
        return {"messages": [response]}

    async def tools_node(state: AgentState) -> dict:
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)
        out_messages: list[ToolMessage] = []
        out_calls: list[dict] = []
        latency_ms = 0.0
        for call in last.tool_calls:
            start = time.perf_counter()
            result = await adapter.call_tool(call["name"], call["args"])
            latency_ms += (time.perf_counter() - start) * 1000
            out_calls.append({"name": call["name"], "arguments": call["args"]})
            content = (
                f"Tool error: {result.text}"
                if result.is_error
                else _tool_message_content(result.text, result.structured_content)
            )
            out_messages.append(
                ToolMessage(
                    content=content,
                    tool_call_id=call["id"],
                    status="error" if result.is_error else "success",
                )
            )
        return {
            "messages": out_messages,
            "iterations": state["iterations"] + 1,
            "tool_calls": out_calls,
            "mcp_latency_ms": latency_ms,
        }

    def route_after_llm(state: AgentState) -> str:
        last = state["messages"][-1]
        if (
            isinstance(last, AIMessage)
            and last.tool_calls
            and state["iterations"] < MAX_TOOL_ITERATIONS
        ):
            return "tools"
        return END

    # ty limitation: TypedDict class objects don't satisfy langgraph's
    # StateLike protocol bound even for a plain TypedDict; runtime is correct.
    graph = StateGraph(AgentState)  # ty: ignore[invalid-argument-type]
    graph.add_node("llm", llm_node)
    graph.add_node("tools", tools_node)
    graph.add_edge(START, "llm")
    graph.add_conditional_edges("llm", route_after_llm, {"tools": "tools", END: END})
    graph.add_edge("tools", "llm")
    return graph.compile()
