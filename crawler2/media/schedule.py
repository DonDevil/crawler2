"""When a media wants a probe or a hash (design §5.1). Pure decisions + the resolver's sink.

- **Probe** per ``MediaId``, never per sighting: when the media has no probe,
  or its latest probe is older than ``reprobe_after`` (7 d).
- **Hash** after a probe: only for hashable kinds (``video_file``,
  ``audio_file``, or ``unknown`` whose probe shows a media type/container;
  manifests never, P8-D6), only for a ``probed`` media, and only when the
  last hash attempt is for other validators, or — without any validator —
  older than ``rehash_after`` (30 d). A failed attempt for the same
  validators is retried after ``rehash_after`` too.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from antipiracy_contracts.models.media import MediaKind, MediaObservation, ProbeStatus

from crawler2.core.observability import Metrics
from crawler2.media.model import HashOutcome, MediaLatest, ProbeRecord
from crawler2.media.work import Job, RedisWork

HASHED_KINDS = frozenset({MediaKind.VIDEO_FILE, MediaKind.AUDIO_FILE})
MANIFEST_KINDS = frozenset({MediaKind.HLS_MANIFEST, MediaKind.DASH_MANIFEST})


def probe_due(latest: MediaLatest | None, now: datetime, reprobe_after: timedelta) -> bool:
    probe = latest.probe if latest is not None else None
    return probe is None or now - probe.probed_at >= reprobe_after


def hashable(probe: ProbeRecord) -> bool:
    if probe.probe_status is not ProbeStatus.PROBED or probe.kind in MANIFEST_KINDS:
        return False
    if probe.kind in HASHED_KINDS:
        return True
    return probe.kind is MediaKind.UNKNOWN and probe.is_media


def hash_due(
    probe: ProbeRecord, latest: MediaLatest | None, now: datetime, rehash_after: timedelta
) -> bool:
    if not hashable(probe):
        return False
    last = latest.hash if latest is not None else None
    if last is None or last.validators != probe.validators:
        return True
    if last.outcome is not HashOutcome.OK or probe.validators.empty:
        return now - last.hashed_at >= rehash_after
    return False


class ProbeScheduler:
    """The resolver's sink: want a probe for a committed observation's media."""

    def __init__(
        self, work: RedisWork, *, reprobe_after: timedelta, metrics: Metrics | None = None
    ) -> None:
        self._work = work
        self._reprobe_after = reprobe_after
        self._wants = (
            metrics.counter(
                "media_wants_total", "Probe/hash wants, by stage and result", ["stage", "result"]
            )
            if metrics is not None
            else None
        )

    def observed(self, observation: MediaObservation, latest: MediaLatest | None) -> None:
        if not probe_due(latest, self._work.now(), self._reprobe_after):
            return
        media = observation.media
        queued = self._work.want(
            "probe",
            Job(
                media_id=str(media.media_id),
                locator_url=media.locator.url,
                kind=media.kind.value,
                referer=observation.page.url,
            ),
        )
        if self._wants is not None:
            self._wants.labels(stage="probe", result="queued" if queued else "already").inc()
