"""Library types, and the contract an app implements.

`Field`, `Embed`, and `Payload` mirror Discord's webhook and embed-field shapes.
`Item` — whatever an app models — is never one of these: the library only ever
passes an item back to the app's own `key` and `render`.

Everything here is a leaf: this module imports nothing from the rest of the
library, which is what lets an app depend on the contract without pulling in the
loop or the transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

GREEN = 0x2ECC71
RED = 0xE74C3C
BLUE = 0x3498DB


class SourceBusy(Exception):
    """The upstream is busy, not broken.

    Raised by a source to mean "try again at the normal cadence". The runner holds
    its poll interval instead of escalating backoff, and leaves the escalation
    ladder where it was so a real fault arriving later still climbs from where it
    left off.

    Jeffco needs this because SmartFindExpress answers HTTP 400 while the account
    holder's own session is active: over the monitor's first ~59 hours all 34 of
    its 400s landed between 06:00 and midnight, with none across three nights of
    overnight polling. Escalating on those would blind the monitor for minutes
    precisely when someone is claiming the job the alert just announced.
    """


@dataclass(frozen=True)
class Field:
    """One embed field. `inline` defaults true because day-cards are inline."""

    name: str
    value: str
    inline: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "inline": self.inline}


@dataclass(frozen=True)
class Embed:
    title: str
    description: str | None
    color: int
    url: str | None = None
    fields: tuple[Field, ...] | None = None
    footer_text: str | None = None

    def to_dict(self) -> dict[str, Any]:
        # Key order mirrors the TypeScript object literals, so an unsorted dump
        # reads the same shape side by side. The parity harness sorts keys anyway,
        # but a human diffing by eye does not.
        out: dict[str, Any] = {"title": self.title}
        if self.url is not None:
            out["url"] = self.url
        if self.description is not None:
            out["description"] = self.description
        out["color"] = self.color
        if self.fields is not None:
            out["fields"] = [f.to_dict() for f in self.fields]
        if self.footer_text is not None:
            out["footer"] = {"text": self.footer_text}
        return out


@dataclass(frozen=True)
class Payload:
    embeds: tuple[Embed, ...]
    content: str | None = None
    allowed_mentions_parse: tuple[str, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict. Absent values are OMITTED keys, never `null`.

        `JSON.stringify` drops properties whose value is `undefined`, so emitting
        `"content": null` would diverge from the TypeScript payload both on the
        wire and in the differential dump. Discord also treats the two
        differently: a null `content` is a validation error, an absent one is a
        normal embed-only message.
        """
        out: dict[str, Any] = {}
        if self.content is not None:
            out["content"] = self.content
        out["embeds"] = [e.to_dict() for e in self.embeds]
        if self.allowed_mentions_parse is not None:
            out["allowed_mentions"] = {"parse": list(self.allowed_mentions_parse)}
        return out


@dataclass(frozen=True)
class Message:
    """One webhook post plus the item keys it announces.

    `covers` is the whole reason this type exists. Jeffco posts one message per
    job and, when a post fails, withholds exactly those jobs' keys from the
    baseline so the next tick re-alerts them — which the runner cannot do without
    knowing which keys each message announces. Melanzana's single message covers
    every fresh key, so one shape serves both.
    """

    payload: Payload
    covers: tuple[str, ...]


@dataclass(frozen=True)
class HeartbeatExtras:
    """Whatever an app wants to add to the next heartbeat.

    `footer_text` is not decoration. Jeffco's heartbeat carries the school names
    its filter did not recognise as a field, and *replaces the footer* with "If any
    of these are high schools, add them to HS_SCHOOLS." The field is the data; the
    footer is the thing to do about it. A bare `list[Field]` return cannot reach
    the footer, so the seam built for jeffco would not have fit jeffco.
    """

    fields: tuple[Field, ...] = ()
    footer_text: str | None = None


@dataclass(frozen=True)
class OpsLabels:
    """Per-app wording for the ops messages the library formats.

    Three fields, because three is exactly where the two existing apps' ops
    strings diverge: the monitor's name, the noun it counts ("slot(s)" vs "high
    school job(s)"), and the death alert's footer, which tells the reader what to
    do while the monitor is blind and is therefore app-specific advice.
    """

    name: str
    tracked_noun: str
    death_footer: str


class Monitor[Item](Protocol):
    """What an app must provide. Four methods, deliberately not six.

    `Item` is whatever the app models — a slot, a job, a posting. The library never
    inspects it; it only ever passes it back to `key` and `render`.

    Filtering is not here on purpose: `fetch()` returns only the items worth
    tracking, so jeffco's High-School predicate and its gap logging stay inside
    jeffco. Authentication is not here either — jeffco is the only app with any, and
    the pieces that look generic are entangled with one API's JWT.

    A class rather than free functions because both apps hold per-instance state:
    jeffco a token cache, a refresh margin, a lockout counter and an accumulated
    filter-gap set; melanzana none today.

    **Clock convention.** An implementation that needs the current time takes a
    `now_unix: Callable[[], int]` in its constructor, and `main.py` passes the SAME
    callable to the monitor and to `run_forever`. `fetch()` takes no argument (the
    spec's signature), so nothing enforces this — a test that freezes one clock and
    not the other will produce results that look inexplicable.
    """

    async def fetch(self) -> list[Item]:
        """Every item worth tracking, right now.

        May raise `SourceBusy` to mean "the upstream is busy, not broken", which
        holds the normal cadence instead of escalating backoff. Any other exception
        is a fault: the runner backs off and does not touch the baseline, because no
        data is not the same as "nothing available".
        """
        ...

    def key(self, item: Item) -> str:
        """The item's stable identity. This is what the baseline stores."""
        ...

    async def render(self, new: list[Item]) -> list[Message]:
        """Build the messages announcing `new`.

        Async, and allowed to perform I/O: jeffco fetches per-job detail after the
        diff to build each alert's date line, so enrichment is necessarily
        post-diff. Prefer degrading one message over raising — a raise here is
        treated as "these items were not announced", so they are withheld and
        retried next tick.

        Every fresh key should appear in some message's `covers`. Any that does not
        is withheld rather than banked, because a key banked without being announced
        is an item silently swallowed forever.
        """
        ...

    def heartbeat_extras(self) -> HeartbeatExtras:
        """Extra content for the next heartbeat.

        The library decides *when* a heartbeat is due; the app supplies anything
        extra. Called only when one is actually due, and a raise degrades to empty
        rather than propagating — an app-side error here must not take down the loop
        that reports the app is alive.
        """
        ...
