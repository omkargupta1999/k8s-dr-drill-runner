"""Unit tests for drill.preflight. The Kubernetes CoreV1Api is fully
mocked -- no real cluster or network access is used or required.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from kubernetes.client.rest import ApiException

from drill.models import PhaseStatus
from drill.preflight import (
    check_api_reachable,
    check_node_readiness,
    check_quota_headroom,
    run_preflight,
)


def _ready_node():
    node = MagicMock()
    cond = MagicMock()
    cond.type = "Ready"
    cond.status = "True"
    node.status.conditions = [cond]
    return node


def _not_ready_node():
    node = MagicMock()
    cond = MagicMock()
    cond.type = "Ready"
    cond.status = "False"
    node.status.conditions = [cond]
    return node


class TestCheckApiReachable:
    def test_passes_when_api_responds(self):
        core_v1 = MagicMock()
        core_v1.list_namespace.return_value = MagicMock()
        result = check_api_reachable(core_v1, timeout_seconds=10)
        assert result.passed is True

    def test_fails_on_api_exception(self):
        core_v1 = MagicMock()
        core_v1.list_namespace.side_effect = ApiException(status=503, reason="Service Unavailable")
        result = check_api_reachable(core_v1, timeout_seconds=10)
        assert result.passed is False
        assert "503" in result.detail

    def test_fails_on_generic_connection_error(self):
        core_v1 = MagicMock()
        core_v1.list_namespace.side_effect = ConnectionError("no route to host")
        result = check_api_reachable(core_v1, timeout_seconds=10)
        assert result.passed is False
        assert "Connection error" in result.detail


class TestCheckNodeReadiness:
    def test_passes_when_enough_ready_nodes(self):
        core_v1 = MagicMock()
        core_v1.list_node.return_value = MagicMock(items=[_ready_node(), _ready_node()])
        result = check_node_readiness(core_v1, min_ready_nodes=2)
        assert result.passed is True

    def test_fails_when_not_enough_ready_nodes(self):
        core_v1 = MagicMock()
        core_v1.list_node.return_value = MagicMock(items=[_ready_node(), _not_ready_node()])
        result = check_node_readiness(core_v1, min_ready_nodes=2)
        assert result.passed is False
        assert "1 ready node" in result.detail


class TestCheckQuotaHeadroom:
    def test_passes_when_no_quota_defined(self):
        core_v1 = MagicMock()
        core_v1.list_namespaced_resource_quota.return_value = MagicMock(items=[])
        result = check_quota_headroom(core_v1, "app-primary", max_usage_ratio=0.85)
        assert result.passed is True

    def test_fails_when_usage_exceeds_threshold(self):
        core_v1 = MagicMock()
        quota = MagicMock()
        quota.status.hard = {"pods": "10"}
        quota.status.used = {"pods": "9"}
        core_v1.list_namespaced_resource_quota.return_value = MagicMock(items=[quota])
        result = check_quota_headroom(core_v1, "app-primary", max_usage_ratio=0.85)
        assert result.passed is False

    def test_passes_when_usage_within_threshold(self):
        core_v1 = MagicMock()
        quota = MagicMock()
        quota.status.hard = {"pods": "10"}
        quota.status.used = {"pods": "2"}
        core_v1.list_namespaced_resource_quota.return_value = MagicMock(items=[quota])
        result = check_quota_headroom(core_v1, "app-primary", max_usage_ratio=0.85)
        assert result.passed is True


class TestRunPreflight:
    @pytest.fixture
    def base_config(self):
        return {
            "namespace": "app-primary",
            "preflight": {
                "api_timeout_seconds": 10,
                "min_ready_nodes": 1,
                "max_quota_usage_ratio": 0.85,
            },
        }

    def test_all_checks_pass_yields_passed_phase(self, base_config):
        core_v1 = MagicMock()
        core_v1.list_namespace.return_value = MagicMock()
        core_v1.list_node.return_value = MagicMock(items=[_ready_node()])
        core_v1.list_namespaced_resource_quota.return_value = MagicMock(items=[])

        result = run_preflight(core_v1, base_config)

        assert result.status is PhaseStatus.PASSED
        assert result.phase == "preflight"
        assert len(result.checks) == 3
        assert all(c.passed for c in result.checks)

    def test_one_failed_check_yields_failed_phase(self, base_config):
        core_v1 = MagicMock()
        core_v1.list_namespace.return_value = MagicMock()
        core_v1.list_node.return_value = MagicMock(items=[_not_ready_node()])
        core_v1.list_namespaced_resource_quota.return_value = MagicMock(items=[])

        result = run_preflight(core_v1, base_config)

        assert result.status is PhaseStatus.FAILED
        assert result.reason != ""
