"""
eval_core.py — Python 代码执行引擎

核心职责：
  1. 在受控命名空间中执行 LLM 生成的 Python 脚本
  2. 将 bpy 及 builtins 注入脚本执行上下文
  3. 捕获 __result__ 作为返回值
  4. 所有执行必须在 Blender 主线程中完成（通过 bpy.app.timers 调度）

约定：
  LLM 生成的脚本末尾必须赋值 __result__ = ...
  若脚本抛出异常，返回 {"error": traceback_string}
"""

import bpy
from collections import deque
from dataclasses import dataclass
import queue
import threading
import time
import traceback
from typing import Any
import uuid

from .builtin_loader import BuiltinLoader
from .runtime_context import (
    CapabilityRegistry,
    LoadedBuiltinProxy,
    RuntimeContext,
    RuntimeFacade,
)


_loader: BuiltinLoader | None = None
_runtime_context: RuntimeContext | None = None

MAX_PENDING_TASKS = 32
MAX_CODE_BYTES = 256 * 1024
TASK_HISTORY_LIMIT = 50


def setup(loader: BuiltinLoader):
    global _loader, _runtime_context
    _loader = loader
    _runtime_context = RuntimeContext(CapabilityRegistry(loader))


def _make_exec_globals() -> dict:
    """构造脚本执行时的全局命名空间。"""

    def get_builtin(name: str):
        """兼容接口：加载 builtin，并将其激活到当前 Runtime Context。"""
        if _runtime_context is None:
            raise RuntimeError("RuntimeContext not initialized")
        return _runtime_context.load_compat(name)

    def get_builtin_doc(name: str) -> str:
        """
        返回指定 builtin 的完整 API 文档字符串，不加载模块（Fix1）。
        在不确定某个 builtin 的签名时，先执行：
          __result__ = get_builtin_doc('name')
        查阅文档后再写调用代码，避免盲猜 API。
        """
        if _runtime_context is None:
            raise RuntimeError("RuntimeContext not initialized")
        try:
            described = _runtime_context.describe(name)
        except ModuleNotFoundError as exc:
            return str(exc)
        return described["capabilities"][0].get("description") or "(无文档)"

    if _runtime_context is None:
        raise RuntimeError("RuntimeContext not initialized")

    return {
        "bpy": bpy,
        "runtime": RuntimeFacade(_runtime_context),
        "tools": LoadedBuiltinProxy(_runtime_context),
        "get_builtin": get_builtin,
        "get_builtin_doc": get_builtin_doc,
        "__builtins__": __builtins__,
    }


def run_code(code: str) -> Any:
    """
    在主线程同步执行 Python 代码字符串。

    调用方应确保此函数在 Blender 主线程中被调用（例如通过 bpy.app.timers）。
    返回脚本中 __result__ 的值；若未赋值则返回 None；若异常则返回错误字典。
    """
    if _loader is not None:
        _loader.begin_call()   # Fix1: 重置本次调用的新加载记录，在主线程执行前调用
    exec_globals = _make_exec_globals()
    try:
        exec(compile(code, "<llm_script>", "exec"), exec_globals)
    except Exception:
        return {
            "error": {
                "code": "EXECUTION_ERROR",
                "message": "Python execution failed",
                "details": traceback.format_exc(),
            }
        }

    if "__result__" not in exec_globals:
        return {
            "error": {
                "code": "MISSING_RESULT",
                "message": "Script did not assign __result__",
            }
        }
    return exec_globals["__result__"]


# ── 线程安全的异步执行（供 Agent 后台线程调用）──────────────────────────────

def _error_result(code: str, message: str, **details: Any) -> dict[str, Any]:
    error = {"code": code, "message": message}
    error.update(details)
    return {"error": error}


def _is_error_result(result: Any) -> bool:
    return isinstance(result, dict) and "error" in result


def _result_type(result: Any) -> str:
    if _is_error_result(result):
        return "error"
    if isinstance(result, dict) and "screenshot" in result:
        return "image"
    return type(result).__name__


def _code_summary(code: str, limit: int = 96) -> str:
    summary = " ".join(code.split())
    if len(summary) > limit:
        return summary[: limit - 1] + "…"
    return summary or "<empty>"


@dataclass(frozen=True)
class TaskOutcome:
    request_id: str
    status: str
    result: Any
    queue_ms: float
    execution_ms: float | None
    docs: tuple[str, ...] = ()


class _ExecutionTask:
    def __init__(self, code: str):
        self.request_id = uuid.uuid4().hex
        self.code = code
        self.result: Any = None
        self.status = "queued"
        self.submitted_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self._submitted_mono = time.monotonic()
        self._started_mono: float | None = None
        self.queue_ms = 0.0
        self.execution_ms: float | None = None
        self.result_type: str | None = None
        self.docs: tuple[str, ...] = ()
        self.timeout_phase: str | None = None
        self.execution_continues = False
        self._event = threading.Event()
        self._lock = threading.Lock()

    def start(self) -> bool:
        with self._lock:
            if self.status != "queued":
                return False
            self.status = "running"
            self.started_at = time.time()
            self._started_mono = time.monotonic()
            self.queue_ms = (self._started_mono - self._submitted_mono) * 1000
            return True

    def mark_done(self, result: Any, docs: list[str] | None = None) -> None:
        now_mono = time.monotonic()
        with self._lock:
            self.docs = tuple(docs or ())
            self.result_type = _result_type(result)
            self.finished_at = time.time()
            if self._started_mono is not None:
                self.execution_ms = (now_mono - self._started_mono) * 1000
            if self.status == "timed_out":
                # The caller already received a timeout. Preserve that visible
                # status while recording when the underlying Python finished.
                self.execution_continues = False
                return
            self.result = result
            self.status = "failed" if _is_error_result(result) else "succeeded"
            self._event.set()

    def reject(self, code: str, message: str, **details: Any) -> TaskOutcome:
        with self._lock:
            self.status = "failed"
            self.finished_at = time.time()
            self.queue_ms = (time.monotonic() - self._submitted_mono) * 1000
            self.result = _error_result(code, message, phase="queue", **details)
            self.result_type = "error"
            self._event.set()
            return self._outcome_locked()

    def cancel(self, message: str = "Blender execution service stopped") -> bool:
        with self._lock:
            if self.status != "queued":
                return False
            self.status = "cancelled"
            self.finished_at = time.time()
            self.queue_ms = (time.monotonic() - self._submitted_mono) * 1000
            self.result = _error_result("CANCELLED", message, phase="queue")
            self.result_type = "error"
            self._event.set()
            return True

    def _outcome_locked(self) -> TaskOutcome:
        return TaskOutcome(
            request_id=self.request_id,
            status=self.status,
            result=self.result,
            queue_ms=self.queue_ms,
            execution_ms=self.execution_ms,
            docs=self.docs,
        )

    def wait(self, timeout: float = 30.0) -> TaskOutcome:
        if self._event.wait(timeout):
            with self._lock:
                return self._outcome_locked()

        with self._lock:
            if self._event.is_set():
                return self._outcome_locked()

            now_mono = time.monotonic()
            previous_status = self.status
            self.status = "timed_out"
            self.timeout_phase = "queue" if previous_status == "queued" else "execution"
            self.finished_at = time.time() if previous_status == "queued" else None
            if previous_status == "queued":
                self.queue_ms = (now_mono - self._submitted_mono) * 1000
            elif self._started_mono is not None:
                self.execution_ms = (now_mono - self._started_mono) * 1000
                self.execution_continues = True
            self.result = _error_result(
                "QUEUE_TIMEOUT" if previous_status == "queued" else "EXECUTION_TIMEOUT",
                f"Blender execution timed out after {timeout:.1f} seconds",
                phase=self.timeout_phase,
                execution_continues=self.execution_continues,
            )
            self.result_type = "error"
            self._event.set()
            return self._outcome_locked()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "request_id": self.request_id,
                "status": self.status,
                "submitted_at": self.submitted_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "queue_ms": self.queue_ms,
                "execution_ms": self.execution_ms,
                "code_summary": _code_summary(self.code),
                "result_type": self.result_type,
                "timeout_phase": self.timeout_phase,
                "execution_continues": self.execution_continues,
            }


_pending_tasks: queue.Queue[_ExecutionTask] = queue.Queue(maxsize=MAX_PENDING_TASKS)
_task_history: deque[_ExecutionTask] = deque(maxlen=TASK_HISTORY_LIMIT)
_history_lock = threading.Lock()


def _remember_task(task: _ExecutionTask) -> None:
    with _history_lock:
        _task_history.append(task)


def get_task_history(limit: int = 10) -> list[dict[str, Any]]:
    """Return newest-first immutable snapshots for UI and diagnostics."""
    safe_limit = max(0, min(limit, TASK_HISTORY_LIMIT))
    with _history_lock:
        tasks = list(_task_history)[-safe_limit:] if safe_limit else []
    return [task.snapshot() for task in reversed(tasks)]


def clear_task_history() -> None:
    with _history_lock:
        _task_history.clear()


def _timer_callback():
    """注册到 bpy.app.timers，在主线程中轮询并执行待处理任务。"""
    try:
        task = _pending_tasks.get_nowait()
    except queue.Empty:
        task = None

    if task is not None:
        if task.start():
            result = run_code(task.code)
            docs = _loader.get_newly_loaded_descriptions() if _loader is not None else []
            task.mark_done(result, docs)
        _pending_tasks.task_done()

    return 0.05  # 每 50ms 轮询一次


def run_code_from_thread(code: str, timeout: float = 60.0) -> TaskOutcome:
    """
    从后台线程安全地在 Blender 主线程执行代码。
    阻塞直到执行完成或超时。
    """
    task = _ExecutionTask(code)
    _remember_task(task)
    code_bytes = len(code.encode("utf-8"))
    if code_bytes > MAX_CODE_BYTES:
        return task.reject(
            "CODE_TOO_LARGE",
            f"Code exceeds the {MAX_CODE_BYTES}-byte limit",
            actual_bytes=code_bytes,
            limit_bytes=MAX_CODE_BYTES,
        )
    try:
        _pending_tasks.put_nowait(task)
    except queue.Full:
        return task.reject(
            "QUEUE_FULL",
            f"Execution queue is full ({MAX_PENDING_TASKS} pending tasks)",
            limit=MAX_PENDING_TASKS,
        )
    return task.wait(timeout)


def start_timer():
    if not bpy.app.timers.is_registered(_timer_callback):
        bpy.app.timers.register(_timer_callback, persistent=True)


def stop_timer():
    if bpy.app.timers.is_registered(_timer_callback):
        bpy.app.timers.unregister(_timer_callback)

    while True:
        try:
            task = _pending_tasks.get_nowait()
        except queue.Empty:
            break
        task.cancel()
        _pending_tasks.task_done()
