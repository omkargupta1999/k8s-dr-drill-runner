"""Failover phase: simulate a failover by executing an ordered sequence of
cluster mutations -- scaling the secondary up, waiting for it to become
ready, shifting a Service's selector to point at it, and scaling the
primary down.

The sequence and its ordering are entirely config-driven
(config["failover"]["sequence"]), not hardcoded, so the drill can be
adapted to a different failover strategy (e.g. skip the scale-down step,
or add an extra warmup step) without touching this module.

Steps execute strictly in order. If a step fails, remaining steps are not
attempted -- partial failover is recorded as such rather than silently
continuing, since continuing past a failed step could leave the cluster in
an inconsistent state that's harder to reason about than stopping.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from kubernetes import client
from kubernetes.client.rest import ApiException

from drill.models import CheckResult, PhaseResult, PhaseStatus

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _find_deployment_entry(checks_config: dict, role: str) -> dict | None:
    for entry in checks_config.get("deployments", []):
        if entry.get("role") == role:
            return entry
    return None


def _find_service_entry(checks_config: dict) -> dict | None:
    services = checks_config.get("services", [])
    return services[0] if services else None


def step_scale_up(
    apps_v1: client.AppsV1Api, namespace: str, checks_config: dict, target: str
) -> CheckResult:
    entry = _find_deployment_entry(checks_config, target)
    if entry is None:
        return CheckResult(
            name=f"scale_up:{target}", passed=False, detail="No deployment configured for this role"
        )
    name = entry["name"]
    replicas = entry.get("expected_min_replicas", 1)
    try:
        apps_v1.patch_namespaced_deployment_scale(
            name=name, namespace=namespace, body={"spec": {"replicas": replicas}}
        )
        return CheckResult(
            name=f"scale_up:{target}", passed=True, detail=f"Scaled {name} to {replicas} replicas"
        )
    except ApiException as exc:
        return CheckResult(
            name=f"scale_up:{target}", passed=False, detail=f"Scale failed for {name}: {exc.reason}"
        )


def step_scale_down(
    apps_v1: client.AppsV1Api, namespace: str, checks_config: dict, target: str
) -> CheckResult:
    entry = _find_deployment_entry(checks_config, target)
    if entry is None:
        return CheckResult(
            name=f"scale_down:{target}",
            passed=False,
            detail="No deployment configured for this role",
        )
    name = entry["name"]
    try:
        apps_v1.patch_namespaced_deployment_scale(
            name=name, namespace=namespace, body={"spec": {"replicas": 0}}
        )
        return CheckResult(
            name=f"scale_down:{target}", passed=True, detail=f"Scaled {name} to 0 replicas"
        )
    except ApiException as exc:
        return CheckResult(
            name=f"scale_down:{target}",
            passed=False,
            detail=f"Scale failed for {name}: {exc.reason}",
        )


def step_wait_for_ready(
    apps_v1: client.AppsV1Api,
    namespace: str,
    checks_config: dict,
    target: str,
    step_timeout_seconds: int,
) -> CheckResult:
    entry = _find_deployment_entry(checks_config, target)
    if entry is None:
        return CheckResult(
            name=f"wait_for_ready:{target}",
            passed=False,
            detail="No deployment configured for this role",
        )
    name = entry["name"]
    expected = entry.get("expected_min_replicas", 1)

    deadline = time.monotonic() + step_timeout_seconds
    last_ready = 0
    while time.monotonic() < deadline:
        try:
            dep = apps_v1.read_namespaced_deployment_status(name=name, namespace=namespace)
            last_ready = dep.status.ready_replicas or 0
            if last_ready >= expected:
                return CheckResult(
                    name=f"wait_for_ready:{target}",
                    passed=True,
                    detail=f"{name} reached {last_ready}/{expected} ready replicas",
                )
        except ApiException as exc:
            return CheckResult(
                name=f"wait_for_ready:{target}",
                passed=False,
                detail=f"Error reading {name}: {exc.reason}",
            )
        time.sleep(min(3, max(1, step_timeout_seconds // 10)))

    return CheckResult(
        name=f"wait_for_ready:{target}",
        passed=False,
        detail=f"Timed out after {step_timeout_seconds}s; {name} at {last_ready}/{expected} ready",
    )


def step_shift_traffic(
    core_v1: client.CoreV1Api, namespace: str, checks_config: dict
) -> CheckResult:
    service_entry = _find_service_entry(checks_config)
    if service_entry is None:
        return CheckResult(
            name="shift_traffic:service", passed=False, detail="No service configured"
        )

    name = service_entry["name"]
    label_key = service_entry["selector_label"]
    secondary_value = service_entry["secondary_value"]
    try:
        core_v1.patch_namespaced_service(
            name=name,
            namespace=namespace,
            body={"spec": {"selector": {label_key: secondary_value}}},
        )
        return CheckResult(
            name="shift_traffic:service",
            passed=True,
            detail=f"Service {name} selector {label_key}={secondary_value}",
        )
    except ApiException as exc:
        return CheckResult(
            name="shift_traffic:service",
            passed=False,
            detail=f"Patch failed for {name}: {exc.reason}",
        )


_STEP_HANDLERS = {
    "scale_up": step_scale_up,
    "scale_down": step_scale_down,
    "wait_for_ready": step_wait_for_ready,
    "shift_traffic": step_shift_traffic,
}


def run_failover(
    core_v1: client.CoreV1Api,
    apps_v1: client.AppsV1Api,
    config: dict,
    checks_config: dict,
) -> PhaseResult:
    """Execute the configured failover sequence in order, stopping at the
    first failed step. Every step's outcome is recorded as a CheckResult
    so the report shows exactly how far the failover progressed.
    """
    started = _now()
    start_t = time.monotonic()

    fo_cfg = config.get("failover", {})
    namespace = config["namespace"]
    sequence = fo_cfg.get("sequence", [])
    step_timeout = fo_cfg.get("step_timeout_seconds", 90)
    settle_seconds = fo_cfg.get("settle_seconds", 10)

    checks: list[CheckResult] = []
    reason = ""
    stopped_early = False

    for step in sequence:
        action = step["action"]
        target = step.get("target", "")

        if action == "scale_up":
            result = step_scale_up(apps_v1, namespace, checks_config, target)
        elif action == "scale_down":
            result = step_scale_down(apps_v1, namespace, checks_config, target)
        elif action == "wait_for_ready":
            result = step_wait_for_ready(apps_v1, namespace, checks_config, target, step_timeout)
        elif action == "shift_traffic":
            result = step_shift_traffic(core_v1, namespace, checks_config)
            if result.passed and settle_seconds:
                logger.debug("Settling %ds after traffic shift", settle_seconds)
                time.sleep(settle_seconds)
        else:
            result = CheckResult(
                name=f"unknown_action:{action}",
                passed=False,
                detail="Unrecognized action in config",
            )

        checks.append(result)
        logger.info(
            "Failover step %s (target=%s): %s", action, target, "OK" if result.passed else "FAILED"
        )

        if not result.passed:
            reason = f"Step '{action}' (target={target}) failed: {result.detail}"
            stopped_early = True
            break

    all_passed = bool(checks) and all(c.passed for c in checks) and not stopped_early

    finished = _now()
    duration = time.monotonic() - start_t

    return PhaseResult(
        phase="failover",
        status=PhaseStatus.PASSED if all_passed else PhaseStatus.FAILED,
        started_at=started,
        finished_at=finished,
        duration_seconds=duration,
        reason=reason,
        checks=checks,
        data={"steps_attempted": len(checks), "steps_configured": len(sequence)},
    )
