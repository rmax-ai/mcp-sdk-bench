"""scripts/bench.py — thin wrapper: run the full benchmark (SPEC.md §26)."""
from mcp_sdk_bench.cli import app

if __name__ == "__main__":
    app(["benchmark"])
