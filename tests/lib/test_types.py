from monitor.types import BLUE, GREEN, RED, Embed, Field, HeartbeatExtras, Payload


def test_payload_omits_absent_content_and_mentions() -> None:
    # JSON.stringify drops undefined-valued properties, so emitting null here
    # would diverge from the TypeScript payload on the wire AND in the
    # differential dump. Discord also treats the two differently: a null content
    # is a validation error, an absent one is a normal embed-only message.
    payload = Payload(embeds=(Embed(title="t", description="d", color=1),))
    assert payload.to_dict() == {"embeds": [{"title": "t", "description": "d", "color": 1}]}


def test_payload_includes_the_everyone_pair_when_set() -> None:
    payload = Payload(
        embeds=(Embed(title="t", description="d", color=1),),
        content="@everyone",
        allowed_mentions_parse=("everyone",),
    )
    assert payload.to_dict() == {
        "content": "@everyone",
        "embeds": [{"title": "t", "description": "d", "color": 1}],
        "allowed_mentions": {"parse": ["everyone"]},
    }


def test_embed_omits_url_fields_and_footer_when_absent() -> None:
    assert Embed(title="t", description="d", color=1).to_dict() == {
        "title": "t",
        "description": "d",
        "color": 1,
    }


def test_embed_emits_an_empty_fields_array_when_given_one() -> None:
    # formatAlert always sets `fields`, even when the list is empty, so an empty
    # tuple must render as [] rather than being dropped.
    assert Embed(title="t", description="d", color=1, fields=()).to_dict()["fields"] == []


def test_embed_renders_url_fields_and_footer() -> None:
    embed = Embed(
        title="t",
        description="d",
        color=1,
        url="https://example.test/x",
        fields=(Field(name="n", value="v"),),
        footer_text="f",
    )
    assert embed.to_dict()["url"] == "https://example.test/x"
    assert embed.to_dict()["fields"] == [{"name": "n", "value": "v", "inline": True}]
    assert embed.to_dict()["footer"] == {"text": "f"}


def test_heartbeat_extras_defaults_to_adding_nothing() -> None:
    # The common case: an app with nothing to say. Both halves must be absent, so
    # format_heartbeat can tell "no fields" from "an empty fields array" and can
    # keep its own default footer.
    extras = HeartbeatExtras()
    assert extras.fields == ()
    assert extras.footer_text is None
    assert HeartbeatExtras(footer_text="do this instead").footer_text == "do this instead"


def test_the_colours_are_the_typescript_constants() -> None:
    # Asserted because they cross a module boundary now: melanzana.alert reads GREEN
    # from here, and a payload whose colour changed would fail the parity harness
    # with a diff that points at the wrong place.
    assert (GREEN, RED, BLUE) == (0x2ECC71, 0xE74C3C, 0x3498DB)
