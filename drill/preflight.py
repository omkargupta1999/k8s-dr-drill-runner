"""Preflight phase: is the cluster in a state where it's safe to even
attempt a DR drill?

This phase deliberately never mutates cluster state -- it only reads. Its
job is to fail fast and cheaply before the drill does anything that would
need to be reasoned about or unwound. Every threshold used here comes from
config["preflight"] (see config/drill-config.yaml); nothing is hardcoded.
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


def check_api_reachable(core_v1: client.CoreV1Api, timeout_seconds: int) -> CheckResult:
    """A minimal, cheap call that proves the API server is reachable and
    the current credentials are authorized to at least list namespaces.
    """
    try:
        core_v1.list_namespace(limit=1, _request_timeout=timeout_seconds)
        return CheckResult(name="api_reachable", passed=True, detail="API server responded")
    except ApiException as exc:
        return CheckResult(
            name="api_reachable", passed=False, detail=f"API error: {exc.status} {exc.reason}"
        )
    except Exception as exc:  # noqa: BLE001 - surfaces as a failed check, not a crash
        return CheckResult(name="api_reachable", passed=False, detail=f"Connection error: {exc}")


def check_node_readiness(core_v1: client.CoreV1Api, min_ready_nodes: int) -> CheckResult:
    """Count nodes reporting a Ready condition with status True."""
    try:
        nodes = core_v1.list_node()
    except ApiException as exc:
        return CheckResult(
            name="node_readiness", passed=False, detail=f"Could not list nodes: {exc.reason}"
        )

    ready_count = 0
    for node in nodes.items:
        for cond in node.status.conditions or []:
            if cond.type == "Ready" and cond.status == "True":
                ready_count += 1
                break

    passed = ready_count >= min_ready_nodes
    detail = f"{ready_count} ready node(s), require >= {min_ready_nodes}"
    return CheckResult(name="node_readiness", passed=passed, detail=detail)


def check_quota_headroom(
    core_v1: client.CoreV1Api, namespace: str, max_usage_ratio: float
) -> CheckResult:
    """Ensure the target namespace isn't already close to its resource
    quota, which would make a scale-up during failover fail partway
    through in a confusing way. If no ResourceQuota objects exist in the
    namespace, this check passes trivially (nothing to be close to).
    """
    try:
        quotas = core_v1.list_namespaced_resource_quota(namespace=namespace)
    except ApiException as exc:
        return CheckResult(
            name="quota_headroom",
            passed=False,
            detail=f"Could not read resource quotas: {exc.reason}",
        )

    if not quotas.items:
        return CheckResult(
            name="quota_headroom", passed=True, detail="No ResourceQuota defined in namespace"
        )

    worst_ratio = 0.0
    worst_resource = None
    for quota in quotas.items:
        hard = (quota.status.hard or {}) if quota.status else {}
        used = (quota.status.used or {}) if quota.status else {}
        for resource_name, hard_value in hard.items():
            try:
                hard_qty = float(hard_value)
                used_qty = float(used.get(resource_name, "0"))
            except (TypeError, ValueError):
                continue
            if hard_qty <= 0:
                continue
            ratio = used_qty / hard_qty
            if ratio > worst_ratio:
                worst_ratio = ratio
                worst_resource = resource_name

    passed = worst_ratio <= max_usage_ratio
    detail = (
        f"Highest quota usage is {worst_ratio:.0%} ({worst_resource}), "
        f"threshold {max_usage_ratio:.0%}"
        if worst_resource
        else "No measurable quota usage"
    )
    return CheckResult(name="quota_headroom", passed=passed, detail=detail)


def run_preflight(core_v1: client.CoreV1Api, config: dict) -> PhaseResult:
    """Run all preflight checks and aggregate them into a single PhaseResult.

    `config` is the full drill configuration; this function reads only the
    `preflight` and `namespace` sections so it stays independent of the
    other phases' settings.
    """
    started = _now()
    start_t = time.monotonic()

    pf_cfg = config.get("preflight", {})
    namespace = config["namespace"]

    checks = [
        check_api_reachable(core_v1, pf_cfg.get("api_timeout_seconds", 10)),
        check_node_readiness(core_v1, pf_cfg.get("min_ready_nodes", 1)),
        check_quota_headroom(core_v1, namespace, pf_cfg.get("max_quota_usage_ratio", 0.85)),
    ]

    all_passed = all(c.passed for c in checks)
    reason = "" if all_passed else "One or more preflight checks failed; see checks[]"

    finished = _now()
    duration = time.monotonic() - start_t

    logger.info(
        "Preflight %s (%d/%d checks passed)",
        "passed" if all_passed else "failed",
        sum(1 for c in checks if c.passed),
        len(checks),
    )

    return PhaseResult(
        phase="preflight",
        status=PhaseStatus.PASSED if all_passed else PhaseStatus.FAILED,
        started_at=started,
        finished_at=finished,
        duration_seconds=duration,
        reason=reason,
        checks=checks,
    )
