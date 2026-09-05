"""Make the M2.1 conformance harness (session factories, StdioProxy)
importable as `helpers` from the task-lifecycle tests (M3.2 report kind /
M3.3 migration kind)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "conformance"))
