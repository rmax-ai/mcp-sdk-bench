# Phase 1 Findings — Current MCP Landscape (verified 2026-09-03)

> All claims verified against primary sources on 2026-09-03: PyPI release metadata, installed package internals (probed via `scripts/probe_api.py`), and the modelcontextprotocol/specification repository. URLs are primary sources. Rerun `uv run python scripts/probe_api.py` after any bump to re-verify.

## 1. Pinned versions (live PyPI + installed probes)

| Package | Version | Uploaded | Notes |
|---|---|---|---|
| mcp (official SDK v2) | **2.1.1** | 2026-08-25 | `LATEST_PROTOCOL_VERSION = "2026-07-28"` (probed) |
| fastmcp | **4.0.2** | 2026-09-02 | metapackage → `fastmcp-slim==4.0.2` |
| google-adk | **2.8.0** | 2026-08-26 | MCP needs `google-adk[mcp]` extra |
| langgraph | 1.2.11 | 2026-08-11 | benchmark agent runtime |
| langchain-core | 1.6.1 | 2026-08-27 | |
| ruff / ty / pytest | 0.16.6 / 0.0.78 / 9.1.1 | | dev gates |

## 2. Protocol state

- Stable revisions: 2024-11-05, 2025-03-26, 2025-06-18, 2025-11-25, **2026-07-28** (latest) + `draft` — https://github.com/modelcontextprotocol/specification/tree/main/docs/specification
- 2026-07-28 restructured core into `architecture / basic / client / server` (+ `changelog.mdx`, `deprecated.mdx`, `schema.mdx`).
- Official SDK exposes `LATEST_PROTOCOL_VERSION="2026-07-28"`; `SUPPORTED_PROTOCOL_VERSIONS` attr absent (negotiation surface lives elsewhere — verify in M2 interop tests).

## 3. Tasks — SEP-1686 (Final, historical) → SEP-2663 `io.modelcontextprotocol/tasks` (Final, Extensions Track)

- SEP-1686 status: "preserved as a historical record of the experimental tasks feature shipped in the 2025-11-25 specification… The draft specification moves tasks out of the core protocol and into the io.modelcontextprotocol/tasks extension (SEP-2663)". https://github.com/modelcontextprotocol/specification/blob/main/seps/1686-tasks.md
- SEP-2663: Final, Extensions Track, Agents Working Group. https://github.com/modelcontextprotocol/specification/blob/main/seps/2663-tasks-extension.md
- **SDK status (probed):** official `mcp` 2.1.1 ships full Task type surface (`ListTasks/GetTask/CancelTask/GetTaskPayload` + `TaskStatusNotification` + capability flags). FastMCP: `FastMCP(tasks=...)` param + `fastmcp-tasks` extra (`fastmcp[tasks]`). ADK: no MCP Tasks surface probed — its native async-task abstraction exists (`LongRunningFunctionTool`-style); classify in M3 (spec-supports / SDK-implements / framework-native table, SPEC §17).

## 4. Elicitation & multi-round-trip — Final

- SEP-1330 (elicitation enum schemas, Final): https://github.com/modelcontextprotocol/specification/blob/main/seps/1330-elicitation-enum-schema-improvements-and-standards.md
- SEP-1036 (URL-mode elicitation, secure out-of-band interaction) present in seps/.
- SEP-2322 (Multi Round-Trip Requests, Final): https://github.com/modelcontextprotocol/specification/blob/main/seps/2322-MRTR.md
- **SDK status (probed):** official SDK: `ElicitRequest` (Form/URL params), `ElicitResult`, `ElicitCompleteNotification`, capability flags. FastMCP Client: `elicitation_handler`, `input_required_max_rounds=10`; server ctx has elicit handling. ADK: not probed — M3.

## 5. Sampling — Final (+tools)

- SEP-1577 (sampling-with-tools) in seps/. Official SDK: `SamplingCapability`, `SamplingMessage`, `SamplingToolsCapability`, `SamplingContextCapability` (probed). FastMCP Client: `sampling_handler`, `sampling_capabilities` (probed).

## 6. Extensions — SEP-2133 (Final, Standards Track)

- https://github.com/modelcontextprotocol/specification/blob/main/seps/2133-extensions.md
- **SDK status (probed):** official SDK: no dedicated `mcp.extensions` module — capability/negotiation via generic dict surfaces (verify in M4). FastMCP: `fastmcp.server.extensions` module EXISTS + `Client(extensions=...)` + `FastMCP(experimental_capabilities=...)` — first-class support (probed). ADK: not applicable as general MCP framework; M4 experiment.

## 7. MCP Apps — SEP-1865 (Final, Extensions Track)

- https://github.com/modelcontextprotocol/specification/blob/main/seps/1865-mcp-apps-interactive-user-interfaces-for-mcp.md — spec docs: docs/extensions/apps/{overview,build}.mdx
- **SDK status (probed):** official SDK 2.1.1: NO App-typed surface in `mcp.types` (probed). FastMCP: `fastmcp[apps]` extra → `prefab-ui>=0.18` (UI renderer). ADK: none. M4 experiment decides actual capability per candidate.

## 8. Skills over MCP — SEP-2640 (DRAFT, Extensions Track) — classification: EXPERIMENTAL

- SEP-2640 "Skills Extension", id `io.modelcontextprotocol/skills`, Status **Draft**, created 2026-04-23, Skills Over MCP Working Group. https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/seps/2640-skills-extension.md
- Charter: https://modelcontextprotocol.io/community/working-groups/skills-over-mcp
- Design: skill directories served as resources under `skill://` URIs; `SKILL.md` + frontmatter; `skill://index.json` index; format delegated to Agent Skills spec (https://agentskills.io/specification). Direction is evolving toward `skills/list` + `skills/get` RPCs (Aug 2026 WG notes mention `skills/list` carrying `ttlMs`/`cacheScope` as of protocol 2026-07-28).
- Governance history: declined by core maintainers on 2026-06-24 (archives, local code execution, index.json vs skills/list, governance) → scope-down; as of 2026-08-11 the core-maintainer vote is the remaining gate; OpenAI adopted the draft in plugins (install-a-snapshot). Reference impls: https://github.com/modelcontextprotocol/experimental-ext-skills
- **Benchmark consequence:** treat Skills over MCP as EXPERIMENTAL per SPEC §29. Implement the incident_triage skill against the draft's resource-based model; expect SDK support to be absent/partial everywhere (M5).

## 9. Auth & identity

- SEPs: 1046 (OAuth client credentials flow), 2468 (issuer claim), 2207 (OIDC refresh guidance), 1024 (client security requirements for local servers) — all in seps/.
- **SDK status (probed):** official SDK ships `mcp.server.auth` + `mcp.client.auth` (OAuth provider/client). FastMCP: `FastMCP(auth=AuthProvider)`, Client `auth=httpx2.Auth | Literal['oauth']`. ADK: client-side only (connection params, no MCP auth layer probed).
- **Reusable prior art:** `rmax-ai/mcp-auth-test-server` — OAuth 1.0a/2.0(2L/3L)/2.1/Bearer endpoints for testing MCP clients (Python, created 2026-05-20). The §19 experiment should build on it rather than reinvent.

## 10. Dependency universes — THREE separate environments required (architectural constraint)

Verified by install + import probes:

| Environment | Packages | mcp constraint |
|---|---|---|
| official | mcp 2.1.1 (+langgraph harness) | mcp-types inside mcp 2.1.1 |
| fastmcp | fastmcp-slim 4.0.2 (+`fastmcp[apps]`, `fastmcp[tasks]` when needed) | `mcp-types>=2.0.0,<3.0.0` — **no full SDK dependency** (decoupled runtime) |
| adk | google-adk 2.8.0 + `google-adk[mcp]` extra | `mcp>=1.24,<2` — **v1 SDK, incompatible with mcp 2.x** (verified: `google.adk.tools.mcp_tool.mcp_toolset` imports `mcp.shared.session`, which does not exist in 2.1.1) |

Consequences for architecture:
- The adapter boundary becomes a **process boundary**: server variants run as subprocesses in their own uv environments; the LangGraph harness + official client live in the main env. Interop matrix pairs (SPEC §8) are then trivially real cross-implementation pairings (e.g. FastMCP client in fastmcp env ↔ official server subprocess).
- FastMCP's decoupling is itself a finding: FastMCP 4 is protocol-compatible via `mcp-types` but owns its transport/session runtime.

## 11. ADK 2.8 MCP surface (installed-package verified)

- Real path: `google.adk.tools.mcp_tool.mcp_toolset` — class **`McpToolset`** (old name `MCPToolset` is a deprecated alias emitting DeprecationWarning).
- Config supports stdio / SSE / streamable-HTTP connection params + `tool_filter: Optional[List[str]]` (name-based filtering).
- Adjacent modules: `mcp_tool` (McpTool), `mcp_session_manager`, `conversion_utils`, `session_context`; `google.adk.agents.mcp_instruction_provider` (server instructions → agent); `google.adk.tools.load_mcp_resource_tool` (MCP resources surfaced as ADK tools — resources are NOT first-class in ADK, they become tools); `google.adk.tools._remote_mcp_server` (ADK agent hosted AS an MCP server — server-side path exists).
- Interpretation: ADK is an **MCP client for tool consumption** (+ resource-via-tool shim + agent-as-server hosting), not a general MCP SDK. Consistent with SPEC §H3.

## 12. Prior art in rmax-ai (reuse candidates)

- `rmax-ai/mcp-conformance` — scenario-driven conformance test runner for MCP client/server (Python). → reuse for conformance layer (SPEC §8).
- `rmax-ai/mcph` — MCP-Hurl declarative conformance DSL. → alternative/interop test authoring.
- `rmax-ai/mcp-auth-test-server` — OAuth test endpoints. → §19 auth experiment.

## 13. Python pinning

- `uv init` resolved the venv to **CPython 3.14.5** (uv-managed). Benchmark reproducibility requires a pin: `.python-version = 3.13` (matches system 3.13.5, all candidates support ≥3.10). Note in DECISIONS.md.

## 14. Open items carried into milestones

- M2: negotiation/back-compat behavior (which protocol version each SDK *sends*), transport matrix, interop pairings.
- M3: per-candidate Tasks/Elicitation/MRTR implementation status (spec vs SDK vs framework-native, SPEC §17).
- M4: Apps support per candidate (official SDK shows no App types — expect manual support only); custom extension costs.
- M5: Skills draft conformance; auth ergonomics on mcp-auth-test-server.
- Model provider: decide at M1 (OpenAI-compatible; budget ≈ 1,200 agent runs at N=10 × 40 tasks × 3 SDKs — choose the cheapest capable model for main runs, verify on a stronger model only for a smoke subset).
