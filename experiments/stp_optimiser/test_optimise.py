"""Focused adapter checks. Synthetic receipts below are fixtures, never flight claims."""

from __future__ import annotations

import ast
import copy
import importlib.metadata
import json
import os
import shutil
import sys
import tempfile
import unittest
from collections import deque
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import optimise as opt


def controller_namespace(name):
    spec = opt.SPACES[name]
    source = (opt.REPO / spec["directory"] / spec["entrypoint"]).read_text()
    tree = ast.parse(source)
    ns = dict(np=np, os=os, json=json, deque=deque)
    if name == "ali":
        body = source.split("# BEGIN ALI MPPI CORE", 1)[1].split("# END ALI MPPI CORE", 1)[0]
        exec(compile(body, "ali-core", "exec"), ns)
        nodes = [
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "native_tuning_settings"
        ]
    else:
        names = {
            "Rock",
            "Tracker",
            "Avoider",
            "GoalPlanner",
            "BurnGovernor",
            "clearance",
            "two_sideways_directions",
            "native_tuning_settings",
            "cone_angle",
        }
        nodes = [
            n
            for n in tree.body
            if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name in names
        ]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "native-input", "exec"), ns)
    return ns, tree


def native_fixture(native, install, prepared_config=None):
    """Small native-provenance fixture; no SDK or native process is involved."""
    snapshot = native / "snapshot"
    snapshot.mkdir(parents=True, exist_ok=True)
    asset = snapshot / "asset.txt"
    asset.write_text("fixed asset")
    install.mkdir(parents=True, exist_ok=True)
    binary = install / "native.exe"
    if not binary.exists():
        binary.write_text("fixed binary")
    release = install / "ReleaseVersion_0.54.2_Win64_User.txt"
    if not release.exists():
        release.write_text("fixed release")
    config = dict(
        install=str(install),
        limits={"simulation_s": 300},
        seed_scope="saved_field_impulses_radar_fixed_100",
        sample_period_s=0.1,
        diagnostic_rerun=False,
        thresholds={},
        repetitions=1,
    )
    if prepared_config is not None:
        config = prepared_config
    provenance = dict(
        configuration=config,
        mission_sha256="mission",
        scorer_sha256="scorer",
        package_revision="package",
        release=release.read_text(),
        binaries={str(binary): opt.evidence.digest(binary)},
        python=sys.version,
        python_dependencies={
            d.metadata["Name"]: d.version for d in importlib.metadata.distributions()
        },
        machine={"hostname": "fixture"},
        scheduling="normal",
        dependencies={str(asset): {"sha256": opt.evidence.digest(asset)}},
        snapshot_hashes=opt.benchmark.hashes(snapshot),
    )
    opt.write(native / "provenance.json", provenance)
    opt.write(native / "campaign.json", dict(config=config, attempts=[]))
    return provenance, config


def runtime_fixture(repo):
    root = repo / "runs/active"
    native = root / "baseline/native"
    provenance, config = native_fixture(native, repo / "install")
    opt.write(root / "experiment.json", dict(native_config=config, final_test=[100]))
    bank = dict(seeds=[1], scenario={})
    opt.write(root / "optimisation-bank.json", bank)
    opt.write(root / "final-bank.json", dict(seeds=[100], scenario={}))
    opt.write(
        root / "runtime-pin.json",
        dict(
            provenance_sha256=opt.evidence.digest(native / "provenance.json"),
            signature=opt.runtime_signature(native, provenance),
        ),
    )
    return root, native


class ControllerInputs(unittest.TestCase):
    def test_ali_settings_enter_native_state_and_active_mppi_plan(self):
        ns, tree = controller_namespace("ali")
        supplied = opt.settings(
            "ali", {"cruise_speed_mps": 13.0, "plan_horizon_s": 38.0, "risk_weight": 1500.0}
        )
        with patch.dict(os.environ, {"WSORB_STP_TUNING": json.dumps(supplied)}):
            state_node = next(
                n
                for n in tree.body
                if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "state" for t in n.targets)
            )
            exec(
                compile(ast.Module(body=[state_node], type_ignores=[]), "native-state", "exec"), ns
            )
        state = ns["state"]
        self.assertEqual(
            state["policy"], supplied | {"time_budget_s": 300.0, "arrival_radius_m": 50.0}
        )
        state["launching"] = False
        obs = dict(
            position=np.array([2000.0, 0.0, 0.0]),
            velocity=np.array([0.0, 8.0, 0.0]),
            target=np.array([2000.0, 1000.0, 0.0]),
            body_x=np.array([0.0, 1.0, 0.0]),
            thrust_axis=np.array([0.0, 1.0, 0.0]),
            angular_velocity=np.zeros(3),
            fuel=0.0,
            damage=0.0,
            radius=3.0,
            max_accel=0.5,
            max_angular=0.05,
            mass=3388.7,
        )
        seen = []
        original = ns["plan"]

        def tracked(*args):
            seen.append(copy.deepcopy(args[0]["policy"]))
            return original(*args)

        ns["plan"] = tracked
        for now in (1.0, 1.1):
            acceleration, angular, record = ns["step"](state, obs, [], now, 0.1)
            self.assertTrue(np.isfinite(acceleration).all())
            self.assertTrue(np.isfinite(angular).all())
            self.assertEqual(record["avoidance"], "mppi")
        self.assertTrue(seen, "native state must reach the active MPPI planner")
        self.assertEqual(seen[-1]["plan_horizon_s"], 38.0)
        self.assertEqual(seen[-1]["risk_weight"], 1500.0)
        self.assertEqual(state["cruise_speed_mps"], 13.0)
        with patch.dict(os.environ, {"WSORB_STP_TUNING": "{}"}):
            self.assertEqual(ns["native_tuning_settings"](), {})
        self.assertEqual(ns["DEFAULT_POLICY"]["avoidance"], "avoider")
        self.assertEqual(
            {k: ns["DEFAULT_POLICY"][k] for k in opt.SPACES["ali"]["defaults"]},
            opt.SPACES["ali"]["defaults"],
        )

    def test_polyfit_actual_instances_and_clearance_receive_all_settings(self):
        ns, tree = controller_namespace("polyfit")
        supplied = {"cruise_speed": 11.0, "margin": 10.0, "horizon": 25.0}
        assignments = [
            n
            for n in tree.body
            if isinstance(n, ast.Assign)
            and any(
                isinstance(t, ast.Name)
                and t.id in {"tracker", "tuning_settings", "avoider", "governor", "planner"}
                for t in n.targets
            )
        ]
        with patch.dict(os.environ, {"WSORB_STP_TUNING": json.dumps(supplied)}):
            exec(
                compile(ast.Module(body=assignments, type_ignores=[]), "native-instances", "exec"),
                ns,
            )
        self.assertEqual(ns["planner"].cruise_speed, 11.0)
        self.assertEqual(ns["governor"].gain, 50.0)
        self.assertEqual(ns["avoider"].unknown_extra, 4.0)
        wanted = (
            ns["planner"].desired_velocity
            if hasattr(ns["planner"], "desired_velocity")
            else ns["planner"].wanted_velocity
        )
        self.assertAlmostEqual(
            np.linalg.norm(wanted(np.zeros(3), np.zeros(3), np.array([1000.0, 0.0, 0.0]))), 11.0
        )
        rock = SimpleNamespace(
            diameter=2.0,
            velocity=np.zeros(3),
            predicted_position=lambda now: np.array([200.0, 0.0, 0.0]),
        )
        original = ns["clearance"]
        observed = []

        def tracked(velocity, relative, speeds, keep_out, horizon):
            observed.append((keep_out.copy(), horizon))
            return original(velocity, relative, speeds, keep_out, horizon)

        ns["clearance"] = tracked
        ns["avoider"].choose(np.array([11.0, 0.0, 0.0]), [rock], np.zeros(3), 1.0)
        self.assertTrue(observed)
        self.assertEqual(observed[0][1], 25.0)
        np.testing.assert_allclose(observed[0][0], [11.0])
        with patch.dict(os.environ, {"WSORB_STP_TUNING": "{}"}):
            self.assertEqual(ns["native_tuning_settings"](), opt.SPACES["polyfit"]["defaults"])

    def test_invalid_or_uncontrolled_settings_fail_closed(self):
        for name in opt.SPACES:
            ns, _ = controller_namespace(name)
            for supplied in (
                {"unknown": 1.0},
                {next(iter(opt.SPACES[name]["bounds"])): float("nan")},
                {next(iter(opt.SPACES[name]["bounds"])): True},
            ):
                with (
                    patch.dict(os.environ, {"WSORB_STP_TUNING": json.dumps(supplied)}),
                    self.assertRaises(ValueError),
                ):
                    ns["native_tuning_settings"]()
        ns, _ = controller_namespace("ali")
        with (
            patch.dict(os.environ, {"WSORB_STP_TUNING": '{"cruise_speed_mps": 9}'}),
            self.assertRaises(ValueError),
        ):
            ns["native_tuning_settings"]()


class HarryTerminalCoast(unittest.TestCase):
    def test_intersection_uses_future_relative_motion(self):
        ns, _ = controller_namespace("ali")
        hit = ns["coast_intersects_goal"]
        position = np.zeros(3)
        target = np.array([200.0, 0.0, 0.0])
        self.assertTrue(hit(position, [8.0, 0.0, 0.0], target, 50.0))
        self.assertFalse(hit(position, [0.0, 8.0, 0.0], target, 50.0))
        self.assertFalse(hit(position, [-8.0, 0.0, 0.0], target, 50.0))
        self.assertFalse(hit(position, [0.0, 0.0, 0.0], target, 50.0))
        self.assertTrue(hit(position, [0.0, 0.0, 0.0], [20.0, 0.0, 0.0], 50.0))
        self.assertTrue(hit(position, [8.0, 0.0, 0.0], [200.0, 50.0, 0.0], 50.0))
        self.assertFalse(hit(position, [8.0, 0.0, 0.0], target, 50.0, [8.0, 0.0, 0.0]))
        self.assertFalse(hit(position, [8.0, 0.0, 0.0], target, 50.0, [0.0, 8.0, 0.0]))

    def test_real_step_corrects_miss_and_receding_paths_but_coasts_on_arrival_path(self):
        ns, _ = controller_namespace("ali")
        for velocity, expected_terminal in (
            ([8.0, 0.0, 0.0], False),
            ([0.0, -8.0, 0.0], False),
            ([0.0, 8.0, 0.0], True),
        ):
            state = dict(
                burning=True,
                attitude_hold=False,
                last_control=np.zeros(3),
                rng=np.random.default_rng(12345),
                launched=True,
                policy=opt.settings("ali", opt.SPACES["ali"]["defaults"]),
            )
            obs = dict(
                position=np.array([2000.0, 0.0, 0.0]),
                velocity=np.array(velocity),
                target=np.array([2000.0, 200.0, 0.0]),
                target_velocity=np.zeros(3),
                body_x=np.array([0.0, 1.0, 0.0]),
                thrust_axis=np.array([0.0, 1.0, 0.0]),
                angular_velocity=np.zeros(3),
                fuel=0.0,
                damage=0.0,
                radius=3.0,
                max_accel=0.5,
                max_angular=0.05,
                mass=3388.7,
            )
            command, _, record = ns["step"](state, obs, [], 10.0, 0.1)
            self.assertEqual(record["phase"] == "terminal", expected_terminal)
            self.assertEqual(bool(np.any(command)), not expected_terminal)

    def test_harry_variant_records_its_frozen_ali_origin(self):
        frozen = opt.REPO / "runs/20261118T070841Z-phobos-mppi-smac3/sources/ali/controller.py"
        origin_sha256 = "e74e206a57dfe6590e3a56075165c6903f5c5df5e74b575bba16ac92b599d1fd"
        provenance = (opt.REPO / "controllers/harry/mppi/README.md").read_text(encoding="utf-8")
        self.assertIn(origin_sha256, provenance)
        if frozen.exists():
            self.assertEqual(opt.evidence.digest(frozen), origin_sha256)
        self.assertEqual(opt.SPACES["ali"]["directory"], "controllers/harry/mppi")


class RuntimeAndArchive(unittest.TestCase):
    def test_runtime_allows_snapshot_relocation_and_rejects_condition_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            root, baseline = runtime_fixture(repo)
            candidate = root / "ali/screen-01/native"
            provenance, _ = native_fixture(candidate, repo / "install")
            opt.assert_runtime(root, candidate, live=True)
            provenance["binaries"] = {"different": "hash"}
            opt.write(candidate / "provenance.json", provenance)
            with self.assertRaisesRegex(ValueError, "binaries"):
                opt.assert_runtime(root, candidate)
            with (
                patch.object(opt.benchmark, "run_campaign") as launch,
                self.assertRaises(ValueError),
            ):
                opt.run_native(candidate, False)
            launch.assert_not_called()
            (baseline / "snapshot/asset.txt").write_text("changed physics")
            with self.assertRaisesRegex(ValueError, "snapshot changed"):
                opt.assert_runtime(root)

    def test_binary_change_is_rejected_before_preparing_or_launching_another_stage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root, baseline = runtime_fixture(Path(temporary))
            provenance = opt.read(baseline / "provenance.json")
            binary = Path(next(iter(provenance["binaries"])))
            binary.write_text("updated executable")
            with (
                patch.object(opt.benchmark, "prepare") as prepare,
                self.assertRaisesRegex(ValueError, "binary changed"),
            ):
                opt.prepare_stage(root, root / "ali/screen-01", [1], [])
            prepare.assert_not_called()
            self.assertFalse((root / "ali/screen-01").exists())

    def test_new_stage_provenance_must_match_anchor_after_preparation(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            root, _ = runtime_fixture(repo)

            def changed_prepare(config, native, mode):
                provenance, _ = native_fixture(native, repo / "install", opt.read(config))
                provenance["scheduling"] = "different scheduling"
                opt.write(native / "provenance.json", provenance)

            with (
                patch.object(opt.benchmark, "prepare", side_effect=changed_prepare),
                self.assertRaisesRegex(ValueError, "scheduling"),
            ):
                opt.prepare_stage(root, root / "ali/screen-01", [1], [dict(id="candidate")])

    def test_archive_retains_nested_caps_and_incomplete_outcomes_without_invented_scores(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            root, baseline = runtime_fixture(repo)
            old = repo / "runs/historical/nested-campaign"
            provenance, config = native_fixture(old, repo / "install")
            opt.write(baseline / "snapshot/seed_adapter.json", dict(radar=100))
            opt.write(old / "snapshot/seed_adapter.json", dict(radar=100))
            attempts = []
            for index, outcome in enumerate(("success", "simulation_limit", "evidence_failure")):
                identity = f"run-{index}"
                attempts.append(dict(seed=100, run_id=identity, controller=dict(id="previous")))
                folder = old / identity
                folder.mkdir()
                opt.write(
                    folder / "receipt.json",
                    dict(
                        outcome=outcome,
                        official_score=25.0 if outcome == "success" else None,
                        cleanup="owned_process_tree_exited",
                        inputs=None,
                    ),
                )
                opt.write(
                    folder / "manifest.json",
                    dict(
                        effective_seeds=dict(spawner=100, radar=100),
                        limits=config["limits"],
                        normal_scheduling=True,
                        diagnostic_rerun=False,
                    ),
                )
            attempts.append(
                dict(seed=1729, run_id="outside-held-out-block", controller=dict(id="previous"))
            )
            opt.write(old / "campaign.json", dict(config=config, attempts=attempts))
            with patch.object(opt, "REPO", repo):
                opt.archive_audit(root)
            records = opt.read(root / "archive.json")
            self.assertEqual(len(records), 3)
            self.assertEqual(sum(r["official_score"] is None for r in records), 2)
            cap = next(r for r in records if r["outcome"] == "simulation_limit")
            self.assertTrue(cap["directly_comparable"])
            infrastructure = next(r for r in records if r["outcome"] == "evidence_failure")
            self.assertFalse(infrastructure["directly_comparable"])
            coverage = opt.archive_coverage(records)[0]
            self.assertEqual(coverage["attempted"], 3)
            self.assertEqual(coverage["scored"], 1)
            self.assertEqual(coverage["outcomes"]["simulation_limit"], 1)
            self.assertTrue(all(r["seed"] == 100 for r in records))
            group = [dict(seed=100, official_score=30.0)]
            comparisons = opt.historical_pairs(root, group, group)
            self.assertEqual(
                next(r for r in comparisons if r["outcome"] == "success")["finalist_score_delta"],
                5.0,
            )
            self.assertIsNone(
                next(r for r in comparisons if r["outcome"] == "simulation_limit")[
                    "finalist_score_delta"
                ]
            )


class HarryPolyfitCone(unittest.TestCase):
    def test_aligned_roundoff_contact_is_retained_without_warnings(self):
        ns, _ = controller_namespace("polyfit")
        vector = np.array([-0.6826779865523179, 104.61432923049026, 74.15884212884828])
        row = np.zeros((3, 3))
        row[0] = vector
        row[1] = [np.linalg.norm(vector) - 1.0, 0.0, 2.0]
        row[2, 0] = 1.0
        tracker = ns["Tracker"]()
        with np.errstate(invalid="raise", divide="raise"):
            tracker.update([row], np.zeros(3), 1.0, vector * 2.0)
        self.assertEqual(len(tracker.rocks), 1)
        self.assertAlmostEqual(ns["cone_angle"](vector, vector * 2.0), 0.0)

    def test_degenerate_vectors_are_rejected_and_core_bubble_is_preserved(self):
        ns, _ = controller_namespace("polyfit")
        angle = ns["cone_angle"]
        with np.errstate(invalid="raise", divide="raise"):
            self.assertTrue(np.isinf(angle(np.zeros(3), [1.0, 0.0, 0.0])))
            self.assertTrue(np.isinf(angle([1.0, 0.0, 0.0], np.zeros(3))))
            self.assertTrue(np.isinf(angle([np.inf, 0.0, 0.0], [1.0, 0.0, 0.0])))
            self.assertAlmostEqual(angle([1.0, 0.0, 0.0], [-1.0, 0.0, 0.0]), np.pi)
            row = np.zeros((3, 3))
            row[1] = [10.0, 0.0, 2.0]
            row[2, 0] = 1.0
            tracker = ns["Tracker"]()
            tracker.update([row], np.zeros(3), 1.0, np.zeros(3))
        self.assertEqual(len(tracker.rocks), 1)
        self.assertEqual(opt.SPACES["polyfit"]["directory"], "controllers/harry/polyfit")

    def test_external_package_is_required_and_reviewed_inputs_are_pinned(self):
        profile = opt.read(opt.REPO / "experiments/stp_optimiser/bui_polyfit.json")
        self.assertIsNone(profile["native_config"]["package"])
        pins = profile["package_file_sha256"]
        for name in (
            profile["native_config"]["mission"],
            "Systems/Scoring.py",
            "Systems/Radar.py",
            "Systems/JetSelectionActuator.py",
            "Systems/SpawnDebris.py",
            "Data/SpaceBuggyJetConfigNoMains.csv",
            "Entities/Rock1.enconf",
        ):
            self.assertIn(name, pins)
        self.assertEqual(
            pins[profile["native_config"]["mission"]], profile["native_config"]["mission_sha256"]
        )
        self.assertEqual(pins["Systems/Scoring.py"], profile["native_config"]["scorer_sha256"])
        readme = (opt.REPO / "controllers/harry/polyfit/readme.md").read_text(encoding="utf-8")
        self.assertIn("a1d4fed643440c85b0957c28aad22811fdfb35ddf6ac7a1c4a1b1b55e006626b", readme)


class ExternalPackageValidation(unittest.TestCase):
    def fixture(self, root):
        files = {
            "Sims/FINAL_SUBMISSION.simconf": "mission",
            "Systems/Scoring.py": "scorer",
            "Systems/Radar.py": "radar",
            "Data/thrusters.csv": "data",
        }
        pins = {}
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            pins[relative] = opt.evidence.digest(path)
        config = dict(
            package=str(root),
            mission="Sims/FINAL_SUBMISSION.simconf",
            mission_sha256=pins["Sims/FINAL_SUBMISSION.simconf"],
            scorer_sha256=pins["Systems/Scoring.py"],
        )
        return config, dict(package_file_sha256=pins)

    def test_matching_external_configs_are_accepted_without_modification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, profile = self.fixture(root)
            before = opt.benchmark.hashes(root)
            opt.validate_package(config, profile)
            self.assertEqual(opt.benchmark.hashes(root), before)

    def test_missing_or_modified_configs_name_each_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, profile = self.fixture(root)
            (root / "Systems/Radar.py").unlink()
            (root / "Data/thrusters.csv").write_text("different data", encoding="utf-8")
            with self.assertRaises(ValueError) as raised:
                opt.validate_package(config, profile)
            self.assertIn("missing Systems/Radar.py", str(raised.exception))
            self.assertIn("hash mismatch Data/thrusters.csv", str(raised.exception))
            self.assertEqual((root / "Data/thrusters.csv").read_text(), "different data")

    def test_package_manifest_cannot_reference_files_outside_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            config, profile = self.fixture(Path(temporary))
            profile["package_file_sha256"]["../outside"] = "bad"
            with self.assertRaisesRegex(ValueError, "escapes"):
                opt.validate_package(config, profile)


class StagePreparationRecovery(unittest.TestCase):
    def test_unflown_partial_native_is_preserved_and_same_stage_can_resume(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            root, _ = runtime_fixture(repo)
            target = root / "polyfit/screen-01"
            controllers = [dict(id="polyfit_01", environment={})]

            def interrupted(config, native, mode):
                (native / "snapshot").mkdir(parents=True)
                (native / "snapshot/partial.txt").write_text("preserve this staging record")
                raise RuntimeError("interrupted staging")

            with (
                patch.object(opt.benchmark, "prepare", side_effect=interrupted),
                self.assertRaisesRegex(RuntimeError, "interrupted staging"),
            ):
                opt.prepare_stage(root, target, [1], controllers)
            saved_input = (target / "input.json").read_bytes()

            def completed(config, native, mode):
                native_fixture(native, repo / "install", opt.read(config))

            with patch.object(opt.benchmark, "prepare", side_effect=completed) as prepare:
                native = opt.prepare_stage(root, target, [1], controllers)
                again = opt.prepare_stage(root, target, [1], controllers)
            self.assertEqual(native, again)
            self.assertEqual(prepare.call_count, 1)
            self.assertEqual((target / "input.json").read_bytes(), saved_input)
            preserved = list(target.glob("native-incomplete-*"))
            self.assertEqual(len(preserved), 1)
            self.assertEqual(
                (preserved[0] / "snapshot/partial.txt").read_text(), "preserve this staging record"
            )
            self.assertFalse(list(native.rglob("receipt.json")))
            with self.assertRaisesRegex(ValueError, "inputs changed"):
                opt.prepare_stage(root, target, [1], [dict(id="different", environment={})])

    def test_partial_stage_with_flight_evidence_is_never_rebuilt(self):
        for filename in (
            "receipt.json",
            "manifest.json",
            "events.jsonl",
            "launcher.log",
            "status.json",
        ):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as temporary:
                repo = Path(temporary)
                root, _ = runtime_fixture(repo)
                target = root / "polyfit/screen-01"
                evidence_file = target / "native/attempt" / filename
                evidence_file.parent.mkdir(parents=True)
                evidence_file.write_text("original physical evidence")
                with (
                    patch.object(opt.benchmark, "prepare") as prepare,
                    self.assertRaisesRegex(ValueError, "flight evidence"),
                ):
                    opt.prepare_stage(root, target, [1], [dict(id="polyfit_01")])
                prepare.assert_not_called()
                self.assertEqual(evidence_file.read_text(), "original physical evidence")
                self.assertFalse(list(target.glob("native-incomplete-*")))

    def test_partial_outer_directory_and_path_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            root, _ = runtime_fixture(repo)
            target = root / "polyfit/screen-01"
            target.mkdir(parents=True)
            with patch.object(
                opt.benchmark,
                "prepare",
                side_effect=lambda cfg, native, mode: native_fixture(
                    native, repo / "install", opt.read(cfg)
                ),
            ) as prepare:
                opt.prepare_stage(root, target, [1], [dict(id="polyfit_01")])
                self.assertEqual(prepare.call_count, 1)
            with self.assertRaisesRegex(ValueError, "inside its campaign root"):
                opt.prepare_stage(root, root.parent / "escaped", [1], [dict(id="polyfit_01")])
            self.assertFalse((root.parent / "escaped").exists())


class LossAndCoverage(unittest.TestCase):
    def test_native_scores_caps_and_infrastructure(self):
        self.assertEqual(
            opt.loss(dict(outcome="success", official_score=12.345678901)), -12.345678901
        )
        self.assertEqual(opt.loss(dict(outcome="failure", official_score=-256.123)), 256.123)
        cap = dict(outcome="simulation_limit", official_score=None)
        self.assertEqual(opt.loss(cap), 1000.0)
        self.assertIsNone(cap["official_score"])
        self.assertEqual(opt.loss(cap, 500.0), 500.0)
        for outcome in (
            "evidence_failure",
            "aborted",
            "interrupted",
            "startup_timeout",
            "in_progress",
            "failure",
        ):
            with self.assertRaises(ValueError):
                opt.loss(dict(outcome=outcome, official_score=None))

    def test_matched_ranking_safety_and_penalty_sensitivity(self):
        def row(seed, score, damage=0.0, outcome="success"):
            return dict(
                seed=seed,
                official_score=score,
                outcome=outcome,
                damage_J=damage,
                fuel_kg=2.0,
                arrival_time_s=200.0,
                arrival_speed_m_s=8.0,
            )

        base = [row(1, 1.0), row(2, 2.0)]
        good = [row(1, 3.0), row(2, 4.0)]
        self.assertEqual(opt.rank({"a": good, "b": base}, base), ["a", "b"])
        with self.assertRaises(ValueError):
            opt.rank({"a": good[:1]}, base)
        self.assertTrue(opt.safety(good, base)["eligible"])
        self.assertFalse(
            opt.safety([row(1, 100.0, damage=5000.0), row(2, 100.0, damage=5000.0)], base)[
                "eligible"
            ]
        )
        self.assertFalse(
            opt.safety([row(1, 100.0, outcome="failure"), row(2, 100.0)], base)["eligible"]
        )
        self.assertEqual([p["score_delta"] for p in opt.paired(good, base)], [2.0, 2.0])
        capped = [row(1, None, outcome="simulation_limit"), row(2, 100.0)]
        poor = [row(1, -300.0), row(2, -300.0)]
        self.assertNotEqual(
            opt.rank({"cap": capped, "poor": poor}, base),
            opt.rank({"cap": capped, "poor": poor}, base, 500.0),
        )

    def test_finalist_must_strictly_improve_baseline_and_pass_safety(self):
        def group(score, damage=0.0):
            return [
                dict(
                    seed=1,
                    outcome="success",
                    official_score=score,
                    damage_J=damage,
                    fuel_kg=1.0,
                    arrival_time_s=100.0,
                    arrival_speed_m_s=8.0,
                )
            ]

        self.assertEqual(
            opt.choose_finalist({0: group(100.0), 1: group(10.0), 2: group(20.0)}, [1, 2]), 0
        )
        self.assertEqual(
            opt.choose_finalist({0: group(100.0), 1: group(100.0), 2: group(100.0)}, [1, 2]), 0
        )
        self.assertEqual(
            opt.choose_finalist(
                {0: group(100.0), 1: group(110.0), 2: group(120.0, 5000.0)}, [1, 2]
            ),
            1,
        )

    def test_banks_are_disjoint_and_preserve_frozen_membership(self):
        bank = opt.read(
            opt.REPO / "docs/analytics/stp-runs/20261117T040830Z-HY/final-seed-bank.json"
        )
        used = set(bank["seeds"])
        generated = []
        for index in range(opt.TRAIN_SEED_COUNT + opt.VALIDATION_SEED_COUNT):
            value = opt.seed_tools.appended_seed("phobos-smac3-optimisation-v1", index, used)
            generated.append(value)
            used.add(value)
        self.assertEqual(len(set(generated)), 12)
        self.assertFalse(set(generated) & set(bank["seeds"]))
        self.assertEqual(opt.derived_bank(bank, bank["seeds"], "final")["seeds"], bank["seeds"])
        with self.assertRaises(ValueError):
            opt.derived_bank(bank, generated, "wrong")


class CampaignScope(unittest.TestCase):
    def test_preparation_freezes_mppi_only_and_broader_disjoint_blocks(self):
        original_repo = opt.REPO
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            docs = Path("docs/analytics/stp-runs/20261117T040830Z-HY")
            (repo / docs).mkdir(parents=True)
            for name in ("updated-candidates.json", "final-seed-bank.json"):
                shutil.copy2(original_repo / docs / name, repo / docs / name)
            shutil.copytree(
                original_repo / opt.SPACES["ali"]["directory"],
                repo / opt.SPACES["ali"]["directory"],
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            root = repo / "runs/preparation-fixture"
            with (
                patch.object(opt, "REPO", repo),
                patch.object(opt.benchmark, "revision", return_value="fixture"),
                patch.object(
                    opt,
                    "prepare_stage",
                    side_effect=lambda root, target, *args: native_fixture(
                        target / "native", repo / "install"
                    ),
                ) as stage,
                patch.object(
                    opt, "archive_audit", side_effect=lambda p: opt.write(p / "archive.json", [])
                ),
            ):
                opt.prepare(root)
                experiment = opt.read(root / "experiment.json")
                self.assertEqual(experiment["controllers"], ["ali"])
                self.assertEqual(len(experiment["train"]), 8)
                self.assertEqual(len(experiment["validation"]), 4)
                self.assertEqual(len(experiment["final_test"]), 6)
                self.assertEqual(experiment["flight_budget"], 88)
                self.assertEqual(
                    experiment["final_test"],
                    opt.read(repo / docs / "final-seed-bank.json")["seeds"][:6],
                )
                self.assertEqual(len(opt.read(root / "final-bank.json")["seeds"]), 22)
                self.assertEqual(set(experiment["resolved_settings"]), {"ali"})
                self.assertFalse((root / "sources/polyfit").exists())
                self.assertEqual(stage.call_count, 1)
                self.assertEqual(len(stage.call_args.args[2]), 8)
                self.assertEqual([c["id"] for c in stage.call_args.args[3]], ["ali_baseline"])
                bank = opt.read(root / "optimisation-bank.json")
                self.assertEqual(bank["seeds"], experiment["train"] + experiment["validation"])
                self.assertFalse(set(bank["seeds"]) & set(experiment["final_test"]))
                self.assertEqual(
                    opt.read(root / "final-bank.json"),
                    opt.read(repo / docs / "final-seed-bank.json"),
                )
                opt.assert_pins(root)

    def test_polyfit_profile_prepares_without_ali_sources_or_local_machine_paths(self):
        original_repo = opt.REPO
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            docs = Path("docs/analytics/stp-runs/20261117T040830Z-HY")
            (repo / docs).mkdir(parents=True)
            shutil.copy2(
                original_repo / docs / "final-seed-bank.json", repo / docs / "final-seed-bank.json"
            )
            source_dir = opt.SPACES["polyfit"]["directory"]
            shutil.copytree(
                original_repo / source_dir,
                repo / source_dir,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            profile_path = original_repo / "experiments/stp_optimiser/bui_polyfit.json"
            root = repo / "runs/polyfit-fixture"
            with (
                patch.object(opt, "REPO", repo),
                patch.object(opt, "validate_package"),
                patch.object(opt.benchmark, "revision", return_value=None),
                patch.object(
                    opt,
                    "prepare_stage",
                    side_effect=lambda root, target, *args: native_fixture(
                        target / "native", repo / "install"
                    ),
                ) as stage,
                patch.object(
                    opt, "archive_audit", side_effect=lambda p: opt.write(p / "archive.json", [])
                ),
            ):
                opt.prepare(
                    root,
                    config_path=profile_path,
                    install=repo / "install",
                    package=repo / "inputs/STU4-Sim",
                )
            experiment = opt.read(root / "experiment.json")
            self.assertEqual(experiment["controllers"], ["polyfit"])
            self.assertEqual(experiment["flight_budget"], 88)
            self.assertEqual(set(experiment["resolved_settings"]), {"polyfit"})
            self.assertFalse((root / "sources/ali").exists())
            self.assertEqual(stage.call_args.args[3][0]["id"], "polyfit_baseline")
            self.assertNotIn("MPPI", stage.call_args.args[3][0]["method"])
            self.assertNotIn("terminal-coast", stage.call_args.args[3][0]["source_changes"])
            self.assertEqual(
                json.loads(stage.call_args.args[3][0]["environment"]["WSORB_STP_TUNING"]),
                opt.SPACES["polyfit"]["defaults"],
            )
            self.assertEqual(
                experiment["input_profile"]["controller_sha256"],
                opt.evidence.digest(root / "sources/polyfit/Controller.py"),
            )

    def test_original_seed_bank_pin_is_validated_before_output_or_staging(self):
        original_repo = opt.REPO
        profile_path = original_repo / "experiments/stp_optimiser/bui_polyfit.json"
        original = opt.read(
            original_repo / "docs/analytics/stp-runs/20261117T040830Z-HY/final-seed-bank.json"
        )

        def reorder(bank):
            bank["seeds"][0], bank["seeds"][-1] = bank["seeds"][-1], bank["seeds"][0]

        mutations = (
            reorder,
            lambda b: b["seeds"].__setitem__(0, b["seeds"][1]),
            lambda b: b.update(id="other bank"),
            lambda b: b["scenario"].update(scorer_sha256="different scorer"),
            lambda b: b["seeds"].__setitem__(0, -1),
        )
        for mutate in mutations:
            with self.subTest(mutation=mutate), tempfile.TemporaryDirectory() as temporary:
                repo = Path(temporary)
                bank = copy.deepcopy(original)
                mutate(bank)
                bank_path = (
                    repo / "docs/analytics/stp-runs/20261117T040830Z-HY/final-seed-bank.json"
                )
                bank_path.parent.mkdir(parents=True)
                opt.write(bank_path, bank)
                source_dir = opt.SPACES["polyfit"]["directory"]
                shutil.copytree(
                    original_repo / source_dir,
                    repo / source_dir,
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
                root = repo / "runs/reject-bank"
                with (
                    patch.object(opt, "REPO", repo),
                    patch.object(opt, "validate_package"),
                    patch.object(opt.benchmark, "prepare") as prepare,
                    self.assertRaises(ValueError),
                ):
                    opt.prepare(
                        root,
                        config_path=profile_path,
                        install=repo / "install",
                        package=repo / "external-STU4-Sim",
                    )
                prepare.assert_not_called()
                self.assertFalse(root.exists())

    def test_polyfit_profile_requires_machine_path_and_exact_source(self):
        profile_path = opt.REPO / "experiments/stp_optimiser/bui_polyfit.json"
        with self.assertRaisesRegex(ValueError, "Set --install"):
            opt.preparation_config(profile_path)
        with self.assertRaisesRegex(ValueError, "Set --package"):
            opt.preparation_config(profile_path, install="installed")
        with self.assertRaisesRegex(ValueError, "differs from the pinned"):
            opt.preparation_config(profile_path, controller_name="ali", install="installed")
        with tempfile.TemporaryDirectory() as temporary:
            profile = opt.read(profile_path)
            profile["controller_sha256"] = "wrong"
            broken = Path(temporary) / "broken-profile.json"
            opt.write(broken, profile)
            with self.assertRaisesRegex(ValueError, "source differs"):
                opt.preparation_config(broken, install="installed")

    def test_run_dispatches_only_frozen_selected_controller(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            opt.write(root / "experiment.json", dict(controllers=["ali"]))
            (root / "ali").mkdir()
            opt.write(root / "ali/selection.json", {})
            with (
                patch.object(opt.sys, "argv", ["optimise.py", "run", str(root)]),
                patch.object(opt, "assert_pins"),
                patch.object(opt.seed_tools, "bank_lock", return_value=nullcontext()),
                patch.object(opt, "tune") as tune,
                patch.object(opt, "final_test") as final,
            ):
                opt.main()
                tune.assert_called_once_with(root, "ali", False)
                final.assert_called_once_with(root, "ali", False)


class Resumption(unittest.TestCase):
    def test_real_smac_eight_aggregate_proposals_replay_exactly(self):
        from smac.runhistory.dataclasses import TrialValue

        for name in opt.SPACES:
            histories = []
            for _ in range(2):
                with tempfile.TemporaryDirectory() as temporary:
                    smac = opt.make_smac(name, Path(temporary))
                    configs = []
                    for _index in range(8):
                        trial = smac.ask()
                        configs.append(dict(trial.config))
                        smac.tell(
                            trial,
                            TrialValue(cost=float(sum(dict(trial.config).values())), time=0.0),
                        )
                    histories.append(configs)
            self.assertEqual(histories[0], histories[1])
            self.assertEqual(histories[0][0], opt.SPACES[name]["defaults"])
            self.assertEqual(len({json.dumps(c, sort_keys=True) for c in histories[0]}), 8)

    def test_interruption_preserves_pending_proposal_and_does_not_repeat_flights(self):
        class FakeSmac:
            def __init__(self):
                self.index = 0

            def ask(self):
                values = dict(opt.SPACES["polyfit"]["defaults"])
                values["margin"] += self.index * 0.1
                self.index += 1
                return SimpleNamespace(config=values)

            def tell(self, *args):
                pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            opt.write(
                root / "experiment.json",
                dict(train=[1, 2], validation=[3, 4], checkout_revision="fixture"),
            )
            (root / "baseline/native").mkdir(parents=True)
            scheduled = {}
            launches = []

            def prepare(root, target, assigned, controllers):
                (target / "native").mkdir(parents=True, exist_ok=True)
                scheduled[str(target / "native")] = (assigned, controllers)

            def collect(native, label, assigned):
                return [
                    dict(
                        seed=s,
                        outcome="success",
                        official_score=10.0,
                        damage_J=0.0,
                        fuel_kg=1.0,
                        arrival_time_s=200.0,
                        arrival_speed_m_s=8.0,
                    )
                    for s in assigned
                ]

            def launch(native, resume):
                launches.append(str(native))
                if len(launches) == 3:
                    raise RuntimeError("synthetic interruption before completion")

            with (
                patch.object(opt, "make_smac", side_effect=lambda *args: FakeSmac()),
                patch.object(opt, "prepare_stage", side_effect=prepare),
                patch.object(opt, "rows", side_effect=collect),
                patch.object(opt, "run_native", side_effect=launch),
            ):
                with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
                    opt.tune(root, "polyfit", False)
                saved = opt.read(root / "polyfit/screen.json")
                self.assertEqual(len(saved), 3)
                self.assertIsNone(saved[2]["loss"])
                self.assertEqual(saved[1]["loss"], -10.0)
                launches.clear()
                with patch.object(
                    opt,
                    "run_native",
                    side_effect=lambda native, resume: launches.append(str(native)),
                ):
                    selected = opt.tune(root, "polyfit", True)
                self.assertFalse(any("screen-01" in p or "baseline" in p for p in launches))
                self.assertEqual(launches[0], str(root / "polyfit/screen-02/native"))
                self.assertEqual(selected["top_two"], [1, 2])
                self.assertTrue((root / "polyfit/selection.json").exists())


if __name__ == "__main__":
    unittest.main()
