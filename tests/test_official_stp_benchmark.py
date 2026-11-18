"""Focused stdlib tests runnable in the native STP Python 3.12 environment."""

import ast
import csv
import datetime
import importlib.util
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

BENCHMARK = Path(__file__).resolve().parents[1] / "experiments" / "stp_benchmark"
sys.path.insert(0, str(BENCHMARK))
spec = importlib.util.spec_from_file_location("official_stp_evidence", BENCHMARK / "evidence.py")
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)
import benchmark  # noqa: E402
import capture  # noqa: E402
import seeds  # noqa: E402


class OfficialBenchmarkTests(unittest.TestCase):
    def test_matched_addition_uses_frozen_settings_and_never_reschedules_existing_controllers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bank, _ = self.bank_fixture(root)
            base_config = dict(
                self.config(), seed_count=1, seeds=[100], repetitions=1, seed_bank=str(bank)
            )
            base = root / "runs/base"
            base.mkdir(parents=True)
            evidence.write_json(base / "campaign.json", dict(mode="seeded", config=base_config))
            evidence.write_json(
                base / "provenance.json", dict(configuration=base_config, snapshot_hashes={})
            )
            before = (base / "campaign.json").read_bytes()
            incoming = json.loads((BENCHMARK / "campaign.json").read_text())
            incoming["limits"]["simulation_s"] = 999
            source = root / "incoming.json"
            evidence.write_json(source, incoming)

            def inspect(config, mode):
                self.assertEqual([c["id"] for c in config["controllers"]], ["bui"])
                self.assertEqual(config["limits"], base_config["limits"])
                self.assertEqual(config["seeds"], [100])
                self.assertEqual(config["reference"], "bui")
                self.assertEqual(config["extends_campaign"], str(base.resolve()))
                self.assertNotIn("supersedes_execution", config)
                raise RuntimeError("configuration inspected before native preparation")

            with (
                patch.object(benchmark, "REPO", root.resolve()),
                patch.object(benchmark, "schedule", side_effect=inspect),
                self.assertRaisesRegex(RuntimeError, "configuration inspected"),
            ):
                benchmark.prepare(
                    source, root / "runs/bui", "seeded", controller_ids=["bui"], match_campaign=base
                )
            self.assertEqual((base / "campaign.json").read_bytes(), before)
            with (
                patch.object(benchmark, "REPO", root.resolve()),
                self.assertRaisesRegex(ValueError, "only new controller IDs"),
            ):
                benchmark.prepare(
                    source,
                    root / "runs/reject-old",
                    "seeded",
                    controller_ids=["tom"],
                    match_campaign=base,
                )
            with (
                patch.object(benchmark, "REPO", root.resolve()),
                self.assertRaisesRegex(ValueError, "complete seed selection"),
            ):
                benchmark.prepare(
                    source,
                    root / "runs/reject-seeds",
                    "seeded",
                    seed_count=2,
                    controller_ids=["bui"],
                    match_campaign=base,
                )

    def test_combined_analysis_preserves_sources_and_pairs_new_controller_with_saved_seed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = dict(self.config(), seed_count=1, seeds=[100], repetitions=1)

            def make_source(name, selected, outcomes):
                folder = root / name
                folder.mkdir()
                effective = json.loads(json.dumps(config))
                effective.update(
                    controllers=selected, reference=selected[0]["id"], execution_batch=name
                )
                attempts = evidence.schedule(effective, "seeded")
                snapshots = {}
                for i, attempt in enumerate(attempts):
                    attempt["run_id"] = f"{name}-{i}"
                    snapshot = (
                        folder
                        / "snapshot/controllers"
                        / attempt["controller"]["id"]
                        / "Controller.py"
                    )
                    snapshot.parent.mkdir(parents=True)
                    snapshot.write_text("pass\n")
                    snapshots[snapshot.relative_to(folder / "snapshot").as_posix()] = (
                        evidence.digest(snapshot)
                    )
                    run = folder / attempt["run_id"]
                    run.mkdir()
                    outcome, score = outcomes[i]
                    evidence.write_json(
                        run / "receipt.json", dict(outcome=outcome, official_score=score)
                    )
                evidence.write_json(
                    folder / "campaign.json",
                    dict(mode="seeded", config=effective, attempts=attempts),
                )
                evidence.write_json(
                    folder / "provenance.json",
                    dict(
                        configuration=effective,
                        repository_revision=None,
                        snapshot_hashes=snapshots,
                        binaries={"sdk": "same"},
                        python="3.12",
                        python_dependencies={},
                        machine={"hostname": "same"},
                        scheduling="normal",
                        dependencies={
                            str(folder.resolve() / "snapshot/mission"): {"sha256": "same"}
                        },
                    ),
                )
                return folder

            base = make_source(
                "base", config["controllers"], [("success", 10), ("simulation_limit", None)]
            )
            bui_controller = dict(
                id="bui",
                author="Bui",
                method="Alpha-beta",
                directory="controllers/bui",
                entrypoint="Controller.py",
            )
            bui = make_source("bui", [bui_controller], [("success", 15)])

            def source_hashes():
                return {
                    str(p): evidence.digest(p)
                    for source in (base, bui)
                    for p in source.rglob("*")
                    if p.is_file()
                }

            before = source_hashes()
            summary = evidence.combine_campaigns([base, bui], root / "combined")
            self.assertEqual(source_hashes(), before)
            self.assertEqual(summary["tom"]["outcomes"], {"success": 1})
            self.assertEqual(summary["ali_mppi"]["outcomes"], {"simulation_limit": 1})
            self.assertEqual(summary["bui"]["controller_author"], "Bui")
            with (root / "combined/matched.csv").open(newline="") as stream:
                paired = next(row for row in csv.DictReader(stream) if row["controller"] == "bui")
            self.assertEqual(float(paired["score_delta"]), 5)
            self.assertEqual(paired["seed"], "100")
            self.assertTrue((root / "combined/analysis_sources.json").is_file())

            metadata_path = bui / "provenance.json"
            original = json.loads(metadata_path.read_text())
            mismatch = dict(original, binaries={"sdk": "changed"})
            evidence.write_json(metadata_path, mismatch)
            with self.assertRaisesRegex(ValueError, "binaries"):
                evidence.combine_campaigns([base, bui], root / "invalid")
            self.assertFalse((root / "invalid").exists())

    def test_atomic_json_retries_transient_windows_sharing_and_preserves_previous_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "manifest.json"
            evidence.write_json(target, {"version": 1})
            sharing = PermissionError("Windows reader temporarily denies replacement")
            sharing.winerror = 32
            original_replace = Path.replace
            attempts = 0

            def replace(source, destination):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise sharing
                return original_replace(source, destination)

            with patch.object(Path, "replace", autospec=True, side_effect=replace):
                evidence.write_json(target, {"version": 2})
            self.assertEqual(json.loads(target.read_text()), {"version": 2})
            self.assertEqual(attempts, 2)
            with (
                patch.object(Path, "replace", side_effect=sharing),
                patch.object(evidence.time, "monotonic", side_effect=[0, 2]),
                self.assertRaises(PermissionError),
            ):
                evidence.write_json(target, {"version": 3})
            self.assertEqual(json.loads(target.read_text()), {"version": 2})
            self.assertEqual(list(root.iterdir()), [target])

    def test_busy_evidence_stream_cannot_bypass_a_watchdog_deadline(self):
        events = queue.Queue()
        events.put({"kind": "command"})
        with patch.object(benchmark.time, "monotonic", return_value=10):
            with self.assertRaises(queue.Empty):
                benchmark.next_evidence_event(events, deadline=10)
            self.assertEqual(events.qsize(), 1)
            self.assertEqual(benchmark.next_evidence_event(events, deadline=11)["kind"], "command")

    def test_sensor_capture_failure_halts_even_when_controller_exceptions_may_continue(self):
        outcome = benchmark.event_error_outcome("sensor_error")
        self.assertEqual(outcome, "evidence_failure")
        self.assertTrue(
            benchmark.must_halt(
                dict(
                    outcome=outcome,
                    cleanup="owned_process_tree_exited",
                    error=dict(role="controller"),
                ),
                continue_controller_errors=True,
            )
        )
        self.assertIsNone(benchmark.event_error_outcome("telemetry"))

    def test_receiver_distinguishes_cleanup_disconnects_from_runtime_failures(self):
        class ResetStream:
            def readline(self, limit):
                return json.dumps(dict(token="test-token", role="controller")) + "\n"

            def __iter__(self):
                raise ConnectionResetError("simulator connection closed")

        for cleanup_started in (False, True):
            with self.subTest(cleanup_started=cleanup_started):
                receiver = object.__new__(benchmark.Receiver)
                receiver.events = queue.Queue()
                receiver.closed = False
                receiver.cleanup_started = cleanup_started
                receiver.token = "test-token"
                connection = SimpleNamespace(
                    settimeout=lambda value: None,
                    makefile=lambda *args, **kwargs: ResetStream(),
                    close=lambda: None,
                )
                receiver.read(connection)
                error = receiver.events.get_nowait()
                closed = receiver.events.get_nowait()
                self.assertEqual(error["kind"], "hook_error")
                self.assertEqual(error["phase"], "cleanup" if cleanup_started else "flight")
                self.assertEqual(closed["phase"], error["phase"])
                issue = evidence.incidents(
                    [error], {"outcome": "simulation_limit"}, self.config()["thresholds"]
                )
                self.assertEqual(
                    issue[0]["kind"], "cleanup_stream_error" if cleanup_started else "hook_error"
                )

    def test_execute_halts_on_sensor_error_and_preserves_native_logs_without_duplicate_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config()
            config.update(install=str(root / "install"), user_root=str(root / "user"))
            evidence.write_json(root / "campaign.json", dict(config=config))
            logs = root / "user/Logs/run"
            logs.mkdir(parents=True)
            native_log = logs / "controller.log"
            native_log.write_text("before")
            events = queue.Queue()
            for role in ("controller", "scorer"):
                events.put(
                    dict(
                        kind="connected",
                        role=role,
                        native_run_uuid="test",
                        native_logs=str(logs),
                        native_output=str(logs),
                    )
                )
            events.put(dict(kind="sensor_error", role="controller", error="sample failed"))
            receiver = SimpleNamespace(
                events=events, close=lambda: None, begin_cleanup=lambda: None
            )
            process = SimpleNamespace(wait=lambda **kwargs: 0, returncode=0)

            def launch(*args):
                native_log.write_text("after: native evidence")
                (logs.parent / "startup.log").write_text("startup diagnostic")
                return process

            def mission(*args):
                path = root / "attempt/mission.simconf"
                path.write_text("{}")
                return path, []

            job = SimpleNamespace(launch=launch, active=lambda: 1, close=lambda: None)
            attempt = dict(run_id="attempt", controller=config["controllers"][0], seed=None)
            with (
                patch.object(benchmark, "Receiver", return_value=receiver),
                patch.object(benchmark, "WindowsJob", return_value=job),
                patch.object(benchmark, "attempt_mission", side_effect=mission),
            ):
                receipt = benchmark.execute(root, attempt)
            self.assertEqual(receipt["outcome"], "evidence_failure")
            self.assertEqual(receipt["termination_reason"], "sensor_error")
            self.assertEqual(receipt["cleanup"], "owned_process_tree_exited")
            copies = list((root / "attempt").rglob("controller.log"))
            self.assertEqual(len(copies), 1)
            self.assertEqual(copies[0].read_text(), "after: native evidence")
            self.assertTrue((root / "attempt/native_startup/0/startup.log").is_file())

    def test_state_capture_supports_actual_current_governors_and_mppi_record(self):
        class Vector(list):
            def __sub__(self, other):
                return Vector(a - b for a, b in zip(self, other, strict=True))

        def body(position, velocity):
            return SimpleNamespace(
                GetBodyFixedFrame=lambda: None,
                getLocVelAcc=lambda: SimpleNamespace(
                    loc_WRT_ExprIn=lambda frame: Vector(position),
                    vel_WRT_ExprIn=lambda frame: Vector(velocity),
                ),
                getRotation=lambda: SimpleNamespace(Quat_WRT=lambda frame: [0, 0, 0, 1]),
                getAngVelocity=lambda: SimpleNamespace(WRT_ExprIn=lambda frame: [0, 0, 0]),
                GetParam=lambda kind, key: 5 if key[-1] == "FuelUsed" else 0,
            )

        start = datetime.datetime(2026, 11, 16, tzinfo=datetime.UTC)

        def stamp(value):
            return SimpleNamespace(as_datetime=lambda: value)

        shuttle, target = body([0, 0, 0], [2, 0, 0]), body([100, 0, 0], [0, 0, 0])
        fake_sdk = SimpleNamespace(
            VarType=SimpleNamespace(timestamp=0, double=1),
            SimGlobals=SimpleNamespace(
                SimClock=SimpleNamespace(
                    GetTimeNow=lambda: stamp(start + datetime.timedelta(seconds=4))
                ),
                GetSimEntity=lambda: SimpleNamespace(GetParam=lambda *args: stamp(start)),
            ),
            shuttle=shuttle,
            target=target,
            phobos=shuttle,
        )
        fake_numpy = SimpleNamespace(
            linalg=SimpleNamespace(norm=lambda values: sum(x * x for x in values) ** 0.5)
        )
        cases = []
        for directory in ("tom/kalman", "tom/polyfit", "bui"):
            path = BENCHMARK.parents[1] / "controllers" / directory / "Controller.py"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            if not any(isinstance(n, ast.ClassDef) and n.name == "BurnGovernor" for n in tree.body):
                path = path.with_name("avoidance.py")
                tree = ast.parse(path.read_text(encoding="utf-8"))
            governor = next(
                n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BurnGovernor"
            )
            namespace = {}
            exec(
                compile(ast.Module(body=[governor], type_ignores=[]), str(path), "exec"), namespace
            )
            instance = namespace["BurnGovernor"]()
            setattr(instance, "burning" if hasattr(instance, "burning") else "firing", True)
            cases.append((directory, instance, {}, "burn"))
        cases.extend(
            [
                ("mppi", None, {"mode": "coast"}, "coast"),
                ("old_governor", SimpleNamespace(firing=False), {}, "coast"),
                ("missing_optional_diagnostics", object(), {}, None),
            ]
        )
        for name, governor, diagnostic, expected_mode in cases:
            with self.subTest(controller=name), tempfile.TemporaryDirectory() as temporary:
                receiver = benchmark.Receiver()
                original_argv = sys.argv.copy()
                try:
                    folder = Path(temporary)
                    source = folder / "controller.py"
                    source.write_text(
                        "import spaceteams as st\n"
                        "shuttle, target, phobos = st.shuttle, st.target, st.phobos\n"
                        "governor, record = st.governor, st.record\n"
                        "mode = st.policy_phase\n"
                        "def GetNearestDebrisLocations(count):\n"
                        "    return [[[100,0,0],[100,0,1],[4,0,0]]] * st.radar_count\n"
                        "def SetCommand(accelCommand, angAccelCommand):\n    pass\n"
                        "GetNearestDebrisLocations(1)\nSetCommand([0.5,0,0], [0,0,0])\n",
                        encoding="utf-8",
                    )
                    fake_sdk.governor, fake_sdk.record = governor, diagnostic
                    fake_sdk.policy_phase = "avoid" if name == "bui" else None
                    fake_sdk.radar_count = 500 if name == "bui" else 1
                    spec = folder / "spec.json"
                    evidence.write_json(
                        spec,
                        dict(
                            source=str(source),
                            role="controller",
                            port=receiver.port,
                            token=receiver.token,
                            sample_period_s=0.1,
                        ),
                    )
                    with patch.dict(sys.modules, spaceteams=fake_sdk, numpy=fake_numpy):
                        capture.run(spec)
                    records = []
                    while not records or records[-1]["kind"] != "process_finished":
                        records.append(receiver.events.get(timeout=5))
                    self.assertFalse(
                        [r for r in records if r["kind"] in {"sensor_error", "hook_error"}]
                    )
                    telemetry = next(r for r in records if r["kind"] == "telemetry")
                    self.assertEqual(telemetry["mode"], expected_mode)
                    self.assertEqual(telemetry["controller_phase"], fake_sdk.policy_phase)
                    self.assertEqual(len(telemetry["radar_measurements"]), fake_sdk.radar_count)
                    self.assertEqual(telemetry["range_m"], 100)
                    self.assertEqual(telemetry["relative_speed_m_s"], 2)
                    self.assertEqual(telemetry["sim_s"], 4)
                finally:
                    sys.argv[:] = original_argv
                    receiver.close()

    def test_commit_match_checks_all_support_files_and_tolerates_only_line_endings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Controller.py").write_bytes(b"import helper\r\n")
            (root / "helper.py").write_bytes(b"value = 1\n")
            blobs = {
                "controllers/tom/Controller.py": b"import helper\n",
                "controllers/tom/helper.py": b"value = 1\n",
            }

            def read(command, **kwargs):
                if "ls-tree" in command:
                    return SimpleNamespace(stdout="\0".join(blobs).encode() + b"\0")
                return SimpleNamespace(stdout=blobs[command[-1].split(":", 1)[1]])

            files = {name: evidence.digest(root / name) for name in ("Controller.py", "helper.py")}
            with patch.object(evidence.subprocess, "run", side_effect=read):
                match = evidence.commit_match(root, "a" * 40, "controllers/tom", root, files)
                self.assertEqual(match, "line_endings_only")
                (root / "helper.py").write_bytes(b"value = 2\n")
                self.assertEqual(
                    evidence.commit_match(root, "a" * 40, "controllers/tom", root, files),
                    "modified",
                )

    def test_dataset_labels_bind_author_method_and_verified_commit_to_frozen_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "snapshot/controllers/tom"
            snapshot.mkdir(parents=True)
            (snapshot / "Controller.py").write_text("pass\n")
            config = self.config()
            config["controllers"] = [config["controllers"][0]]
            campaign = dict(config=config, mode="pilot", attempts=[])
            provenance = dict(
                repository_revision="a" * 40,
                snapshot_hashes={
                    "controllers/tom/Controller.py": evidence.digest(snapshot / "Controller.py")
                },
            )
            evidence.write_json(root / "provenance.json", provenance)
            with patch.object(evidence, "commit_match", return_value="exact") as matcher:
                identity = evidence.controller_versions(root, campaign)["tom"]
                self.assertEqual(identity["controller_source_commit"], "a" * 40)
                config["controllers"][0].update(
                    source_commit="b" * 40, source_directory="examples/tom"
                )
                pinned = evidence.controller_versions(root, campaign)["tom"]
                self.assertEqual(pinned["controller_source_commit"], "b" * 40)
                self.assertEqual(pinned["controller_checkout_commit"], "a" * 40)
                self.assertEqual(matcher.call_args.args[1:3], ("b" * 40, "examples/tom"))
                config["controllers"][0].pop("source_commit")
                config["controllers"][0].pop("source_directory")
                evidence.write_json(
                    root / "dataset_labels.json",
                    dict(
                        tom=dict(
                            author="Tom Kirkpatrick",
                            method="Kalman",
                            source_bundle_sha256=identity["controller_source_sha256"],
                        )
                    ),
                )
                labelled = evidence.controller_versions(root, campaign)["tom"]
                self.assertIn(
                    "Tom Kirkpatrick — Kalman — aaaaaaaaaaaa", labelled["controller_label"]
                )
                self.assertEqual(labelled["controller_author"], "Tom Kirkpatrick")
            with patch.object(evidence, "commit_match", return_value="modified"):
                modified = evidence.controller_versions(root, campaign)["tom"]
                self.assertIsNone(modified["controller_source_commit"])
                self.assertEqual(modified["controller_checkout_commit"], "a" * 40)
                self.assertIn("unverified commit", modified["controller_label"])
            provenance["snapshot_hashes"]["controllers/tom/Controller.py"] = "b" * 64
            evidence.write_json(root / "provenance.json", provenance)
            with self.assertRaisesRegex(ValueError, "does not match frozen source bundle"):
                evidence.controller_versions(root, campaign)

    def test_read_only_status_counts_terminal_attempts_and_shows_active_telemetry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = dict(
                mode="seeded",
                attempts=[
                    dict(run_id="finished", controller=dict(id="tom"), seed=100, repetition=0),
                    dict(run_id="active", controller=dict(id="bui"), seed=100, repetition=0),
                    dict(run_id="unstarted", controller=dict(id="tom"), seed=1729, repetition=0),
                ],
            )
            evidence.write_json(root / "campaign.json", campaign)
            for run_id, outcome in (("finished", "success"), ("active", "in_progress")):
                (root / run_id).mkdir()
                evidence.write_json(root / run_id / "receipt.json", dict(outcome=outcome))
            (root / "active" / "events.jsonl").write_text(
                '{"kind":"telemetry","sim_s":12,"range_m":1234}\n'
            )
            before = (root / "campaign.json").read_bytes()
            result = benchmark.campaign_status(root)["campaigns"][0]
            self.assertEqual(result["completed"], 1)
            self.assertEqual(result["remaining"], 2)
            self.assertEqual(result["outcomes"], {"success": 1})
            self.assertEqual(result["active"]["controller"], "bui")
            self.assertEqual(result["active"]["telemetry"]["range_m"], 1234)
            self.assertEqual((root / "campaign.json").read_bytes(), before)

    def test_unattended_continuation_requires_controller_exception_and_verified_cleanup(self):
        receipt = dict(
            outcome="exception", cleanup="owned_process_tree_exited", error=dict(role="controller")
        )
        self.assertTrue(benchmark.must_halt(receipt))
        self.assertFalse(benchmark.must_halt(receipt, continue_controller_errors=True))
        for role in ("actuator", "scorer", "radar", "spawner", None):
            self.assertTrue(benchmark.must_halt(dict(receipt, error=dict(role=role)), True))
        self.assertTrue(benchmark.must_halt(dict(receipt, cleanup_errors=["failed"]), True))
        self.assertTrue(benchmark.must_halt(dict(receipt, cleanup="failed_or_incomplete"), True))
        for outcome in ("evidence_failure", "controller_stall", "simulator_stall", "aborted"):
            self.assertTrue(benchmark.must_halt(dict(receipt, outcome=outcome), True))
        for outcome in ("success", "failure", "simulation_limit", "wall_timeout"):
            self.assertFalse(benchmark.must_halt(dict(receipt, outcome=outcome), True))

    def test_queue_stops_before_next_campaign_after_infrastructure_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaigns = [root / "baseline", root / "seeded"]
            status_path = root / "status.json"
            with (
                patch.object(benchmark, "keep_awake") as awake,
                patch.object(benchmark, "run_campaign", return_value=False) as run,
            ):
                self.assertFalse(
                    benchmark.run_queue(
                        campaigns, continue_controller_errors=True, status_file=status_path
                    )
                )
                run.assert_called_once_with(campaigns[0], None, True, resume=False)
                awake.return_value.__exit__.assert_called_once()
            status = json.loads(status_path.read_text())
            self.assertEqual(status["state"], "halted")
            self.assertEqual(status["finished_campaigns"], [])

    def test_queue_finishes_both_campaigns_and_saves_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaigns = [root / "baseline", root / "seeded"]
            status_path = root / "status.json"
            with (
                patch.object(benchmark, "keep_awake"),
                patch.object(benchmark, "run_campaign", return_value=True) as run,
            ):
                self.assertTrue(benchmark.run_queue(campaigns, status_file=status_path))
                self.assertEqual([call.args[0] for call in run.call_args_list], campaigns)
            status = json.loads(status_path.read_text())
            self.assertEqual(status["state"], "finished")
            self.assertEqual(len(status["finished_campaigns"]), 2)

    def config(self):
        config = json.loads((BENCHMARK / "campaign.json").read_text())
        config["controllers"] = [c for c in config["controllers"] if c["id"] in {"tom", "ali_mppi"}]
        for controller in config["controllers"]:
            for field in ("source_commit", "source_directory", "source_changes"):
                controller.pop(field, None)
        # Retain the historical two-pass fixtures for backward compatibility checks.
        config.pop("repetitions", None)
        bank = json.loads((BENCHMARK / "seed_bank.json").read_text())
        config["seeds"] = bank["seeds"][: config["seed_count"]]
        return config

    def test_current_campaign_is_one_pass_with_matched_controller_seed_coverage(self):
        config = json.loads((BENCHMARK / "campaign.json").read_text())
        config["seeds"] = self.config()["seeds"]
        runs = evidence.schedule(config, "seeded")
        self.assertEqual(config["repetitions"], 1)
        self.assertEqual(len(runs), 88)
        self.assertEqual({r["repetition"] for r in runs}, {0})
        for controller in config["controllers"]:
            self.assertTrue(
                (
                    BENCHMARK.parents[1] / controller["directory"] / controller["entrypoint"]
                ).is_file(),
                f"Missing entrypoint for {controller['id']}",
            )
        for block, seed in enumerate(config["seeds"]):
            group = [r for r in runs if r["block"] == block]
            self.assertEqual({r["seed"] for r in group}, {seed})
            self.assertCountEqual(
                [r["controller"]["id"] for r in group], ["tom", "tom_polyfit", "ali_mppi", "bui"]
            )
            self.assertEqual(group[0]["controller"]["id"], config["controllers"][block % 4]["id"])

    def test_invalid_repetitions_are_rejected(self):
        for value in (0, -1, True, 1.5, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                evidence.schedule(dict(self.config(), repetitions=value), "seeded")

    def test_scenario_analysis_requires_all_configured_flights_and_retains_timeouts(self):
        for repetitions in (1, 2):
            with self.subTest(repetitions=repetitions), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = dict(self.config(), repetitions=repetitions, seed_count=1, seeds=[100])
                attempts = evidence.schedule(config, "seeded")
                for i, attempt in enumerate(attempts):
                    attempt["run_id"] = f"run-{i}"
                    folder = root / attempt["run_id"]
                    folder.mkdir()
                    evidence.write_json(
                        folder / "receipt.json",
                        dict(
                            outcome="success",
                            official_score=10 if attempt["controller"]["id"] == "tom" else 15,
                            wall_s=300,
                            evidence_bytes=100,
                        ),
                    )
                evidence.write_json(
                    root / "campaign.json", dict(mode="seeded", config=config, attempts=attempts)
                )
                summary = evidence.analyze(root)
                self.assertEqual(summary["tom"]["scheduled"], repetitions)

                def comparison(root=root):
                    with (root / "scenario_comparisons.csv").open(newline="") as stream:
                        return next(csv.DictReader(stream))

                self.assertEqual(comparison()["complete_pair"], "True")
                self.assertEqual(float(comparison()["mean_score_delta"]), 5)
                cost = json.loads((root / "cost_estimate.json").read_text())
                self.assertEqual(cost["seeded_bytes"], 2 * repetitions * 100)
                self.assertAlmostEqual(cost["seeded_wall_hours"], 2 * repetitions * 300 / 3600)

                # A missing score must not be imputed or form a complete scenario.
                candidate = next(a for a in attempts if a["controller"]["id"] == "ali_mppi")
                folder = root / candidate["run_id"]
                evidence.write_json(
                    folder / "receipt.json", dict(outcome="simulation_limit", official_score=None)
                )
                (folder / "events.jsonl").write_text(
                    '{"kind":"telemetry","sim_s":300,"range_m":123,"relative_speed_m_s":2.5}\n'
                )
                summary = evidence.analyze(root)
                self.assertEqual(summary["ali_mppi"]["outcomes"]["simulation_limit"], 1)
                self.assertEqual(summary["ali_mppi"]["distributions"]["range_m"]["values"], [123])
                with (root / "attempts.csv").open(newline="") as stream:
                    reading = next(
                        row
                        for row in csv.DictReader(stream)
                        if row["run_id"] == candidate["run_id"]
                    )
                self.assertEqual(float(reading["last_telemetry_sim_s"]), 300)
                self.assertEqual(float(reading["last_relative_speed_m_s"]), 2.5)
                self.assertEqual(comparison()["complete_pair"], "False")
                self.assertEqual(comparison()["mean_score_delta"], "")

    def test_resume_keeps_started_attempts_and_only_schedules_unstarted_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempts = [dict(run_id=name) for name in ("success", "aborted", "partial", "next")]
            campaign = dict(config=self.config(), attempts=attempts)
            for name, outcome in (
                ("success", "success"),
                ("aborted", "aborted"),
                ("partial", "in_progress"),
            ):
                (root / name).mkdir()
                evidence.write_json(root / name / "receipt.json", dict(outcome=outcome))
            successful = (root / "success" / "receipt.json").read_bytes()
            with self.assertRaises(ValueError):
                benchmark.pending_attempts(root, campaign)
            self.assertEqual(
                benchmark.pending_attempts(root, campaign, resume=True), [attempts[-1]]
            )
            self.assertEqual((root / "success" / "receipt.json").read_bytes(), successful)
            partial = json.loads((root / "partial" / "receipt.json").read_text())
            self.assertEqual(partial["outcome"], "interrupted")
            self.assertIsNone(partial["official_score"])
            self.assertTrue((root / "partial" / "receipt.before_resume.json").exists())

    def test_resume_recovers_already_observed_official_score(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "partial").mkdir()
            evidence.write_json(root / "partial/receipt.json", dict(outcome="in_progress"))
            (root / "partial/events.jsonl").write_text(
                json.dumps(
                    dict(
                        kind="official_receipt",
                        outcome="success",
                        official_score=12.3,
                        inputs=dict(fuelUsed_kg=48),
                        termination_reason="official_arrival",
                    )
                )
                + "\n"
            )
            campaign = dict(config=self.config(), attempts=[dict(run_id="partial")])
            self.assertEqual(benchmark.pending_attempts(root, campaign, resume=True), [])
            receipt = json.loads((root / "partial/receipt.json").read_text())
            self.assertEqual(receipt["official_score"], 12.3)
            self.assertEqual(receipt["inputs"]["fuelUsed_kg"], 48)

    def test_stop_marks_every_queued_campaign(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaigns = [root / "baseline", root / "seeded"]
            for campaign in campaigns:
                campaign.mkdir()
                evidence.write_json(campaign / "campaign.json", {})
            evidence.write_json(
                root / "queue_status.json", dict(campaigns=[str(p) for p in campaigns])
            )
            benchmark.request_stop(root)
            self.assertTrue(all((p / "STOP").exists() for p in campaigns))

    def bank_fixture(self, root):
        bank = json.loads((BENCHMARK / "seed_bank.json").read_text())
        location = root / "bank.json"
        evidence.write_json(location, bank)
        return location, bank

    def test_seed_bank_reuses_prefix_and_extends_once_for_every_controller(self):
        config = self.config()
        with tempfile.TemporaryDirectory() as temporary:
            location, original = self.bank_fixture(Path(temporary))
            self.assertEqual(original["seeds"][0], 100)
            before = location.read_bytes()
            first, _ = seeds.select_seeds(location, config, 20)
            self.assertEqual(first["seeds"], original["seeds"][:20])
            self.assertEqual(location.read_bytes(), before)
            self.assertEqual(first["appended_count"], 0)
            target = len(original["seeds"]) + 9
            expanded, bank = seeds.select_seeds(location, config, target)
            self.assertEqual(bank["seeds"][: len(original["seeds"])], original["seeds"])
            self.assertEqual(expanded["seeds"][:20], first["seeds"])
            self.assertEqual(len(set(expanded["seeds"])), target)
            self.assertEqual(expanded["appended_count"], 9)
            frozen = json.dumps(first)
            repeat, _ = seeds.select_seeds(location, config, target)
            self.assertEqual(repeat["seeds"], expanded["seeds"])
            self.assertEqual(repeat["bank_sha256"], expanded["bank_sha256"])
            self.assertEqual(repeat["appended_count"], 0)
            self.assertEqual(json.dumps(first), frozen)
            config.update(seed_count=target, seeds=expanded["seeds"])
            runs = evidence.schedule(config, "seeded")
            for controller in config["controllers"]:
                values = [r["seed"] for r in runs if r["controller"]["id"] == controller["id"]]
                self.assertEqual(values, expanded["seeds"] + expanded["seeds"])

    def test_seed_extension_is_stable_across_incremental_preparations(self):
        config = self.config()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            direct, original = self.bank_fixture(root)
            base_count = len(original["seeds"])
            incremental = root / "incremental.json"
            incremental.write_bytes(direct.read_bytes())
            full, _ = seeds.select_seeds(direct, config, base_count + 9)
            seeds.select_seeds(incremental, config, base_count + 3)
            stages, _ = seeds.select_seeds(incremental, config, base_count + 9)
            self.assertEqual(full["seeds"], stages["seeds"])

    def test_seed_bank_rejects_missing_duplicate_reordered_and_changed_scenarios(self):
        config = self.config()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(FileNotFoundError):
                seeds.select_seeds(root / "missing.json", config, 20)
            location, original = self.bank_fixture(root)
            for invalid in ([], [True], [-1], [2**32], [100, 100]):
                bank = dict(original, seeds=invalid)
                evidence.write_json(location, bank)
                with self.assertRaises(ValueError):
                    seeds.select_seeds(location, config, 20)
            bank = dict(original, seeds=list(reversed(original["seeds"])))
            evidence.write_json(location, bank)
            with self.assertRaises(ValueError):
                seeds.select_seeds(location, config, 20)
            evidence.write_json(location, original)
            changed = dict(config, mission_sha256="another-mission")
            with self.assertRaises(ValueError):
                seeds.select_seeds(location, changed, 20)
            for invalid_count in (0, -1, True, 1.5):
                with self.assertRaises(ValueError):
                    seeds.select_seeds(location, config, invalid_count)

    def test_saved_seed_extensions_cannot_be_replaced_or_reordered(self):
        config = self.config()
        with tempfile.TemporaryDirectory() as temporary:
            location, original = self.bank_fixture(Path(temporary))
            _, expanded = seeds.select_seeds(location, config, len(original["seeds"]) + 2)
            expanded["seeds"][-2:] = expanded["seeds"][-2:][::-1]
            evidence.write_json(location, expanded)
            with self.assertRaises(ValueError):
                seeds.select_seeds(location, config, len(expanded["seeds"]))

    def test_concurrent_seed_bank_update_fails_without_replacing_history(self):
        config = self.config()
        with tempfile.TemporaryDirectory() as temporary:
            location, original = self.bank_fixture(Path(temporary))
            before = location.read_bytes()
            with seeds.bank_lock(location), self.assertRaises(OSError):
                seeds.select_seeds(location, config, len(original["seeds"]) + 1)
            self.assertEqual(location.read_bytes(), before)

    def test_pairs_rotate_without_changing_matching_or_repetitions(self):
        config = self.config()
        config["execution_order"] = "paired"
        runs = evidence.schedule(config, "seeded")
        n = len(config["controllers"])
        self.assertEqual(len(runs), n * config["seed_count"] * 2)
        for block in range(config["seed_count"]):
            group = runs[block * n * 2 : (block + 1) * n * 2]
            self.assertEqual(group[0]["controller"]["id"], config["controllers"][block % n]["id"])
            self.assertEqual({r["seed"] for r in group}, {config["seeds"][block]})
            for i in range(0, len(group), 2):
                self.assertEqual(group[i]["controller"], group[i + 1]["controller"])
                self.assertEqual([group[i]["repetition"], group[i + 1]["repetition"]], [0, 1])
        baseline = evidence.schedule(config, "baseline")
        self.assertEqual(len(baseline), 10 * n)
        self.assertTrue(all(r["seed"] is None for r in baseline))

    def test_coverage_first_visits_all_controller_seed_combinations_before_repeats(self):
        config = self.config()
        runs = evidence.schedule(config, "seeded")
        n = len(config["controllers"])
        pass_size = config["seed_count"] * n
        self.assertEqual({r["repetition"] for r in runs[:pass_size]}, {0})
        self.assertEqual({r["repetition"] for r in runs[pass_size:]}, {1})
        self.assertEqual(
            [r["controller"]["id"] for r in runs[:n]], [c["id"] for c in config["controllers"]]
        )
        self.assertEqual({r["seed"] for r in runs[:n]}, {100})
        for block in range(config["seed_count"]):
            for repetition in range(2):
                group = runs[
                    repetition * pass_size + block * n : repetition * pass_size + (block + 1) * n
                ]
                self.assertEqual(len({r["controller"]["id"] for r in group}), n)
                self.assertEqual({r["seed"] for r in group}, {config["seeds"][block]})
                self.assertEqual(
                    group[0]["controller"]["id"], config["controllers"][block % n]["id"]
                )
        paired = evidence.order_attempts(config, runs, "paired")
        self.assertCountEqual(
            [(r["block"], r["repetition"], r["controller"]["id"]) for r in paired],
            [(r["block"], r["repetition"], r["controller"]["id"]) for r in runs],
        )

    def test_duplicate_seed_rejected(self):
        config = self.config()
        config["seeds"][-1] = config["seeds"][0]
        with self.assertRaises(ValueError):
            evidence.schedule(config, "seeded")

    def test_independent_score_matches_reviewed_formula(self):
        inputs = dict(
            time_s=200,
            nominalTime_s=120,
            damageTaken_J=20920,
            maxDamage_J=209200,
            fuelUsed_kg=90,
            nominalFuel_kg=75,
            distanceToShip=500,
            initial_distanceToShip=1000,
        )
        win = evidence.score_components(dict(outcome="success", inputs=inputs))
        self.assertAlmostEqual(win["calculated_score"], -30)
        loss = evidence.score_components(dict(outcome="failure", inputs=inputs))
        self.assertAlmostEqual(loss["calculated_score"], -140)
        inputs["time_s"] = 0
        self.assertAlmostEqual(
            evidence.score_components(dict(outcome="success", inputs=inputs))["time_points"], 0
        )
        self.assertIsNone(evidence.score_components(dict(outcome="wall_timeout", inputs=inputs)))

    def test_score_reconciles_observed_official_11_november_receipt(self):
        # Original native scorer return in pilot dfdb53bb, not a practice-model score.
        receipt = dict(
            outcome="success",
            inputs=dict(
                time_s=166.40587000000002,
                nominalTime_s=120,
                damageTaken_J=0,
                maxDamage_J=209200,
                fuelUsed_kg=103.36057751696077,
                nominalFuel_kg=75,
            ),
        )
        self.assertAlmostEqual(
            evidence.score_components(receipt)["calculated_score"], -15.70126262502879
        )

    def test_report_retains_missing_and_never_promotes_repeated_runs_to_scenarios(self):
        config = self.config()
        config["controllers"] = config["controllers"][:2]
        config["seed_count"] = 30
        attempts = evidence.schedule(config, "pilot")
        for i, run in enumerate(attempts):
            run["run_id"] = f"run-{i}"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence.write_json(
                root / "campaign.json", dict(mode="pilot", config=config, attempts=attempts)
            )
            folder = root / "run-0"
            folder.mkdir()
            evidence.write_json(
                folder / "receipt.json",
                dict(
                    outcome="wall_timeout",
                    official_score=None,
                    inputs=None,
                    wall_s=60,
                    evidence_bytes=100,
                ),
            )
            (folder / "events.jsonl").write_text(
                '{"kind":"telemetry","seq":0,"damage_J":0}\n'
                '{"kind":"telemetry","seq":1,"damage_J":1}\n{"broken"'
            )
            summary = evidence.analyze(root)["tom"]
            self.assertEqual(summary["outcomes"], {"wall_timeout": 1, "not_started": 1})
            self.assertEqual(summary["scored_coverage"], 0)
            self.assertEqual(summary["distributions"]["official_score"]["n"], 0)
            self.assertIsNone(summary["native_collision_incidence"])
            incidents = json.loads((folder / "incidents.json").read_text())
            self.assertIn("damage_increase", {i["kind"] for i in incidents})
            self.assertTrue(all(i["suspected_cause"] is None for i in incidents))
            cost = json.loads((root / "cost_estimate.json").read_text())
            self.assertEqual(cost["seeded_scenarios"], 30)
            self.assertEqual(cost["seeded_bytes"], 12000)

    def test_missing_score_is_unavailable_even_with_complete_inputs(self):
        self.assertIsNone(evidence.score_components(dict(outcome="failed_start")))
        self.assertEqual(evidence.distribution([None, 0])["n"], 1)

    def test_native_score_capture_precedes_later_counter_changes(self):
        receiver = benchmark.Receiver()
        original_argv = sys.argv.copy()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                folder = Path(temporary)
                source = folder / "original.py"
                source.write_text(
                    "import sys\n"
                    "assert sys.getprofile() is None\n"
                    "assert sys.monitoring.get_events(3) == 0\n"
                    "sim_elapsed_time = 123.5\n"
                    "distanceToShip = 49.5\n"
                    "initial_distanceToShip = 1310\n"
                    "def FullScoreWin(*args):\n    return 72.25\n"
                    "def YouWon(time_s, nominalTime_s, damageTaken_J, maxDamage_J, "
                    "fuelUsed_kg, nominalFuel_kg):\n"
                    "    score = FullScoreWin(time_s, nominalTime_s, damageTaken_J, "
                    "maxDamage_J, fuelUsed_kg, nominalFuel_kg)\n"
                    "YouWon(123.5, 120, 0, 209200, 48, 75)\n"
                    "sim_elapsed_time = 9999\n",
                    encoding="utf-8",
                )
                spec_path = folder / "spec.json"
                evidence.write_json(
                    spec_path,
                    dict(
                        source=str(source),
                        role="scorer",
                        port=receiver.port,
                        token=receiver.token,
                        sample_period_s=0.1,
                    ),
                )
                capture.run(spec_path)
                records = []
                while not records or records[-1]["kind"] != "process_finished":
                    records.append(receiver.events.get(timeout=5))
                receipts = [r for r in records if r["kind"] == "official_receipt"]
                self.assertEqual(len(receipts), 1)
                self.assertEqual(receipts[0]["official_score"], 72.25)
                self.assertEqual(receipts[0]["inputs"]["fuelUsed_kg"], 48)
                self.assertEqual(receipts[0]["scorer_time_s"], 123.5)
                self.assertEqual(receipts[0]["range_m"], 49.5)
        finally:
            sys.argv[:] = original_argv
            receiver.close()

    def test_delivery_requires_native_readback_and_impulse_not_just_writer_calls(self):
        writer = dict(kind="command_written", seq=1, monotonic_s=10, linear_body_m_s2=[0.5, 0, 0])
        self.assertFalse(evidence.delivery_summary([writer])["command_delivery_verified"])
        actuator = dict(
            kind="actuator_applied",
            seq=2,
            received=dict(linear_body_m_s2=[0.5, 0, 0], linear_read_monotonic_s=10.01),
            linear_impulse_body_kg_m_s=[1, 0, 0],
            position_resident_m=[0, 0, 0],
            velocity_resident_m_s=[0, 0, 0],
        )
        result = evidence.delivery_summary([writer, actuator])
        self.assertTrue(result["command_delivery_verified"])
        self.assertFalse(result["simulator_motion_observed"])
        moved = dict(
            actuator, seq=3, position_resident_m=[1, 0, 0], velocity_resident_m_s=[0.1, 0, 0]
        )
        result = evidence.delivery_summary([writer, actuator, moved])
        self.assertTrue(result["simulator_motion_observed"])
        wrong = dict(
            actuator, received=dict(linear_body_m_s2=[0, 0.5, 0], linear_read_monotonic_s=10.01)
        )
        self.assertFalse(evidence.delivery_summary([writer, wrong])["command_delivery_verified"])

    @unittest.skipUnless(os.name == "nt", "Native Windows job test")
    def test_cleanup_owns_and_terminates_descendants(self):
        job = benchmark.WindowsJob()
        process = None
        try:
            child = "import threading; threading.Event().wait()"
            parent = (
                "import subprocess,sys,threading; "
                f"p=subprocess.Popen([sys.executable,'-c',{child!r}]); "
                "print(p.pid,flush=True); threading.Event().wait()"
            )
            process = job.launch(
                [sys.executable, "-c", parent], str(BENCHMARK), os.environ.copy(), subprocess.PIPE
            )
            self.assertTrue(process.stdout.readline().strip().isdigit())
            self.assertGreaterEqual(job.active(), 2)
            job.close()  # waits for actual job completion, including child
            process.wait(timeout=5)
            self.assertIsNotNone(process.returncode)
        finally:
            job.close()
            if process:
                process.stdout.close()

    def test_seed_copy_is_separate_and_refuses_unknown_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original, seeded = root / "original.py", root / "seeded.py"
            original.write_text("seed = 8193\nrng = np.random.default_rng(seed)\n")
            expected = original.read_bytes()
            profile = dict(native_seed=8193, sha256=evidence.digest(original))
            benchmark.seed_copy(original, seeded, 1729, "spawner", profile)
            self.assertEqual(original.read_bytes(), expected)
            self.assertIn("seed = 1729", seeded.read_text())
            original.write_text("seed = 999\n")
            with self.assertRaises(ValueError):
                benchmark.seed_copy(original, seeded, 1729, "spawner", profile)

    def test_seed_adapter_matches_actual_pinned_competition_sources(self):
        package = benchmark.path(self.config()["package"])
        if not package.is_dir():
            self.skipTest("Pinned official STP package is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            for role, name in (("spawner", "SpawnDebris.py"), ("radar", "Radar.py")):
                original = package / "Systems" / name
                target = Path(temporary) / name
                self.assertEqual(
                    evidence.digest(original), benchmark.seed_adapter()["systems"][role]["sha256"]
                )
                if role == "spawner":
                    benchmark.seed_copy(original, target, 100, role)
                    self.assertIn("seed = 100", target.read_text())
                else:
                    with self.assertRaises(ValueError):
                        benchmark.seed_copy(original, target, 1729, role)

    def test_seeded_mission_preserves_original_radar_and_changes_only_spawner_seed(self):
        config = self.config()
        package = benchmark.path(config["package"])
        if not package.is_dir():
            self.skipTest("Pinned official STP package is not installed")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            snapshot = root / "snapshot"
            snapshot.mkdir()
            shutil.copytree(package, snapshot / "STU4-Sim")
            controller = config["controllers"][0]
            shutil.copytree(
                benchmark.path(controller["directory"]), snapshot / "controllers" / controller["id"]
            )
            attempt = dict(run_id="seeded", controller=controller, seed=1729)
            (root / "seeded").mkdir()
            receiver = SimpleNamespace(port=1234, token="test-only")
            _, changes = benchmark.attempt_mission(root, attempt, receiver, config)
            radar = json.loads((root / "seeded/launchers/radar.json").read_text())
            self.assertEqual(Path(radar["source"]), snapshot / "STU4-Sim/Systems/Radar.py")
            self.assertEqual(
                evidence.digest(radar["source"]),
                benchmark.seed_adapter()["systems"]["radar"]["sha256"],
            )
            self.assertFalse((root / "seeded/launchers/seeded_radar.py").exists())
            spawner = root / "seeded/launchers/seeded_spawner.py"
            self.assertIn("seed = 1729", spawner.read_text())
            self.assertEqual([c["role"] for c in changes if "seeded_sha256" in c], ["spawner"])


class EvidenceRetryTests(unittest.TestCase):
    def receipt(self, outcome="evidence_failure", **values):
        return dict(outcome=outcome, cleanup="owned_process_tree_exited", **values)

    def test_only_clean_unscored_evidence_failures_are_retryable(self):
        self.assertTrue(evidence.retryable_evidence_failure(self.receipt()))
        for outcome in ("success", "exception", "simulator_crash", "startup_timeout", "aborted"):
            self.assertFalse(evidence.retryable_evidence_failure(self.receipt(outcome)))
        for patch_value in (
            {"cleanup": "failed"},
            {"cleanup_errors": ["error"]},
            {"official_score": 0},
            {"inputs": {"time_s": 1}},
        ):
            self.assertFalse(
                evidence.retryable_evidence_failure(dict(self.receipt(), **patch_value))
            )

    def exercise(self, outcomes, stop=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        attempt = dict(run_id="logical", controller={"id": "ali"}, seed=100, repetition=0)
        calls = []

        def execute(folder, physical):
            calls.append(dict(physical))
            destination = folder / physical["run_id"]
            destination.mkdir(exist_ok=False)
            result = self.receipt(outcomes[len(calls) - 1])
            evidence.write_json(destination / "receipt.json", result)
            (destination / "events.jsonl").write_text("original evidence")
            if stop:
                (folder / "STOP").touch()
            return result

        with patch.object(benchmark, "execute", side_effect=execute):
            result = benchmark.execute_with_evidence_retries(root, attempt, {"state": "running"})
        return root, calls, result

    def test_retries_same_conditions_and_preserves_original(self):
        root, calls, result = self.exercise(["evidence_failure", "evidence_failure", "success"])
        self.assertEqual(result["outcome"], "success")
        self.assertEqual(
            [a["run_id"] for a in calls], ["logical", "logical-retry-1", "logical-retry-2"]
        )
        self.assertTrue(all(a["seed"] == 100 and a["controller"] == {"id": "ali"} for a in calls))
        history = evidence.attempt_history(root, "logical")
        self.assertEqual(
            [r["outcome"] for _, _, r in history], ["evidence_failure"] * 2 + ["success"]
        )
        self.assertEqual((root / "logical/events.jsonl").read_text(), "original evidence")

    def test_three_retries_are_a_persistent_limit(self):
        root, calls, result = self.exercise(["evidence_failure"] * 4)
        self.assertEqual(len(calls), 4)
        self.assertTrue(benchmark.must_halt(result, True))
        self.assertEqual(
            benchmark.pending_attempts(root, {"attempts": [{"run_id": "logical"}]}, resume=True), []
        )

    def test_resume_failed_run_uses_next_physical_id_only(self):
        root, calls, _ = self.exercise(["evidence_failure"], stop=True)
        campaign = {"attempts": [calls[0], {"run_id": "unstarted"}]}
        self.assertEqual(benchmark.pending_attempts(root, campaign), [{"run_id": "unstarted"}])
        pending = benchmark.pending_attempts(root, campaign, resume=True)
        self.assertEqual(pending[0]["run_id"], "logical-retry-1")
        self.assertEqual(pending[0]["logical_run_id"], "logical")
        self.assertEqual(pending[1]["run_id"], "unstarted")

    def test_stop_and_other_outcomes_do_not_retry(self):
        _, calls, _ = self.exercise(["evidence_failure"], stop=True)
        self.assertEqual(len(calls), 1)
        _, calls, _ = self.exercise(["exception"])
        self.assertEqual(len(calls), 1)

    def test_illegal_history_is_rejected(self):
        root, _, _ = self.exercise(["success"])
        (root / "logical-retry-1").mkdir()
        with self.assertRaises(ValueError):
            evidence.attempt_history(root, "logical")

    def test_status_and_analysis_use_one_logical_run_with_retry_provenance(self):
        root, calls, _ = self.exercise(["evidence_failure", "success"])
        campaign = dict(
            mode="seeded",
            config=dict(
                reference="ali",
                controllers=[dict(id="ali", directory="controllers/ali")],
                thresholds={},
            ),
            attempts=[dict(calls[0], block=0)],
        )
        evidence.write_json(root / "campaign.json", campaign)
        status = benchmark.campaign_status(root)["campaigns"][0]
        self.assertEqual(status["scheduled"], 1)
        self.assertEqual(status["completed"], 1)
        self.assertEqual(status["remaining"], 0)
        self.assertEqual(status["evidence_retries"], 1)
        # Replace fake event payload with an empty stream for analysis.
        (root / "logical-retry-1/events.jsonl").write_text("")
        with patch.object(evidence, "controller_versions", return_value={"ali": {}}):
            rows = evidence.campaign_rows(root, write_derived=False)[2]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "success")
        self.assertEqual(rows[0]["physical_run_id"], "logical-retry-1")
        self.assertEqual(json.loads(rows[0]["prior_outcomes"]), ["evidence_failure"])


if __name__ == "__main__":
    unittest.main()
