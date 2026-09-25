"""
builtin_loader.py — Builtins 发现与懒加载管理器

渐进式文档机制：
  SUMMARY      = "一行摘要"          # capability search 返回
  TAGS         = ["scene", ...]      # capability search 索引
  SIDE_EFFECTS = "read|write|mixed"  # 能力副作用提示
  RESULT_TYPES = ["text", ...]       # 典型结果类型
  DESCRIPTION  = "完整 API 文档..."  # describe 显式读取

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
class BuiltinOperationMetadata:
    name: str
    capability_id: str
    load_id: str
    signature: str
    summary: str
    tags: tuple[str, ...]
    side_effects: str
    result_types: tuple[str, ...]
    requires_ui_context: bool
    cost: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.capability_id,
            "load_id": self.load_id,
            "name": self.name,
            "kind": "builtin_operation",
            "signature": self.signature,
            "summary": self.summary,
            "tags": list(self.tags),
            "side_effects": self.side_effects,
            "result_types": list(self.result_types),
            "requires_ui_context": self.requires_ui_context,
            "cost": self.cost,
        }


@dataclass(frozen=True)
class BuiltinMetadata:
    name: str
    capability_id: str
    summary: str
    description: str | None
    tags: tuple[str, ...]
    side_effects: str
    result_types: tuple[str, ...]
    operations: tuple[BuiltinOperationMetadata, ...]

    def as_dict(self, *, include_description: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.capability_id,
            "name": self.name,
            "kind": "builtin",
            "summary": self.summary,
            "tags": list(self.tags),
            "side_effects": self.side_effects,
            "result_types": list(self.result_types),
            "operation_count": len(self.operations),
        }
        if include_description:
            result["description"] = self.description
        return result


class BuiltinLoader:
    def __init__(self):
        self._registry: dict[str, Path] = {}        # name -> .py path
        self._loaded: dict[str, ModuleType] = {}    # name -> module
        self._metadata: dict[str, BuiltinMetadata] = {}

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
            raw_operations = self._read_literal(path, "OPERATIONS")
            operations = self._parse_operations(name, raw_operations)
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
                operations=operations,
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
        return module

    def normalize_name(self, name: str) -> str:
        """Accept module IDs and validate optional ``builtin.module.operation`` IDs."""
        value = name.removeprefix("builtin.")
        normalized, separator, operation_name = value.partition(".")
        if normalized not in self._registry:
            available = [metadata.capability_id for metadata in self._metadata.values()]
            raise ModuleNotFoundError(
                f"Builtin '{name}' not found. Available capability IDs: {available}"
            )
        if separator and not any(
            operation.name == operation_name
            for operation in self._metadata[normalized].operations
        ):
            available = [
                operation.capability_id
                for operation in self._metadata[normalized].operations
            ]
            raise ModuleNotFoundError(
                f"Builtin operation '{name}' not found. Available operations: {available}"
            )
        return normalized

    def get_metadata(self, name: str) -> BuiltinMetadata:
        return self._metadata[self.normalize_name(name)]

    def get_operation_metadata(self, capability_id: str) -> BuiltinOperationMetadata | None:
        value = capability_id.removeprefix("builtin.")
        module_name, separator, operation_name = value.partition(".")
        if not separator:
            return None
        metadata = self.get_metadata(module_name)
        for operation in metadata.operations:
            if operation.name == operation_name:
                return operation
        raise ModuleNotFoundError(
            f"Builtin operation '{capability_id}' not found. "
            f"Available operations: {[item.capability_id for item in metadata.operations]}"
        )

    def list_metadata(self) -> list[BuiltinMetadata]:
        return list(self._metadata.values())

    def loaded_names(self) -> list[str]:
        return list(self._loaded)

    def peek_description(self, name: str) -> Optional[str]:
        """
        读取指定 builtin 的 DESCRIPTION，不加载模块。
        供 eval 命名空间中的 get_builtin_doc() 使用，让模型在写代码前先查文档。
        """
        try:
            name = self.normalize_name(name)
        except ModuleNotFoundError:
            return None
        return self._metadata[name].description

    def reload(self):
        """重新扫描目录（开发时热重载用）。"""
        self._loaded.clear()
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

    @staticmethod
    def _parse_operations(
        module_name: str,
        value: Any,
    ) -> tuple[BuiltinOperationMetadata, ...]:
        if not isinstance(value, (list, tuple)):
            return ()

        operations = []
        for item in value:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            signature = item.get("signature")
            summary = item.get("summary")
            if not all(isinstance(field, str) and field for field in (name, signature, summary)):
                continue
            side_effects = item.get("side_effects", "mixed")
            if side_effects not in {"read", "write", "mixed"}:
                side_effects = "mixed"
            cost = item.get("cost", "low")
            if cost not in {"low", "medium", "high"}:
                cost = "low"
            operations.append(
                BuiltinOperationMetadata(
                    name=name,
                    capability_id=f"builtin.{module_name}.{name}",
                    load_id=f"builtin.{module_name}",
                    signature=signature,
                    summary=summary,
                    tags=BuiltinLoader._string_tuple(item.get("tags"), fallback=()),
                    side_effects=side_effects,
                    result_types=BuiltinLoader._string_tuple(
                        item.get("result_types"),
                        fallback=("object",),
                    ),
                    requires_ui_context=bool(item.get("requires_ui_context", False)),
                    cost=cost,
                )
            )
        return tuple(operations)
