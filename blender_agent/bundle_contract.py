"""Validate bundled wheels before importing native dependencies into Blender."""

import json
from pathlib import Path
import platform
import sys


METADATA_FILE = "_blender_agent_bundle.json"


def current_platform() -> str:
    key = (platform.system().lower(), platform.machine().lower())
    targets = {
        ("windows", "amd64"): "x86_64-pc-windows-msvc",
        ("windows", "x86_64"): "x86_64-pc-windows-msvc",
        ("windows", "arm64"): "aarch64-pc-windows-msvc",
        ("darwin", "arm64"): "aarch64-apple-darwin",
        ("darwin", "x86_64"): "x86_64-apple-darwin",
        ("linux", "x86_64"): "x86_64-manylinux_2_28",
        ("linux", "aarch64"): "aarch64-manylinux_2_28",
    }
    if key not in targets:
        raise RuntimeError(f"Unsupported Blender runtime platform: {key}")
    return targets[key]


def validate_bundle(libs_dir: Path, *, python_version=None, python_platform=None,
                    addon_version=None, requirements_sha256=None) -> dict:
    """Require an exact build target; old bundles must be rebuilt, not relabelled."""
    path = Path(libs_dir) / METADATA_FILE
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("MCP bundle metadata is missing or invalid; rebuild the add-on") from exc
    expected = {
        "schema_version": 1,
        "python_version": python_version or f"{sys.version_info.major}.{sys.version_info.minor}",
        "python_platform": python_platform or current_platform(),
    }
    if addon_version is not None:
        expected["addon_version"] = addon_version
    if requirements_sha256 is not None:
        expected["requirements_sha256"] = requirements_sha256
    for field, value in expected.items():
        if metadata.get(field) != value:
            raise RuntimeError(
                f"MCP bundle {field} mismatch: expected {value!r}, "
                f"found {metadata.get(field)!r}; rebuild for this Blender installation"
            )
    if not metadata.get("requirements_sha256") or not metadata.get("addon_version"):
        raise RuntimeError("MCP bundle provenance is missing; rebuild the add-on")
    return metadata
