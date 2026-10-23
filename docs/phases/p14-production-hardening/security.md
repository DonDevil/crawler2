# Security model and residual risks

Scope: what the P14 groundwork protects, how that was tested, and what it does **not** protect.
Nothing here is a claim of production security: the topology it would protect does not exist yet.

## 1. Server-side request forgery (SSRF) and DNS rebinding

One implementation, `crawler2/core/netguard.py` (the P8 guard, generalised; `media/ssrf.py` re-exports it).

| Path | Protection | Strength |
|---|---|---|
| HTTP page fetch (`HttpFetcher`, default) | `GuardedTransport` → `PinnedTransport`: the TCP connect resolves the host, validates **every** answer and connects to a validated address. URL/`Host`/SNI/certificate name stay the hostname. Each redirect hop is a new request through the guard. | **Pinned** — DNS rebinding between check and connect is impossible |
| Media probe, media hash (P8) | Same classes (`media.probe.guarded`); the pre-check runs first for a fast typed error | Pinned by default; tests inject mock inner transports (pre-check only) |
| Tor / any proxied fetch | Name is resolved by the proxy, so locally: scheme, userinfo-free literal IPs in **every spelling** (`2130706433`, `0x7f.1`, `0177.0.0.1`, `127.1`, IPv6, IPv4-mapped), internal names (`localhost`, `*.local`, `*.internal`, metadata hosts) are refused on every hop | **Literal/name check only** — a public-looking name that the exit resolves to an internal address is *not* caught locally (Tor exits do not route to your LAN, but this is reliance, not control) |
| Browser (Chromium via Playwright) | Every request, **including redirects, sub-resources and WebSockets**, passes `_PageObserver.destination_ok` (`page.route` + `page.route_web_socket`): scheme allow-list (`data:`, `blob:`, `about:` stay in-process), resolution and the same address rules | **Check at request time, not a pin**: Chromium resolves again when it connects (residual TOCTOU, R-1). With a proxy configured only literals/internal names are checked (as Tor) |
| Fingerprinter acquirer (`fp:acquisition`) | `PinnedHTTPAdapter` mounted on the `requests` session the acquirer creates (resolve → validate → connect to the validated address; SNI/Host unchanged) | **Pinned** for the acquirer's own session; a caller-supplied `requests.Session` is untouched (the old check-then-resolve window remains for it) |
| Other outbound clients | `filtering/cli.py` (rule lists from an operator-configured URL, redirects followed), search adapters (fixed engines), health probes (configured hosts), `diagnostics` | **Not guarded** (R-2): operator-supplied destinations, not crawled content. Listed so nobody assumes they are |

Cost: the per-request pre-check resolves the name once more than the pinned connect does (one extra DNS lookup per
request; the pre-check gives a fast typed refusal and is kept as defence in depth).

Refused destinations: loopback, private (RFC 1918 and ULA), link-local (incl. `169.254.169.254` cloud
metadata), unspecified, multicast, reserved, `100.64.0.0/10`, IPv4-mapped IPv6 of any of these, any scheme
but http/https (`file:`, `gopher:`, `ftp:`…), unix sockets. **One unsafe record among several rejects the
host.** The result is `Outcome.UNSAFE_DESTINATION` (typed, final, recorded as a connection failure, never
retried) and the `UnsafeDestinationError` kind for the media path; both are counted
(`fetch_attempts_total{outcome="unsafe_destination"}`, `media_ssrf_rejections_total`) and alerted (`info`).

**Exemption list.** `ssrf_allowed_networks` (page fetch: `workers.fetch.*`; media: `media.*`) exists for
fixtures. With `environment=prod` the process **refuses to start** if either is non-empty
(`Settings._check_consistency`). Dev/test fixtures set `127.0.0.0/8`.

Tests: `tests/unit/crawlers/test_ssrf_pagefetch.py` (77: literal spellings, hostile redirects, DNS answer
that flips between check and connect against a real loopback server that must never see the request, mixed
answers, proxy path, prod refusal), `tests/unit/media/test_ssrf.py` (P8 suite, passes unchanged),
`tests/contract/crawlers/test_browser_ssrf.py` (real Chromium: loopback navigation refused, same page loads
when exempt), `fp:tests/test_acquisition_rebinding.py`.

Residual risks: **R-1** browser TOCTOU (a hostile DNS server can answer public to the route check and
internal to Chromium); mitigation is network-level (§4), not code. **R-2** unguarded operator-configured
clients. **R-3** Tor: exit-side resolution. **R-4** HTTP/2 is not enabled, so no connection coalescing
across hostnames; if it is ever enabled the pinned pool must be re-reviewed.

## 2. Secrets, authentication, least privilege

| Item | State | How verified |
|---|---|---|
| Production startup refuses missing/placeholder credentials (`environment=prod`: Redis password, Scylla user+password, MinIO service key not root-looking, secret ≥ 16 chars) | **Implemented** | `test_ssrf_pagefetch.py::test_production_refuses_*` |
| Redis ACL (`default` user off; `crawler2` app, `monitor` read-only, `operator` break-glass) | **Implemented; tested on a throwaway Redis**: app user cannot `FLUSHALL/KEYS/CONFIG/ACL/SHUTDOWN`, monitor cannot write, anonymous gets `NOAUTH`; the whole **frontier integration suite (53 tests, Lua scripts included)** and 37 storage/media/evidence/feedback integration tests pass as the `crawler2` user | `deploy/secure/redis.acl.template`; commands in [validation.md](validation.md) §3 |
| Redis username in the app (`redis.username`) | **Added** (was password-only) | same runs |
| MinIO least privilege (service user: get/put/list on named buckets; no delete, no bucket create, no admin) | **Implemented; tested against the dev MinIO**: put/get/list own bucket OK; list/put other bucket, delete, create bucket → 403 | `deploy/secure/provision-minio.sh` |
| Scylla roles (`PasswordAuthenticator`, app role with SELECT/MODIFY only) | **Configured, NOT tested here**: the host's AIO budget is exhausted by the running dev Scylla, so a second Scylla cannot start, and enabling auth on the shared dev node would disrupt it. The driver auth path (`PlainTextAuthProvider`) is existing code | `docker-compose.secure.yml` (flags), roles in §2.1 |
| Secret generation, never printed | `deploy/secure/provision-secrets.sh` (0600 files in a git-ignored `secrets/`; `--rotate`) | run during this sprint; `docker compose … config` valid; secured Redis container healthy |
| Rotation | Procedure in §2.2; **no rotation drill performed** | — |
| Repository secret scan | Regex scan of tracked files: nothing but AWS's published SigV4 example key in a unit test | one-off, not in CI |
| Dependency audit | `uv audit`: 2 advisories on `certifi 2026.7.22` → upgraded to 2026.10.19 (lock + exports regenerated); re-audit clean | `uv audit --frozen` |
| Container/image scanning | **Not done** (no scanner installed; policy undecided, OD-P14-7) | — |

### 2.1 Scylla roles (to run once, after enabling the authenticator)
```
cqlsh -u cassandra -p cassandra -e "ALTER ROLE cassandra WITH PASSWORD='<admin password>';
  CREATE ROLE crawler2_app WITH PASSWORD='<app password>' AND LOGIN=true;
  GRANT SELECT ON KEYSPACE crawler2 TO crawler2_app;  GRANT MODIFY ON KEYSPACE crawler2 TO crawler2_app;"
```
Migrations (`crawler2-storage migrate`) run as an administrative role, never as `crawler2_app`.
LWT needs only SELECT+MODIFY. The fingerprinter gets its own role on its own keyspace.

### 2.2 Rotation
1. `deploy/secure/provision-secrets.sh --rotate` (new files; old values are gone).
2. Redis: re-render `redis.acl`, `ACL LOAD` as `operator`; MinIO: `mc admin user add` with the new secret.
3. Roll the new `crawler2.secrets.env` to each host and restart roles one at a time (`systemctl restart crawler2-role@…`);
   consumers are idempotent, so a restart is safe.

## 3. Evidence artefact safety
* Reports are static HTML with every crawled value escaped and no script, remote resource, iframe or
  `url(http…)` (`tests/unit/evidence/test_package.py`: titles and campaign names, **plus (P14) a poisoned-every-field render and a
  "no remote content" test**). URLs in evidence are redacted of tokens/signatures (existing tests).
* Not built (ADR-005 open, no policy invented): object lock/WORM, retention, personal-data redaction,
  RFC 3161 timestamps/signatures. The evidence bucket has **no** access control beyond §2's service user.

## 4. Network isolation and egress
* `deploy/systemd/crawler2-role@worker-browser.service.d/isolation.conf.example` (and `…worker-tor…`):
  `IPAddressDeny` for RFC 1918/ULA/link-local/loopback with `IPAddressAllow` only for Redis/Scylla/MinIO —
  which closes R-1 for browser/Tor hosts *when applied to system units as root*. **Not exercised here**
  (needs root and real addresses). The compose stack has one flat network; `docker-compose.secure.yml`
  only removes host port publishing.
* No Tor service is defined anywhere in the repository (`tor.py` resolves a proxy only). Tor egress policy is an
  operator decision (OD-P14-8).

## 5. TLS and inter-service authentication
**Absent.** Redis, Scylla, MinIO client traffic and the crawler2↔fingerprinter contract (shared Redis
streams) are plaintext inside the trust boundary. Required for a multi-host deployment across an untrusted
network; not designed or built in this sprint (OD-P14-9). `/metrics` is unauthenticated (§ observability).
