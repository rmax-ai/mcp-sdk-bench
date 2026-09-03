# SPEC.md — MCP SDK Benchmark (mcp-sdk-bench)

> **Ground-truth specification.** Preserved verbatim from Max's original scope prompt (2026-09-03). Every downstream document references this file's sections. This document is authoritative; do not paraphrase away specificity.

Project: experimentally compare the major Python approaches for building and consuming Model Context Protocol (MCP) systems:
1. FastMCP 4.x
2. Google Agent Development Kit (ADK) MCP integration
3. Official Model Context Protocol Python SDK v2

The project must benchmark them through executable experiments rather than producing a feature-comparison article.

**Central research question:** How do FastMCP, Google ADK's MCP integration, and the official MCP SDK differ in protocol coverage, developer ergonomics, runtime behavior, extensibility, interoperability, and their effect on real agent behavior?

The benchmark must include both deterministic protocol tests and agent-level evaluations.

It must also investigate modern MCP capabilities beyond basic tools/list + tools/call, including:
MCP protocol 2026-07-28; protocol negotiation and backwards compatibility; tools; resources; prompts; structured content; sampling; elicitation; multi-round-trip interactions; authorization and propagated identity; tasks / long-running operations; progress and cancellation; extensions; MCP Apps; Skills over MCP; multiple MCP servers; tool discovery; tool filtering; schema handling; dynamic tools/capabilities; connection/session lifecycle; stdio; Streamable HTTP; observability and tracing; error propagation; concurrency; reconnect behavior; interoperability between implementations.

Do not assume that all three systems expose the same abstraction. Google ADK is an agent framework with MCP integration, while FastMCP and the official SDK provide broader MCP server/client primitives.

Represent capabilities honestly as: **supported / partially supported / supported through another abstraction / experimental / unsupported / not applicable**. Never create fake symmetry just to make a table complete.

## 1. Research goals

The project should answer five distinct questions.

**Q1 — Protocol capability.** Which MCP protocol features can each implementation actually express? Distinguish: (a) protocol feature exists, (b) library exposes it, (c) library makes it ergonomic, (d) library interoperates correctly with other implementations. These are different claims.

**Q2 — Developer experience.** How much application code and MCP-specific knowledge is required? Measure: lines of application code, configuration, boilerplate, type annotations, custom protocol handling, error-handling code, lifecycle management, extension plumbing, testing complexity. Avoid purely subjective ratings where objective measurements are possible.

**Q3 — Runtime characteristics.** Measure: startup time, connection establishment, discovery latency, tool-call latency, concurrent calls, repeated calls, memory usage, reconnect behavior, serialization overhead, failure recovery, long-running operations. Do not turn this into an artificial microbenchmark contest. Focus on differences large enough to matter architecturally.

**Q4 — Agent behavior.** Determine whether MCP implementation choices affect an agent's ability to: discover appropriate tools, select the correct tool, supply valid parameters, combine multiple tools, recover from tool errors, interact with users through elicitation, complete long-running tasks, use resources/prompts appropriately, navigate multiple MCP servers, consume skills, interact with MCP Apps where meaningful. Measure behavioral outcomes, not only protocol correctness.

**Q5 — Extensibility.** Investigate whether MCP can realistically serve as an extensible substrate for agent applications rather than simply a remote tool API. Evaluate: MCP Extensions, MCP Apps, Tasks, Skills over MCP, custom experimental extensions. Specifically investigate the boundary between: MCP core protocol → MCP extensions → agent capabilities → application-specific conventions.

## 2. Agent framework

Use Python + LangGraph + a thin benchmark-owned MCP adapter for the benchmark agent.

Do NOT use Google ADK as the common benchmark agent because ADK itself is one of the candidates. Do NOT rely on an agent framework's native MCP abstraction for the main experiment if that abstraction hides candidate-specific behavior.

Architecture:

```
              LangGraph agent
                    │
              Benchmark API
                    │
         ┌──────────┴──────────┐
         │ MCP adapter boundary │
         └──────────┬──────────┘
                    │
           common protocol view
                    │
      ┌─────────────┼─────────────┐
      │             │             │
   FastMCP       ADK MCP      Official MCP
   variant       variant         SDK
      │             │             │
      └──────────── test world ───┘
```

The agent implementation, prompt, model, task dataset, retry policy and context budget MUST remain identical between comparable experiments. Only the MCP integration under test should change. Where direct normalization would erase a meaningful SDK difference, create a separate capability experiment rather than hiding that difference behind the adapter.

## 3. Model provider

Make the model configurable. Support at minimum one model through an OpenAI-compatible or similarly provider-neutral abstraction.

Configuration:
```
MODEL_PROVIDER=
MODEL_NAME=
MODEL_API_KEY=
```

The benchmark must pin the same model, temperature, prompt and tool descriptions across runs. Model comparison is explicitly out of scope. The MCP implementation is the independent variable.

## 4. Repository structure

Use approximately:

```
mcp-sdk-bench/
├── README.md
├── pyproject.toml
├── uv.lock
├── docs/
│   ├── architecture.md
│   ├── methodology.md
│   ├── capability-matrix.md
│   ├── extensions.md
│   └── findings.md
├── src/
│   ├── agent/            # graph.py, prompts.py, runner.py
│   ├── benchmark/        # runner.py, metrics.py, traces.py, result.py
│   ├── adapters/         # base.py, fastmcp.py, adk.py, official.py
│   ├── servers/          # fastmcp/, adk/, official/
│   ├── world/            # state.py, fixtures.py, reset.py
│   ├── extensions/       # apps/, skills/, tasks/, demo_extension/
│   └── evals/
├── tests/
│   ├── conformance/
│   ├── interoperability/
│   ├── failures/
│   └── regression/
├── datasets/             # basic.jsonl, composition.jsonl, failures.jsonl,
│                         # interactive.jsonl, long_running.jsonl, skills.jsonl, adversarial.jsonl
├── results/
│   └── .gitkeep
└── scripts/
    ├── bench.py
    ├── eval.py
    └── report.py
```

## 5. Shared benchmark world

Create a deterministic simulated enterprise environment. Do not benchmark against external APIs. Use an in-memory or SQLite-backed world with entities such as: employees, projects, tickets, documents, deployments, inventory.

Example state:
```
employees:
  alice: team: payments
  bob:   team: risk
tickets:
  PAY-123: title: Payment timeout, status: OPEN
deployments:
  checkout: version: "1.8.2", environment: production
inventory:
  macbook-pro: available: 0
```

Expose operations such as: `search_documents(query)`, `get_ticket(id)`, `create_ticket(…)`, `update_ticket(…)`, `find_employee(…)`, `get_inventory(…)`, `reserve_inventory(…)`, `get_deployment(…)`, `deploy_service(…)`.

All server implementations must operate on exactly the same world state.

## 6. Core MCP server

Implement the logically identical MCP server using each applicable implementation.

Expose:
- **Tools:** search_documents, get_ticket, create_ticket, update_ticket, get_inventory, reserve_inventory, deploy_service
- **Resources:** company://policies/deployment, company://inventory, ticket://{id}
- **Prompts:** incident-triage, onboarding, deployment-review

Ensure descriptions and JSON schemas are equivalent. Generate normalized manifests so differences can be inspected automatically.

## 7. Capability matrix

Automatically derive as much of the matrix as possible.

Columns: Capability | FastMCP | Google ADK | Official SDK | Protocol status | Evidence | Test.

Rows should include at least: Tool server; Tool client; Resources; Prompts; Structured tool output; Stdio; Streamable HTTP; SSE legacy support; 2026-07-28 protocol; Legacy protocol negotiation; Sampling; Elicitation; Multi-round-trip; Tasks; Cancellation; Progress; OAuth; Client credentials; Identity propagation; Extensions negotiation; MCP Apps; Skills over MCP; Custom extensions; Multi-server aggregation; Dynamic tools; Tool filtering; Schema generation; Context injection / DI; Lifecycle hooks; Session state; Stateless operation; Tracing; Testing utilities.

Every claim must link to (1) primary documentation AND (2) an executable test where reasonably possible. Documentation-only support must not be considered experimentally verified.

## 8. Deterministic protocol benchmark

Create protocol-level tests independent of the LLM. For each candidate test:

**DISCOVERY** — connect, discover capabilities, list tools, list resources, list prompts.
**SCHEMA** — primitive parameters, nested objects, enums, nullable values, unions, arrays, structured results.
**ERRORS** — invalid parameters, application exception, timeout, connection loss, malformed server response.
**CONCURRENCY** — 1 request, 10 concurrent requests, 100 concurrent requests.
**LIFECYCLE** — clean startup, clean shutdown, reconnect, server restart, client cancellation.
**INTEROPERABILITY** — Run all meaningful combinations:
- FastMCP client → FastMCP server
- FastMCP client → Official server
- Official client → FastMCP server
- ADK client → FastMCP server
- ADK client → Official server
- and any other supported combinations.

This is critical. A framework working against itself is weak evidence of interoperability.

## 9. Agent evaluation suite

Run the SAME LangGraph agent against each MCP variant. Create at least 40 deterministic tasks.

- **Category A — simple selection:** "What is the status of PAY-123?" → get_ticket(PAY-123)
- **Category B — semantic tool selection:** "Do we have any laptops available for a new engineer?" → get_inventory
- **Category C — multi-step composition:** "A new engineer is joining payments. Check laptop availability and create an onboarding ticket if none are available." → get_inventory → create_ticket
- **Category D — resources:** "Check our deployment policy and tell me whether checkout can be deployed." → read deployment resource → inspect deployment → answer
- **Category E — failure recovery:** inject tool timeout / invalid response / temporary failure; evaluate recovery
- **Category F — ambiguous intent:** "Deploy checkout." → expected: discover missing environment/version or trigger elicitation rather than guessing
- **Category G — interactive workflow:** production deployment requires human approval → agent → deployment tool → MCP elicitation/interactive request → approval → deployment continuation
- **Category H — long-running execution:** "Run the migration and report when complete." Simulate 10–30s operation. Evaluate: task creation, status, progress, completion, failure, cancellation.

## 10. Agent metrics

Record for every task: `task_success`, `correct_final_state`, `tool_selection_accuracy`, `tool_argument_accuracy`, `trajectory_correctness`, `unnecessary_tool_calls`, `tool_call_count`, `LLM_turn_count`, `MCP_round_trips`, `total_latency_ms`, `MCP_latency_ms`, `model_latency_ms`, `input_tokens`, `output_tokens`, `error_recovery_success`, `protocol_errors`, `user_interactions`.

Use deterministic graders whenever possible. Example: correct final state > textual answer. If the task says "Close PAY-123", verify `world.tickets["PAY-123"].status == CLOSED`. Do not use an LLM judge when state inspection is sufficient.

## 11. Trajectory evaluation

The benchmark must distinguish OUTCOME (did the task succeed?) from TRAJECTORY (did the agent use an acceptable execution path?). Example: agent eventually closes ticket — Outcome: PASS; but trajectory `search_documents → search_documents → create_ticket → get_ticket → update_ticket` could be inefficient or incorrect. Store both independently.

## 12. MCP Apps experiment

Implement an MCP App using the official MCP Apps extension. Create an example: `inventory_dashboard`. The MCP tool should return an interactive UI capable of: displaying inventory, selecting an item, reserving an item, refreshing state.

Build a minimal benchmark host capable of rendering or structurally validating the MCP App. Evaluate: extension negotiation, UI resource discovery, tool→UI association, HTML/resource delivery, UI→host messages, UI-initiated tool calls, security metadata, graceful fallback when client does not support MCP Apps.

Determine for each candidate: native support / manual support / interoperability / unsupported. Do NOT reduce MCP Apps to merely returning HTML text.

## 13. Skills over MCP experiment

Treat Skills over MCP separately from MCP Apps. First research the current state of: Skills over MCP working group, relevant SEPs, reference implementations, registry conventions, skill packaging/discovery conventions. Do not assume Skills over MCP has reached stable standardization. Document its maturity as: standard / official extension / draft SEP / experimental convention / ecosystem-specific — based on current primary sources.

Create a skill: `incident_triage`. It should contain rich agent instructions describing: (1) inspect ticket, (2) retrieve relevant operational policy, (3) identify owning team, (4) inspect deployment state, (5) produce recommendation. The skill must NOT simply become another function tool. Its purpose is to test distribution of reusable agent behavior/instructions over MCP.

## 14. Skills experiment

Compare:
- **A. No skill** — agent sees only raw tools.
- **B. Skill copied into system prompt manually.**
- **C. Skill discovered dynamically over MCP.**

Then run identical incident tasks. Measure: task success, trajectory quality, tool-selection accuracy, tokens, tool calls, latency, instruction-following, skill discovery overhead.

This asks a more interesting question than "Can this SDK return a skill?" The question is: **Does dynamic skill delivery through MCP materially improve agent behavior enough to justify the additional protocol abstraction?**

## 15. Progressive disclosure experiment

Skills should also test dynamic context loading. Compare: ALL INSTRUCTIONS UP FRONT vs. skill index → agent selects skill → skill instructions loaded → optional supporting resources loaded. Measure context consumption and agent quality. This tests MCP as a mechanism for capability discovery rather than simply RPC.

## 16. Extensions experiment

Build one deliberately small custom MCP extension: `io.mcpbench.audit`. Purpose: attach a deterministic audit record to sensitive operations. Example capability:

```
extensions:
  io.mcpbench.audit: {}
```

Experiment with: capability advertisement, negotiation, extension-specific metadata, extension-specific request/result, unsupported-client behavior. Implement it using the official extension facilities first. Then determine what is required in FastMCP and ADK. The goal is to compare extensibility costs.

## 17. Tasks experiment

Test the current MCP Tasks extension separately. Use: `generate_monthly_report()`. Behavior: call → task handle → running → progress → completed. Also test: cancellation, failure, reconnect during task, multiple concurrent tasks.

Important: check current SDK implementation status before coding. Distinguish: protocol specification supports feature vs. SDK currently implements feature vs. framework provides equivalent non-MCP abstraction.

## 18. Elicitation and multi-round-trip

Implement `reserve_inventory(item, employee)`: if employee is missing, server requests clarification. Implement `deploy_service(…)`: if production, server requests explicit approval. Evaluate whether the SDK lets developers model these naturally. Measure: application code, protocol-specific code, agent success, round trips, failure modes.

## 19. Authentication and identity

Create a local OAuth-like test environment. Represent: human → agent → MCP client → MCP server → backend.

Test whether identity and authorization context can propagate without exposing credentials to the model. Investigate: OAuth support, client credentials, ID-JAG / identity assertions if currently applicable, per-request metadata, dependency injection, scopes, authorization failures. Never send credentials into model-visible tool parameters. This experiment should evaluate architectural ergonomics, not cryptographic strength.

## 20. Multi-server experiment

Create three servers: `people-mcp`, `ticket-mcp`, `inventory-mcp`. Task: "Find Alice's open onboarding ticket and reserve a laptop for her." Requires people server → ticket server → inventory server.

Compare multi-server aggregation capabilities such as: FastMCP ClientGroup, ADK MCP toolsets, official SDK client composition, and any equivalents currently available. Measure: configuration complexity, connection lifecycle, tool-name collisions, provenance, routing, failure isolation.

## 21. Failure injection

Provide deterministic fault injection. Examples: `FAIL_TOOL_CALL=0.1`, `LATENCY_MS=500`, `DROP_CONNECTION_AFTER=3`, `MALFORMED_RESPONSE_RATE=0.05`, `TASK_FAILURE_RATE=0.1`. Run reliability experiments. Track: recovery probability, agent retries, duplicate side effects, incorrect final state. Explicitly investigate idempotency. An agent retrying a failed MCP call must not accidentally create two tickets.

## 22. Observability

Generate a normalized trace format. Example:

```json
{
  "run_id": "…",
  "sdk": "fastmcp",
  "task": "…",
  "events": [
    {"type": "model_call"},
    {"type": "mcp.discover"},
    {"type": "mcp.tool_call", "tool": "get_ticket"}
  ]
}
```

Capture: model activity, MCP requests, MCP responses, extension negotiation, tool execution, state mutation, latency, errors. Support exporting traces as JSONL.

## 23. Experimental methodology

Control these variables: same model, same prompts, same task order, same MCP schemas, same machine, same data, same world state, same timeout policy, same agent loop.

Run each stochastic agent task at least N = 10 where practical. Use confidence intervals for aggregate results. Do not over-interpret differences smaller than model variance.

## 24. Developer-experience experiment

Implement each equivalent server independently. Measure: source lines of code, number of MCP-specific imports, explicit lifecycle code, custom serialization code, custom auth code, extension code, test code, installation dependencies. Then perform qualitative review: abstraction quality, discoverability, type safety, debuggability, escape hatches, protocol transparency. Do not turn subjective impressions into fake numeric precision. Use prose where measurement is inappropriate.

## 25. Expected output reports

Generate:
- `results/latest/summary.json`
- `results/latest/capabilities.json`
- `results/latest/agent-evals.json`
- `results/latest/performance.json`
- `results/latest/interoperability.json`
- `results/latest/dx.json`
- and `docs/findings.md`

## 26. CLI

Provide:

```
uv run mcpbench capabilities
uv run mcpbench conformance
uv run mcpbench interoperability
uv run mcpbench eval
uv run mcpbench eval -sdk fastmcp
uv run mcpbench eval -sdk official
uv run mcpbench eval -sdk adk
uv run mcpbench extensions
uv run mcpbench apps
uv run mcpbench skills
uv run mcpbench tasks
uv run mcpbench benchmark
uv run mcpbench report
```

## 27. Report structure

The generated report must use:
1. **Executive synthesis** — what actually differs enough to matter?
2. **Protocol coverage** — which MCP capabilities are implemented?
3. **Agent outcomes** — does SDK choice affect agent behavior?
4. **Developer ergonomics** — how much complexity does each abstraction remove?
5. **Interoperability** — can independently implemented clients and servers actually communicate?
6. **Extensions** — how well does each implementation handle the new MCP extension model?
7. **MCP Apps** — how practical is MCP as an agent UI transport?
8. **Skills over MCP** — does protocol-delivered procedural knowledge improve agent behavior?
9. **Tasks and interactive execution** — is MCP viable for workflows beyond simple request/response tools?
10. **Operational characteristics** — latency, concurrency, lifecycle and failure recovery.
11. **Trade-offs** — FastMCP vs ADK vs official SDK.
12. **Decision heuristics** — "Choose FastMCP when …", "Choose the official SDK when …", "Use ADK's MCP integration when …", "Use MCP extensions when …", "Avoid MCP for …". Do not declare an overall winner unless the evidence genuinely supports one.

## 28. Important research discipline

Before implementation, inspect the CURRENT versions of: FastMCP, Google ADK, official MCP Python SDK, MCP specification, MCP Apps extension, Tasks extension, Skills over MCP work, relevant SEPs. Do not rely on model training knowledge for current APIs.

Record versions in `results/environment.json`. Example:
```json
{"date": "…", "python": "…", "fastmcp": "…", "google-adk": "…", "mcp": "…", "protocol": "2026-07-28"}
```
Pin dependencies after confirming current releases.

## 29. Proven vs emerging vs speculative

Explicitly classify every feature:
- **PROVEN** — part of stable protocol + stable SDK implementation + test passes.
- **IMPLEMENTED BUT OPTIONAL** — available but not universal.
- **EMERGING** — official extension, experimental implementation, or recent capability.
- **EXPERIMENTAL** — working-group proposal / draft SEP / ecosystem convention.
- **SPECULATIVE** — architecture explored by this project rather than standardized MCP behavior.

This distinction is particularly important for Skills over MCP.

## 30. Anti-goals

Do NOT: build a generic MCP tutorial; compare only API aesthetics; benchmark only tools/call; use different agents for each SDK; claim ADK is equivalent to a general-purpose MCP SDK; use an LLM judge where deterministic validation is possible; treat latency differences under model noise as important; assume documentation claims equal interoperability; assume a feature is stable because an implementation exposes it; assume Skills over MCP is standardized without checking; hide unsupported features behind compatibility adapters; declare one framework the winner based on LOC alone.

## 31. Key hypotheses

Test rather than assume:

- **H1:** FastMCP substantially reduces application-level MCP boilerplate versus the official SDK.
- **H2:** The official SDK exposes new protocol primitives earlier and with less abstraction loss.
- **H3:** ADK provides an easier MCP experience when MCP is merely a tool source for an ADK agent, but is less suitable as a general MCP protocol framework.
- **H4:** For basic tool calling, SDK choice has little measurable impact on agent success.
- **H5:** Differences become more significant for interactive execution, identity, tasks, extensions and multi-server composition.
- **H6:** MCP Apps demonstrates that MCP can transport application interaction rather than only tools.
- **H7:** Skills over MCP can reduce initial context size through progressive capability disclosure but introduces discovery complexity.
- **H8:** Most practical differences between MCP frameworks are socio-technical — abstraction level, debugging, lifecycle management and team cognitive load — rather than raw runtime performance.

Allow the experiments to falsify every hypothesis.

## 32. First implementation milestone

Do not implement every experimental extension immediately.

**Milestone 1** must produce a complete vertical slice:
Three comparable MCP integrations → Shared deterministic world → 5 tools, 1 resource, 1 prompt → LangGraph agent → 10 evaluation tasks → deterministic state graders → normalized traces → comparison report.

Only after this works add:
- **Milestone 2:** failure injection + interoperability
- **Milestone 3:** elicitation + multi-round-trip + tasks
- **Milestone 4:** MCP Apps + extensions
- **Milestone 5:** Skills over MCP + progressive disclosure

This keeps the research falsifiable and prevents the project from becoming an MCP feature showcase before the benchmark itself works.

## 33. Definition of done

The PoC is successful when a developer can run:

```
uv sync
uv run mcpbench benchmark
uv run mcpbench report
```

and obtain empirical evidence answering:
- What does each SDK support?
- What does it make easier?
- What abstraction does it impose?
- Where does interoperability break?
- Does the choice change agent success?
- How well do modern MCP features work?
- Do MCP Apps change MCP's role from tool protocol toward application protocol?
- Do Skills over MCP provide measurable value?
- Which capabilities are production-ready versus emerging?
- Under what circumstances should an experienced engineering team choose each approach?

The value of the project is not identifying a universal winner. The value is establishing an evidence-based model of the trade-offs between MCP abstractions.
