"""Native Windows official-STP campaign CLI. Run with the STP Python via UV."""

from __future__ import annotations

import argparse
import ctypes
import datetime
import importlib.metadata
import json
import os
import platform
import queue
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
from contextlib import suppress
from ctypes import wintypes
from pathlib import Path

from evidence import SCHEMA, analyze, digest, schedule, write_json

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OFFICIAL_REVISION = "0d753a28e1a4265b2839bf8e9fbbc9194efafb87"


def utc():
    return datetime.datetime.now(datetime.UTC).isoformat()


def path(value):
    value = Path(os.path.expandvars(value)).expanduser()
    return value.resolve() if value.is_absolute() else (REPO / value).resolve()


def hashes(root):
    return {
        str(p.relative_to(root)).replace("\\", "/"): digest(p)
        for p in sorted(root.rglob("*"))
        if p.is_file() and "__pycache__" not in p.parts and ".git" not in p.parts
    }


def revision(root):
    """Read provenance without changing Git/Jujutsu state; dirty source hashes are authoritative."""
    try:
        head = (root / ".git" / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref = head[5:]
            direct = root / ".git" / ref
            if direct.exists():
                return direct.read_text().strip()
            for line in (root / ".git" / "packed-refs").read_text().splitlines():
                if line.endswith(" " + ref):
                    return line.split()[0]
            return None
        return head
    except OSError:
        return None


def resolve_asset(value, install, user_root, package):
    if value.startswith("Local/Repos/STU4-Sim/"):
        return package / value.removeprefix("Local/Repos/STU4-Sim/")
    if value.startswith("Local/"):
        return user_root / "Assets" / value.removeprefix("Local/")
    if value.startswith("Core/Compute_Server/"):
        return install / value.removeprefix("Core/")
    if value.startswith("Core/SharedData/"):
        return install / value.removeprefix("Core/")
    if value.startswith("Core/"):
        return install / "Compute_Server" / "packages" / value.removeprefix("Core/")
    if value.startswith("SharedData/"):
        return install / value
    if Path(value).is_absolute():
        return Path(value)
    if "/bin/" in value:
        return install / "Compute_Server" / "packages" / value
    return None


def map_assets(value, package):
    if isinstance(value, str) and value.startswith("Local/Repos/STU4-Sim/"):
        return (package / value.removeprefix("Local/Repos/STU4-Sim/")).as_posix()
    if isinstance(value, list):
        return [map_assets(v, package) for v in value]
    if isinstance(value, dict):
        return {k: map_assets(v, package) for k, v in value.items()}
    return value


def dependency_records(mission, install, user_root, package):
    """Hash transitively referenced config/data and record missing assets explicitly."""
    found = {}

    def visit(value):
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str):
            candidate = resolve_asset(value, install, user_root, package)
            if candidate is None or str(candidate) in found:
                return
            if not candidate.is_file() and candidate.suffix == "":
                candidate = candidate.with_suffix(".dll")
            key = str(candidate)
            if key in found:
                return
            found[key] = (
                dict(sha256=digest(candidate)) if candidate.is_file() else dict(missing=True)
            )
            if candidate.is_file() and candidate.suffix in {".simconf", ".enconf"}:
                visit(json.loads(candidate.read_text(encoding="utf-8-sig")))

    visit(mission)
    return found


def prepare(config_path, output, mode):
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    if config.get("schema") != SCHEMA:
        raise ValueError("Unsupported campaign schema")
    attempts = schedule(config, mode)
    output = Path(output).resolve()
    if not output.is_relative_to(REPO / "runs"):
        raise ValueError(
            "Raw campaign evidence must stay under the ignored repository runs/ directory"
        )
    # Fresh evidence root only: never overwrite or resume an interrupted attempt.
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / "preparation.json", dict(started_utc=utc(), config=config, mode=mode))
    install, user_root = path(config["install"]), path(config["user_root"])
    package = path(config["package"])
    mission_path = package / config["mission"]
    scorer = package / "Systems" / "Scoring.py"
    if digest(mission_path) != config["mission_sha256"]:
        raise ValueError("Mission hash differs from campaign pin; review a new configuration")
    if digest(scorer) != config["scorer_sha256"]:
        raise ValueError("Scorer hash differs from reviewed formula; review the adapter first")
    if config["package_revision"] != OFFICIAL_REVISION:
        raise ValueError("Unreviewed package revision: inspect and update the adapter explicitly")
    for limit in ("simulation_s", "wall_s", "startup_s", "stall_s", "controller_stall_s"):
        if config["limits"][limit] <= 0:
            raise ValueError(f"Invalid {limit}")
    if config["sample_period_s"] <= 0:
        raise ValueError("sample_period_s must be positive")
    snapshot = output / "snapshot"
    snapshot.mkdir()
    frozen_package = snapshot / "STU4-Sim"
    shutil.copytree(package, frozen_package, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    for controller in config["controllers"]:
        folder = snapshot / "controllers" / controller["id"]
        shutil.copytree(
            path(controller["directory"]),
            folder,
            ignore=shutil.ignore_patterns("__pycache__", "cam_*.png", "controller_log.txt"),
        )
        for extra in controller.get("extra_files", []):
            shutil.copy2(path(extra), folder / Path(extra).name)
        if not (folder / controller["entrypoint"]).is_file():
            raise ValueError(f"Missing entrypoint for {controller['id']}")
    for source in HERE.glob("*.py"):
        shutil.copy2(source, snapshot / source.name)
    mission = json.loads(mission_path.read_text(encoding="utf-8-sig"))
    systems = mission["SystemManager"]["Systems"]
    allowed = {
        "ApplyGravity",
        "JetSelectionActuator",
        "Controller",
        "Radar",
        "Scoring",
        "SpawnDebris",
    }
    if {s["Nametag"] for s in systems if s["Active"]} != allowed:
        raise ValueError("Unexpected active systems; review command ownership before running")
    if mission["DataManager"]["SimEntity"]["DoSpawning"]:
        raise ValueError("Use a saved debris mission; runtime spawning blocks original controllers")
    dependencies = dependency_records(mission, install, user_root, frozen_package)
    binary_paths = [
        install / "Compute_Server" / "run_Server.exe",
        install / "SpaceCRAFT" / "SpaceCRAFT.exe",
        install / "SpaceCRAFT/SpaceCRAFT/Binaries/Win64/SpaceCRAFT-Win64-Shipping.exe",
        user_root / "Versions/v0.54.2/py/Lib/site-packages/spaceteams.cp312-win_amd64.pyd",
    ]
    provenance = dict(
        schema=SCHEMA,
        created_utc=utc(),
        configuration=config,
        package_revision=config["package_revision"],
        repository_revision=revision(REPO),
        snapshot_hashes=hashes(snapshot),
        mission_sha256=digest(mission_path),
        scorer_sha256=digest(scorer),
        dependencies=dependencies,
        binaries={str(p): digest(p) for p in binary_paths},
        release=(install / "ReleaseVersion_0.54.2_Win64_User.txt").read_text(),
        python=sys.version,
        executable=sys.executable,
        python_dependencies={
            d.metadata["Name"]: d.version for d in importlib.metadata.distributions()
        },
        machine=dict(
            hostname=platform.node(),
            platform=platform.platform(),
            cpu=platform.processor(),
            logical_cpus=os.cpu_count(),
        ),
        scheduling="normal; no -det; original Python loop pacing; Unreal Chaos",
        observation=(
            "Python call profiler; read-only sequential SDK state samples; "
            "original source unchanged"
        ),
        limitations=[
            "Profiler, camera, GUI and logging affect wall scheduling",
            "No native collision identity/time stream has been established",
            "Scorer does not terminate the simulation; runner stops after its first receipt",
            "Fixed saved field; experimental seeds vary initial impulses and radar noise",
        ],
    )
    write_json(output / "provenance.json", provenance)
    for attempt in attempts:
        attempt["run_id"] = str(uuid.uuid4())
    campaign = dict(
        schema=SCHEMA,
        mode=mode,
        config=config,
        attempts=attempts,
        prepared_utc=utc(),
        preparation_only=True,
    )
    write_json(output / "campaign.json", campaign)
    print(f"Prepared {len(attempts)} attempts at {output}; no STP sessions launched", flush=True)


class WindowsJob:
    """Own the whole flight process tree; termination is confined to this job."""

    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("Official competition flights require native Windows")
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self.kernel
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("process_time", ctypes.c_int64),
                ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD),
                ("min_ws", ctypes.c_size_t),
                ("max_ws", ctypes.c_size_t),
                ("active_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD),
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("basic", BasicLimits),
                ("io", ctypes.c_uint64 * 6),
                ("process_mem", ctypes.c_size_t),
                ("job_mem", ctypes.c_size_t),
                ("peak_process", ctypes.c_size_t),
                ("peak_job", ctypes.c_size_t),
            ]

        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())
        k.CreateIoCompletionPort.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.c_size_t,
            wintypes.DWORD,
        ]
        k.CreateIoCompletionPort.restype = wintypes.HANDLE
        k.GetQueuedCompletionStatus.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.DWORD,
        ]
        self.port = k.CreateIoCompletionPort(wintypes.HANDLE(-1), None, 0, 1)
        if not self.port:
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

        class CompletionPort(ctypes.Structure):
            _fields_ = [("key", ctypes.c_void_p), ("port", wintypes.HANDLE)]

        completion = CompletionPort(None, self.port)
        if not k.SetInformationJobObject(
            self.handle, 7, ctypes.byref(completion), ctypes.sizeof(completion)
        ):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def launch(self, command, cwd, env, stdout):
        # Assign before the first instruction: no child can escape before job ownership.
        process = subprocess.Popen(
            command, cwd=cwd, env=env, stdout=stdout, stderr=subprocess.STDOUT, creationflags=0x4
        )  # CREATE_SUSPENDED
        try:
            if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            ntdll = ctypes.WinDLL("ntdll")
            ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
            ntdll.NtResumeProcess.restype = ctypes.c_long
            status = ntdll.NtResumeProcess(int(process._handle))
            if status != 0:
                raise OSError(f"NtResumeProcess failed: {status}")
        except BaseException:
            process.kill()
            process.wait(timeout=5)
            raise
        return process

    def active(self):
        class Accounting(ctypes.Structure):
            _fields_ = [
                ("times", ctypes.c_int64 * 4),
                ("faults", wintypes.DWORD),
                ("total", wintypes.DWORD),
                ("active", wintypes.DWORD),
                ("terminated", wintypes.DWORD),
            ]

        info = Accounting()
        if not self.kernel.QueryInformationJobObject(
            self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.active

    def close(self):
        if self.handle:
            try:
                if not self.kernel.TerminateJobObject(self.handle, 0):
                    raise ctypes.WinError(ctypes.get_last_error())
                deadline = time.monotonic() + 10
                while self.active():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Owned STP process tree did not exit")
                    port = getattr(self, "port", None)
                    if not port:
                        raise RuntimeError("Missing process-completion port")
                    message, key, overlapped = (
                        wintypes.DWORD(),
                        ctypes.c_size_t(),
                        ctypes.c_void_p(),
                    )
                    self.kernel.GetQueuedCompletionStatus(
                        port,
                        ctypes.byref(message),
                        ctypes.byref(key),
                        ctypes.byref(overlapped),
                        max(1, int(remaining * 1000)),
                    )
            finally:
                self.kernel.CloseHandle(self.handle)
                self.handle = None
                if getattr(self, "port", None):
                    self.kernel.CloseHandle(self.port)
                    self.port = None


class Receiver:
    def __init__(self):
        self.events = queue.Queue()
        self.token = secrets.token_hex(32)
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.server.listen()
        self.connections = []
        self.closed = False
        self.server.settimeout(0.25)  # Bounded socket readiness poll, not a fixed delay.
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def accept(self):
        while not self.closed:
            try:
                connection, _ = self.server.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections.append(connection)
            threading.Thread(target=self.read, args=(connection,), daemon=True).start()

    def read(self, connection):
        role = None
        try:
            connection.settimeout(10)
            stream = connection.makefile("r", encoding="utf-8")
            hello = json.loads(stream.readline(4096))
            if not secrets.compare_digest(hello.get("token", ""), self.token):
                return
            role = hello["role"]
            if role not in {"controller", "scorer", "radar", "spawner"}:
                return
            connection.settimeout(None)
            for line in stream:
                if len(line) > 4_000_000:
                    raise ValueError("Oversized evidence record")
                record = json.loads(line)
                if record.get("role") != role:
                    raise ValueError("Evidence role mismatch")
                self.events.put(record)
        except Exception:
            if not self.closed:
                self.events.put(dict(kind="hook_error", role=role, error=traceback.format_exc()))
        finally:
            connection.close()
            if role and not self.closed:
                self.events.put(dict(kind="stream_closed", role=role))

    def close(self):
        self.closed = True
        self.server.close()
        for connection in self.connections:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            connection.close()
        self.thread.join(timeout=1)


def seed_copy(source, target, seed, role):
    text = source.read_text(encoding="utf-8")
    anchor = "seed = 100" if role == "spawner" else "np.random.default_rng(100)"
    replacement = f"seed = {seed}" if role == "spawner" else f"np.random.default_rng({seed})"
    if text.count(anchor) != 1:
        raise ValueError(f"Seed adapter anchor changed in {source}; refusing an unreviewed patch")
    target.write_text(text.replace(anchor, replacement), encoding="utf-8")


def attempt_mission(root, attempt, receiver, config):
    folder = root / attempt["run_id"]
    frozen = root / "snapshot"
    package = frozen / "STU4-Sim"
    mission = json.loads((package / config["mission"]).read_text(encoding="utf-8-sig"))
    mission = map_assets(mission, package)
    launchers = folder / "launchers"
    launchers.mkdir()
    # Controllers may save images/logs beside __file__. Each attempt gets a fresh copy.
    controller = attempt["controller"]
    controller_dir = folder / "controller"
    shutil.copytree(frozen / "controllers" / controller["id"], controller_dir)
    changes = []
    for system in mission["SystemManager"]["Systems"]:
        role = {
            "Controller": "controller",
            "Scoring": "scorer",
            "Radar": "radar",
            "SpawnDebris": "spawner",
        }.get(system["Nametag"])
        if role is None:
            continue
        original_source = Path(system["Source"])
        source = (
            controller_dir / controller["entrypoint"] if role == "controller" else original_source
        )
        if attempt["seed"] is not None and role in {"radar", "spawner"}:
            modified = launchers / f"seeded_{role}.py"
            seed_copy(source, modified, attempt["seed"], role)
            changes.append(
                dict(
                    role=role,
                    original_sha256=digest(source),
                    seeded_sha256=digest(modified),
                    seed=attempt["seed"],
                )
            )
            source = modified
        spec = dict(
            source=str(source),
            role=role,
            port=receiver.port,
            token=receiver.token,
            sample_period_s=config["sample_period_s"],
        )
        write_json(launchers / f"{role}.json", spec)
        wrapper = launchers / f"{role}.py"
        wrapper.write_text(
            "import sys\n"
            + f"sys.path.insert(0, {str(frozen)!r})\n"
            + "from capture import run\n"
            + f"run({str(launchers / (role + '.json'))!r})\n",
            encoding="utf-8",
        )
        system["Source"] = wrapper.as_posix()
        changes.append(
            dict(
                role=role,
                source=str(source),
                source_sha256=digest(source),
                launch_adapter_sha256=digest(wrapper),
            )
        )
    mission_path = folder / "mission.simconf"
    write_json(mission_path, mission)
    return mission_path, changes


def execute(root, attempt):
    campaign = json.loads((root / "campaign.json").read_text(encoding="utf-8"))
    config = campaign["config"]
    limits = config["limits"]
    folder = root / attempt["run_id"]
    folder.mkdir(exist_ok=False)
    started = time.monotonic()
    receipt = dict(
        schema=SCHEMA,
        run_id=attempt["run_id"],
        outcome="failed_start",
        official_score=None,
        inputs=None,
        termination_reason="preparing",
        started_utc=utc(),
    )
    write_json(folder / "receipt.json", receipt)
    receiver, job, process = None, None, None
    native_directories = set()
    user_root = path(config["user_root"])
    install = path(config["install"])
    native_roots = [
        user_root / "Logs",
        user_root / "Output",
        install / "SpaceCRAFT/SpaceCRAFT/Saved/Logs",
    ]
    previous_files = {
        str(p): (p.stat().st_mtime_ns, p.stat().st_size)
        for base in native_roots
        if base.is_dir()
        for p in base.rglob("*")
        if p.is_file()
    }
    seq = 0
    with (folder / "events.jsonl").open("w", encoding="utf-8", buffering=1) as events:

        def record(event):
            nonlocal seq
            event.update(seq=seq, received_utc=utc())
            seq += 1
            events.write(json.dumps(event, allow_nan=False) + "\n")
            return event

        try:
            receiver = Receiver()
            mission_path, changes = attempt_mission(root, attempt, receiver, config)
            command = [
                str(install / "SpaceCRAFT" / "SpaceCRAFT.exe"),
                f"--sim={mission_path.as_posix()}",
                "-firstRole",
                "-quitOnStop",
            ]
            env = os.environ.copy()
            # Explicit real Windows app-data location; never use Codex's virtualized directory.
            env["LOCALAPPDATA"] = str(user_root.parent.parent)
            manifest = dict(
                schema=SCHEMA,
                attempt=attempt,
                command=command,
                started_utc=utc(),
                campaign_provenance="../provenance.json",
                mission_sha256=digest(mission_path),
                controller_initial_hashes=hashes(folder / "controller"),
                changes=changes,
                effective_seeds=dict(
                    spawner=attempt["seed"] if attempt["seed"] is not None else 100,
                    radar=attempt["seed"] if attempt["seed"] is not None else 100,
                ),
                seed_scope="saved field initial impulses and radar noise; fixed debris layout",
                settings=attempt["controller"].get("environment", {}),
                limits=limits,
                normal_scheduling=True,
                diagnostic_rerun=config.get("diagnostic_rerun", False),
                native_run_uuid=None,
            )
            # Configuration may select existing controller settings; never rewrite its policy.
            env.update(attempt["controller"].get("environment", {}))
            write_json(folder / "manifest.json", manifest)
            job = WindowsJob()
            with (folder / "launcher.log").open("wb") as output:
                process = job.launch(command, install / "SpaceCRAFT", env, output)

                def completed():
                    receiver.events.put(dict(kind="launcher_exit", code=process.wait()))

                threading.Thread(target=completed, daemon=True).start()
                connected = set()
                last_progress = started
                last_sim = None
                last_controller = started
                radar_origin = None
                seen_native_uuid = None
                while True:
                    now = time.monotonic()
                    deadline = started + limits["wall_s"]
                    if "controller" not in connected or "scorer" not in connected:
                        deadline = min(deadline, started + limits["startup_s"])
                    else:
                        deadline = min(
                            deadline,
                            last_progress + limits["stall_s"],
                            last_controller + limits["controller_stall_s"],
                        )
                    try:
                        event = receiver.events.get(timeout=max(0, deadline - now))
                    except queue.Empty:
                        now = time.monotonic()
                        if now >= started + limits["wall_s"]:
                            reason = "wall_timeout"
                        elif "controller" not in connected or "scorer" not in connected:
                            reason = "startup_timeout"
                        elif now >= last_controller + limits["controller_stall_s"]:
                            reason = "controller_stall"
                        else:
                            reason = "simulator_stall"
                        receipt.update(outcome=reason, termination_reason=reason)
                        record(dict(kind=reason, monotonic_s=now))
                        break
                    kind = event["kind"]
                    if kind == "radar_clock" and event.get("origin_tai"):
                        radar_origin = datetime.datetime.fromisoformat(event["origin_tai"])
                    if kind == "telemetry" and radar_origin is not None:
                        times = event.get("sensor_timestamps_since_radar_origin_s", [])
                        if times:
                            stamp = datetime.datetime.fromisoformat(event["sim_tai"])
                            event["radar_origin_tai"] = radar_origin.isoformat()
                            event["sensor_age_s"] = max(
                                (stamp - radar_origin).total_seconds() - t for t in times
                            )
                    record(event)
                    if kind == "connected":
                        connected.add(event["role"])
                        native_uuid = event["native_run_uuid"]
                        if seen_native_uuid is not None and native_uuid != seen_native_uuid:
                            raise RuntimeError("Systems connected to different native missions")
                        seen_native_uuid = native_uuid
                        manifest["native_run_uuid"] = native_uuid
                        write_json(folder / "manifest.json", manifest)
                        for key in ("native_logs", "native_output"):
                            native_directories.add(event[key])
                        print(f"{attempt['run_id']} {event['role']} connected", flush=True)
                    if kind == "command":
                        last_controller = time.monotonic()
                    if kind == "scorer_progress":
                        sim = event.get("scorer_time_s")
                        if sim is not None and sim != last_sim:
                            last_progress, last_sim = time.monotonic(), sim
                        if sim is not None and sim >= limits["simulation_s"]:
                            receipt.update(
                                outcome="simulation_limit", termination_reason="simulation_limit"
                            )
                            break
                    if kind == "telemetry":
                        sim = event.get("sim_s")
                        if sim is not None and sim >= limits["simulation_s"]:
                            receipt.update(
                                outcome="simulation_limit", termination_reason="simulation_limit"
                            )
                            break
                    if kind == "official_receipt":
                        receipt.update(
                            {
                                k: v
                                for k, v in event.items()
                                if k not in {"seq", "kind", "role", "process_id"}
                            }
                        )
                        break
                    if kind in {"exception", "hook_error", "stream_closed"}:
                        receipt.update(
                            outcome="exception" if kind == "exception" else "evidence_failure",
                            termination_reason=kind,
                            error=event,
                        )
                        break
                    if kind == "launcher_exit" and event["code"] != 0:
                        receipt.update(
                            outcome="failed_start" if not connected else "simulator_crash",
                            termination_reason="launcher_exit",
                            launcher_exit_code=event["code"],
                        )
                        break
        except KeyboardInterrupt:
            receipt.update(outcome="aborted", termination_reason="owner_interrupt")
            raise
        except Exception:
            receipt.update(termination_reason="runner_exception", error=traceback.format_exc())
            record(dict(kind="exception", error=traceback.format_exc()))
        finally:
            cleanup_errors = []
            if job:
                try:
                    receipt["owned_processes_before_cleanup"] = job.active()
                    # Only terminate this attempt's job. Never taskkill by global image name.
                    job.close()
                    receipt["owned_processes_after_cleanup"] = 0
                except Exception:
                    cleanup_errors.append(traceback.format_exc())
            if process:
                try:
                    process.wait(timeout=10)
                    receipt["launcher_exit_code"] = process.returncode
                except Exception:
                    cleanup_errors.append(traceback.format_exc())
            if receiver:
                receiver.close()
                while not receiver.events.empty():
                    record(receiver.events.get_nowait())
            # Snapshot native logs/output before another attempt can trigger STP retention.
            for index, directory in enumerate(sorted(native_directories)):
                origin = Path(directory)
                if origin.is_dir():
                    try:
                        shutil.copytree(origin, folder / "native" / f"{index}-{origin.name}")
                    except Exception:
                        cleanup_errors.append(traceback.format_exc())
                else:
                    receipt.setdefault("missing_native_directories", []).append(directory)
            # Startup may fail before any system connects. Preserve newly created/changed
            # native files as well, including Unreal's startup and crash diagnostics.
            for base_index, base in enumerate(native_roots):
                if not base.is_dir():
                    continue
                for native_file in base.rglob("*"):
                    if not native_file.is_file():
                        continue
                    try:
                        metadata = (native_file.stat().st_mtime_ns, native_file.stat().st_size)
                        if previous_files.get(str(native_file)) != metadata:
                            destination = (
                                folder
                                / "native_startup"
                                / str(base_index)
                                / native_file.relative_to(base)
                            )
                            destination.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(native_file, destination)
                    except Exception:
                        cleanup_errors.append(traceback.format_exc())
            if cleanup_errors:
                receipt["cleanup_errors"] = cleanup_errors
            receipt.update(
                finished_utc=utc(),
                wall_s=time.monotonic() - started,
                cleanup="failed_or_incomplete" if cleanup_errors else "owned_process_tree_exited",
                evidence_bytes=sum(p.stat().st_size for p in folder.rglob("*") if p.is_file()),
            )
            write_json(folder / "receipt.json", receipt)
    print(f"{attempt['run_id']} {receipt['outcome']} score={receipt['official_score']}", flush=True)
    return receipt


def run_campaign(root, max_attempts=None):
    if os.name != "nt":
        raise RuntimeError("Use native Windows, not WSL")
    root = Path(root).resolve()
    campaign = json.loads((root / "campaign.json").read_text(encoding="utf-8"))
    provenance = json.loads((root / "provenance.json").read_text(encoding="utf-8"))
    if campaign["config"] != provenance["configuration"]:
        raise ValueError("Prepared configuration changed; create a new campaign")
    if hashes(root / "snapshot") != provenance["snapshot_hashes"]:
        raise ValueError("Prepared snapshot changed; create a new campaign")
    for binary, expected in provenance["binaries"].items():
        if digest(binary) != expected:
            raise ValueError(f"Native STP binary changed: {binary}")
    import msvcrt

    lock_path = REPO / "runs" / ".official-stp-flight.lock"
    with lock_path.open("a+b") as lock:
        lock.seek(0)
        if not lock.read(1):
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            pending = [a for a in campaign["attempts"] if not (root / a["run_id"]).exists()]
            if max_attempts is not None:
                if max_attempts < 1:
                    raise ValueError("max-attempts must be positive")
                pending = pending[:max_attempts]
            for attempt in pending:
                for dependency, expected in provenance["dependencies"].items():
                    if "sha256" in expected and digest(dependency) != expected["sha256"]:
                        raise ValueError(f"Native mission dependency changed: {dependency}")
                receipt = execute(root, attempt)
                if receipt.get("cleanup_errors"):
                    print(
                        "Campaign halted: cleanup/evidence preservation needs investigation",
                        flush=True,
                    )
                    break
                if receipt["outcome"] in {
                    "failed_start",
                    "startup_timeout",
                    "evidence_failure",
                    "runner_exception",
                    "simulator_crash",
                    "controller_stall",
                    "simulator_stall",
                    "exception",
                }:
                    print(
                        "Campaign halted for investigation; no attempt retried or replaced",
                        flush=True,
                    )
                    break
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            analyze(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("prepare")
    plan.add_argument("--config", type=Path, default=HERE / "campaign.json")
    plan.add_argument("--mode", choices=["pilot", "baseline", "seeded"], required=True)
    plan.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("campaign", type=Path)
    run.add_argument("--max-attempts", type=int)
    report = commands.add_parser("analyze")
    report.add_argument("campaign", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.config, args.output, args.mode)
    elif args.command == "run":
        run_campaign(args.campaign, args.max_attempts)
    else:
        print(json.dumps(analyze(args.campaign), indent=2))


if __name__ == "__main__":
    main()
