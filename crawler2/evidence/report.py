"""Human-readable case report (HTML, self-contained, deterministic).

Generated only from ``case.json`` and the packaged manifests, so the report
cannot say anything the manifests do not. Every value is HTML-escaped; URLs
are the manifests' redacted display forms and are not rendered as links (an
offline reviewer should not be one click from a piracy site). No scripts, no
external resources, no generation time.
"""

from __future__ import annotations

import json
from html import escape
from typing import Any

_CSS = """
body{font:14px/1.45 system-ui,sans-serif;margin:24px;color:#1b1b1b;background:#fff}
h1{font-size:22px}h2{font-size:18px;margin-top:28px;border-bottom:1px solid #ccc}
h3{font-size:15px;margin-top:20px}
table{border-collapse:collapse;margin:8px 0;width:100%}
th,td{border:1px solid #ccc;padding:4px 6px;text-align:left;vertical-align:top}
th{background:#f2f2f2}code{font:12px ui-monospace,monospace;word-break:break-all}
.warn{border:2px solid #b45309;background:#fff7ed;padding:10px;margin:12px 0}
.bad{color:#b91c1c;font-weight:600}.ok{color:#15803d;font-weight:600}
"""


def _e(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict | list):
        return escape(json.dumps(value, sort_keys=True, ensure_ascii=False))
    return escape(str(value))


def _code(value: Any) -> str:
    return f"<code>{_e(value)}</code>"


def _table(head: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "<p>None.</p>"
    th = "".join(f"<th>{escape(h)}</th>" for h in head)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def _state(value: str | None) -> str:
    good = value in ("verified", "included", "pass", "complete")
    return f'<span class="{"ok" if good else "bad"}">{_e(value)}</span>'


def _evidence_section(entry: dict[str, Any], doc: dict[str, Any] | None) -> str:
    eid = entry["evidence_id"]
    out = [
        f'<h3 id="e-{_e(eid)}">Evidence {_code(eid)} — v{_e(entry["latest_version"])} '
        f"({_state(entry['status'])})</h3>"
    ]
    if doc is None:
        out.append('<p class="bad">Manifest could not be packaged; see export failures.</p>')
        return "".join(out)
    ev, claims, target = doc["evidence"], doc["claims"], doc["target"]
    out.append(
        _table(
            ["Field", "Value", "Source"],
            [
                [
                    "Target",
                    _e(target["title"] if target else None)
                    + " "
                    + _code(claims["target_id"])
                    + f" v{_e(claims['target_version'])}",
                    "derived (P2 target projection)",
                ],
                ["Match id", _code(claims["match_id"]), "claimed (matcher)"],
                ["Verdict", _e(claims["verdict"]), "claimed (matcher)"],
                ["Confidence", _e(claims["confidence"]), "claimed (matcher)"],
                ["Technique", _e(claims["technique"]), "claimed (matcher)"],
                ["Decided at", _e(claims["decided_at"]), "claimed (matcher clock)"],
                ["Content id", _code(claims["content_id"]), "claimed (matcher)"],
                ["Collected at", _e(ev["collected_at"]), "crawler2 clock (not trusted time)"],
                ["Supersedes", _e(ev["supersedes"]), "evidence history"],
                ["Correction reason", _e(ev["correction_reason"]), "operator"],
            ],
        )
    )
    rows = []
    for s in doc["observed"]["sources"]:
        f = s["fetch"]
        redirects = "<br>".join(f"{_e(r['status'])} → {_code(r['url'])}" for r in f["redirects"])
        rows.append(
            [
                _code(s["page"]["url"]) + (" <em>(redacted)</em>" if s["page"]["redacted"] else ""),
                _e(s["attribution_basis"]),
                _code(f["final"]["url"] if f["final"] else None),
                redirects or "none",
                _e(f["http_status"]),
                _e(f["capability"]),
                _e(f["observed_at"]),
                _code(f["body_sha256"]),
                _code(s["media_locator"]["url"]),
            ]
        )
    out.append("<p>Sources (observed by crawler2 fetch workers):</p>")
    out.append(
        _table(
            [
                "Page",
                "Basis",
                "Final URL",
                "Redirects",
                "HTTP",
                "Capability",
                "Observed at",
                "Body SHA-256",
                "Media locator",
            ],
            rows,
        )
    )
    out.append("<p>Artifacts:</p>")
    out.append(
        _table(
            ["Role", "Page observation", "SHA-256", "Size", "State", "Detail"],
            [
                [
                    _e(a["role"]),
                    _code(a["subject"]),
                    _code(a["sha256"]),
                    _e(a["size_bytes"]),
                    _state(a["state"]),
                    _e(a["detail"]),
                ]
                for a in doc["artifacts"]
            ],
        )
    )
    out.append("<p>Validation checks:</p>")
    out.append(
        _table(
            ["Check", "Required", "Outcome", "Detail"],
            [
                [_e(c["check"]), _e(c["required"]), _state(c["outcome"]), _e(c["detail"])]
                for c in doc["validation"]["checks"]
            ],
        )
    )
    out.append("<p>Gaps (declared, not fabricated):</p>")
    out.append(
        _table(
            ["Gap", "Subject", "Required", "Detail"],
            [
                [_e(g["code"]), _code(g["subject"]), _e(g["required"]), _e(g["detail"])]
                for g in doc["gaps"]
            ],
        )
    )
    return "".join(out)


def render_report(case: dict[str, Any], manifests: dict[str, dict[str, Any]]) -> str:
    c = case["campaign"]
    stats = case["statistics"]
    failures = case["export_failures"]
    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        f"<title>Case {_e(c['campaign_id'])}</title><style>{_CSS}</style></head><body>",
        f"<h1>Evidence case package — {_e(c['name'])} ({_code(c['campaign_id'])})</h1>",
        f'<div class="warn"><strong>Not a legal determination.</strong> {_e(case["disclaimer"])}'
        "</div>",
        f'<div class="warn"><strong>Trusted timestamp:</strong> '
        f"{_e(case['attestation']['trusted_timestamp'])}; <strong>signature:</strong> "
        f"{_e(case['attestation']['signature'])}. {_e(case['attestation']['note'])}</div>",
    ]
    if failures:
        parts.append(
            '<div class="warn bad"><strong>Export failures:</strong><ul>'
            + "".join(f"<li>{_e(f)}</li>" for f in failures)
            + "</ul></div>"
        )
    parts.append("<h2>Targets</h2>")
    parts.append(
        _table(
            ["Target id", "Version", "Title", "Kind"],
            [
                [_code(t["target_id"]), _e(t["version"]), _e(t["title"]), _e(t["kind"])]
                for t in c["targets"]
            ],
        )
    )
    parts.append("<h2>Summary statistics</h2>")
    flat = [
        ["Window", _e(stats["window"])],
        ["Matches (by verdict)", _e(stats["matches"])],
        ["Media carrying matched content", _e(stats["matched_media"])],
        ["Evidence", _e(stats["evidence"])],
        ["Sources", _e(stats["sources"])],
        ["Artifacts", _e(stats["artifacts"])],
        ["Gaps", _e(stats["gaps"])],
        ["Collection window", _e(stats["collection_window"])],
        ["Unavailable", _e(stats["unavailable"])],
    ]
    parts.append(_table(["Statistic", "Value"], flat))
    parts.append("<h2>Finalized evidence</h2>")
    parts.append(
        _table(
            [
                "Evidence",
                "Version",
                "Status",
                "Verdict",
                "Confidence",
                "Decided at",
                "Collected at",
                "Domains",
                "Manifest SHA-256",
            ],
            [
                [
                    f'<a href="#e-{_e(e["evidence_id"])}">{_code(e["evidence_id"])}</a>',
                    _e(e["latest_version"]),
                    _state(e["status"]),
                    _e(e["verdict"]),
                    _e(e["confidence"]),
                    _e(e["decided_at"]),
                    _e(e["collected_at"]),
                    _e(", ".join(e["source_domains"])),
                    _code(e["versions"][-1]["manifest_sha256"]),
                ]
                for e in case["evidence"]
            ],
        )
    )
    parts.append("<h2>Not finalized</h2>")
    parts.append(
        _table(
            ["Evidence", "Candidate status"],
            [[_code(n["evidence_id"]), _e(n["status"])] for n in case["not_finalized"]],
        )
    )
    parts.append("<h2>Artifact inventory</h2>")
    parts.append(
        _table(
            ["SHA-256", "Role", "Size", "Recorded state", "Export state", "Path", "Evidence"],
            [
                [
                    _code(a["sha256"]),
                    _e(a["role"]),
                    _e(a["size_bytes"]),
                    _state(a["recorded_state"]),
                    _state(a.get("export_state")),
                    _code(a["path"]),
                    _e(len(a["evidence_ids"])),
                ]
                for a in case["artifacts"]
            ],
        )
    )
    parts.append("<h2>Evidence details</h2>")
    for entry in case["evidence"]:
        parts.append(_evidence_section(entry, manifests.get(entry["evidence_id"])))
    first = next(iter(manifests.values()), None)
    if first is not None:
        parts.append("<h2>Review required</h2><ul>")
        parts.extend(f"<li>{_e(r)}</li>" for r in first["review_required"])
        parts.append("</ul><h2>Limitations</h2><ul>")
        parts.extend(f"<li>{_e(r)}</li>" for r in first["limitations"])
        parts.append("</ul>")
    parts.append("</body></html>\n")
    return "".join(parts)
