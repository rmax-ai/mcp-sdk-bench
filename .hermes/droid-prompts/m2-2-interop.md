IMPLEMENT the following changes in the mcp-sdk-bench repo. Write code now and run verification at the end. Do not produce a research report. Do not ask questions.

WORKING DIRECTORY: /home/rmax-10/src/rmax-ai/mcp-sdk-bench
PYTHON: 3.12, uv-managed. NEVER edit pyproject.toml or uv.lock. No new dependencies.

TASK: Milestone 2.2 — the cross-implementation interoperability matrix (SPEC.md §8 INTEROPERABILITY). Closes issue #24.

CONTEXT: M1 shipped three server variants implementing one identical contract (tools get_ticket, update_ticket, get_inventory, reserve_inventory, deploy_service, probe_schema; resource deployment-policy; prompt incident-triage). M2.1 added the conformance suite and tests/conformance/helpers.py (session factories + a stdio proxy with corrupt/delay/drop modes). Self-pairings are already covered by M2.1; this story runs the MEANINGFUL cross pairings, each with a real client from one SDK against a real server from another SDK:

1. FastMCP client → FastMCP server (self-pair baseline)
2. FastMCP client → official server
3. official client → FastMCP server
4. ADK client → FastMCP server
5. ADK client → official server

BEFORE CODING, verify current installed API shapes from the repo venv (AGENTS.md rule 1): fastmcp.Client constructor/transport options (stdio transport support in FastMCP 4.0.2 — check how to point it at a subprocess command), mcp.client.stdio for the official client, and ADK's McpToolset connection parameters (command/args/url forms, which SDK it embeds under the hood — M1 finding: ADK pins mcp 1.x). Record the exact constructor shapes you use in module docstrings.

CHANGES:

FILE 1: tests/interoperability/test_pairings.py
A parametrized matrix over the 5 pairings above. For each pairing, in a fresh subprocess world:
- connect and initialize: capture protocolVersion in BOTH directions where the client surface exposes it (client-announced version, server-accepted version)
- discovery: list tools returns the 6-tool contract; resources and prompts present for the two non-ADK servers
- round-trip: get_ticket("PAY-123") returns the seeded ticket; probe_schema echo round-trips one primitive and one nested field
- teardown: clean close, server subprocess exits
Pairings that fail must FAIL WITH CLASSIFICATION: each failure case asserts the observed behavior and carries a message classifying it as (a) SDK defect with repro steps, or (b) version-negotiation failure (e.g. ADK's embedded mcp 1.x client against a protocol-2026-07-28 server) documented with the wire evidence. If a pairing cannot even connect, write the test to capture that as a documented xfail-with-evidence (pytest.mark.xfail with reason), never a silent skip. Do NOT weaken assertions to make pairings pass — the deliverable is honest wire-level evidence.

FILE 2: tests/interoperability/test_version_negotiation.py
Wire-level protocol version observation using the logging proxy from tests/conformance/helpers.py (extend it there with a "log" mode that passes frames through untouched while recording every JSON-RPC frame to a list, if M2.1's proxy does not already support recording). For pairings 2 and 3 (the two real cross-SDK wire paths), assert: the initialize REQUEST contains a protocolVersion, the initialize RESPONSE contains a protocolVersion, and record which side picked which version. Assert the client actually uses the negotiated version (no hardcoded assumption). Failures classify as defect vs negotiation.

FILE 3: src/mcp_sdk_bench/benchmark/interop.py
A runner module `run_interop()` that executes the 5 pairings end-to-end (reusing the pairing primitives from FILE 1 — factor the per-pairing logic so tests and runner share it) and returns a list of pairing result dicts: {pairing, client_sdk, server_sdk, connected: bool, protocol_version_client, protocol_version_server, discovery_ok: bool, tools_seen: int, resources_seen: int, prompts_seen: int, roundtrip_ok: bool, error: str|null, classification: "pass"|"sdk_defect"|"negotiation_failure"|"harness_limitation"}. Write results/latest/interoperability.json from it (results/latest/ is regenerated per run; results/ is gitignored except results/environment.json — do not commit the json).

FILE 4: src/mcp_sdk_bench/benchmark/cli.py
Add an `interop` subcommand to the existing typer CLI that calls run_interop() and prints the per-pairing table (rows = pairings, columns = connected / versions / discovery / roundtrip / classification). Follow the existing subcommand style in this file exactly.

FILE 5: docs/capability-matrix.md
Add an Interoperability section: table of the 5 pairings with pass/fail status and one-line wire-level explanation per non-pass cell. Mark all cells as run-verified (link to the test module) — docs-only claims are not allowed (AGENTS.md rule 5).

STYLE: mirror existing async test style. Deterministic only. No LLM calls. No network beyond local subprocesses.

AFTER writing all files, run these verification commands in order and fix failures until all pass:
1. uv run ruff check src/ tests/
2. uv run ty check
3. uv run pytest -q (full suite must stay green, including all M2.1 tests)

Then commit with message "M2.2: interoperability matrix + version negotiation evidence" including ONLY the files you changed, and push with `git push origin main`.
