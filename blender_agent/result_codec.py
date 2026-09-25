"""Snapshot script results on Blender's main thread before crossing threads."""

import base64
import json
import math
from typing import Any


MAX_TEXT_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_DEPTH = 64
MAX_NODES = 100_000


class ResultValidationError(ValueError):
    pass


def snapshot_result(value: Any) -> Any:
    """Copy only exact built-in JSON types; never call user conversion methods.

    Tuples become arrays for compatibility with ordinary bpy property snapshots.
    RNA objects, subclasses, cycles and non-finite floats are rejected explicitly.
    """
    remaining = MAX_TEXT_BYTES
    nodes = 0
    ancestors = set()

    def copy(item, depth=0, image=False):
        nonlocal remaining, nodes
        nodes += 1
        if depth > MAX_DEPTH or nodes > MAX_NODES:
            raise ResultValidationError("Result nesting or item count exceeds the limit")
        kind = type(item)
        if kind is str:
            if image:
                if len(item) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
                    raise ResultValidationError("Screenshot exceeds the 10 MiB limit")
                try:
                    decoded = base64.b64decode(item, validate=True)
                except ValueError as exc:
                    raise ResultValidationError("Screenshot must be valid base64") from exc
                if len(decoded) > MAX_IMAGE_BYTES:
                    raise ResultValidationError("Screenshot exceeds the 10 MiB limit")
            else:
                if len(item) > remaining:
                    raise ResultValidationError("Text result exceeds the 1 MiB limit")
                remaining -= len(item.encode("utf-8"))
        elif item is None or kind is bool:
            remaining -= 5
        elif kind is int:
            if item.bit_length() > 4096:
                raise ResultValidationError("Result integer is too large")
            remaining -= len(str(item))
        elif kind is float:
            if not math.isfinite(item):
                raise ResultValidationError("Result floats must be finite")
            remaining -= 24
        elif kind in (dict, list, tuple):
            identity = id(item)
            if identity in ancestors:
                raise ResultValidationError("Result contains a circular reference")
            ancestors.add(identity)
            remaining -= 2 + len(item)
            try:
                if kind is dict:
                    result = {}
                    for key, child in item.items():
                        if type(key) is not str:
                            raise ResultValidationError("Result dictionary keys must be strings")
                        copy(key, depth + 1)
                        is_image = depth == 0 and key == "screenshot" and child is not None
                        if is_image and type(child) is not str:
                            raise ResultValidationError("Screenshot must be a base64 string")
                        result[key] = copy(child, depth + 1, image=is_image)
                else:
                    result = [copy(child, depth + 1) for child in item]
                return result
            finally:
                ancestors.remove(identity)
        else:
            raise ResultValidationError(
                "Return only JSON values; extract names or numeric data from Blender objects explicitly"
            )
        if remaining < 0:
            raise ResultValidationError("Text result exceeds the 1 MiB limit")
        return item

    result = copy(value)
    # Count actual escaping/separators too, without constructing an unbounded string.
    text = result
    if type(result) is dict and "screenshot" in result:
        text = {key: item for key, item in result.items() if key != "screenshot"}
    size = 0
    for chunk in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(text):
        size += len(chunk.encode("utf-8"))
        if size > MAX_TEXT_BYTES:
            raise ResultValidationError("Text result exceeds the 1 MiB limit")
    return result
