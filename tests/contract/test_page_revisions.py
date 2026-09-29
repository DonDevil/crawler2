"""Contract 1.1 additions (P5, ADR-020): page revisions and ``page.changed``."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.events.web import PageChanged
from antipiracy_contracts.ids import ObservationId, PageRevisionId, PageVersionId
from antipiracy_contracts.models.web import PageHashes, UrlRef
from pydantic import ValidationError

PAGE = UrlRef.of("https://site.example/watch/1")
NORM = "html-normalized/v1"


def _digest(text: str) -> ContentDigest:
    return ContentDigest.of_bytes(text.encode())


def _hashes(raw: str = "raw", normalized: str = "n") -> PageHashes:
    return PageHashes(
        raw=_digest(raw),
        normalized=_digest(normalized),
        visible_text=_digest("t"),
        link_set=_digest("l"),
        media_set=_digest("m"),
        structural=_digest("s"),
    )


def _changed(hashes: PageHashes, **overrides: object) -> PageChanged:
    fields: dict[str, object] = {
        "page_observation_id": ObservationId.new(),
        "page": PAGE,
        "page_version_id": PageVersionId.of(PAGE.url_id, hashes.raw),
        "revision_id": PageRevisionId.of(PAGE.url_id, NORM, hashes.normalized),
        "normalization": NORM,
        "hashes": hashes,
        "observed_at": datetime(2026, 9, 28, tzinfo=UTC),
    }
    return PageChanged.model_validate(fields | overrides)


def test_revision_id_depends_on_normalized_content_not_raw_bytes() -> None:
    a, b = _hashes(raw="ads-1"), _hashes(raw="ads-2")
    assert _changed(a).revision_id == _changed(b).revision_id
    assert _changed(a).page_version_id != _changed(b).page_version_id
    assert _changed(_hashes(normalized="other")).revision_id != _changed(a).revision_id


def test_revision_id_is_scoped_by_url_and_normalization() -> None:
    other = UrlRef.of("https://site.example/watch/2")
    digest = _digest("n")
    assert PageRevisionId.of(PAGE.url_id, NORM, digest) != PageRevisionId.of(
        other.url_id, NORM, digest
    )
    assert PageRevisionId.of(PAGE.url_id, NORM, digest) != PageRevisionId.of(
        PAGE.url_id, "html-normalized/v2", digest
    )
    assert PageRevisionId.of(PAGE.url_id, NORM, digest).startswith("pgr_")


def test_mismatched_ids_are_rejected() -> None:
    hashes = _hashes()
    with pytest.raises(ValidationError, match="revision_id"):
        _changed(hashes, revision_id=PageRevisionId.of(PAGE.url_id, NORM, _digest("x")))
    with pytest.raises(ValidationError, match="page_version_id"):
        _changed(hashes, page_version_id=PageVersionId.of(PAGE.url_id, _digest("x")))
    with pytest.raises(ValidationError):
        _changed(hashes, normalization="Not A Scheme")
