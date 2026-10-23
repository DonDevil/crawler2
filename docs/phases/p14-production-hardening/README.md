# P14 — production hardening (groundwork sprint, 2026-10-22)

**Status: groundwork implemented and tested locally; P14 and milestone M6 are NOT complete; the system is not
production-ready.** The hardware, owner decisions and the real matcher output that M6 needs do not exist yet.
What exists is everything that could honestly be built and verified on the single development machine.

| Document | Content |
|---|---|
| [exit.md](exit.md) | acceptance checklist: each item *passed locally / failed / blocked / deferred / not run*; milestone status; ranked remaining work |
| [validation.md](validation.md) | exact commands, environments, results and limitations of everything run in the sprint |
| [decisions.md](decisions.md) | decisions made, and the open owner decisions with the task ids they block |
| [audit.md](audit.md), [design.md](design.md) | starting state; approach |
| [security.md](security.md) | SSRF/DNS-rebinding, secrets, least privilege, residual risks |
| [observability.md](observability.md) | metrics endpoints, SLO/SLI table, alerts, dashboards, correlation/tracing |
| [deployment.md](deployment.md) | supervision, configuration validation, install, upgrade, rollback |
| [recovery.md](recovery.md) | backup, restore, rebuild of derived state, component recovery |
| [runbook.md](runbook.md) | diagnosing a stuck pipeline (alert targets) |
| [soak.md](soak.md) | the 7-day soak specification, harness and operator guide |
| [register-status.md](register-status.md) | per-task status against `to_complete.md` (TC-xxx) |

Register: `../../../to_complete.md` (audit, unchanged; task ids TC-xxx). Code: `crawler2/core/netguard.py`,
`crawler2/soak/`, `crawler2/ops/`, `crawler2/diagnostics/{exporter,trace}.py`, `crawler2/crawlers/backoff.py`,
`deploy/`, `scripts/{soak,backup}/`, `docker-compose.secure.yml`.
