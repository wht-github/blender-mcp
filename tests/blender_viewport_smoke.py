"""Interactive Blender smoke test for the viewport screenshot success path."""

from __future__ import annotations

import base64
import os
from pathlib import Path
import sys
import tempfile
import traceback
import zipfile

import bpy


def _zip_argument() -> Path:
    if "--" not in sys.argv:
        raise RuntimeError("Pass the add-on zip after '--'")
    index = sys.argv.index("--")
    return Path(sys.argv[index + 1]).resolve()


def _run_smoke(archive: Path) -> None:
    with tempfile.TemporaryDirectory(
        prefix="blender-agent-viewport-",
        ignore_cleanup_errors=True,
    ) as temp_dir:
        with zipfile.ZipFile(archive) as package_zip:
            package_zip.extractall(temp_dir)
        sys.path.insert(0, temp_dir)

        from blender_agent.builtins import viewport

        target = bpy.context.active_object
        if target is None:
            raise RuntimeError("Factory scene has no active object")

        selected_before = viewport.get_selected_objects()
        image = viewport.capture_objects([target.name], width=320, height=240)
        image_bytes = base64.b64decode(image, validate=True)
        if len(image_bytes) < 100:
            raise AssertionError("Viewport screenshot is unexpectedly small")
        if viewport.get_selected_objects() != selected_before:
            raise AssertionError("Viewport screenshot did not restore selection")

        print(
            "BLENDER_AGENT_VIEWPORT_OK "
            f"object={target.name} bytes={len(image_bytes)}"
        )
        sys.stdout.flush()


def main() -> None:
    archive = _zip_argument()

    def run_after_ui_ready():
        try:
            _run_smoke(archive)
        except BaseException:
            traceback.print_exc()
            sys.stderr.flush()
            os._exit(1)
        bpy.ops.wm.quit_blender()
        return None

    bpy.app.timers.register(run_after_ui_ready, first_interval=1.0)


if __name__ == "__main__":
    main()
