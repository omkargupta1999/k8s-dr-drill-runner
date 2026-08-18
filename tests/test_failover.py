"""Unit tests for drill.failover. AppsV1Api/CoreV1Api are fully mocked."""

from __future__ import annotations

from unittest.mock import MagicMock

from kubernetes.client.rest import ApiException

from drill.failover import run_failover
from drill.models import PhaseStatus

CHECKS_CONFIG = {
    "deployments": [
        {"name": "web-frontend-primary", "expected_min_replicas": 3, "role": "primary"},
        {"name": "web-frontend-secondary", "expected_min_replicas": 3, "role": "secondary"},
    ],
    "services": [
        {
            "name": "web-frontend",
            "selector_label": "role",
            "primary_value": "web-frontend-primary",
            "secondary_value": "web-frontend-secondary",
            "health_path": "/healthz",
            "port": 8080,
        },
    ],
}

BASE_CONFIG = {
    "namespace": "app-primary",
    "failover": {
        "sequence": [
            {"action": "scale_up", "target": "secondary"},
            {"action": "wait_for_ready", "target": "secondary"},
            {"action": "shift_traffic", "target": "service"},
            {"action": "scale_down", "target": "primary"},
        ],
        "step_timeout_seconds": 5,
        "settle_seconds": 0,
    },
}


def _deployment_status(ready_replicas: int):
    dep = MagicMock()
    dep.status.ready_replicas = ready_replicas
    return dep


class TestRunFailover:
    def test_full_sequence_succeeds(self):
        core_v1 = MagicMock()
        apps_v1 = MagicMock()
        apps_v1.read_namespaced_deployment_status.return_value = _deployment_status(3)

        result = run_failover(core_v1, apps_v1, BASE_CONFIG, CHECKS_CONFIG)

        assert result.status is PhaseStatus.PASSED
        assert len(result.checks) == 4
        apps_v1.patch_namespaced_deployment_scale.assert_any_call(
            name="web-frontend-secondary", namespace="app-primary", body={"spec": {"replicas": 3}}
        )
        core_v1.patch_namespaced_service.assert_called_once()

    def test_stops_after_scale_up_failure_and_skips_remaining_steps(self):
        core_v1 = MagicMock()
        apps_v1 = MagicMock()
        apps_v1.patch_namespaced_deployment_scale.side_effect = ApiException(
            status=500, reason="Internal Error"
        )

        result = run_failover(core_v1, apps_v1, BASE_CONFIG, CHECKS_CONFIG)

        assert result.status is PhaseStatus.FAILED
        # Only the first step (scale_up) should have been attempted.
        assert len(result.checks) == 1
        assert result.checks[0].name == "scale_up:secondary"
        core_v1.patch_namespaced_service.assert_not_called()

    def test_wait_for_ready_times_out_when_replicas_never_arrive(self):
        core_v1 = MagicMock()
        apps_v1 = MagicMock()
        apps_v1.read_namespaced_deployment_status.return_value = _deployment_status(0)

        result = run_failover(core_v1, apps_v1, BASE_CONFIG, CHECKS_CONFIG)

        assert result.status is PhaseStatus.FAILED
        assert any("wait_for_ready" in c.name for c in result.checks)
        # shift_traffic must never have been attempted since wait_for_ready failed first.
        core_v1.patch_namespaced_service.assert_not_called()

    def test_shift_traffic_patches_expected_selector(self):
        core_v1 = MagicMock()
        apps_v1 = MagicMock()
        apps_v1.read_namespaced_deployment_status.return_value = _deployment_status(3)

        run_failover(core_v1, apps_v1, BASE_CONFIG, CHECKS_CONFIG)

        _, kwargs = core_v1.patch_namespaced_service.call_args
        assert kwargs["body"]["spec"]["selector"] == {"role": "web-frontend-secondary"}
