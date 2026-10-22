# Western Sydney Orbital

Team workspace for Space Teams PRO, with a shared notebook and a Python model for comparing search strategies. The built-in Lunar SAR scenario is an approximation, not the real simulator or official competition scoring.

## Tools

[devenv](https://devenv.sh/getting-started/) provides the environment,
[UV](https://docs.astral.sh/uv/) manages Python,
[pytest](https://docs.pytest.org/) runs tests,
[Jujutsu](https://docs.jj-vcs.dev/latest/) manages changes, and
[Tailscale Serve](https://tailscale.com/kb/1312/serve) shares the notebook.

The same environment keeps setup consistent across teammates' machines. Private sharing and version-controlled notes let the team work together without exposing services publicly.

## Get running

Install Nix, devenv, and Jujutsu using the links above, and connect Tailscale on the machine hosting shared services. Windows users need [WSL2](https://learn.microsoft.com/en-us/windows/wsl/install); run the development tools and Tailscale in the same Linux environment.

```sh
jj git clone --colocate https://github.com/koalazub/western_sydney_orbital
cd western_sydney_orbital
devenv shell
uv sync --locked
uv run --locked pytest
```

To share the notebook, run `devenv up` and open the printed tailnet HTTPS URL on another authorized device. Tailscale HTTPS must be enabled; the local backend stays private.

## Use the project

```sh
uv run wsorb sim run --seed 7
uv run wsorb campaign run --replicates 24 --seed 42 --notebook
uv run wsorb notebook add --title "Search idea" --author "Your name" --body "What to try next."
```

The [shared notebook](notebook/NOTEBOOK.md) collects entries from `notebook/entries/`. Campaign reports go into `runs/`; push changes with `jj` to share the files as well as the service.

## Team members

| Member | Responsibility | Reference |
| --- | --- | --- |
| To be added | Mission planning | [Simulation structure](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/Sim-Structure) |
| To be added | Python and automation | [Python systems](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/Programming-STP-Sims-with-Python) |
| To be added | Testing and analysis | [Lunar SAR API](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/STU-Lunar-SAR-API-Docs) |
| To be added | Team coordination | [Collaboration](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/Collaboration-with-Git) |

## Space Teams PRO

[Official documentation](https://github.com/SimDynamX/SpaceTeamsPRO/wiki) · [Install the simulator](https://github.com/SimDynamX/SpaceTeamsPRO/wiki/Installation-Setup) · [Python API](https://simdynamx.github.io/stp-python-api-docs/)

The reference model uses the [historical Lunar SAR starter](https://github.com/SimDynamX/STU_Lunar_SAR). Current competition links still need to be supplied.
