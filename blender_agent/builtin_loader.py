"""
builtin_loader.py — Builtins 发现与懒加载管理器

渐进式文档机制：
  SUMMARY      = "一行摘要"          # capability search 返回
  TAGS         = ["scene", ...]      # capability search 索引
  SIDE_EFFECTS = "read|write|mixed"  # 能力副作用提示
  RESULT_TYPES = ["text", ...]       # 典型结果类型
  DESCRIPTION  = "完整 API 文档..."  # describe/load 时才披露

目录约定：
  builtins/*.py  每个文件是一个独立 builtin 模块
"""

import ast
from dataclasses import dataclass
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Optional


_BUILTINS_DIR = Path(__file__).parent / "builtins"


@dataclass(frozen=True)
class BuiltinMetadata:
    name: str
    capability_id: str
    summary: str
    description: str | None
    tags: tuple[str, ...]
    side_effects: str
    result_types: tuple[str, ...]

    def as_dict(self, *, include_description: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.capability_id,
            "name": self.name,
            "kind": "builtin",
            "summary": self.summary,
            "tags": list(self.tags),
            "side_effects": self.side_effects,
            "result_types": list(self.result_types),
        }
        if include_description:
            result["description"] = self.description
        return result


class BuiltinLoader:
    def __init__(self):
        self._registry: dict[str, Path] = {}        # name -> .py path
        self._loaded: dict[str, ModuleType] = {}    # name -> module
        self._descriptions_shown: set[str] = set()  # 已展示 description 的模块名
        self._metadata: dict[str, BuiltinMetadata] = {}
        self._newly_loaded: list[str] = []          # Fix1: 本次 eval 调用中首次加载的模块

        self._discover()

    def _discover(self):
        """扫描 builtins/ 目录并通过 AST 缓存能力元数据，不执行模块。"""
        self._registry.clear()
        self._metadata.clear()
        for path in sorted(_BUILTINS_DIR.glob("*.py")):
            if path.name.startswith("_"):
                continue
            name = path.stem
            self._registry[name] = path
            summary = self._read_attr(path, "SUMMARY") or "(无摘要)"
            description = self._read_attr(path, "DESCRIPTION")
            raw_tags = self._read_literal(path, "TAGS")
            raw_side_effects = self._read_literal(path, "SIDE_EFFECTS")
            raw_result_types = self._read_literal(path, "RESULT_TYPES")
            self._metadata[name] = BuiltinMetadata(
                name=name,
                capability_id=f"builtin.{name}",
                summary=summary,
                description=description,
                tags=self._string_tuple(raw_tags, fallback=(name,)),
                side_effects=(
                    raw_side_effects
                    if isinstance(raw_side_effects, str)
                    and raw_side_effects in {"read", "write", "mixed"}
                    else "mixed"
                ),
                result_types=self._string_tuple(raw_result_types, fallback=("text",)),
            )

    def get_summaries(self) -> str:
        """
        返回所有 builtins 的 SUMMARY 汇总。

        仅为兼容旧调用保留；R1 后不再注入 MCP tool description。
        """
        lines = ["可用 builtins（在脚本中通过 get_builtin('name') 获取）："]
        for metadata in self._metadata.values():
            lines.append(f"  - {metadata.name}: {metadata.summary}")
        return "\n".join(lines)

    def load(self, name: str) -> ModuleType:
        """按需加载 builtin 模块，返回模块对象。"""
        name = self.normalize_name(name)
        if name in self._loaded:
            return self._loaded[name]

        if name not in self._registry:
            raise ModuleNotFoundError(f"Builtin '{name}' not found. Available: {list(self._registry)}")

        path = self._registry[name]
        module_name = f"blender_agent.builtins.{name}"

        spec = self._require_spec(path, module_name)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        self._loaded[name] = module
        self._newly_loaded.append(name)   # Fix1: 追踪本次调用中的新加载
        return module

    def normalize_name(self, name: str) -> str:
        """Accept both ``scene_info`` and the stable ``builtin.scene_info`` ID."""
        normalized = name.removeprefix("builtin.")
        if normalized not in self._registry:
            available = [metadata.capability_id for metadata in self._metadata.values()]
            raise ModuleNotFoundError(
                f"Builtin '{name}' not found. Available capability IDs: {available}"
            )
        return normalized

    def get_metadata(self, name: str) -> BuiltinMetadata:
        return self._metadata[self.normalize_name(name)]

    def list_metadata(self) -> list[BuiltinMetadata]:
        return list(self._metadata.values())

    def loaded_names(self) -> list[str]:
        return list(self._loaded)

    def mark_descriptions_shown(self, *names: str) -> None:
        """Record explicit describe() disclosure so load() does not repeat it."""
        self._descriptions_shown.update(self.normalize_name(name) for name in names)

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
        try:
            name = self.normalize_name(name)
        except ModuleNotFoundError:
            return None
        if name in self._loaded:
            return getattr(self._loaded[name], "DESCRIPTION", None)
        return self._metadata[name].description

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
    def _require_spec(path: Path, module_name: str):
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Unable to create module spec for '{module_name}' from '{path}'")
        return spec

    @staticmethod
    def _read_attr(path: Path, attr: str) -> Optional[str]:
        """读取模块顶层字符串常量，不导入或执行模块。"""
        value = BuiltinLoader._read_literal(path, attr)
        return value if isinstance(value, str) else None

    @staticmethod
    def _read_literal(path: Path, attr: str) -> Any:
        """Read one top-level literal assignment without importing the module."""
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in tree.body:
                if isinstance(node, ast.Assign):
                    targets = node.targets
                    value_node = node.value
                elif isinstance(node, ast.AnnAssign):
                    targets = [node.target]
                    value_node = node.value
                else:
                    continue
                if value_node is not None and any(
                    isinstance(target, ast.Name) and target.id == attr for target in targets
                ):
                    return ast.literal_eval(value_node)
        except (OSError, SyntaxError, ValueError):
            return None
        return None

    @staticmethod
    def _string_tuple(value: Any, *, fallback: tuple[str, ...]) -> tuple[str, ...]:
        if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
            return tuple(value)
        return fallback
