# Sample Drill Report

This is a realistic example of the Markdown artifact `drill/report.py` writes
to `reports/drill-report-<timestamp>.md` after a successful run. A matching
`reports/drill-report-<timestamp>.json` is written alongside it with the same
data in machine-readable form. Neither file is committed to this repository
(see `.gitignore`) -- this copy exists purely so a reader can see the output
without running the tool against a cluster.

It was produced by:

```bash
python -m drill.cli run --config config/drill-config.yaml --operator jdoe@example.com
```

---

# DR Drill Report: quarterly-dr-failover-drill

**Overall result:** `PASSED`

## Summary

- **Environment:** staging
- **Namespace:** app-primary
- **Operator:** jdoe@example.com
- **Started:** 2026-08-17T09:12:03.481221+00:00
- **Finished:** 2026-08-17T09:14:47.902558+00:00

## Parameters

```json
{
  "kube_context": null,
  "checks_config": "config/checks.yaml",
  "dry_run": false,
  "preflight": {
    "api_timeout_seconds": 10,
    "min_ready_nodes": 1,
    "max_quota_usage_ratio": 0.85
  },
  "healthcheck": {
    "retries": 5,
    "retry_interval_seconds": 3,
    "timeout_seconds": 60
  },
  "failover": {
    "sequence": [
      {"action": "scale_up", "target": "secondary", "description": "Scale the secondary (standby) deployment to serving capacity"},
      {"action": "wait_for_ready", "target": "secondary", "description": "Wait until the secondary deployment reports all replicas Ready"},
      {"action": "shift_traffic", "target": "service", "description": "Patch the Service selector to route traffic to the secondary deployment"},
      {"action": "scale_down", "target": "primary", "description": "Scale the primary deployment down to simulate the DR posture"}
    ],
    "step_timeout_seconds": 90,
    "settle_seconds": 10
  },
  "validate": {
    "retries": 5,
    "retry_interval_seconds": 3,
    "timeout_seconds": 60,
    "max_allowed_degradation_ratio": 0.1
  }
}
```

## Phases

| Phase | Status | Duration (s) | Reason |
|---|---|---|---|
| preflight | PASS | 1.84 | - |
| healthcheck | PASS | 6.21 | - |
| failover | PASS | 118.37 | - |
| validate | PASS | 37.55 | - |

### preflight -- checks

| Check | Result | Detail |
|---|---|---|
| api_reachable | PASS | API server responded |
| node_readiness | PASS | 3 ready node(s), require >= 1 |
| quota_headroom | PASS | Highest quota usage is 41% (requests.cpu), threshold 85% |

### healthcheck -- checks

| Check | Result | Detail |
|---|---|---|
| deployment/web-frontend-primary | PASS | ready_replicas=3 expected>=3 exists=True |
| deployment/web-frontend-secondary | PASS | ready_replicas=3 expected>=3 exists=True |
| deployment/orders-api-primary | PASS | ready_replicas=2 expected>=2 exists=True |
| deployment/orders-api-secondary | PASS | ready_replicas=2 expected>=2 exists=True |
| service/web-frontend | PASS | ready_endpoint_count=3 |
| service/orders-api | PASS | ready_endpoint_count=2 |

### failover -- checks

| Check | Result | Detail |
|---|---|---|
| scale_up:secondary | PASS | Scaled web-frontend-secondary to 3 replicas |
| wait_for_ready:secondary | PASS | web-frontend-secondary reached 3/3 ready replicas |
| shift_traffic:service | PASS | Service web-frontend selector role=web-frontend-secondary |
| scale_down:primary | PASS | Scaled web-frontend-primary to 0 replicas |

### validate -- checks

| Check | Result | Detail |
|---|---|---|
| post/deployment/web-frontend-primary | FAIL | ready_replicas=0 expected>=3 |
| post/deployment/web-frontend-secondary | PASS | ready_replicas=3 expected>=3 |
| post/deployment/orders-api-primary | PASS | ready_replicas=2 expected>=2 |
| post/deployment/orders-api-secondary | PASS | ready_replicas=2 expected>=2 |
| post/service/web-frontend | PASS | ready_endpoint_count=3 |
| post/service/orders-api | PASS | ready_endpoint_count=2 |
| degradation_within_tolerance | PASS | baseline=100% post=83% degradation=17% tolerance=10% |

---

**Note on the `validate` table above:** `post/deployment/web-frontend-primary`
shows `FAIL` because the primary was deliberately scaled to zero by the
`scale_down:primary` failover step -- that's the expected end state of a
completed failover, not a defect. The phase-level result is still `PASS`
because `validate.py` doesn't require every individual check to hold; it
requires the aggregate `healthy_ratio` to stay within
`max_allowed_degradation_ratio` of the pre-failover baseline (here, the drop
from 100% to 83% is within the configured 10% tolerance -- in a run where it
exceeds tolerance, `degradation_within_tolerance` itself would read `FAIL`
and the `validate` phase, and therefore the drill's overall result, would be
`FAILED`). This is intentional: post-failover health is judged against "is
the system still serving," not "does every field match the pre-failover
snapshot exactly."

For an example of what a **failed** drill looks like and how to interpret
it, see `docs/drill-playbook.md`.
