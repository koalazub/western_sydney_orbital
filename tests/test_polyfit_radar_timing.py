"""SDK-free regressions for both native Polyfit timing paths."""

import ast
import unittest
from collections import deque
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def load_timing(version):
    path = ROOT / "controllers" / "tom" / version / "Controller.py"
    tree = ast.parse(path.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "RadarTiming")
    namespace = {"np": np, "deque": deque}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["RadarTiming"]


def row(at, offset=100.0):
    return np.array([[offset, 0, 0], [offset - 1, -8, 2], [at, 0, 0]], float)


class RadarTimingTests(unittest.TestCase):
    def test_history_reconstruction_and_duplicate_reads(self):
        for version in ("polyfit_bodge", "polyfit_bodge_v2"):
            with self.subTest(version=version):
                timing = load_timing(version)()
                timing.prepare([], np.array([0, 0, 0]), 0)
                timing.prepare([], np.array([8, 0, 0]), 1)
                # Measurement at 0.5: shuttle x=4, stationary rock x=100.
                measurement = row(0.5, 96)
                result = timing.prepare([measurement], np.array([20, 0, 0]), 2)
                np.testing.assert_allclose(result[0][0] + [20, 0, 0], [100, 0, 0])
                self.assertEqual(result[0][2, 0], 0.5)
                self.assertEqual(timing.prepare([measurement], np.array([20, 0, 0]), 2), [])
                np.testing.assert_array_equal(measurement, row(0.5, 96))

    def test_mixed_ages_invalid_rows_and_explicit_origin(self):
        for version in ("polyfit_bodge", "polyfit_bodge_v2"):
            with self.subTest(version=version):
                timing = load_timing(version)(origin_offset_s=1)
                timing.prepare([], np.zeros(3), 1)
                bad = row(1)
                bad[0, 0] = np.nan
                rows = timing.prepare(
                    [row(2), row(1), row(0), row(8), bad, np.zeros((2, 2))],
                    np.array([30, 0, 0]),
                    4,
                )
                self.assertEqual([r[2, 0] for r in rows], [2, 3])
                np.testing.assert_allclose([r[0, 0] for r in rows], [80, 90])
                self.assertEqual(timing.prepare([row(1)], np.zeros(3), 10), [])
                # Reset clears both old history and duplicate bookkeeping.
                self.assertEqual(timing.prepare([row(1)], np.zeros(3), 1), [])
                self.assertEqual(len(timing.prepare([row(1)], np.zeros(3), 2)), 1)

    def test_history_miss_is_skipped_and_implementations_match(self):
        timing_classes = [load_timing(v) for v in ("polyfit_bodge", "polyfit_bodge_v2")]
        outputs = []
        for cls in timing_classes:
            timing = cls()
            self.assertEqual(timing.prepare([row(1)], np.zeros(3), 2), [])
            outputs.append(timing.prepare([row(2.5)], np.array([8, 0, 0]), 3))
        np.testing.assert_array_equal(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()

