"""Shared data structures used across all drill phases.

Every phase module (preflight, healthcheck, failover, validate) returns a
PhaseResult so that report.py can render a uniform table regardless of
what the phase actually did internally. Keeping this in one place avoids
each phase re-inventing its own ad hoc result shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PhaseStatus(str, Enum):
    """Outcome of a single drill phase."""

    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class CheckResult:
    """A single named assertion made during a phase (e.g. one deployment's
    replica count, one endpoint's reachability). Phases aggregate a list of
    these into their overall PhaseResult so the report can show granular
    detail, not just a single pass/fail bit per phase.
    """

    name: str
    passed: bool
    detail: str = ""


@dataclass
class PhaseResult:
    """The outcome of one drill phase (preflight / healthcheck / failover /
    validate). `status` is the authoritative pass/fail/skip signal; `checks`
    holds the individual assertions that fed into it and `reason` carries a
    short human-readable explanation, especially useful when status is
    FAILED or SKIPPED.
    """

    phase: str
    status: PhaseStatus
    started_at: str
    finished_at: str
    duration_seconds: float
    reason: str = ""
    checks: list[CheckResult] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status is PhaseStatus.PASSED

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "status": self.status.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 3),
            "reason": self.reason,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail} for c in self.checks
            ],
            "data": self.data,
        }


def skipped_result(phase: str, reason: str, now: str) -> PhaseResult:
    """Convenience constructor for a phase that never ran because an
    upstream phase failed. Keeps the "gating" behavior (see cli.py) from
    having to hand-build a PhaseResult every time.
    """
    return PhaseResult(
        phase=phase,
        status=PhaseStatus.SKIPPED,
        started_at=now,
        finished_at=now,
        duration_seconds=0.0,
        reason=reason,
    )
