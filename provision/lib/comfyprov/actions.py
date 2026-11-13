"""Plan and action model shared by Python (producer) and bash (executor).

Python decides *what* to do; the shell executes a small, fixed vocabulary of
actions. The wire format between them is a line protocol (tab separated,
values base64 encoded) so bash needs no JSON parser and no jq.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, TextIO

# Exit codes used by every command. `all` and the launcher react to them.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_REBOOT = 3
EXIT_PREREQ = 4
EXIT_VERIFY = 5

STATUS_OK = "ok"
STATUS_PENDING = "pending"
STATUS_REBOOT = "needs-reboot"
STATUS_BLOCKED = "blocked"

EFFECT_NOW = "now"
EFFECT_SESSION = "next-session"
EFFECT_BOOT = "next-boot"

KINDS = frozenset(
    {
        "mkdir",
        "write_file",
        "append_line",
        "symlink",
        "exec",
        "apt_install",
        "usermod_groups",
        "git_checkout",
        "pip_install",
        "download",
    }
)

SEPARATOR = "\x1f"


@dataclass(frozen=True)
class Action:
    """One idempotent unit of work."""

    id: str
    kind: str
    summary: str
    params: Mapping[str, Any] = field(default_factory=dict)
    satisfied: bool = False
    sudo: bool = False
    effect: str = EFFECT_NOW

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown action kind: {self.kind}")
        if self.effect not in (EFFECT_NOW, EFFECT_SESSION, EFFECT_BOOT):
            raise ValueError(f"unknown action effect: {self.effect}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "summary": self.summary,
            "params": dict(self.params),
            "satisfied": self.satisfied,
            "sudo": self.sudo,
            "effect": self.effect,
        }


@dataclass
class Plan:
    """Ordered actions for one provisioning step plus its overall status."""

    step: str
    status: str = STATUS_OK
    actions: list[Action] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    exit_code: int = EXIT_OK

    # -- building ------------------------------------------------------------
    def add(self, action: Action) -> Action:
        self.actions.append(action)
        return action

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def block(self, message: str, code: int = EXIT_PREREQ) -> Plan:
        self.status = STATUS_BLOCKED
        self.exit_code = code
        self.note(message)
        return self

    def need_reboot(self, message: str) -> Plan:
        self.status = STATUS_REBOOT
        self.exit_code = EXIT_REBOOT
        self.note(message)
        return self

    # -- querying ------------------------------------------------------------
    @property
    def pending(self) -> list[Action]:
        return [action for action in self.actions if not action.satisfied]

    @property
    def changed(self) -> bool:
        return bool(self.pending)

    def finalize(self) -> Plan:
        """Derive the status from the actions when nothing else set it."""
        if self.status in (STATUS_BLOCKED, STATUS_REBOOT):
            return self
        if self.changed:
            if self.status == STATUS_OK:
                self.status = STATUS_PENDING
        elif self.status == STATUS_PENDING:
            self.status = STATUS_OK
        if self.status == STATUS_BLOCKED:
            self.exit_code = EXIT_PREREQ
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "status": self.status,
            "exit_code": self.exit_code,
            "notes": list(self.notes),
            "actions": [action.to_dict() for action in self.actions],
        }

    def describe(self) -> str:
        lines = [f"{self.step}: {self.status}"]
        lines.extend(f"  note: {note}" for note in self.notes)
        for action in self.actions:
            marker = "ok  " if action.satisfied else "todo"
            lines.append(f"  {marker} {action.kind:<14} {action.summary}")
        return "\n".join(lines)


def _b64(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _encode(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (list, tuple)):
        return SEPARATOR.join("" if item is None else str(item) for item in value)
    if value is None:
        return ""
    return str(value)


def emit_stream(plan: Plan, out: TextIO) -> None:
    """Write the plan in the line protocol consumed by ``apply_plan`` in bash.

    ``ACT`` fields: index, kind, sudo, effect, id, summary(base64).
    ``PARAM`` fields: index, name, value(base64); lists are joined by U+001F.
    """
    out.write(f"PLAN\t{plan.status}\t{plan.exit_code}\t{plan.step}\n")
    for note in plan.notes:
        out.write(f"NOTE\t{_b64(note)}\n")
    for index, action in enumerate(plan.actions):
        out.write(
            "ACT\t{idx}\t{kind}\t{sudo}\t{effect}\t{id}\t{summary}\t{satisfied}\n".format(
                idx=index,
                kind=action.kind,
                sudo="1" if action.sudo else "0",
                effect=action.effect,
                id=action.id,
                summary=_b64(action.summary),
                satisfied="1" if action.satisfied else "0",
            )
        )
        for name, value in action.params.items():
            out.write(f"PARAM\t{index}\t{name}\t{_b64(_encode(value))}\n")
    out.write("END\n")


def merge(plans: Iterable[Plan], step: str = "all") -> Plan:
    """Combine per-step plans, keeping the most severe status."""
    combined = Plan(step=step)
    severity = {STATUS_OK: 0, STATUS_PENDING: 1, STATUS_REBOOT: 2, STATUS_BLOCKED: 3}
    for plan in plans:
        combined.actions.extend(plan.actions)
        for note in plan.notes:
            combined.note(note)
        if severity[plan.status] > severity[combined.status]:
            combined.status = plan.status
        combined.exit_code = max(combined.exit_code, plan.exit_code)
    return combined.finalize()
