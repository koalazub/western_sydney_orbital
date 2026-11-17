*Team will build on Tom's submission as a single Python controller.py, using Polyfit velocity tracking plus a two-tier avoidance design: a conservative long-term planner and a reactionary emergency check.*



## Submission Base and Format

Tom's submission is the base; Kiran's C++ port raised questions about what counts as a legal submission

Organisers expect a single Python file (controller.py), not just code that runs

Tom Kirkpatrick's MVP scores 3s and spins due to a reorientation bug whose fix is not pushed yet



## Polyfit and Tracking

Polyfit is a line of best fit on noisy positions; the slope gives velocity, working well because noise is very low

Tracker uses global nearest neighbour association, gating on diameter and Euclidean distance

Path planning currently uses only each rock's latest velocity, not full predicted tracks

Kieran's graphs show Polyfit predictions within plus-minus 30 meters after about 4 to 5 seconds of tracking



## Avoidance Strategy

**Balance:** maneuvering too early wastes fuel, too late makes hard burns useless

During initial burn, just burn to coast speed with basic checks for rocks directly in the path

**Emergency check:** is a rock in our path within 150 meters, no tracker or prediction needed, O(n) cost

Kieran proposed **frustum culling:** a forward cone ignoring everything outside it

Tom Kirkpatrick suggested focusing on the front 180 degrees since debris moves tangentially

Idea to predict own corridor and check for rocks in it, rather than predicting every asteroid



## Webinar and Logistics

Webinar had little of consequence; organisers will check submissions for hard-coding

Nic Ralph leaves for a flight next Wednesday and may be hard to reach



## Next Steps

(Tom Kirkpatrick) Look into frustum culling for the emergency avoidance check

(Nic Ralph) Pull down Tom's code and work on the avoidance implementation

(Nic Ralph) Leave advice for the team before flying out next Wednesday



## Decisions Made

Avoidance uses two tiers: conservative long-term planner plus a higher-priority emergency proximity check

Submission stays a single Python controller.py file, matching the expected format
