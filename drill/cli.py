"""Command-line entrypoint for the DR drill runner.

    python -m drill.cli run --config config/drill-config.yaml

Wires the five phases together in order, with each phase gating the next:
if preflight fails, healthcheck/failover/validate are all recorded as
skipped rather than attempted. This is the one place in the codebase that
knows about phase ordering -- each phase module itself has no idea what
runs before or after it.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime

import click
import yaml
from kubernetes import client as k8s_client
from rich.console import Console
from rich.table import Table

from drill.failover import run_failover
from drill.healthcheck import run_healthcheck
from drill.k8s_client import build_api_client, resolve_operator
from drill.models import PhaseStatus, skipped_result
from drill.preflight import run_preflight
from drill.report import DrillReport, write_report
from drill.validate import run_validate

console = Console()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _load_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _status_style(status: PhaseStatus) -> str:
    return {
        PhaseStatus.PASSED: "bold green",
        PhaseStatus.FAILED: "bold red",
        PhaseStatus.SKIPPED: "yellow",
    }[status]


@click.group()
@click.option("--verbose", is_flag=True, help="Enable debug logging.")
def cli(verbose: bool) -> None:
    """k8s-dr-drill-runner: an automated Kubernetes DR drill pipeline."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@cli.command()
@click.option("--config", "config_path", required=True, help="Path to drill-config.yaml")
@click.option(
    "--checks",
    "checks_path",
    default="config/checks.yaml",
    show_default=True,
    help="Path to checks.yaml",
)
@click.option("--operator", default=None, help="Operator identity to record in the report.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Run preflight and healthcheck only; never mutate cluster state.",
)
def run(config_path: str, checks_path: str, operator: str | None, dry_run: bool) -> None:
    """Run a full DR drill: preflight -> healthcheck -> failover -> validate -> report.

    Each phase gates the next: a failed phase causes all remaining phases
    to be recorded as skipped, and the drill still produces a report
    reflecting exactly how far it got.
    """
    config = _load_yaml(config_path)
    checks_config = _load_yaml(checks_path)

    kube_context = config.get("kube_context")
    namespace = config["namespace"]
    resolved_operator = (
        operator or config.get("operator") or resolve_operator(operator, kube_context)
    )

    drill_started = _now()
    console.print(f"[bold]Starting drill:[/bold] {config.get('drill_name', 'unnamed-drill')}")
    console.print(
        f"  namespace={namespace} environment={config.get('environment')} "
        f"operator={resolved_operator} dry_run={dry_run}"
    )

    try:
        api_client = build_api_client(kube_context)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]Could not build a Kubernetes client:[/bold red] {exc}")
        sys.exit(2)

    core_v1 = k8s_client.CoreV1Api(api_client)
    apps_v1 = k8s_client.AppsV1Api(api_client)

    phases = []

    preflight_result = run_preflight(core_v1, config)
    phases.append(preflight_result)

    if not preflight_result.passed:
        now = _now()
        phases.append(skipped_result("healthcheck", "Skipped: preflight failed", now))
        phases.append(skipped_result("failover", "Skipped: preflight failed", now))
        phases.append(skipped_result("validate", "Skipped: preflight failed", now))
        _finish(config, checks_path, resolved_operator, drill_started, phases, dry_run)
        return

    healthcheck_result, baseline = run_healthcheck(core_v1, apps_v1, config, checks_config)
    phases.append(healthcheck_result)

    if not healthcheck_result.passed:
        now = _now()
        phases.append(skipped_result("failover", "Skipped: baseline healthcheck failed", now))
        phases.append(skipped_result("validate", "Skipped: baseline healthcheck failed", now))
        _finish(config, checks_path, resolved_operator, drill_started, phases, dry_run)
        return

    if dry_run:
        now = _now()
        phases.append(skipped_result("failover", "Skipped: --dry-run requested", now))
        phases.append(skipped_result("validate", "Skipped: --dry-run requested", now))
        _finish(config, checks_path, resolved_operator, drill_started, phases, dry_run)
        return

    failover_result = run_failover(core_v1, apps_v1, config, checks_config)
    phases.append(failover_result)

    if not failover_result.passed:
        now = _now()
        phases.append(
            skipped_result("validate", "Skipped: failover did not complete successfully", now)
        )
        _finish(config, checks_path, resolved_operator, drill_started, phases, dry_run)
        return

    validate_result = run_validate(core_v1, apps_v1, config, checks_config, baseline)
    phases.append(validate_result)

    _finish(config, checks_path, resolved_operator, drill_started, phases, dry_run)


def _finish(config, checks_path, operator, drill_started, phases, dry_run) -> None:
    report = DrillReport(
        drill_name=config.get("drill_name", "unnamed-drill"),
        environment=config.get("environment", "unknown"),
        namespace=config["namespace"],
        operator=operator,
        started_at=drill_started,
        finished_at=_now(),
        parameters={
            "kube_context": config.get("kube_context"),
            "checks_config": checks_path,
            "dry_run": dry_run,
            "preflight": config.get("preflight", {}),
            "healthcheck": config.get("healthcheck", {}),
            "failover": config.get("failover", {}),
            "validate": config.get("validate", {}),
        },
        phases=phases,
    )

    _print_summary(report)

    output_dir = config.get("report", {}).get("output_dir", "reports")
    md_path, json_path = write_report(report, output_dir)
    console.print(f"\nReport written to [bold]{md_path}[/bold] and [bold]{json_path}[/bold]")

    if report.overall_status != "passed":
        sys.exit(1)


def _print_summary(report: DrillReport) -> None:
    table = Table(title=f"Drill result: {report.overall_status.upper()}")
    table.add_column("Phase")
    table.add_column("Status")
    table.add_column("Duration (s)", justify="right")
    table.add_column("Reason")

    for p in report.phases:
        table.add_row(
            p.phase,
            f"[{_status_style(p.status)}]{p.status.value.upper()}[/{_status_style(p.status)}]",
            f"{p.duration_seconds:.2f}",
            p.reason or "-",
        )

    console.print(table)


if __name__ == "__main__":
    cli()
