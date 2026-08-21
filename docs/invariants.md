# Invariants

Rules that look like accidents, redundancy, or over-engineering, and are not. Each one
exists because the obvious simpler version produces a **silently missed alert** — the
only failure that costs anything here, and the one that looks exactly like everything
working.

Every entry names the test that holds it. If you are considering changing something in
this file, break the named test on purpose first and read what it says.

The bar for removing anything below is not "this looks unnecessary" — it is "I can show
the failure it prevents can no longer happen."

---

## 1. A busy tick must keep the stall clock running

`health.py` / `runner.py`

`SourceBusy` means the source answered "I'm busy" rather than failing. jeffco hits it
routinely, because SmartFindExpress returns HTTP 400 while the account holder's own
session is active.

**The tempting simplification is to exempt busy ticks from the stall check entirely.**
That is wrong. A source that answers 400 *permanently* — broken, or an account genuinely
locked — would then produce permanent silence, under a heartbeat still reporting "still
watching". A monitor that has stopped working while insisting it hasn't is the worst
state this system can reach.

So a busy tick does not reset `last_success_unix`. While *every* failure since the last
success has been busy, the absence is judged against `busy_stall_alert_sec` (3600) with
wording that names the cause; any non-busy fault drops it back to `stall_alert_sec`
(600).

- `test_a_busy_tick_does_not_reset_the_success_clock`
- `test_a_busy_only_absence_uses_the_longer_threshold`
- `test_one_real_fault_forfeits_the_busy_grace_window`
- `test_a_real_fault_uses_the_short_threshold_even_after_busy_ticks`

## 2. Retryability is a rule, not a list

`discord.py`

`retryable` returns true for **any** `>= 500`, plus an explicit set of 4xx. It does not
enumerate 5xx codes.

Enumerating them is how Cloudflare's 520–524 became "permanent", which banked the keys
and swallowed the alerts. If you replace the `>= 500` comparison with a set, you
reintroduce that bug for the next non-standard gateway code.

- `test_retryable_covers_rate_limits_5xx_and_unknown_but_not_a_bad_request`
- `test_a_transport_error_with_no_status_is_treated_as_retryable`

## 3. A missing baseline and an unusable one mean opposite things

`state.py`

`load_state` returns `keys=None, corrupt=False` only for a missing file and
`keys=None, corrupt=True` when one exists but cannot be read as a key array. Both
suppress alerts on that tick, so collapsing them into a single "no baseline" looks
harmless.

It is not. Missing is a normal first boot. An unreadable file (including wrong
ownership or a directory at the configured path), malformed JSON, or valid JSON of the
wrong shape means the baseline is *gone*, this boot re-baselines silently, and
everything currently open goes unannounced. Nothing can recover those keys, so the only
useful response is to say so loudly.

An empty array is a **baseline**, not a first run — `echo '[]' > state.json` is the
supported way to ask for the current backlog.

- `test_missing_file_reads_as_first_run_and_is_not_corrupt`
- `test_an_unreadable_file_reads_as_first_run_AND_reports_corrupt`
- `test_an_unparseable_file_reads_as_first_run_AND_reports_corrupt`
- `test_an_empty_array_is_a_baseline_not_a_first_run`

## 4. A failed post withholds only the keys that message covered

`runner.py`

`Message.covers` exists so a partial failure loses nothing. Withholding the whole batch
would re-alert delivered items; banking the whole batch would swallow undelivered ones.
A duplicate costs one glance; a swallowed item can cost a real person a day's work.

The three status classes are also not interchangeable. A refused *webhook* (401/403/404)
withholds rather than banks, because banking would discard every item while the ops
message reporting the problem went to the same dead endpoint.

- `test_a_failed_post_withholds_only_that_messages_keys`
- `test_a_permanent_rejection_banks_the_keys_and_posts_an_ops_alert`
- `test_a_403_withholds_rather_than_banks_and_posts_no_ops_alert`
- `test_a_mixed_batch_banks_delivered_and_permanent_withholds_retryable`
- `test_a_key_no_message_covers_is_withheld_rather_than_banked`

## 5. A state-write failure must not be reported as a poll failure

`runner.py`

It is logged and the loop continues on the in-memory baseline. Treating it as a poll
failure would throttle polling, latch a false death alert, and suppress the heartbeat —
all while alerting was working perfectly.

## 6. jeffco posts one message per job, and there is no cap

`apps/jeffco/src/jeffco/alert.py`

Discord **merges embeds that share an identical `url`**, keeping only the first one's
title and description. SmartFindExpress has no per-job deep link, so every embed carries
the same URL. Batching therefore displayed one job and silently discarded the rest —
while state recorded all of them as announced. Verified against a real webhook.

There is deliberately no message cap either. Dropping a job is the one failure that costs
something real, and the only batch large enough to matter comes from a deliberate
`echo '[]' > state.json`.

- `test_gives_every_job_its_own_message_never_two_embeds_in_one`

## 7. The filter-gap report keeps the *newest* names

`apps/jeffco/src/jeffco/alert.py` slices `unmatched[-MAX_GAP_NAMES:]`; `monitor.py` holds
`_unmatched_seen` as an **insertion-ordered** `dict`, not a set, and does not sort it.

The list accumulates for the process lifetime. Sorting it, or slicing from the front,
gives the first names ever seen permanent ownership of every visible slot — so a newly
discovered campus never appears, and the report quietly stops doing its job.

`MAX_GAP_NAMES = 10` is not styling: an embed field value is capped at 1024 characters,
so an uncapped accumulating list eventually makes Discord reject the **whole heartbeat**,
turning the message that proves the monitor is alive into one that never arrives.

- `test_keeps_the_newest_names_not_the_first_ten_seen`
- `test_a_newly_discovered_school_reaches_a_report_the_old_ones_already_filled`

## 8. One failed detail fetch degrades one job

`apps/jeffco/src/jeffco/monitor.py`

A `render()` raise withholds **every** fresh key in the batch and then retries the
identical failure forever. So a per-job detail fetch that fails must degrade that job's
date line to the approximate form, inside the per-job `try`, and never propagate.

`format_approximate` catches broadly for the same reason: it is the *degraded* path, and
anything escaping it silences all alerting indefinitely.

- `test_a_failed_detail_fetch_degrades_only_that_one_job`
- `test_a_render_failure_withholds_everything_without_failing_the_poll`
- `test_approximate_degrades_rather_than_raising_on_an_unparseable_date`

## 9. The login-failure ceiling suppresses the network call, not just the result

`apps/jeffco/src/jeffco/sfe.py`

A wrong credential retried on a 60-second cadence is ~1,440 attempts a day against an
account belonging to a real person who gets work through that site. After three
consecutive login failures the client stops attempting for an hour, and the suppressed
tick makes **no request at all**.

This stays in the app rather than the library on purpose: it counts *login* failures
specifically and its window is SmartFindExpress's account policy, not poll arithmetic.
Hoisting it would give the library a failure-ceiling abstraction with one caller.
`timing.py` carries the hazard as a comment so the next authenticated app notices it.

## 10. Every HTTP send goes through one funnel

`apps/jeffco/src/jeffco/sfe.py` — `_send` is the only place `self._client` is touched, and its
parameters are enumerated rather than `**kwargs`.

httpx parses the `Location` header **even with `follow_redirects=False`**, and its
`RemoteProtocolError` quotes that header — which for SFE legitimately carries a live
`;jsessionid=`. A second, unguarded send site is exactly the defect this structure
prevents; it has happened once already.

Origins are compared on `(scheme, netloc)`, not `netloc` alone, so an `http://`
downgrade cannot put session cookies on the wire.

- `test_self_client_is_touched_from_exactly_one_place_in_the_source`

## 11. A partial row drop is announced

`apps/jeffco/src/jeffco/sfe.py`

`parse_jobs` drops a malformed row rather than raising, so one bad row cannot lose a whole
poll, and it raises when *every* row fails. In between, a dropped row is never announced,
never logged, and cannot reach the heartbeat's gap report either — that only covers rows
that parsed. Every signal reads healthy while a real job goes unmentioned, so the count
mismatch is logged.

- `test_a_partially_dropped_response_says_so_instead_of_alerting_less_quietly`

## 12. `isinstance(exp, bool)` is not redundant

`apps/jeffco/src/jeffco/sfe.py`

`bool` is a subclass of `int`, so a JWT carrying `{"exp": true}` passes an `isinstance(x, int)`
check and yields an expiry of `1` — an epoch in 1970, so the token is treated as expired
on every call and the client re-authenticates every tick. That is a login hammer against
an account with a lockout policy.

- `test_token_expiry_unix_rejects_a_bool_exp_despite_bool_being_an_int_subclass`

## 13. Date labels are built from explicit tables, not `strftime`

`apps/jeffco/src/jeffco/dates.py`, `apps/melanzana/src/melanzana/alert.py`

`%a`/`%b` are locale-dependent, and `%-I` (hour with no leading zero) is a glibc/BSD
extension Python does not guarantee. Explicit `WEEKDAYS`/`MONTHS` tuples with the hour,
minute and meridiem assembled by hand are what the parity harness proves byte-identical.

`_RANGE_SEP` is an en dash (U+2013) and the day separator is `·` (U+00B7). Both are
contracts with the reference output, not typography.

---

## The parity harness

`tools/parity-diff.sh` renders every Discord payload both implementations can produce, on
a frozen clock, and requires an empty diff. Three properties make that meaningful, and all
three are easy to destroy:

**No expected output is written down on either side.** Every literal in both dump scripts
is an *input* — item fields, epochs, clock integers, fixture names — or an import from
production code. Every rendered byte comes from each language's own production functions.
The moment a case hardcodes what it expects, it stops comparing implementations and starts
comparing a constant to itself. `LABELS` is *imported* rather than restated, so even config
wording drift is caught.

**Deliberate divergences must never be added as cases.** Where the two implementations are
*meant* to differ, a case can only be a tautology or a permanent diff, and a permanent diff
destroys the meaning of an empty one. Currently excluded on purpose:

- **No busy case.** The reference implementation has no busy Discord payload at all.
- **No shape-valid impossible date** (e.g. `2026-02-30`). `Date.parse` rolls it over to
  March 2 and renders a plausible wrong day; Python raises and degrades. `not-a-date` is
  used instead, which *both* reject, so both degrade and the bytes match. Do not
  "strengthen" it.

**Keep floats out of payloads.** These two serializers agree on everything tested except
one thing: a whole-valued float prints `3.0` in Python and `3` in Node. Nothing
float-valued reaches a payload today. The first one that does will produce a diff that
reads like a regression.

Also: a naive `jobStart` (no UTC offset) is interpreted in the **host** timezone. That
looks like a bug and is what makes the two implementations agree — both treat a naive
datetime as local. Forcing UTC would *create* a divergence. Real SFE always sends `Z`.

The harness must be able to fail. It has been broken on purpose by changing the en dash to
a hyphen, reversing per-job message order, and changing `MAX_GAP_NAMES` — each gives a
non-empty diff and a non-zero exit. If you change it, re-prove that.

---

## Two deploy rules

**`terraform apply` alone is half a deploy.** GCE does not re-run the startup script when
metadata changes, so applying on its own leaves the host running exactly what it ran
before — a deploy that looks like it worked and didn't. Use `./infra/deploy.sh`.

**No plan containing a delete may be applied.** `deploy.sh` parses the plan JSON and
refuses one. A replaced instance takes the boot disk with it, and every app's
`state.json`. The check tests `"delete" in actions` rather than `actions[0] == "delete"`,
because a `create_before_destroy` lifecycle emits `["create", "delete"]` and would
otherwise be waved through.
