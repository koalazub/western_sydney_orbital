# Agent instructions

- Do only the requested work. Track tasks and dependencies in Beads with `bd`; do not add parallel task documents, upkeep notes, verification logs, or tool manuals.
- Publish Beads through `make beads-sync`; its storage commits use no-reply metadata without changing global Git identity or email privacy settings.
- Rust and browser WASM tools must come from pinned Fenix/Nix inputs. Do not use rustup or global Rust toolchain installs.
- Use devenv for the environment and UV for every Python command, including helper scripts. Never invoke `python`, `python3`, or `pip` directly from the shell.
- devenv is the primary environment for tasks and processes; `nix develop` is a compatibility shell reusing its declared packages and environment, not its tasks or processes.
- Keep Python dependencies in `pyproject.toml` and `uv.lock`. Do not add testing or benchmark work unless requested.
- Prefer `jj` for version control. Keep Git compatibility for GitHub, and never change global identity or authentication. `.jjconfig.toml` supplies optional project aliases.
- `worker/` uses `wrangler.jsonc` and deploys with Wrangler. Use `cf` for account/resource commands; do not run its project setup or deployment commands here without an explicit migration.
- Use the project commands `make account`, `make login-cf`, `make login-wrangler`, `make build` and `make deploy` for Cloudflare work. Deployment checks both logins against the configured account and builds the application before uploading it.
- Local shared services use Tailscale Serve, never Funnel. Hosted services use Cloudflare; authenticate private data and control endpoints. Do not change account-wide networking or access rules without permission.
- Support macOS, Linux, and Windows through WSL2. Space Teams PRO itself is installed separately.
- `simulation/` is the browser Bevy engine, `worker/web/` provides Python controls, and `worker/src/` coordinates shared rooms. `src/wsorb/` retains the earlier offline practice runner and notebook tools.
- The Lunar SAR model is a strategy approximation, not the real STP simulator or official competition scoring.
- Notebook entries live in `notebook/entries/`; regenerate the index with `uv run wsorb notebook index`. Preserve other contributors' entries and code.
