"""Allowlisted source-checkout imports, never SDK, display or arbitrary scripts."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from threading import RLock
from types import ModuleType

import numpy as np

ROOT = Path(__file__).resolve().parents[3]
_LOCK = RLock()
_LOADED: dict[str, dict[str, ModuleType]] = {}
_MODULES = {
    "bui": (
        "mvp",
        (
            "config",
            "accel_controller",
            "attitude",
            "state_estimator",
            "trajectory",
            "avoidance",
            "guidance",
        ),
    ),
    "tom": ("tom-mvp", ("debris_tracker", "avoidance")),
}


def require_template_source() -> None:
    if not (ROOT / "examples/tom-mvp/UserAPI.py").is_file():
        raise ValueError(
            "Body +Z template requires the source checkout: examples/tom-mvp/UserAPI.py"
        )


def load_math(profile: str) -> dict[str, ModuleType]:
    """Load only reviewed math modules under a collision-free private namespace.

    No source directory enters sys.path. Relative Bui imports resolve only to
    these preloaded modules; the private package has no filesystem search path.
    A wheel without examples must fail honestly rather than substitute an SDK.
    """
    if profile not in _MODULES:
        raise ValueError("Unknown reviewed controller math profile")
    with _LOCK:
        folder, names = _MODULES[profile]
        paths = {name: ROOT / "examples" / folder / f"{name}.py" for name in names}
        if any(not path.is_file() for path in paths.values()):
            raise ValueError(f"Controller requires the source checkout: examples/{folder}")
        if profile in _LOADED:
            return dict(_LOADED[profile])
        package_name = f"{__package__}._source_{profile}_bounded"
        package = ModuleType(package_name)
        package.__package__ = package_name
        package.__path__ = []
        sys.modules[package_name] = package
        modules = {}
        try:
            for name, path in paths.items():
                qualified = f"{package_name}.{name}"
                spec = importlib.util.spec_from_file_location(qualified, path)
                if spec is None or spec.loader is None:
                    raise ValueError("Reviewed controller math source could not be loaded")
                module = importlib.util.module_from_spec(spec)
                sys.modules[qualified] = module
                spec.loader.exec_module(module)
                setattr(package, name, module)
                modules[name] = module
                if profile == "bui" and name == "config":
                    # Isolated profile configuration, before importing any classes.
                    module.ACC_MAX = np.nextafter(0.25, 0.0)
                    module.ANG_ACC_MAX = 0.05
        except BaseException:
            for name in names:
                sys.modules.pop(f"{package_name}.{name}", None)
            sys.modules.pop(package_name, None)
            raise
        _LOADED[profile] = modules
        return dict(modules)
