"""scripts/eval.py — thin wrapper: run the eval suite for one or all SDKs (SPEC.md §26).

Usage: uv run python scripts/eval.py [--sdk fastmcp|official|adk] [--run-id RID]
"""
import sys

from mcp_sdk_bench.cli import app

if __name__ == "__main__":
    app(["eval", *sys.argv[1:]])
