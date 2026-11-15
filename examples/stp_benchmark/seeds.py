"""Append-only scenario seed bank. Selection always reuses the saved prefix."""

from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path

from evidence import digest, write_json

BANK_SCHEMA = "wsorb.official_stp.seed_bank.v1"
SCENARIO_KEYS = ("package_revision", "mission_sha256", "scorer_sha256", "seed_scope")


def seed_digest(values):
    encoded = json.dumps(values, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_seeds(values):
    if not isinstance(values, list) or not values:
        raise ValueError("Seed bank must contain a nonempty seed list")
    if any(type(seed) is not int or not 0 <= seed < 2**32 for seed in values):
        raise ValueError("Seeds must be uint32 integers")
    if len(set(values)) != len(values):
        raise ValueError("Seed bank contains duplicate seeds")


def appended_seed(bank_id, index, used):
    """Stable across machines/Python versions; no dependence on random global state."""
    nonce = 0
    while True:
        material = f"wsorb.seed-bank.sha256-index-v1\0{bank_id}\0{index}\0{nonce}"
        value = int.from_bytes(hashlib.sha256(material.encode("utf-8")).digest()[:4], "big")
        if value not in used:
            return value
        nonce += 1


@contextmanager
def bank_lock(bank_path):
    """Fail explicitly on competing writers; never wait with fixed delays or retry."""
    with bank_path.with_suffix(bank_path.suffix + ".lock").open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def select_seeds(bank_path, config, count):
    if type(count) is not int or not 1 <= count <= 2**32:
        raise ValueError("seed_count must be a positive integer no larger than the uint32 space")
    bank_path = Path(bank_path)
    # A missing bank is an error: never silently replace a previously saved history.
    if not bank_path.is_file():
        raise FileNotFoundError(f"Saved seed bank is missing: {bank_path}")
    scenario = {key: config[key] for key in SCENARIO_KEYS}
    with bank_lock(bank_path):
        bank = json.loads(bank_path.read_text(encoding="utf-8"))
        if bank.get("schema") != BANK_SCHEMA or bank.get("id") != config["seed_bank_id"]:
            raise ValueError("Seed bank schema or identity differs from the campaign")
        if bank.get("scenario") != scenario:
            raise ValueError("Seed bank belongs to a different mission, scorer or seed scope")
        if bank.get("extension_algorithm") != "sha256-index-v1":
            raise ValueError("Unsupported seed bank extension algorithm")
        values = bank["seeds"]
        validate_seeds(values)
        prefix_count = config["seed_bank_prefix_count"]
        if type(prefix_count) is not int or not 1 <= prefix_count <= len(values):
            raise ValueError("Seed bank has lost its pinned original prefix")
        if seed_digest(values[:prefix_count]) != config["seed_bank_prefix_sha256"]:
            raise ValueError("Previously declared seeds changed or were reordered")
        # Validate each saved extension against its stable index, including collision
        # handling. Even restoring an older bank regenerates the same later values.
        used = set(values[:prefix_count])
        for index in range(prefix_count, len(values)):
            if values[index] != appended_seed(bank["id"], index, used):
                raise ValueError(
                    "Saved seed extension changed; restore its original ordered values"
                )
            used.add(values[index])
        old_count = len(values)
        for index in range(old_count, count):
            value = appended_seed(bank["id"], index, used)
            values.append(value)
            used.add(value)
        if len(values) != old_count:
            write_json(bank_path, bank)
        selection = dict(
            schema=BANK_SCHEMA,
            bank_id=bank["id"],
            bank_path=str(bank_path.resolve()),
            bank_sha256=digest(bank_path),
            scenario=scenario,
            requested_count=count,
            seeds=values[:count],
            selected_seeds_sha256=seed_digest(values[:count]),
            saved_count=len(values),
            appended_count=len(values) - old_count,
        )
        return selection, bank
