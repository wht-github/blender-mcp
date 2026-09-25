"""Exercise a built add-on through Blender's real UI event loop and MCP HTTP.

Run without ``--background`` so Blender can service its application timers::

    blender --factory-startup --python tests/blender_event_loop_smoke.py -- addon.zip

``--disable-execution-timer`` is an ablation check: it removes the execution
timer, and the same test must fail with exit code 1. No execution callback is
called directly by this script.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import queue
import socket
import sys
import tempfile
import threading
import time
import traceback
import zipfile

import addon_utils
import bpy


_CLIENT_TIMEOUT = 10.0
_TOTAL_TIMEOUT = 45.0
_ADDON = "blender_agent"


def _arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--disable-execution-timer", action="store_true")
    arguments = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    return parser.parse_args(arguments)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _raise_addon_error(error):
    raise error


class EventLoopSmoke:
    def __init__(self, arguments):
        self.arguments = arguments
        self.responses = queue.SimpleQueue()
        self.phase = 0
        self.addon = None
        # Imported Windows .pyd files stay locked until process exit.
        self.directory = tempfile.TemporaryDirectory(
            prefix="blender-agent-event-loop-", ignore_cleanup_errors=True
        )

    def start(self):
        threading.Thread(target=self._watchdog, daemon=True).start()
        try:
            if bpy.app.background:
                raise RuntimeError("This test requires Blender's interactive event loop")
            bpy.context.preferences.use_preferences_save = False
            with zipfile.ZipFile(self.arguments.archive.resolve()) as archive:
                archive.extractall(self.directory.name)
            sys.path.insert(0, self.directory.name)

            # default_set=False deliberately avoids changing enabled add-on defaults.
            # The UI operators still need an in-memory preferences entry.
            preference = bpy.context.preferences.addons.new()
            preference.module = _ADDON
            self._enable_and_start()
            bpy.app.timers.register(self._poll_client, first_interval=0.05)
        except BaseException as error:
            self._finish(error)

    def _enable_and_start(self):
        self.addon = addon_utils.enable(
            _ADDON, default_set=False, handle_error=_raise_addon_error
        )
        if self.addon is None or not self.addon.__addon_enabled__:
            raise AssertionError("Add-on did not enable")
        preferences = bpy.context.preferences.addons[_ADDON].preferences
        preferences.mcp_host = "127.0.0.1"
        preferences.mcp_port = _free_port()
        if bpy.ops.agent.start_server() != {"FINISHED"}:
            raise AssertionError("Start server operator failed")
        if not self.addon.mcp_server.is_running():
            raise AssertionError("Start operator did not start the MCP server")

        execution_timer = self.addon.eval_core._timer_callback
        if not bpy.app.timers.is_registered(execution_timer):
            raise AssertionError("Start operator did not register the execution timer")
        if self.arguments.disable_execution_timer:
            bpy.app.timers.unregister(execution_timer)

        url = self.addon.mcp_server.get_url()
        threading.Thread(
            target=self._run_client,
            args=(url, self.phase),
            name=f"event-loop-smoke-client-{self.phase}",
            daemon=True,
        ).start()

    def _run_client(self, url, phase):
        try:
            asyncio.run(self._check_http(url, phase))
        except BaseException as error:
            self.responses.put(error)
        else:
            self.responses.put(None)

    async def _check_http(self, url, phase):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with asyncio.timeout(_CLIENT_TIMEOUT):
            async with streamable_http_client(url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    for name, code, expected in (
                        (
                            "main-thread",
                            "import threading\n"
                            "__result__ = {'main_thread': "
                            "threading.current_thread() is threading.main_thread()}",
                            {"main_thread": True},
                        ),
                        (
                            "business-error-field",
                            "__result__ = {'error': None, 'count': 5}",
                            {"error": None, "count": 5},
                        ),
                    ):
                        request_id = f"event-loop-{phase}-{name}"
                        response = await session.call_tool(
                            "eval_python_code", {"code": code, "request_id": request_id}
                        )
                        if response.isError:
                            raise AssertionError(f"{name} returned an error: {response}")
                        envelope = response.structuredContent
                        if not isinstance(envelope, dict):
                            raise AssertionError(f"Missing structured result: {response}")
                        if (
                            envelope.get("result") != expected
                            or envelope.get("error") is not None
                            or envelope.get("request_id") != request_id
                            or envelope.get("status") != "succeeded"
                            or envelope.get("execution_status") != "succeeded"
                        ):
                            raise AssertionError(f"Unexpected {name} envelope: {envelope}")

    def _poll_client(self):
        try:
            outcome = self.responses.get_nowait()
        except queue.Empty:
            return 0.05

        try:
            if outcome is not None:
                raise outcome
            self._stop_and_disable()
            if self.phase == 0:
                self.phase = 1
                self._enable_and_start()
                return 0.05
            self._finish()
        except BaseException as error:
            self._finish(error)
        return None

    def _stop_and_disable(self):
        if bpy.ops.agent.stop_server() != {"FINISHED"}:
            raise AssertionError("Stop server operator failed")
        if self.addon.mcp_server.is_running():
            raise AssertionError("Stop operator left the server running")
        if bpy.app.timers.is_registered(self.addon.eval_core._timer_callback):
            raise AssertionError("Stop operator left the execution timer registered")
        addon_utils.disable(_ADDON, default_set=False, handle_error=_raise_addon_error)
        if self.addon.__addon_enabled__:
            raise AssertionError("Add-on did not disable")

    def _finish(self, error=None):
        try:
            if self.addon is not None and self.addon.__addon_enabled__:
                self._stop_and_disable()
            self.directory.cleanup()
        except BaseException as cleanup_error:
            error = error or cleanup_error

        if error is not None:
            traceback.print_exception(error)
            print("BLENDER_AGENT_EVENT_LOOP_SMOKE_FAILED", file=sys.stderr, flush=True)
            os._exit(1)

        print("BLENDER_AGENT_EVENT_LOOP_SMOKE_OK cycles=2 main_thread=true", flush=True)
        bpy.ops.wm.quit_blender()

    def _watchdog(self):
        time.sleep(_TOTAL_TIMEOUT)
        print(
            f"BLENDER_AGENT_EVENT_LOOP_SMOKE_FAILED overall timeout {_TOTAL_TIMEOUT}s",
            file=sys.stderr,
            flush=True,
        )
        os._exit(1)


if __name__ == "__main__":
    EventLoopSmoke(_arguments()).start()
