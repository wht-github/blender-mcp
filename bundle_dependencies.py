#!/usr/bin/env python3
"""Bundle the locked MCP runtime into blender_agent/libs for Blender."""

from __future__ import annotations

import argparse
import json
import hashlib
import importlib.util
import tempfile
import tomllib
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parent
LIBS_DIR = ROOT / "blender_agent" / "libs"
REQUIREMENTS = ROOT / "runtime-requirements.txt"


def _load_contract():
    path = ROOT / "blender_agent" / "bundle_contract.py"
    spec = importlib.util.spec_from_file_location("blender_agent_bundle_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_contract = _load_contract()
default_python_platform = _contract.current_platform


def bundle_metadata(python_version: str, python_platform: str) -> dict:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return {
        "schema_version": 1,
        "addon_version": project["version"],
        "python_version": python_version,
        "python_platform": python_platform,
        "requirements_sha256": hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest(),
    }


def validate_existing_bundle(python_version: str, python_platform: str | None = None) -> dict:
    expected = bundle_metadata(python_version, python_platform or default_python_platform())
    return _contract.validate_bundle(
        LIBS_DIR, **{key: value for key, value in expected.items() if key != "schema_version"}
    )


def bundle_runtime_dependencies(
    python_version: str = "3.13",
    python_platform: str | None = None,
) -> Path:
    """Install locked binary wheels into the add-on's importable libs directory."""
    if not REQUIREMENTS.exists():
        raise FileNotFoundError(f"Missing locked requirements file: {REQUIREMENTS}")

    target_platform = python_platform or default_python_platform()
    metadata = bundle_metadata(python_version, target_platform)
    # Install completely before replacing a previously usable bundle.
    with tempfile.TemporaryDirectory(prefix=".blender-agent-deps-", dir=ROOT) as staging:
        target = Path(staging) / "libs"
        command = [
            "uv", "pip", "install", "--target", str(target),
            "--requirements", str(REQUIREMENTS),
            "--python-version", python_version, "--python-platform", target_platform,
            "--only-binary", ":all:", "--no-compile-bytecode", "--strict",
        ]
        print(f"Bundling MCP runtime for Python {python_version} / {target_platform}")
        subprocess.run(command, cwd=ROOT, check=True)
        (target / _contract.METADATA_FILE).write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8",
        )
        expected_path = ROOT.resolve() / "blender_agent" / "libs"
        if LIBS_DIR.resolve() != expected_path:
            raise RuntimeError("Refusing to replace a dependency directory outside the add-on")
        if LIBS_DIR.exists():
            shutil.rmtree(LIBS_DIR)
        shutil.move(str(target), str(LIBS_DIR))

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
