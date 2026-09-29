# P6 Implementation — filter engine and discovery

Design: [design.md](design.md). Decisions and deviations: [decisions.md](decisions.md).

## 1. Modules

| Module | Contents | Pure |
|---|---|---|
| `crawler2/filtering/model.py` | `Classification`, `Action`, `Context`, `RuleKind`, `RuleSource`, `Party`, `Rule` (content-derived `rule_id`), `Policy`, `FilterInput`, `Decision`, precedence ranks | yes |
| `crawler2/filtering/inputs.py` | `host_of`, `registrable_domain` (PSL, LRU-cached), `link_input`, `request_input`, `redirect_inputs` | yes |
| `crawler2/filtering/patterns.py` | ABP pattern → regex, bounded index tokens, `host_only` | yes |
| `crawler2/filtering/engine.py` | `FilterEngine.compile/decide/candidates`, `ruleset_digest` | yes |
| `crawler2/filtering/abp.py` | EasyList/uBO network-filter parser + `ImportReport` (importer `p6-abp/v2`) | yes |
| `crawler2/filtering/v1import.py` + `data/v1_blacklist_review.toml` | V1 blacklist parser, reviewed manifest, `V1Report` | yes |
| `crawler2/filtering/builtin.py` | the `built_in` source (V1 ad networks, out-of-scope sites, ad host labels) | yes |
| `crawler2/filtering/store.py` | `store_source`, `publish`, `load_engine`, `RulesetHolder` (hot reload) | no |
| `crawler2/filtering/intercept.py` | `FilterInterceptor` (P4 `RequestInterceptor`) | reads the holder |
| `crawler2/filtering/cli.py` | `crawler2-filter` | no |
| `crawler2/discovery/admission.py` | `Admitter` (filter → revisit → `admit_many` → decision rows), `Scope`, `LinkAdmissionService` (`urls.discovered`) | no |
| `crawler2/discovery/seeds.py` | seed parser and loader | parser yes |
| `crawler2/discovery/search/adapters.py` | DuckDuckGo, Bing, Brave, Yandex, Ahmia, Torch | yes |
| `crawler2/discovery/search/runner.py` | query file, HTTP fetch, cooldown, provenance, admission | no |
| `crawler2/discovery/cli.py` | `crawler2-discover` | no |
| `crawler2/storage/scylla/cql/V003__filter_discovery.cql`, `storage/scylla/filtering.py` | F1–F11 tables and repositories | no |

P4 changes (additive, crawler2-internal): `crawlers/interception.py`,
`crawlers/model.py` (`RenderMetrics` aggregate), `crawlers/browser.py`
(observer counts decisions, page URL, main-frame flag),
`crawlers/recorder.py` (F8 summary), `crawlers/cli.py` (holder + interceptor
+ background reload). P5 change: `extraction/cli.py` stream client (X-5).
P2 change: `storage/events/consumer.py` `connect_stream_client`.

## 2. Processes

| Command | Role | Notes |
|---|---|---|
| `crawler2-discover admit` | consumer group `discovery-admission` on `urls.discovered` | any number of processes/hosts; metrics on `metrics.port` |
| `crawler2-discover seeds FILE --source S [--meta k=v] [--every 21600]` | seed provenance, scope roots, admission | re-admits unconditionally each run (revisit 0) |
| `crawler2-discover search --queries FILE [--every S]` | search adapters → F10 → admission | Torch only when `workers.tor_network.socks_proxy` is set |
| `crawler2-discover decisions URL` | F5/F6 for one URL | "why was this allowed/blocked" |
| `crawler2-filter import-builtin / import-v1 / import-abp / import-operator` | new source revision (`--dry-run` reports only) | refuses a > 50 % rule drop unless `--force` |
| `crawler2-filter publish --source name@rev|@latest …` | immutable ruleset | prints id, kinds, untokenized count |
| `crawler2-filter activate ID / rollback / status / explain URL` | pointer CAS; provenance; per-URL explanation with all candidates | |

Browser pools (`crawler2-worker --pool browser`) consult the active ruleset
when `filter.browser_interception` is true (default) and poll it every
`filter.reload_interval_s`; without storage (`--no-record`) they allow all.

## 3. Operator procedures

```bash
crawler2-storage migrate                                   # V003
crawler2-filter import-builtin
crawler2-filter import-v1 path/to/domain_blacklist.txt     # --dry-run first
crawler2-filter import-abp --source easylist --url https://easylist.to/easylist/easylist.txt
crawler2-filter import-abp --source easyprivacy --url https://easylist.to/easylist/easyprivacy.txt
crawler2-filter publish --source built_in@latest --source v1_blacklist@latest \
    --source easylist@latest --source easyprivacy@latest --note "…"
crawler2-filter activate rs-…                              # all processes switch within 15 s
crawler2-filter explain https://x.test/ad.js --context request --type script --source-url https://site.test/
crawler2-filter rollback                                   # previous ruleset
```

Protective/override rules are operator rules (TOML, `[[rule]]` with
`kind, pattern, classification, reason, note`, optional `action`,
`contexts`, `types`, `party`, `domains`), imported as a new `operator`
revision and published with the others. Refreshing EasyList = re-import +
publish + activate; a failed load keeps the previous engine everywhere.

## 4. Metrics (bounded labels)

`filter_decisions_total{context,classification,action,source}`,
`filter_reload_total{result}`, `filter_ruleset_state`, `filter_ruleset_rules`,
`discovery_admissions_total{origin,outcome}`,
`discovery_link_events_total{result}`, `search_queries_total{engine,result}`.
No URL, host, path or rule id is a label. Per-URL facts are in F5/F6,
per-observation facts in F7/F8.

## 5. Tests

| Tier | Where | Count |
|---|---|---|
| unit: model, precedence, overrides, provenance, determinism (hypothesis), untrusted input | `tests/unit/filtering/test_engine.py` | 23 |
| unit: ABP parser, pattern semantics, document semantics | `test_abp.py` | 26 |
| unit: V1 import (audit counts pinned), normalization, manifest | `test_v1import.py` | 12 |
| unit: storage, publish, verification, hot reload, rollback, concurrent swap | `test_store.py` | 10 |
| unit: P4 interceptor | `test_intercept.py` | 4 |
| unit: corpus guard (in-git rules) | `test_corpus.py` | 1 |
| unit: admission, scope, revisit, priorities, redirects | `tests/unit/discovery/test_admission.py` | 10 |
| unit: seeds, search adapters, runner, cooldown | `test_seeds_search.py` | 14 |
| unit: stream client regression | `tests/unit/storage/test_stream_client.py` | 1 |
| integration (Scylla): rule store + LWT + hot reload, discovery rows | `tests/integration/storage/test_filter_discovery.py` | 3 |
| integration (Redis+Scylla+MinIO): M1 fixture loop, two admission consumers | `test_m1_loop.py` | 1 |
| integration (Chromium): interception | `tests/integration/crawlers/test_interception.py` | 1 |

`scripts/test-filter-discovery.sh` runs the P6 tier from the host.
