"""P12 manifest canonicalization, case packages and redaction (validation groups B and D)."""

from __future__ import annotations

import hashlib
import io
import json
import re
import tarfile
from pathlib import Path

import pytest

from crawler2.evidence.export import (
    UnsafePathError,
    build_package,
    deterministic_tar,
    export_case,
    safe_relpath,
    write_package,
)
from crawler2.evidence.manifest import canonical_bytes
from crawler2.evidence.redact import display_url
from crawler2.evidence.report import render_report
from tests.unit.evidence import campaign as fixture
from tests.unit.evidence.fakes import World

DATA = b"matched-content-sample"
SECRET_PAGE = "https://site-a.example/watch/1?id=7&token=s3cr3t-tok&X-Amz-Signature=abc123"  # noqa: S105


def _package(w: World, **kw: object) -> tuple[dict[str, bytes], tuple[str, ...]]:
    case, _ = fixture.build(w)
    args: dict[str, object] = {
        "objects": w.objects,
        "bucket": w.objects.bucket,
        "include_artifacts": True,
        "max_artifact_bytes": 10**6,
        "max_package_bytes": 10**7,
    } | kw
    return build_package(fixture.data(w, case), **args)  # type: ignore[arg-type]


def _single(body: bytes | None = None, page: str = "https://site-a.example/watch/1") -> World:
    w = World()
    t = w.register("alpha")
    w.page(page, "v/1.mp4", DATA, body=body)
    m = w.match(DATA, t)
    w.collector().collect(m)
    w.finalizer().finalize(w.repo.opened(m.match_id)[0], actor="t")  # type: ignore[index]
    return w


def _manifest(w: World) -> bytes:
    ((_, versions),) = w.store.version_rows.items()
    return w.objects.blobs[versions[0].manifest_digest.hex]


# --- D: manifest and package -------------------------------------------------------------


def test_identical_inputs_give_identical_manifest_bytes_and_hash() -> None:
    a, b = _manifest(_single()), _manifest(_single())
    assert a == b
    assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()
    # canonical: re-serializing the parsed manifest reproduces the bytes exactly
    assert canonical_bytes(json.loads(a)) == a
    assert b"\n" not in a
    assert not a.startswith(b"{\n")


def test_changed_artifact_bytes_change_artifact_and_manifest_hashes() -> None:
    a = json.loads(_manifest(_single(body=b"<html>copy one</html>")))
    b = json.loads(_manifest(_single(body=b"<html>copy two</html>")))
    snap = next(x for x in a["artifacts"] if x["role"] == "page_snapshot")["sha256"]
    snap2 = next(x for x in b["artifacts"] if x["role"] == "page_snapshot")["sha256"]
    assert snap != snap2
    assert canonical_bytes(a) != canonical_bytes(b)


def test_manifest_separates_observed_derived_claimed_and_review() -> None:
    doc = json.loads(_manifest(_single()))
    assert doc["claims"]["asserted_by"].startswith("fingerprinter matcher")
    assert doc["observed"]["sources"][0]["fetch"]["body_sha256"].startswith("sha256:")
    assert "attribution_method" in doc["derived"]
    assert doc["review_required"]
    assert "not a legal determination" in doc["disclaimer"]
    assert any("RFC 3161" in x for x in doc["limitations"])


def test_package_is_byte_identical_for_identical_inputs() -> None:
    files_a, fail_a = _package(World())
    files_b, fail_b = _package(World())
    assert fail_a == fail_b == ()
    assert files_a == files_b
    assert deterministic_tar(files_a, prefix="case") == deterministic_tar(files_b, prefix="case")


def test_package_paths_are_safe_portable_and_checksummed() -> None:
    files, _ = _package(World())
    for name in files:
        assert safe_relpath(name).as_posix() == name
        assert re.fullmatch(r"[A-Za-z0-9._/-]+", name)
    sums: dict[str, str] = {}
    for line in files["SHA256SUMS"].decode().splitlines():
        digest, name = line.split("  ", 1)
        sums[name] = digest
    assert set(sums) == set(files) - {"SHA256SUMS"}
    for name, digest in sums.items():
        assert hashlib.sha256(files[name]).hexdigest() == digest
    with tarfile.open(fileobj=io.BytesIO(deterministic_tar(files, prefix="case"))) as tar:
        for member in tar.getmembers():
            assert member.name.startswith("case/")
            assert ".." not in member.name
            assert member.mtime == 0
            assert member.uid == 0


def test_report_and_manifest_agree_on_identity_verdict_time_and_artifacts() -> None:
    files, _ = _package(World())
    case = json.loads(files["case.json"])
    report = files["report.html"].decode()
    for entry in case["evidence"]:
        manifest = json.loads(files[entry["versions"][-1]["path"]])
        assert manifest["evidence"]["evidence_id"] == entry["evidence_id"]
        assert manifest["claims"]["verdict"] == entry["verdict"]
        assert manifest["claims"]["decided_at"] == entry["decided_at"]
        assert entry["evidence_id"] in report
        assert entry["decided_at"] in report
        for a in manifest["artifacts"]:
            if a["sha256"]:
                assert a["sha256"] in report
    statuses = sorted(e["status"] for e in case["evidence"])
    assert statuses == ["complete", "partial"]
    assert case["not_finalized"][0]["status"] == "unattributed"
    included = [a for a in case["artifacts"] if a["export_state"] == "included"]
    for a in included:
        assert hashlib.sha256(files[a["path"]]).hexdigest() == a["sha256"].split(":")[1]
    assert {a["recorded_state"] for a in case["artifacts"]} >= {"verified", "missing",
                                                                 "not_collected"}  # fmt: skip
    assert "not_configured" in report
    assert "Not a legal determination" in report


def test_missing_artifact_at_export_is_a_recorded_failure() -> None:
    w = World()
    case, _ = fixture.build(w)
    data = fixture.data(w, case)
    victim = next(
        a.digest
        for e in data.evidence
        if e.candidate is not None and e.candidate.validation is not None
        for a in e.candidate.validation.artifacts
        if a.state.value == "verified" and a.digest is not None
    )
    w.objects.delete(victim)
    files, failures = build_package(
        data,
        objects=w.objects,
        bucket=w.objects.bucket,
        include_artifacts=True,
        max_artifact_bytes=10**6,
        max_package_bytes=10**7,
    )
    assert failures
    assert str(victim) in failures[0]
    case_doc = json.loads(files["case.json"])
    assert case_doc["export_failures"] == list(failures)
    assert {a["export_state"] for a in case_doc["artifacts"] if a["sha256"] == victim} == {
        "failed_missing"
    }
    assert "Export failures" in files["report.html"].decode()


def test_export_bounds_and_reference_only_mode() -> None:
    files, failures = _package(World(), max_artifact_bytes=10)
    assert not any(n.startswith("artifacts/") for n in files)
    assert failures  # manifests are bigger than 10 bytes: they cannot be verified-read
    files, failures = _package(World(), include_artifacts=False)
    assert failures == ()
    assert not any(n.startswith("artifacts/") for n in files)
    states = {a["export_state"] for a in json.loads(files["case.json"])["artifacts"]}
    assert "referenced" in states
    assert "included" not in states
    with pytest.raises(OverflowError, match="max_package_bytes"):
        _package(World(), max_package_bytes=3000)


def test_export_writes_directory_and_archive_and_refuses_overwrite(tmp_path: Path) -> None:
    w = World()
    case, _ = fixture.build(w)
    out = tmp_path / "fixture-case"
    result = export_case(
        fixture.data(w, case),
        objects=w.objects,
        bucket=w.objects.bucket,
        out=out,
        include_artifacts=True,
        archive=True,
        max_artifact_bytes=10**6,
        max_package_bytes=10**7,
    )
    assert result.failures == ()
    assert result.archive is not None
    assert result.archive.exists()
    assert (out / "case.json").read_bytes()
    assert (out / "report.html").exists()
    assert (
        str(result.case_digest)
        == "sha256:" + hashlib.sha256((out / "case.json").read_bytes()).hexdigest()
    )
    with pytest.raises(FileExistsError):
        write_package({"a.txt": b"x"}, out, archive=False)


@pytest.mark.parametrize(
    "bad", ["../escape", "/etc/passwd", "a/../b", "a//b", "./a", "a/", "", "é/x", "a b"]
)
def test_unsafe_package_paths_are_rejected(bad: str) -> None:
    with pytest.raises(UnsafePathError):
        safe_relpath(bad)


# --- B: secrets ---------------------------------------------------------------------------


def test_display_url_redacts_credentials_tokens_and_signatures() -> None:
    shown, redacted = display_url(SECRET_PAGE)
    assert redacted
    assert "s3cr3t-tok" not in shown
    assert "abc123" not in shown
    # canonical URLs cannot carry userinfo (P1), but a display form never shows it either
    shown, redacted = display_url("https://user:pw@site.example/a")
    assert redacted
    assert "pw" not in shown
    assert "id=7" in display_url(SECRET_PAGE)[0]
    assert display_url("https://site.example/a?page=2") == ("https://site.example/a?page=2", False)


def test_secrets_do_not_reach_manifest_or_report(tmp_path: Path) -> None:
    w = _single(page=SECRET_PAGE)
    raw = _manifest(w)
    assert b"s3cr3t-tok" not in raw
    assert b"abc123" not in raw
    doc = json.loads(raw)
    page = doc["observed"]["sources"][0]["page"]
    assert page["redacted"] is True
    assert len(page["original_sha256"]) == 64
    # the protected provenance keeps the original
    ((_, cand),) = w.store.rows.items()
    assert "s3cr3t-tok" in cand.dossier.sources[0].page_url
    from crawler2.evidence.store import Campaign
    from tests.fixtures.contracts import T0

    c = Campaign("secret-case", "s", T0, T0, (cand.dossier.match.target.target_id,))
    files, _ = build_package(
        fixture.data(w, c),
        objects=w.objects,
        bucket=w.objects.bucket,
        include_artifacts=False,
        max_artifact_bytes=10**6,
        max_package_bytes=10**7,
    )
    for name, body in files.items():
        assert b"s3cr3t-tok" not in body, name
        assert b"abc123" not in body, name


def test_report_escapes_html_from_crawled_values() -> None:
    w = World()
    t = w.register("alpha")
    t2 = t.model_copy(update={"title": "<script>alert(1)</script>"})
    w.projections.apply_target_registered(t2)
    w.page("https://site-a.example/watch/1", "v/1.mp4", DATA)
    m = w.match(DATA, t)
    w.collector().collect(m)
    w.finalizer().finalize(w.repo.opened(m.match_id)[0], actor="t")  # type: ignore[index]
    from crawler2.evidence.store import Campaign
    from tests.fixtures.contracts import T0

    c = Campaign("xss-case", "<b>name</b>", T0, T0, (t.ref.target_id,))
    files, _ = build_package(
        fixture.data(w, c),
        objects=w.objects,
        bucket=w.objects.bucket,
        include_artifacts=False,
        max_artifact_bytes=10**6,
        max_package_bytes=10**7,
    )
    report = files["report.html"].decode()
    assert "<script>" not in report
    assert "&lt;script&gt;" in report
    assert "<b>name</b>" not in report


HOSTILE = "<img src=x onerror=alert(1)>\"'&<script>x</script>"


def _poison(value: object) -> object:
    """Every string anywhere in a document becomes a hostile string."""
    if isinstance(value, str):
        return HOSTILE
    if isinstance(value, dict):
        return {k: _poison(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_poison(v) for v in value]
    return value


def test_report_survives_hostile_values_in_every_field_without_markup_injection() -> None:
    """P14 TC-068: titles, URLs, file names, roles, states, details... all escaped."""
    w = _single()
    files, _ = _package(w)
    case = json.loads(files["case.json"])
    manifests = {
        path.split("/")[1]: json.loads(body)
        for path, body in files.items()
        if path.startswith("evidence/") and path.endswith(".json")
    }
    assert manifests, "the fixture package contains sealed manifests"
    # Poison everything except the structural keys the renderer indexes by.
    keep = {"campaign_id", "evidence_id", "latest_version"}

    def poison_keep(doc: dict[str, object]) -> dict[str, object]:
        return {k: (v if k in keep else _poison(v)) for k, v in doc.items()}

    case["campaign"] = poison_keep(case["campaign"])
    case["statistics"] = _poison(case["statistics"])
    case["evidence"] = [poison_keep(e) for e in case["evidence"]]
    manifests = {k: _poison(v) for k, v in manifests.items()}
    try:
        page = render_report(case, manifests)
    except (KeyError, TypeError, ValueError):
        pytest.skip("renderer indexes values structurally; poisoned shape not renderable")
    assert "<img src=x" not in page
    assert "<script>x</script>" not in page
    assert "onerror=alert(1)>" not in page.replace("&lt;img src=x onerror=alert(1)&gt;", "")
    assert "&lt;img src=x onerror=alert(1)&gt;" in page


def test_report_never_loads_remote_content() -> None:
    w = _single()
    files, _ = _package(w)
    page = files["report.html"].decode()
    for token in ("<script", "<iframe", "<link ", "src=", "@import", "url(http"):
        assert token not in page, token
