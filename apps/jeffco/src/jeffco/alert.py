"""Per-job alerts. Alert formatting is per-app by design: melanzana renders one
embed with a field per day, jeffco renders one message per job.

Discord accepts up to 10 embeds per message, but it also *merges* embeds that
share an identical `url` into a single rendered embed, keeping only the first
one's title and description. SFE exposes no per-job deep link, so every embed
below carries the same AVAILABLE_JOBS_URL -- so batching them meant a
multi-job tick displayed exactly one job and silently discarded the rest, with
state recording all of them as announced. Verified against a real webhook: two
embeds sharing a url render as one; the same two with urls differing only by
fragment render as two. The fragment workaround is not usable here either,
because AVAILABLE_JOBS_URL is itself a hash route
(`/ui/#/substitute/jobs/available`) and a second `#` would rewrite the route
rather than append to it. One message per job costs nothing and assumes
nothing -- there is deliberately no cap on how many are produced.
"""

from __future__ import annotations

from collections.abc import Sequence

from monitor.types import GREEN, Embed, Field, HeartbeatExtras, Message, Payload

from jeffco.sfe import AVAILABLE_JOBS_URL
from jeffco.types import Job

#: How many characters the unmatched-school list may spend before it summarizes
#: the rest. Not a style choice: a Discord embed field value is capped at 1024
#: characters, and the gap list accumulates over the process lifetime, so an
#: uncapped list would eventually make Discord reject the entire heartbeat --
#: turning the message that proves the monitor is alive into one that never
#: arrives.
#:
#: A character budget rather than a name count, because characters are the actual
#: limit. The previous fixed count of 10 spent about a fifth of the available
#: field on names roughly 20 characters long and hid the remainder behind
#: "…and N more", which someone then has to go and ask about.
GAP_VALUE_BUDGET = 1000

#: Held back from GAP_VALUE_BUDGET for the "• …and N more" line. Reserved rather
#: than measured because that line's length depends on how many names are hidden,
#: which is not known until the split has already been chosen.
_GAP_SUMMARY_RESERVE = 24


def _describe(job: Job, date_line: str) -> str:
    """The alert body. This layout -- school as the title, then Subject /
    Dates / Teacher as labeled lines -- is the one the account holder was
    shown in docs/for-dad.txt, so it renders what he was promised.

    Labeled lines in one description rather than embed fields: fields reflow
    into columns on desktop and reorder relative to the description, and this
    reads the same everywhere.
    """
    teacher = f"{job.employee_first_name or ''} {job.employee_last_name or ''}".strip()
    lines = [f"**Subject:** {job.classf_name or 'Not specified'}", f"**Dates:** {date_line}"]
    if teacher:
        lines.append(f"**Teacher:** {teacher}")
    # FULL is the overwhelming majority; naming it on every alert would be noise.
    if job.duration_type and job.duration_type != "FULL":
        lines.append(f"**Duration:** {job.duration_type}")
    return "\n".join(lines)


def format_job_alerts(alerts: Sequence[tuple[Job, str]]) -> list[Message]:
    """Build one message per job, in the order given.

    Returns a list because a busy tick needs more than one message; the caller
    posts them in order, spaced out. No cap on the number of messages --
    dropping a job is the one failure mode that costs something real, and the
    only batch big enough to matter is the deliberate one after
    `echo '[]' > state.json`.
    """
    return [
        Message(
            payload=Payload(
                embeds=(
                    Embed(
                        title=f"🏫 {job.location_name}",
                        url=AVAILABLE_JOBS_URL,
                        description=_describe(job, date_line),
                        color=GREEN,
                        footer_text="Tap the title to open Available Jobs — go claim it.",
                    ),
                )
            ),
            covers=(str(job.job_id),),
        )
        for job, date_line in alerts
    ]


def heartbeat_extras_for(
    unmatched: Sequence[str], budget: int = GAP_VALUE_BUDGET
) -> HeartbeatExtras:
    """The filter-gap report riding on the next heartbeat.

    Carries the school names the High-School filter did not recognise, which
    turns the filter-gap report into something that arrives on its own instead
    of needing someone to go looking for it. Empty when there is nothing to
    report, which keeps the library's own default heartbeat footer.

    `budget` is lowered by tests so truncation can be exercised on a handful of
    names instead of a sixty-name fixture. Production always uses the default.
    """
    if not unmatched:
        return HeartbeatExtras()

    # Filled newest-first and then flipped back to discovery order for display.
    # Newest, not first: this list accumulates for the life of the process, so
    # spending the budget from the front would give the earliest names ever seen
    # permanent ownership of every slot, and a newly discovered campus would
    # never appear.
    spendable = budget - _GAP_SUMMARY_RESERVE
    listed: list[str] = []
    used = 0
    for name in reversed(unmatched):
        # +3 for the "• " prefix and the newline joining it to the previous line.
        # Over-counting by one on the first line is deliberate slack.
        cost = len(name) + 3
        if used + cost > spendable:
            break
        listed.append(name)
        used += cost
    listed.reverse()

    if not listed:
        # One name longer than the entire budget. Show it clipped rather than
        # emitting a field whose only content is a summary of what is missing.
        listed = [unmatched[-1][:spendable]]

    rest = len(unmatched) - len(listed)
    value = "\n".join(f"• {name}" for name in listed)
    if rest > 0:
        value += f"\n• …and {rest} more"

    return HeartbeatExtras(
        fields=(Field(name="Schools not on the list", value=value, inline=False),),
        footer_text="If any of these are high schools, add them to HS_SCHOOLS.",
    )
