"""Machine-readable (JSON) and human-readable (Markdown, HTML) soak reports.

Every string that came from the run (series names, event details, keys, hostnames) is
HTML-escaped; the HTML is a single self-contained file with inline SVG and no scripts.
Secrets are never in the inputs: the stored configuration is redacted and credentials are
referenced by environment-variable name only.
"""

from __future__ import annotations

import html
import statistics
from typing import Any

from crawler2.soak.config import redact
from crawler2.soak.evaluate import (
    Evaluation,
    series_values,
    slope_per_hour,
)
from crawler2.soak.store import RunStore, utc_iso

MAX_POINTS = 240


def _downsample(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(points) <= MAX_POINTS:
        return points
    step = len(points) / MAX_POINTS
    return [points[int(i * step)] for i in range(MAX_POINTS)]


def series_summary(samples: list[dict[str, Any]], names: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    total = len(samples)
    for name in names:
        pts = series_values(samples, name)
        if not pts:
            out[name] = {"points": 0, "coverage": 0.0}
            continue
        vals = [v for _, v in pts]
        out[name] = {
            "points": len(pts),
            "coverage": round(len(pts) / total, 4) if total else 0.0,
            "first": vals[0],
            "last": vals[-1],
            "min": min(vals),
            "max": max(vals),
            "mean": statistics.fmean(vals),
            "slope_per_h": slope_per_hour(pts),
            "series": [[round(t, 1), v] for t, v in _downsample(pts)],
        }
    return out


def build_report(store: RunStore, evaluation: Evaluation) -> dict[str, Any]:
    run = store.read_json("run.json") or {}
    cfg = redact(store.read_json("config.json") or {})
    samples = store.read_jsonl("samples.jsonl").rows
    events = store.read_jsonl("events.jsonl").rows
    names = sorted({k for s in samples for k in s.get("values", {})})
    return {
        "generated_at": utc_iso(),
        "run_id": store.run_id,
        "verdict": evaluation.verdict.value,
        "scope": evaluation.scope,
        "m6_eligible": evaluation.m6_eligible,
        "banner": evaluation.banner,
        "summary": evaluation.summary,
        "criteria": evaluation.as_dict()["criteria"],
        "run": redact(run),
        "config": cfg,
        "timeline": [e for e in events if e.get("kind") != "note"][-500:],
        "series": series_summary(samples, names),
        "missing_metrics": sorted(
            {k for s in samples for k, v in s.get("values", {}).items() if v is None}
        ),
    }


def to_markdown(rep: dict[str, Any]) -> str:
    lines = [
        f"# Soak report {rep['run_id']}",
        "",
        f"**Verdict: {rep['verdict']}**  ·  scope: {rep['scope']}  ·  M6-eligible: {rep['m6_eligible']}",
        "",
        f"> {rep['banner']}",
        "",
        "## Criteria",
        "",
        "| Id | Criterion | Status | Detail |",
        "|---|---|---|---|",
    ]
    for c in rep["criteria"]:
        detail = str(c["detail"]).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {c['id']} | {c['title']} | {c['status']} | {detail} |")
    lines += ["", "## Next actions", ""]
    actions = [f"- **{c['id']}**: {c['action']}" for c in rep["criteria"] if c.get("action")]
    lines += actions or ["- none"]
    s = rep["summary"]
    lines += [
        "",
        "## Summary",
        "",
        f"- samples: {s['samples']}, observed span {s['span_h']} h of {s['required_h']} h, "
        f"phase `{s['phase']}`",
        f"- interruptions: {s['interruptions']}, restarts: {s['restarts'] or 'none'}",
        f"- stage event counts: {s['stage_counts'] or 'none'}",
        f"- missing metrics (any sample): {len(rep['missing_metrics'])}",
        "",
        "## Timeline (last events)",
        "",
    ]
    for e in rep["timeline"][-40:]:
        extra = {k: v for k, v in e.items() if k not in ("ts", "at", "kind")}
        lines.append(f"- {e.get('at', '')} `{e.get('kind')}` {extra or ''}")
    return "\n".join(lines) + "\n"


_COLOR = {
    "PASS": "#1a7f37",
    "FAIL": "#cf222e",
    "INCONCLUSIVE": "#9a6700",
    "NOT_APPLICABLE": "#6e7781",
}


def _spark(points: list[list[float]], width: int = 260, height: int = 36) -> str:
    if len(points) < 2:
        return ""
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    dx, dy = (x1 - x0) or 1.0, (y1 - y0) or 1.0
    pts = " ".join(
        f"{(x - x0) / dx * width:.1f},{height - (y - y0) / dy * (height - 2) - 1:.1f}"
        for x, y in points
    )
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="trend"><polyline fill="none" stroke="currentColor" '
        f'stroke-width="1.2" points="{pts}"/></svg>'
    )


def to_html(rep: dict[str, Any]) -> str:
    e = html.escape
    rows = []
    for c in rep["criteria"]:
        colour = _COLOR.get(c["status"], "#6e7781")
        rows.append(
            f"<tr><td>{e(c['id'])}</td><td>{e(c['title'])}</td>"
            f"<td style='color:{colour};font-weight:600'>{e(c['status'])}</td>"
            f"<td>{e(str(c['detail']))}<br><small>{e(str(c.get('action') or ''))}</small></td></tr>"
        )
    series_rows = []
    for name, info in list(rep["series"].items())[:200]:
        if not info.get("points"):
            series_rows.append(f"<tr><td>{e(name)}</td><td colspan='5'>no data</td></tr>")
            continue
        slope = info["slope_per_h"]
        series_rows.append(
            f"<tr><td>{e(name)}</td><td>{info['coverage']:.0%}</td><td>{info['min']:.4g}</td>"
            f"<td>{info['max']:.4g}</td><td>{'' if slope is None else f'{slope:.3g}'}</td>"
            f"<td>{_spark(info['series'])}</td></tr>"
        )
    timeline = "".join(
        f"<li>{e(str(ev.get('at', '')))} <code>{e(str(ev.get('kind')))}</code> "
        f"{e(str({k: v for k, v in ev.items() if k not in ('ts', 'at', 'kind')}))}</li>"
        for ev in rep["timeline"][-60:]
    )
    verdict_colour = _COLOR.get(rep["verdict"], "#9a6700")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Soak report {e(rep["run_id"])}</title>
<style>
body{{font:14px/1.45 system-ui,sans-serif;margin:16px;max-width:1100px;color:#1f2328;background:#fff}}
@media (prefers-color-scheme: dark){{body{{color:#e6edf3;background:#0d1117}}}}
table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #8884;padding:4px 8px;
text-align:left;vertical-align:top}}.banner{{padding:8px 12px;border-left:4px solid {verdict_colour};
background:#8881}}code{{font-size:12px}}</style></head><body>
<h1>Soak report <small>{e(rep["run_id"])}</small></h1>
<p><strong style="color:{verdict_colour}">Verdict: {e(rep["verdict"])}</strong> &middot;
scope {e(rep["scope"])} &middot; M6-eligible: {e(str(rep["m6_eligible"]))}</p>
<p class="banner">{e(rep["banner"])}</p>
<h2>Criteria</h2>
<table><tr><th>Id</th><th>Criterion</th><th>Status</th><th>Detail / next action</th></tr>
{"".join(rows)}</table>
<h2>Series</h2>
<table><tr><th>Series</th><th>Coverage</th><th>Min</th><th>Max</th><th>Slope/h</th><th>Trend</th></tr>
{"".join(series_rows)}</table>
<h2>Timeline</h2><ul>{timeline}</ul>
<p><small>Generated {e(rep["generated_at"])}.
Raw samples: samples.jsonl; events: events.jsonl.</small></p>
</body></html>
"""


def write_reports(store: RunStore, evaluation: Evaluation) -> dict[str, str]:
    from crawler2.soak.store import atomic_write

    rep = build_report(store, evaluation)
    store.write_json("report.json", rep)
    atomic_write(store.path / "report.md", to_markdown(rep).encode())
    atomic_write(store.path / "report.html", to_html(rep).encode())
    return {name: str(store.path / name) for name in ("report.json", "report.md", "report.html")}
