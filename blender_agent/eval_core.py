"""Execute scripts on Blender's main thread and retain bounded task outcomes."""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import queue
import threading
import time
import traceback
from typing import Any
import uuid

import bpy

from .builtin_loader import BuiltinLoader
from .result_codec import snapshot_result, ResultValidationError
from .runtime_context import CapabilityRegistry, LoadedBuiltinProxy, RuntimeContext, RuntimeFacade


MAX_PENDING_TASKS = 32
MAX_CODE_BYTES = 256 * 1024
TASK_HISTORY_LIMIT = 50
REQUEST_HISTORY_LIMIT = 256
RESULT_CACHE_BYTES = 16 * 1024 * 1024
RESULT_CACHE_TTL = 300.0

_loader: BuiltinLoader | None = None


@dataclass(frozen=True)
class ExecutionResult:
    """Execution failures are separate from arbitrary user data, including 'error'."""

    value: Any = None
    error: dict[str, Any] | None = None
    docs: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskOutcome:
    request_id: str
    status: str
    result: Any
    queue_ms: float
    execution_ms: float | None
    docs: tuple[str, ...] = ()
    error: dict[str, Any] | None = None
    execution_status: str = "not_started"
    document_generation: int = 0


def setup(loader: BuiltinLoader):
    global _loader, _accepting, _service_enabled
    _loader = loader
    with _service_lock:
        _service_enabled = True
        _accepting = not _loading_document


def _make_exec_globals() -> dict:
    if _loader is None:
        raise RuntimeError("Builtin loader not initialized")
    context = RuntimeContext(CapabilityRegistry(_loader))

    def get_builtin_doc(name: str) -> str:
        try:
            described = context.describe(name)
        except ModuleNotFoundError as exc:
            return str(exc)
        capability = described["capabilities"][0]
        if capability.get("description"):
            return capability["description"]
        if capability.get("kind") == "builtin_operation":
            return (
                f"{capability['signature']}\n{capability['summary']}\n"
                f"side_effects={capability['side_effects']} "
                f"requires_ui_context={capability['requires_ui_context']} "
                f"cost={capability['cost']}"
            )
        return "(无文档)"

    return {
        "bpy": bpy,
        "runtime": RuntimeFacade(context),
        "tools": LoadedBuiltinProxy(context),
        "get_builtin": context.load_compat,
        "get_builtin_doc": get_builtin_doc,
        "__builtins__": __builtins__,
    }


def _error(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "message": message, **details}


def _safe_traceback() -> str:
    return traceback.format_exc()[-16384:].encode("utf-8", errors="replace").decode("utf-8")


def run_code(code: str) -> ExecutionResult:
    """Execute synchronously on the main thread, with fresh capability activation."""
    try:
        exec_globals = _make_exec_globals()
        exec(compile(code, "<llm_script>", "exec"), exec_globals)
    except BaseException:
        # User code must not terminate Blender's timer with SystemExit etc.
        return ExecutionResult(error=_error(
            "EXECUTION_ERROR", "Python execution failed", details=_safe_traceback(),
        ))
    if "__result__" not in exec_globals:
        return ExecutionResult(error=_error("MISSING_RESULT", "Script did not assign __result__"))
    try:
        return ExecutionResult(value=snapshot_result(exec_globals["__result__"]))
    except (ResultValidationError, UnicodeError) as exc:
        return ExecutionResult(error=_error("INVALID_RESULT", str(exc)))


def _result_type(result: ExecutionResult) -> str:
    if result.error is not None:
        return "error"
    if isinstance(result.value, dict) and "screenshot" in result.value:
        return "image"
    return type(result.value).__name__


def _code_summary(code: str, limit: int = 96) -> str:
    summary = " ".join(code.split())
    return summary[:limit - 1] + "…" if len(summary) > limit else summary or "<empty>"


class _ExecutionTask:
    def __init__(self, code: str, timeout: float | None = None,
                 request_id: str | None = None, *, tracked: bool = True):
        self.request_id = request_id or uuid.uuid4().hex
        self.code = code
        self.code_summary = _code_summary(code[:512])
        self.document_generation = _document_generation
        self.result: Any = None
        self.error: dict[str, Any] | None = None
        self.status = "queued"
        self.execution_status = "not_started"
        self.submitted_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self._submitted_mono = time.monotonic()
        self.deadline = None if timeout is None else self._submitted_mono + max(0, timeout)
        self._started_mono: float | None = None
        self.queue_ms = 0.0
        self.execution_ms: float | None = None
        self.result_type: str | None = None
        self.docs: tuple[str, ...] = ()
        self.timeout_phase: str | None = None
        self.execution_continues = False
        self._tracked = tracked
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._waiters: dict[asyncio.Future, asyncio.AbstractEventLoop] = {}
        self._sync_waiters = 0

    def _publish_locked(self) -> None:
        if not self._tracked:
            return
        with _history_lock:
            _task_history[self.request_id] = self._snapshot_locked()
            while len(_task_history) > TASK_HISTORY_LIMIT:
                _task_history.popitem(last=False)
            if self.finished_at is not None:
                _active_tasks.pop(self.request_id, None)

    def _signal_locked(self) -> None:
        self._event.set()
        self._publish_locked()
        outcome = self._outcome_locked()
        for future, loop in self._waiters.items():
            try:
                loop.call_soon_threadsafe(_deliver_outcome, future, outcome)
            except RuntimeError:
                pass  # A disconnected client's loop may already be closed.
        self._waiters.clear()

    def start(self) -> bool:
        with self._lock:
            if self.status != "queued":
                return False
            if self.document_generation != _document_generation:
                self._cancel_locked("The Blender document changed", code="DOCUMENT_CHANGED")
                return False
            if self.deadline is not None and time.monotonic() >= self.deadline:
                self._expire_locked()
                return False
            self.status = "running"
            self.execution_status = "running"
            self.started_at = time.time()
            self._started_mono = time.monotonic()
            self.queue_ms = (self._started_mono - self._submitted_mono) * 1000
            self._publish_locked()
            return True

    def _retain_locked(self, result: ExecutionResult) -> None:
        if self._tracked:
            try:
                _cache_result(self.request_id, result)
            except Exception:
                # Retention is optional; a cache failure must not stop delivery
                # or disable Blender's execution timer.
                pass

    def mark_done(self, result: ExecutionResult) -> None:
        with self._lock:
            if not self._event.is_set() and self.deadline is not None and time.monotonic() >= self.deadline:
                self._expire_locked()
            self.docs = result.docs
            self.result_type = _result_type(result)
            self.finished_at = time.time()
            if self._started_mono is not None:
                self.execution_ms = (time.monotonic() - self._started_mono) * 1000
            self.execution_status = "failed" if result.error is not None else "succeeded"
            self.execution_continues = False
            self._retain_locked(result)
            if self._event.is_set():
                # The final execution result is queryable; preserve the wait response.
                self._publish_locked()
                return
            self.result, self.error = result.value, result.error
            self.status = self.execution_status
            self._signal_locked()

    def reject(self, code: str, message: str, **details: Any) -> TaskOutcome:
        with self._lock:
            self.status = "failed"
            self.finished_at = time.time()
            self.queue_ms = (time.monotonic() - self._submitted_mono) * 1000
            self.error = _error(code, message, phase="queue", **details)
            self.result_type = "error"
            self.code = ""
            self._retain_locked(ExecutionResult(error=self.error))
            self._signal_locked()
            return self._outcome_locked()

    def _cancel_locked(self, message: str, *, detach_running=False, code="CANCELLED") -> bool:
        if self._event.is_set():
            return False
        queued = self.status == "queued"
        if not queued and not detach_running:
            return False
        self.status = "cancelled"
        self.finished_at = time.time() if queued else None
        self.execution_continues = not queued
        if queued:
            self.queue_ms = (time.monotonic() - self._submitted_mono) * 1000
        self.error = _error(
            code, message, phase="queue" if queued else "execution",
            execution_continues=self.execution_continues,
        )
        self.result_type = "error"
        if queued:
            self._retain_locked(ExecutionResult(error=self.error))
        self._signal_locked()
        return queued

    def cancel(self, message: str = "Blender execution service stopped", *, detach_running=False,
               code="CANCELLED") -> bool:
        with self._lock:
            return self._cancel_locked(message, detach_running=detach_running, code=code)

    def _outcome_locked(self) -> TaskOutcome:
        return TaskOutcome(self.request_id, self.status, self.result,
                           self.queue_ms, self.execution_ms, self.docs, self.error,
                           self.execution_status, self.document_generation)

    def _expire_locked(self) -> TaskOutcome:
        if self._event.is_set():
            return self._outcome_locked()
        queued = self.status == "queued"
        self.status = "timed_out"
        self.timeout_phase = "queue" if queued else "execution"
        self.finished_at = time.time() if queued else None
        if queued:
            self.queue_ms = (time.monotonic() - self._submitted_mono) * 1000
        elif self._started_mono is not None:
            self.execution_ms = (time.monotonic() - self._started_mono) * 1000
            self.execution_continues = True
        self.error = _error(
            "QUEUE_TIMEOUT" if queued else "EXECUTION_TIMEOUT",
            "Blender execution exceeded the request deadline",
            phase=self.timeout_phase, execution_continues=self.execution_continues,
        )
        self.result_type = "error"
        if queued:
            self._retain_locked(ExecutionResult(error=self.error))
        self._signal_locked()
        return self._outcome_locked()

    def _remaining(self, timeout: float) -> float:
        return timeout if self.deadline is None else max(0, self.deadline - time.monotonic())

    def wait(self, timeout: float = 30.0) -> TaskOutcome:
        with self._lock:
            self._sync_waiters += 1
        try:
            self._event.wait(self._remaining(timeout))
            with self._lock:
                return self._expire_locked()
        finally:
            with self._lock:
                self._sync_waiters -= 1

    async def wait_async(self, timeout: float) -> TaskOutcome:
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        with self._lock:
            if self._event.is_set():
                return self._outcome_locked()
            self._waiters[future] = loop
        cancelled = False
        try:
            return await asyncio.wait_for(future, self._remaining(timeout))
        except TimeoutError:
            with self._lock:
                return self._expire_locked()
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            with self._lock:
                self._waiters.pop(future, None)
                if cancelled and not self._waiters and not self._sync_waiters:
                    self._cancel_locked("Client stopped waiting", detach_running=True)

    def _snapshot_locked(self) -> dict[str, Any]:
        error = self.error or {}
        return {
            "request_id": self.request_id, "status": self.status,
            "document_generation": self.document_generation,
            "execution_status": self.execution_status,
            "submitted_at": self.submitted_at, "started_at": self.started_at,
            "finished_at": self.finished_at, "queue_ms": self.queue_ms,
            "execution_ms": self.execution_ms, "code_summary": self.code_summary,
            "result_type": self.result_type,
            "error_code": str(error["code"])[:128] if "code" in error else None,
            "error_message": str(error["message"])[:512] if "message" in error else None,
            "timeout_phase": self.timeout_phase, "execution_continues": self.execution_continues,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()


def _deliver_outcome(future: asyncio.Future[TaskOutcome], outcome: TaskOutcome) -> None:
    if not future.done():
        future.set_result(outcome)


_pending_tasks: queue.Queue[_ExecutionTask] = queue.Queue(maxsize=MAX_PENDING_TASKS)
_task_history: OrderedDict[str, dict[str, Any]] = OrderedDict()
_active_tasks: dict[str, _ExecutionTask] = {}
# Fingerprints outlive result eviction. Keeping an identity never permits silent replay.
_request_fingerprints: OrderedDict[str, str] = OrderedDict()
_history_lock = threading.Lock()
_service_lock = threading.Lock()
_accepting = False
_service_enabled = False
_loading_document = False
_document_generation = 0
_result_cache: OrderedDict[str, tuple[float, str, int]] = OrderedDict()
_result_cache_size = 0


def _prune_results_locked() -> None:
    global _result_cache_size
    now = time.monotonic()
    while _result_cache:
        _, (expires, _, size) = next(iter(_result_cache.items()))
        if expires > now and _result_cache_size <= RESULT_CACHE_BYTES and len(_result_cache) <= TASK_HISTORY_LIMIT:
            break
        _result_cache.popitem(last=False)
        _result_cache_size -= size


def _cache_result(request_id: str, result: ExecutionResult) -> None:
    global _result_cache_size
    encoded = json.dumps({"result": result.value, "error": result.error, "docs": result.docs},
                         ensure_ascii=False, allow_nan=False)
    size = len(encoded.encode("utf-8"))
    with _history_lock:
        previous = _result_cache.pop(request_id, None)
        if previous:
            _result_cache_size -= previous[2]
        _result_cache[request_id] = (time.monotonic() + RESULT_CACHE_TTL, encoded, size)
        _result_cache_size += size
        _prune_results_locked()


def _remember_task(task: _ExecutionTask, fingerprint: str) -> None:
    snapshot = task.snapshot()
    with _history_lock:
        _task_history[task.request_id] = snapshot
        _active_tasks[task.request_id] = task
        _request_fingerprints[task.request_id] = fingerprint
        while len(_task_history) > TASK_HISTORY_LIMIT:
            _task_history.popitem(last=False)
        # Active identities are protected until their execution has finished.
        for request_id in list(_request_fingerprints):
            if len(_request_fingerprints) <= REQUEST_HISTORY_LIMIT:
                break
            if request_id not in _active_tasks:
                del _request_fingerprints[request_id]


def get_task_history(limit: int = 10) -> list[dict[str, Any]]:
    with _history_lock:
        return [dict(item) for item in list(_task_history.values())[::-1][:max(0, min(limit, TASK_HISTORY_LIMIT))]]


def get_execution_status(request_id: str, include_result: bool = False) -> dict[str, Any]:
    """Read execution state without scheduling work on Blender's main thread."""
    with _history_lock:
        _prune_results_locked()
        state = _task_history.get(request_id)
        task = _active_tasks.get(request_id)
        cached = _result_cache.get(request_id)
        state = dict(state) if state is not None else None
    if task is not None:
        state = task.snapshot()
    if state is None:
        return {"request_id": request_id, "status": "not_found",
                "error": _error("TASK_NOT_FOUND", "Task history is no longer available")}
    state["result_available"] = cached is not None
    if include_result and cached is not None:
        state.update(json.loads(cached[1]))
    return state


def get_runtime_diagnostics(history_limit: int = 10) -> dict[str, Any]:
    with _history_lock:
        history_count = len(_task_history)
    return {
        "queue": {"pending": _pending_tasks.qsize(), "capacity": _pending_tasks.maxsize},
        "history_count": history_count, "recent_tasks": get_task_history(history_limit),
        "document_generation": _document_generation, "accepting": _accepting,
        "runtime": {"scope": "execution", "available_count": len(_loader.list_metadata())} if _loader else None,
    }


def clear_task_history() -> None:
    global _result_cache_size
    with _history_lock:
        _task_history.clear()
        _result_cache.clear()
        _result_cache_size = 0
        # Do not erase replay protection when the user clears diagnostics.


def _timer_callback():
    """Skip terminal queue entries, then execute at most one live task per tick."""
    with _history_lock:
        _prune_results_locked()
    while True:
        with _service_lock:
            if not _accepting:
                return 0.05
            try:
                task = _pending_tasks.get_nowait()
            except queue.Empty:
                return 0.05
            started = task.start()
        try:
            if started:
                try:
                    result = run_code(task.code)
                except BaseException:
                    result = ExecutionResult(error=_error(
                        "EXECUTION_ERROR", "Execution service failed", details=_safe_traceback(),
                    ))
                task.mark_done(result)
                return 0.05
        finally:
            task.code = ""
            _pending_tasks.task_done()


def _rejected_task(code: str, request_id: str | None, error_code: str, message: str) -> _ExecutionTask:
    task = _ExecutionTask(code, request_id=request_id, tracked=False)
    task.reject(error_code, message)
    return task


def _replay_task(request_id: str, fingerprint: str) -> _ExecutionTask | None:
    """Called under the admission lock; never replace an existing identity."""
    with _history_lock:
        _prune_results_locked()
        previous = _request_fingerprints.get(request_id)
        active = _active_tasks.get(request_id)
        state = _task_history.get(request_id)
        cached = _result_cache.get(request_id)
    if previous is None:
        return None
    if previous != fingerprint:
        return _rejected_task("", request_id, "REQUEST_ID_CONFLICT", "Request ID was already used with different code")
    if active is not None:
        return active
    if cached is None or state is None:
        return _rejected_task("", request_id, "RESULT_EXPIRED", "Request is known but its result is no longer retained; code was not executed again")
    payload = json.loads(cached[1])
    task = _ExecutionTask("", request_id=request_id, tracked=False)
    task.status = state["status"]
    task.execution_status = state["execution_status"]
    task.document_generation = state["document_generation"]
    # A retry after an execution timeout can return the now-completed result.
    if task.execution_status in ("succeeded", "failed"):
        task.status = task.execution_status
    task.result, task.error = payload["result"], payload["error"]
    task.docs = tuple(payload["docs"])
    task.queue_ms, task.execution_ms = state["queue_ms"], state["execution_ms"]
    task._event.set()
    return task


def submit_code(code: str, timeout: float = 60.0, request_id: str | None = None) -> _ExecutionTask:
    """Same retained request ID and source share one task and its first deadline."""
    if request_id is not None and (not isinstance(request_id, str) or not request_id or len(request_id) > 128):
        return _rejected_task("", None, "INVALID_REQUEST_ID", "Request ID must be a non-empty string of at most 128 characters")
    if request_id is not None:
        try:
            request_id.encode("utf-8")
        except UnicodeError:
            return _rejected_task("", None, "INVALID_REQUEST_ID", "Request ID must be valid UTF-8 text")
    try:
        encoded = code.encode("utf-8")
    except UnicodeError:
        return _rejected_task("", request_id, "INVALID_CODE", "Code must be valid UTF-8 text")
    fingerprint = hashlib.sha256(encoded).hexdigest()
    with _service_lock:
        if request_id is not None:
            existing = _replay_task(request_id, fingerprint)
            if existing is not None:
                return existing
        task = _ExecutionTask(code, timeout, request_id)
        _remember_task(task, fingerprint)
        if len(encoded) > MAX_CODE_BYTES:
            task.reject("CODE_TOO_LARGE", f"Code exceeds the {MAX_CODE_BYTES}-byte limit", limit_bytes=MAX_CODE_BYTES)
        elif _loading_document:
            task.reject("DOCUMENT_LOADING", "Blender is loading a document")
        elif not _accepting:
            task.reject("SERVICE_STOPPED", "Blender execution service is stopped")
        else:
            try:
                _pending_tasks.put_nowait(task)
            except queue.Full:
                task.reject("QUEUE_FULL", f"Execution queue is full ({_pending_tasks.maxsize} pending tasks)", limit=_pending_tasks.maxsize)
    return task


def run_code_from_thread(code: str, timeout: float = 60.0, request_id: str | None = None) -> TaskOutcome:
    return submit_code(code, timeout, request_id).wait(timeout)


async def run_code_async(code: str, timeout: float = 60.0, request_id: str | None = None) -> TaskOutcome:
    return await submit_code(code, timeout, request_id).wait_async(timeout)


def _cancel_pending(message: str, code: str = "CANCELLED") -> None:
    while True:
        try:
            task = _pending_tasks.get_nowait()
        except queue.Empty:
            break
        task.cancel(message, code=code)
        task.code = ""
        _pending_tasks.task_done()


@bpy.app.handlers.persistent
def _on_load_pre(_unused):
    global _accepting, _loading_document, _document_generation
    with _service_lock:
        _accepting = False
        _loading_document = True
        _document_generation += 1
        _cancel_pending("The Blender document changed", "DOCUMENT_CHANGED")


@bpy.app.handlers.persistent
def _on_load_post(_unused):
    global _accepting, _loading_document
    with _service_lock:
        _loading_document = False
        _accepting = _service_enabled


def _document_handlers():
    return (
        (bpy.app.handlers.load_pre, _on_load_pre),
        (bpy.app.handlers.load_post, _on_load_post),
        (bpy.app.handlers.load_post_fail, _on_load_post),
    )


def start_timer():
    global _accepting, _service_enabled
    with _service_lock:
        _service_enabled = True
        _accepting = not _loading_document
    for handlers, callback in _document_handlers():
        if callback not in handlers:
            handlers.append(callback)
    if not bpy.app.timers.is_registered(_timer_callback):
        bpy.app.timers.register(_timer_callback, persistent=True)


def stop_timer():
    global _accepting, _service_enabled, _loading_document
    with _service_lock:
        _accepting = _service_enabled = _loading_document = False
        _cancel_pending("Blender execution service stopped")
    if bpy.app.timers.is_registered(_timer_callback):
        bpy.app.timers.unregister(_timer_callback)
    for handlers, callback in _document_handlers():
        if callback in handlers:
            handlers.remove(callback)
