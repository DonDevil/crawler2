# P11 decisions

Implementation decisions were made in-session (2026-10-22) under the
operator's instruction to proceed without design approval. Each is
reversible by configuration or a small code change.

## 1. Implementation decisions

| Id | Decision | Why |
|---|---|---|
| P11-D1 | No contract change; attribute matches through crawler2's registry (M5 → M3 → W4) | `MatchResult` is keyed by content by design; crawler2 owns the URL mapping. Avoids touching the shared package and the fingerprinter |
| P11-D2 | The hook is an optional `prioritizer` on the P6 `Admitter`, not a scheduler | P7 WHEN (the planned scheduler) is deferred; admission is the only real priority path today and feeds the real P3 claim order |
| P11-D3 | Off by default (`CRAWLER2_FEEDBACK__ENABLED=false`) | B.5 #1 (intelligence optional); keeps today's behaviour until the operator enables it |
| P11-D4 | Boost only, never a penalty; total ≤ 20 on the 0..100 scale | Cold sources keep their crawl opportunity (exploration by construction); a boosted link (≤ 70) never outranks a seed (70) by more than ties; failure handling stays with P7 |
| P11-D5 | Value = Σ distinct targets of max decayed strength, saturated (`1 − e^{−V/2}`) | Duplicates of one target cannot inflate value; several targets matter more than many copies; bounded |
| P11-D6 | Exponential decay τ = 14 d, cut-off 90 d (= B1 TTL) | Same factor for every term ⇒ no rank reversal without evidence; 14 d keeps a weekly-active source boosted, forgets a dead one within ~2 months |
| P11-D7 | Relevance weights: active 1.0, unknown 0.5, retired 0.1 | Events may arrive out of order (unknown); retired targets' evidence still says something about the source, weakly |
| P11-D8 | `probable` verdicts weigh 0.5 | P10 calibration is not validated (M3 not passed); a confirmed match is stronger evidence |
| P11-D9 | Pattern boost needs ≥ 2 distinct carrying pages per template, max 5 | Separates a productive template from a one-off page |
| P11-D10 | Browser preference only with ≥ 1.0 browser evidence and < 0.25× http evidence; 20 % http re-test share; P7 informed advice wins | No lock-in after one success; cheap capability preferred when proven; avoids fighting P7's own re-test |
| P11-D11 | Evidence rows keyed (host, match, page observation), plain upserts, no LWT | Values are deterministic per key ⇒ replays/concurrency write identical cells |
| P11-D12 | Malformed events acked and counted; storage errors leave entries pending | Same semantics as the P8 representation loop; a poison event cannot stall the group |
| P11-D13 | The consumer also populates P2 targets and P3–P5 match projections | They existed but had no runtime writer; P3/P4 make evidence rebuildable |
| P11-D14 | No effort denominator (matches per fetch) in v1 | P7 profiles hold decayed weights, not counts over the same window; noted as follow-up |

## 2. Operator decisions still open

| Id | Question | Default until decided |
|---|---|---|
| OD-1 | Enable feedback in a deployed admission process (`CRAWLER2_FEEDBACK__ENABLED=true`) after running `crawler2-storage migrate` (V006) and `crawler2-intel feedback run` | Disabled |
| OD-2 | Accept M4 as demonstrated with the matcher and fetch simulated (exit.md), or require a real-stack run once P10 M3 passes | M4 NOT PASSED (exit.md) |
| OD-3 | Whether boosts may exceed the seed priority band (raise `max_total_boost` > 20) | 20 |
