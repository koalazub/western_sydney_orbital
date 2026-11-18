"""Evidence semantics for the read-only native STP analysis notebook."""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_SOURCE = Path(__file__).resolve().parents[1] / "docs/controllers/stp_analysis.py"
_SPEC = importlib.util.spec_from_file_location("harry_stp_analysis", _SOURCE)
analysis = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(analysis)


class HarryAnalysisTests(unittest.TestCase):
    def test_delivery_requires_prior_matching_write_and_returned_impulse(self):
        events = [
            dict(kind="connected", role="controller", native_run_uuid="native-mission-1"),
            dict(kind="command_written", monotonic_s=10, linear_body_m_s2=[1, 0, 0]),
            dict(
                kind="actuator_applied",
                received=dict(
                    linear_body_m_s2=[1, 0, 0],
                    linear_read_monotonic_s=9,
                ),
                linear_impulse_body_kg_m_s=[2, 0, 0],
            ),
            dict(kind="telemetry", sim_s=0, monotonic_s=10, range_m=100, damage_J=0),
            dict(kind="telemetry", sim_s=1, monotonic_s=11, range_m=5, damage_J=10),
            dict(kind="telemetry", sim_s=2, monotonic_s=12, range_m=50, damage_J=10),
        ]
        for event, age in zip(
            [e for e in events if e["kind"] == "telemetry"], [3, 4, 5], strict=True
        ):
            event["sensor_age_s"] = age
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "events.jsonl"
            path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
            result = analysis.scan_events(path)
            self.assertFalse(result["delivery_verified"])
            self.assertEqual(result["closest_range_m"], 5)
            self.assertEqual(result["damage_increase_intervals"], 1)
            self.assertEqual(result["observed_clock_ratio"], 1)
            self.assertEqual(result["native_mission_ids"], ["native-mission-1"])
            self.assertAlmostEqual(result["radar_worst_age_p95_s"], 4.9)
            # The 1 s closest approach lies between retained 2 s history buckets.
            self.assertNotIn(5, [r["range_m"] for r in result["telemetry"]])
            events.append(
                dict(
                    kind="actuator_applied",
                    received=dict(
                        linear_body_m_s2=[1, 0, 0],
                        linear_read_monotonic_s=11,
                    ),
                    linear_impulse_body_kg_m_s=[2, 0, 0],
                )
            )
            path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
            self.assertTrue(analysis.scan_events(path)["delivery_verified"])

    def test_incomplete_attempts_and_unmatched_seeds_are_not_pooled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            attempts = [
                dict(run_id="tom-100", seed=100, repetition=0, controller=dict(id="tom")),
                dict(run_id="bui-100", seed=100, repetition=0, controller=dict(id="bui")),
                dict(run_id="tom-200", seed=200, repetition=0, controller=dict(id="tom")),
                dict(run_id="bui-abort", seed=200, repetition=0, controller=dict(id="bui")),
            ]
            campaign = dict(
                mode="seeded",
                attempts=attempts,
                config=dict(
                    controllers=[dict(id="tom"), dict(id="bui")],
                    seeds=[100, 200],
                ),
            )
            (root / "campaign.json").write_text(json.dumps(campaign), encoding="utf-8")
            for attempt, outcome in zip(
                attempts,
                ["simulation_limit", "simulation_limit", "simulation_limit", "aborted"],
                strict=True,
            ):
                folder = root / attempt["run_id"]
                folder.mkdir()
                (folder / "receipt.json").write_text(
                    json.dumps(dict(outcome=outcome)), encoding="utf-8"
                )
            result = analysis.load_campaign(root)
            self.assertEqual(len(result["rows"]), 3)
            self.assertEqual(result["common_seeds"], [100])
            self.assertEqual(len(result["common"]), 2)
            self.assertEqual(result["excluded"], {"aborted": 1})
            self.assertTrue(all(r["official_score"] is None for r in result["rows"]))
            self.assertEqual(analysis.paired_scores(result["rows"]), [])
            self.assertEqual(analysis.repeatability(result["rows"]), [])

    def test_complete_repeat_pairs_preserve_outcome_flips_without_imputing_score(self):
        rows = [
            dict(controller="tom", seed=100, repetition=0, outcome="success", official_score=10),
            dict(
                controller="tom",
                seed=100,
                repetition=1,
                outcome="simulation_limit",
                official_score=None,
            ),
            dict(controller="tom", seed=200, repetition=0, outcome="success", official_score=20),
        ]
        pairs = analysis.repeatability(rows)
        self.assertEqual(len(pairs), 1)
        self.assertTrue(pairs[0]["outcome_changed"])
        self.assertIsNone(pairs[0]["score_delta"])


if __name__ == "__main__":
    unittest.main()
