"""mcpbench CLI — SPEC.md §26.

Subcommands are implemented per milestone. Unimplemented commands exit code 3
with an explicit milestone pointer so benchmark scripts can detect capability gaps.
"""
from __future__ import annotations

import typer

from mcp_sdk_bench import __version__

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


@app.command()
def capabilities() -> None:
    """Derive and report the capability matrix (SPEC.md §7)."""
    _not_implemented("M1")


@app.command()
def conformance() -> None:
    """Run deterministic protocol conformance tests (SPEC.md §8)."""
    _not_implemented("M2")


@app.command()
def interoperability() -> None:
    """Run cross-implementation client/server pairings (SPEC.md §8)."""
    _not_implemented("M2")


@app.command()
def eval(
    sdk: str = typer.Option("all", "--sdk", "-s", help="Limit eval to one SDK: fastmcp | official | adk"),
) -> None:
    """Run the agent evaluation suite (SPEC.md §9-11)."""
    _not_implemented("M1")


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
def benchmark() -> None:
    """Run the full benchmark suite for all configured SDKs."""
    _not_implemented("M1")


@app.command()
def report() -> None:
    """Generate reports: results/latest/*.json + docs/findings.md (SPEC.md §25, §27)."""
    _not_implemented("M1")


if __name__ == "__main__":
    app()
