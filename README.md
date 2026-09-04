# mcp-sdk-bench

**Executable benchmark comparing the three major Python approaches to building and consuming MCP systems:** FastMCP 4.x, Google ADK's MCP integration, and the official MCP Python SDK v2.

Not a tutorial. Not a feature-comparison article. The deliverable is **falsifiable experimental evidence** across five questions: protocol capability, developer ergonomics, runtime behavior, agent behavior, and extensibility — including modern MCP surface: protocol 2026-07-28, Tasks, Elicitation, Multi-Round-Trip, Extensions, MCP Apps, and Skills over MCP.

## Status

| Milestone | Scope | Status |
|---|---|---|
| M1 | Vertical slice: 3 integrations, shared world, 5 tools/1 resource/1 prompt, LangGraph agent, 11 eval tasks, graders, traces, report | ✅ acceptance passed (2026-09-04) |
| M2 | Failure injection + full conformance + cross-implementation interoperability | 🔨 next |
| M3 | Elicitation + multi-round-trip + Tasks | — |
| M4 | MCP Apps + custom extension `io.mcpbench.audit` | — |
| M5 | Skills over MCP + progressive disclosure + multi-server + auth/identity | — |

## Quickstart

```bash
uv sync
cp .envrc.example .envrc   # then fill MODEL_* and run `direnv allow`
uv run mcpbench benchmark  # full run (M1: vertical slice)
uv run mcpbench report     # generates results/latest/* + docs/findings.md
```

Model configuration (pinned across all runs — the MCP implementation is the only independent variable):

| Variable | Purpose |
|---|---|
| `MODEL_PROVIDER` | provider identifier (e.g. openrouter, anthropic, openai-compatible endpoint) |
| `MODEL_NAME` | model id |
| `MODEL_API_KEY` | API key (loaded via `.envrc` + direnv — never committed, never in tool schemas) |

## Method (one paragraph)

A LangGraph agent drives a thin benchmark-owned adapter that presents a common protocol view over each candidate integration. The same model, temperature, prompts, task order, schemas, world state, timeouts, and agent loop run against every variant (N=10 per stochastic task, confidence intervals, deterministic graders whenever state inspection suffices). The shared world is an in-memory/SQLite simulated enterprise (employees, tickets, deployments, inventory) — no external APIs. Protocol-level conformance and cross-implementation interoperability run without any LLM.

## Documentation

| Doc | Contents |
|---|---|
| [SPEC.md](SPEC.md) | Authoritative 33-section spec (ground truth) |
| [docs/architecture.md](docs/architecture.md) | Layered architecture, adapter boundary, world, traces |
| [docs/methodology.md](docs/methodology.md) | Experiment taxonomy, variables, statistics, H1–H8 falsification criteria |
| [docs/roadmap.md](docs/roadmap.md) | M1–M5 delivery plan |
| [docs/research/findings.md](docs/research/findings.md) | Current SDK/spec state, primary-source verified (2026-09-03) |
| [docs/capability-matrix.md](docs/capability-matrix.md) | 33 capabilities × 3 SDKs, test-derived |
| [docs/extensions.md](docs/extensions.md) | Apps / Skills / Tasks / custom-extension experiments |
| [DECISIONS.md](DECISIONS.md) | Architecture decisions and rejected alternatives |

## Repository

```
src/
├── agent/       LangGraph agent: graph, prompts, runner
├── benchmark/   runner, metrics, traces, result
├── adapters/    base + fastmcp/adk/official variants (process boundary)
├── servers/     per-SDK server implementations (own uv envs)
├── world/       deterministic shared state + fixtures
├── extensions/  apps / skills / tasks / demo_extension
└── evals/       deterministic graders
tests/           conformance / interoperability / failures / regression
datasets/        basic, composition, failures, interactive, long_running, skills, adversarial (JSONL)
scripts/         bench.py, eval.py, report.py, probe_api.py
```

## CLI

```
uv run mcpbench capabilities|conformance|interoperability|eval|extensions|apps|skills|tasks|benchmark|report
```

## License

MIT — see [LICENSE](LICENSE).
