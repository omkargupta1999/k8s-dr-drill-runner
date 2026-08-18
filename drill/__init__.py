"""k8s-dr-drill-runner: a tool-agnostic disaster recovery drill runner for Kubernetes.

The package is organized around five sequential phases, each independently
runnable and each gating the next:

    preflight   -> healthcheck (baseline) -> failover -> validate -> report

See drill.cli for the command-line entrypoint.
"""

__version__ = "0.1.0"
