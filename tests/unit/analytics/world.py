"""SYNTHETIC P13 fixture world: source-table rows shaped exactly as the Scylla driver
returns them (uuid columns as ``uuid.UUID``, timestamps as naive UTC datetimes, ``doc``
columns as the stored contract JSON). Reserved ``.example`` hosts; nothing here is real
crawl or matcher output.

Scenario (hand-counted expectations in ``EXPECTED``):

| site            | pages (day)                        | notes                                 |
|-----------------|------------------------------------|---------------------------------------|
| site-a.example  | /watch/1 (d1, d2), /watch/2 (d1)   | media D1 (matched m1, alpha)          |
| site-b.example  | /watch/7 (d1), /watch/8 (d2)       | same template as site-a, no matches   |
| mirror.example  | /e/9 (d2); /go 301 → site-a /watch/2 | media D2 (matched m2, beta, probable) |
| cdn.example     | media host                         |                                       |

Extras: a page_observations row with a corrupt doc (rejected), a url_state row whose
URL has no host (unknown site), an observation after the export boundary.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from antipiracy_contracts.digests import ContentDigest
from antipiracy_contracts.models.matching import MatchVerdict
from antipiracy_contracts.models.web import RedirectHop, UrlRef

from crawler2.extraction.extract import extract
from tests.fixtures.contracts import (
    T0,
    content_key,
    fetch_attempt,
    match,
    media,
    media_observation,
    page_observation,
    target,
)

D1_DAY = T0  # 2026-06-01
D2_DAY = T0 + timedelta(days=1)  # 2026-06-02
BOUNDARY = T0 + timedelta(days=3)
LATE = T0 + timedelta(days=4)  # after the boundary: excluded by the window

C1, C2 = b"synthetic-content-one", b"synthetic-content-two"


def _naive(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(tzinfo=None)


def _html(title: str, links: list[str], media_urls: list[str], filler: int = 3) -> bytes:
    anchors = "".join(f'<a href="{u}">link {i}</a>' for i, u in enumerate(links))
    vids = "".join(f'<video src="{m}"></video>' for m in media_urls)
    paras = "".join(f"<p>paragraph {i} of {title}</p>" for i in range(filler))
    return (
        f"<html><head><title>{title}</title><meta property='og:type' content='video.movie'>"
        f"</head><body><nav>{anchors}</nav><main>{paras}{vids}</main></body></html>"
    ).encode()


class World:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {
            t: []
            for t in (
                "fetch_attempts",
                "page_observations",
                "page_extracts",
                "url_state",
                "domains",
                "page_revisions_by_url",
                "media",
                "media_observations",
                "targets",
                "matches_by_content",
                "evidence_candidates",
                "evidence_versions",
                "evidence_campaigns",
                "intel_source_profiles",
            )
        }
        self._domains: set[str] = set()
        self._urls: set[str] = set()

    # --- crawl side ---------------------------------------------------------------

    def _url(self, ref: UrlRef, at: datetime, status: int | None) -> None:
        if ref.url_id not in self._urls:
            self._urls.add(ref.url_id)
            self.tables["url_state"].append(
                {
                    "url_id": ref.url_id.uuid,
                    "url": ref.url,
                    "domain_id": ref.domain_id.uuid,
                    "first_seen": _naive(at),
                    "last_observed_at": _naive(at) if status else None,
                    "last_status": status,
                }
            )
        host = ref.url.split("/")[2]
        if ref.domain_id not in self._domains:
            self._domains.add(ref.domain_id)
            self.tables["domains"].append(
                {"domain_id": ref.domain_id.uuid, "host": host, "first_seen": _naive(at)}
            )

    def page(
        self,
        url: str,
        *,
        at: datetime,
        links: list[str] = (),  # type: ignore[assignment]
        media_urls: list[str] = (),  # type: ignore[assignment]
        content: dict[str, bytes] | None = None,
        redirected_from: str | None = None,
        status: int = 200,
    ) -> dict[str, Any]:
        ref = UrlRef.of(url)
        body = _html(url, list(links), list(media_urls))
        obs = page_observation(ref, at=at, body=body, status=status)
        if redirected_from is not None:
            src = UrlRef.of(redirected_from)
            obs = obs.model_copy(
                update={"requested": src, "redirects": (RedirectHop(location=ref, status=301),)}
            )
            self._url(src, at, 301)
        attempt = fetch_attempt(obs.requested, at=at - timedelta(seconds=1)).model_copy(
            update={"fetch_attempt_id": obs.fetch_attempt_id}
        )
        self.tables["fetch_attempts"].append(
            {"fetch_attempt_id": attempt.fetch_attempt_id.uuid, "doc": attempt.model_dump_json()}
        )
        self.tables["page_observations"].append(
            {"observation_id": obs.observation_id.uuid, "doc": obs.model_dump_json()}
        )
        self._url(ref, at, status)
        ex = extract(body, page=ref, content_type="text/html")
        for link in ex.links:
            self._url(link.target, at, None)
        extracts = self.tables["page_extracts"]
        if not any(r["page_version_id"] == ex.page_version_id.uuid for r in extracts):
            extracts.append(  # one extract per raw page version (its primary key)
                {
                    "page_version_id": ex.page_version_id.uuid,
                    "observed_at": _naive(at),
                    "doc": ex.model_dump_json(),
                }
            )
        for rev in self.tables["page_revisions_by_url"]:
            if (rev["url_id"], rev["revision_id"]) == (ref.url_id.uuid, ex.revision_id.uuid):
                rev |= {  # upsert of one primary key: last_* latest-wins
                    "last_seen": _naive(at),
                    "last_observation_id": obs.observation_id.uuid,
                    "last_page_version_id": ex.page_version_id.uuid,
                }
                break
        else:
            self._revision(ref, ex, obs, at)
        return self._media(obs, at, list(media_urls), content)

    def _revision(self, ref: UrlRef, ex: Any, obs: Any, at: datetime) -> None:
        self.tables["page_revisions_by_url"].append(
            {
                "url_id": ref.url_id.uuid,
                "revision_id": ex.revision_id.uuid,
                "normalization": ex.normalization,
                "normalized_digest": str(ex.hashes.normalized),
                "first_seen": _naive(at),
                "first_observation_id": obs.observation_id.uuid,
                "first_page_version_id": ex.page_version_id.uuid,
                "last_seen": _naive(at),
                "last_observation_id": obs.observation_id.uuid,
                "last_page_version_id": ex.page_version_id.uuid,
            }
        )

    def _media(
        self, obs: Any, at: datetime, media_urls: list[str], content: dict[str, bytes] | None
    ) -> dict[str, Any]:
        for m_url in media_urls:
            item = media(m_url.split("/", 3)[3], host=m_url.split("/")[2])
            data = (content or {}).get(m_url)
            mo = media_observation(item, obs, content=content_key(data) if data else None)
            if not any(r["media_id"] == item.media_id.uuid for r in self.tables["media"]):
                self.tables["media"].append(
                    {
                        "media_id": item.media_id.uuid,
                        "kind": str(item.kind),
                        "doc": item.model_dump_json(),
                        "first_seen": _naive(at),
                        "last_seen": _naive(at),
                    }
                )
            self.tables["media_observations"].append(
                {
                    "page_observation_id": obs.observation_id.uuid,
                    "media_id": item.media_id.uuid,
                    "observed_at": _naive(at),
                    "doc": mo.model_dump_json(),
                }
            )
        return {"observation": obs}

    # --- matcher / evidence side --------------------------------------------------

    def register(self, seed: str) -> Any:
        t = target(seed)
        self.tables["targets"].append(
            {
                "bucket": 0,
                "target_id": t.ref.target_id.uuid,
                "version": t.ref.version,
                "doc": t.model_dump_json(),
                "retired_at": None,
                "retire_reason": None,
            }
        )
        return t

    def match(
        self,
        data: bytes,
        t: Any,
        *,
        verdict: MatchVerdict = MatchVerdict.CONFIRMED,
        conf: float = 0.97,
    ) -> Any:
        m = match(content_key(data).content_id, t.ref, at=T0 + timedelta(days=2))
        m = m.model_copy(update={"verdict": verdict, "confidence": conf})
        self.tables["matches_by_content"].append(
            {
                "content_id": m.content_id.uuid,
                "match_id": m.match_id.uuid,
                "decided_at": _naive(m.decided_at),
                "doc": m.model_dump_json(),
            }
        )
        return m

    def evidence(
        self, m: Any, *, status: str, sources: list[tuple[Any, str]], finalized: str | None
    ) -> uuid.UUID:
        evidence_id = uuid.uuid5(uuid.NAMESPACE_URL, str(m.match_id))
        collected = T0 + timedelta(days=2, hours=1)
        doc = {
            "schema": "crawler2.evidence.candidate/v1",
            "evidence_id": f"evd_{evidence_id.hex}",
            "collected_at": collected.isoformat(),
            "match": json.loads(m.model_dump_json()),
            "media": [{"media_id": "x", "locator": "x", "kind": "video_file"}],
            "sources": [
                {
                    "page_observation_id": str(obs.observation_id),
                    "media_id": mid,
                    "media_locator": "https://cdn.example/x",
                    "basis": "content_verified",
                    "sighted_at": obs.observed_at.isoformat(),
                    "page_url": obs.requested.url,
                    "page_version_id": str(obs.page_version_id),
                    "observation_present": True,
                    "snapshot": None,
                }
                for obs, mid in sources
            ],
            "sources_total": len(sources),
            "gaps": [{"code": "screenshot_not_collected", "detail": "synthetic"}]
            + ([] if sources else [{"code": "unattributed", "detail": "synthetic"}]),
        }
        self.tables["evidence_candidates"].append(
            {
                "evidence_id": evidence_id,
                "match_id": m.match_id.uuid,
                "target_id": m.target.target_id.uuid,
                "status": status,
                "revision": 1,
                "collected_at": _naive(collected),
                "updated_at": _naive(collected),
                "doc": json.dumps(doc),
                "validation": json.dumps({"outcome": finalized}) if finalized else None,
                "validated_at": _naive(collected) if finalized else None,
            }
        )
        if finalized:
            self.tables["evidence_versions"].append(
                {
                    "evidence_id": evidence_id,
                    "version": 1,
                    "status": finalized,
                    "manifest_uri": "s3://synthetic/raw/sha256/00",
                    "manifest_digest": str(ContentDigest.of_bytes(b"manifest")),
                    "manifest_size": 100,
                    "finalized_at": _naive(collected + timedelta(hours=1)),
                    "supersedes": None,
                }
            )
        return evidence_id

    def campaign(self, campaign_id: str, targets: list[Any]) -> None:
        for t in targets:
            self.tables["evidence_campaigns"].append(
                {
                    "campaign_id": campaign_id,
                    "target_id": t.ref.target_id.uuid,
                    "name": "Synthetic campaign",
                    "since": _naive(T0),
                    "created_at": _naive(T0),
                    "added_at": _naive(T0),
                }
            )


A1 = "https://site-a.example/watch/1"
A2 = "https://site-a.example/watch/2"
B7 = "https://site-b.example/watch/7"
B8 = "https://site-b.example/watch/8"
MIRROR = "https://mirror.example/e/9"
MIRROR_GO = "https://mirror.example/go"
V1 = "https://cdn.example/v/1.mp4"
V2 = "https://cdn.example/v/2.mp4"
V3 = "https://cdn.example/v/3.mp4"


def build() -> tuple[World, dict[str, Any]]:
    w = World()
    p = {}
    p["a1d1"] = w.page(A1, at=D1_DAY, links=[A2, B7], media_urls=[V1], content={V1: C1})
    p["a1d2"] = w.page(
        A1, at=D2_DAY, links=[A2, B7, "https://other.example/x"], media_urls=[V1], content={V1: C1}
    )
    p["a2"] = w.page(
        A2, at=D1_DAY + timedelta(hours=1), links=[A1], media_urls=[V1], content={V1: C1}
    )
    p["b7"] = w.page(B7, at=D1_DAY + timedelta(hours=2), links=[B8], media_urls=[V3])
    p["b8"] = w.page(B8, at=D2_DAY + timedelta(hours=2), links=[B7], media_urls=[V3])
    p["mirror"] = w.page(
        MIRROR, at=D2_DAY + timedelta(hours=3), links=[A1], media_urls=[V2], content={V2: C2}
    )
    p["redirect"] = w.page(
        A2, at=D2_DAY + timedelta(hours=4), links=[A1, MIRROR], redirected_from=MIRROR_GO
    )
    p["late"] = w.page(B7, at=LATE, links=[B8], media_urls=[])
    w.tables["page_observations"].append({"observation_id": uuid.uuid4(), "doc": "{corrupt"})
    w.tables["url_state"].append(
        {
            "url_id": uuid.UUID(int=1),
            "url": "urn:synthetic:no-host",
            "domain_id": uuid.UUID(int=2),
            "first_seen": _naive(D1_DAY),
            "last_observed_at": None,
            "last_status": None,
        }
    )
    alpha, beta = w.register("alpha"), w.register("beta")
    gamma = target("gamma")  # in the campaign, never registered
    m1 = w.match(C1, alpha)
    m2 = w.match(C2, beta, verdict=MatchVerdict.PROBABLE, conf=0.74)
    m1_media = str(media("v/1.mp4").media_id)
    w.evidence(
        m1,
        status="finalized",
        sources=[(p["a1d1"]["observation"], m1_media), (p["a2"]["observation"], m1_media)],
        finalized="complete",
    )
    w.evidence(m2, status="incomplete", sources=[], finalized=None)
    w.campaign("synthetic-case", [alpha, beta, gamma])
    w.tables["intel_source_profiles"].append(
        {
            "domain_id": UrlRef.of(A1).domain_id.uuid,
            "generation": 1,
            "policy": "sources/v1",
            "through_bucket": 1,
            "updated_at": _naive(D2_DAY),
            "profile": json.dumps({"facts": 3}),
        }
    )
    return w, {"pages": p, "m1": m1, "m2": m2, "alpha": alpha, "beta": beta, "gamma": gamma}


def main(argv: list[str] | None = None) -> int:
    """Export the SYNTHETIC world to ``<root>`` (docs quick-start; never a real store)."""
    import sys
    from pathlib import Path

    from crawler2.analytics.export import Exporter, ExportOptions
    from crawler2.analytics.source import MemoryRowSource

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m tests.unit.analytics.world <analytics-root>", file=sys.stderr)
        return 1
    world, _ = build()
    result = Exporter(
        MemoryRowSource(world.tables, name="memory:synthetic-world"),
        Path(args[0]),
        options=ExportOptions(segments=4),
    ).export(until=BOUNDARY)
    print(f"{result.outcome} export_key={result.export_key} revision={result.revision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
