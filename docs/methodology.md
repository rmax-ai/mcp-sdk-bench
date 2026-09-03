# METHODOLOGY — mcp-sdk-bench

Experimental methodology for the MCP SDK benchmark. Defines the research questions (SPEC.md §1), the experiment taxonomy, controlled-variable discipline (§2–§3, §23), agent-evaluation and grading design (§9–§11), statistical treatment (§23), provenance classification (§29), hypotheses with falsification criteria (§31), anti-goals as constraints (§30), and validity threats. Version/API specifics are deliberately absent: they are Phase 1 outputs from primary sources (§28), recorded in `results/environment.json` and `docs/research/`.

## 1. Research questions (SPEC.md §1)

- **Q1 — Protocol capability.** Which MCP protocol features can each implementation actually express? §1(a)–(d) are distinct claims: (a) the protocol feature exists, (b) the library exposes it, (c) the library makes it ergonomic, (d) the library interoperates correctly with other implementations. Each claim type is evidenced separately; the §8 conformance/interop suites address (b)/(d), DX measurement (§24) addresses (c), and capability-matrix cells (§7) record the protocol status per feature.
- **Q2 — Developer experience.** How much application code and MCP-specific knowledge is required? Measured objectively where possible — lines of application code, configuration, boilerplate, type annotations, custom protocol handling, error-handling code, lifecycle management, extension plumbing, testing complexity (§1-Q2, §24) — with qualitative review in prose where numeric measurement would be fake precision (§24).
- **Q3 — Runtime characteristics.** Startup time, connection establishment, discovery latency, tool-call latency, concurrency, repeated calls, memory, reconnect, serialization overhead, failure recovery, long-running operations (§1-Q3). Purposefully not an artificial microbenchmark contest: only differences large enough to matter architecturally are reported (§1-Q3, and the §30 anti-goal on latency under model noise).
- **Q4 — Agent behavior.** Whether implementation choice affects an agent's ability to discover/select tools, supply valid parameters, compose tools, recover from errors, elicit, run long tasks, use resources/prompts, navigate multiple servers, consume skills, and use MCP Apps (§1-Q4). Measured as behavioral outcomes via the §9–§11 pipeline, not protocol correctness alone.
- **Q5 — Extensibility.** Whether MCP works as an extensible substrate (Extensions, MCP Apps, Tasks, Skills over MCP, custom extensions), probing the boundary between core protocol → extensions → agent capabilities → application-specific conventions (§1-Q5, §12–§20).

## 2. Experiment taxonomy

Six experiment lanes. Each lane answers a different claim type, uses different evidence, and reports into its own artifact (§25):

1. **Deterministic protocol tests** (§8) — LLM-independent conformance suites: DISCOVERY, SCHEMA, ERRORS, CONCURRENCY (1/10/100), LIFECYCLE. Support Q1(b)/(d) and Q3 primitives. No stochastic variance expected; repeated runs are cheap.
2. **Interoperability matrix** (§8) — all meaningful cross-implementation client↔server pairings (FastMCP↔FastMCP, FastMCP→Official, Official→FastMCP, ADK→FastMCP, ADK→Official, plus supported others). Critical by design: self-pairing is weak evidence (§8). Unsupportable pairings receive honest labels, not silent omission (§7).
3. **Agent evaluations** (§9–§11) — the same LangGraph agent over ≥40 deterministic tasks in categories A–H; stochastic (model-dependent); the Q4 evidence base.
4. **Runtime characteristics** (Q3, §1) — measured both directly in lane 1 and by trace latency decomposition (`total_latency_ms` = `MCP_latency_ms` + `model_latency_ms`, §10, §22) on agent runs. Reported as relative deltas between SDKs, never as portable absolute numbers (host: 2 cores, ~4 GiB).
5. **Developer-experience measurement** (§24) — per-SDK independent implementation of the equivalent server; counts of source LOC, MCP-specific imports, explicit lifecycle code, custom serialization/auth code, extension code, test code, install dependencies; plus qualitative review of abstraction quality, discoverability, type safety, debuggability, escape hatches, protocol transparency, in prose.
6. **Extension experiments** (§12–§20) — MCP Apps (§12), Skills over MCP + progressive disclosure (§13–§15), custom extensions (§16), Tasks (§17), elicitation/multi-round-trip (§18), auth/identity (§19), multi-server composition (§20). Separate capability lanes by design (architecture.md §8); each carries an honest label and provenance class.

## 3. Controlled variables and the single-independent-variable rule (SPEC.md §2, §3, §23)

The independent variable is the MCP integration under test — nothing else. Controlled variables (SPEC.md §23): same model, same prompts, same task order, same MCP schemas, same machine, same data, same world state, same timeout policy, same agent loop. The agent implementation, prompt, model, task dataset, retry policy, and context budget are identical between comparable experiments (§2). The model is pinned by provider/name/key configuration with the same temperature, prompt, and tool descriptions across runs; model comparison is out of scope (§3).

Operational rules derived from this:

- World state and task datasets are reset deterministically per run; task order is fixed across SDKs so order effects are identical (§23).
- SDK runs are interleaved rather than blocked by SDK, so machine drift and model-provider drift are spread evenly across candidates rather than confounded with candidate.
- Where a candidate genuinely cannot express something the experiment requires, the cell is labeled (partially supported / through another abstraction / unsupported / not applicable per the §7 scale and §29 provenance) — the experiment may degrade to a capability experiment, but the controlled-variable set is never silently violated by substituting a different agent, prompt, or abstraction for one candidate (§30).

## 4. Agent evaluation design (SPEC.md §9–§11)

**Task set.** ≥40 deterministic tasks across categories A–H (A simple selection, B semantic tool selection, C multi-step composition, D resources, E failure recovery, F ambiguous intent, G interactive workflow, H long-running 10–30 s execution) stored as JSONL datasets with stable task IDs (§4, §9). Each task encodes its success condition as an expected final world state where possible.

**Deterministic grading rule (SPEC.md §10).** Graders inspect the shared world state: correct final state outranks textual answer ("Close PAY-123" ⇒ `world.tickets["PAY-123"].status == CLOSED`). An LLM judge is used only when state inspection is genuinely insufficient — never where deterministic validation is possible (§30).

**Outcome vs trajectory (SPEC.md §11).** The two are recorded independently. An agent that closes the ticket (OUTCOME: PASS) via a wasteful or incorrect path (`search_documents → search_documents → create_ticket → get_ticket → update_ticket`) is scored separately on trajectory. Outcome metrics and trajectory metrics therefore never collapse into one number.

**Full metric set per task (SPEC.md §10):** `task_success`, `correct_final_state`, `tool_selection_accuracy`, `tool_argument_accuracy`, `trajectory_correctness`, `unnecessary_tool_calls`, `tool_call_count`, `LLM_turn_count`, `MCP_round_trips`, `total_latency_ms`, `MCP_latency_ms`, `model_latency_ms`, `input_tokens`, `output_tokens`, `error_recovery_success`, `protocol_errors`, `user_interactions`. Trajectory correctness and the latency split derive from the normalized §22 trace; per-category aggregates (not just grand means) are reported because hypotheses H4–H5 predict category-dependent effects (§31).

## 5. Statistical treatment (SPEC.md §23)

- Each stochastic agent task runs at least **N = 10** where practical, per SDK. Upper bound: 40 tasks × 3 SDKs × 10 ≈ **1,200+ agent runs**; the actual feasible N is a function of model availability and cost and is recorded per task, with the achieved N published rather than implied.
- Aggregate results carry **confidence intervals**. Differences smaller than model variance are not interpreted (§23): the benchmark measures the variance floor on a held-out repeated-task set and reports it alongside any cross-SDK delta, so a reader can see when a difference is sub-variance.
- Deterministic lanes (conformance, interoperability) need no N=10 sampling; they are executed per the pairing/concurrency matrix and their variance is environmental (machine noise), handled by reporting relative deltas (AGENTS.md machine constraint).
- Model nondeterminism is a controlled noise source, not a signal: the same model version and temperature are pinned (§3); the SDK is the only systematic difference between paired runs.

## 6. Provenance classification (SPEC.md §29)

Every feature claim is classified on the §29 scale: **PROVEN** (stable protocol + stable SDK implementation + test passes), **IMPLEMENTED BUT OPTIONAL** (available but not universal), **EMERGING** (official extension, experimental implementation, or recent capability), **EXPERIMENTAL** (working-group proposal / draft SEP / ecosystem convention), **SPECULATIVE** (architecture explored by this project, not standardized MCP). The classification is mandatory where maturity is contested — most pointedly Skills over MCP, whose standardization status is researched from primary sources before any experiment is built (§13, §28), and the spec/SDK/framework three-way distinction for Tasks (§17: protocol supports vs SDK implements vs framework provides a non-MCP equivalent). Capability-matrix columns carry Protocol status + Evidence (doc link + test) per row (§7); documentation-only support is not experimentally verified (§7).

## 7. Hypotheses and falsification criteria (SPEC.md §31)

Each hypothesis is tested with an assigned measurement and a pre-stated falsification condition; every hypothesis is allowed to fail (§31):

- **H1 — FastMCP substantially reduces application-level boilerplate versus the official SDK.** Measurement: §24 DX counts (LOC, MCP-specific imports, lifecycle/serialization/auth code) for the equivalent server implementations. Falsified if the official-SDK server is within the same order of magnitude on the objective counts and the qualitative review does not corroborate a substantial difference.
- **H2 — The official SDK exposes new protocol primitives earlier and with less abstraction loss.** Measurement: capability matrix (§7) + extension lanes (§12–§20), comparing first-class exposure vs re-implementation cost, with Phase 1 release-timeline evidence. Falsified if new-primitive support appears in FastMCP/ADK as early and with comparable fidelity.
- **H3 — ADK gives an easier MCP experience as a tool source for an ADK agent, but is weaker as a general MCP protocol framework.** Measurement: DX review of ADK server + client, capability matrix coverage beyond tools/call. Falsified if ADK matches the general-purpose SDKs across protocol features (or fails to show ergonomic advantage in its own tool-source role).
- **H4 — For basic tool calling, SDK choice has little measurable impact on agent success.** Measurement: Category A–C success/state/accuracy metrics. Falsified if cross-SDK success deltas exceed the measured model-variance floor (§23) on simple tasks.
- **H5 — Differences grow for interactive execution, identity, tasks, extensions, multi-server composition.** Measurement: categories F–H + §17–§20 lanes vs A–C baselines. Falsified if category D–H deltas are no larger than A–C deltas.
- **H6 — MCP Apps demonstrates MCP transporting application interaction, not only tools.** Measurement: §12 host validation (negotiation, UI discovery, tool→UI association, messages, UI-initiated calls, fallback). Falsified if the app reduces to returning HTML text with no interactive protocol substrate (§12's explicit warning).
- **H7 — Skills over MCP reduce initial context size via progressive disclosure but add discovery complexity.** Measurement: §14 (no-skill vs prompt-copied vs dynamic) and §15 (all-instructions vs staged) on tokens, success, trajectory, overhead. Falsified if staged delivery shows no context saving, or shows context saving with net task-quality loss exceeding its benefit.
- **H8 — Most practical differences are socio-technical (abstraction level, debugging, lifecycle management, team cognitive load), not raw runtime performance.** Measurement: §24 qualitative review + Q3 relative deltas. Falsified if runtime deltas between SDKs are architecturally significant (order-of-magnitude class) across the board, or if qualitative review finds no consistent socio-technical differentiation.

## 8. Anti-goals as enforced constraints (SPEC.md §30)

The §30 anti-goals are converted into review gates that block report publication:

1. No generic MCP tutorial content in deliverables.
2. No comparisons of API aesthetics alone — DX claims need the §24 measurements.
3. No tools/call-only benchmarking — §8 suites and §12–§20 lanes are mandatory.
4. No different agent per SDK — one LangGraph agent (§2).
5. ADK is never claimed equivalent to a general-purpose MCP SDK (preamble, §30).
6. No LLM judge where deterministic validation is possible (§10, §30).
7. Latency differences under model noise are never treated as important (§23, §30).
8. Documentation claims are never treated as interoperability (§7, §30).
9. A feature exposed by an implementation is never assumed stable (§29, §30).
10. Skills over MCP is never assumed standardized without primary-source checking (§13, §30).
11. Unsupported features are never hidden behind compatibility adapters (§2, §30).
12. No winner declared on LOC alone; no overall winner at all unless evidence genuinely supports one (§27-12, §33).

## 9. Validity threats and limitations

- **Machine constraint.** The benchmark host has 2 cores and ~4 GiB RAM. Absolute latency numbers are machine-specific and are reported only as relative deltas between SDKs run on the same host under the same conditions; absolute values are never published as portable claims (AGENTS.md conventions).
- **Model availability and cost.** N=10 × ≥40 tasks × 3 SDKs ≈ 1,200+ agent runs places a real budget/rate-limit ceiling on the stochastic lanes. Mitigations: deterministic lanes dominate the cheap evidence; if N=10 is infeasible for some tasks, the achieved N is published and CIs widened rather than tasks silently dropped; model and temperature stay pinned (§3).
- **Model nondeterminism vs SDK effects.** The core confound risk of Q4. Mitigated by the single-independent-variable rule (§2, §23), the measured variance floor, and per-category reporting (H4/H5 structure).
- **Grading validity.** Deterministic state inspection is only as good as the state assertions. Task success conditions are specified as world-state predicates at dataset-authoring time; graders themselves are covered by `tests/regression/` so a grader change cannot silently move scores.
- **Interop coverage limits.** The pairing matrix is bounded by what client surfaces each SDK actually ships; Phase 1 research gates the matrix, and absent pairings are labeled, so "no evidence" is never reported as "does not work" (§7 honesty scale, §8).
- **Harness overfitting.** The common adapter (§2, L3) risks encoding one SDK's model of MCP. The conformance suite and the cross-implementation matrix are the countermeasure: if the harness only works against the SDK it was written against, interop tests fail loudly.
- **External validity.** One agent framework (LangGraph), one model provider abstraction, one synthetic enterprise world. Findings are about *these* abstractions under *these* conditions; Q1's (a)–(d) claim separation exists precisely so readers can tell what generalizes (protocol facts) from what is conditional (ergonomics, behavior).
- **DX measurement subjectivity.** §24 mixes objective counts with qualitative review; the review is deliberately prose, and subjective impressions are never dressed up as numeric precision (§24).
- **Honest-capability-labeling rule (preamble, §7).** The six-value scale is the last line of defense against false symmetry; a matrix row that cannot be filled with doc + test evidence is marked with its true status and provenance class (§29), not completed by assumption.
