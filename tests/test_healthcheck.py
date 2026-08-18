"""Unit tests for drill.healthcheck. AppsV1Api/CoreV1Api are fully mocked."""

from __future__ import annotations

from unittest.mock import MagicMock

from kubernetes.client.rest import ApiException

from drill.healthcheck import run_healthcheck, take_snapshot
from drill.models import PhaseStatus

CHECKS_CONFIG = {
    "deployments": [
        {"name": "web-frontend-primary", "expected_min_replicas": 3, "role": "primary"},
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
    "healthcheck": {"retries": 2, "retry_interval_seconds": 0, "timeout_seconds": 30},
}


def _deployment_status(ready_replicas: int):
    dep = MagicMock()
    dep.status.ready_replicas = ready_replicas
    return dep


def _endpoints(address_count: int):
    ep = MagicMock()
    subset = MagicMock()
    subset.addresses = [MagicMock() for _ in range(address_count)]
    ep.subsets = [subset]
    return ep


class TestTakeSnapshot:
    def test_healthy_snapshot(self):
        core_v1 = MagicMock()
        apps_v1 = MagicMock()
        apps_v1.read_namespaced_deployment_status.return_value = _deployment_status(3)
        core_v1.read_namespaced_endpoints.return_value = _endpoints(2)

        snapshot = take_snapshot(core_v1, apps_v1, "app-primary", CHECKS_CONFIG)

        assert snapshot.healthy_ratio == 1.0
        assert snapshot.deployments["web-frontend-primary"].healthy is True
        assert snapshot.services["web-frontend"].healthy is True

    def test_missing_deployment_is_unhealthy(self):
        core_v1 = MagicMock()
        apps_v1 = MagicMock()
        apps_v1.read_namespaced_deployment_status.side_effect = ApiException(
            status=404, reason="Not Found"
        )
        core_v1.read_namespaced_endpoints.return_value = _endpoints(2)

        snapshot = take_snapshot(core_v1, apps_v1, "app-primary", CHECKS_CONFIG)

        assert snapshot.deployments["web-frontend-primary"].exists is False
        assert snapshot.deployments["web-frontend-primary"].healthy is False
        assert snapshot.healthy_ratio == 0.5

    def test_under_replicated_deployment_is_unhealthy(self):
        core_v1 = MagicMock()
        apps_v1 = MagicMock()
        apps_v1.read_namespaced_deployment_status.return_value = _deployment_status(1)
        core_v1.read_namespaced_endpoints.return_value = _endpoints(2)

        snapshot = take_snapshot(core_v1, apps_v1, "app-primary", CHECKS_CONFIG)

        assert snapshot.deployments["web-frontend-primary"].healthy is False

    def test_service_with_no_endpoints_is_unhealthy(self):
        core_v1 = MagicMock()
        apps_v1 = MagicMock()
        apps_v1.read_namespaced_deployment_status.return_value = _deployment_status(3)
        core_v1.read_namespaced_endpoints.return_value = _endpoints(0)

        snapshot = take_snapshot(core_v1, apps_v1, "app-primary", CHECKS_CONFIG)

        assert snapshot.services["web-frontend"].healthy is False


class TestRunHealthcheck:
    def test_passes_when_fully_healthy(self):
        core_v1 = MagicMock()
        apps_v1 = MagicMock()
        apps_v1.read_namespaced_deployment_status.return_value = _deployment_status(3)
        core_v1.read_namespaced_endpoints.return_value = _endpoints(2)

        result, snapshot = run_healthcheck(core_v1, apps_v1, BASE_CONFIG, CHECKS_CONFIG)

        assert result.status is PhaseStatus.PASSED
        assert snapshot is not None
        assert snapshot.healthy_ratio == 1.0

    def test_fails_after_exhausting_retries(self):
        core_v1 = MagicMock()
        apps_v1 = MagicMock()
        apps_v1.read_namespaced_deployment_status.return_value = _deployment_status(0)
        core_v1.read_namespaced_endpoints.return_value = _endpoints(0)

        result, snapshot = run_healthcheck(core_v1, apps_v1, BASE_CONFIG, CHECKS_CONFIG)

        assert result.status is PhaseStatus.FAILED
        assert (
            apps_v1.read_namespaced_deployment_status.call_count
            == BASE_CONFIG["healthcheck"]["retries"]
        )

    def test_recovers_within_retries(self):
        core_v1 = MagicMock()
        apps_v1 = MagicMock()
        # First attempt unhealthy, second attempt healthy.
        apps_v1.read_namespaced_deployment_status.side_effect = [
            _deployment_status(0),
            _deployment_status(3),
        ]
        core_v1.read_namespaced_endpoints.return_value = _endpoints(2)

        result, snapshot = run_healthcheck(core_v1, apps_v1, BASE_CONFIG, CHECKS_CONFIG)

        assert result.status is PhaseStatus.PASSED
