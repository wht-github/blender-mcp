"""Behavioral contracts for document ownership and retry-safe execution."""

import asyncio
import unittest
from unittest.mock import patch

from test_runtime import load_runtime_modules


class ExecutionContractTests(unittest.TestCase):
    def test_cache_failure_preserves_delivery_and_following_execution(self):
        _, core, _ = load_runtime_modules()
        first = core.submit_code('bpy.changed = True; __result__ = 42', request_id='uncached')
        second = core.submit_code('__result__ = 43')
        with patch.object(core, '_cache_result', side_effect=RuntimeError('cache unavailable')):
            self.assertEqual(core._timer_callback(), 0.05)
        self.assertEqual(first.wait(0).result, 42)
        self.assertTrue(core.bpy.changed)
        self.assertFalse(core.get_execution_status('uncached', True)['result_available'])
        replay = core.submit_code('bpy.changed = True; __result__ = 42', request_id='uncached').wait(0)
        self.assertEqual(replay.error['code'], 'RESULT_EXPIRED')
        core._timer_callback()
        self.assertEqual(second.wait(0).result, 43)
        self.assertEqual(core._pending_tasks.unfinished_tasks, 0)

    def test_invalid_unicode_request_id_is_replaced_in_error_response(self):
        _, core, _ = load_runtime_modules()
        invalid_id = 'request-' + chr(0xd800)
        outcome = core.submit_code('bpy.changed = True; __result__ = 42', request_id=invalid_id).wait(0)
        self.assertEqual(outcome.error['code'], 'INVALID_REQUEST_ID')
        self.assertNotEqual(outcome.request_id, invalid_id)
        outcome.request_id.encode('utf-8')
        self.assertEqual(core._pending_tasks.qsize(), 0)
        self.assertFalse(hasattr(core.bpy, 'changed'))

    def test_user_error_field_is_successful_data(self):
        _, core, _ = load_runtime_modules()
        task = core.submit_code("__result__ = {'error': None, 'count': 5}")
        core._timer_callback()
        outcome = task.wait(0)
        self.assertEqual(outcome.status, 'succeeded')
        self.assertIsNone(outcome.error)
        self.assertEqual(outcome.result, {'error': None, 'count': 5})
        state = core.get_execution_status(task.request_id, True)
        self.assertEqual(state['result'], outcome.result)
        self.assertIsNone(state['error'])

    def test_duplicate_request_executes_once_and_conflict_preserves_original(self):
        _, core, _ = load_runtime_modules()
        core.bpy.count = 0
        code = 'bpy.count += 1; __result__ = bpy.count'
        original = core.submit_code(code, request_id='edit-1')
        self.assertIs(core.submit_code(code, request_id='edit-1'), original)
        conflict = core.submit_code('__result__ = 99', request_id='edit-1').wait(0)
        self.assertEqual(conflict.error['code'], 'REQUEST_ID_CONFLICT')
        self.assertEqual(core.get_execution_status('edit-1')['status'], 'queued')
        core._timer_callback()
        replay = core.submit_code(code, request_id='edit-1').wait(0)
        self.assertEqual(replay.result, 1)
        self.assertIsNone(replay.error)
        self.assertEqual(core.bpy.count, 1)
        self.assertEqual(core._pending_tasks.qsize(), 0)

    def test_expired_result_and_cleared_history_never_silently_reexecute(self):
        _, core, _ = load_runtime_modules()
        core.bpy.count = 0
        code = 'bpy.count += 1; __result__ = bpy.count'
        with patch.object(core, 'RESULT_CACHE_TTL', 0):
            core.submit_code(code, request_id='expired')
            core._timer_callback()
        self.assertEqual(core.submit_code(code, request_id='expired').wait(0).error['code'], 'RESULT_EXPIRED')
        core.submit_code(code, request_id='clear-history')
        core._timer_callback()
        core.clear_task_history()
        self.assertEqual(core.submit_code(code, request_id='clear-history').wait(0).error['code'], 'RESULT_EXPIRED')
        self.assertEqual(core.bpy.count, 2)
        self.assertEqual(core._pending_tasks.qsize(), 0)

    def test_replay_identity_storage_is_bounded_and_active_request_is_protected(self):
        _, core, _ = load_runtime_modules()
        active = core.submit_code('__result__ = 1', request_id='active')
        with patch.object(core, 'REQUEST_HISTORY_LIMIT', 3):
            for index in range(10):
                task = core.submit_code('__result__ = 2', request_id=f'done-{index}')
                task.cancel()
            self.assertLessEqual(len(core._request_fingerprints), 3)
            self.assertIs(core.submit_code('__result__ = 1', request_id='active'), active)
        core.stop_timer()

    def test_document_load_cancels_old_queue_and_reopens_on_success_or_failure(self):
        _, core, _ = load_runtime_modules()
        core.start_timer()
        for finish in (core.bpy.app.handlers.load_post, core.bpy.app.handlers.load_post_fail):
            before = core.submit_code('bpy.wrong_document = True; __result__ = 1')
            for handler in core.bpy.app.handlers.load_pre:
                handler(None)
            self.assertEqual(before.wait(0).error['code'], 'DOCUMENT_CHANGED')
            blocked = core.submit_code('__result__ = 2').wait(0)
            self.assertEqual(blocked.error['code'], 'DOCUMENT_LOADING')
            for handler in finish:
                handler(None)
            after = core.submit_code('__result__ = 3')
            core._timer_callback()
            self.assertEqual(after.wait(0).result, 3)
            self.assertGreater(after.document_generation, before.document_generation)
        self.assertFalse(hasattr(core.bpy, 'wrong_document'))
        self.assertEqual(core._pending_tasks.unfinished_tasks, 0)
        core.stop_timer()

    def test_generation_guard_rejects_stale_task_even_if_it_missed_queue_cleanup(self):
        _, core, _ = load_runtime_modules()
        task = core.submit_code('bpy.wrong_document = True; __result__ = 1')
        core._document_generation += 1
        core._timer_callback()
        self.assertEqual(task.wait(0).error['code'], 'DOCUMENT_CHANGED')
        self.assertFalse(hasattr(core.bpy, 'wrong_document'))

    def test_start_stop_owns_handlers_and_a_late_load_callback_cannot_reopen_service(self):
        _, core, _ = load_runtime_modules()
        core.start_timer()
        core.start_timer()
        self.assertEqual(core.bpy.app.handlers.load_pre.count(core._on_load_pre), 1)
        self.assertEqual(core.bpy.app.handlers.load_post.count(core._on_load_post), 1)
        self.assertEqual(core.bpy.app.handlers.load_post_fail.count(core._on_load_post), 1)
        core._on_load_pre(None)
        core.stop_timer()
        core._on_load_post(None)
        self.assertEqual(core.submit_code('__result__ = 1').wait(0).error['code'], 'SERVICE_STOPPED')
        self.assertEqual(core.bpy.app.handlers.load_pre, [])
        self.assertEqual(core.bpy.app.handlers.load_post, [])
        self.assertEqual(core.bpy.app.handlers.load_post_fail, [])
        self.assertFalse(core.bpy.app.timers.is_registered(core._timer_callback))

    def test_one_tick_skips_all_cancelled_tasks_but_executes_only_one_live_task(self):
        _, core, _ = load_runtime_modules()
        for _ in range(8):
            core.submit_code('bpy.cancelled_ran = True; __result__ = 1').cancel()
        first = core.submit_code('__result__ = 10')
        second = core.submit_code('__result__ = 20')
        core._timer_callback()
        self.assertEqual(first.wait(0).result, 10)
        self.assertEqual(second.status, 'queued')
        self.assertFalse(hasattr(core.bpy, 'cancelled_ran'))
        core._timer_callback()
        self.assertEqual(second.wait(0).result, 20)
        self.assertEqual(core._pending_tasks.unfinished_tasks, 0)


class SharedRequestWaiterTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_disconnect_does_not_cancel_another_waiter_for_same_request(self):
        _, core, _ = load_runtime_modules()
        code = 'bpy.changed = True; __result__ = 42'
        first = asyncio.create_task(core.run_code_async(code, 1, 'shared'))
        second = asyncio.create_task(core.run_code_async(code, 1, 'shared'))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(core.get_execution_status('shared')['status'], 'queued')
        core._timer_callback()
        self.assertEqual((await second).result, 42)
        self.assertTrue(core.bpy.changed)

    async def test_all_waiters_receive_the_shared_completion(self):
        _, core, _ = load_runtime_modules()
        calls = [asyncio.create_task(core.run_code_async('__result__ = 42', 1, 'shared')) for _ in range(5)]
        await asyncio.sleep(0)
        self.assertEqual(core._pending_tasks.qsize(), 1)
        core._timer_callback()
        results = await asyncio.gather(*calls)
        self.assertTrue(all(result.result == 42 and result.error is None for result in results))

    async def test_last_disconnect_cancels_queue_and_retry_returns_cancellation(self):
        _, core, _ = load_runtime_modules()
        code = 'bpy.changed = True; __result__ = 42'
        call = asyncio.create_task(core.run_code_async(code, 1, 'cancelled'))
        await asyncio.sleep(0)
        call.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await call
        retry = await core.run_code_async(code, 1, 'cancelled')
        self.assertEqual(retry.status, 'cancelled')
        self.assertEqual(retry.error['code'], 'CANCELLED')
        core._timer_callback()
        self.assertFalse(hasattr(core.bpy, 'changed'))


if __name__ == '__main__':
    unittest.main()
