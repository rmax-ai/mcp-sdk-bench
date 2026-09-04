"""CLI smoke tests — subcommand surface and milestone gating."""
import subprocess
import sys


def run_mcpbench(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mcp_sdk_bench.cli", *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


ALL_SUBCOMMANDS = {
    "capabilities",
    "conformance",
    "interop",
    "interoperability",
    "eval",
    "extensions",
    "apps",
    "skills",
    "tasks",
    "benchmark",
    "report",
}


def test_help_lists_all_subcommands() -> None:
    r = run_mcpbench("--help")
    assert r.returncode == 0, r.stderr
    assert all(name in r.stdout for name in ALL_SUBCOMMANDS)


def test_version() -> None:
    r = run_mcpbench("--version")
    assert r.returncode == 0
    assert "0.1.0" in r.stdout


def test_unimplemented_exits_3_with_milestone() -> None:
    still_stubbed = {"conformance", "extensions", "apps", "skills", "tasks"}
    for name in still_stubbed:
        r = run_mcpbench(name)
        assert r.returncode == 3, f"{name}: expected exit 3, got {r.returncode}"
        assert "not yet implemented" in r.stdout, name


def test_eval_rejects_unknown_sdk() -> None:
    r = run_mcpbench("eval", "--sdk", "bogus")
    assert r.returncode == 2
    assert "unknown sdk" in r.stdout


def test_adk_availability_matches_real_env_check() -> None:
    """_available_adapters must gate adk on adk_env_ok() — google.adk is
    importable in the main env but its mcp_toolset import FAILS on the
    mcp 1.x/2.x clash, so any weaker check makes the failures/eval commands
    run ADK inline and crash at connect() (regression, 2026-09-04)."""
    from mcp_sdk_bench.adapters.adk import adk_env_ok
    from mcp_sdk_bench.cli import _available_adapters

    assert ("adk" in _available_adapters()) == adk_env_ok()
