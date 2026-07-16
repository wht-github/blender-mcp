"""
viewport.py — 在同一 3D 视口中聚焦对象并截图

封装“构图 + 截图 + 选中状态恢复”的高频检查流程，
避免焦点和截图落在不同视口。
"""

import os
import tempfile
from collections.abc import Iterable
from typing import Any

import bpy

from . import screenshot as _screenshot

SUMMARY = "检查截图：在同一 3D 视口中聚焦指定对象或当前选择并截图，减少手写 view_selected/screenshot 流程"
TAGS = ["viewport", "focus", "selection", "screenshot", "image", "视口", "聚焦", "截图"]
SIDE_EFFECTS = "mixed"
RESULT_TYPES = ["image", "object", "array"]
OPERATIONS = [
    {
        "name": "get_selected_objects",
        "signature": "get_selected_objects() -> list[str]",
        "summary": "返回当前 View Layer 中选中的对象名称",
        "tags": ["selection", "query", "选择"],
        "side_effects": "read",
        "result_types": ["array"],
        "cost": "low",
    },
    {
        "name": "focus_objects",
        "signature": "focus_objects(names) -> list[str]",
        "summary": "在当前 View Layer 的一个 3D 视口中选中并框选对象",
        "tags": ["focus", "frame", "view_selected", "聚焦"],
        "side_effects": "write",
        "result_types": ["array"],
        "requires_ui_context": True,
        "cost": "low",
    },
    {
        "name": "capture_objects",
        "signature": "capture_objects(names, width=None, height=None) -> str",
        "summary": "聚焦对象并截图，随后恢复选择、可见性、活动对象和视口构图",
        "tags": ["capture", "focus", "restore", "截图"],
        "side_effects": "mixed",
        "result_types": ["image"],
        "requires_ui_context": True,
        "cost": "medium",
    },
    {
        "name": "capture_selection",
        "signature": "capture_selection(width=None, height=None) -> str",
        "summary": "截图当前选中对象并完整恢复视口状态",
        "tags": ["selection", "capture", "截图"],
        "side_effects": "mixed",
        "result_types": ["image"],
        "requires_ui_context": True,
        "cost": "medium",
    },
    {
        "name": "as_result",
        "signature": "as_result(image_b64, message='当前视口截图', **extra_fields) -> dict",
        "summary": "将 PNG base64 包装为 MCP 图片结果",
        "tags": ["result", "image", "mcp"],
        "side_effects": "read",
        "result_types": ["object"],
        "cost": "low",
    },
]

DESCRIPTION = """
viewport builtin — 视口聚焦与检查截图

函数：
    get_selected_objects() -> list[str]
        返回当前选中对象名称列表。

    focus_objects(names: list[str]) -> list[str]
        在同一个 VIEW_3D 视口中选中并框选指定对象。
        返回实际参与聚焦的对象名称列表。

    capture_objects(names: list[str], width=None, height=None) -> str
        在同一个 VIEW_3D 视口中聚焦指定对象并截图，返回 base64 PNG。
        截图后会恢复选择、活动对象、目标可见性和原始视口构图。

    capture_selection(width=None, height=None) -> str
        基于当前选中对象执行 capture_objects()。

    as_result(image_b64, message='当前视口截图', **extra_fields) -> dict
        将 base64 图片包装成 MCP 可识别的返回结构。

示例：
    viewport = get_builtin('viewport')

    img = viewport.capture_selection(width=1280)
    __result__ = viewport.as_result(img, message='当前选中零件截图')

    img = viewport.capture_objects(['door-left', 'door-right'], width=1024)
    __result__ = viewport.as_result(img, message='车门局部检查图')
"""


def _get_scene_objects() -> list[Any]:
    view_layer = bpy.context.view_layer
    if view_layer is None:
        raise RuntimeError("No active View Layer")
    return list(view_layer.objects)


def _get_objects_by_name() -> dict[str, Any]:
    return {obj.name: obj for obj in _get_scene_objects()}


def _resolve_objects(names: Iterable[str]) -> list[Any]:
    resolved: list[Any] = []
    missing: list[str] = []
    seen = set()

    if isinstance(names, str):
        names = [names]
    objects_by_name = _get_objects_by_name()
    for name in names:
        if name in seen:
            continue
        seen.add(name)

        obj = objects_by_name.get(name)
        if obj is None:
            missing.append(name)
            continue
        resolved.append(obj)

    if missing:
        raise ValueError(f"Objects not found: {missing}")
    if not resolved:
        raise ValueError("No target objects provided")
    return resolved


def _find_view3d_context() -> dict[str, Any]:
    assert bpy.context.window_manager is not None

    for window in bpy.context.window_manager.windows:
        screen = window.screen
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue

            region = next((item for item in area.regions if item.type == "WINDOW"), None)
            if region is None:
                continue

            return {
                "window": window,
                "screen": screen,
                "area": area,
                "region": region,
                "space_data": area.spaces.active,
            }

    raise RuntimeError("No VIEW_3D area available for viewport capture")


def _make_override(view_context: dict[str, Any]) -> dict[str, Any]:
    override = {
        "window": view_context["window"],
        "screen": view_context["screen"],
        "area": view_context["area"],
        "region": view_context["region"],
    }

    space_data = view_context.get("space_data")
    if space_data is not None:
        override["space_data"] = space_data
    return override


def _update_view_layer() -> None:
    view_layer = bpy.context.view_layer
    if view_layer is not None and hasattr(view_layer, "update"):
        view_layer.update()


def _set_selected_objects(objects: list[Any]) -> None:
    selected_names = {obj.name for obj in objects}
    for obj in _get_scene_objects():
        obj.select_set(obj.name in selected_names)

    view_layer = bpy.context.view_layer
    if view_layer is not None:
        view_layer.objects.active = objects[-1] if objects else None


def _snapshot_view(view_context: dict[str, Any]) -> dict[str, Any]:
    space_data = view_context.get("space_data")
    region_3d = getattr(space_data, "region_3d", None)
    if region_3d is None:
        return {}
    return {
        "view_distance": float(region_3d.view_distance),
        "view_location": region_3d.view_location.copy(),
        "view_rotation": region_3d.view_rotation.copy(),
        "view_perspective": region_3d.view_perspective,
        "view_camera_zoom": float(region_3d.view_camera_zoom),
        "view_camera_offset": tuple(region_3d.view_camera_offset),
    }


def _restore_view(view_context: dict[str, Any], state: dict[str, Any]) -> None:
    if not state:
        return
    space_data = view_context.get("space_data")
    region_3d = getattr(space_data, "region_3d", None)
    if region_3d is None:
        return
    region_3d.view_distance = state["view_distance"]
    region_3d.view_location = state["view_location"]
    region_3d.view_rotation = state["view_rotation"]
    region_3d.view_perspective = state["view_perspective"]
    region_3d.view_camera_zoom = state["view_camera_zoom"]
    region_3d.view_camera_offset = state["view_camera_offset"]


def _snapshot_state(
    target_objects: list[Any],
    view_context: dict[str, Any],
) -> dict[str, Any]:
    view_layer = bpy.context.view_layer
    active_name = None
    if view_layer is not None and view_layer.objects.active is not None:
        active_name = view_layer.objects.active.name

    return {
        "selected": get_selected_objects(),
        "active": active_name,
        "target_visibility": {
            obj.name: {
                "hidden": obj.hide_get(),
                "hide_viewport": bool(obj.hide_viewport),
            }
            for obj in target_objects
        },
        "view": _snapshot_view(view_context),
    }


def _restore_state(
    state: dict[str, Any],
    view_context: dict[str, Any],
) -> None:
    objects_by_name = _get_objects_by_name()

    for name, visibility in state["target_visibility"].items():
        obj = objects_by_name.get(name)
        if obj is not None:
            obj.hide_set(bool(visibility["hidden"]))
            obj.hide_viewport = visibility["hide_viewport"]

    selected_names = set(state["selected"])
    for obj in objects_by_name.values():
        obj.select_set(obj.name in selected_names and not obj.hide_get())

    active_name = state["active"]
    view_layer = bpy.context.view_layer
    if view_layer is not None:
        view_layer.objects.active = objects_by_name.get(active_name) if active_name else None
    _restore_view(view_context, state["view"])


def _ensure_visible(objects: list[Any]) -> None:
    for obj in objects:
        obj.hide_viewport = False
        obj.hide_set(False)


def _frame_objects(view_context: dict[str, Any], objects: list[Any]) -> None:
    _set_selected_objects(objects)
    _update_view_layer()

    with bpy.context.temp_override(**_make_override(view_context)):
        bpy.ops.view3d.view_selected()

    _update_view_layer()


def _capture_view3d(view_context: dict[str, Any], width: int | None, height: int | None) -> str:
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        tmp_path = handle.name

    try:
        with bpy.context.temp_override(**_make_override(view_context)):
            screenshot_operator = bpy.ops.screen.screenshot_area
            if not getattr(screenshot_operator, "poll")():
                raise RuntimeError(
                    "Viewport screenshot operator is unavailable in the current "
                    "Blender context"
                )
            bpy.ops.screen.screenshot_area(filepath=tmp_path)

        _screenshot._resize_image_file(tmp_path, width=width, height=height)
        return _screenshot._encode_file_as_base64(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def get_selected_objects() -> list[str]:
    """返回当前选中对象名称列表。"""
    return [obj.name for obj in (bpy.context.selected_objects or [])]


def focus_objects(names: list[str]) -> list[str]:
    """在同一 3D 视口中聚焦指定对象。"""
    objects = _resolve_objects(names)
    view_context = _find_view3d_context()

    _ensure_visible(objects)
    _frame_objects(view_context, objects)
    return [obj.name for obj in objects]


def capture_objects(
    names: list[str],
    width: int | None = None,
    height: int | None = None,
) -> str:
    """在同一 3D 视口中聚焦指定对象并截图。"""
    objects = _resolve_objects(names)
    view_context = _find_view3d_context()
    state = _snapshot_state(objects, view_context)

    try:
        _ensure_visible(objects)
        _frame_objects(view_context, objects)
        return _capture_view3d(view_context, width=width, height=height)
    finally:
        _restore_state(state, view_context)
        _update_view_layer()


def capture_selection(
    width: int | None = None,
    height: int | None = None,
) -> str:
    """基于当前选中对象执行 capture_objects。"""
    selected_names = get_selected_objects()
    if not selected_names:
        raise ValueError("No objects are currently selected")

    return capture_objects(
        names=selected_names,
        width=width,
        height=height,
    )


def as_result(image_b64: str, message: str = "当前视口截图", **extra_fields) -> dict:
    """将图片 base64 包装为 MCP 可识别的截图结果。"""
    return _screenshot.as_result(image_b64, message=message, **extra_fields)
