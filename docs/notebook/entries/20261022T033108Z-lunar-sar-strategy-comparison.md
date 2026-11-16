+++
id = "20261022T033108Z-lunar-sar-strategy-comparison"
title = "Lunar SAR strategy comparison"
created = "2026-10-22T03:31:08+00:00"
author = "Simulation runner"
kind = "campaign"
tags = ["campaign", "lunar_sar"]
+++

# Lunar SAR strategy comparison

Paired campaign over 24 terrain/target draws.

Best policy: `sector-baseline` — 24/24 successes, median report 19m 52s, coverage 43.3%.

| Rank | Strategy | Success | Median report | P90 report | Coverage | Min SOC | Stranded | Energy | Comms |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | `sector-baseline` | 24/24 | 19m 52s | 49m 45s | 43.3% | 52.6% | 12.5% | 8.39 kWh | 73.0% |
| 2 | `sector-conservative` | 24/24 | 19m 52s | 1h 08m | 43.0% | 51.0% | 20.8% | 8.94 kWh | 71.3% |
| 3 | `sweep-baseline` | 24/24 | 23m 15s | 1h 33m | 47.1% | 47.9% | 16.7% | 9.77 kWh | 74.3% |
| 4 | `sweep-conservative` | 24/24 | 23m 15s | 1h 48m | 46.5% | 47.2% | 20.8% | 10.19 kWh | 73.4% |
| 5 | `sector-relay` | 24/24 | 23m 15s | 51m 00s | 44.9% | 47.5% | 16.7% | 9.22 kWh | 71.8% |
| 6 | `sweep-relay` | 23/24 | 23m 00s | 2h 09m | 46.9% | 46.0% | 25.0% | 11.03 kWh | 70.8% |
| 7 | `nearest-baseline` | 23/24 | 1h 29m | 2h 38m | 40.8% | 21.7% | 12.5% | 5.31 kWh | 55.2% |

The generated campaign export was removed during repository cleanup on 3 November 2026. The results above remain as the historical record. The original run ID was `20261022T033108Z-d0f29b3a`.
