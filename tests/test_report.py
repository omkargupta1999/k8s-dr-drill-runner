"""Unit tests for drill.report: markdown/JSON rendering and overall status
aggregation. No Kubernetes client involved here at all.
"""

from __future__ import annotations

import json
import os

from drill.models import CheckResult, PhaseResult, PhaseStatus
from drill.report import DrillReport, render_markdown, write_report


def _phase(name: str, status: PhaseStatus, reason: str = "", checks=None) -> PhaseResult:
    return PhaseResult(
        phase=name,
        status=status,
        started_at="2026-08-18T10:00:00+00:00",
        finished_at="2026-08-18T10:00:05+00:00",
        duration_seconds=5.0,
        reason=reason,
        checks=checks or [],
    )


def _report(phases) -> DrillReport:
    return DrillReport(
        drill_name="test-drill",
        environment="staging",
        namespace="app-primary",
        operator="test-operator",
        started_at="2026-08-18T10:00:00+00:00",
        finished_at="2026-08-18T10:05:00+00:00",
        parameters={"dry_run": False},
        phases=phases,
    )


class TestOverallStatus:
    def test_all_passed_is_passed(self):
        report = _report(
            [_phase("preflight", PhaseStatus.PASSED), _phase("healthcheck", PhaseStatus.PASSED)]
        )
        assert report.overall_status == "passed"

    def test_any_failed_is_failed(self):
        report = _report(
            [_phase("preflight", PhaseStatus.PASSED), _phase("healthcheck", PhaseStatus.FAILED)]
        )
        assert report.overall_status == "failed"

    def test_failed_takes_priority_over_skipped(self):
        report = _report(
            [_phase("preflight", PhaseStatus.FAILED), _phase("healthcheck", PhaseStatus.SKIPPED)]
        )
        # preflight failed AND healthcheck skipped -> failed takes priority
        assert report.overall_status == "failed"

    def test_skipped_only_is_incomplete(self):
        report = _report(
            [_phase("preflight", PhaseStatus.PASSED), _phase("healthcheck", PhaseStatus.SKIPPED)]
        )
        assert report.overall_status == "incomplete"

    def test_no_phases_is_incomplete(self):
        report = _report([])
        assert report.overall_status == "incomplete"


class TestRenderMarkdown:
    def test_contains_key_sections(self):
        checks = [CheckResult(name="api_reachable", passed=True, detail="ok")]
        report = _report([_phase("preflight", PhaseStatus.PASSED, checks=checks)])
        md = render_markdown(report)

        assert "# DR Drill Report: test-drill" in md
        assert "PASSED" in md
        assert "test-operator" in md
        assert "| preflight | PASS |" in md
        assert "api_reachable" in md

    def test_failed_phase_shows_reason(self):
        report = _report([_phase("preflight", PhaseStatus.FAILED, reason="node not ready")])
        md = render_markdown(report)
        assert "node not ready" in md
        assert "FAIL" in md


class TestWriteReport:
    def test_writes_both_artifacts(self, tmp_path):
        report = _report([_phase("preflight", PhaseStatus.PASSED)])
        output_dir = str(tmp_path / "reports")

        md_path, json_path = write_report(report, output_dir)

        assert os.path.exists(md_path)
        assert os.path.exists(json_path)

        with open(json_path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["drill_name"] == "test-drill"
        assert data["overall_status"] == "passed"
        assert len(data["phases"]) == 1

    def test_creates_output_dir_if_missing(self, tmp_path):
        report = _report([_phase("preflight", PhaseStatus.PASSED)])
        output_dir = str(tmp_path / "does" / "not" / "exist")

        md_path, json_path = write_report(report, output_dir)

        assert os.path.isdir(output_dir)
        assert os.path.exists(md_path)
        assert os.path.exists(json_path)
