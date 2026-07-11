from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import socket
import sys
import threading
import time
import types
import unittest

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_DIR = ROOT / "blender_agent"


class _FakeTimers:
    def __init__(self):
        self.registered = set()

    def is_registered(self, callback):
        return callback in self.registered

    def register(self, callback, **_kwargs):
        self.registered.add(callback)

    def unregister(self, callback):
        self.registered.discard(callback)


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {name} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_runtime_modules():
    for name in list(sys.modules):
        if name == "blender_agent" or name.startswith("blender_agent.") or name == "bpy":
            sys.modules.pop(name, None)

    package = types.ModuleType("blender_agent")
    package.__path__ = [str(PACKAGE_DIR)]
    sys.modules["blender_agent"] = package

    fake_bpy = types.ModuleType("bpy")
    setattr(fake_bpy, "app", types.SimpleNamespace(timers=_FakeTimers()))
    sys.modules["bpy"] = fake_bpy

    loader = _load_module(
        "blender_agent.builtin_loader",
        PACKAGE_DIR / "builtin_loader.py",
    )
    eval_core = _load_module("blender_agent.eval_core", PACKAGE_DIR / "eval_core.py")
    mcp_server = _load_module("blender_agent.mcp_server", PACKAGE_DIR / "mcp_server.py")
    return loader, eval_core, mcp_server


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ExecutionQueueTests(unittest.TestCase):
    def test_concurrent_calls_are_fifo_and_not_overwritten(self):
        _loader, eval_core, _server = load_runtime_modules()
        results = {}

        def execute(name: str):
            results[name] = eval_core.run_code_from_thread(
                f"__result__ = '{name}'",
                timeout=2.0,
            )

        first = threading.Thread(target=execute, args=("first",))
        second = threading.Thread(target=execute, args=("second",))
        first.start()
        time.sleep(0.01)
        second.start()

        deadline = time.monotonic() + 1.0
        while eval_core._pending_tasks.qsize() < 2 and time.monotonic() < deadline:
            time.sleep(0.005)

        eval_core._timer_callback()
        eval_core._timer_callback()
        first.join()
        second.join()

        self.assertEqual(results, {"first": "first", "second": "second"})


class StreamableHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_initialize_list_and_call_tool(self):
        _loader, eval_core, server = load_runtime_modules()
        eval_core.start_timer = lambda: None
        eval_core.stop_timer = lambda: None
        eval_core.run_code_from_thread = lambda code, timeout: {"echo": code}

        port = free_port()
        await asyncio.to_thread(server.start, port=port)
        try:
            async with streamable_http_client(server.get_url()) as (read, write, _):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    self.assertEqual(initialized.serverInfo.name, "blender-agent")

                    tools = await session.list_tools()
                    self.assertEqual([tool.name for tool in tools.tools], ["eval_python_code"])

                    result = await session.call_tool(
                        "eval_python_code",
                        {"code": "__result__ = 42"},
                    )
                    self.assertFalse(result.isError)
                    self.assertEqual(result.content[0].type, "text")
                    self.assertIn("__result__ = 42", getattr(result.content[0], "text", ""))
        finally:
            await asyncio.to_thread(server.stop)


if __name__ == "__main__":
    unittest.main()
