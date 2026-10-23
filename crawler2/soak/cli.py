"""``crawler2-soak``: prepare / start / resume / status / stop / report / dry-run.

Exit codes: 0 ok (``report``: verdict PASS), 1 failed or not a pass, 2 usage/preflight error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from crawler2.soak import runner
from crawler2.soak.config import SoakConfig, load_config
from crawler2.soak.evaluate import Verdict
from crawler2.soak.report import write_reports
from crawler2.soak.store import RunStore

EXIT_OK, EXIT_NOT_PASS, EXIT_USAGE = 0, 1, 2


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=True, default=str))


def _config(path: str) -> SoakConfig:
    return load_config(Path(path))


def cmd_prepare(args: argparse.Namespace) -> int:
    cfg = _config(args.config)
    checks = runner.preflight(cfg)
    for c in checks:
        print(f"[{c.status.upper():4}] {c.name}: {c.detail}")
    ok = runner.preflight_ok(checks)
    print("PREFLIGHT " + ("OK" if ok else "FAILED"))
    return EXIT_OK if ok else EXIT_USAGE


def cmd_start(args: argparse.Namespace) -> int:
    cfg = _config(args.config)
    checks = runner.preflight(cfg)
    if not runner.preflight_ok(checks):
        for c in checks:
            if c.status == "fail":
                print(f"[FAIL] {c.name}: {c.detail}", file=sys.stderr)
        print("refusing to start: preflight failed (run `crawler2-soak prepare`)", file=sys.stderr)
        return EXIT_USAGE
    store, phase = runner.run_new(cfg)
    print(f"run {store.run_id} ended in phase {phase}")
    return _finish(store, phase)


def cmd_dry_run(args: argparse.Namespace) -> int:
    cfg = _config(args.config)
    if cfg.m6_eligible:
        print("dry-run needs dry_run=true or mode=synthetic in the config", file=sys.stderr)
        return EXIT_USAGE
    return cmd_start(args)


def cmd_resume(args: argparse.Namespace) -> int:
    store = RunStore(Path(args.run_dir))
    runner.clear_stop(store.path)
    try:
        store, phase = runner.resume_run(store.path)
    except Exception as exc:  # noqa: BLE001 -- operator-facing message
        print(f"cannot resume: {exc}", file=sys.stderr)
        return EXIT_USAGE
    print(f"run {store.run_id} ended in phase {phase}")
    return _finish(store, phase)


def _finish(store: RunStore, phase: str) -> int:
    evaluation = runner.evaluate_run(store)
    paths = write_reports(store, evaluation)
    print(f"verdict {evaluation.verdict.value}; report: {paths['report.html']}")
    return (
        EXIT_OK
        if phase in {"stopped", "completed"} and evaluation.verdict is not Verdict.FAIL
        else EXIT_NOT_PASS
    )


def cmd_status(args: argparse.Namespace) -> int:
    _print(runner.status(RunStore(Path(args.run_dir))))
    return EXIT_OK


def cmd_stop(args: argparse.Namespace) -> int:
    runner.request_stop(Path(args.run_dir))
    print(
        "stop requested; the runner finishes its round, checkpoints and exits (resume is possible)"
    )
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    store = RunStore(Path(args.run_dir))
    evaluation = runner.evaluate_run(store)
    paths = write_reports(store, evaluation)
    _print(
        {
            "verdict": evaluation.verdict.value,
            "m6_eligible": evaluation.m6_eligible,
            "banner": evaluation.banner,
            **paths,
        }
    )
    return EXIT_OK if evaluation.verdict is Verdict.PASS else EXIT_NOT_PASS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crawler2-soak", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, fn, helptext in (
        ("prepare", cmd_prepare, "preflight checks (no run is created)"),
        ("start", cmd_start, "preflight, then run until the duration elapses or a stop"),
        ("dry-run", cmd_dry_run, "short mechanics check (needs dry_run=true or synthetic mode)"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("config")
        p.set_defaults(func=fn)
    for name, fn, helptext in (
        ("resume", cmd_resume, "continue a run whose runner died (records the interruption)"),
        ("status", cmd_status, "progress, liveness and anomaly counts"),
        ("stop", cmd_stop, "ask the runner to stop gracefully"),
        ("report", cmd_report, "evaluate and write report.json/.md/.html"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("run_dir")
        p.set_defaults(func=fn)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
