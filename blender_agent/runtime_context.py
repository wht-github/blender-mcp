"""Progressive capability discovery and logical builtin activation."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from types import ModuleType
from typing import Any

from .builtin_loader import BuiltinLoader, BuiltinMetadata


_SEARCH_TOKEN = re.compile(r"[a-zA-Z0-9_]+|[\u3400-\u9fff]+")
# Small vocabulary for common requests; Chinese bigrams cover literal summaries.
_SEARCH_ALIASES = {
    "查找": ("find", "filter"),
    "筛选": ("find", "filter"),
    "聚焦": ("focus",),
    "截图": ("capture", "screenshot"),
    "选中": ("selection",),
    "材质": ("material",),
    "修改器": ("modifier",),
    "保存": ("save",),
}


def _search_tokens(query: str) -> list[str]:
    tokens = _SEARCH_TOKEN.findall(query)
    for token in tuple(tokens):
        if "\u3400" <= token[0] <= "\u9fff":
            tokens.extend(token[index:index + 2] for index in range(len(token) - 1))
    for phrase, aliases in _SEARCH_ALIASES.items():
        if phrase in query:
            tokens.extend(aliases)
    return list(dict.fromkeys(tokens))


class CapabilityRegistry:
    """Unified read-only catalog for builtins and future runtime functions."""

    def __init__(self, loader: BuiltinLoader):
        self._loader = loader

    def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        query = query.strip().lower()
        if not query:
            return []

        tokens = _search_tokens(query)
        ranked: list[tuple[float, str, dict[str, Any]]] = []
        for metadata in self._loader.list_metadata():
            score = self._score(
                metadata.name,
                metadata.capability_id,
                metadata.tags,
                metadata.summary,
                query,
                tokens,
            )
            if score > 0:
                ranked.append((score, metadata.capability_id, metadata.as_dict()))
            for operation in metadata.operations:
                operation_score = self._score(
                    operation.name,
                    operation.capability_id,
                    operation.tags,
                    operation.summary,
                    query,
                    tokens,
                )
                if operation_score > 0:
                    ranked.append(
                        (
                            operation_score + 2 + 0.25 * self._score(
                                "", "", metadata.tags, "", query, tokens
                            ),
                            operation.capability_id,
                            operation.as_dict(),
                        )
                    )

        ranked.sort(key=lambda item: (-item[0], item[1]))
        safe_limit = max(1, min(int(limit), 20))
        return [
            {
                "id": item["id"],
                "load_id": item.get("load_id", item["id"]),
                "summary": item["summary"],
                "score": round(score, 3),
            }
            for score, _capability_id, item in ranked[:safe_limit]
        ]

    def describe(self, *capability_ids: str) -> list[dict[str, Any]]:
        if not capability_ids:
            raise ValueError("At least one capability ID is required")
        descriptions = []
        for capability_id in capability_ids:
            operation = self._loader.get_operation_metadata(capability_id)
            if operation is not None:
                descriptions.append(operation.as_dict())
            else:
                descriptions.append(
                    self._loader.get_metadata(capability_id).as_dict(
                        include_description=True
                    )
                )
        return descriptions

    def load_module(self, capability_id: str) -> ModuleType:
        return self._loader.load(capability_id)

    def normalize_name(self, capability_id: str) -> str:
        return self._loader.normalize_name(capability_id)

    def metadata(self, capability_id: str) -> BuiltinMetadata:
        return self._loader.get_metadata(capability_id)

    def count(self) -> int:
        return len(self._loader.list_metadata())

    @staticmethod
    def _score(
        name: str,
        capability_id: str,
        tags: tuple[str, ...],
        summary: str,
        query: str,
        tokens: list[str],
    ) -> float:
        name = name.lower()
        capability_id = capability_id.lower()
        lowered_tags = [tag.lower() for tag in tags]
        summary = summary.lower()
        searchable = " ".join((capability_id, summary, *lowered_tags))
        score = 0.0

        if query == name or query == capability_id:
            score += 100
        elif query in searchable:
            score += 24

        for token in tokens:
            if token == name or token == capability_id:
                score += 18
            elif token in name or token in capability_id:
                score += 10
            if token in lowered_tags:
                score += 8
            elif any(token in tag for tag in lowered_tags):
                score += 5
            if token in summary:
                score += 3

        return score


@dataclass
class RuntimeContext:
    """Activation state for one eval; module imports are cached by the loader."""

    registry: CapabilityRegistry
    revision: int = 0
    _loaded: dict[str, ModuleType] = field(default_factory=dict)

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        return {"matches": self.registry.search(query, limit)}

    def describe(self, *capability_ids: str) -> dict[str, Any]:
        descriptions = self.registry.describe(*capability_ids)
        for item in descriptions:
            module_name = self.registry.normalize_name(item.get("load_id", item["id"]))
            item["loaded"] = module_name in self._loaded
        return {"capabilities": descriptions}

    def load(self, *capability_ids: str) -> dict[str, Any]:
        if not capability_ids:
            raise ValueError("At least one capability ID is required")

        names = [self.registry.normalize_name(capability_id) for capability_id in capability_ids]
        loaded = []
        already_loaded = []
        for name in names:
            if name in self._loaded:
                already_loaded.append(f"builtin.{name}")
                continue
            self._loaded[name] = self.registry.load_module(name)
            loaded.append(f"builtin.{name}")

        if loaded:
            self.revision += 1
        return {
            "loaded": loaded,
            "already_loaded": already_loaded,
            "revision": self.revision,
        }

    def unload(self, *capability_ids: str) -> dict[str, Any]:
        if not capability_ids:
            raise ValueError("At least one capability ID is required")

        names = [self.registry.normalize_name(capability_id) for capability_id in capability_ids]
        unloaded = []
        not_loaded = []
        for name in names:
            if self._loaded.pop(name, None) is None:
                not_loaded.append(f"builtin.{name}")
            else:
                unloaded.append(f"builtin.{name}")

        if unloaded:
            self.revision += 1
        return {
            "unloaded": unloaded,
            "not_loaded": not_loaded,
            "revision": self.revision,
        }

    def list(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "loaded": [
                self.registry.metadata(name).as_dict()
                for name in sorted(self._loaded)
            ],
            "available_count": self.registry.count(),
        }

    def get_module(self, name: str) -> ModuleType:
        normalized = self.registry.normalize_name(name)
        try:
            return self._loaded[normalized]
        except KeyError as exc:
            raise AttributeError(
                f"Capability 'builtin.{normalized}' is not loaded. "
                f"Call runtime.load('builtin.{normalized}') first."
            ) from exc

    def load_compat(self, name: str) -> ModuleType:
        """Legacy get_builtin(): load and activate the capability."""
        normalized = self.registry.normalize_name(name)
        if normalized not in self._loaded:
            self.load(normalized)
        return self._loaded[normalized]

    def reset(self) -> None:
        if self._loaded:
            self._loaded.clear()
            self.revision += 1


class RuntimeFacade:
    """Small JSON-friendly API injected into eval scripts as ``runtime``."""

    def __init__(self, context: RuntimeContext):
        self._context = context

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        return self._context.search(query, limit)

    def describe(self, *capability_ids: str) -> dict[str, Any]:
        return self._context.describe(*capability_ids)

    def load(self, *capability_ids: str) -> dict[str, Any]:
        return self._context.load(*capability_ids)

    def unload(self, *capability_ids: str) -> dict[str, Any]:
        return self._context.unload(*capability_ids)

    def list(self) -> dict[str, Any]:
        return self._context.list()


class LoadedBuiltinProxy:
    """Attribute access for capabilities explicitly activated in the runtime."""

    def __init__(self, context: RuntimeContext):
        self._context = context

    def __getattr__(self, name: str) -> ModuleType:
        if name.startswith("_"):
            raise AttributeError(name)
        return self._context.get_module(name)

    def __dir__(self) -> list[str]:
        loaded = self._context.list()["loaded"]
        return sorted(item["name"] for item in loaded)
