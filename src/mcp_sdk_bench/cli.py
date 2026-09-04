"""mcpbench CLI — SPEC.md §26.

Subcommands are implemented per milestone. Unimplemented commands exit code 3
with an explicit milestone pointer so benchmark scripts can detect capability gaps.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import typer

from mcp_sdk_bench import __version__
from mcp_sdk_bench.benchmark import result as result_mod
from mcp_sdk_bench.benchmark.result import new_run_id
from mcp_sdk_bench.benchmark.sweep import REPO_ROOT, environment_snapshot, sweep_sync

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


@app.command()
def eval(
    sdk: str = typer.Option("all", "--sdk", "-s", help="Limit eval to one SDK: fastmcp | official | adk"),
    run_id: str = typer.Option(None, "--run-id", help="Run id (default: new)"),
) -> None:
    """Run the agent evaluation suite (SPEC.md §9-11)."""
    from mcp_sdk_bench.adapters import AdkAdapter, FastMCPAdapter, OfficialAdapter

    rid = run_id or new_run_id()
    available = {
        name: cls
        for name, cls in (("official", OfficialAdapter), ("fastmcp", FastMCPAdapter), ("adk", AdkAdapter))
        if cls is not None
    }
    if sdk == "all":
        targets = [name for name in ("official", "fastmcp") if name in available]
        if "adk" in available:
            targets.append("adk")
        elif sdk == "all":
            typer.echo("mcpbench: adk adapter not importable in this env — adk runs via `mcpbench benchmark` (envs/adk).")
    else:
        if sdk not in ("official", "fastmcp", "adk"):
            typer.echo(f"mcpbench: unknown sdk {sdk!r} (expected official | fastmcp | adk)")
            raise typer.Exit(code=2)
        if sdk not in available:
            typer.echo(
                f"mcpbench: {sdk} adapter unavailable in this environment — run the adk variant via `mcpbench benchmark` (envs/adk)."
            )
            raise typer.Exit(code=2)
        targets = [sdk]

    for name in targets:
        typer.echo(f"mcpbench: eval sdk={name} run_id={rid}")
        sweep_sync(name, rid)


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


@app.command()
def tasks() -> None:
    """Run Tasks extension experiments (SPEC.md §17)."""
    _not_implemented("M3")


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
