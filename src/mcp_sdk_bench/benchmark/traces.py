"""Normalized trace format (SPEC.md §22).

Events: run.start, task.start, task.end, model_call, mcp.discover,
mcp.tool_call, mcp.tool_result, mcp.resource_read, mcp.prompt_get, error.
Exported as JSONL under results/<run_id>/trace-<sdk>.jsonl.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class TraceRecorder:
    def __init__(self, run_id: str, sdk: str) -> None:
        self.run_id = run_id
        self.sdk = sdk
        self.events: list[dict[str, Any]] = []

    def record(self, event_type: str, **fields: Any) -> None:
        event: dict[str, Any] = {
            "ts_ms": int(time.time() * 1000),
            "run_id": self.run_id,
            "sdk": self.sdk,
            "type": event_type,
        }
        event.update(fields)
        self.events.append(event)

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            for event in self.events:
                f.write(json.dumps(event, default=str) + "\n")
        self.events.clear()
        return path
