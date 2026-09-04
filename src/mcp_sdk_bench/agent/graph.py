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
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from mcp_sdk_bench.adapters.base import MCPAdapter, ToolResult, ToolSpec
from mcp_sdk_bench.agent.prompts import SYSTEM_PROMPT
from mcp_sdk_bench.agent.simulator import (
    ScriptedUserSimulator,
    UserSimulator,
    normalize_simulator_answer,
)

MAX_TOOL_ITERATIONS = 12


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    iterations: int
    tool_calls: Annotated[list[dict], operator.add]
    mcp_latency_ms: Annotated[float, operator.add]
    #: M3.1 (SPEC.md §18): simulator answers delivered (clarify-hook
    #: injections + elicitation responses). Read via state.get(..., 0) so
    #: pre-M3.1 initial states without the key keep working.
    user_interactions: int
    #: M3.1: elicitation pause/resume legs (each one is an extra MCP
    #: round trip beyond the LLM-driven round trips).
    elicitation_round_trips: int


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
    user_simulator: UserSimulator | None = None,
) -> Any:
    """Build the compiled LangGraph agent. The model is constructed once
    here; tool execution closes over `adapter` so every call crosses the
    adapter boundary.

    M3.1 (SPEC.md §18): `user_simulator` is the deterministic harness-side
    user (default: ScriptedUserSimulator("none") — no interaction, so all
    M1/M2 tasks behave exactly as before). It serves (a) the category-F
    clarify hook before the first tool call and (b) the pause/resume path
    when a tool call returns an elicitation_request.
    """
    chat_model = model if model is not None else build_model()
    simulator: UserSimulator = (
        user_simulator if user_simulator is not None else ScriptedUserSimulator()
    )
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
        messages = list(state["messages"])
        out: dict = {}
        injected: list[HumanMessage] = []
        # Category-F hook (SPEC.md §18): before the agent's first plan, the
        # scripted user may volunteer the clarification the prompt left out
        # (e.g. "staging v1.7.0" for "Deploy checkout."). Fires exactly once
        # per task, always strictly before the first tool call.
        if state["iterations"] == 0 and not any(
            isinstance(m, (AIMessage, ToolMessage)) for m in messages
        ):
            task_prompt = next(
                (m.content for m in messages if isinstance(m, HumanMessage)), ""
            )
            clarification = await simulator.clarify(str(task_prompt))
            if clarification:
                out["user_interactions"] = int(state.get("user_interactions", 0)) + 1
                injected = [
                    HumanMessage(content=f"Clarification from the user: {clarification}")
                ]
                messages = [*messages, *injected]
        response = await model_with_tools.ainvoke(
            [SystemMessage(content=SYSTEM_PROMPT), *messages]
        )
        # The injected user message must join the graph state (add_messages
        # only appends RETURNED messages), ahead of the model's response.
        out["messages"] = [*injected, response]
        return out

    async def tools_node(state: AgentState) -> dict:
        last = state["messages"][-1]
        assert isinstance(last, AIMessage)
        out_messages: list = []
        user_messages: list[HumanMessage] = []
        out_calls: list[dict] = []
        latency_ms = 0.0
        user_interactions = int(state.get("user_interactions", 0))
        elicitation_round_trips = int(state.get("elicitation_round_trips", 0))
        for call in last.tool_calls:
            name = call["name"]
            start = time.perf_counter()
            # Host-mediated MCP primitives (the agent loop plays the MCP host
            # role — SPEC.md §2): resources and prompts are not function tools
            # on the wire, so the host surfaces them as read-only pseudo-tools.
            if name == "read_resource":
                uri = (call["args"] or {}).get("uri", "")
                try:
                    text = await adapter.read_resource(uri)
                    result = ToolResult(is_error=False, text=text)
                except Exception as err:  # noqa: BLE001 — server errors are tool results
                    result = ToolResult(is_error=True, text=str(err))
            elif name == "get_prompt":
                args = dict(call["args"] or {})
                prompt_name = args.pop("name", "")
                try:
                    text = await adapter.get_prompt(prompt_name, args)
                    result = ToolResult(is_error=False, text=text)
                except Exception as err:  # noqa: BLE001
                    result = ToolResult(is_error=True, text=str(err))
            else:
                result = await adapter.call_tool(name, call["args"])
                if result.elicitation_request is not None:
                    # Multi-round-trip path (SPEC.md §18): the server paused
                    # the call for user input. Consult the scripted user,
                    # deliver the answer, and resume the SAME logical call —
                    # the model is not re-asked to re-issue it.
                    request = result.elicitation_request
                    answer = await simulator.answer(
                        str(request.get("kind", "")),
                        str(request.get("question", "")),
                        request.get("schema") or {},
                    )
                    user_interactions += 1
                    await adapter.respond_to_elicitation(
                        normalize_simulator_answer(request, answer)
                    )
                    # The pause/resume is an extra MCP round trip: the
                    # resumed call is the same call completing (official) or
                    # a fresh wire leg (FastMCP 2026-07-28, SEP-2322).
                    elicitation_round_trips += 1
                    result = await adapter.call_tool(name, call["args"])
                    user_messages.append(
                        HumanMessage(
                            content=(
                                f"User response to the server's "
                                f"{request.get('kind')} request "
                                f"({request.get('question')}): {answer}"
                            )
                        )
                    )
            latency_ms += (time.perf_counter() - start) * 1000
            out_calls.append({"name": name, "arguments": call["args"]})
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
            # ToolMessages must immediately follow the assistant tool-call
            # message (model API contract); the user-side record of each
            # elicitation answer comes after them.
            "messages": [*out_messages, *user_messages],
            "iterations": state["iterations"] + 1,
            "tool_calls": out_calls,
            "mcp_latency_ms": latency_ms,
            "user_interactions": user_interactions,
            "elicitation_round_trips": elicitation_round_trips,
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
