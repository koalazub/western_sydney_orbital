"""Focused stdlib tests runnable in the native STP Python 3.12 environment."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BENCHMARK = Path(__file__).resolve().parents[1] / "examples" / "stp_benchmark"
sys.path.insert(0, str(BENCHMARK))
spec = importlib.util.spec_from_file_location("official_stp_evidence", BENCHMARK / "evidence.py")
evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence)
import benchmark  # noqa: E402
import capture  # noqa: E402
import seeds  # noqa: E402


class OfficialBenchmarkTests(unittest.TestCase):
    def config(self):
        config = json.loads((BENCHMARK / "campaign.json").read_text())
        bank = json.loads((BENCHMARK / "seed_bank.json").read_text())
        config["seeds"] = bank["seeds"][: config["seed_count"]]
        return config

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
                self.assertEqual(values, [s for seed in expanded["seeds"] for s in (seed, seed)])

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
        runs = evidence.schedule(config, "seeded")
        n = len(config["controllers"])
        self.assertEqual(len(runs), n * 40)
        for block in range(20):
            group = runs[block * n * 2 : (block + 1) * n * 2]
            self.assertEqual(group[0]["controller"]["id"], config["controllers"][block % n]["id"])
            self.assertEqual({r["seed"] for r in group}, {config["seeds"][block]})
            for i in range(0, len(group), 2):
                self.assertEqual(group[i]["controller"], group[i + 1]["controller"])
                self.assertEqual([group[i]["repetition"], group[i + 1]["repetition"]], [0, 1])
        baseline = evidence.schedule(config, "baseline")
        self.assertEqual(len(baseline), 10 * n)
        self.assertTrue(all(r["seed"] is None for r in baseline))

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
            original.write_text("seed = 100\nrng = np.random.default_rng(seed)\n")
            expected = original.read_bytes()
            benchmark.seed_copy(original, seeded, 1729, "spawner")
            self.assertEqual(original.read_bytes(), expected)
            self.assertIn("seed = 1729", seeded.read_text())
            original.write_text("seed = 999\n")
            with self.assertRaises(ValueError):
                benchmark.seed_copy(original, seeded, 1729, "spawner")


if __name__ == "__main__":
    unittest.main()
