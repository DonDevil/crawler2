"""``crawler2-evidence``: P12 evidence collector, finalizer and case export.

``collect run`` consumes ``match.found`` (consumer group ``evidence-collector``)
into evidence candidates; any number of instances may run (E1 LWT + idempotent
upserts), and pending work survives restarts in the consumer group.
``collect refresh`` / ``collect rebuild`` re-derive candidates from the match
projections; they never change a finalized version. ``validate``,
``finalize`` and ``supersede`` act on one evidence item; ``finalize`` and
``supersede`` support ``--dry-run``. ``campaign`` manages case membership and
statistics; ``export`` writes a case package.

Exit codes: 0 ok · 1 not found / usage · 2 refused by validation ·
3 conflict with a finalized version · 4 export completed with failures.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from crawler2.core.configuration import Settings, WorkerRole
from crawler2.core.identity import WorkerIdentity
from crawler2.core.observability import Metrics, configure_logging

EXIT_NOT_FOUND, EXIT_REFUSED, EXIT_CONFLICT, EXIT_EXPORT_FAILED = 1, 2, 3, 4


class _Services:
    def __init__(self, settings: Settings, metrics: Metrics | None = None) -> None:
        from crawler2.evidence.collector import EvidenceCollector, EvidenceMetrics
        from crawler2.evidence.finalize import EvidenceFinalizer
        from crawler2.evidence.store import ScyllaEvidenceStore
        from crawler2.storage.objectstore import S3ObjectStore
        from crawler2.storage.scylla import ScyllaStorage

        self.identity = str(WorkerIdentity.create(settings.host_id, WorkerRole.FINALIZER))
        self.settings = settings
        self.storage = ScyllaStorage.open(settings.scylla, instance=self.identity)
        self.store = ScyllaEvidenceStore(self.storage.session)
        self.objects = S3ObjectStore(settings.minio, scratch_dir=settings.scratch_dir)
        self.metrics = EvidenceMetrics(metrics)
        cfg = settings.evidence
        self.collector = EvidenceCollector(
            evidence=self.storage.evidence,
            store=self.store,
            media=self.storage.media,
            pages=self.storage.pages,
            attempts=self.storage.fetch_attempts,
            projections=self.storage.projections,
            settings=cfg,
            actor=self.identity,
            metrics=self.metrics,
        )
        self.finalizer = EvidenceFinalizer(
            store=self.store,
            seals=self.storage.evidence,
            objects=self.objects,
            pages=self.storage.pages,
            producer_instance=self.identity,
            bucket=self.objects.bucket,
            max_artifact_bytes=cfg.max_artifact_bytes,
            metrics=self.metrics,
        )

    def close(self) -> None:
        self.objects.close()
        self.storage.close()


def _print(doc: Any) -> None:
    print(json.dumps(doc, sort_keys=True, default=str))


def _evidence_id(raw: str) -> Any:
    from antipiracy_contracts.ids import EvidenceId

    return EvidenceId(raw)


def _month(raw: str) -> datetime:
    return datetime.strptime(raw, "%Y-%m").replace(tzinfo=UTC)


def cmd_collect_run(args: Any, settings: Settings) -> int:
    from antipiracy_contracts.events.fingerprinting import MatchFound

    from crawler2.evidence.collector import EvidenceLoop
    from crawler2.storage.events.consumer import (
        IdempotentConsumer,
        RedisStreamReader,
        connect_stream_client,
    )
    from crawler2.storage.events.publisher import stream_name

    metrics = Metrics(settings)
    metrics.serve("evidence-collect")
    svc = _Services(settings, metrics)
    cfg = settings.evidence
    reader = RedisStreamReader(
        connect_stream_client(settings.redis),
        stream_name(settings.events, MatchFound.EVENT_TYPE, MatchFound.SCHEMA_MAJOR),
        cfg.consumer_group,
        svc.identity,
    )
    reader.ensure_group()
    consumer = IdempotentConsumer(
        cfg.consumer_group,
        MatchFound,
        svc.collector.handle,
        svc.storage.processed_events,
        metrics=metrics,
    )
    loop = EvidenceLoop(reader, consumer, settings=cfg, metrics=svc.metrics)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: loop.stop())
    try:
        loop.run(max_idle_polls=args.exit_when_idle)
    finally:
        svc.close()
    return 0


def _matches_for(svc: _Services, evidence_id: Any) -> Any:
    cand = svc.store.candidate(evidence_id)
    return cand.dossier.match if cand is not None else None


def cmd_collect_refresh(args: Any, settings: Settings) -> int:
    svc = _Services(settings)
    try:
        match = _matches_for(svc, _evidence_id(args.evidence_id))
        if match is None:
            _print({"error": "no such evidence candidate"})
            return EXIT_NOT_FOUND
        result, dossier = svc.collector.collect(match, refresh=args.finalized)
        _print({"result": result, "evidence_id": dossier.evidence_id, "status": dossier.status})
        return 0
    finally:
        svc.close()


def cmd_collect_rebuild(args: Any, settings: Settings) -> int:
    """Re-collect every match of a target in the P3-P5 projections (never finalized ones)."""
    from antipiracy_contracts.ids import TargetId

    from crawler2.evidence.stats import months

    svc = _Services(settings)
    counts: dict[str, int] = {}
    try:
        now = datetime.now(UTC)
        for month in months(now - timedelta(days=31 * (args.months - 1)), now):
            for summary in svc.storage.projections.matches_for_target(TargetId(args.target), month):
                full = {
                    str(m.match_id): m
                    for m in svc.storage.projections.matches_for_content(summary.content_id)
                }
                match = full.get(str(summary.match_id))
                if match is None:
                    counts["match_missing"] = counts.get("match_missing", 0) + 1
                    continue
                result, _ = svc.collector.collect(match)
                counts[result] = counts.get(result, 0) + 1
    finally:
        svc.close()
    _print({"target": args.target, "months": args.months, "results": counts})
    return 0


def _summary(cand: Any, versions: Any) -> dict[str, Any]:
    d = cand.dossier
    return {
        "evidence_id": d.evidence_id,
        "match_id": str(d.match.match_id),
        "target_id": str(d.match.target.target_id),
        "verdict": d.match.verdict.value,
        "confidence": d.match.confidence,
        "status": cand.status.value,
        "revision": cand.revision,
        "collected_at": d.collected_at.isoformat(),
        "updated_at": cand.updated_at.isoformat(),
        "sources": f"{len(d.sources)}/{d.sources_total}",
        "gaps": sorted({g.code.value for g in d.gaps}),
        "validation": cand.validation.outcome.value if cand.validation else None,
        "versions": [
            {"version": v.version, "status": v.status, "manifest": str(v.manifest_digest)}
            for v in versions
        ],
    }


def cmd_list(args: Any, settings: Settings) -> int:
    from antipiracy_contracts.ids import TargetId

    from crawler2.evidence.stats import months

    svc = _Services(settings)
    try:
        if args.campaign:
            campaign = svc.store.campaign(args.campaign)
            if campaign is None:
                _print({"error": "no such campaign"})
                return EXIT_NOT_FOUND
            targets, since = campaign.target_ids, campaign.since
        else:
            targets = (TargetId(args.target),)
            since = datetime.now(UTC) - timedelta(days=31 * (args.months - 1))
        seen: set[str] = set()
        for t in targets:
            for month in months(since, datetime.now(UTC)):
                for e in svc.storage.evidence.for_target(t, month):
                    seen.add(str(e.evidence_id))
        for raw in sorted(seen):
            cand = svc.store.candidate(_evidence_id(raw))
            if cand is None:
                _print({"evidence_id": raw, "status": "no candidate (E4 only)"})
                continue
            if args.status and cand.status.value != args.status:
                continue
            _print(_summary(cand, svc.store.versions(_evidence_id(raw))))
        return 0
    finally:
        svc.close()


def cmd_show(args: Any, settings: Settings) -> int:
    from crawler2.evidence.manifest import build_manifest
    from crawler2.evidence.model import ValidationOutcome

    svc = _Services(settings)
    try:
        eid = _evidence_id(args.evidence_id)
        cand = svc.store.candidate(eid)
        if cand is None:
            _print({"error": "no such evidence candidate"})
            return EXIT_NOT_FOUND
        doc = _summary(cand, svc.store.versions(eid))
        if args.audit:
            doc["audit"] = [
                {"at": a.at.isoformat(), "action": a.action, "actor": a.actor, "detail": a.detail}
                for a in svc.store.audit_log(eid)
            ]
        if args.detail:
            report = cand.validation
            if report is not None and report.outcome is not ValidationOutcome.REJECTED:
                # The redacted manifest view of the candidate as last validated.
                doc["detail"] = build_manifest(cand.dossier, report, version=0)
            else:
                doc["detail"] = {"gaps": [g.model_dump(mode="json") for g in cand.dossier.gaps]}
        _print(doc)
        return 0
    finally:
        svc.close()


def cmd_validate(args: Any, settings: Settings) -> int:
    from crawler2.evidence.finalize import NotFound
    from crawler2.evidence.model import ValidationOutcome

    svc = _Services(settings)
    try:
        report = svc.finalizer.validate(_evidence_id(args.evidence_id), record=not args.dry_run)
    except NotFound as exc:
        _print({"error": str(exc)})
        return EXIT_NOT_FOUND
    finally:
        svc.close()
    _print(
        {
            "outcome": report.outcome.value,
            "failed": [c.model_dump(mode="json") for c in report.failed],
            "artifacts": dict(sorted(Counter(a.state.value for a in report.artifacts).items())),
        }
    )
    return 0 if report.outcome is ValidationOutcome.COMPLETE else EXIT_REFUSED


def _finalize(args: Any, settings: Settings, *, supersede: bool) -> int:
    from crawler2.evidence.finalize import FinalizationRefused, NotFound
    from crawler2.storage.errors import ConflictError

    svc = _Services(settings)
    eid = _evidence_id(args.evidence_id)
    actor = f"{args.actor}@{svc.identity}"
    try:
        if supersede and args.recollect:
            match = _matches_for(svc, eid)
            if match is not None:
                svc.collector.collect(match, refresh=True)
        if args.dry_run:
            plan = svc.finalizer.plan(
                eid, supersede=supersede, reason=getattr(args, "reason", None)
            )
            _print(
                {
                    "dry_run": True,
                    "outcome": plan.report.outcome.value,
                    "failed": [c.check for c in plan.report.failed],
                    "version": plan.version,
                    "manifest_sha256": str(plan.digest) if plan.manifest else None,
                    "existing": str(plan.existing.manifest_digest) if plan.existing else None,
                    "idempotent": plan.idempotent,
                }
            )
            return 0
        if supersede:
            done = svc.finalizer.supersede(
                eid, reason=args.reason, allow_partial=args.allow_partial, actor=actor
            )
        else:
            done = svc.finalizer.finalize(eid, allow_partial=args.allow_partial, actor=actor)
    except NotFound as exc:
        _print({"error": str(exc)})
        return EXIT_NOT_FOUND
    except FinalizationRefused as exc:
        _print({"refused": str(exc)})
        return EXIT_REFUSED
    except ConflictError as exc:
        _print({"conflict": str(exc)})
        return EXIT_CONFLICT
    finally:
        svc.close()
    v = done.version
    _print(
        {
            "result": done.result,
            "evidence_id": str(v.evidence_id),
            "version": v.version,
            "status": v.status,
            "manifest_sha256": str(v.manifest_digest),
            "manifest_uri": v.manifest_uri,
            "finalized_at": v.finalized_at.isoformat(),
        }
    )
    return 0


def cmd_finalize(args: Any, settings: Settings) -> int:
    return _finalize(args, settings, supersede=False)


def cmd_supersede(args: Any, settings: Settings) -> int:
    return _finalize(args, settings, supersede=True)


def cmd_campaign_create(args: Any, settings: Settings) -> int:
    from antipiracy_contracts.ids import TargetId

    from crawler2.evidence.store import CAMPAIGN_ID, Campaign

    if not CAMPAIGN_ID.fullmatch(args.campaign_id):
        _print({"error": f"campaign id must match {CAMPAIGN_ID.pattern}"})
        return EXIT_NOT_FOUND
    svc = _Services(settings)
    try:
        if svc.store.campaign(args.campaign_id) is not None:
            _print({"error": "campaign exists; use add-target"})
            return EXIT_CONFLICT
        svc.store.create_campaign(
            Campaign(
                campaign_id=args.campaign_id,
                name=args.name,
                since=_month(args.since),
                created_at=datetime.now(UTC),
                target_ids=tuple(TargetId(t) for t in args.target),
            )
        )
    finally:
        svc.close()
    _print({"created": args.campaign_id})
    return 0


def cmd_campaign_add_target(args: Any, settings: Settings) -> int:
    from antipiracy_contracts.ids import TargetId

    svc = _Services(settings)
    try:
        if svc.store.campaign(args.campaign_id) is None:
            _print({"error": "no such campaign"})
            return EXIT_NOT_FOUND
        svc.store.add_campaign_target(args.campaign_id, TargetId(args.target), at=datetime.now(UTC))
    finally:
        svc.close()
    _print({"campaign": args.campaign_id, "added": args.target})
    return 0


def _campaign_data(svc: _Services, campaign_id: str) -> Any:
    from crawler2.evidence.stats import load_campaign

    campaign = svc.store.campaign(campaign_id)
    if campaign is None:
        return None
    return load_campaign(
        campaign,
        store=svc.store,
        index=svc.storage.evidence,
        projections=svc.storage.projections,
        until=datetime.now(UTC),
    )


def cmd_campaign_list(args: Any, settings: Settings) -> int:
    svc = _Services(settings)
    try:
        for cid in svc.store.campaigns():
            c = svc.store.campaign(cid)
            if c is not None:
                _print(
                    {
                        "campaign_id": cid,
                        "name": c.name,
                        "since": c.since.strftime("%Y-%m"),
                        "targets": [str(t) for t in c.target_ids],
                    }
                )
    finally:
        svc.close()
    return 0


def cmd_campaign_stats(args: Any, settings: Settings) -> int:
    from crawler2.evidence.stats import campaign_stats

    svc = _Services(settings)
    try:
        data = _campaign_data(svc, args.campaign_id)
        if data is None:
            _print({"error": "no such campaign"})
            return EXIT_NOT_FOUND
        _print(campaign_stats(data))
    finally:
        svc.close()
    return 0


def cmd_export(args: Any, settings: Settings) -> int:
    from crawler2.evidence.export import build_package, export_case

    cfg = settings.evidence
    svc = _Services(settings)
    try:
        data = _campaign_data(svc, args.campaign)
        if data is None:
            _print({"error": "no such campaign"})
            return EXIT_NOT_FOUND
        if args.dry_run:
            files, failures = build_package(
                data,
                objects=svc.objects,
                bucket=svc.objects.bucket,
                include_artifacts=not args.no_artifacts,
                max_artifact_bytes=cfg.max_artifact_bytes,
                max_package_bytes=cfg.max_package_bytes,
            )
            _print(
                {
                    "dry_run": True,
                    "files": len(files),
                    "bytes": sum(len(b) for b in files.values()),
                    "failures": list(failures),
                }
            )
            return EXIT_EXPORT_FAILED if failures else 0
        result = export_case(
            data,
            objects=svc.objects,
            bucket=svc.objects.bucket,
            out=Path(args.out),
            include_artifacts=not args.no_artifacts,
            archive=args.archive,
            max_artifact_bytes=cfg.max_artifact_bytes,
            max_package_bytes=cfg.max_package_bytes,
            metrics=svc.metrics,
        )
    except FileExistsError as exc:
        _print({"error": str(exc)})
        return EXIT_NOT_FOUND
    except OverflowError as exc:
        _print({"error": str(exc)})
        return EXIT_EXPORT_FAILED
    finally:
        svc.close()
    _print(
        {
            "directory": str(result.directory),
            "archive": str(result.archive) if result.archive else None,
            "case_sha256": str(result.case_digest),
            "evidence_items": result.evidence_items,
            "files": result.files,
            "failures": list(result.failures),
        }
    )
    return EXIT_EXPORT_FAILED if result.failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crawler2-evidence", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="match.found -> evidence candidates").add_subparsers(
        dest="action", required=True
    )
    p = collect.add_parser("run", help="consume match.found (consumer group)")
    p.add_argument("--exit-when-idle", type=int, default=None, metavar="POLLS")
    p.set_defaults(func=cmd_collect_run)
    p = collect.add_parser("refresh", help="re-resolve one candidate's provenance")
    p.add_argument("evidence_id")
    p.add_argument("--finalized", action="store_true", help="also re-resolve a finalized candidate")
    p.set_defaults(func=cmd_collect_refresh)
    p = collect.add_parser("rebuild", help="re-collect a target's matches from projections")
    p.add_argument("--target", required=True)
    p.add_argument("--months", type=int, default=3)
    p.set_defaults(func=cmd_collect_rebuild)

    p = sub.add_parser("list", help="evidence of a campaign or target")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--campaign")
    group.add_argument("--target")
    p.add_argument("--months", type=int, default=3)
    p.add_argument("--status", default=None)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="one candidate: status, versions, gaps")
    p.add_argument("evidence_id")
    p.add_argument("--audit", action="store_true")
    p.add_argument("--detail", action="store_true", help="redacted provenance detail")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("validate", help="check completeness and artifact hashes")
    p.add_argument("evidence_id")
    p.add_argument("--dry-run", action="store_true", help="do not store the result")
    p.set_defaults(func=cmd_validate)

    for name, func, help_ in (
        ("finalize", cmd_finalize, "freeze a validated candidate into version 1"),
        ("supersede", cmd_supersede, "append a corrected version (needs --reason)"),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("evidence_id")
        p.add_argument("--allow-partial", action="store_true")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--actor", default="operator", help="who is finalizing (audit)")
        if name == "supersede":
            p.add_argument("--reason", required=True)
            p.add_argument(
                "--no-recollect",
                dest="recollect",
                action="store_false",
                help="do not re-resolve provenance first",
            )
        p.set_defaults(func=func)

    campaign = sub.add_parser("campaign", help="case membership and statistics").add_subparsers(
        dest="action", required=True
    )
    p = campaign.add_parser("create")
    p.add_argument("campaign_id")
    p.add_argument("--name", required=True)
    p.add_argument("--since", required=True, metavar="YYYY-MM")
    p.add_argument("--target", action="append", default=[])
    p.set_defaults(func=cmd_campaign_create)
    p = campaign.add_parser("add-target")
    p.add_argument("campaign_id")
    p.add_argument("target")
    p.set_defaults(func=cmd_campaign_add_target)
    campaign.add_parser("list").set_defaults(func=cmd_campaign_list)
    p = campaign.add_parser("stats")
    p.add_argument("campaign_id")
    p.set_defaults(func=cmd_campaign_stats)

    p = sub.add_parser("export", help="write a case package")
    p.add_argument("--campaign", required=True)
    p.add_argument("--out", required=False, default=None)
    p.add_argument("--archive", action="store_true", help="also write <out>.tar.gz")
    p.add_argument("--no-artifacts", action="store_true", help="reference artifacts only")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    if getattr(args, "func", None) is cmd_export and not args.dry_run and not args.out:
        parser.error("export needs --out (or --dry-run)")
    settings = Settings()
    configure_logging(settings)
    result: int = args.func(args, settings)
    return result


if __name__ == "__main__":
    sys.exit(main())
