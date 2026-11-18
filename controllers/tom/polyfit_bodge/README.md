# Tom Polyfit experimental baseline

The original cone calculation is intentionally preserved for comparison with
`../polyfit-v2/`. Both versions have identical radar timing handling:

- Interpolate timestamped own-shuttle positions before reconstructing absolute
  debris positions; keep each measurement's timestamp.
- Rebase offsets before the existing cone filter and tracking code.
- Ignore repeated rows and reject invalid, stale, future, initial timestamp-zero
  or unbracketed readings. No current-position fallback or history extrapolation.
- Retain up to 10 s of own-state history (4096 samples maximum), accepting radar
  ages up to the existing 5 s track-age limit. Startup coverage builds naturally.

`WSORB_RADAR_ORIGIN_OFFSET_S` is radar origin minus mission origin, in seconds.
The owner specifies a shared origin for this experiment, so retain the default
zero offset. This is an experiment assumption, not a new runtime measurement.
The optional override is available if a different origin is established later.

The malformed least-bad fallback `OnScreenLogMessage` call is commented out in
both versions, allowing the selected fallback velocity to return. Other logging,
8 m/s cruise, governor, planner and avoidance candidates are unchanged.

These edits affect new source snapshots only. Earlier frozen sweeps retain their
original code and must not be relabelled as timing-corrected results. Numeric
verification does not establish licensed STP flight performance.
