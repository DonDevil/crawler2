| operation | target p99 | host-1 p50 / p99 | host-2 p50 / p99 | met |
|---|---|---|---|---|
| latest_read | 25 ms | 0.97 / 9.66 ms | 0.97 / 9.45 ms | yes |
| url_state_read | 25 ms | 0.71 / 8.25 ms | 0.71 / 8.11 ms | yes |
| observation_get | 25 ms | 0.92 / 10.69 ms | 0.93 / 9.46 ms | yes |
| representation_lookup | 25 ms | 0.68 / 8.55 ms | 0.68 / 9.22 ms | yes |
| dedupe_check | 25 ms | 0.58 / 7.54 ms | 0.57 / 7.51 ms | yes |
| dedupe_mark | 25 ms | 0.43 / 6.34 ms | 0.43 / 6.52 ms | yes |
| outbox_append | 25 ms | 0.62 / 7.05 ms | 0.64 / 6.44 ms | yes |
| content_lookup | 50 ms | 2.48 / 12.23 ms | 2.52 / 12.53 ms | yes |
| discovered_record | 50 ms | 1.58 / 11.53 ms | 1.57 / 11.36 ms | yes |
| attempt_record | 50 ms | 1.66 / 13.71 ms | 1.65 / 13.88 ms | yes |
| observation_record | 100 ms | 2.89 / 20.1 ms | 2.83 / 19.94 ms | yes |
| media_record | 100 ms | 2.26 / 17.47 ms | 2.24 / 16.36 ms | yes |
| links_record | 150 ms | 5.15 / 21.69 ms | 5.21 / 21.2 ms | yes |
| object_put | 250 ms | 3.81 / 12.34 ms | 3.84 / 12.31 ms | yes |
| object_head | 50 ms | 0.9 / 3.7 ms | 0.9 / 3.16 ms | yes |
| object_get | 100 ms | 0.93 / 8.5 ms | 0.93 / 5.09 ms | yes |

- host-1: 99.9/100.0 units/s (10x the expected 10/s), failed units 0, client CPU 1.57 cores, schedule lag p99 6.29 ms, publication lag p50/p99 0.96/2.54 s
- host-2: 100.0/100.0 units/s (10x the expected 10/s), failed units 0, client CPU 1.56 cores, schedule lag p99 5.28 ms, publication lag p50/p99 0.96/2.54 s
