# ARCHITECTURE — mcp-sdk-bench

System architecture for the executable MCP SDK benchmark. Formalizes the layered design of SPEC.md §2, the repository layout of §4, and the experimental subsystems of §5–§22, against the milestone plan of §32. This document makes no claims about specific SDK versions, APIs, or feature support; version/API facts land in `docs/research/` during Phase 1, per SPEC.md §28.

## 1. Overview and purpose

The central research question (SPEC.md preamble): *how do FastMCP, Google ADK's MCP integration, and the official MCP SDK differ in protocol coverage, developer ergonomics, runtime behavior, extensibility, interoperability, and their effect on real agent behavior?*

Design constraints that follow directly from the preamble and §1–§2:

- The deliverable is **executable experiments**, not a feature-comparison article (SPEC.md preamble). Every capability claim requires (a) a primary-documentation link and (b) an executable test; documentation-only support is not experimentally verified (SPEC.md §7).
- The three candidates are not symmetric. ADK is an agent framework with MCP integration; FastMCP and the official SDK provide broader MCP server/client primitives (SPEC.md preamble). No abstraction is assumed common to all three.
- Capabilities are labeled honestly on a six-value scale — supported / partially supported / supported through another abstraction / experimental / unsupported / not applicable — and never coerced into false symmetry (SPEC.md preamble, §7). Each feature additionally receives a provenance classification per §29.
- Versions, APIs, and protocol maturity are open questions until Phase 1 primary-source research (SPEC.md §28); the architecture below is version-agnostic by construction.

## 2. Layered architecture

SPEC.md §2 defines the benchmark topology. Formalized as six layers with strict downward dependency; the agent must never reach past the adapter boundary into a candidate abstraction.

```
L1  LangGraph agent            — fixed; the only agent in the benchmark
L2  Benchmark API              — run control, task delivery, metrics, traces
L3  MCP adapter boundary       — substitution point; only SDK-touching code
L4  Common protocol view       — benchmark-owned capability projection
L5  Candidate variants         — FastMCP / ADK MCP / Official SDK
L6  Shared test world          — single source of truth for all variants
```

- **L1 — LangGraph agent** (SPEC.md §2). One agent implementation, one prompt set, one retry policy, one context budget. It is reused verbatim across all comparable experiments; only the MCP integration under test changes (§2, §23). Google ADK is excluded as the common agent because ADK is itself a candidate; no agent-framework-native MCP abstraction may sit between the agent and the candidates where it would hide candidate-specific behavior (§2).
- **L2 — Benchmark API** (SPEC.md §2). Benchmark-owned surface the agent actually calls: task dataset loading (`datasets/*.jsonl`), run lifecycle, metric recording (§10), normalized trace emission (§22), grading invocation. It never imports candidate SDKs.
- **L3 — MCP adapter boundary** (SPEC.md §2). The only layer that touches a candidate's client surface (`src/adapters/`, §4). It normalizes the mechanical aspects of talking to an MCP server: connection bootstrap, transport selection, tool discovery and schema projection onto L4, call dispatch, error mapping into a canonical error taxonomy, lifecycle teardown. Swapping the adapter swaps the candidate; everything above L3 is SDK-agnostic.
- **L4 — Common protocol view**. A minimal, benchmark-owned representation of what a server exposes (tools, resources, prompts, JSON schemas, offered protocol features). It is a **projection for the planner and graders**, not a translation layer: absence of a feature in the projection must be visible as absence, never as silent emulation.
- **L5 — Candidate variants** (SPEC.md §2, §4, §6). Three logically identical MCP servers (`src/servers/fastmcp|adk|official/`) exposing the §6 contract, each built with its own SDK, plus that SDK's client side exercised through L3.
- **L6 — Shared test world** (SPEC.md §5). Deterministic enterprise simulation state that every server variant reads/writes and every grader inspects (§10). See §4 below.

**Normalization boundary — non-negotiable** (SPEC.md §2, final paragraph): the adapter normalizes *how* a candidate is driven (connection, call mechanics, error surfacing, lifecycle). It must never normalize *what* a candidate can express. Where direct normalization would erase a meaningful SDK difference — a missing protocol feature, a different abstraction model, an ergonomic gap — the difference is **not** hidden behind the adapter; it becomes a separate capability experiment with its own lane, its own provenance classification (§29), and its own honest label (§7, preamble). This rule also blocks the anti-goal "hide unsupported features behind compatibility adapters" (SPEC.md §30).

## 3. Repository layout mapping (SPEC.md §4)

| Path | Purpose |
|---|---|
| `README.md`, `pyproject.toml`, `uv.lock` | Entry point and pinned dependency set; `uv.lock` committed for benchmark reproducibility |
| `docs/` | `architecture.md`, `methodology.md`, `capability-matrix.md`, `extensions.md`, `findings.md` (generated report, §27) |
| `src/agent/` | `graph.py` (LangGraph graph, L1), `prompts.py` (pinned prompt text), `runner.py` (per-run orchestration) |
| `src/benchmark/` | `runner.py` (L2 run control), `metrics.py` (§10 metric definitions), `traces.py` (§22 normalized traces), `result.py` (result records keyed by run_id + sdk) |
| `src/adapters/` | `base.py` (L3 adapter interface + canonical error taxonomy), `fastmcp.py`, `adk.py`, `official.py` (one per candidate) |
| `src/servers/` | `fastmcp/`, `adk/`, `official/` — three logically identical servers per §6 (L5) |
| `src/world/` | `state.py` (world state interface, L6), `fixtures.py` (deterministic seeded fixtures), `reset.py` (world reset between tasks) |
| `src/extensions/` | `apps/` (§12), `skills/` (§13–§15), `tasks/` (§17), `demo_extension/` (`io.mcpbench.audit`, §16) |
| `src/evals/` | Deterministic graders, trajectory evaluation (§10–§11) |
| `tests/` | `conformance/` (discovery/schema/errors/concurrency/lifecycle, §8), `interoperability/` (§8 pairing matrix), `failures/` (§21), `regression/` |
| `datasets/` | JSONL task sets with stable task IDs: basic, composition, failures, interactive, long_running, skills, adversarial (per §9 categories) |
| `results/` | `latest/summary.json`, `capabilities.json`, `agent-evals.json`, `performance.json`, `interoperability.json`, `dx.json` (§25); `environment.json` version record (§28); gitignored except `.gitkeep` |
| `scripts/` | `bench.py`, `eval.py`, `report.py` behind the `uv run mcpbench` CLI (§26) |

## 4. Shared benchmark world (SPEC.md §5)

A deterministic simulated enterprise environment, never a network-bound one (SPEC.md §5; model API calls are the single permitted network dependency). Entities: employees, projects, tickets, documents, deployments, inventory, with seeded example state such as `employees.alice.team == payments`, `tickets["PAY-123"].status == OPEN`, `deployments.checkout.version == "1.8.2"` (§5). Operations exposed to all servers: `search_documents`, `get_ticket`, `create_ticket`, `update_ticket`, `find_employee`, `get_inventory`, `reserve_inventory`, `get_deployment`, `deploy_service` (§5).

Three properties make the world the benchmark's spine:

1. **Single source of truth.** Every server variant operates on exactly the same world state (§5), and every deterministic grader inspects that same state to score outcomes (§10). World, servers, and graders therefore share one state model (`src/world/state.py`).
2. **Determinism guarantees.** Seeded RNG for fixtures and fault injection (§21); no logic dependence on wall-clock time (long-running operations are simulated on a timer, §9-H); full world reset before each task via `reset.py`; identical data and world state across SDK runs is an explicit controlled variable (§23).
3. **Task outcome definition.** A task's success condition is expressed as an expected final world state (e.g., "Close PAY-123" ⇒ `world.tickets["PAY-123"].status == CLOSED`, §10), which is what makes grading deterministic.

**Open decision — storage backend.** SPEC.md §5 permits in-memory or SQLite. Trade-offs: in-memory gives speed and zero serialization overhead but no transactionality; SQLite gives ACID semantics and cross-process sharing (needed once servers run as separate processes in the auth topology of §19 and multi-server topology of §20) at the cost of I/O that can leak into runtime measurements. `src/world/state.py` will expose a storage-agnostic interface; the concrete backend is chosen at M1 with concurrency behavior under the §8 100-request test as the deciding measurement.

## 5. Core MCP server contract (SPEC.md §6)

Each candidate implements the logically identical server (L5):

- **Tools (7):** `search_documents`, `get_ticket`, `create_ticket`, `update_ticket`, `get_inventory`, `reserve_inventory`, `deploy_service`.
- **Resources (3):** `company://policies/deployment`, `company://inventory`, `ticket://{id}`.
- **Prompts (3):** `incident-triage`, `onboarding`, `deployment-review`.

Two equivalence requirements make cross-candidate comparison valid:

- **Schema equivalence.** Tool descriptions and JSON schemas must be equivalent across candidates (§6). Identical schemas are a controlled variable (§23) and a precondition for the §8 SCHEMA suite and for comparing agent tool-selection behavior (§9) without schema-induced confounds.
- **Normalized manifests.** Manifests are generated automatically from each running server so that differences are inspectable by machine, not by eyeball (§6). Manifests feed the auto-derived capability matrix (§7) and the interoperability matrix (§8).

The M1 slice deliberately implements a subset — 5 tools, 1 resource, 1 prompt (§32) — while keeping the same contract shape so later milestones extend rather than reshape the server.

## 6. Deterministic protocol benchmark layers (SPEC.md §8)

LLM-independent protocol tests, organized as five conformance suites plus one interoperability suite (`tests/conformance/`, `tests/interoperability/`):

| Suite | What it exercises |
|---|---|
| DISCOVERY | connect, capability discovery, list tools / resources / prompts |
| SCHEMA | primitive params, nested objects, enums, nullable, unions, arrays, structured results |
| ERRORS | invalid parameters, application exceptions, timeout, connection loss, malformed server responses |
| CONCURRENCY | 1 / 10 / 100 concurrent requests |
| LIFECYCLE | clean startup, clean shutdown, reconnect, server restart, client cancellation |
| INTEROPERABILITY | all meaningful cross-implementation client↔server pairings |

**Interoperability pairing table — critical.** SPEC.md §8 is explicit that a framework working against itself is weak evidence of interoperability. The matrix is run over at least: FastMCP client→FastMCP server, FastMCP client→Official server, Official client→FastMCP server, ADK client→FastMCP server, ADK client→Official server, plus any other supported combinations discovered in Phase 1 research. Pairings that a candidate's client surface cannot express are recorded with an honest label (unsupported / not applicable, §7) — never silently dropped. The full pairing set is gated on Phase 1 findings about each SDK's client capabilities; see `docs/research/` (Phase 1).

## 7. Agent evaluation pipeline (SPEC.md §9–§11)

The same LangGraph agent (L1) runs against each MCP variant over at least 40 deterministic tasks (§9), organized in JSONL datasets with stable task IDs (§4):

| Category | Shape | Example (SPEC.md §9) |
|---|---|---|
| A | Simple selection | status of PAY-123 → `get_ticket(PAY-123)` |
| B | Semantic tool selection | laptop availability → `get_inventory` |
| C | Multi-step composition | onboarding flow → `get_inventory` → `create_ticket` |
| D | Resources | deployment policy check → read resource → inspect → answer |
| E | Failure recovery | injected timeout/invalid response/temporary failure; recovery evaluated |
| F | Ambiguous intent | "Deploy checkout." → must seek missing env/version or elicit, not guess |
| G | Interactive workflow | production deploy needs human approval → elicitation round-trip → continuation |
| H | Long-running execution | 10–30 s simulated migration → task create/status/progress/completion/failure/cancel |

Grading is deterministic-first (§10): graders inspect final world state; correct final state outranks textual answer; an LLM judge is used only where state inspection is genuinely insufficient. **Outcome and trajectory are stored independently** (§11): a task may reach the correct final state via an inefficient or incorrect path, and both facts must survive into the metrics. Per-task metrics (§10): `task_success`, `correct_final_state`, `tool_selection_accuracy`, `tool_argument_accuracy`, `trajectory_correctness`, `unnecessary_tool_calls`, `tool_call_count`, `LLM_turn_count`, `MCP_round_trips`, `total_latency_ms`, `MCP_latency_ms`, `model_latency_ms`, `input_tokens`, `output_tokens`, `error_recovery_success`, `protocol_errors`, `user_interactions`. Trajectory correctness and the latency decomposition derive from the §22 trace substrate (§8 below).

## 8. Traces, failure injection, topologies

**Trace format (SPEC.md §22).** Every run emits a normalized trace — `run_id`, `sdk`, `task`, ordered `events` — capturing model activity, MCP requests and responses, extension negotiation, tool execution, state mutation, latency, and errors, exported as JSONL. Traces are the substrate for three consumers: trajectory evaluation (§11), latency decomposition (`MCP_latency_ms` vs `model_latency_ms`, §10), and post-hoc debugging of protocol errors.

**Failure injection (SPEC.md §21).** Deterministic, env-var-driven faults — `FAIL_TOOL_CALL=0.1`, `LATENCY_MS=500`, `DROP_CONNECTION_AFTER=3`, `MALFORMED_RESPONSE_RATE=0.05`, `TASK_FAILURE_RATE=0.1` — with seeded RNG. Reliability experiments track recovery probability, agent retries, duplicate side effects, and incorrect final state. Idempotency is an explicit target: an agent retrying a failed call must not create two tickets (§21).

**Auth topology (SPEC.md §19).** A local OAuth-like environment models human → agent → MCP client → MCP server → backend, testing identity/authorization propagation without exposing credentials to the model or into model-visible tool parameters. Scope: architectural ergonomics (OAuth support, client credentials, identity assertions where applicable, per-request metadata, DI, scopes, authorization failures), not cryptographic strength.

**Multi-server topology (SPEC.md §20).** Three servers — `people-mcp`, `ticket-mcp`, `inventory-mcp` — serve cross-server tasks ("Find Alice's open onboarding ticket and reserve a laptop"). Candidate aggregation mechanisms (FastMCP ClientGroup, ADK MCP toolsets, official SDK client composition, equivalents per Phase 1 research) are compared on configuration complexity, connection lifecycle, tool-name collisions, provenance, routing, and failure isolation.

**Extension experiment layer (SPEC.md §12–§20).** MCP Apps (`inventory_dashboard` + minimal benchmark host, §12), Skills over MCP (`incident_triage`, §13), the three-way skills comparison and progressive disclosure (§14–§15), the `io.mcpbench.audit` custom extension (§16), the Tasks experiment `generate_monthly_report` (§17), and elicitation/multi-round-trip flows (§18). These are deliberately separate capability lanes that do **not** pass through the normalization boundary — each is a capability experiment in the sense of SPEC.md §2's final paragraph, with its own honest label and provenance class. Feature maturity (especially Skills over MCP standardization status) is researched from primary sources in Phase 1 before any implementation (SPEC.md §13, §28).

## 9. Milestone dependency view (SPEC.md §32)

M1–M5 build strictly in order; extension experiments must not precede a working benchmark:

- **M1 — vertical slice:** three comparable integrations → shared deterministic world → 5 tools / 1 resource / 1 prompt → LangGraph agent → 10 evaluation tasks → deterministic state graders → normalized traces → comparison report. Gate: `uv run mcpbench benchmark` + `uv run mcpbench report` end-to-end for all three SDKs (§32, §33).
- **M2:** failure injection (§21) + interoperability matrix (§8). Depends on M1 harness; supplies Category-E recovery material and the pairing evidence.
- **M3:** elicitation + multi-round-trip (§18) + Tasks (§17). Depends on M2 failure/connection tooling (reconnect-during-task tests).
- **M4:** MCP Apps (§12) + extensions (§16). Depends on M3 round-trip machinery for UI↔host↔tool flows.
- **M5:** Skills over MCP (§13) + progressive disclosure (§14–§15), plus auth (§19) and multi-server (§20) topologies per roadmap. Depends on all prior infrastructure.

Dependency rationale (§32): keeping the research falsifiable requires the M1 benchmark to work before the project can become a feature showcase; each later milestone reuses M1's world, trace, grading, and runner substrate.

## 10. Risks, trade-offs, open questions

- **Adapter boundary drift.** The L3 adapter could quietly absorb a candidate difference (the exact anti-goal of §30). Mitigation: the §2 normalization rule, separate capability lanes, and the capability-matrix claims audit requiring doc + test evidence per cell (§7).
- **World-state contention.** Single-source-of-truth state under 100 concurrent requests (§8) stresses whichever storage backend is chosen; the SQLite vs in-memory decision (§4 above) remains open until M1 measurements.
- **Model variance vs SDK signal.** Agent-eval differences smaller than model variance must not be interpreted (§23). Mitigated by N=10, confidence intervals, and reporting the noise floor (methodology.md).
- **Machine constraints.** The benchmark host (2 cores, ~4 GiB) makes absolute latency numbers machine-specific; only relative deltas between SDKs are reported as claims (AGENTS.md execution conventions).
- **Phase 1 discovery risk.** Candidates may not expose a feature at all, or may expose it through a non-MCP abstraction (e.g., §17's three-way spec/SDK/framework distinction). These outcomes are recorded with honest labels, not engineered around.
- **Open questions.** Storage backend; which interop pairings are supportable given each SDK's client surface; whether ADK's client surface can drive the §8 pairing matrix at all — all gated on `docs/research/` (Phase 1) and recorded as decisions when resolved.
