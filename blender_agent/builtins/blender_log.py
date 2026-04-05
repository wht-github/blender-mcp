"""
blender_log.py — 获取 Blender 系统日志与信息报告

通过钩子收集 Blender 的 Info 日志（操作历史）和运行时 print 输出。
LLM 可在脚本中过滤，只返回相关条目，避免上下文污染。
"""

import bpy
import sys
import io
from contextlib import redirect_stdout, redirect_stderr
from typing import Optional

SUMMARY = "获取 Blender 操作日志（Info 日志）和脚本 print 输出，支持关键词过滤"

DESCRIPTION = """
blender_log builtin — 日志获取与过滤

函数：
  get_info_log(limit=50, filter_keyword=None) -> list[dict]
    获取 Blender Info 区域的操作日志（相当于操作历史）。
    每条 dict: {"type": str, "message": str}
    filter_keyword: 若提供，只返回包含该关键词的条目（大小写不敏感）。

  capture_script_output(code: str) -> dict
    执行一段 Python 代码并捕获其 print 输出。
    返回 {"stdout": str, "stderr": str, "result": any}
    注意：此函数内部调用 exec()，代码在当前命名空间运行。

  get_error_log(limit=20) -> list[str]
    返回最近的错误/警告类型日志。

示例：
  log = get_builtin('blender_log')

  # 只获取与 modifier 相关的操作历史
  mod_logs = log.get_info_log(limit=100, filter_keyword='modifier')
  return mod_logs

  # 捕获脚本输出用于调试
  output = log.capture_script_output("for obj in bpy.data.objects: print(obj.name)")
  return output["stdout"]
"""


def get_info_log(limit: int = 50, filter_keyword: Optional[str] = None) -> list:
    """获取 Blender Info 区域日志，可按关键词过滤。"""
    logs = []
    # Blender 4.x：Info 日志通过报告存储在 window_manager 的 reports 中
    # 兜底方法：通过重定向 Info 区域读取
    try:
        for report in bpy.context.window_manager.reports:
            entry = {
                "type": str(report.type),
                "message": report.message,
            }
            if filter_keyword is None or filter_keyword.lower() in report.message.lower():
                logs.append(entry)
    except AttributeError:
        # Blender 某些版本没有直接的 reports 属性，尝试 Info 区域
        logs = _read_info_area(limit)

    # 按 filter
    if filter_keyword:
        logs = [l for l in logs if filter_keyword.lower() in l.get("message", "").lower()]

    return logs[-limit:]


def _read_info_area(limit: int) -> list:
    """备用：通过 Info 区域 space_data 读取日志。"""
    logs = []
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "INFO":
                space = area.spaces.active
                # INFO 区域无直接 Python API 获取文本，返回空
                break
    return logs


def get_error_log(limit: int = 20) -> list:
    """返回最近的错误/警告类型日志。"""
    error_types = {"ERROR", "WARNING", "ERROR_INVALID_INPUT", "ERROR_INVALID_CONTEXT"}
    logs = []
    try:
        for report in bpy.context.window_manager.reports:
            if str(report.type) in error_types:
                logs.append({"type": str(report.type), "message": report.message})
    except AttributeError:
        pass
    return logs[-limit:]


def capture_script_output(code: str) -> dict:
    """执行代码并捕获 stdout/stderr。"""
    import bpy as _bpy
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    local_ns = {"bpy": _bpy}
    result = None
    with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
        try:
            exec(compile(code, "<capture>", "exec"), local_ns)
            result = local_ns.get("__result__")
        except Exception as e:
            import traceback
            print(traceback.format_exc(), file=sys.stderr)

    return {
        "stdout": stdout_buf.getvalue(),
        "stderr": stderr_buf.getvalue(),
        "result": result,
    }
