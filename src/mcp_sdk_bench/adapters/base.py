"""Common protocol view (SPEC.md §2, layer L4) + adapter boundary (L3).

The adapter normalizes HOW a candidate is driven — connection bootstrap, call
dispatch, error mapping, lifecycle teardown — never WHAT it can express. If a
candidate cannot express resources or prompts, the Discovery projection must
show empty lists (absence as absence), never silent emulation.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict


class ResourceSpec(BaseModel):
    uri: str
    name: str
    mime_type: str
    description: str


class PromptSpec(BaseModel):
    name: str
    description: str
    arguments: list[dict]


class ToolResult(BaseModel):
    is_error: bool = False
    structured_content: dict | None = None
    text: str | None = None


class Discovery(BaseModel):
    tools: list[ToolSpec]
    resources: list[ResourceSpec]
    prompts: list[PromptSpec]


class MCPAdapter(ABC):
    """Async-only adapter boundary. One instance == one server session."""

    @abstractmethod
    async def connect(self) -> Discovery:
        """Open the connection, run protocol handshake, return discovery."""

    @abstractmethod
    async def call_tool(self, name: str, arguments: dict) -> ToolResult:
        """Invoke one tool. isError results map to ToolResult(is_error=True,
        text=message); structuredContent maps to structured_content."""

    @abstractmethod
    async def read_resource(self, uri: str) -> str:
        """Return resource content text. Raise RuntimeError with the server's
        message when the server reports an error."""

    @abstractmethod
    async def get_prompt(self, name: str, arguments: dict) -> str:
        """Render a prompt to its concatenated text content."""

    @abstractmethod
    async def close(self) -> None:
        """Tear down the connection. Safe to call once; never twice-required."""
