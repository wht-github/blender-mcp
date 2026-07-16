"""
blender_log.py — Blender Agent 运行时诊断

提供 Blender Agent 自身的任务历史、结构化错误和 Runtime Context 状态。
不依赖 Blender 版本不稳定且通常不可访问的 Info Editor 内部报告。
"""

from typing import Optional

SUMMARY = "查询 Agent 任务历史、最近结构化错误、队列和 Runtime Context 状态"
TAGS = ["log", "diagnostics", "debug", "history", "queue", "runtime", "日志", "诊断"]
SIDE_EFFECTS = "read"
RESULT_TYPES = ["text", "object", "array"]
OPERATIONS = [
    {
        "name": "get_task_history",
        "signature": "get_task_history(limit=20, status=None) -> list[dict]",
        "summary": "返回最近任务的 request ID、状态、耗时、摘要和结构化错误字段",
        "tags": ["history", "tasks", "request_id", "历史"],
        "side_effects": "read",
        "result_types": ["array"],
        "cost": "low",
    },
    {
        "name": "get_recent_errors",
        "signature": "get_recent_errors(limit=20) -> list[dict]",
        "summary": "返回最近 failed、timed_out 或 cancelled 的 Agent 任务",
        "tags": ["errors", "timeout", "failed", "错误"],
        "side_effects": "read",
        "result_types": ["array"],
        "cost": "low",
    },
    {
        "name": "get_runtime_status",
        "signature": "get_runtime_status(history_limit=5) -> dict",
        "summary": "返回执行队列、历史数量、当前已加载能力和 runtime revision",
        "tags": ["runtime", "queue", "loaded", "状态"],
        "side_effects": "read",
        "result_types": ["object"],
        "cost": "low",
    },
]

DESCRIPTION = """
blender_log builtin — Agent 运行时诊断

函数：
  get_task_history(limit=20, status=None) -> list[dict]
    返回最近任务的 request_id、状态、排队/执行耗时、代码摘要和错误字段。
    status 可指定 queued/running/succeeded/failed/timed_out/cancelled。

  get_recent_errors(limit=20) -> list[dict]
    返回最近失败、超时或取消的任务。

  get_runtime_status(history_limit=5) -> dict
    返回当前队列、任务历史数量、已加载能力和 Runtime Context revision。

兼容接口：
  get_info_log() / get_error_log() 仍可调用，但数据来源是 Agent 任务历史，
  不是 Blender Info Editor。capture_script_output() 已停用；请直接在外层
  eval_python_code 脚本中执行代码并赋值 __result__。

示例：
  log = get_builtin('blender_log')

  failures = log.get_recent_errors(limit=10)
  __result__ = {
      "runtime": log.get_runtime_status(),
      "failures": failures,
  }
"""


def _eval_core():
    from .. import eval_core

    return eval_core


def get_task_history(limit: int = 20, status: Optional[str] = None) -> list[dict]:
    """Return newest-first Blender Agent task snapshots."""
    safe_limit = max(0, min(int(limit), 50))
    history = _eval_core().get_task_history(50 if status else safe_limit)
    if status is not None:
        normalized = status.strip().lower()
        allowed = {
            "queued",
            "running",
            "succeeded",
            "failed",
            "timed_out",
            "cancelled",
        }
        if normalized not in allowed:
            raise ValueError(f"Unknown task status '{status}'. Available: {sorted(allowed)}")
        history = [item for item in history if item["status"] == normalized]
    return history[:safe_limit]


def get_recent_errors(limit: int = 20) -> list[dict]:
    """Return recent failed, timed out, or cancelled task snapshots."""
    error_statuses = {"failed", "timed_out", "cancelled"}
    return [
        item
        for item in _eval_core().get_task_history(50)
        if item["status"] in error_statuses
    ][: max(0, min(int(limit), 50))]


def get_runtime_status(history_limit: int = 5) -> dict:
    """Return compact queue, history, and loaded capability state."""
    return _eval_core().get_runtime_diagnostics(history_limit=history_limit)


def get_info_log(limit: int = 50, filter_keyword: Optional[str] = None) -> list[dict]:
    """Compatibility view over Agent task history, not Blender Info Editor."""
    logs = [
        {
            "type": task["status"].upper(),
            "message": task["code_summary"],
            "request_id": task["request_id"],
            "queue_ms": task["queue_ms"],
            "execution_ms": task["execution_ms"],
        }
        for task in get_task_history(limit=limit)
    ]
    if filter_keyword:
        keyword = filter_keyword.lower()
        logs = [item for item in logs if keyword in item["message"].lower()]
    return logs


def get_error_log(limit: int = 20) -> list[dict]:
    """Compatibility alias for get_recent_errors()."""
    return get_recent_errors(limit=limit)


def capture_script_output(code: str) -> dict:
    """Deprecated compatibility stub; nested eval has intentionally been removed."""
    del code
    return {
        "error": {
            "code": "DEPRECATED_NESTED_EVAL",
            "message": (
                "capture_script_output() is disabled. Execute the code directly in "
                "eval_python_code and assign its value to __result__."
            ),
        }
    }
