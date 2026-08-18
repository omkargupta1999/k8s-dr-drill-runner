"""Kubernetes client construction and operator-identity resolution.

Isolated in its own module so every phase module can be unit tested by
mocking kubernetes.client.CoreV1Api / AppsV1Api directly, without ever
touching real kubeconfig loading logic.
"""

from __future__ import annotations

import getpass
import logging

from kubernetes import client, config

logger = logging.getLogger(__name__)


def build_api_client(kube_context: str | None = None) -> client.ApiClient:
    """Build a Kubernetes ApiClient from the ambient kubeconfig, a named
    context, or in-cluster config when running inside a pod (e.g. as a
    scheduled Job). Tries, in order: in-cluster config, then kubeconfig.
    """
    try:
        config.load_incluster_config()
        logger.debug("Loaded in-cluster Kubernetes config")
    except config.ConfigException:
        config.load_kube_config(context=kube_context)
        logger.debug("Loaded kubeconfig context=%s", kube_context or "<current>")
    return client.ApiClient()


def resolve_operator(explicit_operator: str | None, kube_context: str | None = None) -> str:
    """Determine the identity to attribute this drill run to, in priority
    order: an explicit --operator flag, the "user" of the active kubeconfig
    context, then the OS login name. Never raises -- worst case falls back
    to "unknown".
    """
    if explicit_operator:
        return explicit_operator

    try:
        contexts, active = config.list_kube_config_contexts()
        target = active
        if kube_context:
            target = next((c for c in contexts if c["name"] == kube_context), active)
        user = target["context"].get("user") if target else None
        if user:
            return user
    except Exception:  # noqa: BLE001 - best-effort identity resolution
        logger.debug("Could not resolve operator from kubeconfig context", exc_info=True)

    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "unknown"
