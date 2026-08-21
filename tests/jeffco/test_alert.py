"""Ports the jeffco-owned half of jeffco-sub-monitor's test/discord.test.ts.

discord.test.ts has 21 `it(...)` cases across four `describe` blocks:

  formatJobAlerts (11)   -- APP, ported below as format_job_alerts tests.
    renders the layout the account holder was promised
    links to the available-jobs page
    adds a Duration line only when the job is not a full day  (split into the
      two functions below: a full day is not mentioned / a partial day is)
    omits the Teacher line when SFE names no employee
    falls back rather than printing "undefined" for a missing subject
    gives every job its own message, never two embeds in one
    holds to one embed per message however large the batch
    preserves the order it was given
    has no cap -- it posts every job, however many there are
    never pings
    returns no payloads for no jobs

  formatHeartbeat (6)    -- split: the gap-report content is APP (ported below
                             as heartbeat_extras_for tests); the base heartbeat
                             shape is now assembled by monitor.discord.format_heartbeat
                             and already covered by tests/lib/test_discord.py.
    reports the tracked job count and Discord-native relative time  -- LIBRARY
      (tests/lib/test_discord.py: test_heartbeat_matches_melanzanas_wording_and_never_pings,
      generalized across apps via OpsLabels)
    carries the filter-gap report so it arrives without being asked for  -- APP
    omits the gap field entirely when every school matched  -- APP
    truncates a long gap list so Discord cannot reject the whole heartbeat  -- APP
    never pings  -- LIBRARY (same test as above; the Payload-level guarantee is
      the library's, heartbeat_extras_for never touches content/allowed_mentions)
    keeps the newest names, not the first ten seen  -- APP

  formatStatusAlert (2)  -- LIBRARY. jeffco no longer formats death/recovery
                             messages itself; tests/lib/test_discord.py's
                             test_death_alert_matches_melanzanas_wording_and_never_pings
                             and test_recovery_alert_matches_melanzanas_wording_and_never_pings
                             cover this behaviour generically via OpsLabels.
    describes a stall with the failure count
    describes a recovery

  postAlert (2)          -- LIBRARY. monitor.discord.post, already covered by
                             tests/lib/test_discord.py's
                             test_post_sends_the_payload_as_json and
                             test_post_error_never_contains_the_webhook_url.
    posts the payload as JSON
    throws on a non-ok response without echoing the webhook url

So 11 + 4 = 15 cases are ported below (as 16 functions, since the Duration case
splits in two); the remaining 6 are already exercised in tests/lib/test_discord.py.
"""

from jeffco.alert import MAX_GAP_NAMES, format_job_alerts, heartbeat_extras_for
from jeffco.sfe import AVAILABLE_JOBS_URL
from jeffco.types import Job
from monitor.types import GREEN

DATE_LINE = "Fri Oct 16, 7:45 AM – 3:30 PM"  # noqa: RUF001 -- EN DASH, matches the real date line


def _job(
    job_id: int = 1,
    location_name: str = "GOLDEN HIGH SCHOOL",
    *,
    classf_name: str | None = None,
    employee_first_name: str | None = None,
    employee_last_name: str | None = None,
    duration_type: str | None = None,
) -> Job:
    return Job(
        job_id=job_id,
        location_name=location_name,
        job_start="2026-10-16T13:45Z",
        job_end="2026-10-16T21:30Z",
        classf_name=classf_name,
        employee_first_name=employee_first_name,
        employee_last_name=employee_last_name,
        duration_type=duration_type,
    )


# --- format_job_alerts (discord.test.ts: describe('formatJobAlerts')) -------


def test_renders_the_layout_the_account_holder_was_promised() -> None:
    # This layout -- school as the title, then Subject / Dates / Teacher as
    # labeled lines -- is the one shown in docs/for-dad.txt.
    messages = format_job_alerts(
        [
            (
                _job(
                    location_name="RALSTON VALLEY HS",
                    classf_name="SEC MATH",
                    employee_first_name="Jamie",
                    employee_last_name="Placeholder",
                ),
                "Fri Oct 16 · Mon Oct 19, 7:45 AM – 3:30 PM",  # noqa: RUF001 -- EN DASH
            )
        ]
    )
    embed = messages[0].payload.embeds[0]
    assert "RALSTON VALLEY HS" in embed.title
    assert embed.description == (
        "**Subject:** SEC MATH\n"
        "**Dates:** Fri Oct 16 · Mon Oct 19, 7:45 AM – 3:30 PM\n"  # noqa: RUF001
        "**Teacher:** Jamie Placeholder"
    )
    assert embed.color == GREEN


def test_links_to_the_available_jobs_page() -> None:
    # SFE exposes no per-job deep link, so every embed points at the list.
    messages = format_job_alerts([(_job(), DATE_LINE)])
    assert messages[0].payload.embeds[0].url == AVAILABLE_JOBS_URL


def test_a_full_day_duration_is_not_mentioned() -> None:
    # FULL is the overwhelming majority; naming it on every alert would be noise.
    messages = format_job_alerts([(_job(duration_type="FULL"), DATE_LINE)])
    assert "Duration" not in messages[0].payload.embeds[0].description


def test_a_partial_day_duration_is_mentioned() -> None:
    messages = format_job_alerts([(_job(duration_type="HALF_DAY_AM"), DATE_LINE)])
    assert "**Duration:** HALF_DAY_AM" in messages[0].payload.embeds[0].description


def test_omits_the_teacher_line_when_sfe_names_no_employee() -> None:
    # Not an empty "**Teacher:** " line -- an absent field should look absent.
    messages = format_job_alerts([(_job(employee_first_name="", employee_last_name=""), DATE_LINE)])
    description = messages[0].payload.embeds[0].description
    assert "Teacher" not in description
    assert "**Dates:**" in description


def test_falls_back_rather_than_printing_undefined_for_a_missing_subject() -> None:
    messages = format_job_alerts([(_job(classf_name=""), DATE_LINE)])
    assert "**Subject:** Not specified" in messages[0].payload.embeds[0].description


def test_gives_every_job_its_own_message_never_two_embeds_in_one() -> None:
    # The load-bearing test. Discord merges embeds that share an identical `url`
    # into one rendered embed, keeping only the first one's title and
    # description -- and every embed here points at the same available-jobs
    # page, because SFE exposes no per-job deep link. Batched as embeds, a
    # four-job tick showed one job and silently swallowed three, while state
    # recorded all four as announced: the exact miss this project exists to
    # prevent. Verified against a real webhook.
    alerts = [(_job(job_id=i), DATE_LINE) for i in range(1, 5)]
    messages = format_job_alerts(alerts)
    assert len(messages) == 4
    assert [len(m.payload.embeds) for m in messages] == [1, 1, 1, 1]
    assert [m.covers for m in messages] == [("1",), ("2",), ("3",), ("4",)]


def test_holds_to_one_embed_per_message_however_large_the_batch() -> None:
    alerts = [(_job(job_id=i), DATE_LINE) for i in range(1, 24)]
    messages = format_job_alerts(alerts)
    assert len(messages) == 23
    assert all(len(m.payload.embeds) == 1 for m in messages)


def test_preserves_the_order_it_was_given() -> None:
    # The caller posts these in sequence, so a reordering here would surface as
    # jobs arriving in an order that matches nothing.
    alerts = [(_job(job_id=i, location_name=f"SCHOOL {i} HS"), DATE_LINE) for i in range(1, 4)]
    titles = [m.payload.embeds[0].title for m in format_job_alerts(alerts)]
    assert titles == ["🏫 SCHOOL 1 HS", "🏫 SCHOOL 2 HS", "🏫 SCHOOL 3 HS"]


def test_has_no_cap_it_posts_every_job_however_many_there_are() -> None:
    # There is deliberately no message limit. A cap would mean dropping the
    # very thing this exists to deliver, and the scenario it guards against
    # (dozens of new high school jobs inside one tick) has never happened. The
    # realistic large batch is the first tick after `echo '[]' > state.json`.
    alerts = [(_job(job_id=i), DATE_LINE) for i in range(1, 57)]
    messages = format_job_alerts(alerts)
    assert len(messages) == 56
    assert sum(len(m.payload.embeds) for m in messages) == 56


def test_never_pings() -> None:
    # There is no @everyone option for this monitor -- it alerts one person,
    # and an ordinary Discord message already pushes to their phone.
    messages = format_job_alerts([(_job(), DATE_LINE)])
    payload = messages[0].payload
    assert payload.content is None
    assert payload.allowed_mentions_parse is None


def test_returns_no_payloads_for_no_jobs() -> None:
    assert format_job_alerts([]) == []


# --- heartbeat_extras_for (discord.test.ts: describe('formatHeartbeat'),     -
# --- the filter-gap-report cases only -- the base heartbeat shape is now     -
# --- library-owned; see tests/lib/test_discord.py.)                         -


def test_carries_the_filter_gap_report_so_it_arrives_without_being_asked_for() -> None:
    extras = heartbeat_extras_for(["NEW CAMPUS HS OF SOMETHING", "ANOTHER PLACE"])
    assert extras.fields is not None
    value = extras.fields[0].value
    assert "NEW CAMPUS HS OF SOMETHING" in value
    assert "ANOTHER PLACE" in value


def test_omits_the_gap_field_entirely_when_every_school_matched() -> None:
    extras = heartbeat_extras_for([])
    assert extras.fields == ()
    assert extras.footer_text is None


def test_truncates_a_long_gap_list_so_discord_cannot_reject_the_whole_heartbeat() -> None:
    # A field value is capped at 1024 characters, and the caller accumulates gap
    # names for the life of the process -- so without the cap this eventually
    # 400s the one message whose job is to prove the monitor is alive. The kept
    # slice is the newest MAX_GAP_NAMES (see the dedicated slice-direction test
    # below), so the last name survives truncation and the first does not.
    many = [f"CAMPUS NUMBER {i} MIDDLE SCHOOL" for i in range(25)]
    extras = heartbeat_extras_for(many)
    assert extras.fields is not None
    value = extras.fields[0].value
    assert "CAMPUS NUMBER 24 MIDDLE SCHOOL" in value
    assert "CAMPUS NUMBER 0 MIDDLE SCHOOL" not in value
    assert "…and 15 more" in value
    assert len(value) < 1024


def test_keeps_the_newest_names_not_the_first_ten_seen() -> None:
    # unmatched accumulates in discovery order for the life of the process, so
    # slicing from the front would give the first ten names permanent ownership
    # of every slot and a later campus would never appear. Distinct letters
    # (not a numbered sequence) so no name is a substring of another -- a
    # numbered "CAMPUS 1" / "CAMPUS 12" pair would let a wrong slice pass this
    # assertion by accident.
    letters = [
        "ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT", "GOLF", "HOTEL",
        "INDIA", "JULIETT", "KILO", "LIMA",
    ]  # fmt: skip
    names = [f"{letter} SCHOOL" for letter in letters]
    extras = heartbeat_extras_for(names)
    assert extras.fields is not None
    value = extras.fields[0].value
    assert "LIMA SCHOOL" in value  # the 12th, newest
    assert "ALPHA SCHOOL" not in value  # the 1st, should have aged out
    assert "…and 2 more" in value


def test_gaps_swap_the_footer_for_the_instruction() -> None:
    # This is the seam the library added specifically for jeffco: the field
    # carries the data and the footer carries what to do about it.
    assert heartbeat_extras_for(["DORAL ACADEMY"]).footer_text == (
        "If any of these are high schools, add them to HS_SCHOOLS."
    )


def test_max_gap_names_is_ten() -> None:
    # Pins the constant Task 8 and the docs rely on -- see the truncation and
    # newest-names tests above for the behaviour it drives.
    assert MAX_GAP_NAMES == 10
