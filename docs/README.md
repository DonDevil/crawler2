# crawler2 documentation

Start with the [phase plan](v2-phase-plan.md) (authoritative: status, decisions log, open decisions).
Status words used everywhere: *implemented*, *tested locally*, *validated on a single host*, *validated with
synthetic fixtures*, *validated against a real service using synthetic records*, *validated with real input
data*, *simulated multi-host*, *physical multi-host validated*, *blocked*, *deferred*, *owner decision
required*, *not tested*. An unqualified "passed" is a bug in the docs.

| I want to… | Read |
|---|---|
| install and run it locally | [installation.md](installation.md), then [development.md](development.md) (test tiers, benchmarks, what CI runs) |
| understand a component | [architecture/](architecture/README.md) (operator guide per component) |
| know why something is built that way | [adr/](adr/README.md) |
| see what a phase did, tested and left open | `phases/pNN-*/` (`audit`, `design`, `decisions`, `validation`, `exit`) |
| deploy, supervise, upgrade, roll back | [phases/p14-production-hardening/deployment.md](phases/p14-production-hardening/deployment.md) |
| watch it: metrics, SLOs, alerts, tracing | […/observability.md](phases/p14-production-hardening/observability.md) |
| debug a stuck pipeline | […/runbook.md](phases/p14-production-hardening/runbook.md) |
| back up, restore, rebuild | […/recovery.md](phases/p14-production-hardening/recovery.md) |
| understand the security model and its limits | […/security.md](phases/p14-production-hardening/security.md) |
| run the 7-day soak | […/soak.md](phases/p14-production-hardening/soak.md) |
| see what is still open for production | [phases/p14-production-hardening/exit.md](phases/p14-production-hardening/exit.md) and `../to_complete.md` (audit register, 2026-10-22) |
| compare against V1 / see benchmark numbers | [benchmarks.md](benchmarks.md) (superseded rows are marked), [version2-comaprison.md](version2-comaprison.md) |

P10 (vector index and matcher) lives in the fingerprinter repository
(`../fingerprinter/docs/phases/p10-vector-index-matcher/`); its status is mirrored in the plan.
`gpt-disscussed-plan.md` is a historical discussion, not a specification.
