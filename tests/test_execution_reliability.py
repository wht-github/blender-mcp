"""Regressions for execution admission, cross-thread data and task lifecycle."""

import asyncio
import queue
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from test_runtime import free_port, load_runtime_modules


class ResultBoundaryTests(unittest.TestCase):
    def test_objects_are_rejected_without_invoking_conversion_hooks(self):
        _, core, _ = load_runtime_modules()
        result = core.run_code(
            "class Unsafe:\n"
            "    def __str__(self):\n"
            "        bpy.conversion_called = True\n"
            "        return 'unsafe'\n"
            "__result__ = {'object': Unsafe()}"
        )
        self.assertEqual(result.error['code'], 'INVALID_RESULT')
        self.assertFalse(hasattr(core.bpy, 'conversion_called'))
        result = core.run_code("class Sneaky(dict): pass\n__result__ = Sneaky()")
        self.assertEqual(result.error['code'], 'INVALID_RESULT')

    def test_snapshot_copies_containers_and_accepts_tuple_properties(self):
        _, core, _ = load_runtime_modules()
        core.bpy.saved = {'position': (1, 2, 3), 'values': [4]}
        result = core.run_code('__result__ = bpy.saved')
        core.bpy.saved['values'].append(5)
        self.assertEqual(result.value, {'position': [1, 2, 3], 'values': [4]})

    def test_cycles_nonfinite_unicode_and_oversized_results_are_structured(self):
        _, core, _ = load_runtime_modules()
        for code in (
            "a = []; a.append(a); __result__ = a",
            "__result__ = float('nan')",
            "__result__ = {1: 'value'}",
            "__result__ = chr(0xd800)",
            "__result__ = 'x' * (1024 * 1024 + 1)",
            "__result__ = {'screenshot': 'not base64'}",
        ):
            with self.subTest(code=code):
                result = core.run_code(code)
                self.assertEqual(result.error['code'], 'INVALID_RESULT')

    def test_system_exit_does_not_disable_following_tasks(self):
        _, core, _ = load_runtime_modules()
        first = core.submit_code('raise SystemExit(0)')
        second = core.submit_code('__result__ = 42')
        self.assertEqual(core._timer_callback(), 0.05)
        core._timer_callback()
        self.assertEqual(first.wait(0).error['code'], 'EXECUTION_ERROR')
        self.assertEqual(second.wait(0).result, 42)
        self.assertEqual(core._pending_tasks.unfinished_tasks, 0)

    def test_loading_builtin_does_not_implicitly_attach_documentation(self):
        _, core, _ = load_runtime_modules()
        task = core.submit_code("runtime.load('builtin.materials'); bpy.changed = True; __result__ = 42")
        core._timer_callback()
        self.assertTrue(core.bpy.changed)
        self.assertEqual(task.wait(0).result, 42)
        self.assertEqual(task.wait(0).docs, ())
        self.assertEqual(core._pending_tasks.unfinished_tasks, 0)

    def test_deadline_prevents_execution_even_when_waiter_has_not_resumed(self):
        _, core, _ = load_runtime_modules()
        task = core.submit_code('bpy.changed = True; __result__ = 1', timeout=0)
        core._timer_callback()
        self.assertFalse(hasattr(core.bpy, 'changed'))
        self.assertEqual(task.wait(0).error['code'], 'QUEUE_TIMEOUT')

    def test_late_success_and_failure_are_retrievable_and_response_stays_timeout(self):
        _, core, _ = load_runtime_modules()
        for result, status in (
            (core.ExecutionResult(value=42), 'succeeded'),
            (core.ExecutionResult(error={'code': 'EXECUTION_ERROR'}), 'failed'),
        ):
            task = core.submit_code('__result__ = 1', timeout=1)
            self.assertTrue(task.start())
            task.deadline = time.monotonic() - 1
            response = task.wait(0)
            self.assertEqual(response.status, 'timed_out')
            task.mark_done(result)
            state = core.get_execution_status(task.request_id, include_result=True)
            self.assertEqual(state['execution_status'], status)
            self.assertEqual(state['result'], result.value)
            self.assertEqual(state['error'], result.error)
            self.assertFalse(state['execution_continues'])
            self.assertTrue(response.error['execution_continues'])
        core.stop_timer()

    def test_results_expire_and_cache_enforces_byte_budget(self):
        _, core, _ = load_runtime_modules()
        with patch.object(core, 'RESULT_CACHE_TTL', 0):
            expired = core.submit_code('__result__ = 42')
            core._timer_callback()
        self.assertFalse(core.get_execution_status(expired.request_id, True)['result_available'])
        with patch.object(core, 'RESULT_CACHE_BYTES', 120):
            first = core.submit_code("__result__ = 'a' * 64")
            core._timer_callback()
            second = core.submit_code("__result__ = 'b' * 64")
            core._timer_callback()
            self.assertLessEqual(core._result_cache_size, 120)
            self.assertFalse(core.get_execution_status(first.request_id, True)['result_available'])
            self.assertEqual(core.get_execution_status(second.request_id, True)['result'], 'b' * 64)
        self.assertTrue(all('result' not in item for item in core.get_task_history()))

    def test_shutdown_closes_admission(self):
        _, core, _ = load_runtime_modules()
        task = core.submit_code('__result__ = 1')
        core.stop_timer()
        self.assertEqual(task.wait(0).status, 'cancelled')
        self.assertEqual(core.submit_code('__result__ = 2').wait(0).error['code'], 'SERVICE_STOPPED')
        self.assertEqual(core._pending_tasks.unfinished_tasks, 0)


class AsyncExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_admission_does_not_use_default_executor(self):
        _, core, _ = load_runtime_modules()
        core._pending_tasks = queue.Queue(maxsize=2)
        loop = asyncio.get_running_loop()
        with patch.object(loop, 'run_in_executor', side_effect=AssertionError('hidden executor')):
            calls = [asyncio.create_task(core.run_code_async(f'__result__ = {i}', 0.05)) for i in range(12)]
            outcomes = await asyncio.gather(*calls)
        self.assertEqual(sum(o.status == 'timed_out' for o in outcomes), 2)
        self.assertEqual(sum(o.error['code'] == 'QUEUE_FULL' for o in outcomes), 10)
        core.stop_timer()

    async def test_cancelling_queued_waiter_prevents_side_effects(self):
        _, core, _ = load_runtime_modules()
        call = asyncio.create_task(core.run_code_async('bpy.changed = True; __result__ = 1', 1))
        await asyncio.sleep(0)
        call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await call
        core._timer_callback()
        self.assertFalse(hasattr(core.bpy, 'changed'))
        self.assertEqual(core.get_task_history()[0]['status'], 'cancelled')

    async def test_cancelling_running_waiter_keeps_final_result(self):
        _, core, _ = load_runtime_modules()
        task = core.submit_code('__result__ = 1')
        call = asyncio.create_task(task.wait_async(1))
        await asyncio.sleep(0)
        self.assertTrue(task.start())
        call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await call
        self.assertTrue(core.get_execution_status(task.request_id)['execution_continues'])
        task.mark_done(core.ExecutionResult(value=42))
        state = core.get_execution_status(task.request_id, True)
        self.assertEqual(state['status'], 'cancelled')
        self.assertEqual(state['execution_status'], 'succeeded')
        self.assertEqual(state['result'], 42)
        core.stop_timer()

    async def test_main_thread_completion_delivers_to_event_loop_thread(self):
        _, core, _ = load_runtime_modules()
        task = core.submit_code('__result__ = 42')
        response = {}
        ready = threading.Event()

        def client():
            async def wait():
                ready.set()
                response['outcome'] = await task.wait_async(2)
            asyncio.run(wait())

        worker = threading.Thread(target=client)
        worker.start()
        self.assertTrue(ready.wait(1))
        core._timer_callback()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(response['outcome'].result, 42)


class HTTPExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _, self.core, self.server = load_runtime_modules()
        self.server.start(port=free_port())
        self.client = httpx.AsyncClient(
            base_url=self.server.get_url(), timeout=5,
            headers={'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25'},
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        await asyncio.to_thread(self.server.stop)

    async def call(self, name, arguments, number=1):
        response = await self.client.post(self.server.get_url(), json={
            'jsonrpc': '2.0', 'id': number, 'method': 'tools/call',
            'params': {'name': name, 'arguments': arguments},
        })
        response.raise_for_status()
        return response.json()['result']

    async def until(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate():
            if time.monotonic() > deadline:
                self.fail('HTTP execution did not reach expected state')
            await asyncio.sleep(0.01)

    async def test_real_http_capacity_and_status_bypass_execution_queue(self):
        self.core._pending_tasks = queue.Queue(maxsize=2)
        calls = [asyncio.create_task(self.call('eval_python_code', {'code': f'__result__ = {i}'}, i)) for i in range(12)]
        try:
            await self.until(lambda: sum(call.done() for call in calls) == 10)
            active = next(item for item in self.core.get_task_history(50) if item['status'] == 'queued')
            status = await self.call('get_execution_status', {'request_id': active['request_id']})
            self.assertEqual(status['structuredContent']['status'], 'queued')
            self.core._timer_callback()
            self.core._timer_callback()
            results = await asyncio.gather(*calls)
            self.assertEqual(sum(not result.get('isError', False) for result in results), 2)
            self.assertEqual(sum('QUEUE_FULL' in str(result) for result in results), 10)
            final = await self.call('get_execution_status', {'request_id': active['request_id'], 'include_result': True})
            self.assertFalse(final.get('isError', False))
            self.assertIn('succeeded', str(final))
        finally:
            for call in calls:
                call.cancel()
            await asyncio.gather(*calls, return_exceptions=True)

    async def test_http_disconnect_cancels_queued_script(self):
        call = asyncio.create_task(self.call('eval_python_code', {'code': 'bpy.changed = True; __result__ = 1'}))
        try:
            await self.until(lambda: self.core._pending_tasks.qsize() == 1)
        finally:
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
        await self.until(lambda: self.core.get_task_history()[0]['status'] == 'cancelled')
        self.core._timer_callback()
        self.assertFalse(hasattr(self.core.bpy, 'changed'))

    async def test_shutdown_releases_real_http_waiter(self):
        call = asyncio.create_task(self.call('eval_python_code', {'code': '__result__ = 42'}))
        try:
            await self.until(lambda: self.core._pending_tasks.qsize() == 1)
            await asyncio.to_thread(self.server.stop)
            result = await call
            self.assertTrue(result['isError'])
            self.assertIn('CANCELLED', str(result))
            self.assertIsNone(self.server._runtime)
        finally:
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
