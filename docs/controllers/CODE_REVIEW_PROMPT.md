# Controller audit and refactor prompt

Copy the prompt below into your coding agent for one controller at a time.
Replace the `<...>` placeholders before sending it.

---

## Prompt

You are auditing one competition controller in this repository. Your job is to
make its logic explicit, encapsulated, readable and measurably correct — without
changing validated behaviour by accident.

### Scope and ownership

- Work only on the controller named `<controller name>`:
  - core: `<core file>`
  - adapter: `<adapter file>`
  - settings allowlist/validation: `<settings module>`
  - tests: `<test file>`
  - docs: `<controller README>`
- Do not edit another contributor's controller, adapter, tests or docs.
- Do not change shared physics, transport, deployment, worker or UI code.
- Do not weaken collision, guard, damage or safety checks.
- Do not run the full `just verify` suite unless the owner explicitly asks. Use
  the directly affected tests and a short native-sim seed check.

### Non-negotiable constraints

- The submission is a single-file controller. Keep the marked core region and
  the source loader contract intact. Do not introduce imports or module splits
  that the loader cannot execute.
- Follow the official STU4 template exactly:
  `git clone --branch template https://github.com/Legolas4224/STU4-Solution.git`.
  The `DON'T CHANGE` block must diff clean: exact banners, imports, entity
  handles, camera frames, `PanTiltCamera`, `GetNearestDebrisLocations` and
  `SetCommand`. Keep user code below it. Do not add imports or helpers inside the
  block; if formatting would rewrite it, wrap it in `# fmt: off` / `# fmt: on`
  and compare the block with `diff` before committing.
- Work inside the project environment: `nix develop --command <command>` or the
  documented `just` recipe. Do not wrap commands in `bash -c`, ad-hoc shell
  pipelines or interpreter snippets when a native command exists.
- Native and adapter paths must share preprocessing. If the adapter corrects
  radar epochs, frames or row caps, move that correction into the core (or a
  shared function) and test the native path. The adapter must not be the only
  place a correctness step exists.
- Python is 3.13. Prefer `enum.Enum` and `match`/`case` for states. Do not rely
  on a 3.15-only builtin such as `frozendict`.
- Keep random sampling deterministic for the same seed.
- Keep the controller's public entrypoint and settings keys working unless the
  owner approves an interface change.
- Preserve validated score, fuel, damage and arrival time unless the change is a
  measured optimisation. State the baseline before changing anything.

### Audit checklist

1. **Implicit state machines.** Find every behaviour that depends on a flag,
   timer, dict key or ordering of `if` branches. Typical examples:
   collision clear/escape/detour, guard clear/escape/recover, governor
   burn/coast hysteresis, attitude hold/slew, launch/cruise/terminal phase,
   planner/avoider selection. Replace each with a named enum and one explicit
   transition function. The transition must be exhaustive, with no silent
   fallthrough.
2. **Encapsulation.** Each state machine should own its state. Pure calculations
   should be pure functions; stateful components should be objects with a small
   public surface. Do not leave persistent state spread across a flat dict if a
   named object would make ownership obvious. Do not hide state in module
   globals.
3. **Pattern matching.** Use `match`/`case` for enum and mode transitions where
   it makes the decision table readable. Use guards for cross-field conditions.
   Be careful: `match` compares `True`, `False` and `None` by identity, so cast
   NumPy booleans with `bool(...)` before matching them against literals.
4. **Behavior preservation.** Before changing a formula, threshold, option list,
   default or priority order, write down the old behaviour. Refactors must
   preserve it. If a change is intentional, isolate it and measure it.
5. **Cruft and duplication.** Remove:
   - unused helpers, constants, imports and settings keys;
   - settings that are accepted but ignored;
   - duplicated formulas and duplicated state writes;
   - branches that cannot be reached;
   - wrappers that only call one other function;
   - stale comments, README claims and tests for removed paths.
6. **Readability and abstraction.** Use one key per line in configuration
   dictionaries, grouped with short comments. Name magic numbers. Give each
   section one responsibility. Prefer a small number of well-named functions
   over one long branch chain. Comments should explain why, not restate code.
7. **Tests.** Test observable behaviour, not internal structure. Keep the suite
   small and fast. Every test must execute the path it claims to cover. A green
   suite is not evidence that a controller path works if no test steps it.
8. **Performance.** Track score, fuel, damage and arrival time separately. Do
   not claim an optimisation without a seed result. Report the trade-off:
   lower fuel can lose time points; a safer guard can spend fuel. Keep
   avoidance proactive by predicting clearance from fitted track positions, not
   range rate alone.
9. **Overshoot and inertia.** Check whether a controller burns until it crosses
   a target and then has to reverse. Look for full-thrust velocity commands,
   missing stopping-distance/braking terms, cross-track velocity left after a
   dodge, and attitude commands that change while thrust is applied. Fixes must
   be bounded and measured; do not replace one oscillation with another.
10. **Launch and close-threat cases.** Test a threat near the launch pad before
    the initial burn, not only mid-cruise encounters. A controller that dodges
    at cruise speed may behave differently at zero speed and low thrust.
11. **State ownership and dead labels.** An enum or mode that is written but
    never read is not a state machine; it is dead telemetry. Either make it the
    authoritative transition state or delete it. Do not document a state machine
    that the code does not own.
12. **Time alignment and measurement history.** Radar rows carry a measurement
    epoch. Store recent shuttle states, reconstruct each row from the shuttle
    position at that epoch, and define the fallback when history is unavailable.
    A stationary rock must not move because the shuttle moved between measurement
    and reconstruction. Test the exact aged-row case.
13. **Prediction-based, deterministic policy.** Where a policy chooses a side,
    escape or route, score the candidates by predicted clearance using fitted
    tracks. Use a fixed tie-break and a continuity margin; do not use hit parity,
    random choice or a hardcoded alternation. Expose the score and the reason in
    telemetry so the choice can be audited.
14. **Merge and parity checks.** Fetch before pushing; rebase onto the new
    remote tip instead of force-pushing. Re-run the affected tests after a merge.
    Report local test success, native-sim success and transport/hosted status
    separately; `lease_expired` is a harness failure, not a controller failure.

### Process

**Phase 1 — read-only audit**

Read the core, adapter, settings and tests. Produce a report before editing:

- state machines found, with file:line and the implicit fields/timers;
- dead or duplicated code, with file:line;
- behaviour risks and ambiguous ownership;
- tests that do not exercise their claimed path;
- a prioritized list: safe deletions vs behaviour-changing changes.

**Phase 2 — behaviour-preserving refactor**

Extract one section at a time into named state objects/enums. Keep the maths
identical. Use `match`/`case` for transitions. Do not combine the refactor with
tuning in the same change.

**Phase 3 — verification**

1. Run the directly affected unit tests and the controller catalog test.
2. Run one native-sim baseline seed and compare score, fuel, damage and arrival
   time with the recorded baseline.
3. If the behaviour is intentionally changed, run the agreed seed set and report
   the distribution, not a single lucky seed.
4. Format the changed files.
5. Compare the `DON'T CHANGE` template block with the official template using
   `diff`; it must be empty.
6. Verify native/adapter preprocessing parity on the same input.
7. Commit only the files you own, as one purpose per commit.

### Output format

Return:

1. **Audit table** — area, location, problem, risk, proposed action.
2. **Safe removals** — exact items that do not change behaviour.
3. **Behaviour-changing proposals** — what and why, with expected metric impact.
4. **Test gaps** — paths that are currently unexercised.
5. **Verification evidence** — commands and results, kept separate for local
   tests, native sim and remote/hosted runs.

### Acceptance criteria

- No implicit state machine remains in the audited controller.
- Each state transition is a named enum transition with an exhaustive decision.
- No dead settings or unreachable branches remain.
- The refactor preserves the baseline metrics within noise, or the intentional
  delta is measured and documented.
- The affected tests pass and actually execute the changed paths.
- The `DON'T CHANGE` template block diff is empty.
- Native and adapter paths apply the same epoch/frame/row preprocessing.
- No state label is written but unread, and no dead settings remain.
- Prediction-based choices expose a deterministic score, tie-break and reason.
- Seed results distinguish controller behaviour from transport or lease failures.
- The diff touches only the named controller's files.
