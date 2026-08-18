# Automating Disaster Recovery Drills for Kubernetes: From Manual Coordination to a Single Pipeline

## The problem

In any regulated environment — financial services, healthcare, anything
with an auditor who eventually asks "how do you know your disaster
recovery plan actually works" — running a DR drill isn't optional. It's a
compliance requirement, usually recurring, usually with a paper trail
expected at the end of it. And in most places I've seen, it's still done
by hand.

Hand-done looks like this: someone opens a runbook document, works through
it top to bottom, and pastes results into a ticket as they go. Validate
that the standby environment is actually up. Check current health of the
primary. Coordinate with two or three other teams so nobody is deploying
into the environment mid-drill. Trigger the failover — sometimes a script,
sometimes a sequence of manual `kubectl` commands someone remembers from
last time. Watch dashboards for a while. Declare success or failure based
on gut feel about whether the graphs look right. Write it up. If you're
lucky, someone remembered to take screenshots.

That process takes hours, not because the underlying work is hard, but
because coordinating people is slow and checking things by hand is slow.
And every manual step is a place for something to go quietly wrong: a
health check skipped because someone assumed it was fine, a step run out
of order because the runbook was ambiguous, an outcome recorded from
memory an hour after it happened rather than captured in the moment. The
audit trail, in the worst case, really is "whatever someone remembered to
screenshot."

None of this is specific to one company or one industry. It's what happens
whenever an organization treats DR readiness as a checkbox exercise
performed by humans following a document, rather than as a repeatable,
observable process.

## Why this is harder to automate than it sounds

The instinct is to say "just script it," and a lot of teams do — usually a
shell script that does the failover steps and nothing else. That solves
the tedium of typing commands but doesn't solve the actual problem, which
is that a DR drill isn't a normal deployment.

A normal deploy that fails halfway is usually fine to just retry or roll
forward. A DR drill that fails halfway is a different kind of risk: you
might now have a standby environment partially scaled up, traffic partly
shifted, and the primary partly torn down — worse off than if you'd never
started, and in a state that's genuinely unsafe to leave unattended. That
means an automated drill runner can't just be "run these commands in
sequence and hope." It needs to know, at each step, whether it's safe to
proceed to the next one, and it needs to stop cleanly — with a clear
record of exactly where it stopped — rather than plow forward into a worse
state.

It also needs environment-aware validation, not a fixed script. "Is the
secondary healthy" means something different in a namespace with three
replicas of an API than one with a single deployment behind a Service.
Hardcoding thresholds into the automation just moves the fragility from
"person following a runbook" to "engineer editing a script nobody else
understands," which isn't actually progress.

And it needs a durable record as a first-class output, not an
afterthought bolted on by whoever writes the postmortem.

## The design

The approach I landed on breaks the drill into discrete phases, each one
independently runnable, each one gating the next:

**preflight → health baseline → failover → post-validation → report**

`preflight` asks a narrow question: is it even safe to attempt a drill
right now? Cluster reachable, nodes ready, enough quota headroom that a
scale-up during failover won't immediately fail for an unrelated reason.
It's entirely read-only. If it fails, nothing else runs — there's no
scenario where proceeding past a failed preflight check produces a
meaningful drill result.

`healthcheck` captures a baseline: what does "healthy" look like for this
environment, right now, before anything gets touched? This is also
read-only, and it's what everything after failover gets compared against
— not a fixed expectation, but this specific environment's actual
pre-drill state.

`failover` is the only phase that mutates anything, and it does so as an
explicit, ordered, config-driven sequence — scale the standby up, wait for
it to actually report ready, shift traffic to it, scale the primary down.
Each step's success gates the next; a failed step stops the sequence
immediately rather than attempting to push through.

`validate` re-measures the same signals from the baseline phase against
the post-failover state and asks whether the system is still healthy
enough, within a configured tolerance — not whether it looks byte-for-byte
identical to before, since a successful failover is expected to change
things.

`report` takes whatever happened — however far the drill got — and turns
it into a structured artifact.

In the actual implementation, this maps directly onto five modules:
`preflight.py`, `healthcheck.py`, `failover.py`, `validate.py`, and
`report.py`, each taking a Kubernetes API client and a config object and
returning a structured result. None of them know about the others or about
ordering — the gating logic lives entirely in the CLI entrypoint, which is
the one place that decides "if this phase failed, mark everything after it
as skipped and go straight to reporting." That separation is deliberate:
it means each phase is unit-testable on its own with a mocked API client,
and it means the failure-handling policy (stop vs. continue) is defined
once, in one place, instead of duplicated inside every phase.

## Making it auditable

The part regulated environments actually care about most isn't the
failover mechanics — it's proof that it happened, correctly, and can be
shown to someone who wasn't in the room.

So the report isn't a summary written after the fact. It's the direct
output of execution: operator identity (resolved from the kubeconfig
context or an explicit flag), start and end timestamps, every threshold
and parameter the run actually used, and a pass/fail/skip verdict for
every phase down to the individual check level — all captured as a
by-product of the tool doing its job, not as a separate documentation
step someone has to remember to do. It's emitted as both Markdown, for a
human reviewing it, and JSON, for feeding into whatever system tracks
compliance evidence. If a phase never ran because an earlier one failed,
that's recorded explicitly as `skipped`, with the reason — the report
tells you exactly how far the drill got and why it stopped, not just a
final thumbs up or down.

## Results

In my own experience running drills this way in a regulated environment,
moving from a manual, multi-team, runbook-driven process to a single
automated pipeline cut drill execution time by roughly half — and produced
a complete, timestamped audit record automatically, as a by-product of
running the tool, rather than as separate work someone had to do after the
fact. That second part mattered more than the time savings: the artifact
that used to be "whatever someone remembered to screenshot" became
something generated the same way every time, whether the drill passed or
failed.

## What I'd do differently

A few things I'd change if I were starting over. First, I'd build the
dry-run mode in from day one rather than adding it after the fact —
being able to validate configuration and connectivity with zero risk of
touching cluster state is something people reach for constantly once it
exists, and it should have been the default mental model from the start,
not a flag bolted on later.

Second, I'd think harder, earlier, about the failure mode where the
automation itself makes things worse — a failed failover step that leaves
the environment in a partial state is currently something a human has to
notice and clean up. An automated "rollback of the rollback" — reversing
whatever steps did complete, safely, if a later step fails — is the
obvious next layer, and I'd design the step sequence with that in mind
from the start rather than retrofitting it.

Third, notifications. Right now a drill's result lives in a report file
until someone goes and looks at it. Wiring a pass/fail summary into a
chat or paging system so the right people know the moment a drill
finishes — especially a scheduled one nobody's actively watching — is a
small addition that meaningfully changes how useful the automation is
day to day. It's the kind of thing that's easy to defer because the tool
"works" without it, right up until a scheduled drill fails silently and
nobody notices for a week.
