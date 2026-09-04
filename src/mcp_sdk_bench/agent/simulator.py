"""Deterministic user simulator (SPEC.md §18, M3.1).

The harness-side stand-in for the human in elicitation (category G) and
ambiguous-intent (category F) tasks. It is policy-scripted PER TASK (the
dataset row's ``user_simulator_policy``), never model-driven, so the
multi-round-trip experiments stay reproducible (SPEC.md §23).

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
    """

    async def answer(self, kind: str, question: str, schema: dict) -> str | dict: ...

    async def clarify(self, task_prompt: str) -> str | None: ...


_AUTO_APPROVE = "auto-approve"
_AUTO_DECLINE = "auto-decline"
_CLARIFY_WITH = "clarify-with:"


class ScriptedUserSimulator:
    """Policy-scripted deterministic simulator (see module docstring)."""

    def __init__(self, policy: str | None = None) -> None:
        self.policy = policy or "none"
        if not (
            self.policy in ("none", _AUTO_APPROVE, _AUTO_DECLINE)
            or self.policy.startswith(_CLARIFY_WITH)
        ):
            raise ValueError(
                f"unknown user_simulator_policy {policy!r} "
                f"(expected none | auto-approve | auto-decline | clarify-with:<value>)"
            )

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
