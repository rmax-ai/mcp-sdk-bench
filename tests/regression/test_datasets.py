"""Dataset integrity tests (SPEC.md §9; M1.7).

Every JSONL row must validate against the BenchmarkTask schema, task IDs must
be unique and stable, and expected tools must stay inside the M1 contract.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mcp_sdk_bench.evals import BenchmarkTask, load_dataset

REPO_ROOT = Path(__file__).resolve().parents[2]
DATASET_FILES = [
    REPO_ROOT / "datasets" / "basic.jsonl",
    REPO_ROOT / "datasets" / "composition.jsonl",
]

# M1 5-tool contract plus the resource-read pseudo-tool (SPEC.md §6).
M1_CONTRACT_TOOLS = {
    "get_ticket",
    "update_ticket",
    "get_inventory",
    "reserve_inventory",
    "deploy_service",
}
ALLOWED_EXPECTED_TOOLS = M1_CONTRACT_TOOLS | {"read_resource"}


def _all_tasks() -> list[BenchmarkTask]:
    return [task for path in DATASET_FILES for task in load_dataset(path)]


@pytest.mark.parametrize("path", DATASET_FILES, ids=lambda p: p.name)
def test_every_row_validates_against_the_schema(path: Path) -> None:
    tasks = load_dataset(path)
    assert tasks, f"{path.name} is empty"
    for task in tasks:
        assert task.id
        assert task.category in {"A", "B", "C", "D"}
        assert task.prompt


def test_task_ids_are_unique_across_datasets() -> None:
    ids = [task.id for task in _all_tasks()]
    assert len(ids) == len(set(ids))


def test_stable_spot_check_ids_exist() -> None:
    ids = {task.id for task in _all_tasks()}
    assert "basic-001" in ids
    assert "comp-003" in ids


def test_expected_tools_stay_within_the_m1_contract() -> None:
    for task in _all_tasks():
        unexpected = set(task.expected_tools) - ALLOWED_EXPECTED_TOOLS
        assert not unexpected, f"{task.id} expects non-M1 tools: {unexpected}"
        unexpected_args = set(task.expected_args) - ALLOWED_EXPECTED_TOOLS
        assert not unexpected_args, f"{task.id} has args for non-M1 tools: {unexpected_args}"
