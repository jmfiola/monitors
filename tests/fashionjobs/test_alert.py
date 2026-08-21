from datetime import datetime

from fashionjobs.alert import discord_text, format_job_alert
from fashionjobs.types import FashionJob


def _job(
    *,
    job_id: int = 12000001,
    title: str = "Stage Assistant Produit",
    company: str = "MAISON EXEMPLE",
) -> FashionJob:
    return FashionJob(
        job_id=job_id,
        title=title,
        company=company,
        location="Paris",
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
        ("Published", "<t:1787341827:F> · <t:1787341827:R>"),
    ]
    assert message.covers == ("12000001",)
    assert message.payload.allowed_mentions_parse == ()


def test_source_markdown_is_escaped_and_everyone_is_disabled() -> None:
    message = format_job_alert(_job(title="**@everyone**", company="A_B`C"))
    assert message.payload.embeds[0].title == r"\*\*@everyone\*\*"
    assert message.payload.embeds[0].fields is not None
    assert message.payload.embeds[0].fields[0].value == r"A\_B\`C"
    assert message.payload.to_dict()["allowed_mentions"] == {"parse": []}


def test_discord_text_escapes_every_markdown_control_character() -> None:
    assert discord_text(r"\*_`~|", 20) == r"\\\*\_\`\~\|"


def test_escaped_title_and_fields_respect_discord_limits_without_dangling_escape() -> None:
    message = format_job_alert(_job(title="*" * 400, company="_" * 1500))
    embed = message.payload.embeds[0]
    assert len(embed.title) <= 256
    assert embed.title.endswith("…")
    assert not embed.title.endswith("\\")
    assert embed.fields is not None
    assert len(embed.fields[0].value) <= 1024
    assert embed.fields[0].value.endswith("…")
    assert not embed.fields[0].value.endswith("\\")
