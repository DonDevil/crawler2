"""``crawler2-backup``: create / verify / restore / prune backup sets.

    crawler2-backup create DEST --scylla-container NAME --keyspace crawler2 --bucket crawler2-raw
    crawler2-backup verify DEST
    crawler2-backup restore DEST --scylla-container NAME --keyspace-map crawler2=crawler2_restored
                                 --bucket-map crawler2-raw=crawler2-raw-restored
    crawler2-backup prune ROOT --keep 7 [--yes]

MinIO credentials come from the usual CRAWLER2_MINIO__* settings. A restore only ever writes to
new names. Exit 0 = success; 1 = a verification or integrity failure; 2 = refused/usage.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from crawler2.core.configuration import Settings
from crawler2.ops import backup as b


def _git(repo: str) -> dict[str, str | None]:
    def out(*args: str) -> str | None:
        try:
            return b.run_command(["git", "-C", repo, *args], timeout=30).strip()
        except b.BackupError:
            return None

    return {"commit": out("rev-parse", "HEAD"), "branch": out("rev-parse", "--abbrev-ref", "HEAD")}


def cmd_create(args: argparse.Namespace) -> int:
    settings = Settings()
    targets = b.Targets(
        scylla=b.ScyllaHost(container=args.scylla_container, data_dir=args.scylla_data_dir)
        if args.keyspace
        else None,
        keyspaces=args.keyspace,
        minio=settings.minio if args.bucket else None,
        buckets=args.bucket,
        redis_container=args.redis_container,
    )
    provenance = {"git": {r: _git(r) for r in args.repo}, "environment": settings.environment.value}
    manifest = b.create_backup(Path(args.dest), targets, tag=args.tag, provenance=provenance)
    rep = b.verify_backup(Path(args.dest))
    print(
        json.dumps(
            {
                "dest": args.dest,
                "tag": manifest["tag"],
                "files": len(manifest["files"]),
                "verify_ok": rep.ok,
            },
            indent=2,
        )
    )
    return 0 if rep.ok else 1


def cmd_verify(args: argparse.Namespace) -> int:
    rep = b.verify_backup(Path(args.dest))
    print(json.dumps(rep.__dict__ | {"ok": rep.ok}, indent=2, default=str))
    return 0 if rep.ok else 1


def _pairs(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        src, sep, dst = item.partition("=")
        if not sep or not src or not dst:
            raise b.BackupError(f"expected SRC=DST, got {item!r}")
        out[src] = dst
    return out


def cmd_restore(args: argparse.Namespace) -> int:
    dest = Path(args.dest)
    rep = b.verify_backup(dest)
    if not rep.ok:
        print("refusing to restore: the backup set does not verify", file=sys.stderr)
        return 1
    settings = Settings()
    result: dict[str, object] = {}
    for src, dst in _pairs(args.keyspace_map).items():
        host = b.ScyllaHost(container=args.scylla_container, data_dir=args.scylla_data_dir)
        result[f"scylla:{src}->{dst}"] = b.restore_scylla(host, src, dst, dest, b.run_command)
    scratch = Path(args.scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    for src, dst in _pairs(args.bucket_map).items():
        result[f"minio:{src}->{dst}"] = b.restore_minio(settings.minio, src, dst, dest, scratch)
    print(json.dumps(result, indent=2))
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    root = Path(args.root)
    sets = sorted(
        (p for p in root.iterdir() if (p / "MANIFEST.json").is_file()), key=lambda p: p.name
    )
    old = sets[: max(0, len(sets) - args.keep)]
    for p in old:
        print(("deleting " if args.yes else "would delete ") + str(p))
        if args.yes:
            shutil.rmtree(p)
    if old and not args.yes:
        print("dry run: pass --yes to delete", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crawler2-backup", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create", help="write a new backup set")
    p.add_argument("dest")
    p.add_argument("--keyspace", action="append", default=[])
    p.add_argument("--bucket", action="append", default=[])
    p.add_argument(
        "--scylla-container", default=None, help="docker container (omit on the Scylla host)"
    )
    p.add_argument("--scylla-data-dir", default="/var/lib/scylla/data")
    p.add_argument("--redis-container", default=None)
    p.add_argument("--repo", action="append", default=[], help="git repos whose HEAD to record")
    p.add_argument("--tag", default=None)
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("verify", help="re-read and cross-check a backup set")
    p.add_argument("dest")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("restore", help="load a backup set into NEW keyspaces/buckets")
    p.add_argument("dest")
    p.add_argument("--keyspace-map", action="append", default=[], metavar="SRC=DST")
    p.add_argument("--bucket-map", action="append", default=[], metavar="SRC=DST")
    p.add_argument("--scylla-container", default=None)
    p.add_argument("--scylla-data-dir", default="/var/lib/scylla/data")
    p.add_argument("--scratch", default="var/backup-scratch")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("prune", help="delete the oldest sets under ROOT (dry run by default)")
    p.add_argument("root")
    p.add_argument("--keep", type=int, required=True)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_prune)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except b.BackupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
