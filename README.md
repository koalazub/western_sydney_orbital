# Western Sydney Orbital

Western Sydney Orbital is our team's workspace for the Space Teams PRO competition. We use it to develop mission strategies, compare ideas and share what we learn.

## Our approach

Open the [team simulation](https://western-sydney-orbital-api.western-sydney-orbital-worker.workers.dev) to explore the mission in your browser. Camera navigation, mission controls, telemetry and entity inspection belong to the Bevy game itself.

The Fall 2026 challenge is **Escape from Phobos**. An autonomous crew shuttle must reach a return ship through impact debris. We compare guidance and avoidance strategies using flight time, fuel use and damage, then validate them in Space Teams PRO. Our [shared notebook](notebook/NOTEBOOK.md) brings together research, observations and team decisions.

Our practice game starts a lander on a landing pad on Phobos. The objective is to cross the debris field and reach a return ship near the Mars–Phobos L2 point. The rotating-frame model accounts for both bodies' gravity, Coriolis and centrifugal effects, and calculates L1 and L2 from the same force model.

Top and bottom ship cameras provide fixed 512×512 images with eight bits per RGB channel and fixed exposure. Sun backlighting and similar-lit debris against Mars limit what those feeds reveal. Separate debug views let us inspect the world without changing the ship cameras or their pixels.

Each teammate can run an independent browser instance. Scenario inputs live in [`simulation/scenarios/`](simulation/scenarios/), so proposed changes can be reviewed in the repository and discussed through NotebookLM before publishing a shared release.

This is a circular, point-mass practice model with synthetic terrain and approximate contacts, fuel, damage and radar. Its physics and camera settings are our game model, not validated Space Teams PRO equivalence. Practice results are not official competition scores. Earlier Lunar SAR and rover experiments remain in the repository for reference.

## Team members

| Member | Responsibility | Reference |
| --- | --- | --- |
| To be added | Mission planning | [Simulation structure](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/Sim-Structure) |
| To be added | Python and automation | [Python systems](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/Programming-STP-Sims-with-Python) |
| To be added | Testing and analysis | [Simulator debugging](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/Python-System-Debugging) |
| To be added | Team coordination | [Collaboration](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/Collaboration-with-Git) |

## Competition resources

- [Official announcements and discussions](https://github.com/SimDynamX/SpaceTeamsPRO/discussions)
- [Escape from Phobos rules](https://github.com/SimDynamX/SpaceTeamsPRO/discussions/218)
- [Competition simulation](https://github.com/SimDynamX/STU4-Sim)
- [Controller starter and mission API](https://github.com/SimDynamX/STU4-Solution/wiki)
- [Space Teams PRO documentation](https://github.com/SimDynamX/SpaceTeamsPRO/wiki)
- [Running code in the simulator](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/Programming-STP-Sims-with-Python)
- [Python API](https://simdynamx.github.io/stp-python-api-docs/)
