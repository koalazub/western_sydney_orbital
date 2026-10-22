# Agent instructions

- Do only the requested work. Do not add upkeep notes, task ledgers, verification logs, or tool manuals. Keep explanations short and link to official docs.
- Use devenv for the environment and UV for every Python command, including helper scripts. Never invoke `python`, `python3`, or `pip` directly from the shell.
- devenv is the primary environment for tasks and processes; `nix develop` is a compatibility shell reusing its declared packages and environment, not its tasks or processes.
- Keep Python dependencies in `pyproject.toml` and `uv.lock`. Do not add testing or benchmark work unless requested.
- Prefer `jj` for version control. Keep Git compatibility for GitHub, and never change global identity or authentication. `.jjconfig.toml` supplies optional project aliases.
- `worker/` uses `wrangler.jsonc` and deploys with Wrangler. Use `cf` for account/resource commands; do not run its project setup or deployment commands here without an explicit migration.
- Local shared services use Tailscale Serve, never Funnel. Hosted services use Cloudflare; authenticate private data and control endpoints. Do not change account-wide networking or access rules without permission.
- Support macOS, Linux, and Windows through WSL2. Space Teams PRO itself is installed separately.
- `src/wsorb/` contains scenario loading, route planning, simulation, campaign reports, notebook tools, and the read-only sharing service. Scenarios live in `src/wsorb/data/`; tests in `tests/`.
- The Lunar SAR model is a strategy approximation, not the real STP simulator or official competition scoring.
- Notebook entries live in `notebook/entries/`; regenerate the index with `uv run wsorb notebook index`. Preserve other contributors' entries and code.
