from datetime import datetime

from fashionjobs.alert import discord_text, format_job_alert
from fashionjobs.types import FashionJob


def _job(
    *,
    job_id: int = 12000001,
    title: str = "Stage Assistant Produit",
    company: str = "MAISON EXEMPLE",
    location: str = "Paris",
) -> FashionJob:
    return FashionJob(
        job_id=job_id,
        title=title,
        company=company,
        location=location,
        contract="Stage",
        published_at=datetime.fromisoformat("2026-08-21T21:50:27+02:00"),
        url=f"https://fr.fashionjobs.com/emploi/Stage-assistant-produit,{job_id}.html",
    )


def test_alert_contains_every_reliable_job_field() -> None:
    message = format_job_alert(_job())
    embed = message.payload.embeds[0]
    assert embed.title == "Stage Assistant Produit"
    assert embed.url is not None
    assert embed.url.endswith(",12000001.html")
    assert [(field.name, field.value) for field in embed.fields or ()] == [
        ("Company", "MAISON EXEMPLE"),
        ("Location", "Paris"),
        ("Contract", "Stage"),
        ("Published", "<t:1787341827:F>"),
    ]
    assert message.covers == ("12000001",)
    assert message.payload.allowed_mentions_parse == ()


def test_alert_omits_blank_location_without_empty_fields() -> None:
    message = format_job_alert(_job(location=""))
    embed = message.payload.embeds[0]

    assert [(field.name, field.value) for field in embed.fields or ()] == [
        ("Company", "MAISON EXEMPLE"),
        ("Contract", "Stage"),
        ("Published", "<t:1787341827:F>"),
    ]
    assert all(field.name and field.value for field in embed.fields or ())


def test_alert_publication_time_is_fixed_not_relative() -> None:
    message = format_job_alert(_job())
    published = next(
        field for field in message.payload.embeds[0].fields or () if field.name == "Published"
    )

    assert published.value == "<t:1787341827:F>"
    assert ":R>" not in published.value


def test_source_markdown_is_escaped_and_everyone_is_disabled() -> None:
    message = format_job_alert(_job(title="**@everyone**", company="A_B`C"))
    assert message.payload.embeds[0].title == r"\*\*@everyone\*\*"
    assert message.payload.embeds[0].fields is not None
    assert message.payload.embeds[0].fields[0].value == r"A\_B\`C"
    assert message.payload.to_dict()["allowed_mentions"] == {"parse": []}


def test_discord_text_escapes_every_markdown_control_character() -> None:
    assert discord_text(r"\*_`~|[]()#>-+.!<", 50) == r"\\\*\_\`\~\|\[\]\(\)\#\>\-\+\.\!\<"


def test_source_masked_links_and_images_are_escaped() -> None:
    message = format_job_alert(
        _job(
            title="[Apply](https://example.test)",
            company="![Look](https://images.test/job)",
        )
    )
    embed = message.payload.embeds[0]

    assert embed.title == r"\[Apply\]\(https://example\.test\)"
    assert embed.fields is not None
    assert embed.fields[0].value == r"\!\[Look\]\(https://images\.test/job\)"


def test_source_headings_blockquotes_and_lists_are_escaped() -> None:
    message = format_job_alert(_job(location="# Lead\n> Paris\n- Ready\n+ Fast\n1. Today"))
    embed = message.payload.embeds[0]

    assert embed.fields is not None
    assert embed.fields[1].value == "\\# Lead\n\\> Paris\n\\- Ready\n\\+ Fast\n1\\. Today"


def test_source_autolinks_and_mentions_are_escaped_without_enabling_mentions() -> None:
    message = format_job_alert(_job(title="<https://example.test> <@123> <!channel>"))

    assert message.payload.embeds[0].title == (r"\<https://example\.test\> \<@123\> \<\!channel\>")
    assert message.payload.allowed_mentions_parse == ()
    assert message.payload.to_dict()["allowed_mentions"] == {"parse": []}


def test_escaped_title_and_fields_respect_discord_limits_without_dangling_escape() -> None:
    message = format_job_alert(_job(title="[" * 400, company="<" * 1500))
    embed = message.payload.embeds[0]
    assert embed.title == r"\[" * 127 + "…"
    assert len(embed.title) <= 256
    assert embed.title.endswith("…")
    assert not embed.title.endswith("\\")
    assert embed.fields is not None
    assert embed.fields[0].value == r"\<" * 511 + "…"
    assert len(embed.fields[0].value) <= 1024
    assert embed.fields[0].value.endswith("…")
    assert not embed.fields[0].value.endswith("\\")
