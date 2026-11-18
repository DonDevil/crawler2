# WP1 Run 2 — summary

Details: `run-2-report.md`. Scope: this host, single node, RF = 1. No gate, threshold or workload was changed.

* **NVMe:** healthy (0 media errors, spare 100 %, 9 % used, unsafe shutdowns unchanged). fio: seq read 2,666 / write 1,701 MiB/s, QD1 read 11.9 k / write 55.4 k IOPS. Sustained-write cliff after 8–14 GiB is **not thermal**; sustained reads throttle at TMT2 after ≈ 15 s.
* **CPU thermals:** firmware PL1/PL2 200/83 W vs 45 W design; package pins at 92–96 °C; concurrency is not a lever.
* **Qdrant E-R1:** fails on latency at 1×/2×/3× (≈ 10.8 / 25 / 28 ms vs 10 ms), OOM at 6×; memory linear and predictable; payload retrieval is the scale limiter. No gate-passing scale.
* **GPU:** 6.7× the CPU path, numerically identical, offline; thermal slowdown at 87 °C at larger batches; batch 2 is optimal.
* **P2 Scylla:** third pass at 10×. MinIO 512 MiB limit holds (anon 55 %).
* **Automation:** `wp1r2_hostguard.py` with 33 tests; UNKNOWN is never healthy.
* **Blockers:** H-02 closed for local single-node RF=1 scope (owner to confirm); H-01, H-05 partially closed; H-03/H-04/H-09 blocked; H-08 blocked on owner input; FP-05 open; H-06/H-07/H-10 not tested.
* **Needs the owner:** 81–84 °C test permission, PL1/power-profile A/B, env rebuild, E-R1 measurement-method review, payload-field product change, workload/retention model, target topology.
