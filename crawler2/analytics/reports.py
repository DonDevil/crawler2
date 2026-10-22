"""Standard campaign and source reports from one export revision (design §8).

Every report is a pure function of (export revision, parameters): no clock, no
operational store. It names the revision it was computed from (``export``),
defines every metric (``definitions``), gives every rate its numerator and
denominator (``value`` is ``None`` when the denominator is 0), and sets
``complete: false`` with reasons when a dataset it needs is not in the export.
``report_digest`` (SHA-256 of the canonical body) makes reproduction checkable.

Distinct lifecycle states are never merged: discovered (``urls``) ≠ fetched
(``page_observations``) ≠ fetched OK (2xx) ≠ parsed (``page_extracts``) ≠
media discovered (``page_media_refs``) ≠ media observed/identified
(``media_observations``) ≠ match reported (any verdict) ≠ match confirmed (the
matcher's ``confirmed`` verdict; nobody reviewed it) ≠ evidence candidate (E5)
≠ finalized package (E6).
"""

from __future__ import annotations

import hashlib
from typing import Any

from crawler2.analytics.export import canonical
from crawler2.analytics.query import DatasetUnavailableError, ExportView

CAMPAIGN_REPORT = "campaign-report/v1"
SOURCE_REPORT = "source-report/v1"
UNKNOWN_SITE = "(unknown site)"


def rate(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": None if denominator == 0 else round(numerator / denominator, 6),
    }


def _finish(report: dict[str, Any]) -> dict[str, Any]:
    report["report_digest"] = hashlib.sha256(canonical(report)).hexdigest()
    return report


def _quality(view: ExportView, names: list[str]) -> dict[str, Any]:
    """Per-dataset export counts the numbers rest on (rejected, unknown time, excluded)."""
    out = {}
    for n in names:
        doc = view.available.get(n)
        if doc is None:
            out[n] = {"status": view.unavailable.get(n, "unavailable")}
            continue
        c = doc["counts"]
        out[n] = {
            "status": doc["status"],
            "rows": doc["rows"],
            "rejected": c["rejected"],
            "out_of_window": c["out_of_window"],
            "unknown_time": c["unknown_time"],
            "duplicates_collapsed": c["duplicates_collapsed"],
        }
    return out


# --- campaign -----------------------------------------------------------------------

CAMPAIGN_DEFINITIONS = {
    "targets.in_campaign": "distinct target ids in E8 membership for the campaign",
    "targets.in_projection": "campaign targets present in the targets dataset (crawler2 "
    "received target.registered); the rest are not_in_projection",
    "matches.reported": "distinct match ids for campaign targets (any verdict), decided in "
    "the export window and at/after the campaign's since",
    "matches.confirmed_by_matcher": "reported matches whose matcher verdict is 'confirmed' "
    "(an automated verdict, not a human review)",
    "media.carrying_matched_content": "distinct media ids whose media observations identified "
    "the content id of a reported match",
    "sources.pages / sites": "distinct page URLs / registrable domains of media observations "
    "carrying matched content (registry co-occurrence, the P11/P12 attribution input)",
    "evidence.candidates": "E5 candidates for campaign targets (status as of the export)",
    "evidence.finalized_packages": "candidates with at least one E6 finalized version",
    "evidence.latest_version_by_status": "status of each package's highest version",
    "rates.candidate_coverage": "candidates for reported matches / reported matches",
    "rates.finalized_share": "finalized packages / candidates",
}


def campaign_report(view: ExportView, campaign_id: str) -> dict[str, Any]:
    view.require("campaigns")
    members = view.rows(
        "SELECT target_id, name, since FROM campaigns WHERE campaign_id = $c ORDER BY target_id",
        {"c": campaign_id},
    )
    if not members:
        raise LookupError(f"campaign {campaign_id!r} is not in export {view.ref['export_key']}")
    since = members[0]["since"]
    needed = [
        "targets",
        "matches",
        "media_observations",
        "evidence_candidates",
        "evidence_versions",
    ]
    missing = view.missing(needed)
    c = view.con
    c.execute(
        "CREATE OR REPLACE TEMP TABLE _ct AS SELECT DISTINCT target_id FROM campaigns "
        "WHERE campaign_id = $c",
        {"c": campaign_id},
    )
    report: dict[str, Any] = {
        "report": CAMPAIGN_REPORT,
        "export": view.ref,
        "scope": {"campaign_id": campaign_id, "name": members[0]["name"], "since": since},
        "complete": not missing,
        "incomplete_reasons": [f"{n}: {r}" for n, r in sorted(missing.items())],
        "definitions": CAMPAIGN_DEFINITIONS,
    }
    targets: dict[str, Any] = {"in_campaign": len(members)}
    if "targets" in view.available:
        known = view.rows(
            "SELECT t.target_id, t.retired_at IS NOT NULL AS retired FROM targets t "
            "JOIN _ct USING (target_id) ORDER BY t.target_id"
        )
        targets |= {
            "in_projection": len(known),
            "not_in_projection": len(members) - len(known),
            "retired": sum(1 for k in known if k["retired"]),
        }
    report["targets"] = targets
    matches: dict[str, Any] | None = None
    if "matches" in view.available:
        c.execute(
            "CREATE OR REPLACE TEMP TABLE _cm AS SELECT DISTINCT m.match_id, m.content_id, "
            "m.target_id, m.verdict, m.confidence, m.decided_at FROM matches m JOIN _ct "
            "USING (target_id) WHERE $since IS NULL OR m.decided_at >= CAST($since AS TIMESTAMPTZ)",
            {"since": since},
        )
        by_verdict = {
            r["verdict"]: r["n"]
            for r in view.rows("SELECT verdict, count(*) AS n FROM _cm GROUP BY 1 ORDER BY 1")
        }
        reported = sum(by_verdict.values())
        matches = {
            "reported": reported,
            "by_verdict": by_verdict,
            "confirmed_by_matcher": by_verdict.get("confirmed", 0),
            "distinct_contents": view.scalar("SELECT count(DISTINCT content_id) FROM _cm"),
            "by_target": view.rows(
                "SELECT target_id, count(*) AS reported, "
                "count(*) FILTER (WHERE verdict = 'confirmed') AS confirmed "
                "FROM _cm GROUP BY 1 ORDER BY 1"
            ),
            "by_day": view.rows(
                "SELECT strftime(decided_at, '%Y-%m-%d') AS date, count(*) AS reported, "
                "count(*) FILTER (WHERE verdict = 'confirmed') AS confirmed "
                "FROM _cm GROUP BY 1 ORDER BY 1"
            ),
        }
    report["matches"] = matches
    if matches is not None and "media_observations" in view.available:
        c.execute(
            "CREATE OR REPLACE TEMP TABLE _cmo AS SELECT mo.* FROM media_observations mo "
            "WHERE mo.content_id IN (SELECT content_id FROM _cm)"
        )
        per_site = view.rows(
            f"SELECT coalesce(page_site, '{UNKNOWN_SITE}') AS site, "
            "count(DISTINCT page_url_id) AS pages FROM _cmo GROUP BY 1 ORDER BY 1"
        )
        report["media"] = {
            "carrying_matched_content": view.scalar("SELECT count(DISTINCT media_id) FROM _cmo"),
            "observations_of_matched_content": view.scalar("SELECT count(*) FROM _cmo"),
        }
        report["sources"] = {
            "pages": view.scalar("SELECT count(DISTINCT page_url_id) FROM _cmo"),
            "sites": len(per_site),
            "pages_per_site": {r["site"]: r["pages"] for r in per_site},
            "matches_without_observed_content": view.scalar(
                "SELECT count(*) FROM _cm WHERE content_id NOT IN (SELECT content_id FROM _cmo)"
            ),
        }
    else:
        report["media"] = report["sources"] = None
    evidence: dict[str, Any] | None = None
    if "evidence_candidates" in view.available:
        c.execute(
            "CREATE OR REPLACE TEMP TABLE _ce AS SELECT e.* FROM evidence_candidates e "
            "JOIN _ct USING (target_id)"
        )
        candidates = view.scalar("SELECT count(*) FROM _ce")
        evidence = {
            "candidates": candidates,
            "by_status": {
                r["status"]: r["n"]
                for r in view.rows("SELECT status, count(*) AS n FROM _ce GROUP BY 1 ORDER BY 1")
            },
        }
        if "evidence_versions" in view.available:
            latest = view.rows(
                "SELECT status, count(*) AS n FROM (SELECT v.evidence_id, arg_max(v.status, "
                "v.version) AS status FROM evidence_versions v JOIN _ce USING (evidence_id) "
                "GROUP BY 1) GROUP BY 1 ORDER BY 1"
            )
            finalized = sum(r["n"] for r in latest)
            evidence |= {
                "finalized_packages": finalized,
                "latest_version_by_status": {r["status"]: r["n"] for r in latest},
                "superseded_versions": view.scalar(
                    "SELECT count(*) FROM evidence_versions v JOIN _ce USING (evidence_id) "
                    "WHERE v.version < (SELECT max(version) FROM evidence_versions w "
                    "WHERE w.evidence_id = v.evidence_id)"
                ),
            }
        if matches is not None:
            covered = view.scalar(
                "SELECT count(DISTINCT match_id) FROM _ce WHERE match_id IN "
                "(SELECT match_id FROM _cm)"
            )
            evidence["matches_without_candidate"] = matches["reported"] - covered
            report["rates"] = {"candidate_coverage": rate(covered, matches["reported"])}
        if "finalized_packages" in evidence:
            report.setdefault("rates", {})["finalized_share"] = rate(
                evidence["finalized_packages"], candidates
            )
    report["evidence"] = evidence
    report.setdefault("rates", {})
    report["data_quality"] = _quality(view, ["campaigns", *needed])
    return _finish(report)


# --- source -------------------------------------------------------------------------

SOURCE_DEFINITIONS = {
    "urls_discovered": "W12 URLs first seen in the export window on the site (links, seeds, "
    "search); not all were fetched",
    "fetch_attempts": "fetch attempts whose requested URL is on the site (any outcome)",
    "observations": "page observations whose final URL is on the site (a response was "
    "recorded, any status)",
    "observations_ok": "observations with HTTP status 200-299",
    "pages_fetched_ok": "distinct final URLs with at least one 2xx observation",
    "pages_parsed": "distinct URLs with at least one P5 extract",
    "media_discovered": "distinct media locator URLs referenced by the site's extracts",
    "media_observed": "distinct media ids the P8 registry saw on the site's pages",
    "media_identified": "distinct content ids identified among them",
    "matched_contents_seen": "distinct content ids of reported matches seen on the site's "
    "pages (co-occurrence in the registry; not an attribution decision)",
    "evidence_sources": "P12 evidence source records on the site's pages, by basis",
    "rates.response_rate": "attempts with outcome 'response' / attempts",
    "rates.ok_rate": "observations_ok / observations",
    "rates.parse_coverage": "pages_parsed / pages_fetched_ok (P5 parses non-2xx bodies too, "
    "so this can exceed 1)",
}

_SITE_QUERIES: dict[str, tuple[str, str]] = {
    "urls_discovered": ("urls", "SELECT site, count(*) AS v FROM urls GROUP BY 1"),
    "fetch_attempts": (
        "fetch_attempts",
        "SELECT site, count(*) AS v FROM fetch_attempts GROUP BY 1",
    ),
    "attempts_response": (
        "fetch_attempts",
        "SELECT site, count(*) FILTER (WHERE outcome = 'response') AS v FROM fetch_attempts "
        "GROUP BY 1",
    ),
    "observations": (
        "page_observations",
        "SELECT final_site AS site, count(*) AS v FROM page_observations GROUP BY 1",
    ),
    "observations_ok": (
        "page_observations",
        "SELECT final_site AS site, count(*) FILTER (WHERE http_status BETWEEN 200 AND 299) AS v "
        "FROM page_observations GROUP BY 1",
    ),
    "pages_fetched_ok": (
        "page_observations",
        "SELECT final_site AS site, count(DISTINCT final_url_id) FILTER "
        "(WHERE http_status BETWEEN 200 AND 299) AS v FROM page_observations GROUP BY 1",
    ),
    "pages_parsed": (
        "page_extracts",
        "SELECT site, count(DISTINCT url_id) AS v FROM page_extracts GROUP BY 1",
    ),
    "page_versions_parsed": (
        "page_extracts",
        "SELECT site, count(*) AS v FROM page_extracts GROUP BY 1",
    ),
    "media_discovered": (
        "page_media_refs",
        "SELECT site, count(DISTINCT locator_url) AS v FROM page_media_refs GROUP BY 1",
    ),
    "media_observed": (
        "media_observations",
        "SELECT page_site AS site, count(DISTINCT media_id) AS v FROM media_observations "
        "GROUP BY 1",
    ),
    "media_identified": (
        "media_observations",
        "SELECT page_site AS site, count(DISTINCT content_id) AS v FROM media_observations "
        "GROUP BY 1",
    ),
}


def source_report(view: ExportView, *, site: str | None = None) -> dict[str, Any]:
    used = sorted({ds for ds, _ in _SITE_QUERIES.values()} | {"matches", "evidence_sources"})
    missing = view.missing(used)
    if len(missing) == len(used):
        raise DatasetUnavailableError(f"export has none of the source datasets: {missing}")
    sites: dict[str, dict[str, Any]] = {}

    def put(metric: str, rows: list[dict[str, Any]]) -> None:
        for r in rows:
            name = r["site"] or UNKNOWN_SITE
            sites.setdefault(name, {})[metric] = r["v"]

    for metric, (ds, sql) in _SITE_QUERIES.items():
        if ds in view.available:
            put(metric, view.rows(sql))
    if "matches" in view.available and "media_observations" in view.available:
        put(
            "matched_contents_seen",
            view.rows(
                "SELECT page_site AS site, count(DISTINCT content_id) AS v FROM media_observations "
                "WHERE content_id IN (SELECT content_id FROM matches) GROUP BY 1"
            ),
        )
    if "evidence_sources" in view.available:
        for r in view.rows(
            "SELECT page_site AS site, basis, count(*) AS v FROM evidence_sources "
            "GROUP BY 1, 2 ORDER BY 1, 2"
        ):
            entry = sites.setdefault(r["site"] or UNKNOWN_SITE, {})
            entry.setdefault("evidence_sources", {})[r["basis"]] = r["v"]
    if site is not None:
        sites = {k: v for k, v in sites.items() if k == site}
        if not sites:
            raise LookupError(f"site {site!r} has no rows in export {view.ref['export_key']}")
    metrics = [m for m in _SITE_QUERIES if m != "attempts_response"] + ["matched_contents_seen"]
    table = []
    for name in sorted(sites):
        v = sites[name]
        row: dict[str, Any] = {"site": name}
        for m in metrics:
            row[m] = v.get(m, 0)
        row["evidence_sources"] = v.get("evidence_sources", {})
        row["rates"] = {
            "response_rate": rate(v.get("attempts_response", 0), v.get("fetch_attempts", 0)),
            "ok_rate": rate(v.get("observations_ok", 0), v.get("observations", 0)),
            "parse_coverage": rate(v.get("pages_parsed", 0), v.get("pages_fetched_ok", 0)),
        }
        table.append(row)
    daily: list[dict[str, Any]] = []
    if "page_observations" in view.available:
        daily = view.rows(
            f"SELECT coalesce(final_site, '{UNKNOWN_SITE}') AS site, date, "
            "count(*) AS observations, "
            "count(*) FILTER (WHERE http_status BETWEEN 200 AND 299) AS observations_ok "
            "FROM page_observations GROUP BY 1, 2 ORDER BY 1, 2"
        )
        if site is not None:
            daily = [d for d in daily if d["site"] == site]
    totals = {m: sum(r[m] for r in table) for m in metrics}
    report = {
        "report": SOURCE_REPORT,
        "export": view.ref,
        "scope": {"site": site, "window": view.manifest["scope"]},
        "complete": not missing,
        "incomplete_reasons": [f"{n}: {r}" for n, r in sorted(missing.items())],
        "definitions": SOURCE_DEFINITIONS,
        "totals": totals,
        "sites": table,
        "daily": daily,
        "data_quality": _quality(view, used),
    }
    return _finish(report)


def render_markdown(report: dict[str, Any]) -> str:
    """A compact human-readable rendering; the JSON is the authoritative report."""
    ref = report["export"]
    lines = [
        f"# {report['report']}",
        "",
        f"Export `{ref['export_key']}` revision {ref['revision']} (content "
        f"`{ref['content_id'][:16]}`), window {ref['scope']['since']} → {ref['scope']['until']}.",
        f"Complete: **{report['complete']}**"
        + ("" if report["complete"] else f" — {'; '.join(report['incomplete_reasons'])}"),
        "",
    ]
    if report["report"] == CAMPAIGN_REPORT:
        for section in ("targets", "matches", "media", "sources", "evidence", "rates"):
            value = report.get(section)
            lines.append(f"## {section}")
            if value is None:
                lines.append("unavailable (dataset missing from the export)")
            else:
                for k, v in value.items():
                    if isinstance(v, list):
                        continue
                    lines.append(f"- {k}: {v}")
            lines.append("")
    else:
        cols = [
            "site",
            "urls_discovered",
            "fetch_attempts",
            "observations_ok",
            "pages_parsed",
            "media_discovered",
            "media_observed",
            "matched_contents_seen",
        ]
        lines.append("| " + " | ".join(cols) + " |")
        lines.append("|" + "---|" * len(cols))
        for r in report["sites"]:
            lines.append("| " + " | ".join(str(r[c]) for c in cols) + " |")
        lines.append("")
    lines.append(f"report_digest `{report['report_digest']}`")
    return "\n".join(lines) + "\n"
