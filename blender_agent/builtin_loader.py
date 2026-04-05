"""
builtin_loader.py — Builtins 发现与懒加载管理器

两级文档机制：
  SUMMARY     = "一行摘要"          # 始终注入 tool description（上下文常驻）
  DESCRIPTION = "完整 API 文档..."  # LLM 首次使用该模块时才追加到消息

目录约定：
  builtins/*.py  每个文件是一个独立 builtin 模块
"""

import importlib
import importlib.util
import sys
import os
from pathlib import Path
from types import ModuleType
from typing import Optional


_BUILTINS_DIR = Path(__file__).parent / "builtins"


class BuiltinLoader:
    def __init__(self):
        self._registry: dict[str, Path] = {}        # name -> .py path
        self._loaded: dict[str, ModuleType] = {}    # name -> module
        self._descriptions_shown: set[str] = set()  # 已展示 description 的模块名
        self._summaries: dict[str, str] = {}        # Fix2: 发现时缓存，避免每轮重复 exec
        self._newly_loaded: list[str] = []          # Fix1: 本次 eval 调用中首次加载的模块

        self._discover()

    def _discover(self):
        """扫描 builtins/ 目录，注册所有 .py 模块，并缓存 SUMMARY。"""
        self._registry.clear()
        self._summaries.clear()
        for path in sorted(_BUILTINS_DIR.glob("*.py")):
            if path.name.startswith("_"):
                continue
            name = path.stem
            self._registry[name] = path
            self._summaries[name] = self._read_attr(path, "SUMMARY") or "(无摘要)"

    def get_summaries(self) -> str:
        """
        返回所有 builtins 的 SUMMARY 汇总，注入到 eval_python_code 的 tool description。
        使用 _discover() 时缓存的摘要，避免每轮重复 exec 模块文件（Fix2）。
        """
        lines = ["可用 builtins（在脚本中通过 get_builtin('name') 获取）："]
        for name, summary in self._summaries.items():
            lines.append(f"  - {name}: {summary}")
        return "\n".join(lines)

    def load(self, name: str) -> ModuleType:
        """按需加载 builtin 模块，返回模块对象。"""
        if name in self._loaded:
            return self._loaded[name]

        if name not in self._registry:
            raise ModuleNotFoundError(f"Builtin '{name}' not found. Available: {list(self._registry)}")

        path = self._registry[name]
        module_name = f"blender_agent.builtins.{name}"

        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        self._loaded[name] = module
        self._newly_loaded.append(name)   # Fix1: 追踪本次调用中的新加载
        return module

    def get_pending_descriptions(self) -> list[str]:
        """
        返回已加载但尚未展示 DESCRIPTION 的模块文档列表（后备方法）。
        主路径请使用 get_newly_loaded_descriptions()，它在同一轮 tool result 中注入。
        """
        pending = []
        for name, module in self._loaded.items():
            if name not in self._descriptions_shown:
                desc = getattr(module, "DESCRIPTION", None)
                if desc:
                    pending.append(f"[builtin: {name}]\n{desc}")
                self._descriptions_shown.add(name)
        return pending

    def peek_description(self, name: str) -> Optional[str]:
        """
        读取指定 builtin 的 DESCRIPTION，不加载模块。
        供 eval 命名空间中的 get_builtin_doc() 使用，让模型在写代码前先查文档。
        """
        if name not in self._registry:
            return None
        if name in self._loaded:
            return getattr(self._loaded[name], "DESCRIPTION", None)
        return self._read_attr(self._registry[name], "DESCRIPTION")

    def begin_call(self):
        """在每次代码执行前调用，重置本次 eval 的新加载记录（Fix1）。"""
        self._newly_loaded.clear()

    def get_newly_loaded_descriptions(self) -> list[str]:
        """
        返回本次 eval 调用中首次加载的 builtin 的 DESCRIPTION（Fix1）。
        在代码执行后立即调用，将文档附加进同一轮 tool result，
        让模型下次写代码时已持有正确的 API 签名，无需再多一轮往返。
        """
        docs = []
        for name in self._newly_loaded:
            if name not in self._descriptions_shown:
                desc = getattr(self._loaded[name], "DESCRIPTION", None)
                if desc:
                    docs.append(f"[builtin: {name} — API 文档]\n{desc}")
                self._descriptions_shown.add(name)
        return docs

    def reset_session(self):
        """
        新对话开始时重置全部会话状态（Fix3）。
        清除已加载模块缓存，避免上轮会话的 builtins 在新对话首轮污染上下文。
        """
        self._loaded.clear()
        self._descriptions_shown.clear()
        self._newly_loaded.clear()

    def reload(self):
        """重新扫描目录（开发时热重载用）。"""
        self._loaded.clear()
        self._descriptions_shown.clear()
        self._newly_loaded.clear()
        self._discover()

    @staticmethod
    def _read_attr(path: Path, attr: str) -> Optional[str]:
        """快速读取模块顶层变量，不执行整个模块。"""
        try:
            spec = importlib.util.spec_from_file_location("_tmp", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return getattr(mod, attr, None)
        except Exception:
            return None
