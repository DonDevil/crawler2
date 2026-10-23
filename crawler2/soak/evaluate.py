"""Judge a run: one verdict, and for every criterion its status, evidence and next action.

Verdicts (strongest first): ``INVALIDATED`` (code/config changed), ``INTERRUPTED`` (did not
complete its required duration), ``FAIL``, ``INCONCLUSIVE`` (required evidence missing or a
limit not decided), ``PASS``. A run still going is ``RUNNING`` and has no verdict yet.

Rules that are *not* configurable because the plan fixes them: zero unaccounted-for work,
zero duplicate business effects, zero evidence integrity failures. Every other limit comes
from ``Thresholds``; ``None`` there means "owner has not decided" and yields INCONCLUSIVE
when the criterion would otherwise need it - never a silent pass.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from crawler2.soak.config import REQUIRED_DURATION_H, SoakConfig
from crawler2.soak.reconcile import EdgeResult, EvidenceResult


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class Verdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    INTERRUPTED = "INTERRUPTED"
    INVALIDATED = "INVALIDATED"
    RUNNING = "RUNNING"


@dataclass
class Criterion:
    id: str
    title: str
    status: Status
    detail: str
    category: str = "system"
    """``system`` (our code), ``environment`` (host/dependency), ``measurement`` (observability gap)."""
    evidence: dict[str, Any] = field(default_factory=dict)
    action: str = ""


@dataclass
class Evaluation:
    verdict: Verdict
    m6_eligible: bool
    scope: str
    criteria: list[Criterion]
    summary: dict[str, Any]
    banner: str

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["verdict"] = self.verdict.value
        for c in d["criteria"]:
            c["status"] = c["status"].value if hasattr(c["status"], "value") else c["status"]
        return d


@dataclass
class Inputs:
    config: SoakConfig
    run: dict[str, Any]
    checkpoint: dict[str, Any]
    samples: list[dict[str, Any]]
    events: list[dict[str, Any]]
    corrupt_sample_lines: int
    edges: list[EdgeResult]
    stage_counts: dict[str, int]
    redeliveries: dict[str, int]
    tail_gaps: list[dict[str, Any]]
    tail_errors: int
    evidence: EvidenceResult | None
    finalized_evidence: int
    now: float


# --- helpers ---------------------------------------------------------------------------


def series_values(samples: list[dict[str, Any]], name: str) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for s in samples:
        v = s.get("values", {}).get(name)
        if isinstance(v, int | float) and math.isfinite(v):
            out.append((float(s["ts"]), float(v)))
    return out


def slope_per_hour(points: list[tuple[float, float]]) -> float | None:
    if len(points) < 2:
        return None
    t0 = points[0][0]
    xs = [(t - t0) / 3600 for t, _ in points]
    ys = [v for _, v in points]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / den


def growth_ratio(points: list[tuple[float, float]]) -> float:
    n = max(1, len(points) // 10)
    head = statistics.fmean(v for _, v in points[:n])
    tail = statistics.fmean(v for _, v in points[-n:])
    base = abs(statistics.median(v for _, v in points)) or 1.0
    return (tail - head) / base


def down_runs(points: list[tuple[float, float]], interval_s: float) -> list[float]:
    """Durations (s) of consecutive ``up == 0`` stretches."""
    runs: list[float] = []
    start: float | None = None
    last_down = 0.0
    for ts, v in points:
        if v < 0.5:
            if start is None:
                start = ts
            last_down = ts
        elif start is not None:
            runs.append(max(ts - start, interval_s))
            start = None
    if start is not None:
        runs.append(max(last_down - start + interval_s, interval_s))
    return runs


def _c(id_: str, title: str, status: Status, detail: str, **kw: Any) -> Criterion:
    return Criterion(id_, title, status, detail, **kw)


# --- criteria ----------------------------------------------------------------------------


def evaluate(inp: Inputs) -> Evaluation:
    cfg = inp.config
    th = cfg.thresholds
    required_s = cfg.duration_h * 3600
    samples = inp.samples
    interval = cfg.sample_interval_s
    criteria: list[Criterion] = []

    first = samples[0]["ts"] if samples else None
    last = samples[-1]["ts"] if samples else None
    span = (last - first) if first is not None and last is not None else 0.0
    phase = inp.checkpoint.get("phase", "unknown")

    # frozen code / configuration
    frozen_reasons = [
        f"{k}: {inp.run.get('fingerprints', {}).get(k)} -> {v}"
        for k, v in (inp.checkpoint.get("fingerprints_now") or {}).items()
        if inp.run.get("fingerprints", {}).get(k) != v
    ]
    criteria.append(
        _c(
            "FREEZE",
            "Code, configuration and watched files unchanged since start",
            Status.FAIL if frozen_reasons else Status.PASS,
            "; ".join(frozen_reasons) or "fingerprints identical at every check",
            action="Rerun the soak from the new revision (a changed run is not representative)."
            if frozen_reasons
            else "",
        )
    )

    # duration and continuity
    # The run's elapsed time is start -> its own "completed" record, not the span between the first
    # and last sample: samples are taken at the start of a round, so a slow round must not shorten
    # the run. The sample log is checked separately (METRICS/SAMPLES/CONTINUITY).
    done = phase == "completed"
    started = inp.run.get("started_at")
    completed_ts = next(
        (e["ts"] for e in reversed(inp.events) if e.get("kind") == "completed" and "ts" in e), None
    )
    elapsed = (completed_ts - started) if completed_ts and started else span
    criteria.append(
        _c(
            "DURATION",
            f"Run lasted >= {cfg.duration_h:g} h",
            Status.PASS if done and elapsed >= required_s * 0.999 else Status.FAIL,
            f"ran {elapsed / 3600:.2f} h of {cfg.duration_h:g} h (samples span {span / 3600:.2f} h), "
            f"phase={phase}",
            evidence={
                "elapsed_s": elapsed,
                "sample_span_s": span,
                "required_s": required_s,
                "phase": phase,
            },
            action="Start a new run; an interrupted run does not count." if not done else "",
        )
    )
    gaps = [e for e in inp.events if e.get("kind") == "interruption"]
    if not gaps:
        cont = _c(
            "CONTINUITY",
            "Continuous run (no interruption)",
            Status.PASS,
            "no interruptions recorded",
        )
    elif th.max_interruptions is None or th.max_interruption_gap_s is None:
        cont = _c(
            "CONTINUITY",
            "Continuous run (no interruption)",
            Status.INCONCLUSIVE,
            f"{len(gaps)} interruption(s); the approved continuity criterion (max_interruptions, "
            "max_interruption_gap_s) is not decided",
            evidence={"gaps_s": [g.get("gap_s") for g in gaps]},
            action="Owner decision: how many/how long an interruption a 168 h run may contain.",
        )
    else:
        bad = [g for g in gaps if g.get("gap_s", 0) > th.max_interruption_gap_s]
        over = len(gaps) > th.max_interruptions or bool(bad)
        cont = _c(
            "CONTINUITY",
            "Continuous run (no interruption)",
            Status.FAIL if over else Status.PASS,
            f"{len(gaps)} interruption(s), longest {max(g.get('gap_s', 0) for g in gaps):.0f} s",
            evidence={"gaps_s": [g.get("gap_s") for g in gaps]},
            category="environment",
        )
    criteria.append(cont)
    clock = [e for e in inp.events if e.get("kind") == "clock_anomaly"]
    criteria.append(
        _c(
            "CLOCK",
            "No wall-clock anomalies",
            Status.INCONCLUSIVE if clock else Status.PASS,
            f"{len(clock)} anomaly(ies): {[c.get('detail') for c in clock][:3]}"
            if clock
            else "none",
            category="environment",
        )
    )

    # measurement completeness
    total = len(samples)
    required_series = [
        (f"{c.name}.{s.name}", s) for c in cfg.collectors for s in c.series if s.required
    ]
    cover: dict[str, float] = {}
    for name, _ in required_series:
        present = sum(1 for s in samples if isinstance(s.get("values", {}).get(name), int | float))
        cover[name] = present / total if total else 0.0
    never = sorted(n for n, c in cover.items() if c == 0.0)
    partial = sorted(n for n, c in cover.items() if 0.0 < c < 1.0)
    if not required_series:
        meas = _c(
            "METRICS",
            "Required metrics present",
            Status.INCONCLUSIVE,
            "no required series configured",
            category="measurement",
        )
    elif never:
        meas = _c(
            "METRICS",
            "Required metrics present",
            Status.INCONCLUSIVE,
            f"{len(never)} required series never produced a value: {never[:8]}",
            category="measurement",
            evidence={"never": never, "coverage": cover},
            action="Fix the exporter/collector or remove the series from the contract; a missing metric is not a pass.",
        )
    elif partial and (
        th.min_series_coverage is None or any(cover[n] < th.min_series_coverage for n in partial)
    ):
        meas = _c(
            "METRICS",
            "Required metrics present",
            Status.INCONCLUSIVE,
            f"{len(partial)} series have gaps (worst {min(cover[n] for n in partial):.1%}); "
            + (
                "min_series_coverage not decided"
                if th.min_series_coverage is None
                else f"below {th.min_series_coverage:.0%}"
            ),
            category="measurement",
            evidence={"coverage": {n: cover[n] for n in partial}},
            action="Owner decision on min_series_coverage, or fix the collector.",
        )
    else:
        meas = _c(
            "METRICS",
            "Required metrics present",
            Status.PASS,
            f"{len(required_series)} series, {total} samples",
            evidence={"samples": total},
        )
    criteria.append(meas)
    criteria.append(
        _c(
            "SAMPLES",
            "Sample log intact",
            Status.INCONCLUSIVE if inp.corrupt_sample_lines or total == 0 else Status.PASS,
            f"{inp.corrupt_sample_lines} unreadable line(s), {total} samples",
            category="measurement",
        )
    )

    # dependency outages
    up_series = [
        (f"{c.name}.{s.name}", s) for c in cfg.collectors for s in c.series if s.role == "up"
    ]
    outages: dict[str, list[float]] = {
        n: down_runs(series_values(samples, n), interval) for n, _ in up_series
    }
    longest = max((max(v) for v in outages.values() if v), default=0.0)
    if not up_series:
        out_c = _c(
            "OUTAGE",
            "Dependency outages within limit",
            Status.INCONCLUSIVE,
            "no `up` series configured",
            category="measurement",
        )
    elif longest == 0.0:
        out_c = _c(
            "OUTAGE",
            "Dependency outages within limit",
            Status.PASS,
            "all dependencies up at every sample",
        )
    elif th.max_dependency_outage_s is None:
        out_c = _c(
            "OUTAGE",
            "Dependency outages within limit",
            Status.INCONCLUSIVE,
            f"outage(s) up to {longest:.0f} s but max_dependency_outage_s is not decided",
            category="environment",
            evidence={"outages_s": {k: v for k, v in outages.items() if v}},
        )
    else:
        out_c = _c(
            "OUTAGE",
            "Dependency outages within limit",
            Status.FAIL if longest > th.max_dependency_outage_s else Status.PASS,
            f"longest {longest:.0f} s, limit {th.max_dependency_outage_s:.0f} s",
            category="environment",
            evidence={"outages_s": {k: v for k, v in outages.items() if v}},
        )
    criteria.append(out_c)

    # restarts
    restarts: dict[str, int] = {}
    for e in inp.events:
        if e.get("kind") == "restart":
            restarts[e.get("service", "?")] = restarts.get(e.get("service", "?"), 0) + 1
    if not restarts:
        criteria.append(
            _c(
                "RESTARTS",
                "Process restarts within limit",
                Status.PASS,
                "no restart observed (only processes/exporters under watch)",
            )
        )
    elif th.max_restarts_per_service is None:
        criteria.append(
            _c(
                "RESTARTS",
                "Process restarts within limit",
                Status.INCONCLUSIVE,
                f"restarts {restarts} but max_restarts_per_service not decided",
                evidence={"restarts": restarts},
            )
        )
    else:
        worst = max(restarts.values())
        criteria.append(
            _c(
                "RESTARTS",
                "Process restarts within limit",
                Status.FAIL if worst > th.max_restarts_per_service else Status.PASS,
                f"{restarts}, limit {th.max_restarts_per_service}",
                evidence={"restarts": restarts},
            )
        )

    # growth
    watched = [
        (f"{c.name}.{s.name}", s) for c in cfg.collectors for s in c.series if s.growth_watch
    ]
    growth_rows: dict[str, Any] = {}
    g_status, g_notes = Status.PASS, []
    if not watched:
        g_status, g_notes = Status.INCONCLUSIVE, ["no series marked growth_watch"]
    for name, _ in watched:
        pts = series_values(samples, name)
        if len(pts) < 20:
            g_status = _worse(g_status, Status.INCONCLUSIVE)
            g_notes.append(f"{name}: only {len(pts)} points")
            continue
        sl, ratio = slope_per_hour(pts), growth_ratio(pts)
        limit = th.growth.get(name)
        growth_rows[name] = {
            "slope_per_h": sl,
            "growth_ratio": ratio,
            "limit": limit.model_dump() if limit else None,
        }
        if limit is None:
            g_status = _worse(g_status, Status.INCONCLUSIVE)
            g_notes.append(f"{name}: slope {sl:.4g}/h, no approved limit")
        elif sl is not None and sl > limit.max_slope_per_h and ratio >= limit.min_growth_ratio:
            g_status = _worse(g_status, Status.FAIL)
            g_notes.append(
                f"{name}: slope {sl:.4g}/h > {limit.max_slope_per_h:g}/h (growth {ratio:+.0%})"
            )
    criteria.append(
        _c(
            "GROWTH",
            "No unbounded growth (RSS, FDs, threads, memory, queue depth, ...)",
            g_status,
            "; ".join(g_notes) or f"{len(watched)} series flat within limits",
            evidence=growth_rows,
            action="Explain and bound each growing series, or fix the leak, then rerun."
            if g_status is Status.FAIL
            else "",
        )
    )

    # queue age, lag, disk, retries
    criteria.append(
        _max_series(
            inp, "QUEUE_AGE", "Queue age within limit", "queue_age", th.max_queue_age_s, "s"
        )
    )
    criteria.append(
        _max_series(
            inp, "EVENT_LAG", "Event lag within limit", "event_lag", th.max_event_lag_s, "s"
        )
    )
    criteria.append(_disk(inp))
    criteria.append(_retries(inp))

    # pipeline reconciliation
    criteria.extend(_reconciliation(inp))

    # verdict
    statuses = [c.status for c in criteria if c.id not in {"DURATION"}]
    if frozen_reasons:
        verdict = Verdict.INVALIDATED
    elif phase == "running":
        verdict = Verdict.RUNNING
    elif not done or elapsed < required_s * 0.999:
        verdict = Verdict.INTERRUPTED
    elif Status.FAIL in statuses:
        verdict = Verdict.FAIL
    elif Status.INCONCLUSIVE in statuses:
        verdict = Verdict.INCONCLUSIVE
    else:
        verdict = Verdict.PASS

    eligible = cfg.m6_eligible and verdict is Verdict.PASS
    if cfg.mode.value == "synthetic":
        banner = "SYNTHETIC RUN - generated workload; NOT evidence for the M6 soak gate."
    elif cfg.dry_run:
        banner = f"DRY RUN ({cfg.duration_h:g} h) - validates the harness mechanics only; NOT the {REQUIRED_DURATION_H:g} h soak."
    elif eligible:
        banner = "Production soak PASSED on the stated criteria; M6 also needs the chaos matrix, restore drill and RF=3 evidence."
    else:
        banner = f"Production soak verdict {verdict.value}: not accepted."
    summary = {
        "samples": total,
        "span_h": round(span / 3600, 3),
        "required_h": cfg.duration_h,
        "interruptions": len(gaps),
        "restarts": restarts,
        "stage_counts": inp.stage_counts,
        "redeliveries": inp.redeliveries,
        "phase": phase,
    }
    return Evaluation(
        verdict,
        eligible,
        cfg.mode.value + ("-dry-run" if cfg.dry_run else ""),
        criteria,
        summary,
        banner,
    )


def _worse(a: Status, b: Status) -> Status:
    order = [Status.PASS, Status.NOT_APPLICABLE, Status.INCONCLUSIVE, Status.FAIL]
    return max(a, b, key=order.index)


def _role_series(inp: Inputs, role: str) -> list[str]:
    return [f"{c.name}.{s.name}" for c in inp.config.collectors for s in c.series if s.role == role]


def _max_series(
    inp: Inputs, id_: str, title: str, role: str, limit: float | None, unit: str
) -> Criterion:
    names = _role_series(inp, role)
    peaks = {n: max((v for _, v in series_values(inp.samples, n)), default=None) for n in names}
    seen = {n: p for n, p in peaks.items() if p is not None}
    if not names or not seen:
        return _c(
            id_,
            title,
            Status.INCONCLUSIVE,
            f"no `{role}` series produced data",
            category="measurement",
            action=f"Expose and configure a {role} series; it is a required SLI of the soak.",
        )
    if limit is None:
        return _c(
            id_,
            title,
            Status.INCONCLUSIVE,
            f"peaks {_fmt(seen)}; limit not decided",
            evidence={"peaks": seen},
            action=f"Owner decision: maximum acceptable {role} ({unit}).",
        )
    worst = max(seen.values())
    return _c(
        id_,
        title,
        Status.FAIL if worst > limit else Status.PASS,
        f"peak {worst:.4g} {unit}, limit {limit:g} {unit}",
        evidence={"peaks": seen, "limit": limit},
    )


def _fmt(d: dict[str, float]) -> str:
    return ", ".join(f"{k}={v:.4g}" for k, v in list(d.items())[:4])


def _disk(inp: Inputs) -> Criterion:
    names = _role_series(inp, "disk_free")
    lows = {n: min((v for _, v in series_values(inp.samples, n)), default=None) for n in names}
    seen = {n: v for n, v in lows.items() if v is not None}
    limit = inp.config.thresholds.min_disk_free_fraction
    if not seen:
        return _c(
            "DISK",
            "Disk headroom",
            Status.INCONCLUSIVE,
            "no disk_free series produced data",
            category="measurement",
        )
    if limit is None:
        return _c(
            "DISK",
            "Disk headroom",
            Status.INCONCLUSIVE,
            f"minimum free fraction {_fmt(seen)}; limit not decided",
            evidence={"min_free": seen},
        )
    worst = min(seen.values())
    return _c(
        "DISK",
        "Disk headroom",
        Status.FAIL if worst < limit else Status.PASS,
        f"minimum free {worst:.1%}, limit {limit:.0%}",
        category="environment",
        evidence={"min_free": seen},
    )


def _retries(inp: Inputs) -> Criterion:
    cfg = inp.config
    retry = [n for n in _role_series(inp, "retry")]
    work = [n for n in _role_series(inp, "work")]
    limit = cfg.thresholds.max_retry_ratio

    def delta(names: list[str]) -> float | None:
        total, seen = 0.0, False
        for n in names:
            pts = series_values(inp.samples, n)
            if len(pts) >= 2:
                total += max(0.0, pts[-1][1] - pts[0][1])
                seen = True
        return total if seen else None

    r, w = delta(retry), delta(work)
    if r is None or w is None:
        return _c(
            "RETRIES",
            "No retry storm",
            Status.INCONCLUSIVE,
            "retry/work counters not available",
            category="measurement",
        )
    ratio = r / w if w else (math.inf if r else 0.0)
    if limit is None:
        return _c(
            "RETRIES",
            "No retry storm",
            Status.INCONCLUSIVE,
            f"retries/work = {ratio:.3g}; max_retry_ratio not decided",
            evidence={"retries": r, "work": w},
        )
    return _c(
        "RETRIES",
        "No retry storm",
        Status.FAIL if ratio > limit else Status.PASS,
        f"retries/work = {ratio:.3g}, limit {limit:g}",
        evidence={"retries": r, "work": w},
    )


def _reconciliation(inp: Inputs) -> list[Criterion]:
    cfg = inp.config
    out: list[Criterion] = []
    req = [e for e in inp.edges if e.required]
    unaccounted = sum(e.unaccounted for e in req)
    ungraced = [e.name for e in req if e.grace_s is None]
    examples = {e.name: e.examples for e in req if e.unaccounted}
    if not req:
        status, detail = Status.INCONCLUSIVE, "no required reconciliation edge configured"
    elif unaccounted:
        status, detail = (
            Status.FAIL,
            f"{unaccounted} unit(s) of work unaccounted for past their grace period",
        )
    elif ungraced:
        status, detail = (
            Status.INCONCLUSIVE,
            f"edges without an approved grace_s cannot be judged: {ungraced}",
        )
    elif inp.tail_gaps or inp.tail_errors:
        status, detail = (
            Status.INCONCLUSIVE,
            f"observer gaps ({len(inp.tail_gaps)}) / read errors ({inp.tail_errors}): lost entries cannot be ruled out",
        )
    elif sum(e.sources for e in req) == 0:
        status, detail = Status.INCONCLUSIVE, "no source events observed: nothing was reconciled"
    else:
        status, detail = (
            Status.PASS,
            f"{sum(e.sources for e in req)} sources across {len(req)} edges, {sum(e.pending for e in req)} still within grace",
        )
    out.append(
        _c(
            "LOST_WORK",
            "No unaccounted-for work (task -> ... -> evidence)",
            status,
            detail,
            evidence={
                "edges": [asdict(e) for e in inp.edges],
                "examples": examples,
                "gaps": inp.tail_gaps,
            },
            action="Trace the listed keys with the stuck-pipeline runbook; a lost event is a defect."
            if status is Status.FAIL
            else "",
        )
    )
    dup = sum(e.duplicate_effects for e in inp.edges)
    out.append(
        _c(
            "DUPLICATES",
            "No duplicate business effects",
            Status.FAIL if dup else (Status.PASS if req else Status.INCONCLUSIVE),
            f"{dup} key(s) with more than one distinct effect; redeliveries (explained): {sum(inp.redeliveries.values())}",
            evidence={"redeliveries": inp.redeliveries},
        )
    )
    missing_stages = [s for s in cfg.required_stages if inp.stage_counts.get(s, 0) == 0]
    if not cfg.required_stages:
        st = _c(
            "STAGES",
            "Every required pipeline stage exercised",
            Status.INCONCLUSIVE,
            "required_stages is empty",
            category="measurement",
        )
    elif missing_stages:
        st = _c(
            "STAGES",
            "Every required pipeline stage exercised",
            Status.INCONCLUSIVE,
            f"no event of type {missing_stages} during the run; the pipeline was not exercised end to end",
            category="measurement",
            evidence={"stage_counts": inp.stage_counts},
            action="match.found is held until P10 calibration is released; the soak cannot pass without it."
            if "match.found" in missing_stages
            else "",
        )
    else:
        st = _c(
            "STAGES",
            "Every required pipeline stage exercised",
            Status.PASS,
            f"{len(cfg.required_stages)} stages observed",
            evidence={"stage_counts": inp.stage_counts},
        )
    out.append(st)
    ev = inp.evidence
    if ev is None or ev.checked == 0:
        out.append(
            _c(
                "EVIDENCE",
                "Evidence manifests resolve to artefacts with matching hashes",
                Status.INCONCLUSIVE,
                f"no evidence verified ({inp.finalized_evidence} finalized observed)",
                category="measurement",
            )
        )
    elif ev.mismatched or ev.missing:
        out.append(
            _c(
                "EVIDENCE",
                "Evidence manifests resolve to artefacts with matching hashes",
                Status.FAIL,
                f"{len(ev.mismatched)} hash mismatch, {len(ev.missing)} missing artefact among {ev.checked} checked",
                category="evidence",
                evidence={"mismatched": ev.mismatched[:10], "missing": ev.missing[:10]},
            )
        )
    elif ev.errors or ev.unverifiable:
        out.append(
            _c(
                "EVIDENCE",
                "Evidence manifests resolve to artefacts with matching hashes",
                Status.INCONCLUSIVE,
                f"{len(ev.errors)} validator error(s), {len(ev.unverifiable)} unverifiable among {ev.checked} checked",
                category="evidence",
            )
        )
    else:
        out.append(
            _c(
                "EVIDENCE",
                "Evidence manifests resolve to artefacts with matching hashes",
                Status.PASS,
                f"{ev.checked} evidence items verified",
                category="evidence",
            )
        )
    return out
