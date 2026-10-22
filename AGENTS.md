# Working in this repository

Read `justfile` before running commands. Use its recipes for the development workflow; extend it when a new sequence is needed instead of inventing ad hoc CLI calls. Read `devenv.nix` for the environment and devenv-managed Rust nightly setup. Never use rustup or global Rust installs. Use UV for Python.

Track work, dependencies and subagent ownership in Beads. Delegate independent tasks in parallel and keep shared-file ownership explicit. Do not add parallel plans, upkeep notes, verification documents, test projects or benchmarks unless requested. Preserve existing experiments and other contributors' work.

Use Jujutsu for version control, not Git write commands. Make atomic, single-purpose commits; do not bundle environment changes, physics changes and interface changes into one commit. Use the version-control recipes in `justfile`. Do not change global identity or authentication.

Keep Rust domain data separate from implementation. Internal states and commands use enums and exhaustive pattern matching; string parsing belongs at external boundaries. Scenario parameters belong in versioned data. Prefer pure calculations and state transitions, with explicit adapters for Bevy, physics and browser effects.

`simulation/` owns the game, HUD, camera and physics. `worker/web/` is a thin browser loader and platform adapter. Deployment and resource configuration live in `worker/wrangler.jsonc`; Cloudflare releases remain owner-controlled. Keep private control endpoints authenticated. Local sharing uses Tailscale Serve, not Funnel. Do not change account-wide settings without permission.

Keep `README.md` focused on the competition rather than duplicating command or configuration references. Preserve notebook entries and historical Python experiments. Do not claim exact STP equivalence without authoritative runtime evidence.
