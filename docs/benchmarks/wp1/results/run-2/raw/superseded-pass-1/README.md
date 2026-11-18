# Superseded first-pass data (kept as evidence, NOT used for Run 2 conclusions)

Produced earlier on 2026-11-18, then re-run because of provenance or contamination concerns found in the audit:

* `nvme/fio*`: the suite was assembled from three launches under different script versions (the CPU abort rule was changed
  mid-way, one job was aborted and repeated, one job completed while its parent shell was killed) and ran while lint/unit tests were
  being executed on the same host.
* `nvme/sustained-A*`, `sustained-B*`: run A had no SMART counters (background process had no sudo ticket); run B's post-idle
  burst was rate-capped and therefore uninformative. Their conclusions were reproduced by the re-runs.
* `qdrant/e-r1-*`, `qdrant/qdrant-latency-diag*`: lint/unit-test/doc scripts were run on the host while these latency-sensitive
  benchmarks were running.
* `gpu/gpu-probe*`: no CPU leg (workstream E2 needs a CPU/GPU comparison on identical inputs).

Nothing here was edited. The first-pass numbers are cited in the reports only where they are compared with the re-run.
