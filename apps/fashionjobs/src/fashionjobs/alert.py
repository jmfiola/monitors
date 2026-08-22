from __future__ import annotations

from monitor.types import GREEN, Embed, Field, Message, Payload

from fashionjobs.types import FashionJob

_DISCORD_ESCAPED = frozenset("\\*_`~|[]()#>-+.!<")


def discord_text(value: str, limit: int) -> str:
    """Escape Discord Markdown while respecting a rendered field limit."""
    if limit <= 0:
        return ""

    escaped: list[str] = []
    length = 0
    for index, character in enumerate(value):
        piece = f"\\{character}" if character in _DISCORD_ESCAPED else character
        needs_ellipsis = index < len(value) - 1
        if length + len(piece) + int(needs_ellipsis) > limit:
            return "".join(escaped) + "…"
        escaped.append(piece)
        length += len(piece)
    return "".join(escaped)


def format_job_alert(job: FashionJob) -> Message:
    fields = [Field(name="Company", value=discord_text(job.company, 1024))]
    if job.location:
        fields.append(Field(name="Location", value=discord_text(job.location, 1024)))
    fields.append(Field(name="Contract", value=discord_text(job.contract, 1024)))
    embed = Embed(
        title=discord_text(job.title, 256),
        description=None,
        color=GREEN,
        url=job.url,
        fields=tuple(fields),
    )
    return Message(
        payload=Payload(embeds=(embed,), allowed_mentions_parse=()),
        covers=(str(job.job_id),),
    )
