# WSO controller

This is the WSO entrypoint. There are two distinct paths:

## Licensed STP controller

[`stp_controller.py`](stp_controller.py) is the portable ControllerSystem
entrypoint. Send it with these five files from `src/wsorb` beside it:
`stp_api.py`, `stp_policy.py`, `control_contract.py`, `maneuvers.py` and
`tracking.py`.

Keep the server's existing ControllerSystem launch arguments unchanged. Start
with a read-only probe:

```json
{"schema":"wsorb.stp_policy","schema_version":1,"enabled":false}
```

Set it with `WSORB_STP_POLICY_JSON`, or use a file through
`WSORB_STP_POLICY_FILE`. The probe writes no commands. For an enabled run:

```json
{
  "schema": "wsorb.stp_policy",
  "schema_version": 1,
  "enabled": true,
  "radar_clock_offset_s": 0.0,
  "policy": "checkpoint_velocity",
  "checkpoint_radius_m": 50.0,
  "body_axis_preference": false,
  "max_speed_mps": 8.0,
  "duration_wall_s": 600.0,
  "tracking_diagnostics": true
}
```

`radar_clock_offset_s` must be independently established. `checkpoint_radius_m`
must match the server's Scoring `KeepOutRadius_m` (50 m in the official
reference). This is the portable checkpoint controller; it is not the
`wso_official_profile` policy that scored 6/6 in the local comparison. The
official-profile policy has not been ported to the STP SDK.

The implementation is [`src/wsorb/stp_policy.py`](../../src/wsorb/stp_policy.py)
and the transport is [`src/wsorb/stp_api.py`](../../src/wsorb/stp_api.py). See
[Send a controller to a colleague](../../docs/flight-lab.md#send-a-controller-to-a-colleague)
for the full handoff, clock and single-writer requirements.

## Local official-profile comparison

The local policy that produced the 6/6 official-profile result is
[`src/wsorb/flight/official_profile.py`](../../src/wsorb/flight/official_profile.py).
It is selected as `wso_official_profile` by
[`../flight_sweep.py`](../flight_sweep.py), not by the controller-profiles
catalog. The matched comparison is:

```sh
just flight-sweep examples/official_comparison.json runs/official-comparison-new
```

The local result is practice-model evidence, not an STP score. The local
checkpoint profiles `baseline` and `body-axis` still come from
`src/wsorb/stp_policy.py` and remain selectable in **Controller profiles**.
