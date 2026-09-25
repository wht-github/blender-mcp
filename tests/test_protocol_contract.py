"""Exercise recovery through the public HTTP contract, with no internal task IDs."""

import asyncio
import threading
import time
import types
import unittest

import httpx

from test_runtime import free_port, load_runtime_modules


class ProtocolContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_is_transported_once_without_consuming_business_error_field(self):
        _, core, server = load_runtime_modules()
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jM1sAAAAASUVORK5CYII="
        task = core.submit_code(f"__result__ = {{'screenshot': {png!r}, 'error': None}}")
        core._timer_callback()
        response = server._format_mcp_result(task.wait(0))
        self.assertFalse(response.isError)
        envelope = response.structuredContent
        self.assertEqual(envelope["result"], {"error": None})
        self.assertNotIn(png, str(envelope))
        image = response.content[envelope["image"]["content_index"]]
        self.assertEqual(image.type, "image")
        self.assertEqual(image.data, png)

    async def test_cancel_immediately_after_mcp_submission_cancels_queued_edit(self):
        _, core, server = load_runtime_modules()
        request = asyncio.create_task(server._execute_request(
            "bpy.unexpected = True; __result__ = 42", 10, request_id="immediately-cancelled",
        ))
        await asyncio.sleep(0)
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        core._timer_callback()
        self.assertFalse(hasattr(core.bpy, "unexpected"))
        self.assertEqual(core.get_execution_status("immediately-cancelled")["status"], "cancelled")

    async def test_known_id_recovers_after_disconnect_and_does_not_repeat_scene_edit(self):
        _, core, server = load_runtime_modules()
        server.start(port=free_port())
        headers = {"Accept": "application/json, text/event-stream"}
        request_id = "client-known-recovery-id"
        core.bpy.edit_started = threading.Event()
        core.bpy.finish_edit = threading.Event()
        code = (
            "bpy.edit_started.set(); bpy.finish_edit.wait(3); "
            "bpy.edit_count = getattr(bpy, 'edit_count', 0) + 1; "
            "__result__ = {'error': None, 'count': bpy.edit_count}"
        )
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "eval_python_code", "arguments": {"code": code, "request_id": request_id},
        }}
        try:
            async with httpx.AsyncClient(headers=headers, timeout=5) as client:
                request = asyncio.create_task(client.post(server.get_url(), json=body))
                deadline = time.monotonic() + 2
                while core._pending_tasks.empty() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                self.assertFalse(core._pending_tasks.empty())
                executor = asyncio.create_task(asyncio.to_thread(core._timer_callback))
                while not core.bpy.edit_started.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                self.assertTrue(core.bpy.edit_started.is_set())
                # Drop HTTP while the script is still running, then let it finish.
                request.cancel()
                try:
                    await request
                except asyncio.CancelledError:
                    pass
                deadline = time.monotonic() + 2
                while core.get_execution_status(request_id)["status"] != "cancelled" and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                self.assertEqual(core.get_execution_status(request_id)["execution_status"], "running")
                self.assertEqual(core.get_execution_status(request_id)["status"], "cancelled")
                core.bpy.finish_edit.set()
                await executor

                recovered = (await client.post(server.get_url(), json={
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                        "name": "get_execution_status",
                        "arguments": {"request_id": request_id, "include_result": True},
                    },
                })).json()["result"]
                self.assertFalse(recovered["isError"])
                self.assertEqual(recovered["structuredContent"]["result"], {"error": None, "count": 1})
                self.assertIsNone(recovered["structuredContent"]["error"])

                repeated = (await client.post(server.get_url(), json=body)).json()["result"]
                self.assertFalse(repeated["isError"])
                self.assertEqual(repeated["structuredContent"]["request_id"], request_id)
                self.assertEqual(core.bpy.edit_count, 1)
                self.assertTrue(core._pending_tasks.empty())

                body["params"]["arguments"]["code"] = "__result__ = 'different code'"
                conflict = (await client.post(server.get_url(), json=body)).json()["result"]
                self.assertTrue(conflict["isError"])
                self.assertEqual(conflict["structuredContent"]["error"]["code"], "REQUEST_ID_CONFLICT")
                self.assertEqual(core.bpy.edit_count, 1)
        finally:
            core.bpy.finish_edit.set()
            await asyncio.to_thread(server.stop)

    async def test_dead_server_can_be_started_again(self):
        _, core, server = load_runtime_modules()
        server._runtime = server._Runtime(
            "127.0.0.1", free_port(),
            types.SimpleNamespace(started=True, should_exit=False),
            thread=types.SimpleNamespace(is_alive=lambda: False, join=lambda **kwargs: None),
        )
        self.assertFalse(server.is_running())
        try:
            server.start(port=free_port())
            self.assertTrue(server.is_running())
        finally:
            await asyncio.to_thread(server.stop)


if __name__ == "__main__":
    unittest.main()
