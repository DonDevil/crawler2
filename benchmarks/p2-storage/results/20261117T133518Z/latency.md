| operation | target p99 | host-1 p50 / p99 | host-2 p50 / p99 | met |
|---|---|---|---|---|
| latest_read | 25 ms | 0.97 / 8.72 ms | 0.96 / 9.02 ms | yes |
| url_state_read | 25 ms | 0.72 / 8.05 ms | 0.71 / 8.0 ms | yes |
| observation_get | 25 ms | 0.94 / 9.18 ms | 0.96 / 11.9 ms | yes |
| representation_lookup | 25 ms | 0.65 / 7.63 ms | 0.67 / 9.05 ms | yes |
| dedupe_check | 25 ms | 0.56 / 7.32 ms | 0.59 / 8.33 ms | yes |
| dedupe_mark | 25 ms | 0.41 / 5.81 ms | 0.44 / 6.24 ms | yes |
| outbox_append | 25 ms | 0.61 / 9.02 ms | 0.65 / 5.75 ms | yes |
| content_lookup | 50 ms | 2.44 / 11.51 ms | 2.52 / 10.62 ms | yes |
| discovered_record | 50 ms | 1.54 / 10.66 ms | 1.64 / 10.44 ms | yes |
| attempt_record | 50 ms | 1.63 / 14.52 ms | 1.68 / 14.57 ms | yes |
| observation_record | 100 ms | 2.83 / 19.91 ms | 2.88 / 18.58 ms | yes |
| media_record | 100 ms | 2.13 / 18.31 ms | 2.27 / 15.5 ms | yes |
| links_record | 150 ms | 5.14 / 22.18 ms | 5.37 / 20.68 ms | yes |
| object_put | 250 ms | 4.04 / 13.46 ms | 4.21 / 14.36 ms | yes |
| object_head | 50 ms | 0.97 / 5.74 ms | 0.97 / 4.23 ms | yes |
| object_get | 100 ms | 1.0 / 6.63 ms | 1.05 / 8.49 ms | yes |

- host-1: 99.9/100.0 units/s (10x the expected 10/s), failed units 0, client CPU 1.54 cores, schedule lag p99 5.67 ms, publication lag p50/p99 0.96/2.54 s
- host-2: 100.0/100.0 units/s (10x the expected 10/s), failed units 0, client CPU 1.58 cores, schedule lag p99 5.41 ms, publication lag p50/p99 0.96/2.54 s
