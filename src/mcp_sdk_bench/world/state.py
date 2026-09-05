"""Deterministic shared benchmark world — SPEC.md §5.

Single source of truth for all server variants and all graders.
Rules:
- No wall-clock anywhere in state; mutation ordering is a monotonic counter.
  (M3.2 tasks: asyncio sleeps pace the ticker — runtime pacing, not state
  ordering; wire timestamps are synthesized from the op counter, never
  wall-clock.)
- All mutations go through World methods so op_log is complete and
  graders can verify correct_final_state without LLM judging.
- Fixture data lives in fixtures.py; reset.py rebuilds from seed.

M3.2 (SPEC.md §17 Tasks): the world owns the report-task registry —
``generate_monthly_report`` starts a simulated long-running report task with
deterministic progress ticks (0.0, 0.2, ..., 1.0), a status enum
(queued/running/completed/failed/cancelled), a 2-concurrent-task limit, and
fault integration: ``FaultEngine.task_failure()`` is drawn ONCE at task start
and, when it fires, the task fails at its first progress tick with the
canonical ``INJECTED_TASK_FAILURE`` message (an ASYNCHRONOUS mid-task failure
— the synchronous per-call fault layer in the servers deliberately does not
wrap the task tools; see the server module docstrings). Registry records are
ordinary World fields, so they persist with the world store
(MCP_BENCH_WORLD_STATE_FILE, see reset.py) and survive a server-process
restart; the asyncio runners are process-local and are re-spawned lazily by
whichever task method runs first on the new process (``_ensure_task_runners``).

Notification seam (mirrors the elicitation seam): the WORLD owns the policy
(a task notifies the session that started it — or re-bound it via an opt-in
poll — and only when that client opted in); the SERVER VARIANT owns the
protocol mechanics behind ``set_task_update_hook`` (official: wire
``notifications/progress`` + ``notifications/tasks/status``; fastmcp/adk: no
hook, app-level polling only)."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, PrivateAttr


class TicketStatus(str, Enum):
    OPEN = "OPEN"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    CLOSED = "CLOSED"


class DeploymentStatus(str, Enum):
    ACTIVE = "active"
    ROLLING_OUT = "rolling_out"
    FAILED = "failed"


class Employee(BaseModel):
    id: str
    name: str
    team: str
    title: str = ""


class Ticket(BaseModel):
    id: str
    title: str
    status: TicketStatus
    team: str = ""
    assignee: str | None = None
    description: str = ""
    priority: str | None = None
    #: Set only for tickets created via create_ticket (SPEC.md §21
    #: idempotency); seeded fixture tickets have no key.
    idempotency_key: str | None = None


class Document(BaseModel):
    id: str
    title: str
    body: str
    tags: list[str] = Field(default_factory=list)


class Deployment(BaseModel):
    service: str
    version: str
    environment: str
    status: DeploymentStatus = DeploymentStatus.ACTIVE


class InventoryItem(BaseModel):
    name: str
    available: int = 0
    reserved_by: list[str] = Field(default_factory=list)


class Project(BaseModel):
    id: str
    name: str
    team: str


class OpRecord(BaseModel):
    seq: int
    op: str
    entity: str
    entity_id: str
    payload: dict[str, Any] = Field(default_factory=dict)


class WorldError(Exception):
    """Domain error raised by world mutations (mapped to MCP errors by servers)."""


class ElicitationUnavailable(Exception):
    """Raised by a server's elicit fn when the CLIENT has no elicitation
    capability (M3.1, SPEC.md §18). The world catches it and falls back to
    its legacy no-channel policy, so capability-free clients keep the
    pre-M3.1 behavior exactly (honest degradation, not a faked answer)."""


# ---- elicitation seam (SPEC.md §18, M3.1) ----

#: The world's elicitation callback contract. The WORLD owns the policy
#: (which situations require clarification/approval and what the answer
#: means); the SERVER VARIANT owns the protocol mechanics behind the
#: callback (official SDK: imperative ``elicitation/create`` via
#: ``ServerSession.elicit_form``; FastMCP 4 on 2026-07-28: the SEP-2322
#: ``InputRequiredResult`` guard pattern). The callback receives the
#: normalized request ``{kind, question, schema}`` and returns the
#: normalized response ``{status: approved|declined|clarified, answer: ...}``.
#:
#: The seam methods are async precisely so this measurement stays honest:
#: the application code (this module) is identical across SDK variants and
#: only the callback — the protocol-specific code — differs.
ElicitFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

#: Kinds of elicitation the world can request.
ELICIT_CLARIFICATION = "clarification"
ELICIT_APPROVAL = "approval"

#: Canonical decline message for a rejected production-deployment approval.
DEPLOYMENT_DECLINED = "deployment declined by user"


# ---- report task registry (SPEC.md §17, M3.2) ----

#: Canonical world statuses for a report task. The MCP wire Task.status
#: vocabulary (working/input_required/completed/failed/cancelled) has no
#: queued/running split; server variants map queued+running to "working" and
#: carry the world status in statusMessage (see servers/official/server.py).
class ReportTaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Terminal states: a task in one of these never ticks again and cannot be
#: cancelled.
REPORT_TASK_TERMINAL: frozenset[ReportTaskStatus] = frozenset(
    {ReportTaskStatus.COMPLETED, ReportTaskStatus.FAILED, ReportTaskStatus.CANCELLED}
)

#: Progress steps of the simulated monthly-report task (SPEC.md §17): ticks
#: advance 0.2 at a time until 1.0 == completed.
REPORT_TASK_PROGRESS_STEPS: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8, 1.0)

#: Default seconds between progress ticks. Tests and the CLI override via
#: MCP_BENCH_TASK_TICK_S to keep the hermetic suite fast; the nominal
#: 15-second report of SPEC.md §17 maps to the default pacing.
DEFAULT_TASK_TICK_S = 2.0

#: The registry supports exactly two concurrently active (queued/running)
#: tasks; a third start is rejected with WorldError.
MAX_ACTIVE_REPORT_TASKS = 2

#: Deterministic row count of the simulated report.
REPORT_ROWS = 1200

#: Fixed TTL (ms) stamped on wire Task objects. The mcp 2.1.1 wire types
#: REQUIRE the ttl key (no default) and the session dump strips None, so a
#: real value must be sent; deterministic, wall-clock-free.
REPORT_TASK_TTL_MS = 3_600_000


def task_timestamp(seq: int) -> str:
    """Deterministic synthetic timestamp derived from the op counter — the
    world has no wall clock, so wire createdAt/lastUpdatedAt values are
    functions of mutation order, identical across runs and SDKs."""
    return (datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=seq)).strftime("%Y-%m-%dT%H:%M:%SZ")


class ReportTask(BaseModel):
    """One registered report task (persisted with the world store)."""

    handle: str
    status: ReportTaskStatus
    progress: float = 0.0
    result: dict[str, Any] | None = None
    error: str | None = None
    #: Number of progress ticks already applied (resume point after a
    #: server-process restart).
    tick: int = 0
    #: Drawn once at start from FaultEngine.task_failure(); when true the task
    #: fails at its first progress tick.
    will_fail: bool = False
    #: Whether the client that started (or re-bound) this task opted into
    #: server-pushed notifications. Persisted so the policy survives restart;
    #: the session it refers to is process-local (_task_sessions).
    notify_opt_in: bool = False
    created_seq: int
    updated_seq: int


class ReportTaskView(BaseModel):
    """The SDK-agnostic task view shared by all three server variants'
    structured output and mapped to adapters.base.TaskView."""

    handle: str
    status: str
    progress: float
    result: dict[str, Any] | None = None
    error: str | None = None


def report_task_view(task: ReportTask) -> ReportTaskView:
    return ReportTaskView(
        handle=task.handle,
        status=task.status.value,
        progress=task.progress,
        result=task.result,
        error=task.error,
    )


def load_task_tick_s(env: dict[str, str] | None = None) -> float:
    """Seconds between progress ticks (default DEFAULT_TASK_TICK_S). Read once
    at server startup from MCP_BENCH_TASK_TICK_S."""
    import os

    raw = (os.environ if env is None else env).get("MCP_BENCH_TASK_TICK_S")
    if raw is None or raw == "":
        return DEFAULT_TASK_TICK_S
    try:
        value = float(raw)
    except ValueError as err:
        raise ValueError(f"invalid value for MCP_BENCH_TASK_TICK_S: {raw!r}") from err
    if value <= 0:
        raise ValueError(f"MCP_BENCH_TASK_TICK_S must be > 0, got {raw!r}")
    return value


def clarification_payload(field: str, question: str) -> dict[str, Any]:
    """Normalized clarification request: ask the user for one string field."""
    return {
        "kind": ELICIT_CLARIFICATION,
        "question": question,
        "schema": {
            "type": "object",
            # Root "title" doubles as the machine-readable kind marker; the
            # restricted requestedSchema subset ignores unknown root keys,
            # so clients also recover kind from the schema shape.
            "title": ELICIT_CLARIFICATION,
            "properties": {field: {"type": "string", "title": field}},
            "required": [field],
        },
    }


def approval_payload(question: str) -> dict[str, Any]:
    """Normalized approval request: ask the user for a boolean decision."""
    return {
        "kind": ELICIT_APPROVAL,
        "question": question,
        "schema": {
            "type": "object",
            "title": ELICIT_APPROVAL,
            "properties": {"approved": {"type": "boolean", "title": "Approved"}},
            "required": ["approved"],
        },
    }


def requested_field(payload: dict[str, Any]) -> str:
    """The single required property of a clarification payload's schema."""
    required = (payload.get("schema") or {}).get("required") or []
    return str(required[0]) if required else "value"


def elicitation_response(
    action: str, content: dict[str, Any] | None, payload: dict[str, Any]
) -> dict[str, Any]:
    """Normalize a wire-level elicitation outcome (the MCP ElicitResult
    action/content shape) into the world seam's response dict.

    accept + approval payload -> approved iff content.approved is truthy;
    accept + clarification payload -> clarified with the requested field's
    value; decline/cancel -> declined (the world's policy maps that to a
    WorldError).
    """
    if action != "accept":
        return {"status": "declined"}
    data = content or {}
    if payload.get("kind") == ELICIT_APPROVAL:
        return {"status": "approved" if data.get("approved") else "declined"}
    return {"status": "clarified", "answer": data.get(requested_field(payload))}


# ---- schema probe (SPEC.md §8 SCHEMA) ----

#: Valid values for the probe's enum_field. Kept as data (not an Enum) so the
#: world method signature stays ``enum_field: str``; each server variant maps
#: this constraint onto its own schema mechanism.
PROBE_SCHEMA_ENUM_VALUES: tuple[str, str, str] = ("alpha", "beta", "gamma")


class ProbeNestedObject(BaseModel):
    """Nested object shape for probe_schema.nested_field."""

    id: str
    tags: list[str]


class ProbeNestedItem(BaseModel):
    """Array element shape for probe_schema.nested_list_field."""

    name: str
    count: int


class World(BaseModel):
    employees: dict[str, Employee] = Field(default_factory=dict)
    tickets: dict[str, Ticket] = Field(default_factory=dict)
    documents: dict[str, Document] = Field(default_factory=dict)
    deployments: dict[str, Deployment] = Field(default_factory=dict)
    inventory: dict[str, InventoryItem] = Field(default_factory=dict)
    projects: dict[str, Project] = Field(default_factory=dict)
    op_log: list[OpRecord] = Field(default_factory=list)
    #: idempotency_key -> ticket_id for tickets created via create_ticket
    #: (SPEC.md §21). Populated on first execution only; a retry with a known
    #: key is a replay and never creates a second ticket.
    ticket_idempotency_keys: dict[str, str] = Field(default_factory=dict)
    #: M3.2 report task registry (SPEC.md §17): handle -> task record.
    #: Ordinary world state, so it persists with the world store
    #: (MCP_BENCH_WORLD_STATE_FILE) and survives a server-process restart.
    report_tasks: dict[str, ReportTask] = Field(default_factory=dict)
    #: Monotonic handle counter (persisted; handles stay unique across
    #: restarts sharing one world store).
    report_task_seq: int = 0

    # ---- process-local task runtime (never serialized) ----
    _task_runners: dict[str, asyncio.Task[None]] = PrivateAttr(default_factory=dict)
    _task_update_hook: Callable[[ReportTask], Awaitable[None]] | None = PrivateAttr(None)
    _task_sessions: dict[str, Any] = PrivateAttr(default_factory=dict)
    _task_tick_s: float = PrivateAttr(DEFAULT_TASK_TICK_S)
    _state_file: Path | None = PrivateAttr(None)

    # ---- mutation surface (single implementation shared by all server variants) ----

    def _record(self, op: str, entity: str, entity_id: str, **payload: Any) -> None:
        self.op_log.append(
            OpRecord(seq=len(self.op_log), op=op, entity=entity, entity_id=entity_id, payload=payload)
        )

    def get_ticket(self, ticket_id: str) -> Ticket:
        if ticket_id not in self.tickets:
            raise WorldError(f"ticket {ticket_id} not found")
        return self.tickets[ticket_id]

    def create_ticket(
        self,
        ticket_id: str,
        title: str,
        priority: str | None = None,
        *,
        idempotency_key: str,
    ) -> Ticket:
        """Create a ticket with idempotency semantics (SPEC.md §21).

        If `idempotency_key` was already used in this world session, return
        the EXISTING ticket unchanged (no duplicate, no error, no op_log
        entry) — a retried call must never create two tickets. Otherwise
        create the ticket (status OPEN) and record the key.
        """
        if idempotency_key in self.ticket_idempotency_keys:
            return self.tickets[self.ticket_idempotency_keys[idempotency_key]]
        if ticket_id in self.tickets:
            raise WorldError(f"ticket {ticket_id} already exists")
        ticket = Ticket(
            id=ticket_id,
            title=title,
            status=TicketStatus.OPEN,
            priority=priority,
            idempotency_key=idempotency_key,
        )
        self.tickets[ticket_id] = ticket
        self.ticket_idempotency_keys[idempotency_key] = ticket_id
        self._record(
            "create_ticket",
            "ticket",
            ticket_id,
            title=title,
            priority=priority,
            idempotency_key=idempotency_key,
        )
        return ticket

    def ticket_for_idempotency_key(self, idempotency_key: str) -> Ticket | None:
        """Return the ticket previously created under `idempotency_key`, or
        None. This is how the fault layer and the tests tell a create_ticket
        call that DID execute (side effect applied, key recorded) from a
        rejected/replayed one."""
        ticket_id = self.ticket_idempotency_keys.get(idempotency_key)
        return self.tickets.get(ticket_id) if ticket_id is not None else None

    def update_ticket(
        self,
        ticket_id: str,
        status: TicketStatus | None = None,
        assignee: str | None = None,
    ) -> Ticket:
        ticket = self.get_ticket(ticket_id)
        if status is not None:
            ticket.status = status
        if assignee is not None:
            ticket.assignee = assignee
        self._record("update_ticket", "ticket", ticket_id, status=status, assignee=assignee)
        return ticket

    def get_inventory(self) -> dict[str, InventoryItem]:
        return dict(self.inventory)

    async def reserve_inventory(
        self,
        item: str,
        employee_id: str | None = None,
        *,
        elicit: ElicitFn | None = None,
    ) -> InventoryItem:
        """Reserve one unit of an item for an employee (SPEC.md §18).

        Clarification policy (world-owned): a missing employee_id requires
        asking the user. With an `elicit` callback the world requests a
        clarification and the answer becomes the employee id; a decline (or
        empty answer) raises WorldError. Without a callback the call raises
        on the missing employee — the pre-M3.1 behavior for a request the
        server cannot clarify.
        """
        if item not in self.inventory:
            raise WorldError(f"unknown inventory item {item}")
        if employee_id is None:
            if elicit is None:
                raise WorldError(f"reserve_inventory for {item} requires an employee id")
            try:
                response = await elicit(
                    clarification_payload(
                        "employee_id",
                        f"Which employee should the {item} reservation be recorded for?",
                    )
                )
            except ElicitationUnavailable:
                # Client cannot answer elicitations: legacy behavior.
                raise WorldError(
                    f"reserve_inventory for {item} requires an employee id"
                ) from None
            if response.get("status") != "clarified" or not response.get("answer"):
                raise WorldError("reservation declined by user")
            employee_id = str(response["answer"])
        if employee_id not in self.employees:
            raise WorldError(f"unknown employee {employee_id}")
        inv = self.inventory[item]
        if inv.available <= 0:
            raise WorldError(f"{item} has no available units")
        inv.available -= 1
        inv.reserved_by.append(employee_id)
        self._record("reserve_inventory", "inventory", item, employee_id=employee_id)
        return inv

    def get_deployment(self, service: str) -> Deployment:
        if service not in self.deployments:
            raise WorldError(f"unknown deployment {service}")
        return self.deployments[service]

    async def deploy_service(
        self,
        service: str,
        target_version: str,
        environment: str,
        *,
        elicit: ElicitFn | None = None,
    ) -> Deployment:
        """Deploy a service (SPEC.md §18 approval flow).

        Approval policy (world-owned): a production deploy requires explicit
        user approval when an elicitation channel exists; a decline raises
        WorldError(DEPLOYMENT_DECLINED) and the world is left unchanged.
        Without a callback the pre-M3.1 guard applies unchanged (production
        deploys of services not already in production are rejected).
        """
        dep = self.get_deployment(service)
        if environment == "production":
            if elicit is not None:
                try:
                    response = await elicit(
                        approval_payload(
                            f"Approve deployment of {service} {target_version} to production?"
                        )
                    )
                except ElicitationUnavailable:
                    # Client cannot answer elicitations: legacy guard below.
                    elicit = None
                else:
                    if response.get("status") != "approved":
                        raise WorldError(DEPLOYMENT_DECLINED)
            # Legacy pre-M3.1 guard (no elicitation channel): production
            # deploys of services not already in production are rejected.
            if elicit is None and dep.environment != environment:
                raise WorldError(f"{service} is not deployed in {environment}")
        dep.version = target_version
        dep.environment = environment
        dep.status = DeploymentStatus.ACTIVE
        self._record("deploy_service", "deployment", service, version=target_version, environment=environment)
        return dep

    def probe_schema(
        self,
        string_field: str,
        int_field: int,
        float_field: float,
        bool_field: bool,
        enum_field: str,
        nullable_field: str | None,
        union_field: str | int,
        list_field: list[str],
        nested_field: ProbeNestedObject,
        nested_list_field: list[ProbeNestedItem],
    ) -> dict[str, Any]:
        """Side-effect-free echo probe exercising every JSON-schema primitive
        the SPEC.md §8 SCHEMA category requires. NOT recorded in op_log (no
        mutation), so it is safe for concurrency bursts. Returns a canonical
        dict that must be identical across all three server variants."""
        if enum_field not in PROBE_SCHEMA_ENUM_VALUES:
            raise WorldError(
                f"enum_field must be one of {list(PROBE_SCHEMA_ENUM_VALUES)}; got {enum_field!r}"
            )
        received: dict[str, Any] = {
            "string_field": string_field,
            "int_field": int_field,
            "float_field": float_field,
            "bool_field": bool_field,
            "enum_field": enum_field,
            "nullable_field": nullable_field,
            "union_field": union_field,
            "list_field": list_field,
            "nested_field": nested_field.model_dump(mode="json"),
            "nested_list_field": [item.model_dump(mode="json") for item in nested_list_field],
        }
        return {"received": received, "count": len(received)}

    def search_documents(self, query: str) -> list[Document]:
        q = query.lower()
        return [d for d in self.documents.values() if q in d.title.lower() or q in d.body.lower()]

    def find_employee(self, employee_id: str) -> Employee:
        if employee_id not in self.employees:
            raise WorldError(f"unknown employee {employee_id}")
        return self.employees[employee_id]

    # ---- report task registry (SPEC.md §17, M3.2) ----

    def set_task_runtime(
        self,
        *,
        tick_s: float,
        update_hook: Callable[[ReportTask], Awaitable[None]] | None = None,
    ) -> None:
        """Per-process runtime configuration, called once by the server
        variant at startup. `update_hook` is the notification seam: invoked
        (awaited) after every task transition; None for variants without a
        server-pushed notification surface (fastmcp/adk)."""
        self._task_tick_s = tick_s
        self._task_update_hook = update_hook

    def set_state_file(self, path: Path | None) -> None:
        """Attach the world store file (reset.py; MCP_BENCH_WORLD_STATE_FILE).
        Task transitions persist the whole world there."""
        self._state_file = path

    def _persist(self) -> None:
        if self._state_file is not None:
            self._state_file.write_text(self.model_dump_json())

    def register_task_session(self, handle: str, session: Any) -> None:
        """Bind the notification target for `handle` to `session` and mark the
        task opted in (world policy: the client that started the task — or
        re-bound it with an opt-in poll — receives its notifications).

        Direct dict lookup, NOT get_report_task: this runs mid-start before
        the runner is registered, and _ensure_task_runners would spawn a
        duplicate runner for the not-yet-registered handle."""
        if handle not in self.report_tasks:
            raise WorldError(f"report task {handle} not found")
        self.report_tasks[handle].notify_opt_in = True
        self._task_sessions[handle] = session

    def task_session(self, handle: str) -> Any | None:
        return self._task_sessions.get(handle)

    def _ensure_task_runners(self) -> None:
        """Lazily (re)spawn asyncio runners for persisted queued/running
        tasks. Called from every task registry method, so a fresh server
        process sharing the world store resumes its tasks on the first
        task-related call — create_server() itself may run outside an event
        loop (fastmcp variant), hence lazy rather than at construction."""
        for task in self.report_tasks.values():
            if task.status not in REPORT_TASK_TERMINAL and (
                task.handle not in self._task_runners or self._task_runners[task.handle].done()
            ):
                self._task_runners[task.handle] = asyncio.create_task(
                    self._run_report_task(task.handle)
                )

    async def start_report_task(
        self, fault_engine: Any, *, session: Any | None = None
    ) -> ReportTask:
        """Start a simulated monthly-report task (SPEC.md §17).

        Draws ``fault_engine.task_failure()`` ONCE here (deterministic per
        seed); when it fires the task fails asynchronously at its first
        progress tick with the canonical injected-task-failure message. At
        most MAX_ACTIVE_REPORT_TASKS tasks may be active concurrently; a
        third start raises WorldError.
        """
        # Deferred import: faults.py imports this module (no import cycle).
        from mcp_sdk_bench.faults import INJECTED_TASK_FAILURE

        self._ensure_task_runners()
        active = [
            t for t in self.report_tasks.values() if t.status not in REPORT_TASK_TERMINAL
        ]
        if len(active) >= MAX_ACTIVE_REPORT_TASKS:
            raise WorldError(
                f"report task limit reached ({MAX_ACTIVE_REPORT_TASKS} concurrent tasks)"
            )
        will_fail = fault_engine.task_failure()
        self.report_task_seq += 1
        handle = f"report-{self.report_task_seq:03d}"
        task = ReportTask(
            handle=handle,
            status=ReportTaskStatus.QUEUED,
            will_fail=will_fail,
            created_seq=len(self.op_log),
            updated_seq=len(self.op_log),
        )
        self.report_tasks[handle] = task
        self._record_task(task, "start", injected_failure_message=INJECTED_TASK_FAILURE)
        # Spawn the runner BEFORE any other registry call so
        # _ensure_task_runners never double-spawns this handle.
        self._task_runners[handle] = asyncio.create_task(self._run_report_task(handle))
        if session is not None:
            self.register_task_session(handle, session)
        return task

    def get_report_task(self, handle: str) -> ReportTask:
        self._ensure_task_runners()
        if handle not in self.report_tasks:
            raise WorldError(f"report task {handle} not found")
        return self.report_tasks[handle]

    def list_report_tasks(self) -> list[ReportTask]:
        self._ensure_task_runners()
        return list(self.report_tasks.values())

    async def cancel_report_task(self, handle: str) -> ReportTask:
        """Cancel a queued/running task: the ticker stops, no result is ever
        produced. Cancelling a terminal task raises WorldError."""
        task = self.get_report_task(handle)
        if task.status in REPORT_TASK_TERMINAL:
            raise WorldError(f"report task {handle} is already {task.status.value}")
        task.status = ReportTaskStatus.CANCELLED
        runner = self._task_runners.pop(handle, None)
        if runner is not None:
            runner.cancel()
        self._record_task(task, "cancel")
        return task

    def _record_task(self, task: ReportTask, event: str, **extra: Any) -> None:
        task.updated_seq = len(self.op_log)
        self._record(
            f"report_task_{event}",
            "report_task",
            task.handle,
            status=task.status.value,
            progress=task.progress,
            tick=task.tick,
            **extra,
        )
        self._persist()

    async def _notify_task(self, task: ReportTask) -> None:
        hook = self._task_update_hook
        if hook is not None and task.notify_opt_in:
            await hook(task)

    async def _run_report_task(self, handle: str) -> None:
        """The ticker: QUEUED -> RUNNING, then one progress step per tick;
        completes at 1.0. A will_fail task fails at its FIRST progress tick
        (the M3.2 asynchronous injected failure). Cancellation is delivered
        by cancel_report_task, which sets the status before cancelling."""
        from mcp_sdk_bench.faults import INJECTED_TASK_FAILURE

        task = self.report_tasks[handle]
        try:
            if task.status == ReportTaskStatus.QUEUED:
                task.status = ReportTaskStatus.RUNNING
                self._record_task(task, "running")
                await self._notify_task(task)
            for step in REPORT_TASK_PROGRESS_STEPS[task.tick :]:
                await asyncio.sleep(self._task_tick_s)
                if task.will_fail and task.tick == 0:
                    task.status = ReportTaskStatus.FAILED
                    task.error = INJECTED_TASK_FAILURE
                    self._record_task(task, "fail")
                    await self._notify_task(task)
                    return
                task.tick += 1
                task.progress = step
                if step >= 1.0:
                    task.status = ReportTaskStatus.COMPLETED
                    task.result = {
                        "report_id": f"monthly-{handle}",
                        "rows": REPORT_ROWS,
                        "generated_at": task_timestamp(task.updated_seq),
                    }
                    self._record_task(task, "complete")
                else:
                    self._record_task(task, "tick")
                await self._notify_task(task)
        except asyncio.CancelledError:
            # cancel_report_task already set the terminal status.
            return
