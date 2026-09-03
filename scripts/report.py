"""scripts/report.py — thin wrapper: generate reports (SPEC.md §25)."""
import sys

from mcp_sdk_bench.cli import app

if __name__ == "__main__":
    app(["report", *sys.argv[1:]])
