"""Python-stdlib source contracts only, not executed JS, DOM or native-run proof."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "worker/web/controller-status.ts").read_text()
AVIONICS = (ROOT / "simulation/src/avionics.rs").read_text()
RESILIENCE = (ROOT / "simulation/src/physics/resilience.rs").read_text()


def section(start: str, end: str) -> str:
    return SOURCE.split(start, 1)[1].split(end, 1)[0]


class ControllerStatusSourceContracts(unittest.TestCase):
    def test_public_api_is_unknown_input_and_plain_string_output(self):
        self.assertIn(
            "export function describeControllerStatus(connection: unknown, observation: unknown, "
            "runtime: unknown): ControllerStatus",
            SOURCE,
        )
        self.assertIn(
            "export type ControllerStatus = { state: string; eventKey: string; headline: string; "
            "evidence: string };",
            SOURCE,
        )

    def test_presenter_has_no_effects_or_control_capabilities(self):
        for forbidden in (
            "import ",
            "fetch(",
            "XMLHttpRequest",
            "WebSocket",
            "document.",
            "window.",
            "globalThis",
            "location.",
            "localStorage",
            "sessionStorage",
            "console.",
            "setTimeout(",
            "setInterval(",
            "requestAnimationFrame(",
            "Date.",
            "new Date(",
            "performance.",
            "Math.random(",
            "JSON.parse(",
            "JSON.stringify(",
            "innerHTML",
            "eval(",
            "new Function(",
            "api.control",
            "engine.",
            "Authorization",
            "Bearer ",
            "token",
            "page_id",
            "profile_id",
            "job_id",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, SOURCE)
        self.assertNotRegex(SOURCE, r"\b(?:async|await|Promise|any)\b")

    def test_native_struct_names_and_statuses_come_from_actual_sources(self):
        for struct in ("ControlAuthority", "ControlFence", "CommandWatchdog"):
            self.assertIn(f"pub struct {struct}", AVIONICS)
        for field in (
            "status",
            "epoch",
            "term",
            "run_id",
            "controller_id",
            "source_time_s",
            "expires_at_sim_time_s",
            "controller_fence",
        ):
            with self.subTest(field=field):
                self.assertIn(f"pub {field}:", AVIONICS)
                self.assertIn(field, SOURCE)
        self.assertIn('report["control_authority"] = json!(self.control_authority())', RESILIENCE)
        self.assertIn(
            "authority.expires_at_sim_time_s != watchdog.expires_at_sim_time_s", RESILIENCE
        )
        self.assertIn("pub fn valid_control_id", AVIONICS)
        for native_status in (
            "Idle",
            "Active",
            "Expired",
            "Revoked",
            "SensorChanged",
            "Restored",
        ):
            self.assertIn(native_status, AVIONICS)

    def test_validators_reject_non_json_records_accessors_and_extra_fields(self):
        self.assertIn("!Array.isArray(value)", SOURCE)
        self.assertIn("[Object.prototype, null].includes(Object.getPrototypeOf(value))", SOURCE)
        self.assertIn("Object.getOwnPropertyDescriptor(value, name)", SOURCE)
        self.assertIn('Object.hasOwn(property, "value")', SOURCE)
        validation = section("function shape(", "function matches(")
        self.assertIn("required.every(key => Object.hasOwn(value, key))", validation)
        self.assertIn("Reflect.ownKeys(value).every", validation)
        self.assertIn("property.enumerable", validation)
        self.assertIn('typeof key === "string"', validation)
        self.assertIn('typeof value === "number" && Number.isFinite(value)', SOURCE)
        self.assertIn("Number.isInteger(value) && value <= 4294967295", SOURCE)
        self.assertIn("/^[A-Za-z0-9_-]{1,64}$/", SOURCE)
        self.assertNotRegex(SOURCE, r"\b(?:parseInt|parseFloat)\(")
        self.assertNotIn("Number(value)", SOURCE)

    def test_idle_requires_complete_idle_native_authority_and_watchdog(self):
        authority = section("function readAuthority(", "function readFence(")
        self.assertIn(
            "term !== 0 || run_id !== null || controller_id !== null || expiry !== null",
            authority,
        )
        watchdog = section("function readWatchdog(", "function matches(")
        self.assertIn('if (status === "idle")', watchdog)
        self.assertIn("source === null && expiry === null && fence === null", watchdog)
        native = section("function nativeStatus(", "/** Use eventKey")
        self.assertIn('if (authority.status === "idle")', native)
        self.assertIn('if (watchdog?.status !== "idle") return unknownStatus();', native)
        self.assertIn('presentation("idle", "Controller idle"', native)
        self.assertIn("Native control authority: idle. No leased command accepted.", native)

    def test_connected_is_never_itself_running_or_idle(self):
        public = section("export function describeControllerStatus(", "\nNOT_PRESENT")
        self.assertIn('status === "connected" ? native', public)
        self.assertNotIn('presentation("running"', public)
        self.assertNotIn('presentation("idle"', public)
        native = section("function nativeStatus(", "/** Use eventKey")
        self.assertIn('!finite(time) || mission !== "running"', native)
        self.assertIn('typeof running !== "boolean" || keyboard !== false', native)
        self.assertIn("!unsigned(revision)", native)
        self.assertIn("observedRevision === revision", native)
        self.assertIn("!sameTrial", native)
        self.assertIn("authority === null) return unknownStatus()", native)

    def test_running_requires_full_current_fence_and_both_deadlines(self):
        matching = section("function matches(", "function vector(")
        for identity in ("epoch", "term", "run_id", "controller_id"):
            self.assertIn(f"fence.{identity} === authority.{identity}", matching)
        watchdog = section("function readWatchdog(", "function matches(")
        self.assertIn("!finite(source) || !finite(expiry) || source > time", watchdog)
        self.assertIn("expiry - source < 0.05 - 1e-9", watchdog)
        self.assertIn("expiry - source > 1 + 1e-9", watchdog)
        native = section("function nativeStatus(", "/** Use eventKey")
        self.assertIn("expiry > time + 1 + 1e-9", native)
        self.assertIn(
            'authority.status === "active" && (expiry === null || time >= expiry)', native
        )
        self.assertIn("!matches(authority, watchdog.controller_fence)", native)
        self.assertIn(
            "source === null || deadline === null || source > time || "
            "time >= deadline || expiry !== deadline",
            native,
        )
        self.assertIn('presentation(running ? "running" : "paused"', native)
        self.assertIn(
            'running ? "Controller running" : "Controller owns control · flight paused"', native
        )
        self.assertEqual(SOURCE.count('"Controller running"'), 1)

    def test_latest_receipt_is_evidence_not_a_first_start_or_wall_clock_claim(self):
        native = section("function nativeStatus(", "/** Use eventKey")
        self.assertIn("Latest accepted command: T+${timestamp(source)} s.", native)
        self.assertIn("Command expiry: T+${timestamp(deadline)} s.", native)
        self.assertIn("Verified ${identity(authority)}.", native)
        self.assertIn('new Intl.NumberFormat("en"', SOURCE)
        self.assertIn("maximumFractionDigits: 2", SOURCE)
        self.assertNotRegex(native, r"\b(?:started|starting|start_time|since|wall.clock)\b")
        self.assertIn("Control claimed; waiting for a command", SOURCE)
        self.assertIn("No current matching leased command is verified.", SOURCE)

    def test_native_expiry_wins_even_before_simulation_deadline(self):
        native = section("function nativeStatus(", "/** Use eventKey")
        expired = native.index('authority.status === "expired"')
        time_guard = native.index(
            'authority.status === "active" && (expiry === null || time >= expiry)'
        )
        self.assertLess(expired, time_guard)
        self.assertIn("Controller lease expired", native)
        self.assertIn("Controller released control", native)
        self.assertIn("Controller command cleared by sensor change", native)
        self.assertIn("Controller command cleared after restore", native)
        for native_status in ("expired", "revoked", "sensor_changed", "restored"):
            self.assertIn(f'case "{native_status}"', native)

    def test_terminal_and_keyboard_signals_outrank_old_controller_receipts(self):
        native = section("function nativeStatus(", "/** Use eventKey")
        terminal = native.index('mission === "success" || mission === "failed_damage"')
        manual = native.index("if (keyboard === true)")
        authority = native.index("authority === null) return unknownStatus()")
        receipt = native.index("const watchdog = readWatchdog(")
        self.assertLess(terminal, manual)
        self.assertLess(manual, authority)
        self.assertLess(authority, receipt)
        self.assertIn('"Flight ended · arrived"', native)
        self.assertIn('"Flight ended · impact damage"', native)
        self.assertIn("Native mission reports arrival.", native)
        self.assertIn("Native mission reports failure from impact damage.", native)
        manual_branch = native.split("if (keyboard === true)", 1)[1].split("if (!finite", 1)[0]
        self.assertIn('presentation("manual", "Manual keyboard control"', manual_branch)
        self.assertNotIn("identity(authority)", manual_branch)
        self.assertNotIn("Latest accepted command", manual_branch)
        self.assertNotRegex(SOURCE, r"\b(?:score|performance_estimate|success_rate)\b")

    def test_raw_command_readback_is_current_finite_and_not_a_stopped_ship_claim(self):
        requested = section("function requestedCommand(", "function identity(")
        self.assertIn('field(observation, "applied_physics")', requested)
        self.assertIn('field(physics, "source_time_s") !== time', requested)
        self.assertIn("requested.accel_body_mps2", requested)
        self.assertIn("requested.angular_accel_body_rad_s2", requested)
        self.assertIn("!vector(linear) || !vector(angular)", requested)
        self.assertIn("Native requested command is zero; motion may continue.", requested)
        self.assertIn("Native requested command is nonzero", requested)
        self.assertIn(
            "Current command output is unverified. A native stop is not confirmed.", requested
        )
        self.assertIn('presentation("unleased", "Unleased API command"', SOURCE)
        self.assertNotRegex(
            SOURCE, r"\b(?:thrusters off|thrust stopped|ship stopped|zero velocity)\b"
        )

    def test_native_identity_is_validated_short_and_never_joined_to_queue_data(self):
        identity = section("function identity(", "function timestamp(")
        self.assertIn("authority.run_id.slice(-8)", identity)
        self.assertIn("owner ${authority.controller_id.slice(-8)}", identity)
        self.assertNotIn("connection", identity)
        self.assertNotRegex(SOURCE, r"\.(?:active|profile|profiles|job|jobs|catalog|origin|url)\b")

    def test_connection_errors_are_classified_with_fixed_copy_and_recovery(self):
        public = section("export function describeControllerStatus(", "\nNOT_PRESENT")
        self.assertIn(
            'typeof error === "string" && error.includes('
            '"Another browser page holds the local simulation lease")',
            public,
        )
        self.assertIn("Switch to the connected game; do not reset either flight.", public)
        self.assertIn('presentation("disconnected", "Python API disconnected"', public)
        self.assertIn('presentation("error", "Python API connection error"', public)
        self.assertIn("Disconnection alone does not confirm a native stop.", public)
        self.assertIn("Reconnect the local Python API and inspect the flight", public)
        self.assertIn("Check the local Python service and reconnect", public)
        self.assertIn("Last native observation:", public)
        self.assertNotRegex(public, r"\$\{(?:error|status|connection)(?:[.}]|\b)")
        self.assertEqual(public.count('field(connection, "error")'), 1)
        self.assertEqual(public.count("error.includes("), 1)

    def test_event_key_tracks_only_lifecycle_identity_revision_and_terminal_outcome(self):
        key = section("const run = authority === null", "\nNOT_PRESENT")
        for identity in ("epoch", "term", "run_id", "controller_id"):
            self.assertIn(f"authority.{identity}", key)
        self.assertIn("unsigned(revision) ? revision", key)
        self.assertIn("eventKey: `${view.state}:${run}:", key)
        self.assertIn('view.state === "ended"', key)
        for ticking_field in (
            "time_s",
            "source_time",
            "expires_at",
            "timestamp(",
            "evidence",
            "error",
            "connection",
        ):
            self.assertNotIn(ticking_field, key)
