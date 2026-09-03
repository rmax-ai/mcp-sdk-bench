# Capability Matrix — mcp-sdk-bench

> **STATUS: skeleton.** Cells are populated by `uv run mcpbench capabilities` (M1+) from executable tests. Until then, every cell is `TBD`. No cell may be filled from documentation alone — SPEC.md §7 requires both a primary-source link and an executable test.

Legend (SPEC.md §7): ✅ supported · ◐ partially · 🔁 via another abstraction · 🧪 experimental · ❌ unsupported · ➖ not applicable

| Capability | FastMCP 4.x | Google ADK | Official SDK | Protocol status | Evidence (docs + test) |
|---|---|---|---|---|---|
| Tool server | TBD | TBD | TBD | TBD | TBD |
| Tool client | TBD | TBD | TBD | TBD | TBD |
| Resources | TBD | TBD | TBD | TBD | TBD |
| Prompts | TBD | TBD | TBD | TBD | TBD |
| Structured tool output | TBD | TBD | TBD | TBD | TBD |
| Stdio transport | TBD | TBD | TBD | TBD | TBD |
| Streamable HTTP | TBD | TBD | TBD | TBD | TBD |
| SSE legacy support | TBD | TBD | TBD | TBD | TBD |
| 2026-07-28 protocol | TBD | TBD | TBD | TBD | TBD |
| Legacy protocol negotiation | TBD | TBD | TBD | TBD | TBD |
| Sampling | TBD | TBD | TBD | TBD | TBD |
| Elicitation | TBD | TBD | TBD | TBD | TBD |
| Multi-round-trip | TBD | TBD | TBD | TBD | TBD |
| Tasks | TBD | TBD | TBD | TBD | TBD |
| Cancellation | TBD | TBD | TBD | TBD | TBD |
| Progress | TBD | TBD | TBD | TBD | TBD |
| OAuth | TBD | TBD | TBD | TBD | TBD |
| Client credentials | TBD | TBD | TBD | TBD | TBD |
| Identity propagation | TBD | TBD | TBD | TBD | TBD |
| Extensions negotiation | TBD | TBD | TBD | TBD | TBD |
| MCP Apps | TBD | TBD | TBD | TBD | TBD |
| Skills over MCP | TBD | TBD | TBD | TBD | TBD |
| Custom extensions | TBD | TBD | TBD | TBD | TBD |
| Multi-server aggregation | TBD | TBD | TBD | TBD | TBD |
| Dynamic tools | TBD | TBD | TBD | TBD | TBD |
| Tool filtering | TBD | TBD | TBD | TBD | TBD |
| Schema generation | TBD | TBD | TBD | TBD | TBD |
| Context injection / DI | TBD | TBD | TBD | TBD | TBD |
| Lifecycle hooks | TBD | TBD | TBD | TBD | TBD |
| Session state | TBD | TBD | TBD | TBD | TBD |
| Stateless operation | TBD | TBD | TBD | TBD | TBD |
| Tracing | TBD | TBD | TBD | TBD | TBD |
| Testing utilities | TBD | TBD | TBD | TBD | TBD |

Machine-readable output: `results/latest/capabilities.json`, generated from `tests/conformance/` + `tests/interoperability/` results, never hand-edited.
