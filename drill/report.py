"""Report phase: turn the list of PhaseResults from a drill run into a
durable, structured artifact -- both Markdown (for humans, audit
attachments) and JSON (for machine consumption, dashboards, alerting).

This is the part of the tool that makes the drill's execution itself the
audit trail: operator identity, timestamps, parameters, and per-phase
pass/fail are captured automatically as a by-product of running the tool,
rather than depending on someone remembering to document the run
afterward.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

from drill.models import PhaseResult, PhaseStatus


@dataclass
class DrillReport:
    """The top-level report object. Assembled by cli.py once all phases
    (or as many as ran before a gate stopped the drill) have completed.
    """

    drill_name: str
    environment: str
    namespace: str
    operator: str
    started_at: str
    finished_at: str
    parameters: dict
    phases: list[PhaseResult] = field(default_factory=list)

    @property
    def overall_status(self) -> str:
        if not self.phases:
            return "incomplete"
        if any(p.status is PhaseStatus.FAILED for p in self.phases):
            return "failed"
        if any(p.status is PhaseStatus.SKIPPED for p in self.phases):
            # A drill with any skipped phase (due to an earlier failure)
            # cannot be called an unqualified success even if nothing
            # that *did* run failed outright.
            return "incomplete"
        return "passed"

    def to_dict(self) -> dict:
        return {
            "drill_name": self.drill_name,
            "environment": self.environment,
            "namespace": self.namespace,
            "operator": self.operator,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "overall_status": self.overall_status,
            "parameters": self.parameters,
            "phases": [p.to_dict() for p in self.phases],
        }


_STATUS_ICON = {
    PhaseStatus.PASSED: "PASS",
    PhaseStatus.FAILED: "FAIL",
    PhaseStatus.SKIPPED: "SKIP",
}


def render_markdown(report: DrillReport) -> str:
    lines: list[str] = []
    lines.append(f"# DR Drill Report: {report.drill_name}")
    lines.append("")
    lines.append(f"**Overall result:** `{report.overall_status.upper()}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **Environment:** {report.environment}")
    lines.append(f"- **Namespace:** {report.namespace}")
    lines.append(f"- **Operator:** {report.operator}")
    lines.append(f"- **Started:** {report.started_at}")
    lines.append(f"- **Finished:** {report.finished_at}")
    lines.append("")
    lines.append("## Parameters")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(report.parameters, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("## Phases")
    lines.append("")
    lines.append("| Phase | Status | Duration (s) | Reason |")
    lines.append("|---|---|---|---|")
    for p in report.phases:
        reason = p.reason.replace("|", "\\|") if p.reason else "-"
        lines.append(
            f"| {p.phase} | {_STATUS_ICON[p.status]} | {p.duration_seconds:.2f} | {reason} |"
        )
    lines.append("")

    for p in report.phases:
        if not p.checks:
            continue
        lines.append(f"### {p.phase} — checks")
        lines.append("")
        lines.append("| Check | Result | Detail |")
        lines.append("|---|---|---|")
        for c in p.checks:
            lines.append(f"| {c.name} | {'PASS' if c.passed else 'FAIL'} | {c.detail} |")
        lines.append("")

    return "\n".join(lines)


def write_report(report: DrillReport, output_dir: str) -> tuple[str, str]:
    """Write both the Markdown and JSON artifacts to output_dir, creating
    it if necessary. Returns (markdown_path, json_path).
    """
    os.makedirs(output_dir, exist_ok=True)

    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = f"drill-report-{ts}"
    md_path = os.path.join(output_dir, f"{base}.md")
    json_path = os.path.join(output_dir, f"{base}.json")

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(report))

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2)

    return md_path, json_path
