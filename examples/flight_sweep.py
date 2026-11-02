"""A script or small notebook-cell baseline for Phobos experiments.

Run ``just build`` once, then ``just flight-sweep``. No browser or flight lab
is needed. The default sweep is 50 trials and may take time; start with fewer
seeds/counts or a shorter duration while developing your policy.

In your own notebook, the essential calls are:

    import json
    from pathlib import Path
    from runpy import run_path
    from wsorb.sweep import run_sweep, load_trial
    from wsorb.sweep_analysis import write_report

    experiment_dir = Path("examples")
    POLICIES = run_path(str(experiment_dir / "flight_sweep.py"))["POLICIES"]
    config = json.loads((experiment_dir / "flight_sweep.json").read_text())
    config["seeds"] = [11, 22]
    config["debris_counts"] = [0, 100, 500]
    summaries = run_sweep(config, POLICIES, "runs/my-first-sweep")
    report = write_report("runs/my-first-sweep")
    failed = load_trial("runs/my-first-sweep/trial-00000")
    # Pick any saved trial for a detailed, paired comparison:
    report = write_report("runs/my-first-sweep", trial="trial-00002")

Edit the functions below, or import your own algorithm. Factories reset memory
between trials. Observations contain public radar, not hidden rock positions.
The baseline is deliberately simple, not a promised successful flight policy.
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from time import monotonic

import numpy as np

from wsorb.controllers import PROFILE_IDS, policy_factory
from wsorb.flight import ResilientGuidance
from wsorb.flight.official_profile import official_basic_command
from wsorb.sweep import run_sweep
from wsorb.sweep_analysis import write_report


def basic_policy(settings):
    """Replace this with your controller; closures can hold per-trial memory."""

    return policy_factory("basic_pd_radar", settings)


def official_basic_policy(settings):
    """Published 10 m/s beeline with no debris avoidance; a comparison baseline."""
    if settings:
        raise ValueError("The official example baseline takes no settings")
    return official_basic_command


def official_profile_policy(settings):
    """WSO's official-profile beeline with fresh-radar avoidance."""
    if settings:
        raise ValueError("The official profile takes no settings")
    guidance = ResilientGuidance()

    def decide(observation):
        readiness = guidance.update(
            observation,
            wall_time_s=monotonic(),
            running=True,
        )
        if not readiness.can_command:
            return np.zeros(3), np.zeros(3)
        return guidance.command(readiness.observation, None, {})

    return decide


def coast_policy(settings):
    """Zero thrust gives a useful control trial; it does not stop momentum."""
    return policy_factory("coast", settings)


POLICIES = {profile: partial(policy_factory, profile) for profile in PROFILE_IDS}
POLICIES.update(
    basic_pd_radar=basic_policy,
    coast=coast_policy,
    official_basic_beeline=official_basic_policy,
    wso_official_profile=official_profile_policy,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_suffix(".json"))
    parser.add_argument("--output", default="", help="New output directory; never overwrites a run")
    parser.add_argument(
        "--analyze-only", action="store_true", help="Rebuild a saved sweep's HTML report"
    )
    args = parser.parse_args()
    if args.analyze_only:
        if not args.output:
            parser.error("--analyze-only requires --output pointing at a saved sweep")
        print(write_report(args.output))
        return
    config = json.loads(args.config.read_text())
    output = (
        Path(args.output)
        if args.output
        else Path("runs") / datetime.now(UTC).strftime("phobos-%Y%m%dT%H%M%S%fZ")
    )

    def progress(row):
        print(
            f"{row['algorithm']} seed={row['seed']} debris={row['debris_count']}: "
            f"{row['outcome']} at {row['elapsed_sim_s']}s",
            flush=True,
        )

    run_sweep(config, POLICIES, output, progress=progress)
    print(f"Report: {write_report(output)}")


if __name__ == "__main__":
    main()
