"""Uniform reporting for checks, preflight and verification."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

OK = "ok"
WARN = "warn"
FAIL = "fail"

_MARKERS = {OK: "  OK", WARN: "WARN", FAIL: "FAIL"}
_SEVERITY = {OK: 0, WARN: 1, FAIL: 2}


@dataclass(frozen=True)
class Finding:
    level: str
    name: str
    detail: str = ""

    def render(self) -> str:
        suffix = f" ({self.detail})" if self.detail else ""
        return f"{_MARKERS[self.level]} {self.name}{suffix}"

    def to_dict(self) -> dict:
        return {"level": self.level, "name": self.name, "detail": self.detail}


def render(findings: Iterable[Finding]) -> str:
    return "\n".join(item.render() for item in findings)


def worst(findings: Iterable[Finding]) -> str:
    level = OK
    for item in findings:
        if _SEVERITY[item.level] > _SEVERITY[level]:
            level = item.level
    return level


def has_failures(findings: Iterable[Finding]) -> bool:
    return worst(findings) == FAIL


def counts(findings: Iterable[Finding]) -> dict[str, int]:
    summary = {OK: 0, WARN: 0, FAIL: 0}
    for item in findings:
        summary[item.level] += 1
    return summary
