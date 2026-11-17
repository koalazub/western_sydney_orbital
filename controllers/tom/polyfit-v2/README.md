# Tom Polyfit v2

`Controller.py` is a separate native STP experiment candidate copied from
`../polyfit/Controller.py`. It retains the 8 m/s cruise, polynomial-fit tracker,
avoidance candidates, governor and goal planner.

## Cone-filter correction

Radar row `item[0, :]` is already relative to the shuttle and expressed in
Phobos axes. The original cone filter subtracts the shuttle's absolute position
again. It also calculates `acos(dot / debris_norm * target_norm)` instead of
`acos(dot / (debris_norm * target_norm))`. This can put the argument outside
[-1, 1], produce NaN and reject a contact directly ahead.

V2 uses the relative radar vector directly, divides by both vector magnitudes,
clips the cosine to [-1, 1] for floating-point roundoff, and rejects degenerate
zero-length vectors. The existing forward-facing 45-degree cone is preserved.
This restores the intended filter; it does not establish collision safety.

## Shared logging correction

The least-bad avoidance fallback's `OnScreenLogMessage` call is commented out
in both this version and `../polyfit/Controller.py`. It omits the category
argument required by the installed SDK and can raise an exception before the
fallback velocity is returned. Commenting it out preserves the selected velocity
and control calculations, while allowing that branch to complete. Results from
an older version that crashed on this call are therefore not equivalent.
Other existing logging is retained.

## Experimental interpretation

Compare this candidate with the original Polyfit under matched native STP
conditions, with the fallback log disabled in both. Freeze a new campaign to
include this directory; existing prepared campaigns keep their frozen sources.
This change does not add a campaign entry or start a sweep.

Both versions now apply identical own-position history interpolation before
tracking, preserve individual timestamps, and skip duplicate, stale, future,
bootstrap-zero and unbracketed measurements. The history covers up to 10 s
(4096 samples maximum); usable measurement age remains 5 s.
`WSORB_RADAR_ORIGIN_OFFSET_S` specifies radar origin minus mission origin.
The default zero uses the owner-specified shared-origin assumption for this
experiment, consistent with Ali's history correction. No packet-arrival offset
is guessed; no independent native clock measurement is claimed.
The cone still excludes contacts outside its 45-degree field,
including possible crossing threats. Validate outcomes in licensed STP;
numeric checks alone do not establish flight performance.
