"""The soak runner: preflight, start, resume, status, stop, evaluate.

One loop serves the 168 h run and the minutes-long dry run. State survives a crash: every
round appends the sample, ingests new events, then rewrites the checksummed checkpoint. A
restarted runner appends an ``interruption`` event for the time it was gone (the continuity
criterion decides whether that is acceptable) and continues; it never silently papers over it.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import psutil

from crawler2.soak.collectors import CollectorImpl, Reading, build_collector
from crawler2.soak.config import Mode, SoakConfig, redact
from crawler2.soak.evaluate import Evaluation, Inputs, evaluate
from crawler2.soak.reconcile import (
    CommandEvidenceVerifier,
    EvidenceVerifier,
    Ledger,
    StreamTailer,
    TailResult,
)
from crawler2.soak.store import RunStore, utc_iso
from crawler2.soak.synthetic import Faults, SyntheticVerifier, SyntheticWorkload

STOP_FILE = "STOP"
MIN_DISK_FREE_GB = 5.0


class Source(Protocol):
    """What feeds the ledger: ``StreamTailer`` (real) or ``SyntheticWorkload``."""

    def start_positions(self) -> dict[str, dict[str, Any]]: ...

    def poll(self, positions: dict[str, dict[str, Any]]) -> TailResult: ...


@dataclass
class Runtime:
    collectors: dict[str, CollectorImpl]
    source: Source
    ledger: Ledger
    verifier: EvidenceVerifier | None
    clock: Callable[[], float] = time.time
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail
    detail: str


# --- environment / fingerprints ------------------------------------------------------------


def _git(repo: str, *args: str) -> str | None:
    try:
        return subprocess.run(
            ["git", "-C", repo, *args], capture_output=True, text=True, timeout=30, check=True
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None


def git_state(repo: str, *, deep: bool) -> dict[str, Any]:
    state: dict[str, Any] = {
        "commit": _git(repo, "rev-parse", "HEAD"),
        "branch": _git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
    }
    porcelain = _git(repo, "status", "--porcelain", "--untracked-files=no")
    state["dirty"] = bool(porcelain)
    if deep:
        diff = _git(repo, "diff", "HEAD") or ""
        state["diff_sha256"] = hashlib.sha256(diff.encode()).hexdigest() if diff else ""
    return state


def hash_path(path: str) -> str:
    p = Path(path)
    digest = hashlib.sha256()
    files = (
        [p] if p.is_file() else sorted(f for f in p.rglob("*") if f.is_file()) if p.is_dir() else []
    )
    for f in files:
        digest.update(str(f.relative_to(p) if p.is_dir() else f.name).encode())
        digest.update(hashlib.sha256(f.read_bytes()).digest())
    return digest.hexdigest() if files else "missing"


def fingerprints(cfg: SoakConfig, *, deep: bool) -> dict[str, str]:
    fp = {
        "config": cfg.fingerprint(),
        "topology": hashlib.sha256(json.dumps(cfg.topology, sort_keys=True).encode()).hexdigest(),
    }
    for repo in cfg.repos:
        st = git_state(repo, deep=deep)
        fp[f"git:{repo}:commit"] = str(st["commit"])
        fp[f"git:{repo}:dirty"] = str(st["dirty"])
        if deep:
            fp[f"git:{repo}:diff"] = st["diff_sha256"]
    if deep:
        for path in cfg.watch_paths:
            fp[f"file:{path}"] = hash_path(path)
    return fp


def environment_info(cfg: SoakConfig) -> dict[str, Any]:
    vm = psutil.virtual_memory()
    disks = {}
    for part in psutil.disk_partitions(all=False):
        try:
            u = shutil.disk_usage(part.mountpoint)
        except OSError:
            continue
        disks[part.mountpoint] = {
            "device": part.device,
            "fstype": part.fstype,
            "total_gb": round(u.total / 2**30, 1),
            "free_gb": round(u.free / 2**30, 1),
        }
    gpu = None
    if shutil.which("nvidia-smi"):
        try:
            gpu = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,memory.total,driver_version",
                    "--format=csv,noheader",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=True,
            ).stdout.strip()
        except (subprocess.SubprocessError, OSError):
            gpu = "nvidia-smi failed"
    versions: dict[str, str] = {}
    from importlib import metadata

    for pkg in (
        "crawler2",
        "antipiracy-contracts",
        "redis",
        "httpx",
        "psutil",
        "cassandra-driver",
        "prometheus-client",
    ):
        try:
            versions[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            versions[pkg] = "not installed"
    return {
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "mem_total_gb": round(vm.total / 2**30, 1),
        "disks": disks,
        "gpu": gpu,
        "topology": cfg.topology,
        "versions": versions,
        "repos": {r: git_state(r, deep=False) for r in cfg.repos},
    }


# --- runtime construction ---------------------------------------------------------------------


def build_runtime(cfg: SoakConfig, store: RunStore) -> Runtime:
    ledger = Ledger(store.path / "ledger.sqlite3", cfg.edges)
    collectors = {c.name: build_collector(c) for c in cfg.collectors}
    source: Source
    if cfg.redis is not None:
        import redis

        r = cfg.redis
        client = redis.Redis(
            host=r.host,
            port=r.port,
            db=r.db,
            username=os.environ.get(r.username_env) if r.username_env else None,
            password=os.environ.get(r.password_env) if r.password_env else None,
            decode_responses=True,
            socket_timeout=10,
            socket_connect_timeout=5,
        )
        types = sorted(
            {e.from_type for e in cfg.edges}
            | {t for e in cfg.edges for t in e.to_types}
            | set(cfg.required_stages)
            | {"evidence.finalized"}
        )
        streams = {t: f"{r.stream_prefix}{t}.v1" for t in types}
        source = StreamTailer(client, streams, ledger)
    elif cfg.mode is Mode.SYNTHETIC:
        source = SyntheticWorkload(ledger, faults=Faults(finalize_every=3))
    else:
        raise ValueError("production mode needs a [redis] section to observe the pipeline")
    verifier: EvidenceVerifier | None = None
    if cfg.evidence.enabled:
        verifier = (
            CommandEvidenceVerifier(cfg.evidence)
            if cfg.mode is Mode.PRODUCTION
            else SyntheticVerifier()
        )
    return Runtime(collectors, source, ledger, verifier)


# --- preflight -----------------------------------------------------------------------------------


def preflight(cfg: SoakConfig, run_root: Path | None = None) -> list[Check]:
    checks: list[Check] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append(Check(name, status, detail))

    th = cfg.thresholds
    add("mode", "ok", f"{cfg.mode.value}, duration {cfg.duration_h:g} h, dry_run={cfg.dry_run}")
    if cfg.m6_eligible:
        if not cfg.approval.complete:
            add(
                "approval",
                "fail",
                "approval.approved_by/approved_at/reference are required for an M6-eligible run",
            )
        else:
            add(
                "approval",
                "ok",
                f"limits approved by {cfg.approval.approved_by} ({cfg.approval.reference})",
            )
        if not cfg.topology:
            add("topology", "fail", "topology is empty: record the physical hosts and roles")
        if cfg.redis is None:
            add("redis", "fail", "[redis] section missing: the pipeline cannot be observed")
        if not cfg.repos:
            add("repos", "fail", "repos is empty: the code revision would not be pinned")
        undecided = [k for k, v in th.model_dump().items() if v is None or v == {}]
        if undecided:
            add(
                "thresholds",
                "warn",
                f"undecided limits (their criteria will end INCONCLUSIVE): {undecided}",
            )
        else:
            add("thresholds", "ok", "all limits decided")
        if any(e.grace_s is None and e.required for e in cfg.edges):
            add("edges", "warn", "required edges without grace_s cannot be judged")
        if "match.found" not in cfg.required_stages:
            add(
                "stages",
                "warn",
                "match.found is not a required stage: the soak could pass without a real match",
            )
        for repo in cfg.repos:
            st = git_state(repo, deep=False)
            if st["commit"] is None:
                add(f"git:{repo}", "fail", "not a git repository")
            elif st["dirty"]:
                add(
                    f"git:{repo}",
                    "fail",
                    "working tree has uncommitted changes (the run would not be reproducible)",
                )
            else:
                add(f"git:{repo}", "ok", f"clean at {str(st['commit'])[:12]}")
    root = run_root or cfg.run_root
    try:
        root.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(root).free / 2**30
        add(
            "run_root",
            "ok" if free >= MIN_DISK_FREE_GB else "fail",
            f"{root}: {free:.1f} GB free (need {MIN_DISK_FREE_GB:g})",
        )
    except OSError as exc:
        add("run_root", "fail", f"{root}: {exc}")
    series_expected = set(cfg.series_names())
    if not series_expected:
        add("collectors", "fail" if cfg.m6_eligible else "warn", "no series configured")
    for c in cfg.collectors:
        try:
            reading = build_collector(c).collect()
        except Exception as exc:  # noqa: BLE001
            add(f"collector:{c.name}", "fail", f"cannot collect: {str(exc)[:120]}")
            continue
        missing = [s.name for s in c.series if s.required and reading.values.get(s.name) is None]
        if missing:
            add(
                f"collector:{c.name}",
                "fail" if cfg.m6_eligible else "warn",
                f"required series unavailable now: {missing[:6]} "
                f"({[reading.errors.get(m) for m in missing[:2]]})",
            )
        else:
            add(f"collector:{c.name}", "ok", f"{len(c.series)} series readable")
    if cfg.redis is not None:
        try:
            import redis

            r = cfg.redis
            client = redis.Redis(
                host=r.host,
                port=r.port,
                db=r.db,
                username=os.environ.get(r.username_env) if r.username_env else None,
                password=os.environ.get(r.password_env) if r.password_env else None,
                socket_timeout=5,
                socket_connect_timeout=5,
            )
            client.ping()
            add("redis", "ok", f"{r.host}:{r.port} reachable")
        except Exception as exc:  # noqa: BLE001
            add("redis", "fail", f"unreachable: {str(exc)[:100]}")
    return checks


def preflight_ok(checks: list[Check]) -> bool:
    return all(c.status != "fail" for c in checks)


# --- the loop ---------------------------------------------------------------------------


@dataclass
class _State:
    started_at: float
    seq: int = 0
    last_ts: float = 0.0
    last_mono: float = 0.0
    phase: str = "running"
    identities: dict[str, list[str]] = field(default_factory=dict)
    seen_up: dict[str, bool] = field(default_factory=dict)
    positions: dict[str, dict[str, Any]] = field(default_factory=dict)
    tail_gaps: list[dict[str, Any]] = field(default_factory=list)
    tail_errors: int = 0
    fingerprints_now: dict[str, str] = field(default_factory=dict)
    stop_reason: str | None = None

    def dump(self) -> dict[str, Any]:
        return dict(self.__dict__)


class SoakRunner:
    def __init__(self, cfg: SoakConfig, store: RunStore, rt: Runtime) -> None:
        self.cfg, self.store, self.rt = cfg, store, rt
        store.clock = rt.clock
        self._stop = False
        self.state = _State(started_at=rt.clock())

    def request_stop(self, *_: object) -> None:
        self._stop = True

    # -- lifecycle
    def initialize(self) -> None:
        cfg, store, rt = self.cfg, self.store, self.rt
        store.write_json("config.json", redact(cfg.model_dump(mode="json")))
        fps = fingerprints(cfg, deep=True)
        store.write_json(
            "run.json",
            {
                "run_id": store.run_id,
                "mode": cfg.mode.value,
                "dry_run": cfg.dry_run,
                "m6_eligible": cfg.m6_eligible,
                "started_at": rt.clock(),
                "started_iso": utc_iso(rt.clock()),
                "duration_h": cfg.duration_h,
                "environment": environment_info(cfg),
                "fingerprints": fps,
                "preflight": [c.__dict__ for c in preflight(cfg, store.path.parent)],
            },
        )
        self.state = _State(
            started_at=rt.clock(),
            last_ts=rt.clock(),
            last_mono=rt.monotonic(),
            fingerprints_now=fps,
        )
        self.state.positions = rt.source.start_positions()
        store.event("start", run_id=store.run_id)
        store.save_checkpoint(self.state.dump())

    def resume_state(self) -> None:
        store, rt = self.store, self.rt
        data, source = store.load_checkpoint()
        run = store.read_json("run.json") or {}
        if data is None:
            samples = store.read_jsonl("samples.jsonl").rows
            started = run.get("started_at", rt.clock())
            self.state = _State(
                started_at=started,
                seq=len(samples),
                last_ts=samples[-1]["ts"] if samples else started,
            )
            self.state.tail_gaps.append(
                {
                    "stream": "*",
                    "event_type": "*",
                    "entries_lost": -1,
                    "reason": f"checkpoint {source}: stream positions lost",
                }
            )
            store.event("checkpoint_recovered", source=source, samples=len(samples))
        else:
            known = {k: v for k, v in data.items() if k in _State.__dataclass_fields__}
            self.state = _State(**known)
            if source == "previous":
                store.event("checkpoint_recovered", source=source)
        if self.state.phase == "completed":
            raise RuntimeError("run already completed")
        now = rt.clock()
        gap = now - self.state.last_ts
        if gap < -5:
            store.event(
                "clock_anomaly",
                detail=f"wall clock is {-gap:.0f} s behind the last recorded sample",
            )
        elif gap > 2 * self.cfg.sample_interval_s:
            store.event(
                "interruption",
                gap_s=gap,
                from_ts=self.state.last_ts,
                to_ts=now,
                reason="runner_restart",
            )
        self.state.phase = "running"
        self.state.last_ts = now  # monotonic time restarts with the process: compare afresh
        self.state.last_mono = rt.monotonic()

    def stop_requested(self) -> bool:
        return self._stop or (self.store.path / STOP_FILE).exists()

    # -- one round
    def round(self) -> None:
        cfg, store, rt, st = self.cfg, self.store, self.rt, self.state
        now, mono = rt.clock(), rt.monotonic()
        if st.last_mono and st.last_ts:
            wall_d, mono_d = now - st.last_ts, mono - st.last_mono
            if abs(wall_d - mono_d) > max(5.0, 0.1 * cfg.sample_interval_s) and wall_d < 3600:
                store.event(
                    "clock_anomaly", detail=f"wall delta {wall_d:.1f}s vs monotonic {mono_d:.1f}s"
                )
        values: dict[str, float | None] = {}
        errors: dict[str, str] = {}
        for name, collector in self.rt.collectors.items():
            try:
                reading: Reading = collector.collect()
            except Exception as exc:  # noqa: BLE001 -- a broken collector is a missing metric, not a dead run
                declared = [s.name for c in cfg.collectors if c.name == name for s in c.series]
                reading = Reading(
                    values={s: None for s in declared},
                    errors={s: f"collector crashed: {str(exc)[:80]}" for s in declared},
                )
            for k, v in reading.values.items():
                values[f"{name}.{k}"] = v
            for k, e in reading.errors.items():
                errors[f"{name}.{k}"] = e
            self._restarts(name, reading, now)
        st.seq += 1
        store.append(
            "samples.jsonl",
            {"seq": st.seq, "ts": now, "at": utc_iso(now), "values": values, "errors": errors},
        )
        try:
            tail = rt.source.poll(st.positions)
            st.tail_gaps.extend(tail.gaps)
            st.tail_errors += len(tail.errors)
            for gap in tail.gaps:
                store.event("stream_gap", **gap)
            for err in tail.errors:
                store.event("tail_error", detail=err)
        except Exception as exc:  # noqa: BLE001
            st.tail_errors += 1
            store.event("tail_error", detail=str(exc)[:160])
        deep = st.seq % 10 == 0
        st.fingerprints_now = {**st.fingerprints_now, **fingerprints(cfg, deep=deep)}
        st.last_ts, st.last_mono = now, mono
        store.heartbeat()
        store.save_checkpoint(st.dump())

    def _restarts(self, collector: str, reading: Reading, now: float) -> None:
        for label, ids in reading.identities.items():
            key = f"{collector}.{label}"
            prev = self.state.identities.get(key)
            if ids:
                if (
                    prev is not None
                    and set(prev) != set(ids)
                    and (prev or self.state.seen_up.get(key))
                ):
                    self.store.event("restart", service=key, before=prev, after=ids)
                self.state.seen_up[key] = True
            self.state.identities[key] = ids

    def run(self) -> str:
        """Loop until the duration elapsed or a stop is requested; returns the final phase."""
        cfg, rt, st = self.cfg, self.rt, self.state
        end = st.started_at + cfg.duration_h * 3600
        interval = cfg.sample_interval_s
        next_tick = rt.clock()
        while True:
            self.round()
            if self.stop_requested():
                st.phase, st.stop_reason = "stopped", "operator stop"
                self.store.event("stop", reason=st.stop_reason)
                break
            if rt.clock() >= end:
                self._finish()
                break
            next_tick += interval
            late = rt.clock() - next_tick
            if late > 2 * interval:
                self.store.event("sampling_overrun", late_s=late)
                next_tick = rt.clock()
            while (wait := next_tick - rt.clock()) > 0 and not self.stop_requested():
                rt.sleep(min(wait, 1.0))
        self.store.save_checkpoint(st.dump())
        return st.phase

    def _finish(self) -> None:
        st = self.state
        try:
            tail = self.rt.source.poll(st.positions)
            st.tail_gaps.extend(tail.gaps)
        except Exception as exc:  # noqa: BLE001
            st.tail_errors += 1
            self.store.event("tail_error", detail=str(exc)[:160])
        if self.rt.verifier is not None:
            ids = self.rt.ledger.finalized_evidence(self.cfg.evidence.sample)
            res = self.rt.verifier.verify(ids)
            self.store.write_json("evidence_check.json", res.__dict__)
        st.phase = "completed"
        self.store.event("completed")


# --- operator entry points --------------------------------------------------------------


def run_new(
    cfg: SoakConfig,
    run_root: Path | None = None,
    rt_factory: Callable[[SoakConfig, RunStore], Runtime] = build_runtime,
) -> tuple[RunStore, str]:
    store = RunStore.create(run_root or cfg.run_root, cfg.run_name)
    with store.locked():
        rt = rt_factory(cfg, store)
        runner = SoakRunner(cfg, store, rt)
        runner.initialize()
        _install_signals(runner)
        try:
            phase = runner.run()
        finally:
            rt.ledger.close()
    return store, phase


def resume_run(
    run_dir: Path, rt_factory: Callable[[SoakConfig, RunStore], Runtime] = build_runtime
) -> tuple[RunStore, str]:
    store = RunStore(run_dir)
    cfg_doc = store.read_json("config.json")
    if cfg_doc is None:
        raise RuntimeError("config.json missing: cannot resume")
    cfg = SoakConfig.model_validate(cfg_doc)
    with store.locked():
        rt = rt_factory(cfg, store)
        runner = SoakRunner(cfg, store, rt)
        runner.resume_state()
        store.event("resume", pid=os.getpid())
        _install_signals(runner)
        try:
            phase = runner.run()
        finally:
            rt.ledger.close()
    return store, phase


def _install_signals(runner: SoakRunner) -> None:
    try:
        signal.signal(signal.SIGTERM, runner.request_stop)
        signal.signal(signal.SIGINT, runner.request_stop)
    except ValueError:  # not the main thread (tests)
        pass


def request_stop(run_dir: Path) -> None:
    (run_dir / STOP_FILE).write_text(utc_iso())


def clear_stop(run_dir: Path) -> None:
    (run_dir / STOP_FILE).unlink(missing_ok=True)


def load_config_of(store: RunStore) -> SoakConfig:
    doc = store.read_json("config.json")
    if doc is None:
        raise RuntimeError(f"{store.path}: config.json missing")
    return SoakConfig.model_validate(doc)


def evaluate_run(store: RunStore, now: float | None = None) -> Evaluation:
    cfg = load_config_of(store)
    now = time.time() if now is None else now
    run = store.read_json("run.json") or {}
    ckpt, source = store.load_checkpoint()
    ckpt = ckpt or {"phase": "unknown"}
    if (
        ckpt.get("phase") == "running"
        and not store.runner_alive()
        and now - ckpt.get("last_ts", 0.0) > cfg.heartbeat_stale_s
    ):
        # The runner died and nobody resumed it: that is an interruption, not "still running".
        ckpt = {**ckpt, "phase": "interrupted"}
    samples = store.read_jsonl("samples.jsonl")
    events = store.read_jsonl("events.jsonl").rows
    if source == "previous":
        events = [
            *events,
            {"kind": "note", "detail": "checkpoint recovered from previous generation"},
        ]
    ledger = Ledger(store.path / "ledger.sqlite3", cfg.edges)
    try:
        ev_doc = store.read_json("evidence_check.json")
        from crawler2.soak.reconcile import EvidenceResult

        evidence = EvidenceResult(**ev_doc) if ev_doc else None
        inputs = Inputs(
            config=cfg,
            run=run,
            checkpoint=ckpt,
            samples=samples.rows,
            events=events,
            corrupt_sample_lines=samples.corrupt_lines,
            edges=ledger.edge_results(now, run.get("started_at", 0.0)),
            stage_counts=ledger.stage_counts(),
            redeliveries=ledger.redeliveries(),
            tail_gaps=ckpt.get("tail_gaps", []),
            tail_errors=ckpt.get("tail_errors", 0),
            evidence=evidence,
            finalized_evidence=len(ledger.finalized_evidence(10**6)),
            now=now,
        )
        return evaluate(inputs)
    finally:
        ledger.close()


def status(store: RunStore, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    run = store.read_json("run.json") or {}
    ckpt, source = store.load_checkpoint()
    ckpt = ckpt or {}
    alive = store.runner_alive()
    lock = store.lock_info() or {}
    events = store.read_jsonl("events.jsonl").rows
    started = run.get("started_at")
    cfg_h = run.get("duration_h", 0)
    elapsed = (now - started) if started else 0.0
    last_ts = ckpt.get("last_ts")
    return {
        "run_id": store.run_id,
        "mode": run.get("mode"),
        "m6_eligible": run.get("m6_eligible"),
        "phase": ckpt.get("phase", "unknown"),
        "runner_alive": alive,
        "runner_pid": lock.get("pid") if alive else None,
        "checkpoint": source,
        "elapsed_h": round(elapsed / 3600, 3),
        "required_h": cfg_h,
        "progress": round(min(1.0, elapsed / (cfg_h * 3600)), 4) if cfg_h else None,
        "samples": ckpt.get("seq", 0),
        "last_sample_age_s": round(now - last_ts, 1) if last_ts else None,
        "interruptions": sum(1 for e in events if e.get("kind") == "interruption"),
        "restarts": sum(1 for e in events if e.get("kind") == "restart"),
        "anomalies": sum(
            1
            for e in events
            if e.get("kind") in {"clock_anomaly", "stream_gap", "tail_error", "sampling_overrun"}
        ),
        "needs_resume": ckpt.get("phase") == "running" and not alive,
    }
