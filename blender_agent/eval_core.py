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
import traceback
import threading
from typing import Any

from .builtin_loader import BuiltinLoader


_loader: BuiltinLoader | None = None


def setup(loader: BuiltinLoader):
    global _loader
    _loader = loader


def _make_exec_globals() -> dict:
    """构造脚本执行时的全局命名空间。"""

    def get_builtin(name: str):
        """LLM 在脚本中调用此函数按需获取 builtin 模块。"""
        if _loader is None:
            raise RuntimeError("BuiltinLoader not initialized")
        return _loader.load(name)

    def get_builtin_doc(name: str) -> str:
        """
        返回指定 builtin 的完整 API 文档字符串，不加载模块（Fix1）。
        在不确定某个 builtin 的签名时，先执行：
          __result__ = get_builtin_doc('name')
        查阅文档后再写调用代码，避免盲猜 API。
        """
        if _loader is None:
            raise RuntimeError("BuiltinLoader not initialized")
        doc = _loader.peek_description(name)
        if doc is None:
            available = list(_loader._registry.keys())
            return f"Builtin '{name}' 不存在或无文档。可用列表：{available}"
        return doc

    return {
        "bpy": bpy,
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
        return {"error": traceback.format_exc()}

    return exec_globals.get("__result__", None)


# ── 线程安全的异步执行（供 Agent 后台线程调用）──────────────────────────────

class _ExecutionTask:
    def __init__(self, code: str):
        self.code = code
        self.result: Any = None
        self._event = threading.Event()

    def mark_done(self, result: Any):
        self.result = result
        self._event.set()

    def wait(self, timeout: float = 30.0) -> Any:
        self._event.wait(timeout)
        return self.result


_pending_task: _ExecutionTask | None = None
_task_lock = threading.Lock()


def _timer_callback():
    """注册到 bpy.app.timers，在主线程中轮询并执行待处理任务。"""
    global _pending_task
    with _task_lock:
        task = _pending_task
        _pending_task = None

    if task is not None:
        result = run_code(task.code)
        task.mark_done(result)

    return 0.05  # 每 50ms 轮询一次


def run_code_from_thread(code: str, timeout: float = 60.0) -> Any:
    """
    从后台线程安全地在 Blender 主线程执行代码。
    阻塞直到执行完成或超时。
    """
    global _pending_task
    task = _ExecutionTask(code)
    with _task_lock:
        _pending_task = task
    return task.wait(timeout)


def start_timer():
    if not bpy.app.timers.is_registered(_timer_callback):
        bpy.app.timers.register(_timer_callback, persistent=True)


def stop_timer():
    if bpy.app.timers.is_registered(_timer_callback):
        bpy.app.timers.unregister(_timer_callback)
