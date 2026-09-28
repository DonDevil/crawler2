"""Deterministic, additive schema migrations for the crawler2 keyspace.

- Migrations are ``cql/V<NNN>__<name>.cql`` files, applied in version
  order, each exactly once. ``${keyspace}`` is the only substitution.
- History lives in ``<keyspace>.schema_migrations`` with the file checksum;
  an applied migration whose file changed is an error, never re-applied.
- Only additive DDL (``CREATE ... IF NOT EXISTS``, ``ALTER ... ADD``) is
  accepted; ``DROP``/``TRUNCATE``/``DELETE`` are refused at load time.
- One migrator at a time (an LWT lease row with a TTL); repositories never
  mutate schema.
- The keyspace itself is created from configuration (replication is
  per-environment, ADR-002) with tablets disabled: Scylla 6.2 tablets do
  not support LWT, which E1/E2 require (ADR-012).
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from string import Template

from crawler2.core.configuration import ReplicationStrategy, ScyllaSettings
from crawler2.storage.errors import SchemaError
from crawler2.storage.scylla.session import Consistency, ScyllaSession, utc

MIGRATIONS_DIR = Path(__file__).with_name("cql")
_FILE_RE = re.compile(r"^V(\d{3})__([a-z0-9_]+)\.cql$")
_FORBIDDEN_RE = re.compile(r"\b(DROP|TRUNCATE|DELETE)\b", re.IGNORECASE)
_TABLE_RE = re.compile(r"CREATE TABLE IF NOT EXISTS \$\{keyspace\}\.(\w+)", re.IGNORECASE)
_LOCK_TTL_S = 300


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    source: str
    checksum: str

    def statements(self, keyspace: str) -> list[str]:
        text = Template(self.source).substitute(keyspace=keyspace)
        lines = [line for line in text.splitlines() if not line.lstrip().startswith("--")]
        return [part.strip() for part in "\n".join(lines).split(";") if part.strip()]

    @property
    def tables(self) -> tuple[str, ...]:
        return tuple(_TABLE_RE.findall(self.source))


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str
    applied_at: datetime
    applied_by: str


@dataclass(frozen=True, slots=True)
class SchemaStatus:
    keyspace: str
    keyspace_exists: bool
    applied: tuple[AppliedMigration, ...]
    pending: tuple[Migration, ...]
    problems: tuple[str, ...]

    @property
    def current_version(self) -> int:
        return max((m.version for m in self.applied), default=0)

    @property
    def up_to_date(self) -> bool:
        return self.keyspace_exists and not self.pending and not self.problems


def load_migrations(directory: Path = MIGRATIONS_DIR) -> tuple[Migration, ...]:
    migrations: list[Migration] = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".cql":
            continue
        match = _FILE_RE.fullmatch(path.name)
        if match is None:
            raise SchemaError(f"migration file name must be V<NNN>__<name>.cql: {path.name}")
        source = path.read_text(encoding="utf-8")
        code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("--"))
        if _FORBIDDEN_RE.search(code):
            raise SchemaError(f"{path.name}: destructive statements are not allowed")
        migrations.append(
            Migration(
                version=int(match.group(1)),
                name=match.group(2),
                source=source,
                checksum=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            )
        )
    migrations.sort(key=lambda m: m.version)
    expected = list(range(1, len(migrations) + 1))
    if [m.version for m in migrations] != expected:
        raise SchemaError(
            f"migration versions must be 1..N without gaps or duplicates: "
            f"{[m.version for m in migrations]}"
        )
    return tuple(migrations)


def _replication(settings: ScyllaSettings) -> str:
    if settings.replication_strategy is ReplicationStrategy.SIMPLE:
        return f"{{'class': 'SimpleStrategy', 'replication_factor': {settings.replication_factor}}}"
    return (
        f"{{'class': 'NetworkTopologyStrategy', "
        f"'{settings.local_dc}': {settings.replication_factor}}}"
    )


class Migrator:
    def __init__(
        self,
        session: ScyllaSession,
        settings: ScyllaSettings,
        *,
        applied_by: str,
        migrations: tuple[Migration, ...] | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._applied_by = applied_by
        self._migrations = load_migrations() if migrations is None else migrations
        self._ks = session.keyspace

    @property
    def migrations(self) -> tuple[Migration, ...]:
        return self._migrations

    # --- inspection -----------------------------------------------------------

    def _keyspace_exists(self) -> bool:
        rows = self._session.execute(
            self._session.bind(
                "SELECT keyspace_name FROM system_schema.keyspaces WHERE keyspace_name = ?",
                Consistency.STRONG_READ,
                (self._ks,),
            )
        )
        return bool(rows)

    def _history_exists(self) -> bool:
        rows = self._session.execute(
            self._session.bind(
                "SELECT table_name FROM system_schema.tables "
                "WHERE keyspace_name = ? AND table_name = 'schema_migrations'",
                Consistency.STRONG_READ,
                (self._ks,),
            )
        )
        return bool(rows)

    def _applied(self) -> tuple[AppliedMigration, ...]:
        if not self._history_exists():
            return ()
        rows = self._session.execute(
            self._session.bind(
                "SELECT version, name, checksum, applied_at, applied_by "
                "FROM {ks}.schema_migrations",
                Consistency.STRONG_READ,
                (),
            )
        )
        applied = [
            AppliedMigration(r.version, r.name, r.checksum, utc(r.applied_at), r.applied_by)
            for r in rows
        ]
        return tuple(sorted(applied, key=lambda m: m.version))

    def _existing_tables(self) -> set[str]:
        rows = self._session.execute(
            self._session.bind(
                "SELECT table_name FROM system_schema.tables WHERE keyspace_name = ?",
                Consistency.STRONG_READ,
                (self._ks,),
            )
        )
        return {r.table_name for r in rows}

    def _tablets_enabled(self) -> bool:
        rows = self._session.execute(
            self._session.bind(
                "SELECT initial_tablets FROM system_schema.scylla_keyspaces "
                "WHERE keyspace_name = ?",
                Consistency.STRONG_READ,
                (self._ks,),
            )
        )
        return bool(rows) and rows[0].initial_tablets is not None

    def status(self) -> SchemaStatus:
        if not self._keyspace_exists():
            return SchemaStatus(self._ks, False, (), self._migrations, ())
        applied = self._applied()
        by_version = {m.version: m for m in self._migrations}
        problems: list[str] = []
        for record in applied:
            known = by_version.get(record.version)
            if known is None:
                problems.append(f"V{record.version:03d} is applied but unknown to this build")
            elif known.checksum != record.checksum:
                problems.append(
                    f"V{record.version:03d} ({record.name}) changed after it was applied"
                )
        applied_versions = {m.version for m in applied}
        if sorted(applied_versions) != list(range(1, len(applied_versions) + 1)):
            problems.append(f"applied versions are not contiguous: {sorted(applied_versions)}")
        if self._tablets_enabled():
            problems.append("keyspace uses tablets; LWT (E1/E2, schema lock) is unavailable")
        pending = tuple(m for m in self._migrations if m.version not in applied_versions)
        if applied_versions and not pending:
            missing = {t for m in self._migrations for t in m.tables} - self._existing_tables()
            if missing:
                problems.append(f"tables missing although applied: {sorted(missing)}")
        return SchemaStatus(self._ks, True, applied, pending, tuple(problems))

    # --- mutation ---------------------------------------------------------------

    def _bootstrap(self) -> None:
        self._session.execute_raw(
            f"CREATE KEYSPACE IF NOT EXISTS {self._ks} "
            f"WITH replication = {_replication(self._settings)} "
            f"AND durable_writes = true AND tablets = {{'enabled': false}}"
        )
        self._session.execute_raw(
            f"CREATE TABLE IF NOT EXISTS {self._ks}.schema_migrations ("
            "version int PRIMARY KEY, name text, checksum text, "
            "applied_at timestamp, applied_by text)"
        )
        self._session.execute_raw(
            f"CREATE TABLE IF NOT EXISTS {self._ks}.schema_lock ("
            "name text PRIMARY KEY, owner text, acquired_at timestamp)"
        )

    def _acquire_lock(self, wait_s: float) -> None:
        deadline = time.monotonic() + wait_s
        while True:
            rows = self._session.execute(
                self._session.bind(
                    "INSERT INTO {ks}.schema_lock (name, owner, acquired_at) "
                    f"VALUES ('migrate', ?, ?) IF NOT EXISTS USING TTL {_LOCK_TTL_S}",
                    Consistency.SERIAL,
                    (self._applied_by, datetime.now(UTC)),
                )
            )
            if rows[0].applied or rows[0].owner == self._applied_by:
                return
            if time.monotonic() >= deadline:
                raise SchemaError(f"schema migration already in progress by {rows[0].owner}")
            time.sleep(1.0)

    def _release_lock(self) -> None:
        self._session.execute(
            self._session.bind(
                "DELETE FROM {ks}.schema_lock WHERE name = 'migrate' IF owner = ?",
                Consistency.SERIAL,
                (self._applied_by,),
            )
        )

    def migrate(self, *, lock_wait_s: float = 60.0) -> tuple[int, ...]:
        """Create the keyspace if needed and apply pending migrations. Returns applied versions."""
        self._bootstrap()
        self._acquire_lock(lock_wait_s)
        try:
            status = self.status()
            if status.problems:
                raise SchemaError("; ".join(status.problems))
            done: list[int] = []
            for migration in status.pending:
                for statement in migration.statements(self._ks):
                    self._session.execute_raw(statement)
                self._session.execute(
                    self._session.bind(
                        "INSERT INTO {ks}.schema_migrations "
                        "(version, name, checksum, applied_at, applied_by) "
                        "VALUES (?, ?, ?, ?, ?) IF NOT EXISTS",
                        Consistency.SERIAL,
                        (
                            migration.version,
                            migration.name,
                            migration.checksum,
                            datetime.now(UTC),
                            self._applied_by,
                        ),
                    )
                )
                done.append(migration.version)
            final = self.status()
            if not final.up_to_date:
                raise SchemaError(f"schema not up to date after migrate: {final.problems}")
            return tuple(done)
        finally:
            self._release_lock()

    def require_current(self) -> None:
        """For processes that use repositories: fail fast on a missing/outdated schema."""
        status = self.status()
        if not status.up_to_date:
            pending = [m.version for m in status.pending]
            raise SchemaError(
                f"keyspace {self._ks} not at schema v{len(self._migrations)} "
                f"(current v{status.current_version}, pending {pending}, "
                f"problems {list(status.problems)}); run `crawler2-storage migrate`"
            )
