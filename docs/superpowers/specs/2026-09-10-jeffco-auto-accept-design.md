# Jeffco Automatic Job Acceptance Design

## Goal

Automatically accept a Jeffco substitute job when all of these conditions hold:

- SFE reports the job as a full day.
- The school is Ralston Valley High School, Golden High School, or Arvada West
  High School.
- The first work date is at least seven Denver calendar days after today.

The monitor sends one Discord outcome message after the attempt: a confirmation
when the job was accepted, or an urgent manual-claim warning when automatic
acceptance failed.

## Scope

This is a Jeffco-only feature. It adds an authenticated production mutation to an
application that currently only reads SFE and posts alerts, so it is disabled by
default and must be explicitly enabled in production.

This design does not add automatic declines, cancellations, schedule-conflict
resolution, configurable target schools, a configurable lead time, a second SFE
process, or a durable notification outbox. It does not change Melanzana,
FashionJobs, or the shared monitor runner.

## API research

The current public Jeffco SFE SPA was inspected without logging in or making a
mutation. Its JavaScript implements acceptance as:

```text
PUT /api/jobAssignment/{jobId}
Body: none
```

The SPA treats any successful HTTP response as acceptance and does not consume a
response body. It verifies active assignments through:

```text
GET /api/jobAssignment/{jobId}
```

This evidence came from the public bundle served on 2026-09-10 at
`9.c2615a525c3dd10bcb52.js`. The code calls `putAcceptJob(jobId)`, which sends
`http.put(baseUrl + "jobAssignment/" + jobId, null)`; `baseUrl` is `/api/`.

No authenticated research request was made and no job was accepted during
research.

## Eligibility

Eligibility is a pure, fail-closed decision owned by
`apps/jeffco/src/jeffco/auto_accept.py`.

The job qualifies only when:

1. `duration_type == "FULL"` exactly. Missing or unfamiliar values do not
   qualify.
2. The normalized location matches one of these normalized names:
   `RALSTON VALLEY HS`, `GOLDEN HS`, or `ARVADA WEST HS`. Matching reuses
   `normalize_school_name`; the broader high-school pattern is not sufficient
   for automatic acceptance.
3. `job_start` is a valid ISO-8601 timestamp with an explicit UTC offset. Its
   `America/Denver` date must be greater than or equal to the current Denver date
   plus seven days. Exactly seven days qualifies; six days does not.

`job_start` is the first work date used for the decision. The detail endpoint is
not fetched before acceptance because the available-jobs row already provides
the required start, duration, location, and ID, and acceptance speed matters.
Job detail remains the authoritative source for the subsequent Discord date
line.

Malformed timestamps, naive timestamps, absent duration values, and unexpected
school spellings remain ordinary alert-only jobs. They are logged as ineligible
without logging source payloads.

## Components

### SFE client

`SfeClient` gains two public operations:

```python
async def accept_job(self, job_id: int) -> None: ...
async def has_job_assignment(self, job_id: int) -> bool: ...
```

`accept_job` sends the bodyless `PUT /api/jobAssignment/{job_id}`.
`has_job_assignment` sends `GET /api/jobAssignment/{job_id}` and returns true
only when SFE proves the authenticated substitute has that assignment.

The existing authenticated request mechanics are factored so JSON reads and
status-only operations share token refresh, one reauthentication after a 401,
HTML-response rejection, status handling, and the single `_send` funnel.
Available-job and job-detail behavior and error text remain unchanged.
Acceptance never requires a JSON response and never includes an SFE response
body in an exception or log.

### Automatic acceptance reconciler

`auto_accept.py` owns:

- the three target schools and seven-day threshold;
- the pure eligibility predicate;
- retry classification and timing;
- immutable accepted and failed outcome types; and
- an `AutoAccepter` that remembers decisions for the current process.

The reconciler receives the currently visible high-school jobs on every poll.
It removes cached decisions for eligible jobs that are no longer visible. Each
newly visible eligible job gets one acceptance sequence, and its accepted or
failed decision is cached while it remains visible. Therefore:

- a continuously visible job is not hammered every 45 seconds;
- a job that disappears and later returns is attempted again; and
- a fresh process attempts every eligible job currently visible, including jobs
  that were already present in alert state.

The cache is intentionally in memory. Restarting is defined to retry current
eligible jobs, not to preserve a terminal decision from an earlier process.

### Jeffco monitor item

The Jeffco monitor tracks a small wrapper containing the original `Job` and an
optional automatic-acceptance outcome. Ordinary items retain the existing
`str(job_id)` key. Outcome keys use separate namespaces:

```text
auto-accepted:{job_id}
auto-failed:{job_id}
```

This makes an outcome fresh even if the ordinary job ID was alerted before the
feature was enabled. Stable outcome keys prevent duplicate messages on later
polls while the same job remains visible.

When `AUTO_ACCEPT_ENABLED` is false, `JeffcoMonitor.fetch`, `key`, and `render`
retain their current externally observable behavior.

## Poll data flow

Each enabled poll performs these steps:

1. Fetch available SFE jobs through the existing 180-day Denver window.
2. Partition the result using the existing high-school filter and update the
   unmatched-school gap report.
3. Evaluate only the high-school jobs for automatic acceptance.
4. Reuse a cached decision for a continuously visible eligible job; otherwise
   run the bounded acceptance sequence.
5. Return ordinary jobs and outcome-bearing jobs to the shared runner.
6. Let the runner diff keys and call Jeffco rendering as it does today.
7. Resolve job detail after the diff and emit one ordinary, accepted, or failed
   message per fresh item.

The acceptance action happens in `fetch`, before the runner's new-item diff.
Consequently, existing alert state and the runner's first-run alert suppression
cannot prevent the action. Normal production restarts load the existing state
and send outcome messages normally. If alert state is missing or corrupt, the
action still takes priority, but the runner may silently baseline that first
outcome under its existing first-run rule. Durable notification across missing
state, process death, or prolonged Discord failure is outside this feature's
scope; acceptance is recorded in logs and in SFE itself.

## Acceptance and retry behavior

There are at most three PUT attempts per visibility episode. Attempt one is
immediate; attempts two and three wait one and three seconds respectively.
The injected sleeper makes this deterministic in tests.

The SFE client's existing one-time 401 reauthentication occurs inside an
individual attempt. A second 401 ends the sequence as a permanent failure.

These failures are retryable:

- HTTP 408;
- HTTP 429;
- HTTP 5xx; and
- `httpx` timeout or transport failures.

Other HTTP 4xx responses are permanent. Before retrying or reporting any failed
PUT, the reconciler calls `has_job_assignment(job_id)`. A successful assignment
lookup converts the result to accepted, covering the case where SFE committed a
PUT but the response was lost. An inconclusive verification never invents
success: retryable PUT failures continue within the attempt limit, while a
permanent PUT failure remains failed.

After three retryable failures, the outcome is failed and no further attempt is
made until the job disappears and returns or the process restarts. All error
reporting is reduced to operation, job ID, exception class, and HTTP status;
response bodies, authorization values, cookies, and credential-bearing redirect
headers are never logged.

## Discord messages

Ordinary jobs continue using the existing blue available-job message byte for
byte, preserving the current TypeScript parity cases.

An accepted outcome produces one green message with:

- title: the school name;
- status: `Automatically accepted`;
- subject, resolved dates, and teacher using the existing formatting rules;
- job ID;
- the Available Jobs URL; and
- a footer that states the monitor accepted the job automatically.

A failed outcome produces one urgent red message with:

- title: the school name;
- status: `Auto-accept failed — claim manually`;
- subject, resolved dates, and teacher;
- job ID;
- sanitized failure status; and
- the Available Jobs URL and an explicit manual-action footer.

There is no preliminary "job seen" message. Claiming is attempted before any
Discord delivery so a slow webhook cannot delay acceptance. A detail-fetch
failure degrades only that message to the existing approximate date line.

## Configuration and deployment

`JeffcoConfig` gains `auto_accept_enabled: bool`, loaded through the existing
strict `env_bool` helper from `AUTO_ACCEPT_ENABLED` with a default of false.

Infrastructure gains a boolean `jeffco_auto_accept_enabled` variable with a
default of false and renders it into the Jeffco environment as
`AUTO_ACCEPT_ENABLED`. The example tfvars documents the setting without changing
the real, secret-bearing `terraform.tfvars` during implementation.

Rollout has two separately authorized production deployments:

1. Deploy the new image with automatic acceptance disabled and verify service
   health and unchanged ordinary alerts.
2. Set the production variable true and deploy again. The first qualifying job
   is the first real mutation test and must be checked against the Discord
   outcome and SFE's active assignments.

No Terraform apply, image push, deployment, or runtime-state edit is part of
implementation unless explicitly authorized.

## Testing

Focused tests cover:

- exactly seven days qualifies and six days does not;
- Denver-local dates on both sides of DST changes;
- explicit-offset parsing and rejection of naive or malformed timestamps;
- exact full-day matching and fail-closed missing or unfamiliar durations;
- normalization of all three target schools and rejection of other high schools;
- the exact bodyless acceptance PUT and assignment-verification GET;
- empty and JSON 2xx acceptance responses;
- one reauthentication and retry after a 401;
- HTML and malformed responses without credential or response-body leakage;
- immediate, one-second, and three-second attempt timing;
- transient retry, permanent short-circuit, and ambiguous-success verification;
- one sequence per continuous visibility episode;
- reset after disappearance, and retry from a fresh reconciler process;
- automatic acceptance of IDs already present in ordinary alert state;
- accepted, failed, and unchanged ordinary Discord payloads;
- one failed detail fetch degrading only its own outcome message;
- disabled-by-default configuration and explicit enablement; and
- Terraform wiring and formatting.

Development uses focused `just test tests/jeffco/...` selectors. Completion
requires `just check`. `just parity` remains unchanged and must continue to
compare only the ordinary payloads shared with the sibling TypeScript monitor;
the deliberate Python-only automatic-acceptance messages are not parity cases.

## Documentation invariants

`docs/architecture.md` will describe the Jeffco-only reconciler and authenticated
mutation. `docs/invariants.md` will record these load-bearing rules:

- automatic acceptance is fail closed;
- an eligible job gets one bounded sequence per visibility episode;
- ambiguous PUT failures are verified before another mutation;
- acceptance is never delayed by Discord;
- outcome keys are separate from ordinary alert keys; and
- every SFE request still passes through the single protected send funnel.
