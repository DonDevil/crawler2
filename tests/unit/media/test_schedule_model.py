"""Probe/hash want decisions (design §5.1), stored forms, the stale-hash rule (§6.2),
CLI wiring and settings validation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from antipiracy_contracts.ids import MediaId
from antipiracy_contracts.models.media import MediaKind, ProbeStatus
from antipiracy_contracts.models.web import UrlRef
from pydantic import ValidationError

from crawler2.core.configuration import MediaSettings, Settings
from crawler2.media import cli
from crawler2.media.model import (
    SCHEME,
    HashOutcome,
    HashRecord,
    MediaLatest,
    ProbeRecord,
    Validators,
)
from crawler2.media.schedule import hash_due, hashable, probe_due
from crawler2.media.work import Job

NOW = datetime(2026, 10, 1, tzinfo=UTC)
WEEK, MONTH = timedelta(days=7), timedelta(days=30)
MEDIA = MediaId.of_locator(UrlRef.of("https://cdn.example/v.mp4").url)


def _probe(**kw: object) -> ProbeRecord:
    base: dict[str, object] = {
        "probe_id": "p1",
        "media_id": str(MEDIA),
        "locator_url": "https://cdn.example/v.mp4",
        "kind": MediaKind.VIDEO_FILE,
        "probed_at": NOW - timedelta(days=1),
        "probe_status": ProbeStatus.PROBED,
        "worker": "w",
        "fetch_outcome": "media",
        "content_type": "video/mp4",
        "total_length": 100,
        "etag": '"v1"',
        "range_honoured": True,
    }
    base.update(kw)
    return ProbeRecord(**base)  # type: ignore[arg-type]


def _hash(probe: ProbeRecord, **kw: object) -> HashRecord:
    base: dict[str, object] = {
        "hash_id": "h1",
        "media_id": probe.media_id,
        "locator_url": probe.locator_url,
        "hashed_at": NOW - timedelta(days=1),
        "outcome": HashOutcome.OK,
        "worker": "w",
        "probe_id": probe.probe_id,
        "validators": probe.validators,
        "digest": "sha256:" + "b" * 64,
        "byte_count": 100,
    }
    base.update(kw)
    return HashRecord(**base)  # type: ignore[arg-type]


def test_probe_due_per_media_and_interval() -> None:
    assert probe_due(None, NOW, WEEK)
    assert probe_due(MediaLatest(MEDIA), NOW, WEEK)
    fresh = MediaLatest(MEDIA, probe=_probe())
    assert not probe_due(fresh, NOW, WEEK)
    old = MediaLatest(MEDIA, probe=_probe(probed_at=NOW - timedelta(days=8)))
    assert probe_due(old, NOW, WEEK)


@pytest.mark.parametrize(
    ("kind", "ctype", "container", "status", "expected"),
    [
        (MediaKind.VIDEO_FILE, "video/mp4", "mp4", ProbeStatus.PROBED, True),
        (MediaKind.AUDIO_FILE, "audio/mpeg", None, ProbeStatus.PROBED, True),
        (MediaKind.HLS_MANIFEST, "application/vnd.apple.mpegurl", None, ProbeStatus.PROBED, False),
        (MediaKind.DASH_MANIFEST, "application/dash+xml", None, ProbeStatus.PROBED, False),
        (MediaKind.UNKNOWN, "text/html", None, ProbeStatus.PROBED, False),
        (MediaKind.UNKNOWN, "application/octet-stream", "webm", ProbeStatus.PROBED, True),
        (MediaKind.UNKNOWN, "video/webm", None, ProbeStatus.PROBED, True),
        (MediaKind.VIDEO_FILE, "video/mp4", "mp4", ProbeStatus.FORBIDDEN, False),
        (MediaKind.VIDEO_FILE, "text/html", None, ProbeStatus.NOT_MEDIA, False),
    ],
)
def test_hashable_kinds(
    kind: MediaKind, ctype: str, container: str | None, status: ProbeStatus, expected: bool
) -> None:
    probe = _probe(kind=kind, content_type=ctype, container=container, probe_status=status)
    assert hashable(probe) is expected


def test_hash_due_on_validator_change_and_absent_validators() -> None:
    probe = _probe()
    assert hash_due(probe, None, NOW, MONTH)
    done = MediaLatest(MEDIA, probe=probe, hash=_hash(probe), ok_hash=_hash(probe))
    assert not hash_due(probe, done, NOW, MONTH)  # same validators: nothing to do
    changed = _probe(etag='"v2"')
    assert hash_due(changed, done, NOW, MONTH)
    bare = _probe(total_length=None, etag=None, last_modified=None, declared_length=None)
    assert bare.validators.empty
    recent = MediaLatest(MEDIA, probe=bare, hash=_hash(bare))
    assert not hash_due(bare, recent, NOW, MONTH)
    stale = MediaLatest(MEDIA, probe=bare, hash=_hash(bare, hashed_at=NOW - timedelta(days=31)))
    assert hash_due(bare, stale, NOW, MONTH)
    failed = MediaLatest(MEDIA, probe=probe, hash=_hash(probe, outcome=HashOutcome.TIMEOUT))
    assert not hash_due(probe, failed, NOW, MONTH)
    old_fail = replace(
        failed, hash=_hash(probe, outcome=HashOutcome.TIMEOUT, hashed_at=NOW - MONTH)
    )
    assert hash_due(probe, old_fail, NOW, MONTH)


def test_stale_hash_is_never_attached() -> None:
    probe = _probe()
    ok = _hash(probe)
    latest = MediaLatest(MEDIA, probe=probe, hash=ok, ok_hash=ok)
    assert latest.current_content() == ok.content
    assert ok.content is not None
    assert ok.content.scheme == SCHEME == "full-bytes/v1"
    changed = replace(latest, probe=_probe(probe_id="p2", last_modified="Thu, 01 Oct 2026"))
    assert changed.current_content() is None  # validators differ from the hashed probe's
    assert replace(latest, probe=None).current_content() is None
    forbidden = replace(latest, probe=_probe(probe_status=ProbeStatus.FORBIDDEN))
    assert forbidden.current_content() is None
    failed = _hash(probe, outcome=HashOutcome.TOO_LARGE, digest=None)
    assert failed.content is None
    assert failed.content_id is None


def test_validators_of_full_and_ranged_responses() -> None:
    ranged = _probe(total_length=None, declared_length=65536, range_honoured=True)
    assert ranged.validators.length is None  # a 206 Content-Length is the slice, not the file
    full = _probe(total_length=None, declared_length=1000, range_honoured=False)
    assert full.validators.length == 1000
    assert full.hints().content_length == 1000
    assert Validators().empty


def test_records_round_trip() -> None:
    probe = _probe(head_sample_sha256=None, referer="https://site.example/p")
    assert ProbeRecord.from_json(probe.to_json()) == probe
    ok = _hash(probe, final_url="https://cdn2.example/v.mp4", redirect_count=1)
    assert HashRecord.from_json(ok.to_json()) == ok
    job = Job(str(MEDIA), probe.locator_url, "video_file", referer="r", probe_id="p1")
    assert Job.from_json(job.to_json()) == job


def test_cli_parser_and_settings() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["resolve", "accept-gap", "--note", "lost 3 days", "--by", "op"])
    assert args.func is cli.cmd_resolve_accept_gap
    assert parser.parse_args(["resolve", "status"]).func is cli.cmd_resolve_status
    with pytest.raises(SystemExit):
        parser.parse_args(["resolve", "accept-gap"])  # a note is mandatory
    config = cli.resolver_config(Settings())
    assert config.lease_ttl == timedelta(seconds=60)
    assert config.settle == timedelta(seconds=600)
    assert config.lag_critical == timedelta(days=7)
    assert cli.producer_for("h:media_probe:1:" + "0" * 32).component.value == "media_registry"
    m = MediaSettings()
    assert (m.hash_max_bytes, m.hash_timeout_s) == (8 * 1024**3, 3600.0)
    assert (m.reprobe_after_s, m.rehash_after_s) == (7 * 86400, 30 * 86400)
    assert (m.probe_concurrency, m.hash_concurrency, m.hash_per_host_concurrency) == (8, 2, 1)
    assert m.ssrf_allowed_networks == []
    with pytest.raises(ValidationError):
        MediaSettings(lag_warn_s=10, lag_critical_s=5)
    with pytest.raises(ValidationError):
        MediaSettings(lag_critical_s=20 * 86400)  # must alarm before retention expires
