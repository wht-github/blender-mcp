from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import queue
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
    handlers = types.ModuleType("bpy.app.handlers")
    handlers.persistent = lambda callback: callback
    handlers.load_pre = []
    handlers.load_post = []
    handlers.load_post_fail = []
    sys.modules["bpy.app.handlers"] = handlers
    setattr(fake_bpy, "app", types.SimpleNamespace(timers=_FakeTimers(), handlers=handlers))
    sys.modules["bpy"] = fake_bpy

    loader = _load_module(
        "blender_agent.builtin_loader",
        PACKAGE_DIR / "builtin_loader.py",
    )
    eval_core = _load_module("blender_agent.eval_core", PACKAGE_DIR / "eval_core.py")
    mcp_server = _load_module("blender_agent.mcp_server", PACKAGE_DIR / "mcp_server.py")
    eval_core.setup(loader.BuiltinLoader())
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

        self.assertEqual(
            {name: outcome.result for name, outcome in results.items()},
            {"first": "first", "second": "second"},
        )
        self.assertTrue(all(outcome.status == "succeeded" for outcome in results.values()))

    def test_none_is_valid_but_missing_result_is_structured_error(self):
        _loader, eval_core, _server = load_runtime_modules()

        self.assertIsNone(eval_core.run_code("__result__ = None").value)
        missing = eval_core.run_code("value = 1")
        self.assertEqual(missing.error["code"], "MISSING_RESULT")

    def test_queue_timeout_is_not_executed_later(self):
        _loader, eval_core, _server = load_runtime_modules()

        outcome = eval_core.run_code_from_thread("__result__ = 1", timeout=0.01)
        self.assertEqual(outcome.status, "timed_out")
        self.assertEqual(outcome.error["code"], "QUEUE_TIMEOUT")

        eval_core._timer_callback()
        history = eval_core.get_task_history()
        self.assertEqual(history[0]["status"], "timed_out")
        self.assertIsNone(history[0]["started_at"])

    def test_running_timeout_records_that_execution_continues(self):
        _loader, eval_core, _server = load_runtime_modules()
        result = {}

        worker = threading.Thread(
            target=lambda: result.update(
                outcome=eval_core.run_code_from_thread(
                    "import time; time.sleep(0.1); __result__ = 1",
                    timeout=0.02,
                )
            )
        )
        worker.start()
        deadline = time.monotonic() + 1.0
        while eval_core._pending_tasks.qsize() < 1 and time.monotonic() < deadline:
            time.sleep(0.005)

        executor = threading.Thread(target=eval_core._timer_callback)
        executor.start()
        worker.join()
        active_history = eval_core.get_task_history()

        self.assertEqual(result["outcome"].status, "timed_out")
        self.assertEqual(result["outcome"].error["code"], "EXECUTION_TIMEOUT")
        self.assertTrue(active_history[0]["execution_continues"])

        executor.join()
        self.assertFalse(eval_core.get_task_history()[0]["execution_continues"])

    def test_queue_capacity_rejection_is_structured(self):
        _loader, eval_core, _server = load_runtime_modules()
        eval_core._pending_tasks = queue.Queue(maxsize=1)
        eval_core._pending_tasks.put_nowait(eval_core._ExecutionTask("__result__ = 1"))

        outcome = eval_core.run_code_from_thread("__result__ = 2", timeout=0.1)

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error["code"], "QUEUE_FULL")
        eval_core.stop_timer()

    def test_code_size_limit_is_structured_and_recorded(self):
        _loader, eval_core, _server = load_runtime_modules()

        outcome = eval_core.run_code_from_thread(
            "x" * (eval_core.MAX_CODE_BYTES + 1),
            timeout=0.1,
        )

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error["code"], "CODE_TOO_LARGE")
        history = eval_core.get_task_history()[0]
        self.assertEqual(history["request_id"], outcome.request_id)
        self.assertEqual(history["error_code"], "CODE_TOO_LARGE")

    def test_stop_cancels_queued_task(self):
        _loader, eval_core, _server = load_runtime_modules()
        result = {}
        worker = threading.Thread(
            target=lambda: result.update(
                outcome=eval_core.run_code_from_thread("__result__ = 1", timeout=1.0)
            )
        )
        worker.start()
        deadline = time.monotonic() + 1.0
        while eval_core._pending_tasks.qsize() < 1 and time.monotonic() < deadline:
            time.sleep(0.005)

        eval_core.stop_timer()
        worker.join()

        self.assertEqual(result["outcome"].status, "cancelled")
        self.assertEqual(result["outcome"].error["code"], "CANCELLED")

    def test_one_hundred_concurrent_requests_have_unique_matched_outcomes(self):
        _loader, eval_core, _server = load_runtime_modules()
        results = {}
        results_lock = threading.Lock()
        stop_draining = threading.Event()

        def execute(index: int):
            outcome = eval_core.run_code_from_thread(
                f"__result__ = {index}",
                timeout=2.0,
            )
            with results_lock:
                results[index] = outcome

        def drain():
            while not stop_draining.is_set() or not eval_core._pending_tasks.empty():
                eval_core._timer_callback()
                time.sleep(0.001)

        drainer = threading.Thread(target=drain)
        workers = [threading.Thread(target=execute, args=(index,)) for index in range(100)]
        drainer.start()
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        stop_draining.set()
        drainer.join()

        self.assertEqual(len(results), 100)
        self.assertEqual(len({outcome.request_id for outcome in results.values()}), 100)
        for index, outcome in results.items():
            if outcome.status == "succeeded":
                self.assertEqual(outcome.result, index)
            else:
                self.assertEqual(outcome.error["code"], "QUEUE_FULL")


class RuntimeContextTests(unittest.TestCase):
    def test_all_declared_operations_exist_on_their_builtin_modules(self):
        loader_module, _eval_core, _server = load_runtime_modules()
        loader = loader_module.BuiltinLoader()

        for metadata in loader.list_metadata():
            module = loader.load(metadata.name)
            for operation in metadata.operations:
                self.assertTrue(
                    callable(getattr(module, operation.name, None)),
                    operation.capability_id,
                )

    def test_search_returns_compact_metadata_without_importing_builtin(self):
        _loader, eval_core, _server = load_runtime_modules()
        module_name = "blender_agent.builtins.viewport"

        result = eval_core.run_code(
            "__result__ = runtime.search('focus selection screenshot', limit=5)"
        )

        ids = [item["id"] for item in result.value["matches"]]
        self.assertIn("builtin.viewport", ids)
        self.assertTrue(all(
            set(item) == {"id", "load_id", "summary", "score"}
            for item in result.value["matches"]
        ))
        self.assertNotIn(module_name, sys.modules)

    def test_search_and_describe_can_target_one_builtin_operation(self):
        _loader, eval_core, _server = load_runtime_modules()
        module_name = "blender_agent.builtins.materials"

        search = eval_core.run_code(
            "__result__ = runtime.search('assign mesh faces material edit mode', limit=5)"
        )
        operation_ids = [item["id"] for item in search.value["matches"]]
        self.assertIn("builtin.materials.assign_faces_by_index", operation_ids)

        described = eval_core.run_code(
            "__result__ = runtime.describe("
            "'builtin.materials.assign_faces_by_index'"
            ")"
        )
        operation = described.value["capabilities"][0]
        self.assertEqual(operation["kind"], "builtin_operation")
        self.assertEqual(operation["load_id"], "builtin.materials")
        self.assertIn("material_index", operation["signature"])
        self.assertNotIn("description", operation)
        self.assertNotIn(module_name, sys.modules)

    def test_describe_discloses_full_doc_without_loading(self):
        _loader, eval_core, _server = load_runtime_modules()
        module_name = "blender_agent.builtins.materials"

        result = eval_core.run_code(
            "__result__ = runtime.describe('builtin.materials')"
        )
        capability = result.value["capabilities"][0]

        self.assertEqual(capability["id"], "builtin.materials")
        self.assertFalse(capability["loaded"])
        self.assertIn("ensure_principled_material", capability["description"])
        self.assertNotIn(module_name, sys.modules)

    def test_load_activates_builtin_for_tools_proxy(self):
        _loader, eval_core, _server = load_runtime_modules()

        result = eval_core.run_code(
            "runtime.load('builtin.scene_info')\n"
            "__result__ = {\n"
            "    'module': tools.scene_info.__name__,\n"
            "    'state': runtime.list(),\n"
            "}"
        )

        self.assertEqual(result.value["module"], "blender_agent.builtins.scene_info")
        self.assertEqual(
            [item["id"] for item in result.value["state"]["loaded"]],
            ["builtin.scene_info"],
        )
        self.assertEqual(result.value["state"]["revision"], 1)

    def test_loading_operation_id_activates_its_module(self):
        _loader, eval_core, _server = load_runtime_modules()

        result = eval_core.run_code(
            "load_result = runtime.load("
            "'builtin.screenshot.capture_viewport'"
            ")\n"
            "__result__ = {\n"
            "    'load_result': load_result,\n"
            "    'module': tools.screenshot.__name__,\n"
            "}"
        )

        self.assertEqual(result.value["load_result"]["loaded"], ["builtin.screenshot"])
        self.assertEqual(result.value["module"], "blender_agent.builtins.screenshot")

    def test_loading_unknown_operation_is_rejected(self):
        _loader, eval_core, _server = load_runtime_modules()

        result = eval_core.run_code(
            "__result__ = runtime.load('builtin.materials.not_a_real_operation')"
        )

        self.assertEqual(result.error["code"], "EXECUTION_ERROR")
        self.assertIn("not_a_real_operation", result.error["details"])

    def test_tools_proxy_rejects_capability_before_load(self):
        _loader, eval_core, _server = load_runtime_modules()

        result = eval_core.run_code("__result__ = tools.scene_info.__name__")

        self.assertEqual(result.error["code"], "EXECUTION_ERROR")
        self.assertIn("is not loaded", result.error["details"])

    def test_unload_is_logical_and_keeps_module_cache(self):
        _loader, eval_core, _server = load_runtime_modules()

        state = eval_core.run_code(
            "runtime.load('builtin.scene_info')\n"
            "runtime.unload('builtin.scene_info')\n"
            "__result__ = runtime.list()"
        )
        after_unload = eval_core.run_code(
            "runtime.load('builtin.scene_info')\n"
            "runtime.unload('builtin.scene_info')\n"
            "__result__ = tools.scene_info.__name__"
        )

        self.assertEqual(state.value["loaded"], [])
        self.assertEqual(state.value["revision"], 2)
        self.assertEqual(after_unload.error["code"], "EXECUTION_ERROR")
        self.assertIn("scene_info", eval_core._loader.loaded_names())

    def test_legacy_get_builtin_loads_and_activates(self):
        _loader, eval_core, _server = load_runtime_modules()

        state = eval_core.run_code(
            "module = get_builtin('scene_info')\n"
            "__result__ = {'module': module.__name__, 'state': runtime.list()}"
        )

        self.assertEqual(state.value["module"], "blender_agent.builtins.scene_info")
        self.assertEqual(state.value["state"]["loaded"][0]["id"], "builtin.scene_info")

    def test_each_eval_has_independent_activation_and_reuses_module_cache(self):
        _loader, eval_core, _server = load_runtime_modules()
        first = eval_core.run_code(
            "runtime.load('builtin.scene_info')\n"
            "__result__ = id(tools.scene_info)"
        )
        unloaded = eval_core.run_code("__result__ = runtime.list()")
        inaccessible = eval_core.run_code("__result__ = tools.scene_info.__name__")
        second = eval_core.run_code(
            "runtime.load('builtin.scene_info')\n"
            "__result__ = id(tools.scene_info)"
        )

        self.assertIsNone(first.error)
        self.assertEqual(unloaded.value["loaded"], [])
        self.assertEqual(unloaded.value["revision"], 0)
        self.assertEqual(inaccessible.error["code"], "EXECUTION_ERROR")
        self.assertEqual(first.value, second.value)

    def test_describe_is_repeatable_and_loading_never_appends_docs(self):
        _loader, eval_core, _server = load_runtime_modules()
        module_code = "__result__ = runtime.describe('builtin.materials')"
        before = eval_core.run_code(module_code)
        operation = eval_core.run_code(
            "__result__ = runtime.describe('builtin.materials.assign_faces_by_index')"
        )
        loaded = eval_core.run_code("__result__ = runtime.load('builtin.materials')")
        after = eval_core.run_code(module_code)

        self.assertIsNone(operation.error)
        self.assertEqual(before.value, after.value)
        self.assertIn(
            "ensure_principled_material",
            after.value["capabilities"][0]["description"],
        )
        self.assertFalse(after.value["capabilities"][0]["loaded"])
        self.assertIsNone(loaded.error)
        self.assertEqual(loaded.docs, ())

    def test_chinese_requests_find_relevant_operations_in_top_three(self):
        _loader, eval_core, _server = load_runtime_modules()
        cases = {
            "查找没有材质的对象": "builtin.scene_info.find_objects",
            "聚焦截图": "builtin.viewport.capture_objects",
            "筛选没有修改器的对象": "builtin.scene_info.find_objects",
            "保存场景": "builtin.scene_info.save_scene",
        }
        for query, expected_id in cases.items():
            with self.subTest(query=query):
                result = eval_core.run_code(
                    f"__result__ = runtime.search({query!r}, limit=3)"
                )
                self.assertIsNone(result.error)
                self.assertIn(
                    expected_id, [item["id"] for item in result.value["matches"]]
                )

    def test_screenshot_builtin_rejects_oversize_and_reserved_result_fields(self):
        _loader, eval_core, _server = load_runtime_modules()

        oversized = eval_core.run_code(
            "runtime.load('builtin.screenshot')\n"
            "__result__ = tools.screenshot._resolve_dimensions("
            "8192, 8192, 1920, 1080"
            ")"
        )
        reserved = eval_core.run_code(
            "runtime.load('builtin.screenshot')\n"
            "__result__ = tools.screenshot.as_result("
            "'abc', screenshot='override'"
            ")"
        )

        self.assertEqual(oversized.error["code"], "EXECUTION_ERROR")
        self.assertIn("4096", oversized.error["details"])
        self.assertEqual(reserved.error["code"], "EXECUTION_ERROR")
        self.assertIn("reserved fields", reserved.error["details"])

    def test_diagnostics_builtin_reports_runtime_and_disables_nested_eval(self):
        _loader, eval_core, _server = load_runtime_modules()

        result = eval_core.run_code(
            "runtime.load('builtin.blender_log')\n"
            "__result__ = {\n"
            "    'status': tools.blender_log.get_runtime_status(),\n"
            "    'nested': tools.blender_log.capture_script_output("
            "\"__result__ = 1\""
            "),\n"
            "}"
        )

        self.assertIn("queue", result.value["status"])
        self.assertEqual(
            result.value["nested"]["error"]["code"],
            "DEPRECATED_NESTED_EVAL",
        )


class StreamableHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_port_conflict_fails_without_runtime_or_timer(self):
        _loader, eval_core, server = load_runtime_modules()
        occupied = socket.socket()
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = int(occupied.getsockname()[1])
        try:
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                await asyncio.to_thread(server.start, port=port)
            self.assertIsNone(server._runtime)
            self.assertFalse(eval_core.bpy.app.timers.is_registered(eval_core._timer_callback))
        finally:
            occupied.close()

    async def test_initialize_list_and_call_tool(self):
        _loader, eval_core, server = load_runtime_modules()
        eval_core.start_timer = lambda: None
        eval_core.stop_timer = lambda: None
        async def execute(code, timeout, request=None, request_id=None):
            return {"echo": code}

        server._execute_request = execute

        port = free_port()
        await asyncio.to_thread(server.start, port=port)
        try:
            async with streamable_http_client(server.get_url()) as (read, write, _):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    self.assertEqual(initialized.serverInfo.name, "blender-agent")

                    tools = await session.list_tools()
                    self.assertEqual([tool.name for tool in tools.tools], ["eval_python_code", "get_execution_status"])
                    description = tools.tools[0].description or ""
                    self.assertIn("runtime.search(query)", description)
                    self.assertNotIn(
                        "创建/复用 Principled 材质、应用常见预设",
                        description,
                    )

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
