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


class OfficialBenchmarkTests(unittest.TestCase):
    def config(self):
        return json.loads((BENCHMARK / "campaign.json").read_text())

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
        self.assertAlmostEqual(win["calculated_score"], 70)
        loss = evidence.score_components(dict(outcome="failure", inputs=inputs))
        self.assertAlmostEqual(loss["calculated_score"], -40)
        inputs["time_s"] = 0
        self.assertAlmostEqual(
            evidence.score_components(dict(outcome="success", inputs=inputs))["time_points"], 0
        )
        self.assertIsNone(evidence.score_components(dict(outcome="wall_timeout", inputs=inputs)))

    def test_report_retains_missing_and_never_promotes_repeated_runs_to_scenarios(self):
        config = self.config()
        config["controllers"] = config["controllers"][:2]
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
                folder / "receipt.json", dict(outcome="wall_timeout", official_score=None)
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
