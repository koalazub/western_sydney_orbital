# Western Sydney Orbital

Controller development and flight practice for **Escape from Phobos**, the Fall 2026 Space Teams PRO challenge. The mission is to fly a shuttle from Phobos to the return ship through impact debris, balancing time, fuel and damage.

This repository contains the team's Python controllers, a local practice simulator and recorded flight comparisons. The local simulator is not an exact reproduction of Space Teams PRO. Validate a candidate controller in the licensed competition runtime before submitting it.

## Get started

- Follow the [flight lab setup](docs/flight-lab.md#start-locally), then [fly manually](docs/flight-lab.md#fly-manually) or [run a controller](docs/flight-lab.md#run-a-controller).
- Find each person's entrypoint and shared implementation in [the controller index](examples/README.md).
- Inspect recorded flights in [Compare flights](https://western-sydney-orbital-api.western-sydney-orbital-worker.workers.dev/compare.html). It needs neither Python nor WebGPU.
- Check the [competition reference and local differences](docs/competition-reference.md) and [scoring notes](docs/scoring.md) before interpreting results.

Use the repository's pinned Nix/devenv environment and [Just recipes](justfile). Setup, controls, troubleshooting and SDK handoff instructions live in the [flight lab guide](docs/flight-lab.md).

## Competition experiments

For competition-focused practice, select **Competition reference · 1.31 km hop** with **Official debris cloud**. This ports the official relative geometry and 500-body debris distribution into the local model.

The default **Far-side L2** world uses **Transit traffic** moving **Across Phobos** at 20 m/s. It is a separate stress test, not the competition field.

The [official-profile comparison](docs/official-comparison.md) records matched attempts by the published beeline controller, Ali's official-profile policy, WSO's checkpoint profiles and the basic PD/radar reference.

![Official-profile outcomes, arrival time, final speed, local fuel and damage](docs/analytics/official-comparison/performance.svg)

Compare time, arrival speed, fuel and damage together. Local arrival means entry to the station's 50 m region, not docking. Displayed points apply the published formula to local counters; they are not official STP results.

The [older teammate comparison](docs/flight-lab.md#recorded-teammate-flights) used a different broad-stream world. Do not mix its results with the official-cloud or transit experiments. Instructions for [repeating and publishing comparisons](docs/flight-lab.md#repeat-and-publish-a-comparison) are in the guide.

## Team records

- [Meeting minutes](minutes/)
- [Shared mission notebook](notebook/NOTEBOOK.md)
- [Controller theory and implementation review](docs/python-flight-theory.md)

Historical Lunar SAR experiments and notebooks remain as reference material. Raw sweep outputs stay local under the ignored `runs/` directory; reviewed comparison datasets and charts live in [`docs/analytics/`](docs/analytics/).

Keep connection files, tokens and editor links private. The notebook executes code on its host. Use owner-approved authenticated Tailscale Serve for remote access, not a public editor or Funnel. Cloudflare releases remain owner-controlled.

## Competition resources

- [Escape from Phobos rules and submissions](https://github.com/SimDynamX/SpaceTeamsPRO/discussions/218)
- [Official announcements and discussions](https://github.com/SimDynamX/SpaceTeamsPRO/discussions)
- [Competition simulation source](https://github.com/SimDynamX/STU4-Sim)
- [Controller starter and mission API](https://github.com/SimDynamX/STU4-Solution/wiki)
- [Space Teams PRO documentation](https://github.com/SimDynamX/SpaceTeamsPRO/wiki) and [Python API](https://simdynamx.github.io/stp-python-api-docs/)
