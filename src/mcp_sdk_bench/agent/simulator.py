"""Deterministic user simulator (SPEC.md §18, M3.1; SPEC.md §9 H, M3.3).

The harness-side stand-in for the human in elicitation (category G),
ambiguous-intent (category F), and long-running (category H) tasks. It is
policy-scripted PER TASK (the dataset row's ``user_simulator_policy``),
never model-driven, so the multi-round-trip experiments stay reproducible
(SPEC.md §23).

Policies:
- ``none`` (default): no interaction. ``clarify`` returns None; ``answer``
  declines — the safe, non-fabricating default when a server elicits without
  a scripted user. All M1/M2 tasks run under this policy and are unchanged.
- ``auto-approve``: approvals are approved.
- ``auto-decline``: approvals are declined (the world then raises
  "deployment declined by user").
- ``clarify-with:<value>``: clarifications are answered with <value>, and
  the category-F pre-tool hook volunteers <value> as clarification text
  (e.g. "staging v1.7.0" for "Deploy checkout."). Approvals are approved
  (a cooperative user); mixed-policy tasks are not in the M3.1 dataset.
- ``cancel-at-progress:<float>`` (M3.3, SPEC.md §9 H): once the agent
  observes a tool result carrying migration progress >= <float> (fired AT
  MOST ONCE per task), the scripted user injects "Actually — cancel the
  migration." (the category-H cancel lane behind datasets/longrunning.jsonl
  h-02). The hook is ``observe_tool_result``, wired into the agent loop at
  the post-tool-result point exactly like the clarify hook.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class UserSimulator(Protocol):
    """The agent loop's user-side interface (SPEC.md §18).

    ``answer`` serves a server-initiated elicitation (the pause/resume path):
    given the normalized request, return the normalized response payload
    (``{status: approved|declined|clarified, answer: ...}``) or a plain
    string (a clarification answer, or an approval phrase).

    ``clarify`` serves the category-F hook: before the agent's first tool
    call, return clarification text to append as a user message, or None
    when the scripted user has nothing to add.

    ``observe_tool_result`` serves the category-H hook (M3.3): after each
    tool result the agent sees, return a user message to inject (e.g. the
    cancel-at-progress cancellation) or None.
    """

    async def answer(self, kind: str, question: str, schema: dict) -> str | dict: ...

    async def clarify(self, task_prompt: str) -> str | None: ...

    async def observe_tool_result(
        self, name: str, structured: dict | None, text: str | None
    ) -> str | None: ...


_AUTO_APPROVE = "auto-approve"
_AUTO_DECLINE = "auto-decline"
_CLARIFY_WITH = "clarify-with:"
_CANCEL_AT_PROGRESS = "cancel-at-progress:"

#: The injected cancellation message (deterministic h-02 lane).
CANCEL_MIGRATION_MESSAGE = "Actually — cancel the migration."


def _progress_from_structured(structured: dict | None) -> float | None:
    """Extract the migration progress carried by a task-tool result
    (structured content envelope {"task": {"progress": ...}}). Returns None
    when the result is not a task view."""
    if not isinstance(structured, dict):
        return None
    task = structured.get("task")
    if not isinstance(task, dict):
        return None
    progress = task.get("progress")
    return progress if isinstance(progress, (int, float)) else None


class ScriptedUserSimulator:
    """Policy-scripted deterministic simulator (see module docstring)."""

    def __init__(self, policy: str | None = None) -> None:
        self.policy = policy or "none"
        self.cancel_threshold: float | None = None
        self._cancel_fired = False
        if not (
            self.policy in ("none", _AUTO_APPROVE, _AUTO_DECLINE)
            or self.policy.startswith(_CLARIFY_WITH)
            or self.policy.startswith(_CANCEL_AT_PROGRESS)
        ):
            raise ValueError(
                f"unknown user_simulator_policy {policy!r} "
                f"(expected none | auto-approve | auto-decline | "
                f"clarify-with:<value> | cancel-at-progress:<float>)"
            )
        if self.policy.startswith(_CANCEL_AT_PROGRESS):
            raw = self.policy[len(_CANCEL_AT_PROGRESS):]
            try:
                threshold = float(raw)
            except ValueError as err:
                raise ValueError(
                    f"invalid cancel-at-progress threshold {raw!r} (expected a float)"
                ) from err
            if not 0.0 <= threshold <= 1.0:
                raise ValueError(
                    f"cancel-at-progress threshold must be within 0.0..1.0, got {raw!r}"
                )
            self.cancel_threshold = threshold

    @property
    def _clarify_value(self) -> str | None:
        if self.policy.startswith(_CLARIFY_WITH):
            return self.policy[len(_CLARIFY_WITH):]
        return None

    async def clarify(self, task_prompt: str) -> str | None:
        """Category-F hook: the scripted user volunteers the missing
        environment/version (or employee) unprompted when the policy carries
        one; otherwise None (the agent must ask or abstain on its own)."""
        return self._clarify_value

    async def observe_tool_result(
        self, name: str, structured: dict | None, text: str | None
    ) -> str | None:
        """Category-H hook (M3.3): under the cancel-at-progress policy, the
        first tool result whose migration progress reaches the threshold
        triggers the user's cancellation message (exactly once per task).
        Every other policy returns None (no injection)."""
        if self.cancel_threshold is None or self._cancel_fired:
            return None
        progress = _progress_from_structured(structured)
        if progress is None or progress < self.cancel_threshold:
            return None
        self._cancel_fired = True
        return CANCEL_MIGRATION_MESSAGE

    async def answer(self, kind: str, question: str, schema: dict) -> dict[str, Any]:
        """Answer one server-initiated elicitation per the scripted policy."""
        if kind == "approval":
            if self.policy == _AUTO_DECLINE or self.policy == "none":
                return {"status": "declined"}
            return {"status": "approved"}
        # clarification
        value = self._clarify_value
        if value is not None:
            return {"status": "clarified", "answer": value}
        if self.policy == _AUTO_APPROVE:
            # Cooperative but valueless: the policy carries no answer.
            return {"status": "clarified", "answer": "yes"}
        return {"status": "declined"}


def normalize_simulator_answer(request: dict, answer: str | dict) -> dict:
    """Normalize a UserSimulator.answer return value into the response
    payload dict the adapters expect. Dicts pass through; a plain string is
    a clarification answer, or an approval phrase for approval kinds.
    """
    if isinstance(answer, dict):
        return answer
    if request.get("kind") == "approval":
        affirmative = answer.strip().lower() in {"approve", "approved", "yes", "y"}
        return {"status": "approved" if affirmative else "declined"}
    return {"status": "clarified", "answer": answer}
