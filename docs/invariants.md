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
`keys=None, corrupt=True` when one exists but cannot be read as a string-key array. Both
suppress alerts on that tick, so collapsing them into a single "no baseline" looks
harmless.

It is not. Missing is a normal first boot. An unreadable file (including wrong
ownership or a directory at the configured path), malformed JSON, or valid JSON of the
wrong shape means the baseline is *gone*, this boot re-baselines silently, and
everything currently open goes unannounced. Nothing can recover those keys, so the only
useful response is to say so loudly.

Every array element must already be a string. Coercing JSON `null`, numbers, or
objects invents healthy-looking keys no monitor could have written and can make app
validation disagree with the runner's baseline.

An empty array is a **baseline**, not a first run — `echo '[]' > state.json` is the
supported way to ask for the current backlog.

- `test_missing_file_reads_as_first_run_and_is_not_corrupt`
- `test_an_unreadable_file_reads_as_first_run_AND_reports_corrupt`
- `test_an_unparseable_file_reads_as_first_run_AND_reports_corrupt`
- `test_an_empty_array_is_a_baseline_not_a_first_run`
- `test_a_key_array_with_any_non_string_is_corrupt`

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

The ordinary rendering assertions run under the host locale and therefore do not, by
themselves, enforce this rule. The locale test switches `LC_TIME` to each available
non-English fixture, proves `strftime` diverges, and proves both table-based renderers
do not.

- `test_explicit_date_tables_ignore_lc_time_when_strftime_does_not`

## 14. FashionJobs' product filter is fixed and proven from every HTML page

`apps/fashionjobs/src/fashionjobs/site.py`

The source is exactly the France-wide FashionJobs `Stage` HTML route. There is no
keyword, role, title, company, category, region, department, city, or other location
filter. The parser also requires the canonical route and checked structured contract
filter ID `5`; a URL that merely looks plausible is not enough.

Parser completion requires balanced HTML depth and finalized card, capture, and
heading state. Each card has exactly two semantic metadata fields, contract and
location. The localized timestamp display text may be empty and is ignored; the
timezone-aware absolute `time-ago[data-value]` is still required. The known contract
whitelist is `Stage`, `CDI`, `CDD`, `Alternance`, `Intérim`, and `Free-lance`.
Recognized non-Stage cards are deliberately excluded and logged; they do not fail the
page merely for being non-Stage. An unknown label or metadata shape fails closed. If
the page declares positive Stage results but every recognized card is non-Stage, the
complete filter leak still fails rather than looking empty.

- `test_stage_route_is_fixed_and_has_no_keyword_or_location_query`
- `test_truncated_document_fails_after_a_complete_card`
- `test_missing_interior_closing_tag_fails`
- `test_extra_closing_tag_fails_with_negative_depth`
- `test_balanced_depth_with_unfinished_parser_state_fails`
- `test_empty_timestamp_display_text_is_valid`
- `test_extra_muted_metadata_field_fails`
- `test_unknown_contract_label_fails`
- `test_excludes_a_recognized_non_stage_contract`
- `test_a_complete_contract_filter_leak_fails_instead_of_looking_empty`
- `test_a_malformed_card_fails_instead_of_being_skipped`

## 15. FashionJobs pagination commits one monotonic identity transaction

`apps/fashionjobs/src/fashionjobs/site.py`

Every process startup requires a bounded full scan through the first page's declared
end, even from seeded state. After a successful scan, another becomes due when at
least 86,400 monotonic seconds have elapsed and runs on the next poll; ordinary
intervening reads may stop at the first page with no ID unseen before that read.
Failed startup and due scans remain due because no candidate IDs, records, force
flag, or completion timestamp commits until the entire required traversal succeeds.
`MAX_PAGES=100` accepts page 100 and rejects a declared page 101 before the crawler
can fan out unexpectedly.

Numeric FJOB IDs never shrink when cards reorder or disappear. Full records remain
available in memory when possible; otherwise `KnownJob` placeholders preserve the
identity. Ordinary and promoted cards with one ID reconcile only when their core
fields agree, preferring a direct `/emploi/` URL. Promoted `/redir/` cards are parsed
from the result HTML but never crawled for identity; traversal requests only the
fixed Stage pagination URLs.

The direct-URL preference and HTML content-type check are load-bearing too: a direct
`/emploi/` record must replace an earlier promoted `/redir/` duplicate, and a 2xx
non-HTML body is a source error rather than parser input.

- `test_page_two_failure_discards_the_whole_candidate_read`
- `test_seeded_startup_scans_to_end_then_frontier_fast_stops`
- `test_failed_seeded_startup_scan_retries_the_full_walk`
- `test_seeded_frontier_still_stops_before_daily_full_scan_is_due`
- `test_due_daily_scan_crosses_duplicates_to_find_a_later_new_id`
- `test_failed_due_daily_scan_remains_due`
- `test_successful_due_daily_scan_resets_the_deadline`
- `test_declared_end_at_page_limit_is_valid`
- `test_declared_end_above_page_limit_fails`
- `test_same_read_duplicate_does_not_extend_frontier_traversal`
- `test_reordered_or_removed_cards_do_not_shrink_returned_ids`
- `test_full_record_is_retained_when_it_disappears_from_the_site`
- `test_extracts_ordinary_and_promoted_stage_cards`
- `test_duplicate_ids_keep_the_direct_emploi_url`
- `test_non_html_response_raises_an_error`

## 16. FashionJobs state and degraded items fail closed without blocking valid alerts

`apps/fashionjobs/src/fashionjobs/state.py` / `monitor.py` / `runner.py`

FashionJobs accepts persisted keys only when guarded integer conversion succeeds,
the value is positive, and converting it back produces the identical canonical
decimal string. One invalid key corrupts the entire state. `main.py` validates once,
gives the source that state's exact key object, and gives the runner the identical
`LoadedState`; corruption is loudly logged and silently rebaselined without a second
disk load or divergent interpretation.

The first complete read with no usable state is banked without alerts. A restart
seeded by persisted IDs does not duplicate alerts. Later full jobs produce one
message each in `(published_at, job_id)` order. A `KnownJob` has identity but no safe
alert fields, so render omits it. The runner's unchanged uncovered-key guard logs and
withholds only that placeholder; valid full jobs in the same batch still post and
settle. A retryable Discord failure withholds the failed message's covered IDs until
delivery succeeds, while later messages are ordinarily still attempted. A 429 is the
deliberate exception: the runner abandons all later messages, so the failed message's
IDs and every later unattempted ID are unsettled and withheld for the next tick.

- `test_any_noncanonical_job_id_makes_the_whole_state_corrupt`
- `test_canonical_positive_decimal_ids_are_preserved_exactly`
- `test_a_job_id_too_large_for_safe_integer_conversion_is_corrupt`
- `test_process_seeds_exact_state_and_reuses_one_http_client`
- `test_process_marks_noncanonical_state_corrupt_before_source_and_runner`
- `test_a_supplied_state_is_used_instead_of_reloading_the_file`
- `test_a_corrupt_supplied_state_loudly_rebaselines_without_alerting`
- `test_first_run_baselines_all_jobs_without_alerting`
- `test_restart_with_persisted_ids_does_not_duplicate_alerts`
- `test_failed_notification_is_withheld_then_retried_successfully`
- `test_a_429_abandons_the_rest_of_the_batch`
- `test_multiple_new_jobs_are_posted_in_deterministic_order`
- `test_render_orders_new_jobs_by_timestamp_then_numeric_id`
- `test_heartbeat_reports_the_tracked_listing_identity_count`
- `test_mixed_job_and_placeholder_posts_job_and_withholds_only_placeholder`
- `test_placeholder_only_batch_is_loudly_uncovered_without_raising`
- `test_real_source_posts_one_new_job_without_duplicating_retained_ids`

## 17. FashionJobs source text cannot create a Discord mention

`apps/fashionjobs/src/fashionjobs/alert.py`

Each listing gets one embed with title and URL plus `Company`, `Location`, `Contract`,
and `Published` fields. Source Markdown is escaped within Discord limits, and the
payload always sends `allowed_mentions: {"parse": []}`. Removing that pairing turns
an upstream title such as `@everyone` into a channel-wide ping.

- `test_alert_contains_every_reliable_job_field`
- `test_source_markdown_is_escaped_and_everyone_is_disabled`
- `test_escaped_title_and_fields_respect_discord_limits_without_dangling_escape`

---

## The parity harness

`tools/parity-diff.sh` renders every Discord payload both implementations can produce, on
a frozen clock, and requires an empty diff. Three properties make that meaningful, and all
three are easy to destroy:

“Both implementations” means only the Melanzana and Jeffco Python/TypeScript pairs.
FashionJobs has no sibling TypeScript implementation and must not be added to this
harness; its committed fixtures and Python tests own that scope.

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
