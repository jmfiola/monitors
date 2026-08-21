from dataclasses import replace

import httpx
import pytest
from monitor.discord import (
    DiscordPostError,
    format_delivery_failure,
    format_heartbeat,
    format_status_alert,
    post,
)
from monitor.health import init_health
from monitor.types import BLUE, GREEN, RED, Embed, Field, HeartbeatExtras, OpsLabels, Payload

LABELS = OpsLabels(
    name="Melanzana monitor",
    tracked_noun="slot(s)",
    death_footer="Liveness alert — the monitor may be blocked or down.",
)
WEBHOOK = "https://discord.test/webhook"


def _payload() -> Payload:
    return Payload(embeds=(Embed(title="t", description="d", color=GREEN),))


async def test_post_sends_the_payload_as_json() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content.decode()
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await post(WEBHOOK, _payload(), client)

    assert seen["url"] == WEBHOOK
    assert '"title": "t"' in str(seen["body"]) or '"title":"t"' in str(seen["body"])


async def test_post_raises_on_a_non_2xx_response_and_keeps_the_status() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(429))
    ) as client:
        with pytest.raises(DiscordPostError, match="429") as exc:
            await post(WEBHOOK, _payload(), client)
    assert exc.value.status_code == 429


def test_retryable_covers_rate_limits_5xx_and_unknown_but_not_a_bad_request() -> None:
    # The whole point of carrying the status: a 429 will succeed later, a 400 never
    # will, and a network error with no status gets the cautious reading.
    assert DiscordPostError("x", 429).retryable is True
    assert DiscordPostError("x", 503).retryable is True
    assert DiscordPostError("x", None).retryable is True
    assert DiscordPostError("x", 400).retryable is False
    assert DiscordPostError("x", 404).retryable is False
    # 408 Request Timeout and 425 Too Early are in RETRYABLE_STATUS deliberately:
    # both are transient timing signals rather than "the request's own fault", which
    # is the distinction that decides whether the runner withholds an item's key or
    # banks it. Pinned here so the frozenset's contents are asserted, not inferred.
    assert DiscordPostError("x", 408).retryable is True
    assert DiscordPostError("x", 425).retryable is True
    # Cloudflare sits in front of Discord and 520-524 are routine. Enumerating 5xx made
    # every unlisted one permanent, which banks the item's key and swallows the alert.
    for status in (520, 521, 522, 523, 524, 599):
        assert DiscordPostError("x", status).retryable is True


async def test_post_accepts_any_client_with_the_right_shape() -> None:
    # Proves the transport is structural: this stands in for curl_cffi, which
    # fashionjobs needs and which is not an httpx.AsyncClient.
    seen: dict[str, object] = {}

    class FakeResponse:
        status_code = 204

    class NotHttpx:
        async def post(self, url: str, *, json: object, headers: object) -> FakeResponse:
            seen["url"] = url
            seen["json"] = json
            return FakeResponse()

    await post(WEBHOOK, _payload(), NotHttpx())
    assert seen["url"] == WEBHOOK
    assert seen["json"] == _payload().to_dict()


async def test_post_error_never_contains_the_webhook_url() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(500))
    ) as client:
        with pytest.raises(DiscordPostError) as exc:
            await post("https://discord.test/secret-path", _payload(), client)
    assert "secret-path" not in str(exc.value)


def test_heartbeat_matches_melanzanas_wording_and_never_pings() -> None:
    state = replace(init_health(1000), items_tracked=7, last_success_unix=1500)
    payload = format_heartbeat(LABELS, state, 1000 + 3600)
    assert payload.content is None
    assert payload.allowed_mentions_parse is None
    embed = payload.embeds[0]
    assert embed.title == "💚 Melanzana monitor — still watching"
    assert embed.description == ("Up 1h · tracking 7 slot(s) · last successful poll <t:1500:R>.")
    assert embed.color == BLUE
    assert embed.footer_text == "Routine heartbeat — no action needed."
    # No fields key at all when the app has nothing to add — matches the
    # TypeScript embed, which never sets one.
    assert embed.fields is None
    assert "fields" not in payload.to_dict()["embeds"][0]


def test_heartbeat_carries_the_apps_extra_fields() -> None:
    # Jeffco's heartbeat reports the school names its filter did not recognise.
    # The library decides *when*; the app supplies the content.
    gap = Field(name="Schools not on the list", value="• X", inline=False)
    payload = format_heartbeat(LABELS, init_health(1000), 1000, HeartbeatExtras(fields=(gap,)))
    assert payload.embeds[0].fields == (gap,)
    # A field without a footer change keeps the default wording.
    assert payload.embeds[0].footer_text == "Routine heartbeat — no action needed."


def test_an_app_can_replace_the_heartbeat_footer() -> None:
    # This is the case a list[Field] return could not express: jeffco swaps the
    # footer for the instruction that makes the gap report actionable.
    payload = format_heartbeat(
        LABELS,
        init_health(1000),
        1000,
        HeartbeatExtras(
            fields=(Field(name="Schools not on the list", value="• X", inline=False),),
            footer_text="If any of these are high schools, add them to HS_SCHOOLS.",
        ),
    )
    assert payload.embeds[0].footer_text == (
        "If any of these are high schools, add them to HS_SCHOOLS."
    )


def test_a_delivery_failure_alert_says_the_items_were_abandoned_and_never_pings() -> None:
    payload = format_delivery_failure(LABELS, 400, 3)
    assert payload.content is None
    assert payload.allowed_mentions_parse is None
    embed = payload.embeds[0]
    assert "HTTP 400" in embed.description
    assert "3 slot(s)" in embed.description
    # The reader has to understand that these items are gone, not delayed.
    assert "will NOT be announced" in embed.description
    assert embed.color == RED


def test_death_alert_matches_melanzanas_wording_and_never_pings() -> None:
    state = replace(init_health(1000), last_success_unix=1000, consecutive_failures=4)
    payload = format_status_alert("death", LABELS, state, 2000)
    assert payload.content is None
    assert payload.allowed_mentions_parse is None
    embed = payload.embeds[0]
    assert embed.title == "⚠️ Melanzana monitor — no successful poll"
    assert embed.description == (
        "No successful poll since <t:1000:R> (4 consecutive failures). "
        "Still retrying; you'll get one more message when it recovers."
    )
    assert embed.color == RED
    assert embed.footer_text == "Liveness alert — the monitor may be blocked or down."


def test_a_busy_alert_names_the_cause_instead_of_claiming_the_monitor_is_down() -> None:
    state = replace(init_health(1000), last_success_unix=1000, consecutive_failures=40)
    payload = format_status_alert("busy", LABELS, state, 5000)
    embed = payload.embeds[0]
    assert payload.content is None  # ops messages never ping
    assert "in use elsewhere" in embed.description
    assert "blocked or down" not in embed.description
    assert embed.color == RED


def test_recovery_alert_matches_melanzanas_wording_and_never_pings() -> None:
    payload = format_status_alert("recovery", LABELS, init_health(1000), 2100)
    assert payload.content is None
    assert payload.allowed_mentions_parse is None
    embed = payload.embeds[0]
    assert embed.title == "✅ Melanzana monitor — recovered"
    assert embed.description == "Polling succeeded again <t:2100:R>. Back to normal."
    assert embed.color == GREEN
    assert embed.footer_text == "Liveness alert."
