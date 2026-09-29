# TITAN FLEET benchmark: SLO-Budget Regret

_Simulated fleet, synthetic workload, illustrative parameters. Every number below was produced by the command in the provenance section._

- 25 scenarios x 3 seeds (seeds [11, 12, 13]), one simulated hour each, ~3 requests/s across three SLO classes.
- Objective J = cost + sum over classes of penalty x violations beyond the error budget. SBR = J(policy) - J(oracle). The oracle is a fluid LP with full hindsight (see `titan_fleet/oracle.py`); it is optimistic, not achievable.
- Policies with negative SBR anywhere (would indicate an oracle modelling gap): none.

| SLO class | Metric | Target | Objective | Penalty per violation over budget |
|---|---|---|---|---|
| interactive | ttft | 1.2 s | 99% | $0.5 |
| batch | e2e | 60.0 s | 95% | $0.1 |
| background | e2e | 600.0 s | 90% | $0.02 |

## Summary over all scenarios

| Policy | Total SBR ($) | Median SBR / J* | Total cost ($) | Total penalty ($) | Best in |
|---|---|---|---|---|---|
| Round robin + HPA | 10810.53 | 2754.0% | 1565.68 | 9707.39 | 0/25 |
| Cheapest endpoint + HPA | 11186.92 | 1951.6% | 718.73 | 10930.73 | 0/25 |
| Latency-only + HPA | 3771.79 | 544.3% | 1243.7 | 2990.64 | 0/25 |
| Per-provider autoscaling (static failover + HPA) | 10727.4 | 1500.3% | 780.24 | 10409.71 | 0/25 |
| Tiered static routing + HPA (extra baseline) | 3320.88 | 451.5% | 1191.49 | 2591.94 | 2/25 |
| **S3 controller (mechanism)** | 1314.65 | 144.1% | 734.11 | 1043.08 | 3/25 |
| S3 without error-budget term (ablation) | 1316.92 | 144.8% | 734.84 | 1044.62 | 2/25 |
| S3 without trend look-ahead (ablation) | 1415.49 | 142.6% | 708.14 | 1169.88 | 16/25 |
| S3 without proactive scaling (ablation) | 1391.82 | 160.4% | 765.53 | 1088.83 | 2/25 |

S3 has lower SBR than: round_robin in 25/25, cheapest in 25/25, latency_only in 23/25, per_provider_autoscale in 25/25, tiered_static in 23/25, s3_no_budget in 18/25, s3_no_trend in 8/25, s3_reactive in 21/25.

## Negative cases: S3 worse than the best baseline

| Scenario | What happens | Best baseline | Baseline SBR | S3 SBR |
|---|---|---|---|---|
| outage_selfhost | self-hosted pool down for 10 minutes | tiered_static | 59.5168 | 113.3795 |
| gpu_price_high | GPU-hour price x5 | tiered_static | 29.5829 | 55.6513 |

## SBR per scenario ($, mean over seeds)

| Scenario | J* (oracle) | round_robin | cheapest | latency_only | per_provider_autoscale | tiered_static | s3 | s3_no_budget | s3_no_trend | s3_reactive |
|---|---|---|---|---|---|---|---|---|---|---|
| steady | 8.8554 | 237.2973 | 172.8219 | 46.7788 | 161.2061 | 31.1398 | 13.5036 | 13.5168 | **12.8836** | 14.2005 |
| diurnal | 8.8894 | 218.501 | 113.2704 | 39.6941 | 112.8224 | 29.5499 | 11.2079 | 11.232 | **10.4721** | 12.0281 |
| burst_3x_5min | 10.742 | 567.9681 | 1154.2233 | 267.5556 | 1149.6615 | 126.1827 | 38.2137 | **35.5406** | 41.671 | 38.2451 |
| burst_interactive_4x | 9.7059 | 564.9423 | 863.4089 | 213.8773 | 858.7108 | 110.4131 | 15.4249 | 15.4589 | **14.5428** | 15.7594 |
| batch_flood | 11.8196 | 314.7117 | 368.1667 | 39.3638 | 362.5027 | 61.2868 | 17.034 | 17.1166 | **16.8509** | 17.8408 |
| flash_crowd_8x_90s | 10.9707 | 703.6741 | 890.5717 | 484.408 | 879.3941 | 367.4856 | 299.5466 | 302.5291 | 299.7092 | **296.8888** |
| slow_ramp | 18.18 | 498.852 | 123.1396 | 39.1421 | 118.8054 | 95.7036 | 16.958 | 16.9538 | **15.9475** | 18.0057 |
| square_wave | 15.5011 | 1034.5255 | 1819.4743 | 119.5935 | 1807.5646 | 56.2052 | 31.9052 | 31.9663 | **23.268** | 31.5079 |
| outage_apiA_east | 9.0278 | 287.5742 | 122.8588 | 50.1381 | 117.4109 | 78.4379 | 12.5192 | 12.5247 | **11.775** | 13.7654 |
| outage_region_east | 177.0505 | 625.1632 | 473.9469 | 487.8134 | 821.6498 | 453.9065 | **393.1573** | 394.0044 | 396.2883 | 404.3297 |
| outage_region_west | 8.9653 | 250.0296 | 135.7487 | 40.4176 | 130.7636 | 31.1597 | 12.2749 | 12.3038 | **11.3733** | 13.1338 |
| outage_selfhost | 13.1816 | 363.0268 | 545.803 | 78.3833 | 478.5908 | **59.5168** | 113.3795 | 108.7218 | 115.9815 | 120.9223 |
| quota_cut_apiA | 8.9374 | 233.5902 | 133.7852 | 34.4778 | 128.4393 | 39.7863 | 11.836 | 11.9461 | **11.3588** | 13.0013 |
| quota_exhaustion | 10.7763 | 509.5353 | 677.8828 | 40.456 | 672.0695 | 263.8936 | **17.0538** | 17.4203 | 34.366 | 19.6757 |
| price_drop_apiB | 8.849 | 213.9017 | 300.597 | 44.5362 | 132.7585 | 29.7556 | 11.8715 | 11.9569 | **11.4283** | 13.3614 |
| price_spike_apiA | 8.9554 | 283.592 | 135.8276 | 50.3641 | 131.2513 | 61.0878 | 12.1585 | 12.1834 | **11.5056** | 13.2789 |
| cold_start_storm | 16.2439 | 1376.5778 | 635.3159 | 1083.4582 | 572.5113 | 253.0823 | **80.5993** | 88.7733 | 153.3838 | 125.0972 |
| gpu_price_high | 40.0633 | 217.0651 | 581.2248 | 53.0425 | 185.2286 | **29.5829** | 55.6513 | 55.5841 | 55.6513 | 65.0212 |
| low_load | 2.5087 | 61.6606 | 15.0024 | 21.9196 | 15.0024 | 8.425 | 4.5121 | 4.5231 | **3.9086** | 4.0766 |
| brownout_apiA_east | 8.9791 | 432.6747 | 137.7067 | 77.5012 | 133.2305 | 800.3373 | 12.7845 | 12.7317 | **12.0247** | 13.7424 |
| selfhost_degraded | 11.4902 | 243.5469 | 352.7236 | 62.545 | 347.2258 | 33.946 | 19.6293 | **17.8504** | 28.4127 | 26.2886 |
| interactive_only | 5.6449 | 364.9246 | 277.4676 | 37.0934 | 277.4676 | 41.9157 | 7.5929 | 7.5995 | **6.8894** | 7.6648 |
| background_heavy | 12.0965 | 139.6061 | 59.5139 | 53.013 | 57.1966 | 23.8836 | 16.5784 | 16.6008 | **15.3563** | 17.3022 |
| burst_during_outage | 10.2814 | 666.2189 | 907.7002 | 272.863 | 902.4614 | 188.0089 | 71.2379 | 69.8593 | 83.8055 | **57.5836** |
| high_load | 14.826 | 401.3683 | 188.74 | 33.3538 | 173.4783 | 46.1892 | 18.0162 | 18.0197 | **16.6314** | 19.1002 |

## SLO attainment and cost per scenario (S3 vs the best baseline)

| Scenario | Policy | Cost ($) | Penalty ($) | Interactive | Batch | Background | p99 TTFT (s) | 429s | 5xx | Replica-h |
|---|---|---|---|---|---|---|---|---|---|---|
| steady | s3 | 22.3589 | 0.0 | 99.89% | 99.83% | 100.00% | 0.7957 | 0.0 | 0.0 | 8.0143 |
| steady | tiered_static | 39.5818 | 0.4133 | 99.33% | 95.20% | 100.00% | 1.1373 | 0.0 | 0.0 | 7.0897 |
| diurnal | s3 | 20.0973 | 0.0 | 99.95% | 99.83% | 100.00% | 0.643 | 0.0 | 0.0 | 7.6753 |
| diurnal | tiered_static | 38.4393 | 0.0 | 99.40% | 98.38% | 100.00% | 1.122 | 0.0 | 0.0 | 6.7513 |
| burst_3x_5min | s3 | 30.4557 | 18.5 | 98.43% | 98.09% | 100.00% | 1.3357 | 0.0 | 0.0 | 9.139 |
| burst_3x_5min | tiered_static | 47.678 | 89.2467 | 98.05% | 80.23% | 100.00% | 1.3793 | 0.0 | 0.0 | 9.0233 |
| burst_interactive_4x | s3 | 25.1308 | 0.0 | 99.52% | 99.83% | 100.00% | 1.164 | 0.0 | 0.0 | 9.022 |
| burst_interactive_4x | tiered_static | 44.5524 | 75.5667 | 96.86% | 96.34% | 100.00% | 1.6147 | 0.0 | 0.0 | 7.374 |
| batch_flood | s3 | 28.8536 | 0.0 | 99.85% | 99.84% | 100.00% | 0.8503 | 0.0 | 0.0 | 10.3057 |
| batch_flood | latency_only | 43.0633 | 8.12 | 98.94% | 97.81% | 99.66% | 1.3443 | 154.3333 | 0.0 | 8.673 |
| flash_crowd_8x_90s | s3 | 29.1473 | 281.37 | 91.77% | 84.07% | 92.69% | inf | 2175.0 | 0.0 | 9.1217 |
| flash_crowd_8x_90s | tiered_static | 45.3316 | 333.1247 | 91.13% | 76.33% | 90.42% | inf | 2798.0 | 0.0 | 8.7383 |
| slow_ramp | s3 | 35.138 | 0.0 | 99.95% | 99.93% | 100.00% | 0.6423 | 0.0 | 0.0 | 13.367 |
| slow_ramp | latency_only | 57.3221 | 0.0 | 99.20% | 99.99% | 100.00% | 1.136 | 0.0 | 0.0 | 10.755 |
| square_wave | s3 | 47.4063 | 0.0 | 99.39% | 99.88% | 100.00% | 1.175 | 0.0 | 0.0 | 15.308 |
| square_wave | tiered_static | 71.5563 | 0.15 | 99.11% | 96.28% | 100.00% | 1.185 | 0.0 | 0.0 | 13.463 |
| outage_apiA_east | s3 | 21.547 | 0.0 | 99.93% | 99.86% | 100.00% | 0.7177 | 0.0 | 0.0 | 7.9847 |
| outage_apiA_east | latency_only | 38.3325 | 20.8333 | 98.43% | 99.99% | 100.00% | 1.3297 | 0.0 | 21.6667 | 5.8877 |
| outage_region_east | s3 | 25.4632 | 544.7447 | 80.93% | 79.69% | 79.93% | inf | 257.3333 | 5293.3333 | 7.897 |
| outage_region_east | tiered_static | 41.899 | 589.058 | 80.38% | 70.67% | 79.98% | inf | 263.3333 | 4596.6667 | 8.1117 |
| outage_region_west | s3 | 21.2402 | 0.0 | 99.93% | 99.88% | 100.00% | 0.7327 | 0.0 | 0.0 | 7.8807 |
| outage_region_west | tiered_static | 40.125 | 0.0 | 99.37% | 96.38% | 100.00% | 1.1363 | 0.0 | 0.0 | 7.4023 |
| outage_selfhost | s3 | 33.0777 | 93.4833 | 95.54% | 98.90% | 95.62% | 1.7617 | 361.6667 | 507.0 | 7.7173 |
| outage_selfhost | tiered_static | 49.8617 | 22.8367 | 98.77% | 89.95% | 95.34% | 1.262 | 530.3333 | 570.0 | 8.0363 |
| quota_cut_apiA | s3 | 20.7734 | 0.0 | 99.90% | 99.86% | 100.00% | 0.7543 | 0.0 | 0.0 | 7.6923 |
| quota_cut_apiA | latency_only | 37.1152 | 6.3 | 99.05% | 99.80% | 100.00% | 1.1927 | 0.0 | 0.0 | 5.7873 |
| quota_exhaustion | s3 | 26.7801 | 1.05 | 99.23% | 99.82% | 100.00% | 1.1673 | 0.0 | 0.0 | 9.3627 |
| quota_exhaustion | latency_only | 29.4656 | 21.7667 | 98.43% | 99.94% | 100.00% | 1.79 | 0.0 | 0.0 | 8.4513 |
| price_drop_apiB | s3 | 20.7205 | 0.0 | 99.95% | 99.84% | 100.00% | 0.751 | 0.0 | 0.0 | 7.6897 |
| price_drop_apiB | tiered_static | 38.6046 | 0.0 | 99.39% | 96.67% | 100.00% | 1.132 | 0.0 | 0.0 | 6.5393 |
| price_spike_apiA | s3 | 21.1138 | 0.0 | 99.91% | 99.84% | 100.00% | 0.7363 | 0.0 | 0.0 | 7.845 |
| price_spike_apiA | latency_only | 59.1694 | 0.15 | 99.23% | 99.97% | 100.00% | 1.1213 | 0.0 | 0.0 | 5.2043 |
| cold_start_storm | s3 | 50.5265 | 46.3167 | 98.06% | 99.52% | 100.00% | 1.3743 | 0.0 | 0.0 | 12.9903 |
| cold_start_storm | tiered_static | 68.2528 | 201.0733 | 95.08% | 93.52% | 100.00% | 1.8017 | 0.0 | 0.0 | 12.4053 |
| gpu_price_high | s3 | 95.7146 | 0.0 | 99.95% | 99.87% | 100.00% | 0.7203 | 0.0 | 0.0 | 7.531 |
| gpu_price_high | tiered_static | 69.6462 | 0.0 | 99.37% | 99.99% | 100.00% | 1.1263 | 0.0 | 0.0 | 2.465 |
| low_load | s3 | 7.0209 | 0.0 | 99.95% | 99.96% | 100.00% | 0.6183 | 0.0 | 0.0 | 2.786 |
| low_load | tiered_static | 10.9337 | 0.0 | 99.38% | 99.96% | 100.00% | 1.1217 | 0.0 | 0.0 | 2.063 |
| brownout_apiA_east | s3 | 21.7636 | 0.0 | 99.95% | 99.87% | 100.00% | 0.7187 | 0.0 | 0.0 | 8.0717 |
| brownout_apiA_east | latency_only | 41.2136 | 45.2667 | 97.55% | 99.81% | 100.00% | 1.5447 | 0.0 | 0.0 | 5.1893 |
| selfhost_degraded | s3 | 27.7362 | 3.3833 | 98.87% | 99.17% | 100.00% | 1.23 | 0.0 | 0.0 | 10.3017 |
| selfhost_degraded | tiered_static | 45.4362 | 0.0 | 99.39% | 95.22% | 100.00% | 1.1233 | 0.0 | 0.0 | 9.7817 |
| interactive_only | s3 | 13.2378 | 0.0 | 99.94% | - | - | 0.9157 | 0.0 | 0.0 | 4.9297 |
| interactive_only | latency_only | 38.505 | 4.2333 | 99.09% | - | - | 1.1497 | 0.0 | 0.0 | 2.2657 |
| background_heavy | s3 | 28.6749 | 0.0 | 99.91% | 99.86% | 100.00% | 0.7667 | 0.0 | 0.0 | 10.9117 |
| background_heavy | tiered_static | 35.9802 | 0.0 | 99.55% | 96.60% | 100.00% | 1.114 | 0.0 | 0.0 | 10.6813 |
| burst_during_outage | s3 | 27.286 | 54.2333 | 97.27% | 99.89% | 100.00% | 1.606 | 0.0 | 8.6667 | 9.017 |
| burst_during_outage | tiered_static | 44.607 | 153.6833 | 94.46% | 92.86% | 100.00% | 1.889 | 0.0 | 29.0 | 8.8303 |
| high_load | s3 | 32.8422 | 0.0 | 99.97% | 99.85% | 100.00% | 0.7077 | 0.0 | 0.0 | 11.9567 |
| high_load | latency_only | 48.1797 | 0.0 | 99.40% | 99.97% | 100.00% | 1.0977 | 0.0 | 0.0 | 9.7 |

## Provenance

- commit: `48221721278e3b7a0384e4e37e6da6541cde890d`
- command: `python -m titan_fleet bench --out reports`
- python: `3.10.6`
- platform: `Windows-10-10.0.26200-SP0`
- scipy: `1.15.3`
- scenarios_sha256: `38f4a9e481d687f6`
- workload: `synthetic (seeded non-homogeneous Poisson)`
- generated_at: `2026-09-29T09:26:08+00:00`
- seed: `11`
- seeds: `3`
