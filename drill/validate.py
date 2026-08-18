"""Post-failover validation phase: re-measure the same signals captured in
the healthcheck baseline and assert the system is still in an acceptable
state after failover -- i.e. the secondary is now doing the job the
primary was doing before.

Validation deliberately compares against the *baseline snapshot* rather
than re-asserting a fixed set of expectations, so that "healthy" means
"the system as a whole is still serving," not "every individual field
looks identical to before." Some degradation is tolerated via
config["validate"]["max_allowed_degradation_ratio"].
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from kubernetes import client
from kubernetes.client.rest import ApiException

from drill.healthcheck import HealthSnapshot, take_snapshot
from drill.models import CheckResult, PhaseResult, PhaseStatus

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def run_validate(
    core_v1: client.CoreV1Api,
    apps_v1: client.AppsV1Api,
    config: dict,
    checks_config: dict,
    baseline: HealthSnapshot,
) -> PhaseResult:
    """Poll for a post-failover snapshot and compare its healthy_ratio
    against the baseline's, retrying per config["validate"] until either
    the degradation is within tolerance or retries are exhausted.
    """
    started = _now()
    start_t = time.monotonic()

    v_cfg = config.get("validate", {})
    namespace = config["namespace"]
    retries = v_cfg.get("retries", 5)
    retry_interval = v_cfg.get("retry_interval_seconds", 3)
    timeout_seconds = v_cfg.get("timeout_seconds", 60)
    max_degradation = v_cfg.get("max_allowed_degradation_ratio", 0.10)

    post_snapshot: HealthSnapshot | None = None
    last_error: str | None = None

    for attempt in range(1, retries + 1):
        if time.monotonic() - start_t > timeout_seconds:
            last_error = f"Exceeded overall timeout of {timeout_seconds}s"
            break
        try:
            post_snapshot = take_snapshot(core_v1, apps_v1, namespace, checks_config)
        except ApiException as exc:
            last_error = f"API error on attempt {attempt}: {exc.reason}"
            post_snapshot = None
        except Exception as exc:  # noqa: BLE001
            last_error = f"Unexpected error on attempt {attempt}: {exc}"
            post_snapshot = None

        if post_snapshot is not None:
            degradation = baseline.healthy_ratio - post_snapshot.healthy_ratio
            if degradation <= max_degradation:
                break

        logger.debug(
            "Validate attempt %d/%d degradation exceeds tolerance, retrying", attempt, retries
        )
        if attempt < retries:
            time.sleep(retry_interval)

    checks: list[CheckResult] = []

    if post_snapshot is not None:
        for name, d in post_snapshot.deployments.items():
            checks.append(
                CheckResult(
                    name=f"post/deployment/{name}",
                    passed=d.healthy,
                    detail=(
                        f"ready_replicas={d.ready_replicas} " f"expected>={d.expected_min_replicas}"
                    ),
                )
            )
        for name, s in post_snapshot.services.items():
            checks.append(
                CheckResult(
                    name=f"post/service/{name}",
                    passed=s.healthy,
                    detail=f"ready_endpoint_count={s.ready_endpoint_count}",
                )
            )

        degradation = baseline.healthy_ratio - post_snapshot.healthy_ratio
        degradation_ok = degradation <= max_degradation
        checks.append(
            CheckResult(
                name="degradation_within_tolerance",
                passed=degradation_ok,
                detail=(
                    f"baseline={baseline.healthy_ratio:.0%} post={post_snapshot.healthy_ratio:.0%} "
                    f"degradation={degradation:.0%} tolerance={max_degradation:.0%}"
                ),
            )
        )
        all_passed = degradation_ok
        reason = "" if all_passed else "Post-failover health degraded beyond tolerance"
    else:
        all_passed = False
        reason = last_error or "Could not obtain a post-failover health snapshot"

    finished = _now()
    duration = time.monotonic() - start_t

    return PhaseResult(
        phase="validate",
        status=PhaseStatus.PASSED if all_passed else PhaseStatus.FAILED,
        started_at=started,
        finished_at=finished,
        duration_seconds=duration,
        reason=reason,
        checks=checks,
        data={
            "baseline_healthy_ratio": round(baseline.healthy_ratio, 3),
            "post_healthy_ratio": round(post_snapshot.healthy_ratio, 3) if post_snapshot else None,
        },
    )
