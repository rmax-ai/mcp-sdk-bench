"""World reset — deterministic per-task state rebuild (SPEC.md §23).

`reset_world()` always returns a fresh copy of the seed; each task runs
against an untouched world unless the task spec says otherwise.
"""
from __future__ import annotations

from mcp_sdk_bench.world.fixtures import seed_world
from mcp_sdk_bench.world.state import World


def reset_world() -> World:
    return seed_world()
