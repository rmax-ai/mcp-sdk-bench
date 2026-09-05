"""Migration task lifecycle tests (SPEC.md §9 H, M3.3) — hermetic, NO LLM.

Drives the official and fastmcp adapters against their real stdio server
subprocesses (same hermetic pattern as tests/tasks/test_task_lifecycle.py and
tests/conformance/test_adapters.py). The MIGRATION kind is exercised through
the adapter common view (start_task("migration", {"scope": ...}) /
poll_task / cancel_task):

- the official adapter exercises the REAL MCP Tasks protocol surface for
  migration handles (tasks/get | tasks/cancel | tasks/result +
  server-pushed progress — regression-proven below with raw wire requests);
- the fastmcp adapter the app-level plain tools (start_migration /
  get_migration_status / cancel_migration), picked from the handle's kind
  prefix.

The deterministic h-03 failure lane (scope == "canary") and the shared
MAX_ACTIVE_TASKS=2 limit across BOTH task kinds are covered here.

Tick pacing is overridden via MCP_BENCH_TASK_TICK_S (0.05s) to keep the
suite fast; fault injection uses no env override — the canary lane is the
deterministic failure story, not the seeded FaultEngine draw.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest
from helpers import (  # ty: ignore[unresolved-import] — tests/conformance/helpers.py, on sys.path via tests/tasks/conftest.py
    OFFICIAL_SESSION,
)
from mcp import types

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

TERMINAL = {"completed", "failed", "cancelled"}

#: Fast deterministic pacing for the hermetic suite (default is 2.0s/tick).
FAST_TICK = {"MCP_BENCH_TASK_TICK_S": "0.05"}

MIGRATION_RESULT_ROWS = 4800


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


async def _wait_until_running(
    adapter: MCPAdapter, handle: str, *, timeout: float = 10.0
) -> TaskView:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        view = await adapter.poll_task(handle)
        if view.status == "running" and view.progress > 0.0:
            return view
        assert loop.time() < deadline, "migration never started ticking"


async def _start_migration(adapter: MCPAdapter, scope: str = "customer-data") -> TaskView:
    started = await adapter.start_task("migration", {"scope": scope})
    assert started.handle.startswith("migrate-"), started.handle
    assert started.status in ("queued", "running")
    assert started.progress == 0.0
    assert started.result is None
    return started


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_migration_start_poll_to_completion(cls) -> None:
    async with _connected(cls, FAST_TICK) as adapter:
        started = await _start_migration(adapter, "customer-data")

        views = await _poll_until_terminal(adapter, started.handle)
        final = views[-1]
        assert final.status == "completed"
        assert final.progress == 1.0
        assert final.error is None
        assert final.result is not None
        # Migration result envelope: {migration_id, rows, completed_at}.
        assert final.result["migration_id"].startswith("customer-data-migrate-")
        assert final.result["rows"] == MIGRATION_RESULT_ROWS
        assert "completed_at" in final.result
        # Progress increases monotonically across observed views.
        progresses = [v.progress for v in views]
        assert progresses == sorted(progresses)


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_migration_cancel_mid_run_stops_ticks(cls) -> None:
    async with _connected(cls, FAST_TICK) as adapter:
        started = await _start_migration(adapter)
        await _wait_until_running(adapter, started.handle)

        cancelled = await adapter.cancel_task(started.handle)
        assert cancelled.status == "cancelled"
        assert cancelled.result is None

        await asyncio.sleep(0.2)  # several ticks: a live ticker would advance
        after = await adapter.poll_task(started.handle)
        assert after.status == "cancelled"
        assert after.progress == cancelled.progress  # no further progress
        assert after.result is None


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_migration_canary_scope_fails_at_first_tick(cls) -> None:
    """The deterministic h-03 lane: scope == "canary" always fails at its
    first progress tick with the canonical injected-task-failure message."""
    async with _connected(cls, FAST_TICK) as adapter:
        started = await _start_migration(adapter, "canary")

        views = await _poll_until_terminal(adapter, started.handle)
        final = views[-1]
        assert final.status == "failed"
        assert final.error == INJECTED_TASK_FAILURE
        assert final.result is None


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_shared_concurrency_limit_across_task_kinds(cls) -> None:
    """MAX_ACTIVE_TASKS=2 is shared across BOTH kinds (M3.3): a running
    report + a running migration exhaust the limit, so a second migration
    start is rejected."""
    async with _connected(cls, FAST_TICK) as adapter:
        report = await adapter.start_task("generate_monthly_report")
        migration = await _start_migration(adapter, "customer-data")
        assert report.handle.startswith("report-")

        with pytest.raises(RuntimeError, match="limit"):
            await adapter.start_task("migration", {"scope": "customer-data"})

        views_report, views_migration = await asyncio.gather(
            _poll_until_terminal(adapter, report.handle),
            _poll_until_terminal(adapter, migration.handle),
        )
        assert views_report[-1].status == "completed"
        assert views_migration[-1].status == "completed"
        # Independent results: report_id vs migration_id envelopes.
        report_result = views_report[-1].result
        migration_result = views_migration[-1].result
        assert report_result is not None and migration_result is not None
        assert report_result["rows"] != migration_result["rows"]


@pytest.mark.parametrize("cls", ADAPTER_CLASSES, ids=lambda c: c.__name__)
async def test_start_task_accepts_kind_token_and_tool_name(cls) -> None:
    """start_task("migration", ...) and start_task("start_migration", ...)
    resolve to the same wire start tool (adapters/base.resolve_start_tool)."""
    async with _connected(cls, FAST_TICK) as adapter:
        token = await adapter.start_task("migration", {"scope": "customer-data"})
        named = await adapter.start_task("start_migration", {"scope": "customer-data"})
        assert token.handle.startswith("migrate-")
        assert named.handle.startswith("migrate-")
        assert token.handle != named.handle
        # Tidy up: cancel both so no runner outlives the session.
        await adapter.cancel_task(token.handle)
        await adapter.cancel_task(named.handle)


# ---- official-only wire-level assertions (real protocol Tasks on migration handles) ----


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


async def test_official_tasks_get_and_cancel_resolve_a_migration_handle() -> None:
    """Regression (M3.3): the official protocol tasks/get + tasks/cancel
    handlers resolve a MIGRATION handle (world.find_task / world.cancel_task
    are kind-agnostic). Raw wire requests prove the protocol path — not the
    app-level mirror tools — serves migration handles."""
    async with OFFICIAL_SESSION() as session:
        started = await session.call_tool("start_migration", {"scope": "customer-data"})
        assert not started.is_error
        assert started.structured_content is not None
        handle = started.structured_content["task"]["handle"]
        assert handle.startswith("migrate-"), handle

        # tasks/get returns the working (queued/running) migration.
        fetched = await session.send_request(
            types.GetTaskRequest(
                params=types.GetTaskRequestParams(task_id=handle)
            ),
            types.GetTaskResult,
        )
        assert fetched.status == "working"
        assert fetched.task_id == handle

        # tasks/cancel cancels it (cancelling a queued/running task is valid).
        cancelled = await session.send_request(
            types.CancelTaskRequest(
                params=types.CancelTaskRequestParams(task_id=handle)
            ),
            types.CancelTaskResult,
        )
        assert cancelled.status == "cancelled"

        # tasks/get after cancel shows the terminal state.
        after = await session.send_request(
            types.GetTaskRequest(params=types.GetTaskRequestParams(task_id=handle)),
            types.GetTaskResult,
        )
        assert after.status == "cancelled"


async def test_official_protocol_tasks_list_shows_migration_and_report() -> None:
    """A real tasks/list wire request shows both task kinds (report-... and
    migrate-... handles) — the registry is the single source of truth."""
    async with _connected_official(FAST_TICK) as adapter:
        report = await adapter.start_task("generate_monthly_report")
        migration = await adapter.start_task("migration", {"scope": "customer-data"})
        listed = await adapter.list_tasks()
        handles = {t.handle for t in listed}
        assert {report.handle, migration.handle} <= handles
        views_report, views_migration = await asyncio.gather(
            _poll_until_terminal(adapter, report.handle),
            _poll_until_terminal(adapter, migration.handle),
        )
        assert views_report[-1].status == "completed"
        assert views_migration[-1].status == "completed"


async def test_official_client_receives_pushed_progress_for_migration() -> None:
    """Server-pushed notifications flow for MIGRATION handles too: the
    official adapter started the migration with a _meta progressToken, so the
    world's notification seam pushes progress to this session. Exercises the
    same wire-level assertion surface test_task_lifecycle.py uses for the
    report kind."""
    async with _connected_official(FAST_TICK) as adapter:
        started = await _start_migration(adapter)
        views = await _poll_until_terminal(adapter, started.handle)
        assert views[-1].status == "completed"
        await asyncio.sleep(0.2)  # let the final notifications land
        pushed = adapter.pushed_progress(started.handle)
        assert len(pushed) >= 2, f"expected pushed progress, got {pushed}"
        assert pushed == sorted(pushed)
        assert pushed[-1] == 1.0
        assert adapter.pushed_status(started.handle) == "completed"
