"""World reset — deterministic per-task state rebuild (SPEC.md §23).

`reset_world()` always returns a fresh copy of the seed; each task runs
against an untouched world unless the task spec says otherwise.

M3.2 (SPEC.md §17): when MCP_BENCH_WORLD_STATE_FILE is set, that JSON file
IS the world store — a server process loads it when present (a reconnecting
client's fresh server process then sees the previous process's report tasks,
and the registry resumes their tickers lazily) and task transitions persist
the whole world back to it. The file is the world's own model_dump_json —
no new store format. Unset: the pre-M3.2 behavior (fresh in-memory seed per
server process, no disk persistence).
"""
from __future__ import annotations

import os
from pathlib import Path

from mcp_sdk_bench.world.fixtures import seed_world
from mcp_sdk_bench.world.state import World

#: Env var naming the shared world store file (M3.2 task reconnect tests).
WORLD_STATE_FILE_ENV = "MCP_BENCH_WORLD_STATE_FILE"


def reset_world() -> World:
    raw = os.environ.get(WORLD_STATE_FILE_ENV)
    path = Path(raw) if raw else None
    if path is not None and path.exists():
        world = World.model_validate_json(path.read_text())
    else:
        world = seed_world()
    world.set_state_file(path)
    return world
