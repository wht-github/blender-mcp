"""Check real Blender document-load handlers in an isolated background process."""

from pathlib import Path
import sys
import tempfile
import zipfile

import bpy


def main():
    archive = Path(sys.argv[sys.argv.index("--") + 1]).resolve()
    with tempfile.TemporaryDirectory(prefix="blender-document-test-", ignore_cleanup_errors=True) as temporary:
        with zipfile.ZipFile(archive) as package:
            package.extractall(temporary)
        sys.path.insert(0, temporary)
        from blender_agent import eval_core
        from blender_agent.builtin_loader import BuiltinLoader

        eval_core.setup(BuiltinLoader())
        eval_core.start_timer()
        try:
            source_file = str(Path(temporary) / "other-document.blend")
            bpy.ops.wm.save_as_mainfile(filepath=source_file)
            previous = eval_core.submit_code(
                "bpy.context.scene['old_request_ran'] = True; __result__ = True",
                request_id="before-document-switch",
            )
            generation = previous.document_generation
            bpy.ops.wm.open_mainfile(filepath=source_file)
            eval_core._timer_callback()
            assert previous.error["code"] == "DOCUMENT_CHANGED", previous.snapshot()
            assert "old_request_ran" not in bpy.context.scene
            assert bpy.app.timers.is_registered(eval_core._timer_callback)

            current = eval_core.submit_code(
                "bpy.context.scene['new_request_ran'] = True; __result__ = True",
                request_id="after-document-switch",
            )
            eval_core._timer_callback()
            assert current.document_generation > generation
            assert current.execution_status == "succeeded", current.snapshot()
            assert bpy.context.scene["new_request_ran"] is True
            print("BLENDER_AGENT_DOCUMENT_LIFECYCLE_OK old_cancelled=true new_succeeded=true")
        finally:
            eval_core.stop_timer()
        assert eval_core._on_load_pre not in bpy.app.handlers.load_pre
        assert eval_core._on_load_post not in bpy.app.handlers.load_post


if __name__ == "__main__":
    main()
