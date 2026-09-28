| operation | target p99 | host-1 p50 / p99 | host-2 p50 / p99 | met |
|---|---|---|---|---|
| latest_read | 25 ms | 163.48 / 822.29 ms | 167.24 / 826.87 ms | NO |
| url_state_read | 25 ms | 157.8 / 861.2 ms | 165.98 / 811.76 ms | NO |
| observation_get | 25 ms | 174.79 / 865.09 ms | 175.81 / 918.18 ms | NO |
| representation_lookup | 25 ms | 201.27 / 803.4 ms | 205.97 / 770.06 ms | NO |
| dedupe_check | 25 ms | 194.08 / 876.48 ms | 190.73 / 911.27 ms | NO |
| dedupe_mark | 25 ms | 134.06 / 708.82 ms | 139.51 / 693.54 ms | NO |
| outbox_append | 25 ms | 170.3 / 720.42 ms | 172.41 / 714.51 ms | NO |
| content_lookup | 50 ms | 272.74 / 922.78 ms | 274.81 / 926.55 ms | NO |
| discovered_record | 50 ms | 258.51 / 830.51 ms | 265.04 / 867.93 ms | NO |
| attempt_record | 50 ms | 858.36 / 2683.5 ms | 853.1 / 2541.16 ms | NO |
| observation_record | 100 ms | 952.2 / 2657.79 ms | 946.4 / 2867.14 ms | NO |
| media_record | 100 ms | 935.0 / 2620.51 ms | 946.21 / 2614.07 ms | NO |
| links_record | 150 ms | 1256.73 / 3371.41 ms | 1235.36 / 3549.31 ms | NO |
| object_put | 250 ms | 516.1 / 2280.28 ms | 530.08 / 2242.86 ms | NO |
| object_head | 50 ms | 3.43 / 310.52 ms | 2.62 / 426.18 ms | NO |
| object_get | 100 ms | 2.25 / 221.85 ms | 2.08 / 299.23 ms | NO |

- host-1: 25.2/100.0 units/s (10x the expected 10/s), failed units 102, client CPU 0.34 cores, schedule lag p99 172410.3 ms, publication lag p50/p99 106.02/236.62 s
- host-2: 25.1/100.0 units/s (10x the expected 10/s), failed units 98, client CPU 0.34 cores, schedule lag p99 173914.62 ms, publication lag p50/p99 106.02/236.62 s
