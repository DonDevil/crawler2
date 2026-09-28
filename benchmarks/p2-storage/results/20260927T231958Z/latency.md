| operation | target p99 | host-1 p50 / p99 | host-2 p50 / p99 | met |
|---|---|---|---|---|
| latest_read | 25 ms | 0.93 / 68.03 ms | 0.89 / 148.2 ms | NO |
| url_state_read | 25 ms | 0.52 / 26.76 ms | 0.52 / 13.9 ms | NO |
| observation_get | 25 ms | 0.64 / 3.11 ms | 0.66 / 5.11 ms | yes |
| representation_lookup | 25 ms | 0.41 / 1.71 ms | 0.44 / 14.69 ms | yes |
| dedupe_check | 25 ms | 0.41 / 2.96 ms | 0.42 / 3.16 ms | yes |
| dedupe_mark | 25 ms | 0.29 / 1.6 ms | 0.28 / 1.57 ms | yes |
| outbox_append | 25 ms | 0.39 / 8.8 ms | 0.39 / 0.85 ms | yes |
| content_lookup | 50 ms | 1.65 / 61.47 ms | 1.63 / 81.3 ms | NO |
| discovered_record | 50 ms | 1.05 / 57.65 ms | 1.05 / 57.48 ms | NO |
| attempt_record | 50 ms | 1.23 / 58.73 ms | 1.21 / 27.16 ms | NO |
| observation_record | 100 ms | 2.22 / 175.17 ms | 2.2 / 231.73 ms | NO |
| media_record | 100 ms | 1.39 / 32.77 ms | 1.45 / 120.28 ms | NO |
| links_record | 150 ms | 3.34 / 118.83 ms | 3.34 / 146.95 ms | yes |
| object_put | 250 ms | 29.55 / 423.26 ms | 30.85 / 629.57 ms | NO |
| object_head | 50 ms | 0.77 / 4.85 ms | 0.77 / 3.49 ms | yes |
| object_get | 100 ms | 0.72 / 3.27 ms | 0.73 / 2.56 ms | yes |

- host-1: 10.1/10.0 units/s (10x the expected 10/s), failed units 0, client CPU 0.16 cores, schedule lag p99 0.67 ms, publication lag p50/p99 0.58/1.83 s
- host-2: 10.1/10.0 units/s (10x the expected 10/s), failed units 0, client CPU 0.16 cores, schedule lag p99 0.57 ms, publication lag p50/p99 0.58/1.83 s
