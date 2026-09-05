"""mcpbench CLI — SPEC.md §26.

Subcommands are implemented per milestone. Unimplemented commands exit code 3
with an explicit milestone pointer so benchmark scripts can detect capability gaps.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Annotated

import typer

from mcp_sdk_bench import __version__
from mcp_sdk_bench.benchmark import result as result_mod
from mcp_sdk_bench.benchmark.result import new_run_id
from mcp_sdk_bench.benchmark.sweep import REPO_ROOT, environment_snapshot, sweep_sync


def _available_adapters() -> dict[str, type]:
    from mcp_sdk_bench.adapters import AdkAdapter, FastMCPAdapter, OfficialAdapter
    from mcp_sdk_bench.adapters.adk import adk_env_ok

    return {
        name: cls
        for name, cls in (("official", OfficialAdapter), ("fastmcp", FastMCPAdapter), ("adk", AdkAdapter))
        if cls is not None and (name != "adk" or adk_env_ok())
    }

app = typer.Typer(
    name="mcpbench",
    help="MCP SDK benchmark: FastMCP 4.x vs Google ADK vs official MCP Python SDK v2.",
    no_args_is_help=True,
)


def _not_implemented(milestone: str) -> None:
    typer.echo(f"mcpbench: this command is not yet implemented (milestone {milestone}).")
    raise typer.Exit(code=3)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"mcpbench {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", "-V", callback=_version_callback, help="Show version and exit."),
) -> None:
    """mcp-sdk-bench command line interface."""


def _run_adk_via_env(run_id: str) -> None:
    """The ADK variant must run inside envs/adk (DECISIONS.md D1). Re-exec the
    same CLI there with PYTHONPATH pointing at this repo's src/."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [
        "uv",
        "run",
        "--project",
        str(REPO_ROOT / "envs" / "adk"),
        "python",
        "-m",
        "mcp_sdk_bench.cli",
        "eval",
        "--sdk",
        "adk",
        "--run-id",
        run_id,
    ]
    typer.echo(f"mcpbench: running adk variant in envs/adk: {' '.join(cmd[:5])} ...")
    proc = subprocess.run(cmd, cwd=REPO_ROOT, env=env, check=False)
    if proc.returncode != 0:
        typer.echo(f"mcpbench: adk variant run failed (exit {proc.returncode})")
        raise typer.Exit(code=proc.returncode)


@app.command()
def capabilities(run_id: str = typer.Option(None, "--run-id", help="Run id (default: new)")) -> None:
    """Discovery snapshot per SDK into results/<run_id>/capabilities.json."""
    import asyncio

    from mcp_sdk_bench.adapters import AdkAdapter, FastMCPAdapter, OfficialAdapter

    rid = run_id or new_run_id()
    snapshots: dict = {}
    for name, cls in (("official", OfficialAdapter), ("fastmcp", FastMCPAdapter), ("adk", AdkAdapter)):
        if cls is None:
            snapshots[name] = {"available": False, "reason": "not importable in this environment (DECISIONS.md D1)"}
            continue

        async def _snapshot(cls=cls, name=name) -> None:
            adapter = cls()
            try:
                discovery = await adapter.connect()
                snapshots[name] = {
                    "available": True,
                    "tools": [t.name for t in discovery.tools],
                    "resources": [r.uri for r in discovery.resources],
                    "prompts": [p.name for p in discovery.prompts],
                }
            finally:
                await adapter.close()

        asyncio.run(_snapshot())

    result_mod.write_capabilities(rid, snapshots, environment_snapshot())
    typer.echo(f"mcpbench: capabilities snapshot written to results/{rid}/capabilities.json")


@app.command()
def conformance() -> None:
    """Run deterministic protocol conformance tests (SPEC.md §8)."""
    _not_implemented("M2")


def _run_interop() -> list[dict]:
    from mcp_sdk_bench.benchmark.interop import run_interop

    results = run_interop()
    typer.echo(
        f"{'pairing':<20} {'conn':>5} {'client-ver':>11} {'server-ver':>11} "
        f"{'disc':>5} {'rtrip':>6} {'classification':>20}"
    )
    for r in results:
        typer.echo(
            f"{r['pairing']:<20} {r['connected']!s:>5} "
            f"{r['protocol_version_client'] or '-':>11} "
            f"{r['protocol_version_server'] or '-':>11} "
            f"{r['discovery_ok']!s:>5} {r['roundtrip_ok']!s:>6} "
            f"{r['classification']:>20}"
        )
        if r["error"]:
            typer.echo(f"  error: {r['error']}")
    typer.echo("mcpbench: interoperability matrix written to results/latest/interoperability.json")
    return results


@app.command()
def interop() -> None:
    """Run the cross-implementation client/server pairing matrix (SPEC.md §8)."""
    results = _run_interop()
    if any(r["classification"] != "pass" for r in results):
        raise typer.Exit(code=1)


@app.command()
def interoperability() -> None:
    """Alias for `interop` (SPEC.md §8 INTEROPERABILITY)."""
    results = _run_interop()
    if any(r["classification"] != "pass" for r in results):
        raise typer.Exit(code=1)


def _resolve_datasets(names: list[str] | None) -> list[Path] | None:
    """Resolve --dataset values to JSONL paths (M3.1). A bare name resolves
    under datasets/ (suffix optional); a path is used as given. None keeps
    the sweep default (basic + composition), unchanged from M1."""
    if not names:
        return None
    paths: list[Path] = []
    for name in names:
        candidate = Path(name)
        if not candidate.is_absolute() and not candidate.exists():
            candidate = REPO_ROOT / "datasets" / name
        if candidate.suffix != ".jsonl":
            candidate = candidate.with_suffix(".jsonl")
        if not candidate.exists():
            typer.echo(f"mcpbench: unknown dataset {name!r} (looked for {candidate})")
            raise typer.Exit(code=2)
        paths.append(candidate)
    return paths


@app.command()
def eval(
    sdk: Annotated[list[str] | None, typer.Option("--sdk", "-s", help="Repeatable: official | fastmcp | adk (default: all available).")] = None,
    dataset: Annotated[list[str] | None, typer.Option("--dataset", "-d", help="Repeatable dataset (name under datasets/ or path; default: basic + composition). M3.1: interactive.")] = None,
    run_id: str = typer.Option(None, "--run-id", help="Run id (default: new)"),
) -> None:
    """Run the agent evaluation suite (SPEC.md §9-11; §18 interactive)."""

    rid = run_id or new_run_id()
    available = _available_adapters()
    dataset_paths = _resolve_datasets(dataset)
    requested = sdk or ["all"]
    unknown = [s for s in requested if s not in ("official", "fastmcp", "adk", "all")]
    if unknown:
        typer.echo(f"mcpbench: unknown sdk(s) {unknown} (expected official | fastmcp | adk)")
        raise typer.Exit(code=2)
    if "all" in requested:
        targets = [name for name in ("official", "fastmcp") if name in available]
        if "adk" in available:
            targets.append("adk")
        else:
            typer.echo("mcpbench: adk adapter not importable in this env — adk runs via `mcpbench benchmark` (envs/adk).")
    else:
        targets = []
        for name in requested:
            if name not in available:
                typer.echo(
                    f"mcpbench: {name} adapter unavailable in this environment — run the adk variant via `mcpbench benchmark` (envs/adk)."
                )
                raise typer.Exit(code=2)
            targets.append(name)

    for name in targets:
        typer.echo(f"mcpbench: eval sdk={name} run_id={rid}")
        sweep_sync(name, rid, dataset_paths)


# ---- failures (M2.3b, SPEC.md §21) ----

FAILURES_DATASET = REPO_ROOT / "datasets" / "failures.jsonl"


def _failures_config_payload(label: str, tasks: list, records: list[dict]) -> dict:
    from mcp_sdk_bench.benchmark.reliability import aggregate_runs

    per_task = {
        task.id: aggregate_runs([r for r in records if r["task_id"] == task.id])
        for task in tasks
    }
    overall = aggregate_runs(records)
    return {
        "label": label,
        "tasks": per_task,
        "overall": {
            "success_rate": overall.get("success_rate"),
            "recovery_probability": overall.get("recovery_probability"),
            "recovery_probability_reason": overall.get("recovery_probability_reason"),
            "duplicate_side_effect_rate": overall.get("duplicate_side_effect_rate"),
            "incorrect_final_state_rate": overall.get("incorrect_final_state_rate"),
            "protocol_error_rate": overall.get("protocol_error_rate"),
        },
    }


def _write_failures_partial(run_id: str, sdk: str, n: int, configs: dict, records: list[dict]) -> Path:
    path = result_mod.run_dir(run_id) / f"failures-{sdk}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "sdk": sdk,
        "n_runs": n,
        "model": os.environ.get("MODEL_NAME"),
        "environment": environment_snapshot(),
        "configs": configs,
        "records": records,
    }
    path.write_text(json.dumps(payload, indent=2, default=str))
    return path


def _run_adk_failures_via_env(run_id: str, n: int, config_names: list[str]) -> int:
    """The ADK variant must run inside envs/adk (DECISIONS.md D1). Re-exec
    `failures --sdk adk --partial` there; it writes failures-adk.json."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [
        "uv",
        "run",
        "--project",
        str(REPO_ROOT / "envs" / "adk"),
        "python",
        "-m",
        "mcp_sdk_bench.cli",
        "failures",
        "--sdk",
        "adk",
        "--run-id",
        run_id,
        "--n",
        str(n),
        "--partial",
    ]
    for name in config_names:
        cmd += ["--config", name]
    typer.echo(f"mcpbench: running adk failures in envs/adk: {' '.join(cmd[:5])} ...")
    proc = subprocess.run(cmd, cwd=REPO_ROOT, env=env, check=False)
    if proc.returncode != 0:
        typer.echo(f"mcpbench: adk failures run failed (exit {proc.returncode})")
    return proc.returncode


def _fmt_rate(value: object) -> str:
    return f"{value:.2f}" if isinstance(value, (int, float)) else "-"


def _print_failures_table(payload: dict) -> None:
    typer.echo(f"\n=== mcpbench failures — run {payload['run_id']} ===")
    typer.echo(
        f"{'sdk':<10} {'config':<12} {'success':>8} {'recovery':>9} "
        f"{'dup_rate':>9} {'bad_state':>10} {'proto_err':>10}"
    )
    for sdk, configs in sorted(payload["per_sdk"].items()):
        for config_name, cell in configs.items():
            overall = cell["overall"]
            typer.echo(
                f"{sdk:<10} {config_name:<12} "
                f"{_fmt_rate(overall['success_rate']):>8} "
                f"{_fmt_rate(overall['recovery_probability']):>9} "
                f"{_fmt_rate(overall['duplicate_side_effect_rate']):>9} "
                f"{_fmt_rate(overall['incorrect_final_state_rate']):>10} "
                f"{_fmt_rate(overall['protocol_error_rate']):>10}"
            )


@app.command()
def failures(
    n: int = typer.Option(3, "--n", help="Runs per (task, sdk, config) cell (SPEC.md §23)."),
    sdk: Annotated[list[str] | None, typer.Option("--sdk", "-s", help="Repeatable: official | fastmcp | adk (default: all three).")] = None,
    config: Annotated[list[str] | None, typer.Option("--config", "-c", help="Repeatable fault config name (default: BASELINE FAIL_BEFORE FAIL_AFTER LATENCY).")] = None,
    run_id: str = typer.Option(None, "--run-id", help="Run id (default: new)"),
    partial: bool = typer.Option(False, "--partial", hidden=True, help="Write only the per-SDK partial (used by the envs/adk re-exec)."),
) -> None:
    """Run the failure-injection reliability experiment (SPEC.md §21).

    Drives datasets/failures.jsonl across the fault config grid. Wire-level
    configs (drop/malformed) are excluded from the default grid — see
    benchmark.reliability's module docstring.
    """
    import asyncio

    from mcp_sdk_bench.benchmark.reliability import (
        DEFAULT_FAULT_CONFIGS,
        fault_config_label,
        run_reliability,
    )
    from mcp_sdk_bench.evals.datasets import load_dataset

    rid = run_id or new_run_id()
    config_names = config or list(DEFAULT_FAULT_CONFIGS)
    unknown_configs = [c for c in config_names if c not in DEFAULT_FAULT_CONFIGS]
    if unknown_configs:
        typer.echo(
            f"mcpbench: unknown fault config(s) {unknown_configs} "
            f"(expected one of {sorted(DEFAULT_FAULT_CONFIGS)})"
        )
        raise typer.Exit(code=2)

    available = _available_adapters()
    requested = sdk or ["official", "fastmcp", "adk"]
    unknown_sdks = [s for s in requested if s not in ("official", "fastmcp", "adk")]
    if unknown_sdks:
        typer.echo(f"mcpbench: unknown sdk(s) {unknown_sdks} (expected official | fastmcp | adk)")
        raise typer.Exit(code=2)
    inline = [s for s in requested if s in available]
    unavailable = [s for s in requested if s not in available]
    # ADK runs via envs/adk re-exec (DECISIONS.md D1), unless we ARE the
    # re-exec'd adk process (partial mode), where any unavailable requested
    # SDK is a usage error.
    deferred_adk = not partial and "adk" in unavailable
    if partial and unavailable:
        typer.echo(f"mcpbench: requested sdk(s) unavailable in this environment: {unavailable}")
        raise typer.Exit(code=2)

    tasks = load_dataset(FAILURES_DATASET)
    failed = False
    for name in inline:
        per_config: dict = {}
        all_records: list[dict] = []
        for config_name in config_names:
            fault_config = DEFAULT_FAULT_CONFIGS[config_name]
            typer.echo(f"mcpbench: failures sdk={name} config={config_name} run_id={rid}")
            try:
                records = asyncio.run(run_reliability(tasks, name, fault_config, n))
            except Exception as err:  # noqa: BLE001 — experiment-level error: report, mark, continue other SDKs
                typer.echo(f"mcpbench: failures sdk={name} config={config_name} failed: {err}")
                failed = True
                continue
            all_records.extend(records)
            per_config[config_name] = _failures_config_payload(
                fault_config_label(fault_config), tasks, records
            )
        path = _write_failures_partial(rid, name, n, per_config, all_records)
        typer.echo(f"mcpbench: wrote {path}")

    if deferred_adk and not failed and _run_adk_failures_via_env(rid, n, config_names) != 0:
        failed = True

    if not partial:
        per_sdk: dict = {}
        for path in sorted(result_mod.run_dir(rid).glob("failures-*.json")):
            payload = json.loads(path.read_text())
            per_sdk[payload["sdk"]] = payload["configs"]
        latest = {
            "run_id": rid,
            "n_runs": n,
            "model": os.environ.get("MODEL_NAME"),
            "per_sdk": per_sdk,
        }
        result_mod.LATEST_DIR.mkdir(parents=True, exist_ok=True)
        (result_mod.LATEST_DIR / "failures.json").write_text(
            json.dumps(latest, indent=2, default=str)
        )
        typer.echo("mcpbench: reliability results written to results/latest/failures.json")
        _print_failures_table(latest)

    if failed:
        raise typer.Exit(code=1)


@app.command()
def extensions() -> None:
    """Run custom extension experiments (SPEC.md §16)."""
    _not_implemented("M4")


@app.command()
def apps() -> None:
    """Run MCP Apps experiments (SPEC.md §12)."""
    _not_implemented("M4")


@app.command()
def skills() -> None:
    """Run Skills over MCP experiments (SPEC.md §13-15)."""
    _not_implemented("M5")


# ---- tasks (M3.2, SPEC.md §17) ----

TASK_LAYERS = {"official": "protocol", "fastmcp": "app-level", "adk": "app-level"}
TASK_TOOL = "generate_monthly_report"
TASK_TERMINAL = {"completed", "failed", "cancelled"}


async def _poll_task_terminal(adapter, handle: str, timeout: float = 60.0):
    """Poll until terminal; return (final_view, statuses, progresses)."""
    import asyncio

    statuses: list[str] = []
    progresses: list[float] = []
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        view = await adapter.poll_task(handle)
        if not statuses or view.status != statuses[-1]:
            statuses.append(view.status)
        if not progresses or view.progress != progresses[-1]:
            progresses.append(view.progress)
        if view.status in TASK_TERMINAL:
            return view, statuses, progresses
        await asyncio.sleep(0.1)
    return None, statuses, progresses


async def _tasks_lifecycle(sdk: str) -> dict:
    """Deterministic lifecycle matrix for one SDK through its adapter (no
    model anywhere). Rows: start-complete, cancel, failure (injected),
    concurrent. Official additionally records wire-level evidence (real
    tasks/list, server-pushed notifications)."""
    import asyncio
    import contextlib

    from mcp_sdk_bench.faults import INJECTED_TASK_FAILURE

    cls = _available_adapters()[sdk]
    rows: list[dict] = []

    @contextlib.asynccontextmanager
    async def _adapter(env: dict[str, str] | None = None):
        # AdkAdapter forwards os.environ to its server subprocess and takes
        # no env kwarg; official/fastmcp take a merged env kwarg.
        saved: dict[str, str | None] = {}
        if sdk == "adk":
            for key, value in (env or {}).items():
                saved[key] = os.environ.get(key)
                os.environ[key] = value
            adapter = cls()
        else:
            adapter = cls(env=env)
        try:
            await adapter.connect()
            yield adapter
        finally:
            await adapter.close()
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    # start -> complete
    async with _adapter() as adapter:
        started = await adapter.start_task(TASK_TOOL)
        final, statuses, progresses = await _poll_task_terminal(adapter, started.handle)
        ok = (
            final is not None
            and final.status == "completed"
            and final.progress == 1.0
            and final.result is not None
            and {"report_id", "rows", "generated_at"} <= set(final.result)
            and progresses == sorted(progresses)
        )
        row: dict = {
            "scenario": "start-complete",
            "handle": started.handle,
            "statuses": statuses,
            "progress": progresses,
            "final": final.model_dump() if final else None,
        }
        if sdk == "official":
            await asyncio.sleep(0.3)  # let the final notifications land
            row["wire_progress_notifications"] = len(adapter.pushed_progress(started.handle))
            row["wire_task_status_notification"] = adapter.pushed_status(started.handle)
            ok = (
                ok
                and row["wire_progress_notifications"] >= 2
                and row["wire_task_status_notification"] == "completed"
            )
        row["ok"] = ok
        rows.append(row)

    # cancel mid-run
    async with _adapter() as adapter:
        started = await adapter.start_task(TASK_TOOL)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30.0
        while True:
            view = await adapter.poll_task(started.handle)
            if view.status == "running" and view.progress > 0.0:
                break
            if loop.time() >= deadline:
                break
            await asyncio.sleep(0.1)
        cancelled = await adapter.cancel_task(started.handle)
        await asyncio.sleep(0.3)
        after = await adapter.poll_task(started.handle)
        rows.append(
            {
                "scenario": "cancel",
                "handle": started.handle,
                "progress_at_cancel": cancelled.progress,
                "final": after.model_dump(),
                "ok": cancelled.status == "cancelled"
                and cancelled.result is None
                and after.status == "cancelled"
                and after.progress == cancelled.progress
                and after.result is None,
            }
        )

    # concurrent pair + 2-task limit (official: real tasks/list evidence)
    async with _adapter() as adapter:
        first = await adapter.start_task(TASK_TOOL)
        second = await adapter.start_task(TASK_TOOL)
        limit_error: str | None = None
        try:
            await adapter.start_task(TASK_TOOL)
        except RuntimeError as err:
            limit_error = str(err)
        row = {
            "scenario": "concurrent",
            "handles": [first.handle, second.handle],
            "limit_error": limit_error,
        }
        if sdk == "official":
            listed = await adapter.list_tasks()
            row["wire_tasks_list"] = sorted(t.handle for t in listed)
        (final_first, _, _), (final_second, _, _) = await asyncio.gather(
            _poll_task_terminal(adapter, first.handle),
            _poll_task_terminal(adapter, second.handle),
        )
        row["finals"] = [
            final_first.model_dump() if final_first else None,
            final_second.model_dump() if final_second else None,
        ]
        row["ok"] = (
            first.handle != second.handle
            and final_first is not None
            and final_second is not None
            and final_first.status == "completed"
            and final_second.status == "completed"
            and limit_error is not None
            and "limit" in limit_error
            and (
                sdk != "official"
                or {first.handle, second.handle} <= set(row["wire_tasks_list"])
            )
        )
        rows.append(row)

    # injected mid-task failure (deterministic: rate 1.0)
    async with _adapter({"TASK_FAILURE_RATE": "1.0"}) as adapter:
        started = await adapter.start_task(TASK_TOOL)
        final, statuses, _ = await _poll_task_terminal(adapter, started.handle)
        rows.append(
            {
                "scenario": "failure",
                "handle": started.handle,
                "statuses": statuses,
                "final": final.model_dump() if final else None,
                "ok": final is not None
                and final.status == "failed"
                and final.error == INJECTED_TASK_FAILURE
                and final.result is None,
            }
        )

    return {
        "layer": TASK_LAYERS[sdk],
        "rows": rows,
        "ok": all(row["ok"] for row in rows),
    }


def _write_tasks_partial(run_id: str, sdk: str, payload: dict) -> Path:
    path = result_mod.run_dir(run_id) / f"tasks-{sdk}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"run_id": run_id, "sdk": sdk, "environment": environment_snapshot(), **payload},
            indent=2,
            default=str,
        )
    )
    return path


def _run_adk_tasks_via_env(run_id: str) -> int:
    """The ADK variant must run inside envs/adk (DECISIONS.md D1). Re-exec
    `tasks --sdk adk --partial` there; it writes tasks-adk.json."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [
        "uv",
        "run",
        "--project",
        str(REPO_ROOT / "envs" / "adk"),
        "python",
        "-m",
        "mcp_sdk_bench.cli",
        "tasks",
        "--sdk",
        "adk",
        "--run-id",
        run_id,
        "--partial",
    ]
    typer.echo(f"mcpbench: running adk tasks in envs/adk: {' '.join(cmd[:5])} ...")
    proc = subprocess.run(cmd, cwd=REPO_ROOT, env=env, check=False)
    if proc.returncode != 0:
        typer.echo(f"mcpbench: adk tasks run failed (exit {proc.returncode})")
    return proc.returncode


def _print_tasks_table(per_sdk: dict) -> None:
    typer.echo(f"\n{'sdk':<10} {'layer':<10} {'scenario':<16} {'final':<10} {'progress':<18} {'ok':>3}")
    for sdk, payload in sorted(per_sdk.items()):
        for row in payload.get("rows", []):
            final = row.get("final") or {}
            finals = row.get("finals")
            if finals:
                final = finals[-1] or {}
            progress = row.get("progress")
            progress_str = ",".join(f"{p:g}" for p in progress) if progress else "-"
            typer.echo(
                f"{sdk:<10} {payload['layer']:<10} {row['scenario']:<16} "
                f"{final.get('status', '-'):<10} {progress_str:<18} "
                f"{'yes' if row['ok'] else 'NO':>3}"
            )


@app.command()
def tasks(
    sdk: Annotated[list[str] | None, typer.Option("--sdk", "-s", help="Repeatable: official | fastmcp | adk (default: all three).")] = None,
    run_id: str = typer.Option(None, "--run-id", help="Run id (default: new)"),
    partial: bool = typer.Option(False, "--partial", hidden=True, help="Write only the per-SDK partial (used by the envs/adk re-exec)."),
) -> None:
    """Run the Tasks extension experiment (SPEC.md §17, M3.2).

    Deterministic protocol-level runner (NO model): per SDK, drive the
    lifecycle matrix (start/complete, cancel, concurrent, injected failure)
    through the adapters — the official adapter exercises the real protocol
    Tasks methods, fastmcp/adk the app-level plain tools — and write
    results/latest/tasks.json. Exit 1 on any lifecycle mismatch.
    """
    import asyncio

    rid = run_id or new_run_id()
    available = _available_adapters()
    requested = sdk or ["official", "fastmcp", "adk"]
    unknown = [s for s in requested if s not in ("official", "fastmcp", "adk")]
    if unknown:
        typer.echo(f"mcpbench: unknown sdk(s) {unknown} (expected official | fastmcp | adk)")
        raise typer.Exit(code=2)
    inline = [s for s in requested if s in available]
    deferred_adk = not partial and "adk" in requested and "adk" not in available
    if partial and len(inline) != len(requested):
        typer.echo("mcpbench: requested sdk(s) unavailable in this environment")
        raise typer.Exit(code=2)

    failed = False
    for name in inline:
        typer.echo(f"mcpbench: tasks sdk={name} layer={TASK_LAYERS[name]} run_id={rid}")
        try:
            payload = asyncio.run(_tasks_lifecycle(name))
        except Exception as err:  # noqa: BLE001 — experiment-level error: report, mark, continue other SDKs
            typer.echo(f"mcpbench: tasks sdk={name} failed: {err}")
            payload = {"layer": TASK_LAYERS[name], "rows": [], "ok": False, "error": str(err)}
            failed = True
        _write_tasks_partial(rid, name, payload)
        if not payload["ok"]:
            failed = True

    if deferred_adk and _run_adk_tasks_via_env(rid) != 0:
        failed = True

    if not partial:
        per_sdk: dict = {}
        for path in sorted(result_mod.run_dir(rid).glob("tasks-*.json")):
            payload = json.loads(path.read_text())
            per_sdk[payload["sdk"]] = {
                "layer": payload["layer"],
                "rows": payload["rows"],
                "ok": payload["ok"],
            }
            if not payload["ok"]:
                failed = True
        latest = {"run_id": rid, "experiment": "tasks (SPEC.md §17)", "per_sdk": per_sdk}
        result_mod.LATEST_DIR.mkdir(parents=True, exist_ok=True)
        (result_mod.LATEST_DIR / "tasks.json").write_text(
            json.dumps(latest, indent=2, default=str)
        )
        typer.echo("mcpbench: tasks results written to results/latest/tasks.json")
        _print_tasks_table(per_sdk)

    if failed:
        raise typer.Exit(code=1)


@app.command()
def benchmark(run_id: str = typer.Option(None, "--run-id", help="Run id (default: new)")) -> None:
    """Run the full benchmark: official + fastmcp inline, adk via envs/adk, then report."""
    rid = run_id or new_run_id()
    for name in ("official", "fastmcp"):
        typer.echo(f"mcpbench: benchmark sdk={name} run_id={rid}")
        sweep_sync(name, rid)
    _run_adk_via_env(rid)
    typer.echo("mcpbench: generating report ...")
    result_mod.report(rid)
    _print_summary(rid)


def _print_summary(run_id: str) -> None:
    import json

    summary_path = Path(REPO_ROOT) / "results" / "latest" / "summary.json"
    if not summary_path.exists():
        return
    summary = json.loads(summary_path.read_text())
    typer.echo(f"\n=== mcpbench summary — run {run_id} ===")
    typer.echo(f"{'sdk':<10} {'n':>3} {'success':>8} {'final_state':>11} {'tool_sel':>8} {'traj':>6} {'lat_ms':>9} {'mcp_ms':>8}")
    for sdk, agg in sorted(summary["per_sdk"].items()):
        typer.echo(
            f"{sdk:<10} {agg['n']:>3} "
            f"{agg['task_success_rate'] or 0:>7.2%} "
            f"{agg['correct_final_state_rate'] or 0:>10.2%} "
            f"{agg['tool_selection_accuracy'] or 0:>7.2%} "
            f"{agg['trajectory_correctness'] or 0:>5.2%} "
            f"{agg['mean_total_latency_ms'] or 0:>9.0f} "
            f"{agg['mean_mcp_latency_ms'] or 0:>8.0f}"
        )


@app.command()
def report(run_id: str = typer.Option(None, "--run-id", help="Run id (default: latest)")) -> None:
    """Generate reports: results/latest/*.json (SPEC.md §25)."""
    from mcp_sdk_bench.benchmark.result import RESULTS_DIR
    from mcp_sdk_bench.benchmark.result import report as gen_report

    rid = run_id
    if rid is None:
        eval_dirs = sorted(RESULTS_DIR.glob("*/eval-*.json"))
        if not eval_dirs:
            typer.echo("mcpbench: no eval results found — run `mcpbench benchmark` first.")
            raise typer.Exit(code=2)
        rid = eval_dirs[-1].parent.name
    gen_report(rid)
    _print_summary(rid)


if __name__ == "__main__":
    app()
