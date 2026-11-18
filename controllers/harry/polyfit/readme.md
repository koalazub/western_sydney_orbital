# Harry Polyfit: finite cone-angle fix

Copied on 18 November 2026 from
`controllers/tom/polyfit_v2_bodge/Controller.py`. Tom Kirkpatrick is the original
controller author; the source already includes Harry's disabled malformed
`Dodge info` alert and native tuning inputs. The exact copied source SHA-256 is
`a1d4fed643440c85b0957c28aad22811fdfb35ddf6ac7a1c4a1b1b55e006626b`.
The original file is preserved. This is a new controller revision, not a relabelled
historical result, and is distinct from `polyfit_bodge_v2`.

## Fix

The original forward-cone test called `acos` on an unbounded floating-point
cosine. A directly aligned radar contact can yield `1.0000000000000002` through
roundoff, giving NaN and silently excluding that contact. The copied controller
uses the pure `cone_angle` helper in both the normal and debug cone paths. It
clips finite cosine values to [-1, 1] before `arccos`, and returns infinity for
zero-length or non-finite vectors so degenerate contacts are rejected explicitly.
The existing 50 m core-bubble inclusion still has priority over the cone test.

The 30-degree forward cone, 50 m core bubble, 500 radar rows, tracker, avoidance
candidates, governor, planner, timing assumptions and existing logging fix remain
as copied. This does not add the historical cone/timing variant's own-state
interpolation or change radar clock handling. The native `WSORB_STP_TUNING`
boundary remains unchanged: cruise speed 8 m/s, margin 6 m and horizon 50 s are the
baseline, with tuning bounds [6,12], [3,12] and [15,60]. All other logic is frozen.

## Experiment

Bui's profile in `experiments/stp_optimiser/bui_polyfit.json` pins this copy's
actual SHA-256 after formatting. Baseline and every proposal use the same patched
revision. Eight matched training seeds, four additional validation seeds and six
held-out final-test seeds give 88 flights before evidence retries. Preparation
and focused numerical checks do not establish native flight performance. The
original upstream follow-up covered only 13 exception-selected seeds and is not
proof that this revision is the best controller across the seed bank.
