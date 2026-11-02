# Controller entrypoints by person

Each folder is one person's entrypoint. Shared prediction, control math and
transport stay in `src/wsorb`; do not copy them into a person's folder. The
runtime profile registry is `src/wsorb/controllers/catalog.json`.

| Person | STP entrypoint | Local profile IDs | Implementation | Notes |
| --- | --- | --- | --- | --- |
| Ali | [`ali/official_profile.py`](ali/official_profile.py) | `ali_official_profile` (sweep; `wso_official_profile` alias) | `src/wsorb/controllers/official_profile.py`, `src/wsorb/flight/official_profile.py` | The 6/6 official-profile controller. Local-only; not yet STP-portable. |
| WSO | [`wso/stp_controller.py`](wso/stp_controller.py) | `baseline`, `body-axis` | `src/wsorb/stp_policy.py` | The portable checkpoint controller and STP handoff. |
| Bui | [`bui/Controller.py`](bui/Controller.py) | `bui_alpha_beta_mvp` | `bui/mvp/`, `src/wsorb/controllers/_bui.py` | The `mvp/` folder must stay beside `Controller.py`. |
| Tom | [`tom/Controller.py`](tom/Controller.py) | `tom_kalman_velocity_mvp` | `tom/`, `src/wsorb/controllers/_tom.py` | `tom/UserAPI.py` is the body +Z template, not the target-seeking controller. |
| Harry | none — shared local controller | `basic_pd_radar`, `coast` | `src/wsorb/flight/control.py`, `src/wsorb/controllers/_simple.py` | The local harness selects it by profile ID; see [`harry/README.md`](harry/README.md). |

## Switch between them

In the local game, open **Controller profiles** or press **Shift+C**. The
catalog presents the reviewed profile IDs above. Start the private runner once:

```sh
just controllers serve --connection-file .devenv/wsorb-local.json --run
```

Then choose a profile and **Run fresh test**. The runner does not auto-start
another profile; each Run replaces the current trial flight.

`ali_official_profile` is a matched-sweep policy, not a catalog profile. It is
launched by [`flight_sweep.py`](flight_sweep.py) and documented in
[`../docs/official-comparison.md`](../docs/official-comparison.md). The old
`wso_official_profile` key remains an alias so earlier configs still run.

## Keep the boundary clear

- `examples/<person>/` is a runnable or shareable entrypoint.
- `src/wsorb/` is shared reviewed implementation.
- `examples/flight_sweep.py` and the comparison JSON files are cross-team
  experiments, not anyone's controller.
- Only one writer may command the shuttle's jets. Do not run two entrypoints
  against the same flight.
