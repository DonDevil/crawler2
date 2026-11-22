| operation | target p99 | host-1 p50 / p99 | host-2 p50 / p99 | met |
|---|---|---|---|---|
| latest_read | 25 ms | 0.96 / 8.59 ms | 0.98 / 9.02 ms | yes |
| url_state_read | 25 ms | 0.64 / 7.18 ms | 0.67 / 7.08 ms | yes |
| observation_get | 25 ms | 0.93 / 5.14 ms | 0.9 / 6.98 ms | yes |
| representation_lookup | 25 ms | 0.65 / 7.09 ms | 0.63 / 9.63 ms | yes |
| dedupe_check | 25 ms | 0.54 / 6.19 ms | 0.54 / 7.66 ms | yes |
| dedupe_mark | 25 ms | 0.42 / 5.51 ms | 0.41 / 6.24 ms | yes |
| outbox_append | 25 ms | 0.61 / 5.37 ms | 0.61 / 7.93 ms | yes |
| content_lookup | 50 ms | 2.51 / 10.84 ms | 2.46 / 12.38 ms | yes |
| discovered_record | 50 ms | 1.58 / 10.95 ms | 1.55 / 10.59 ms | yes |
| attempt_record | 50 ms | 1.53 / 12.34 ms | 1.57 / 12.92 ms | yes |
| observation_record | 100 ms | 2.78 / 18.95 ms | 2.8 / 19.21 ms | yes |
| media_record | 100 ms | 2.26 / 16.18 ms | 2.2 / 17.1 ms | yes |
| links_record | 150 ms | 5.22 / 21.47 ms | 4.99 / 24.18 ms | yes |
| object_put | 250 ms | 2.89 / 10.6 ms | 2.91 / 10.71 ms | yes |
| object_head | 50 ms | 0.87 / 3.12 ms | 0.87 / 5.51 ms | yes |
| object_get | 100 ms | 0.89 / 5.82 ms | 0.91 / 2.8 ms | yes |

- host-1: 100.0/100.0 units/s (10x the expected 10/s), failed units 0, client CPU 1.56 cores, schedule lag p99 5.81 ms, publication lag p50/p99 0.92/2.3 s
- host-2: 100.0/100.0 units/s (10x the expected 10/s), failed units 0, client CPU 1.56 cores, schedule lag p99 5.3 ms, publication lag p50/p99 0.92/2.3 s
