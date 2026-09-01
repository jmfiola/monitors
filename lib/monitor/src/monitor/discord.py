"""Webhook transport, and the ops messages every app sends about itself.

Alert formatting is deliberately absent: melanzana renders one embed with day-card
fields, jeffco renders one message per job, and there is no shared shape between
them worth naming. Heartbeats and liveness alerts *are* shared, down to three
strings — see OpsLabels.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, Protocol

from monitor.health import HealthState
from monitor.types import BLUE, GREEN, RED, Embed, HeartbeatExtras, OpsLabels, Payload

#: Posts one payload to one webhook. The runner takes this rather than a client so
#: tests can drive failure without a transport, and so an app can wrap it.
Poster = Callable[[str, Payload], Awaitable[None]]

#: Posts an ops message. Its implementation must swallow its own errors.
StatusPoster = Callable[[Payload], Awaitable[None]]

#: Retryable statuses outside the 5xx range. Everything else in 4xx is the request's
#: own fault and will be rejected identically forever, so retrying it is a way of
#: never noticing.
RETRYABLE_STATUS = frozenset({408, 425, 429})

#: The payload was rejected, not the transport. Retrying an identical payload cannot
#: help, so the runner banks these keys to stop an infinite retry — see
#: format_delivery_failure. Deliberately narrow: a 401/403/404 means the WEBHOOK is
#: refused (rotated, revoked, deleted), which is transient in the only sense that
#: matters — someone can fix it — and banking every key while the ops message that
#: would report it goes to the same dead webhook is silent, total alert loss.
PAYLOAD_REJECTED_STATUS = frozenset({400, 413, 422})


class HttpResponse(Protocol):
    """The structural response surface shared Discord transport needs."""

    @property
    def status_code(self) -> int: ...


class HttpClient(Protocol):
    """The transport surface `post` needs, structurally.

    Deliberately not a concrete client: app wiring owns its HTTP transport, while
    this library owns Discord delivery and its security invariants once.
    """

    async def post(self, url: str, *, json: Any, headers: Mapping[str, str]) -> HttpResponse: ...


class DiscordPostError(Exception):
    """A webhook POST was rejected.

    Never carries the URL — it is a credential, and this text reaches the logs. It
    does carry the status, because the caller's response depends on it: a 429 is
    worth retrying and a 400 never will be.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code

    @property
    def retryable(self) -> bool:
        """Worth trying again.

        Unknown (a network error, no status at all) counts as retryable: the cautious
        reading is that the message may yet be delivered. **Any** 5xx counts too, not
        just the ones named in RETRYABLE_STATUS — Discord sits behind Cloudflare, whose
        520/521/522/523/524 are routine and are emphatically not "the request's own
        fault". Enumerating 5xx was the original bug: an unlisted one took the permanent
        branch, banking the item's key and swallowing the alert forever.
        """
        if self.status_code is None:
            return True
        if self.status_code >= 500:
            return True
        return self.status_code in RETRYABLE_STATUS


async def post(url: str, payload: Payload, client: HttpClient) -> None:
    """POST a payload to a Discord webhook. Raises on a non-2xx response."""
    response = await client.post(
        url, json=payload.to_dict(), headers={"content-type": "application/json"}
    )
    if not 200 <= response.status_code < 300:
        raise DiscordPostError(
            f"Discord webhook failed: HTTP {response.status_code}", response.status_code
        )


#: Module-level singleton so the default below is a name lookup, not a call — ruff
#: (B008) flags a function call in a default expression even for a frozen,
#: immutable dataclass like this one.
_DEFAULT_HEARTBEAT_EXTRAS = HeartbeatExtras()


def _when(unix: int) -> str:
    """An instant, rendered so it stays true after the message ages.

    Discord computes `<t:U:R>` — "2 minutes ago" — on the READER's clock at the
    moment they look, not at the moment it was posted. Ops messages persist in a
    channel and get scrolled back to, so a bare relative timestamp silently
    rewrites itself: a heartbeat that correctly said "last successful poll a few
    seconds ago" when it arrived reads "last successful poll 4 days ago" when it is
    read four days later. That is indistinguishable from a monitor that has been
    dead for four days, and it has already caused exactly that alarm.

    `<t:U:f>` is absolute and still localizes to the reader's timezone, so pairing
    the two keeps the at-a-glance freshness while leaving the fact recoverable.
    Collapsing this back to `<t:U:R>` alone looks like a tidy-up and reintroduces
    the misreport.
    """
    return f"<t:{unix}:f> (<t:{unix}:R>)"


def format_heartbeat(
    labels: OpsLabels,
    state: HealthState,
    now_unix: int,
    extras: HeartbeatExtras = _DEFAULT_HEARTBEAT_EXTRAS,
) -> Payload:
    """Heartbeat ops message — confirms the monitor is alive. Never pings.

    Timestamps render as absolute-with-relative, `<t:U:f> (<t:U:R>)`, not bare
    `<t:U:R>` — see `_when` for why the tidier-looking version misreports.

    `extras.footer_text` wins over the default when the app sets one: jeffco's
    heartbeat lists the schools its filter did not recognise and replaces the footer
    with what to do about them, which is the actionable half of that report.
    """
    uptime_hours = (now_unix - state.started_unix) // 3600
    return Payload(
        embeds=(
            Embed(
                title=f"💚 {labels.name} — still watching",
                description=(
                    f"Up {uptime_hours}h · tracking {state.items_tracked} "
                    f"{labels.tracked_noun} · last successful poll "
                    f"{_when(state.last_success_unix)}."
                ),
                color=BLUE,
                # None, not (), when the app adds nothing: an empty tuple would
                # emit `"fields": []`, which the TypeScript embed never does.
                fields=extras.fields or None,
                footer_text=extras.footer_text or "Routine heartbeat — no action needed.",
            ),
        )
    )


def format_status_alert(
    kind: Literal["death", "busy", "recovery"],
    labels: OpsLabels,
    state: HealthState,
    now_unix: int,
) -> Payload:
    """Liveness ops message. `death` = sustained inability to poll; `busy` = the same
    absence of data, but every attempt reported the source busy elsewhere; `recovery`
    = polling resumed. Never pings — only real item alerts do."""
    if kind == "busy":
        return Payload(
            embeds=(
                Embed(
                    title=f"⚠️ {labels.name} — no successful poll",
                    description=(
                        f"No successful poll since {_when(state.last_success_unix)}. "
                        f"Every attempt since then reported the source busy, which "
                        f"usually means the account is in use elsewhere — so this is "
                        f"probably someone working, not an outage. Still retrying; "
                        f"you'll get one more message when it recovers."
                    ),
                    color=RED,
                    footer_text="Liveness alert — no action needed unless it persists.",
                ),
            )
        )
    if kind == "death":
        return Payload(
            embeds=(
                Embed(
                    title=f"⚠️ {labels.name} — no successful poll",
                    description=(
                        f"No successful poll since {_when(state.last_success_unix)} "
                        f"({state.consecutive_failures} consecutive failures). "
                        f"Still retrying; you'll get one more message when it recovers."
                    ),
                    color=RED,
                    footer_text=labels.death_footer,
                ),
            )
        )
    return Payload(
        embeds=(
            Embed(
                title=f"✅ {labels.name} — recovered",
                description=f"Polling succeeded again at {_when(now_unix)}. Back to normal.",
                color=GREEN,
                footer_text="Liveness alert.",
            ),
        )
    )


def format_delivery_failure(labels: OpsLabels, status_code: int, item_count: int) -> Payload:
    """A permanently rejected alert. Never pings.

    Withholding a key assumes the next tick can succeed. When Discord rejects a
    payload for what it *is* — over the 6000-character total embed cap, most
    likely — every retry is rejected identically, so the runner banks the keys to
    stop the loop and posts this instead. Without it that path is indefinite silence
    underneath a heartbeat still reporting "still watching", which is the worst
    failure this project has: the one that looks like nothing happening.
    """
    return Payload(
        embeds=(
            Embed(
                title=f"⚠️ {labels.name} — an alert could not be delivered",
                description=(
                    f"Discord rejected an alert covering {item_count} "
                    f"{labels.tracked_noun} with HTTP {status_code}, which will not "
                    f"succeed on a retry. Those items are now recorded as seen and "
                    f"will NOT be announced. Polling is unaffected."
                ),
                color=RED,
                footer_text="Check the logs — this usually means the payload was too large.",
            ),
        )
    )
