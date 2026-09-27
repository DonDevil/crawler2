import uuid

import pytest
from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.ids import (
    ContentId,
    DomainId,
    EventId,
    InvalidIdError,
    MediaId,
    PageVersionId,
    RepresentationId,
    TargetId,
    TypedId,
    UrlId,
)
from antipiracy_contracts.urls import canonicalize_url
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel, ValidationError

ALL_ID_TYPES = [cls for base in TypedId.__subclasses__() for cls in base.__subclasses__()]


def test_prefixes_are_unique_and_short() -> None:
    prefixes = [cls.PREFIX for cls in ALL_ID_TYPES]
    assert len(prefixes) == len(set(prefixes))
    assert all(p.isalpha() and p.islower() and len(p) == 3 for p in prefixes)


def test_derived_ids_are_pinned() -> None:
    # Derivation is part of the contract: these values must never change within major 1,
    # because durable data in both services is keyed by them.
    url = canonicalize_url("https://example.com/")
    assert UrlId.of(url) == "url_9c48239fd2ab87fe9466760835873109"
    assert DomainId.of_url(url) == "dom_70b439b462a48b9a9c921496064f8fac"
    assert MediaId.of_locator(url) == "med_986baea5df228b1d92f5e22e0b128eeb"
    digest = ContentDigest.of_bytes(b"")
    assert ContentId.of("sampled-bytes/v1", digest) == "cnt_21ff354dd34f8b99bec22c6fdf57009f"


def test_same_input_different_entity_types_never_collide() -> None:
    url = canonicalize_url("https://example.com/v.mp4")
    assert UrlId.of(url).uuid != MediaId.of_locator(url).uuid


def test_derivation_input_encoding_is_injective() -> None:
    digest = ContentDigest.of_bytes(b"cfg")
    content = ContentId.of("s/v1", digest)
    assert RepresentationId.for_content(content, "ab", "c", digest) != (
        RepresentationId.for_content(content, "a", "bc", digest)
    )


@given(st.from_regex(r"https?://[a-z]{1,10}\.example/[a-z0-9/]{0,20}", fullmatch=True))
def test_url_ids_are_deterministic_and_url_specific(raw: str) -> None:
    url = canonicalize_url(raw)
    assert UrlId.of(url) == UrlId.of(canonicalize_url(raw))
    assert UrlId.of(url) != UrlId.of(canonicalize_url(url + "x"))


def test_page_version_depends_on_url_and_bytes() -> None:
    a = UrlId.of(canonicalize_url("https://a.example/"))
    b = UrlId.of(canonicalize_url("https://b.example/"))
    body = ContentDigest.of_bytes(b"same")
    assert PageVersionId.of(a, body) == PageVersionId.of(a, body)
    assert PageVersionId.of(a, body) != PageVersionId.of(b, body)
    assert PageVersionId.of(a, body) != PageVersionId.of(a, ContentDigest.of_bytes(b"other"))


def test_uuid_versions() -> None:
    assert UrlId.of(canonicalize_url("https://example.com/")).uuid.version == 8
    allocated = EventId.new()
    assert allocated.uuid.version == 7
    assert allocated.uuid.variant == uuid.RFC_4122
    assert EventId.from_uuid(allocated.uuid) == allocated


def test_allocated_ids_are_unique() -> None:
    assert len({TargetId.new() for _ in range(1000)}) == 1000


@pytest.mark.parametrize(
    "value",
    [
        "",
        "tgt_",
        "TGT_0192a0b3c4d570018000000000000001",
        "tgt_0192a0b3c4d570018000000000000001x",
        "tgt_0192A0B3C4D570018000000000000001",  # uppercase hex
        "tgt_0192a0b3c4d580018000000000000001",  # v8 where v7 required
        "tgt_0192a0b3c4d570010000000000000001",  # wrong variant
        "evt_0192a0b3c4d570018000000000000001",  # another type's prefix
    ],
)
def test_malformed_or_foreign_ids_are_rejected(value: str) -> None:
    with pytest.raises(InvalidIdError):
        TargetId(value)


class _Holder(BaseModel):
    target_id: TargetId
    url_id: UrlId


def test_ids_serialize_as_plain_strings_and_parse_back() -> None:
    holder = _Holder(target_id=TargetId.new(), url_id=UrlId.of(canonicalize_url("https://e.x/")))
    restored = _Holder.model_validate_json(holder.model_dump_json())
    assert restored == holder
    assert type(restored.target_id) is TargetId
    with pytest.raises(ValidationError):
        _Holder.model_validate({"target_id": holder.url_id, "url_id": holder.url_id})
