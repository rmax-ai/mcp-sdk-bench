"""Dataset schema + loader (SPEC.md §9).

Datasets are JSONL, one task per line, validated against BenchmarkTask.
`forbidden_contains` is the one optional field (default: empty); every other
schema field must be present in every row, and unknown fields are rejected so
dataset drift fails loudly instead of silently mis-grading.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict


class BenchmarkTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    category: str
    prompt: str
    expected_tools: list[str]
    expected_args: dict[str, dict[str, Any]]
    # check_id -> {field: value}; "ticket:<id>" reads via get_ticket,
    # "inventory:<name>" reads via get_inventory then item lookup.
    expected_final_state: dict[str, dict[str, Any]]
    answer_contains: list[str]
    forbidden_contains: list[str] = []
    expected_trajectory: list[str] | None
    allowed_extra_tools: list[str]


def load_dataset(path: Path) -> list[BenchmarkTask]:
    """Load and validate one JSONL dataset file."""
    tasks: list[BenchmarkTask] = []
    for line_no, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            tasks.append(BenchmarkTask.model_validate(json.loads(line)))
        except Exception as err:
            raise ValueError(f"{path}:{line_no}: invalid task row: {err}") from err
    return tasks
