"""Tasks extension lifecycle tests (SPEC.md §17, M3.2) — hermetic, NO LLM.

Drives the official and fastmcp adapters against their real stdio server
subprocesses (same hermetic pattern as tests/conformance/test_adapters.py).
The world registry is exercised through the adapter common view
(start_task/poll_task/cancel_task): the official adapter drives the REAL
protocol Tasks methods (tasks/get, tasks/cancel, tasks/list, tasks/result +
server-pushed notifications), the fastmcp adapter the app-level plain tools
— the layering each adapter docstring states.

The tick pacing is overridden via MCP_BENCH_TASK_TICK_S (0.05s) to keep the
suite fast; fault injection uses TASK_FAILURE_RATE=1.0 so the deterministic
FaultEngine.task_failure() draw always fires (deterministic per seed).

The ADK variant is covered in test_adk_tasks.py (adk env only).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    # Unguarded concrete class for annotations (the package-level import is
    # guarded and typed as a None-union).
    from mcp_sdk_bench.adapters.official import (
        OfficialAdapter as _ConcreteOfficialAdapter,
    )

from mcp_sdk_bench.adapters import FastMCPAdapter, MCPAdapter, OfficialAdapter
from mcp_sdk_bench.adapters.base import TaskView
from mcp_sdk_bench.faults import INJECTED_TASK_FAILURE

# Main-env module (mcp 2.x + fastmcp installed): the guarded imports in
# adapters/__init__ are never None here.
assert OfficialAdapter is not None and FastMCPAdapter is not None

ADAPTER_CLASSES = [OfficialAdapter, FastMCPAdapter]

TASK_TOOL = "generate_monthly_report"
TERMINAL = {"completed", "failed", "cancelled"}

#: Fast deterministic pacing for the hermetic suite (default is 2.0s/tick).
FAST_TICK = {"MCP_BENCH_TASK_TICK_S": "0.05"}


@asynccontextmanager
async def _connected(
    cls: type[MCPAdapter], env: dict[str, str] | None = None
) -> AsyncIterator[MCPAdapter]:
    # Both adapter classes take an env kwarg (merged over the SDK default
    # subprocess environment); the base ABC declares no __init__.
    adapter = cls(env=env)  # ty: ignore[unknown-argument]
    await adapter.connect()
    try:
        yield adapter
    finally:
        await adapter.close()


async def _poll_until_terminal(
    adapter: MCPAdapter, handle: str, *, timeout: float = 15.0
) -> list[TaskView]:
    """Poll until a terminal status; return the observed view sequence
    (deduplicated on (status, progress))."""
    views: list[TaskView] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        view = await adapter.poll_task(handle)
        if not views or (view.status, view.progress) != (views[-1].status, views[-1].progress):
            views.append(view)
        if view.status in TERMINAL:
            return views
        await asyncio.sleep(0.02)
    raise TimeoutError(f"task {handle} never reached a terminal status (last: {views[-1]})")


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_start_poll_to_completion(cls) -> None:
    async with _connected(cls, FAST_TICK) as adapter:
        started = await adapter.start_task(TASK_TOOL)
        assert started.handle
        assert started.status in ("queued", "running")
        assert started.progress == 0.0
        assert started.result is None

        views = await _poll_until_terminal(adapter, started.handle)
        final = views[-1]
        assert final.status == "completed"
        assert final.progress == 1.0
        assert final.result is not None
        assert {"report_id", "rows", "generated_at"} <= set(final.result)
        assert final.error is None
        # Progress increases monotonically across observed views.
        progresses = [v.progress for v in views]
        assert progresses == sorted(progresses)


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_cancel_mid_run_stops_ticks(cls) -> None:
    async with _connected(cls, FAST_TICK) as adapter:
        started = await adapter.start_task(TASK_TOOL)
        # Wait until the task is demonstrably mid-run.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10.0
        while True:
            view = await adapter.poll_task(started.handle)
            if view.status == "running" and view.progress > 0.0:
                break
            assert loop.time() < deadline, "task never started ticking"

        cancelled = await adapter.cancel_task(started.handle)
        assert cancelled.status == "cancelled"
        assert cancelled.result is None

        await asyncio.sleep(0.2)  # several ticks: a live ticker would advance
        after = await adapter.poll_task(started.handle)
        assert after.status == "cancelled"
        assert after.progress == cancelled.progress  # no further progress
        assert after.result is None


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_failure_injection_fails_at_first_tick(cls) -> None:
    env = {**FAST_TICK, "TASK_FAILURE_RATE": "1.0"}
    async with _connected(cls, env) as adapter:
        started = await adapter.start_task(TASK_TOOL)
        assert started.status in ("queued", "running")

        views = await _poll_until_terminal(adapter, started.handle)
        final = views[-1]
        assert final.status == "failed"
        assert final.error == INJECTED_TASK_FAILURE
        assert final.result is None


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_two_concurrent_tasks_and_limit(cls) -> None:
    async with _connected(cls, FAST_TICK) as adapter:
        first = await adapter.start_task(TASK_TOOL)
        second = await adapter.start_task(TASK_TOOL)
        assert first.handle != second.handle

        # The registry supports exactly 2 concurrent tasks (world policy).
        with pytest.raises(RuntimeError, match="limit"):
            await adapter.start_task(TASK_TOOL)

        views_first, views_second = await asyncio.gather(
            _poll_until_terminal(adapter, first.handle),
            _poll_until_terminal(adapter, second.handle),
        )
        assert views_first[-1].status == "completed"
        assert views_second[-1].status == "completed"
        assert views_first[-1].progress == 1.0
        assert views_second[-1].progress == 1.0
        # Independent progress: the two tasks carry distinct handles and each
        # produced its own progression.
        assert views_first[-1].result is not None
        assert views_second[-1].result is not None
        assert (
            views_first[-1].result["report_id"] != views_second[-1].result["report_id"]
        )


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_reconnect_resumes_running_task(cls, tmp_path: Path) -> None:
    """Start a task, close the session (server process exits), open a NEW
    session against a fresh server process sharing the same world store file
    (MCP_BENCH_WORLD_STATE_FILE — the registry persists in the world store,
    not the process): the task is still running and then completes."""
    state_file = tmp_path / "world.json"
    env = {"MCP_BENCH_TASK_TICK_S": "0.4", "MCP_BENCH_WORLD_STATE_FILE": str(state_file)}

    async with _connected(cls, env) as adapter:
        started = await adapter.start_task(TASK_TOOL)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10.0
        while True:
            view = await adapter.poll_task(started.handle)
            if view.status == "running" and view.progress > 0.0:
                break
            assert loop.time() < deadline, "task never started ticking"
    # First server process is gone; the world store file holds the task.
    assert state_file.exists()

    async with _connected(cls, env) as adapter2:
        resumed = await adapter2.poll_task(started.handle)
        # The resumed runner sleeps a full tick before advancing, so the
        # first poll of the new session deterministically sees it running.
        assert resumed.status == "running"
        assert resumed.progress < 1.0
        views = await _poll_until_terminal(adapter2, started.handle, timeout=20.0)
        assert views[-1].status == "completed"
        assert views[-1].progress == 1.0
        assert views[-1].result is not None


# ---- official-only wire-level assertions (real protocol Tasks) ----


@asynccontextmanager
async def _connected_official(
    env: dict[str, str] | None = None,
) -> AsyncIterator[_ConcreteOfficialAdapter]:
    assert OfficialAdapter is not None  # main env (guarded import in adapters/__init__)
    adapter = OfficialAdapter(env=env)
    await adapter.connect()
    try:
        yield adapter
    finally:
        await adapter.close()


async def test_official_wire_tasks_list_shows_both_tasks() -> None:
    """A real tasks/list wire request shows both concurrent tasks."""
    async with _connected_official(FAST_TICK) as adapter:
        first = await adapter.start_task(TASK_TOOL)
        second = await adapter.start_task(TASK_TOOL)
        listed = await adapter.list_tasks()
        handles = {t.handle for t in listed}
        assert {first.handle, second.handle} <= handles
        views = await asyncio.gather(
            _poll_until_terminal(adapter, first.handle),
            _poll_until_terminal(adapter, second.handle),
        )
        assert all(v[-1].status == "completed" for v in views)


async def test_official_client_receives_pushed_progress_notifications() -> None:
    """The client receives server-pushed notifications/progress (and the
    notifications/tasks/status binding) during a run — not just polled
    snapshots."""
    async with _connected_official(FAST_TICK) as adapter:
        started = await adapter.start_task(TASK_TOOL)
        views = await _poll_until_terminal(adapter, started.handle)
        assert views[-1].status == "completed"
        await asyncio.sleep(0.2)  # let the final notifications land
        pushed = adapter.pushed_progress(started.handle)
        assert len(pushed) >= 2, f"expected pushed progress, got {pushed}"
        assert pushed == sorted(pushed)
        assert pushed[-1] == 1.0
        assert adapter.pushed_status(started.handle) == "completed"


async def test_base_adapter_task_methods_are_honest_not_implemented() -> None:
    """The common view never stubs tasks (SPEC.md §7): the base
    implementation raises NotImplementedError."""
    from mcp_sdk_bench.adapters.base import MCPAdapter

    class Bare(MCPAdapter):
        async def connect(self):
            raise NotImplementedError

        async def call_tool(self, name, arguments):
            raise NotImplementedError

        async def read_resource(self, uri):
            raise NotImplementedError

        async def get_prompt(self, name, arguments):
            raise NotImplementedError

        async def close(self):
            pass

    with pytest.raises(NotImplementedError, match="no task surface"):
        await Bare().start_task(TASK_TOOL)
    with pytest.raises(NotImplementedError, match="no task surface"):
        await Bare().poll_task("report-000")
    with pytest.raises(NotImplementedError, match="no task surface"):
        await Bare().cancel_task("report-000")
