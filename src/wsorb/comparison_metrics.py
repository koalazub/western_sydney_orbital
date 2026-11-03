"""Publish bounded native metrics and policy timings for an existing path cohort.

Only saved receipts are read. No engine, policy, sensor payload, exception text or
source snapshot is published. The path publications and source archives are read-only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .flight_analytics import (
    COUNTERS,
    FlightAnalyticsRecorder,
    validate_run_analytics,
)
from .path_comparison import (
    _engine,
    _integer,
    _loads,
    _number,
    _plain_file,
    validate_comparison,
)
from .path_comparison import (
    _manifest as _path_manifest,
)

MAX_BYTES = 2 * 1024 * 1024
MAX_RUNS = 128
MAX_SAMPLES = 192
MAX_TRACE_LINE_BYTES = 1024 * 1024
_ROOT = Path(__file__).resolve().parents[2]
_PATHS = _ROOT / "docs" / "analytics" / "path-comparison"
_OUTPUT = _ROOT / "docs" / "analytics" / "path-metrics"
_IDS = ("official", "team")
_POLICY_KEYS = (
    "sample_count",
    "median_policy_wall_s",
    "max_policy_wall_s",
    "policy_calls_over_step_budget",
)
LIMITATIONS = [
    "Saved local measurements, not STP results. No flights are run by this publisher.",
    "Native totals come only from validated physics-step receipts. Missing receipts stay null; "
    "commands never stand in for burns or turns.",
    "Native series contain actual retained readings, not interpolated motion. Thinning does not "
    "alter native episode totals.",
    "Policy wall times describe recorded calls on the source machine, not simulation time. "
    "Counts cover only calls with timings; missing timings are not zero.",
    "Source run and trace hashes, pairing and engine must match the path publication. Worlds "
    "and engine builds are never pooled.",
]


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _hash(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(cell in "0123456789abcdef" for cell in value)
    )


def _same(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-7, abs_tol=1e-7)


def _bounded_file(path: Path, maximum: int = MAX_BYTES) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("A saved publication or receipt must be a bounded regular file")
    raw = _plain_file(path)
    if len(raw) > maximum:
        raise ValueError("A saved file changed beyond its byte budget")
    return raw


def _encode(data: Any) -> bytes:
    raw = (json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if len(raw) > MAX_BYTES:
        raise ValueError("A metrics publication exceeds the 2 MiB file budget")
    return raw


def _published_paths(comparison_id: str) -> tuple[dict, bytes]:
    if comparison_id not in _IDS:
        raise ValueError("comparison_id must be official or team")
    if _PATHS.is_symlink() or not _PATHS.is_dir():
        raise ValueError("The existing path publication directory is unavailable")
    manifest = _path_manifest(_loads(_bounded_file(_PATHS / "manifest.json", 65536)))
    raw = _bounded_file(_PATHS / f"{comparison_id}.json", 4 * 1024 * 1024)
    entry = next((entry for entry in manifest["artifacts"] if entry["id"] == comparison_id), None)
    if entry is None or entry["bytes"] != len(raw) or entry["sha256"] != _digest(raw):
        raise ValueError("The existing path publication differs from its manifest")
    data = validate_comparison(_loads(raw))
    if data["id"] != comparison_id:
        raise ValueError("The existing path publication has a different cohort ID")
    return data, raw


def _trial(directory: Path, path_run: dict, comparison: dict) -> dict:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("A saved trial must be a regular directory")
    run_bytes = _bounded_file(directory / "run.json", 4 * 1024 * 1024)
    if _digest(run_bytes) != path_run["source_sha256"]["run"]:
        raise ValueError("A saved run receipt differs from the existing path source")
    run = _loads(run_bytes)
    if not isinstance(run, dict):
        raise ValueError("A saved run receipt is malformed")
    expected = {
        "schema_version": 1,
        "execution_kind": "owned_native_manual_step",
        "algorithm": path_run["algorithm"],
        "seed": path_run["seed"],
        "outcome": path_run["outcome"],
        "preset": comparison["preset"],
        "debris_count": comparison["debris_count"],
        "duration_s": comparison["duration_s"],
        "step_s": comparison["step_s"],
    }
    if any(run.get(key) != value for key, value in expected.items()):
        raise ValueError("A saved run is not the published paired attempt")
    _integer(run["schema_version"], minimum=1, maximum=1)
    _integer(run["seed"])
    repetition = _integer(run.get("repetition", 1), minimum=1, maximum=10)
    if repetition != path_run["repetition"] or directory.name != path_run["id"]:
        raise ValueError("A saved run ID or repetition differs from the path source")
    engine, _, arrival_radius = _engine(run, comparison)
    engine_hash = engine["engine_build"]["sha256"]
    if engine_hash != comparison["engine_sha256"] or not _same(
        arrival_radius, comparison["arrival_radius_m"]
    ):
        raise ValueError("A saved engine differs from the path source")
    recorder = FlightAnalyticsRecorder(MAX_SAMPLES)
    digest = hashlib.sha256()
    count = 0
    times = []
    policy_receipt = False
    trace = directory / "trace.jsonl"
    if trace.is_symlink() or not trace.is_file():
        raise ValueError("A saved trace must be a regular file")
    with trace.open("rb") as stream:
        while line := stream.readline(MAX_TRACE_LINE_BYTES + 1):
            if len(line) > MAX_TRACE_LINE_BYTES or count >= path_run["source_sample_count"]:
                raise ValueError("A saved trace exceeds its published sample or line budget")
            digest.update(line)
            sample = _loads(line)
            if not isinstance(sample, dict):
                raise ValueError("A saved trace row is malformed")
            observation = sample.get("observation")
            if not isinstance(observation, dict) or observation.get("seed") != path_run["seed"]:
                raise ValueError("A saved observation differs from its paired seed")
            recorder.observe(observation)
            policy_receipt |= "policy_wall_s" in sample
            elapsed = _number(sample.get("policy_wall_s"))
            if elapsed is not None:
                if count == 0:
                    raise ValueError("An initial observation cannot claim a policy call")
                times.append(elapsed)
            count += 1
    if (
        count != path_run["source_sample_count"]
        or digest.hexdigest() != path_run["source_sha256"]["trace"]
    ):
        raise ValueError("A saved trace differs from the existing path source")
    # The recorder rejects missing, legacy or inconsistent native receipts.
    # Never substitute commands, CSV summaries or a current engine for null.
    analytics = validate_run_analytics(recorder.report)
    if analytics is not None and (
        any(analytics["totals"][key] > 2**53 - 1 for key in COUNTERS)
        or not _same(analytics["totals"]["observed_sim_s"], path_run["final"]["time_s"])
    ):
        raise ValueError("A native receipt cannot be represented or joined safely")
    return {
        "id": path_run["id"],
        "algorithm": path_run["algorithm"],
        "seed": path_run["seed"],
        "repetition": path_run["repetition"],
        "outcome": path_run["outcome"],
        "engine_sha256": engine_hash,
        "source_sha256": dict(path_run["source_sha256"]),
        "analytics": analytics,
        "policy": {
            "sample_count": len(times),
            "median_policy_wall_s": statistics.median(times) if times else None,
            "max_policy_wall_s": max(times) if times else None,
            "policy_calls_over_step_budget": (
                sum(elapsed > comparison["step_s"] for elapsed in times) if times else None
            ),
        }
        if policy_receipt
        else None,
    }


def build_metrics(directory: str | Path, *, comparison_id: str) -> dict:
    """Read saved receipts joined to the committed paths. No writes or flights."""
    data, path_bytes = _published_paths(comparison_id)
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("The saved source must be a regular directory")
    expected = sorted(run["id"] for run in data["runs"])
    if sorted(path.name for path in directory.glob("trial-*")) != expected:
        raise ValueError("The saved source does not contain the exact published cohort")
    publication = {
        "schema": "wsorb.comparison_metrics",
        "schema_version": 1,
        "id": comparison_id,
        "source": {
            "path_sha256": _digest(path_bytes),
            "engine_sha256": data["comparison"]["engine_sha256"],
            "step_s": data["comparison"]["step_s"],
        },
        "runs": [_trial(directory / run["id"], run, data["comparison"]) for run in data["runs"]],
        "limitations": list(LIMITATIONS),
    }
    # Keep actual readings and native totals. A large cohort may need fewer
    # interior readings to fit the file budget, but never invented timestamps.
    while True:
        try:
            _encode(publication)
            break
        except ValueError:
            compacted = False
            for item in publication["runs"]:
                analytics = item["analytics"]
                if analytics is not None and len(analytics["samples"]) > 2:
                    rows = analytics["samples"]
                    analytics["samples"] = [rows[0], *rows[2:-1:2], rows[-1]]
                    analytics["sample_count"] = len(analytics["samples"])
                    compacted = True
            if not compacted:
                raise ValueError("Required metrics exceed the 2 MiB file budget") from None
    return validate_metrics(publication, data, path_sha256=_digest(path_bytes))


def validate_metrics(value: Any, data: dict, *, path_sha256: str) -> dict:
    """Validate only allowlisted metrics, their bounds and the exact source join."""
    if not isinstance(value, dict) or set(value) != {
        "schema",
        "schema_version",
        "id",
        "source",
        "runs",
        "limitations",
    }:
        raise ValueError("A metrics publication has unknown or missing fields")
    if (
        value["schema"] != "wsorb.comparison_metrics"
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["id"] != data["id"]
        or value["limitations"] != LIMITATIONS
        or value["source"]
        != {
            "path_sha256": path_sha256,
            "engine_sha256": data["comparison"]["engine_sha256"],
            "step_s": data["comparison"]["step_s"],
        }
        or not _hash(path_sha256)
    ):
        raise ValueError("A metrics publication disagrees with its path cohort")
    runs = value["runs"]
    if (
        not isinstance(runs, list)
        or not 1 <= len(runs) <= MAX_RUNS
        or len(runs) != len(data["runs"])
    ):
        raise ValueError("A metrics publication must include every paired run within 128 runs")
    seen = set()
    by_id = {run["id"]: run for run in data["runs"]}
    for item in runs:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "algorithm",
            "seed",
            "repetition",
            "outcome",
            "engine_sha256",
            "source_sha256",
            "analytics",
            "policy",
        }:
            raise ValueError("A metrics run has unknown or missing fields")
        if not isinstance(item["id"], str) or item["id"] not in by_id or item["id"] in seen:
            raise ValueError("A metrics run has an unknown or duplicate source ID")
        run = by_id[item["id"]]
        seen.add(item["id"])
        if (
            any(
                item[key] != run[key]
                for key in ("algorithm", "seed", "repetition", "outcome", "source_sha256")
            )
            or item["engine_sha256"] != data["comparison"]["engine_sha256"]
        ):
            raise ValueError("A metrics run differs from its exact path source")
        _integer(item["seed"])
        _integer(item["repetition"], minimum=1, maximum=10)
        analytics = item["analytics"]
        if analytics is not None:
            report = validate_run_analytics(analytics)
            if (
                report is None
                or not 1 <= report["sample_count"] <= min(MAX_SAMPLES, run["source_sample_count"])
                or any(report["totals"][key] > 2**53 - 1 for key in COUNTERS)
                or not _same(report["totals"]["observed_sim_s"], run["final"]["time_s"])
            ):
                raise ValueError("A metrics run has an invalid native receipt")
            for row in report["samples"]:
                path = next((sample for sample in run["samples"] if _same(sample[0], row[0])), None)
                if path is not None and any(
                    row[native] is not None
                    and path[column] is not None
                    and not _same(row[native], path[column])
                    for native, column in ((1, 6), (6, 5), (7, 4), (8, 7))
                ):
                    raise ValueError("A native public reading differs from its path source")
        policy = item["policy"]
        if policy is None:
            continue
        if not isinstance(policy, dict) or set(policy) != set(_POLICY_KEYS):
            raise ValueError("A policy summary has unknown or missing fields")
        count = _integer(policy["sample_count"], maximum=max(0, run["source_sample_count"] - 1))
        if count == 0:
            if any(policy[key] is not None for key in _POLICY_KEYS[1:]):
                raise ValueError("Missing policy timings must remain null")
        else:
            median = _number(policy["median_policy_wall_s"], optional=False)
            maximum = _number(policy["max_policy_wall_s"], optional=False)
            over = _integer(policy["policy_calls_over_step_budget"], maximum=count)
            if median > maximum or (maximum > data["comparison"]["step_s"]) != (over > 0):
                raise ValueError("Policy timing reductions are inconsistent")
    _encode(value)
    return deepcopy(value)


def _manifest(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != {"schema", "schema_version", "artifacts"}:
        raise ValueError("A metrics manifest has unknown or missing fields")
    if (
        value["schema"] != "wsorb.comparison_metrics.artifacts"
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("A metrics manifest has an unsupported schema")
    artifacts = value["artifacts"]
    if not isinstance(artifacts, list) or len(artifacts) > len(_IDS):
        raise ValueError("A metrics manifest exceeds its allowlisted artifact set")
    seen = set()
    for entry in artifacts:
        if not isinstance(entry, dict) or set(entry) != {"id", "path", "bytes", "sha256"}:
            raise ValueError("A metrics manifest artifact has unknown or missing fields")
        identity = entry["id"]
        if (
            not isinstance(identity, str)
            or identity not in _IDS
            or identity in seen
            or entry["path"] != f"{identity}.json"
            or not _hash(entry["sha256"])
        ):
            raise ValueError("A metrics manifest artifact has an invalid identity or hash")
        _integer(entry["bytes"], minimum=1, maximum=MAX_BYTES)
        seen.add(identity)
    return deepcopy(value)


def _output_path(path: Path) -> Path:
    if _OUTPUT.is_symlink() or path.is_symlink() or path.parent.resolve() != _OUTPUT.resolve():
        raise ValueError("Metrics output must be in the separate path-metrics directory")
    if path.name not in ("official.json", "team.json", "manifest.json"):
        raise ValueError("Metrics output is not an allowlisted publication")
    return path.resolve()


def write_metrics(
    directory: str | Path,
    output: str | Path,
    *,
    comparison_id: str,
    manifest: str | Path,
) -> dict:
    """Write separate metrics and manifest, refusing unverified existing files.

    Existing paths, summaries and saved sources are never outputs. An existing
    metrics file may change only after its own manifest verifies its bytes.
    """
    if comparison_id not in _IDS:
        raise ValueError("comparison_id must be official or team")
    output = _output_path(Path(output))
    manifest_path = _output_path(Path(manifest))
    if output.name != f"{comparison_id}.json" or manifest_path.name != "manifest.json":
        raise ValueError("Metrics output and its independent manifest must be named siblings")
    source = Path(directory)
    if source.is_symlink() or not source.is_dir() or output.is_relative_to(source.resolve()):
        raise ValueError("Metrics output must be separate from its regular saved source")
    previous_manifest = _bounded_file(manifest_path, 65536) if manifest_path.exists() else None
    records = (
        _manifest(_loads(previous_manifest))
        if previous_manifest is not None
        else {"schema": "wsorb.comparison_metrics.artifacts", "schema_version": 1, "artifacts": []}
    )
    previous = {manifest_path: previous_manifest}
    for entry in records["artifacts"]:
        path = _output_path(output.parent / entry["path"])
        raw = _bounded_file(path)
        if len(raw) != entry["bytes"] or _digest(raw) != entry["sha256"]:
            raise ValueError("A metrics file changed; refusing to discard possible user work")
        data, path_bytes = _published_paths(entry["id"])
        validate_metrics(_loads(raw), data, path_sha256=_digest(path_bytes))
        previous[path] = raw
    publication = build_metrics(source, comparison_id=comparison_id)
    encoded = _encode(publication)
    old = _bounded_file(output) if output.exists() else None
    if old is not None and old != encoded and output not in previous:
        raise FileExistsError("Refusing to replace an unverified existing metrics file")
    previous[output] = old
    records["artifacts"] = sorted(
        [entry for entry in records["artifacts"] if entry["id"] != comparison_id]
        + [
            {
                "id": comparison_id,
                "path": output.name,
                "bytes": len(encoded),
                "sha256": _digest(encoded),
            }
        ],
        key=lambda entry: entry["id"],
    )
    payloads = {output: encoded, manifest_path: _encode(_manifest(records))}
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".wsorb-metrics-", dir=output.parent) as temporary:
        staged = {}
        for index, (path, raw) in enumerate(payloads.items()):
            staged[path] = Path(temporary) / str(index)
            staged[path].write_bytes(raw)
        for path, raw in previous.items():
            if path.is_symlink() or (path.read_bytes() if path.exists() else None) != raw:
                raise ValueError("A metrics output changed during publication")
        for path, raw in payloads.items():
            if raw == previous[path]:
                continue
            if previous[path] is None:
                with path.open("xb") as stream:
                    stream.write(raw)
            else:
                staged[path].replace(path)
    return publication


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Existing saved sweep")
    parser.add_argument("--id", dest="comparison_id", choices=_IDS, required=True)
    parser.add_argument("--output", type=Path, required=True, help="Separate curated metrics JSON")
    parser.add_argument("--manifest", type=Path, required=True, help="Independent sibling manifest")
    args = parser.parse_args(argv)
    try:
        write_metrics(
            args.input, args.output, comparison_id=args.comparison_id, manifest=args.manifest
        )
    except (OSError, ValueError, TypeError, OverflowError):
        parser.exit(2, "Metrics publication failed. Check the saved cohort and source hashes.\n")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
