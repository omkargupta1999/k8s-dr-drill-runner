"""Healthcheck phase: capture a baseline health snapshot before failover,
and (reused by validate.py) re-measure the same signals afterward.

The snapshot records, per deployment listed in checks.yaml: whether it
exists, its ready replica count vs. expected_min_replicas, and whether its
Service endpoints have at least one ready address. This snapshot is what
validate.py compares the post-failover state against.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from kubernetes import client
from kubernetes.client.rest import ApiException

from drill.models import CheckResult, PhaseResult, PhaseStatus

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class DeploymentSnapshot:
    name: str
    exists: bool
    ready_replicas: int
    expected_min_replicas: int

    @property
    def healthy(self) -> bool:
        return self.exists and self.ready_replicas >= self.expected_min_replicas


@dataclass
class ServiceSnapshot:
    name: str
    ready_endpoint_count: int

    @property
    def healthy(self) -> bool:
        return self.ready_endpoint_count > 0


@dataclass
class HealthSnapshot:
    """The full point-in-time picture used both as the pre-failover
    baseline and the post-failover measurement.
    """

    deployments: dict[str, DeploymentSnapshot]
    services: dict[str, ServiceSnapshot]

    @property
    def healthy_ratio(self) -> float:
        """Fraction of tracked deployments+services that are healthy.
        Used by validate.py to detect degradation relative to baseline
        without requiring every single check to match exactly.
        """
        total = len(self.deployments) + len(self.services)
        if total == 0:
            return 1.0
        healthy = sum(d.healthy for d in self.deployments.values()) + sum(
            s.healthy for s in self.services.values()
        )
        return healthy / total

    def to_dict(self) -> dict:
        return {
            "deployments": {
                name: {
                    "exists": d.exists,
                    "ready_replicas": d.ready_replicas,
                    "expected_min_replicas": d.expected_min_replicas,
                    "healthy": d.healthy,
                }
                for name, d in self.deployments.items()
            },
            "services": {
                name: {"ready_endpoint_count": s.ready_endpoint_count, "healthy": s.healthy}
                for name, s in self.services.items()
            },
            "healthy_ratio": round(self.healthy_ratio, 3),
        }


def _snapshot_deployment(
    apps_v1: client.AppsV1Api, namespace: str, name: str, expected_min_replicas: int
) -> DeploymentSnapshot:
    try:
        dep = apps_v1.read_namespaced_deployment_status(name=name, namespace=namespace)
        ready = dep.status.ready_replicas or 0
        return DeploymentSnapshot(
            name=name,
            exists=True,
            ready_replicas=ready,
            expected_min_replicas=expected_min_replicas,
        )
    except ApiException as exc:
        if exc.status == 404:
            return DeploymentSnapshot(
                name=name,
                exists=False,
                ready_replicas=0,
                expected_min_replicas=expected_min_replicas,
            )
        raise


def _snapshot_service_endpoints(
    core_v1: client.CoreV1Api, namespace: str, name: str
) -> ServiceSnapshot:
    try:
        endpoints = core_v1.read_namespaced_endpoints(name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            return ServiceSnapshot(name=name, ready_endpoint_count=0)
        raise

    ready_count = 0
    for subset in endpoints.subsets or []:
        ready_count += len(subset.addresses or [])
    return ServiceSnapshot(name=name, ready_endpoint_count=ready_count)


def take_snapshot(
    core_v1: client.CoreV1Api,
    apps_v1: client.AppsV1Api,
    namespace: str,
    checks_config: dict,
) -> HealthSnapshot:
    """Read the current state of every deployment/service listed in
    checks.yaml. Pure read -- safe to call both before and after failover.
    """
    deployments = {}
    for entry in checks_config.get("deployments", []):
        snap = _snapshot_deployment(
            apps_v1, namespace, entry["name"], entry.get("expected_min_replicas", 1)
        )
        deployments[entry["name"]] = snap

    services = {}
    for entry in checks_config.get("services", []):
        snap = _snapshot_service_endpoints(core_v1, namespace, entry["name"])
        services[entry["name"]] = snap

    return HealthSnapshot(deployments=deployments, services=services)


def run_healthcheck(
    core_v1: client.CoreV1Api,
    apps_v1: client.AppsV1Api,
    config: dict,
    checks_config: dict,
) -> tuple[PhaseResult, HealthSnapshot | None]:
    """Poll for a healthy baseline snapshot, retrying per
    config["healthcheck"] until either everything tracked is healthy or
    retries are exhausted. Returns both the PhaseResult (for the report)
    and the HealthSnapshot itself (consumed by failover/validate), since
    the snapshot carries data the report doesn't need verbatim.
    """
    started = _now()
    start_t = time.monotonic()

    hc_cfg = config.get("healthcheck", {})
    namespace = config["namespace"]
    retries = hc_cfg.get("retries", 5)
    retry_interval = hc_cfg.get("retry_interval_seconds", 3)
    timeout_seconds = hc_cfg.get("timeout_seconds", 60)

    snapshot: HealthSnapshot | None = None
    last_error: str | None = None

    for attempt in range(1, retries + 1):
        if time.monotonic() - start_t > timeout_seconds:
            last_error = f"Exceeded overall timeout of {timeout_seconds}s"
            break
        try:
            snapshot = take_snapshot(core_v1, apps_v1, namespace, checks_config)
        except ApiException as exc:
            last_error = f"API error on attempt {attempt}: {exc.reason}"
            snapshot = None
        except Exception as exc:  # noqa: BLE001
            last_error = f"Unexpected error on attempt {attempt}: {exc}"
            snapshot = None

        if snapshot is not None and snapshot.healthy_ratio >= 1.0:
            break

        logger.debug("Healthcheck attempt %d/%d not fully healthy yet, retrying", attempt, retries)
        if attempt < retries:
            time.sleep(retry_interval)

    checks: list[CheckResult] = []
    if snapshot is not None:
        for name, d in snapshot.deployments.items():
            checks.append(
                CheckResult(
                    name=f"deployment/{name}",
                    passed=d.healthy,
                    detail=(
                        f"ready_replicas={d.ready_replicas} "
                        f"expected>={d.expected_min_replicas} exists={d.exists}"
                    ),
                )
            )
        for name, s in snapshot.services.items():
            checks.append(
                CheckResult(
                    name=f"service/{name}",
                    passed=s.healthy,
                    detail=f"ready_endpoint_count={s.ready_endpoint_count}",
                )
            )

    all_passed = snapshot is not None and snapshot.healthy_ratio >= 1.0
    reason = "" if all_passed else (last_error or "Baseline health snapshot was not fully healthy")

    finished = _now()
    duration = time.monotonic() - start_t

    result = PhaseResult(
        phase="healthcheck",
        status=PhaseStatus.PASSED if all_passed else PhaseStatus.FAILED,
        started_at=started,
        finished_at=finished,
        duration_seconds=duration,
        reason=reason,
        checks=checks,
        data=snapshot.to_dict() if snapshot else {},
    )
    return result, snapshot
