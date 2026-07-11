#!/usr/bin/env python3
"""Bundle the locked MCP runtime into blender_agent/libs for Blender."""

from __future__ import annotations

import argparse
import json
import platform as host_platform
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parent
LIBS_DIR = ROOT / "blender_agent" / "libs"
REQUIREMENTS = ROOT / "runtime-requirements.txt"


def default_python_platform() -> str:
    system = host_platform.system().lower()
    machine = host_platform.machine().lower()
    mapping = {
        ("windows", "amd64"): "x86_64-pc-windows-msvc",
        ("windows", "x86_64"): "x86_64-pc-windows-msvc",
        ("windows", "arm64"): "aarch64-pc-windows-msvc",
        ("darwin", "arm64"): "aarch64-apple-darwin",
        ("darwin", "x86_64"): "x86_64-apple-darwin",
        ("linux", "x86_64"): "x86_64-manylinux_2_28",
        ("linux", "aarch64"): "aarch64-manylinux_2_28",
    }
    try:
        return mapping[(system, machine)]
    except KeyError as exc:
        raise RuntimeError(
            f"Unsupported build host {system}/{machine}; pass --python-platform explicitly"
        ) from exc


def bundle_runtime_dependencies(
    python_version: str = "3.13",
    python_platform: str | None = None,
) -> Path:
    """Install locked binary wheels into the add-on's importable libs directory."""
    if not REQUIREMENTS.exists():
        raise FileNotFoundError(f"Missing locked requirements file: {REQUIREMENTS}")

    target_platform = python_platform or default_python_platform()
    if LIBS_DIR.exists():
        shutil.rmtree(LIBS_DIR)
    LIBS_DIR.mkdir(parents=True)

    command = [
        "uv",
        "pip",
        "install",
        "--target",
        str(LIBS_DIR),
        "--requirements",
        str(REQUIREMENTS),
        "--python-version",
        python_version,
        "--python-platform",
        target_platform,
        "--only-binary",
        ":all:",
        "--no-compile-bytecode",
        "--strict",
    ]
    print(f"Bundling MCP runtime for Python {python_version} / {target_platform}")
    subprocess.run(command, cwd=ROOT, check=True)

    metadata = {
        "python_version": python_version,
        "python_platform": target_platform,
        "requirements": REQUIREMENTS.name,
    }
    (LIBS_DIR / "_blender_agent_bundle.json").write_text(
        json.dumps(metadata, indent=2) + "\n",
        encoding="utf-8",
    )

    file_count = sum(1 for path in LIBS_DIR.rglob("*") if path.is_file())
    size_mb = sum(path.stat().st_size for path in LIBS_DIR.rglob("*") if path.is_file()) / 1024**2
    print(f"Bundled {file_count} files ({size_mb:.1f} MiB) into {LIBS_DIR}")
    return LIBS_DIR


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python-version", default="3.13")
    parser.add_argument("--python-platform", default=None)
    args = parser.parse_args()
    bundle_runtime_dependencies(args.python_version, args.python_platform)
