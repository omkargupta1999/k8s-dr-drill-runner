# DR Drill Operational Playbook

This playbook is for the person running (or on call for) a DR drill using
this tool. It covers when to run a drill, how to read a failed phase, and
what to do about it. It assumes you've already read the top-level `README.md`
for an overview of the phases and how to invoke the CLI.

## When to run a drill

- **Scheduled cadence.** The `.github/workflows/drill.yml` workflow includes
  a weekly `schedule` trigger as a starting point. In a regulated
  environment, the actual cadence is usually dictated by an internal policy
  or an external audit requirement (e.g. "DR capability must be
  demonstrated quarterly") -- adjust the cron expression to match yours.
- **Before a compliance/audit window.** Run a drill and keep its report a
  few days before an audit so you have a fresh, dated artifact on hand
  rather than scrambling to produce evidence after the fact.
- **After any change to failover-relevant infrastructure.** Changes to
  Service definitions, replica counts, resource quotas, or the failover
  sequence in `config/drill-config.yaml` should be validated with a drill
  before being trusted.
- **On demand, via `workflow_dispatch`.** Useful for validating a fix to
  the drill configuration itself, or for an ad hoc readiness check ahead of
  a planned maintenance window.

Always prefer running against a staging or DR-designated environment first.
`--dry-run` (preflight + healthcheck only, no mutation) is the safe way to
validate connectivity and configuration before ever reaching the failover
phase.

## Reading the report

Every run produces `reports/drill-report-<timestamp>.md` and the matching
`.json`. Start with the **Phases** table -- it's the fastest way to see how
far the drill got:

| Phase | Status | Duration (s) | Reason |
|---|---|---|---|
| preflight | PASS | 1.84 | - |
| healthcheck | PASS | 6.21 | - |
| failover | FAIL | 92.10 | Step 'wait_for_ready' (target=secondary) failed: ... |
| validate | SKIP | 0.00 | Skipped: failover did not complete successfully |

The `reason` column on a failed or skipped phase is a one-line summary; the
per-phase `### <phase> — checks` table below it in the same report has the
full detail for every individual assertion that phase made.

`overall_status` in the JSON report (and the report title in the Markdown)
is one of:

- **`passed`** — every phase that ran, passed.
- **`failed`** — at least one phase failed. Downstream phases were skipped,
  not attempted, to avoid compounding an already-bad state.
- **`incomplete`** — no phase failed outright, but at least one was skipped
  (e.g. a deliberate `--dry-run`, or the drill was stopped for a reason
  other than a failure). Treat this the same as "did not demonstrate DR
  readiness" for audit purposes — only `passed` counts as evidence.

## Interpreting a failed phase

### preflight failed

The cluster itself isn't in a state where a drill should proceed. Check the
`checks[]` detail for which specific check failed:

- `api_reachable` failed — the cluster is unreachable or credentials are
  invalid. Nothing else in the drill can run; this is an infrastructure or
  access problem, not a drill problem.
- `node_readiness` failed — fewer nodes are Ready than
  `preflight.min_ready_nodes` requires. Investigate node health before
  retrying; running a failover drill on an already-degraded cluster
  produces a false negative.
- `quota_headroom` failed — the namespace is close to its resource quota.
  Proceeding could cause the failover phase's scale-up step to fail
  partway through for a reason unrelated to DR readiness itself. Free up
  quota or raise the threshold deliberately (not silently) before retrying.

**Nothing was mutated.** Preflight is read-only, so it's always safe to
fix the underlying issue and re-run from the top.

### healthcheck (baseline) failed

The environment wasn't healthy *before* the drill even started shifting
traffic. This almost always means there's a pre-existing incident or
misconfiguration unrelated to DR — the drill correctly refuses to simulate
a failover on top of an already-unhealthy baseline, since you'd have no way
to tell whether any post-failover issue was caused by the drill or was
already there.

Check which `deployment/*` or `service/*` entries in the checks table
failed, resolve the underlying health issue, and re-run.

### failover failed

The `checks[]` list shows exactly which step in the sequence failed and
which steps after it were never attempted (see `drill/failover.py` —
steps execute strictly in order and stop at the first failure). Common
causes:

- `scale_up` / `scale_down` failed — usually an RBAC permission issue (the
  operator's kubeconfig lacks `patch` on `deployments/scale`) or the
  target deployment doesn't exist under the name configured in
  `checks.yaml`.
- `wait_for_ready` timed out — the secondary didn't reach its expected
  replica count within `failover.step_timeout_seconds`. Check pod events
  in the namespace; this is often a resource-starvation or image-pull
  issue on the standby side specifically, which is exactly the kind of
  gap a DR drill exists to surface.
- `shift_traffic` failed — the Service patch was rejected, often because
  the service name or selector label in `checks.yaml` doesn't match the
  actual Service manifest.

**Escalation:** because failover mutates cluster state, a failed failover
step may leave the environment in a partially-shifted state (e.g. the
secondary scaled up but traffic not yet shifted). Escalate to the
on-call/platform team immediately per your organization's incident process
before attempting cleanup — don't just re-run the drill against a
partially-mutated environment. See "Recovering from a partial failover"
below.

### validate failed

Failover *completed* (all its steps reported success) but the post-failover
system doesn't look healthy enough relative to the pre-failover baseline.
Look at the `degradation_within_tolerance` check specifically — it shows
`baseline=X% post=Y% degradation=Z% tolerance=T%`. If `Z > T`, something the
failover sequence changed didn't actually restore full service (e.g. the
secondary came up but one of its dependent endpoints didn't).

This is the most operationally significant failure mode: from the failover
phase's point of view everything succeeded, but the drill still caught a
real gap. Treat a `validate` failure as seriously as a `failover` failure —
escalate before assuming service is actually restored.

## Recovering from a partial failover

If a drill run stops mid-`failover` or fails `validate`, the environment
may be left with the secondary scaled up and/or traffic shifted, but the
primary not yet in its original state (or vice versa, depending on which
step failed). This tool does not attempt automatic rollback — see
`README.md`'s "Trade-offs and limitations" for why. Manual recovery:

1. Confirm current state: `kubectl get deployments,svc,endpoints -n <namespace>`.
2. Compare against the `data` block in the failed report's JSON — it
   records the healthcheck baseline snapshot, which is the state you're
   restoring toward.
3. Manually reverse whichever steps in the `failover.sequence` succeeded,
   in reverse order, using standard `kubectl scale` / `kubectl patch`
   commands.
4. Re-run `healthcheck` alone (or the full drill with `--dry-run`) to
   confirm the environment is back to baseline before considering the
   incident closed.
5. File the drill's JSON report as part of the incident record — it
   already has the timestamped, step-by-step account of what was attempted
   and where it stopped.

## Audit / compliance notes

Every report includes `operator`, `started_at`/`finished_at`, the full
`parameters` block (so the exact thresholds and sequence used are part of
the record), and per-phase, per-check pass/fail detail. For audit purposes,
retain the JSON report (it's the complete machine-readable record); the
Markdown version is generated from the same data and is meant for human
review, not as a separate source of truth. See `docs/dc-dr-writeup.md` for
the reasoning behind treating the report as the audit artifact itself
rather than a manually-compiled summary.
