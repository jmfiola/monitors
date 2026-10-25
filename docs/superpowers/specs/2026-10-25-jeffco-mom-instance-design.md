# Jeffco Second Instance (Mom) Design

## Intent

Run a second copy of the Jeffco substitute-job monitor for Jacob's mom. She is a
substitute on the same Jeffco SmartFindExpress site as the existing account
holder (Dad), with her own SFE login and her own set of schools. She wants a Discord
alert whenever a new job appears at one of her schools, and nothing else.

The existing instance keeps working exactly as it does today. Mom's instance reuses
the same `jeffco` package and image; the difference between the two is per-instance
configuration, not copied code.

## Goals

- A second, fully isolated Jeffco process: its own SFE credentials, token cache,
  lockout counter, `state.json`, systemd unit, and container.
- Mom's job alerts go to her own channel on the same Discord server.
- Mom's ops messages (heartbeat, stall/busy/recovery, filter-gap report) go to
  Jacob's existing Jeffco status channel, next to Dad's, and are clearly labelled as
  Mom's.
- Mom's filter alerts only on jobs at an exact, named list of 19 schools. Like
  Dad's, it filters on school name alone; there is no subject filter.
- Dad's instance is unchanged in behavior: same high-school filter (built-in list
  union pattern, additive `HS_SCHOOLS`), same auto-accept rules, same alerts, same
  parity output.

## Non-goals

- Automatic acceptance for Mom. Her instance is alerts-only.
- A different SFE site or district. Both instances use
  `https://jeffco.eschoolsolutions.com`.
- A different alert layout for Mom's job messages. They render exactly like Dad's.
- A generic "any number of profiles from environment variables" system. There are
  exactly two named profiles, defined in code.
- Changes to `lib/monitor`, Melanzana, or FashionJobs.
- Secret Manager, a dead-man's switch, or any other known gap in
  `docs/architecture.md`.
- Announcing the backlog that exists when her instance first starts. Her first run
  is the normal silent baseline.

## Anti-goals (a failure even if it technically works)

- Any change to what Dad's instance alerts on, accepts, or renders.
- Mom's instance running with Dad's high-school filter, or with an empty school list
  that alerts on nothing while looking healthy.
- Mom's SFE account ever performing an automatic acceptance.
- A deploy plan that deletes or replaces any resource (`deploy.sh` refuses these;
  the design must not produce one).
- Secrets in git, or Mom's real name in any tracked file. She is "Mom" everywhere.

## Constraints

- Read `docs/invariants.md` first. Every Jeffco invariant (one message per job,
  newest-first gap report, degraded detail fetch, login-failure ceiling, single
  send funnel, partial row drop, bool `exp`, date tables, auto-accept exactness)
  applies to both instances unchanged.
- Mom shares her login with herself using the site, so the 45 s poll interval and
  the HTTP 400 → `SourceBusy` handling apply to her for the same reason as Dad.
- Strict typing (`mypy --strict`), ruff, and `just check` must pass. `just parity`
  must still produce an empty diff for Dad's item alerts if the sibling repo is
  available.
- Production mutations (image pushes, `deploy.sh`) need Jacob's explicit go-ahead
  at the time. Never a bare `terraform apply`. Never print `terraform.tfvars` or
  state.
- Docs (`architecture.md`, `invariants.md`, READMEs) describe the resulting system
  in the present tense. No "we added" or history.

## Decided design

### Profiles, selected per instance

The Jeffco app gains a profile concept: two named profiles, `dad` and `mom`, defined
in code. A required environment variable `JEFFCO_PROFILE` selects one. Missing or
any value other than exactly `dad` or `mom` is a `ConfigError` at startup. Required
rather than defaulted, because a forgotten variable on Mom's instance would
otherwise silently run Dad's high-school filter against her feed.

A profile owns:

- **The school filter.**
  - `dad`: today's behavior, unchanged. `DEFAULT_HS_SCHOOLS` union `HS_SCHOOLS`,
    union the high-school name pattern.
  - `mom`: an exact match against Mom's named list, using the same
    `normalize_school_name` on both sides. The high-school pattern does not apply.
- **The ops labels.**
  - `dad`: today's `LABELS`, unchanged (`Jeffco sub monitor`,
    `high school job(s)`, same death footer).
  - `mom`: a name that makes her messages distinguishable in the shared status
    channel, such as `Jeffco sub monitor (Mom)`, with a tracked noun that does not
    say "high school".
- **The filter-gap footer.**
  - `dad`: unchanged (`If any of these are high schools, add them to HS_SCHOOLS.`).
  - `mom`: says to add the school to Mom's list in code.
- **Whether auto-accept is allowed.** `dad` allows it (still gated by
  `AUTO_ACCEPT_ENABLED`); `mom` does not.

### Mom's school list lives in code

Her list is a tuple in `apps/jeffco/src/jeffco/schools.py`, next to
`DEFAULT_HS_SCHOOLS`, normalized once at import. That matches how Dad's list is
shipped: the tests exercise the real names, and a deployment typo cannot empty it.
Changing it means a new image and a deploy.

Tests must hold that the list is non-empty and has no duplicates after
normalization. The `mom` profile also refuses to start if its normalized list is
empty, as a second guard.

Her list, as her SFE profile spells it (district codes alongside, for reference
only — the API does not carry them, so they are not matched on):

| Location | Code |
| --- | --- |
| EXCEL ACADEMY | 155300 |
| FAIRMOUNT ELE | 114000 |
| FREMONT ELE | 116000 |
| HACKBERRY HILL ELE | 118800 |
| LAWRENCE ELE | 123000 |
| LINCOLN ACADEMY | 155800 |
| LITTLE ELE | 123500 |
| MAPLE GROVE ELE | 125100 |
| MEIKLEJOHN ELE | 126500 |
| SECREST ELE | 140000 |
| SEMPER ELE | 140100 |
| SIERRA ELE | 140500 |
| STOTT ELE | 138200 |
| THREE CREEKS K-8 | 143700 |
| VAN ARSDALE ELE | 144200 |
| VANDERHOOF ELE | 144000 |
| WARDER ELE | 144800 |
| WEBER ELE | 145200 |
| WEST WOODS ELE | 147700 |

Ship these 19 verbatim. Normalization replaces the hyphen in `K-8` with a space on
both sides, so `THREE CREEKS K-8` still matches itself.

### No subject filter

Her SFE profile also lists the job types she takes (1st–3rd grade, ELE general,
ELE instrumental music, ELE kindergarten, K-12 art, drama/theater, reading, vocal
music). These are not filtered by the monitor. Like Dad's instance, Mom's relies
on the available-jobs endpoint returning only jobs the account holder is qualified
and assigned for, and filters on school name alone. If SFE also filters by her
profile's locations, her school list is a backstop that rarely excludes anything;
anything it does exclude appears in the gap report.

### Config validation for the `mom` profile

Each of these refuses to start (`ConfigError`) rather than being ignored:

- `AUTO_ACCEPT_ENABLED=true`. A value that silently does nothing invites someone
  to believe it works.
- A non-empty `HS_SCHOOLS`. It is Dad's additive knob, and on Mom's instance it
  would read as "this adds a school" while doing nothing.
- An empty normalized school list.

Under the `dad` profile, every existing config behavior and test stays as it is,
apart from adding `JEFFCO_PROFILE=dad` to the test base environment.

### Gap report

The unmatched-name mechanism is unchanged: insertion-ordered accumulation,
newest-first budget, a log of the full list at each heartbeat. Under `mom`, every
school in her feed that is not on her list is "unmatched". The report will
therefore routinely be full of schools she has chosen to ignore. That is expected.
It is how a misspelled entry on her list gets noticed: her school appears in the
gap report instead of alerting. It goes to Jacob's status channel, not hers.

### Startup log line

`app config — …` gains `profile=<name>`. `schools=` reports the active profile's
list size. It still never logs the SFE user id.

### Infrastructure

The app key is `jeffco-mom`. Everything per app is derived from the key, giving
unit and container `jeffco-mom-monitor`, volume `/var/lib/jeffco-mom-data`, and env
file `/etc/monitors/jeffco-mom.env`. `just logs jeffco-mom` works unchanged.

- **Image reuse.** `var.apps` entries gain an optional `repository` field that
  defaults to the key. `local.images` uses it, so Mom's entry pulls the existing
  `jeffco` repository's `jeffco-sub-monitor` image instead of needing a duplicate
  push. `google_artifact_registry_repository.app` iterates the distinct repository
  names rather than the app keys. For the existing three apps the keys are
  unchanged, so the plan must show **no** change to any repository. Verify with
  `just infra-plan` before any deploy.
- **Tag.** Mom's entry carries the same `image_tag` as every other app. It is the
  same image as Dad's.
- **Variables.** These are new, following the existing Jeffco variables' types,
  sensitivity, defaults, and validation:
  - `jeffco_mom_sfe_user_id` (sensitive)
  - `jeffco_mom_sfe_pin` (sensitive)
  - `jeffco_mom_discord_webhook_url` (sensitive)
  - `jeffco_mom_poll_interval_sec` (default 45, with the same shared-login
    comment)
  - `jeffco_mom_heartbeat_at` (default `07:00`)
- **Status webhook.** Mom's `STATUS_WEBHOOK_URL` reuses
  `var.jeffco_status_webhook_url` directly, using the same conditional-merge
  pattern, so there is no second copy of the same secret.
- **`local.app_env`.**
  - Dad's map gains `JEFFCO_PROFILE = "dad"`.
  - Mom's `jeffco-mom` map has `JEFFCO_PROFILE = "mom"`, `AUTO_ACCEPT_ENABLED =
    "false"` (explicit), her creds and webhook, the shared status webhook,
    `STATE_PATH`, `POLL_INTERVAL_SEC`, `HEARTBEAT_INTERVAL_SEC`, `STALL_ALERT_SEC`,
    and the conditional `HEARTBEAT_AT`.
  - The existing precondition already fails the plan if the map is missing.
- **`terraform.tfvars.example`.** Gains a Mom block with placeholders. Jacob adds
  the real values to the gitignored `terraform.tfvars`.
- **`deploy.sh`.** The pre-first-apply fallback unit list gains
  `jeffco-mom-monitor.service`. The normal path derives units from `var.apps`
  already.
- **Memory.** Before choosing her cap, measure the live Jeffco container with a
  read-only `docker stats --no-stream` on the host. That is authenticated
  production access; ask Jacob first. Today's declared caps total 512 MiB on a
  roughly 1 GiB `e2-micro`. Mom's container runs the same code as Dad's 256 MiB
  one. Record the chosen cap and the new total, with its reasoning, in the docs
  that currently state 512 MiB.

### Logs Explorer

A fourth saved query, `jeffco-mom-monitor-logs`, mirrors the per-app union in
`infra/README.md`. The `cos_system` half cannot match on the image string alone,
because Mom and Dad run the same `jeffco-sub-monitor` image. It must also match the
container name, or Dad's and Mom's deploy events appear in each other's queries.
Creating the saved query is a manual, production-side step for Jacob. The docs
describe four per-app queries and the corrected union.

### Rollout (each deploy needs Jacob's go-ahead)

There is one release: Mom's list is known, so `just check` can pass with the real
list in code. The infra changes are split across two deploys. Mom's new Terraform
variables are required and have no defaults, and `env_b64` resolves
`var.apps[k]` for every app. If her variables, `app_env` map, and `apps` entry
shipped with deploy 1, its plan would fail on missing values.

- **Deploy 1** carries the app code and Dad's `JEFFCO_PROFILE = "dad"`. It also
  carries the optional `repository` field and the per-repository registry
  `for_each`.
- **Deploy 2** carries everything specific to `jeffco-mom`: her variables, her
  `app_env` map, her `apps` entry, and her `terraform.tfvars.example` block.

Terraform state and `terraform.tfvars` exist only in the main checkout at
`~/personal/monitors`. Run `just infra-plan` and `deploy.sh` from there after the
relevant change is merged, never from a worktree.

1. **Release.** Build and push the new tag for all three images, as today. The
   `jeffco` push serves both Jeffco instances.
2. **Deploy 1: the existing apps on the new tag, no Mom entry yet.** First,
   `just infra-plan` must show no change to any Artifact Registry repository.
   After deploying, verify Dad's `app config` line shows `profile=dad`, the same
   school count as before, and `auto_accept=enabled`, plus normal polling. This
   proves Dad is unchanged before anything new runs.
3. **Deploy 2: add the `jeffco-mom` entry and her secrets.** Verify her line shows
   `profile=mom`, `schools=19`, `auto_accept=disabled`, and `firstRun=True`. Also
   verify the unit is active and the plan contained only creates and updates.
4. **Spelling check.** For the first day, read her instance's `filter gap:` log
   lines (`just logs jeffco-mom --freshness=24h`). Any of her 19 schools appearing
   there is a spelling mismatch between her profile and the jobs feed, and gets
   fixed in code. The Discord gap report is no substitute for the log here: under
   her profile it is mostly schools she chose to ignore, so a mismatch can hide
   behind "…and N more".
5. **First heartbeat.** Confirm her heartbeat arrives in Jacob's status channel at
   07:00, labelled as Mom's.

Before deploy 2, Mom should log in to SFE once by hand to confirm the credentials
Jacob enters are right. A wrong PIN costs three attempts and then one attempt an
hour against her real account, and surfaces only as a stall alert.

**Rollback.** An image older than this release ignores `JEFFCO_PROFILE` and would
run Dad's high-school filter on Mom's account, so she would silently get no alerts.
Her env still sets `AUTO_ACCEPT_ENABLED=false`, so no acceptance is possible.
Rolling the tag back below this release therefore removes the `jeffco-mom` entry
in the same change. `infra/README.md` states this next to the existing rollback
rule.

Mom's list is committed in code in a public repository. Jacob has approved that
explicitly, as with Dad's list.

### Documentation

- `docs/architecture.md`
  - Jeffco runs as two instances of one package, selected by `JEFFCO_PROFILE`.
  - What a profile owns.
  - The config list.
  - Memory caps.
  - The diagram, if it lists `schools.py`'s role.
- `docs/invariants.md`
  - A new entry: the profile is required, and `mom` is exact and fails closed on an
    empty list, on `HS_SCHOOLS`, and on `AUTO_ACCEPT_ENABLED=true`.
  - Name the tests that hold this.
  - The existing auto-accept invariant (14) notes that only the `dad` profile can
    reach it.
- `infra/README.md`
  - The `jeffco-mom` app.
  - The repository override.
  - Memory totals.
  - Four saved queries and the corrected union.
  - Build instructions noting one Jeffco push serves both instances.
- `apps/README.md`
  - The optional `repository` field, for an additional instance of an existing app.

## Left to the builder

- Module layout for profiles: a new `profiles.py` or inside `config.py`/
  `schools.py`.
- How `partition_jobs` receives the matcher: a callable, a small protocol, or a
  filter object. It must stay pure and fully tested.
- Exact wording of Mom's `OpsLabels` and gap footer, within the constraints above.
- Whether `LOG_PREFIX` becomes per-profile. Container names already separate the
  logs, so keeping one prefix is acceptable.
- Exact test names, as long as each new invariant names its guard tests and each
  guard is proven by mutation (break it, watch the test fail).
- Commit structure and the release tag number (next minor after `v2.4.1`).

## Open items for Jacob

- **Go-ahead for the read-only host memory measurement and for each deploy.**
