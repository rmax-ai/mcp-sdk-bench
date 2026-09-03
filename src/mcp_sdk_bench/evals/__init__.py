"""Evaluation layer (SPEC.md §10): dataset schema/loading + deterministic graders."""
from mcp_sdk_bench.evals.datasets import BenchmarkTask, load_dataset
from mcp_sdk_bench.evals.graders import grade_task

__all__ = ["BenchmarkTask", "grade_task", "load_dataset"]
