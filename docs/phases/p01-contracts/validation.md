# P1 Validation

Date: 2026-09-28. Base: P0 commit `7dc1913`. Host: Docker Engine 29.8.1,
Compose v5.5.1, Python 3.12.3 (`./env`), pydantic 2.13.5.

## Results

| Check | Command | Result |
|---|---|---|
| Lint + format | `make lint` (ruff check, ruff format --check) | clean |
| Types | `make typecheck` (mypy --strict + pydantic plugin, `init_forbid_extra`) | no issues, 59 files |
| Full test suite | `make check` | **157 passed**, 2 skipped (integration tests skip outside the stack by design) |
| Contract suite | `pytest tests/contract` | **125 passed** |
| Schema drift | `python -m antipiracy_contracts.schemas --check` | current (14 event schemas + catalog) |
| Requirements drift (CI rule) | `make requirements && git diff` | only the expected `antipiracy-contracts` provenance comment |
| Contract + integration tests **inside Docker** | `make up` (image rebuilt), then `docker compose exec -T app env RUN_INTEGRATION_TESTS=1 HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hyp python -m pytest -q -p no:cacheprovider tests/contract tests/integration` | **127 passed** (125 contract + 2 integration) |
| Stack, both profiles | `scripts/validate-stack.sh` | passed: redis/scylla/minio/app healthy; app-host-1/2 healthy; integration tests pass in `app`, `app-host-1`, `app-host-2`; host ids `host-1`/`host-2`; scratch isolation ok; read-only root fs ok |
| Static producer strictness | mypy on a probe `HttpValidators(etag="x", etagg="typo")` | rejected: `Unexpected keyword argument "etagg"` (probe not committed) |

In-container note: the root filesystem is read-only, so pytest's cache is
disabled and Hypothesis stores its database in the `/tmp` tmpfs. No
Docker or Compose change was needed.

## Exit criteria (plan P1 + phase brief)

| Criterion | Status | Where |
|---|---|---|
| Core IDs defined and tested | ✅ | `ids.py`, `urls.py`; `test_ids.py`, `test_urls.py` (derivations pinned) |
| Core domain contracts | ✅ | `models/*` |
| Entity / observation / attempt / event / representation / evidence explicit | ✅ | `ContractKind` on every model (tested); design §3 |
| Event envelope | ✅ | `events/envelope.py`; ADR-009 |
| Event payloads without transport | ✅ | 14 payloads; nothing references streams |
| Producer/consumer ownership documented and enforced | ✅ | `CATALOG`; events.md; `test_catalog.py`, `test_events.py` |
| Versioning/evolution rules | ✅ | ADR-009; design §6; forward fixtures |
| Fingerprinter boundary | ✅ | ADR-011; `events/fingerprinting.py`; `test_compat.py` |
| Contracts independent of crawler internals | ✅ | `test_boundaries.py` (stdlib + pydantic only) |
| Access-pattern catalog sufficient for P2 | ✅ | access-patterns.md (web, media, projections, evidence, fingerprinter, plumbing) |
| No Scylla tables/repositories | ✅ | none added |
| Compatibility fixtures (crawler- and fingerprinter-side) | ✅ | `compat/fixtures` (14 base, 4 forward, 2 model) + manifest |
| Serialization tests | ✅ | round trip + byte-level equality per fixture |
| JSON Schemas generated and tested | ✅ | `schema_json/`; `test_schemas.py` |
| P0 tests still pass | ✅ | 32 P0 tests within the 157 |
| `make check` passes | ✅ | above |
| Compose stack healthy | ✅ | above |
| V1 unchanged | ✅ | nothing written under `../crawler` (its working tree has pre-existing local changes from 2026-09-09, untouched) |
| No P2+ implementation | ✅ | no transport, storage, frontier, workers, extraction, media, encoder, evidence code |

## Known limitations (non-blocking)

- ADR-005 (legal/evidence requirements) remains open. The evidence
  contracts hold only provenance and sealing; P12 extends them additively.
- The fingerprinter repository has not adopted the package yet (planned
  for P9). Until then, compatibility is proven by this repository's
  fingerprinter-side fixture tests only.
- `page.changed` is intentionally deferred to P5 (additive).
