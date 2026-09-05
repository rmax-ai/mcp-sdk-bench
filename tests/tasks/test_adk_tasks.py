"""ADK Tasks lifecycle (SPEC.md §17 report kind M3.2, §9 H migration kind
M3.3) — ADK env only.

Skips in the main env (mcp 2.x) like the other ADK modules. Run it for real
with:

    PYTHONPATH=src uv run --project envs/adk pytest tests/tasks/test_adk_tasks.py

The ADK variant exercises the APP-LEVEL task surface (plain tools
generate_monthly_report / get_report_task / cancel_report_task over the
McpToolset channel, plus the M3.3 migration tools start_migration /
get_migration_status / cancel_migration); mcp 1.x has no protocol Tasks wire
surface and McpToolset no task client API — classified as app-level, never
protocol tasks (docs/capability-matrix.md).

Env-var configuration (tick pacing, fault rate, world store) goes through
os.environ because AdkAdapter forwards the parent environment to its server
subprocess and takes no env kwarg.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip(
    "google.adk.tools.mcp_tool.mcp_toolset",
    reason="adk env only (main env pins mcp 2.x; google-adk[mcp] needs mcp 1.x)",
)

from mcp_sdk_bench.adapters.adk import AdkAdapter
from mcp_sdk_bench.adapters.base import TaskView
from mcp_sdk_bench.faults import INJECTED_TASK_FAILURE

TASK_TOOL = "generate_monthly_report"
TERMINAL = {"completed", "failed", "cancelled"}


@pytest.fixture(autouse=True)
def _fast_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_BENCH_TASK_TICK_S", "0.05")


async def _poll_until_terminal(adapter: AdkAdapter, handle: str, *, timeout: float = 30.0) -> list[TaskView]:
    views: list[TaskView] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        view = await adapter.poll_task(handle)
        if not views or (view.status, view.progress) != (views[-1].status, views[-1].progress):
            views.append(view)
        if view.status in TERMINAL:
            return views
        await asyncio.sleep(0.05)
    raise TimeoutError(f"task {handle} never reached a terminal status (last: {views[-1]})")


async def test_adk_start_poll_to_completion() -> None:
    adapter = AdkAdapter()
    try:
        await adapter.connect()
        started = await adapter.start_task(TASK_TOOL)
        assert started.handle
        assert started.status in ("queued", "running")

        views = await _poll_until_terminal(adapter, started.handle)
        final = views[-1]
        assert final.status == "completed"
        assert final.progress == 1.0
        assert final.result is not None
        assert {"report_id", "rows", "generated_at"} <= set(final.result)
        progresses = [v.progress for v in views]
        assert progresses == sorted(progresses)
    finally:
        await adapter.close()


async def test_adk_cancel_mid_run() -> None:
    adapter = AdkAdapter()
    try:
        await adapter.connect()
        started = await adapter.start_task(TASK_TOOL)
        cancelled = await adapter.cancel_task(started.handle)
        assert cancelled.status == "cancelled"
        assert cancelled.result is None
        await asyncio.sleep(0.2)
        after = await adapter.poll_task(started.handle)
        assert after.status == "cancelled"
        assert after.result is None
    finally:
        await adapter.close()


async def test_adk_failure_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASK_FAILURE_RATE", "1.0")
    adapter = AdkAdapter()
    try:
        await adapter.connect()
        started = await adapter.start_task(TASK_TOOL)
        views = await _poll_until_terminal(adapter, started.handle)
        assert views[-1].status == "failed"
        assert views[-1].error == INJECTED_TASK_FAILURE
    finally:
        await adapter.close()


async def test_adk_two_concurrent_tasks() -> None:
    adapter = AdkAdapter()
    try:
        await adapter.connect()
        first = await adapter.start_task(TASK_TOOL)
        second = await adapter.start_task(TASK_TOOL)
        assert first.handle != second.handle
        with pytest.raises(RuntimeError, match="limit"):
            await adapter.start_task(TASK_TOOL)
        views_first, views_second = await asyncio.gather(
            _poll_until_terminal(adapter, first.handle),
            _poll_until_terminal(adapter, second.handle),
        )
        assert views_first[-1].status == "completed"
        assert views_second[-1].status == "completed"
    finally:
        await adapter.close()


async def test_adk_reconnect_resumes_running_task(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state_file = tmp_path / "world.json"
    monkeypatch.setenv("MCP_BENCH_TASK_TICK_S", "0.4")
    monkeypatch.setenv("MCP_BENCH_WORLD_STATE_FILE", str(state_file))

    adapter = AdkAdapter()
    try:
        await adapter.connect()
        started = await adapter.start_task(TASK_TOOL)
    finally:
        await adapter.close()
    assert state_file.exists()

    adapter2 = AdkAdapter()
    try:
        await adapter2.connect()
        resumed = await adapter2.poll_task(started.handle)
        assert resumed.status == "running"
        views = await _poll_until_terminal(adapter2, started.handle, timeout=30.0)
        assert views[-1].status == "completed"
        assert views[-1].result is not None
    finally:
        await adapter2.close()


async def test_adk_long_running_tool_native_not_wired() -> None:
    """Honesty anchor: ADK 2.8.0 ships LongRunningFunctionTool (verified
    import), but the benchmark variant deliberately does NOT wire it —
    tasks here are the app-level MCP tool equivalent (SPEC.md §23)."""
    from google.adk.tools import LongRunningFunctionTool  # noqa: F401


# ---- M3.3 migration kind (SPEC.md §9 H): same app-level surface ----

MIGRATION_RESULT_ROWS = 4800


async def _start_adk_migration(adapter: AdkAdapter, scope: str = "customer-data") -> TaskView:
    started = await adapter.start_task("migration", {"scope": scope})
    assert started.handle.startswith("migrate-"), started.handle
    assert started.status in ("queued", "running")
    assert started.progress == 0.0
    return started


async def test_adk_migration_start_poll_to_completion() -> None:
    adapter = AdkAdapter()
    try:
        await adapter.connect()
        started = await _start_adk_migration(adapter, "customer-data")

        views = await _poll_until_terminal(adapter, started.handle)
        final = views[-1]
        assert final.status == "completed"
        assert final.progress == 1.0
        assert final.error is None
        assert final.result is not None
        assert final.result["migration_id"].startswith("customer-data-migrate-")
        assert final.result["rows"] == MIGRATION_RESULT_ROWS
        assert "completed_at" in final.result
        progresses = [v.progress for v in views]
        assert progresses == sorted(progresses)
    finally:
        await adapter.close()


async def test_adk_migration_cancel_mid_run() -> None:
    adapter = AdkAdapter()
    try:
        await adapter.connect()
        started = await _start_adk_migration(adapter)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10.0
        while True:
            view = await adapter.poll_task(started.handle)
            if view.status == "running" and view.progress > 0.0:
                break
            assert loop.time() < deadline, "migration never started ticking"

        cancelled = await adapter.cancel_task(started.handle)
        assert cancelled.status == "cancelled"
        assert cancelled.result is None

        await asyncio.sleep(0.2)  # several ticks: a live ticker would advance
        after = await adapter.poll_task(started.handle)
        assert after.status == "cancelled"
        assert after.progress == cancelled.progress
        assert after.result is None
    finally:
        await adapter.close()


async def test_adk_migration_canary_scope_fails() -> None:
    """The deterministic h-03 lane (scope == "canary") fails at the first
    progress tick with the canonical injected-task-failure message."""
    adapter = AdkAdapter()
    try:
        await adapter.connect()
        started = await _start_adk_migration(adapter, "canary")

        views = await _poll_until_terminal(adapter, started.handle)
        assert views[-1].status == "failed"
        assert views[-1].error == INJECTED_TASK_FAILURE
        assert views[-1].result is None
    finally:
        await adapter.close()
