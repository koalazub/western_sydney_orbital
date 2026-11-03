# Working in this repository

Read `justfile` and `devenv.nix` before running commands. Just recipes only invoke npm tasks; `worker/package.json` owns the project build, formatting and verification commands. Enter the Nix/devenv environment outside those tasks. Use other installed tools directly, including `cf`, Wrangler, `jj`, `gh`, `bd` and `uv`; do not add command aliases, passthrough scripts or duplicate task layers. Rust nightly is devenv-managed. Never use rustup or global Rust installs. Use UV for Python.

Deployment must use the existing Nix/devenv/Just workflow and project-pinned `cf` and Wrangler executables directly. Nix/devenv owns the environment; Just and npm scripts invoke the tools. Do not add custom CLI or build/deploy orchestration wrappers in JavaScript, shell, or another language, a parallel build/deploy workflow, or credential-provisioning automation to work around a CLI or environment blocker. Inspect the installed CLI's help, schemas and original error output first. If the supported workflow cannot perform the required operation, report the specific limitation and agree on the workflow change before implementing it. This rule also applies to delegated work.

Hosting must stay on Cloudflare Free. Teammate Python controllers run on their own machines, using the local game or the existing authenticated room relay. Do not add paid runner infrastructure or change billing.

After the owner confirms a dashboard operation, investigate conflicting CLI evidence before asking them to repeat it. A failed metadata request is not proof that the Git integration is disconnected. Keep local test success, remote build configuration and an observed hosted flight distinct in reports.

Complete the entire agreed implementation before running formatting, builds, tests or verification. Do not run checks after individual edits, track test counts as progress, or turn an unfinished feature into repeated redesign and validation cycles. Once all scoped implementation is complete, format the whole project and run one unfiltered verification. Fix the reported failures together before another full run. Never weaken safety checks or fabricate results to finish.

When the owner challenges the workflow, stop execution and answer directly. Do not immediately start another implementation, formatting or verification cycle. Do not expand a bug fix into a controller redesign or a new protocol feature without first agreeing that scope with the owner.

Do not spawn audit-only or proof-review agents unless the owner explicitly requests them. Delegate concrete independent implementation tasks only when useful, with explicit file ownership. Do not perform routine Beads logging. Do not add parallel plans, upkeep notes, verification documents, test projects or benchmarks unless requested. Preserve existing experiments and other contributors' work.

Use Jujutsu directly for version control, not Git write commands. Make atomic, single-purpose commits; do not bundle environment changes, physics changes and interface changes into one commit. Do not change global identity or authentication.

Keep Rust domain data separate from implementation. Internal states and commands use enums and exhaustive pattern matching; string parsing belongs at external boundaries. Scenario parameters belong in versioned data. Prefer pure calculations and state transitions, with explicit adapters for Bevy, physics and browser effects.

`simulation/` owns the game, HUD, camera and physics. `worker/web/` is a thin browser loader and platform adapter. Deployment and resource configuration live in `worker/wrangler.jsonc`; Cloudflare releases remain owner-controlled. Keep private control endpoints authenticated. Local sharing uses Tailscale Serve, not Funnel. Do not change account-wide settings without permission.

Keep `README.md` focused on the competition rather than duplicating command or configuration references. Preserve notebook entries and historical Python experiments. Do not claim exact STP equivalence without authoritative runtime evidence.
