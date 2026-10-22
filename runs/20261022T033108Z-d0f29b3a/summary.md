# Campaign `20261022T033108Z-d0f29b3a`

**Scenario:** Shackleton Lunar Search & Rescue (`lunar_sar`)  
**Generated:** 2026-10-22T03:31:08+00:00  
**Replicates:** 24  
**Master seed:** 42

Each replicate draws one terrain and one target, then runs every strategy on that
same draw. This is a paired comparison; differences come from the policy, not luck.

## Ranking

| Rank | Strategy | Success | Median report | P90 report | Coverage | Min SOC | Stranded | Energy | Comms |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | `sector-baseline` | 24/24 | 19m 52s | 49m 45s | 43.3% | 52.6% | 12.5% | 8.39 kWh | 73.0% |
| 2 | `sector-conservative` | 24/24 | 19m 52s | 1h 08m | 43.0% | 51.0% | 20.8% | 8.94 kWh | 71.3% |
| 3 | `sweep-baseline` | 24/24 | 23m 15s | 1h 33m | 47.1% | 47.9% | 16.7% | 9.77 kWh | 74.3% |
| 4 | `sweep-conservative` | 24/24 | 23m 15s | 1h 48m | 46.5% | 47.2% | 20.8% | 10.19 kWh | 73.4% |
| 5 | `sector-relay` | 24/24 | 23m 15s | 51m 00s | 44.9% | 47.5% | 16.7% | 9.22 kWh | 71.8% |
| 6 | `sweep-relay` | 23/24 | 23m 00s | 2h 09m | 46.9% | 46.0% | 25.0% | 11.03 kWh | 70.8% |
| 7 | `nearest-baseline` | 23/24 | 1h 29m | 2h 38m | 40.8% | 21.7% | 12.5% | 5.31 kWh | 55.2% |

Ranking is success rate first, then median time-to-report, then coverage.

## Strategies

| Strategy | Pattern | Reserve | Relay | Speed | Description |
| --- | --- | ---: | :---: | ---: | --- |
| `sector-baseline` | sector | 20% | no | 100% | Even sectors, 20% reserve, no relay. |
| `sweep-baseline` | sweep | 20% | no | 100% | Contiguous bands, 20% reserve, no relay. |
| `nearest-baseline` | nearest | 20% | no | 100% | Greedy nearest-workload assignment, no relay. |
| `sector-conservative` | sector | 35% | no | 100% | Even sectors, 35% reserve, no relay. |
| `sweep-conservative` | sweep | 35% | no | 100% | Contiguous bands, 35% reserve, no relay. |
| `sector-relay` | sector | 20% | yes | 100% | Even sectors, 20% reserve, redeploy ANT-1 to the best relay site. |
| `sweep-relay` | sweep | 20% | yes | 100% | Contiguous bands, 20% reserve, redeploy ANT-1. |

## Mission geometry

- Search box: x [-317, 1199], y [-718, 796] m
- Assets: 7; antennas: 3; charger bays: 2
- Horizon: 6h 00m; step: 15 s
- Search spacing factor: 1.50 × sensor radius

## Reproduce

```bash
uv run wsorb campaign run \
  --scenario lunar_sar --replicates 24 --seed 42
```

## Scope

This is a campaign-level surrogate, not Space Teams PRO itself. It models terrain,
slope, battery, charging queues, sensor coverage, line-of-sight comms and reporting
delay. It does not model rigid-body dynamics, wheel slip, thermal limits or the
proprietary STP asset models. Use it to reject weak strategies before spending time
in the high-fidelity simulator.

Command:

```bash
uv run --locked wsorb campaign run --scenario lunar_sar --replicates 24 --seed 42 --strategies all --out runs --notebook --author 'Simulation runner' --title 'Lunar SAR strategy comparison'
```
