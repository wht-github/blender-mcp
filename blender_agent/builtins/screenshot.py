"""
screenshot.py — 截取 Blender Viewport / Render 画面

返回 base64 编码的 PNG 图像，可直接作为图像消息传给支持视觉的 LLM。
"""

import base64
import os
import tempfile

import bpy

SUMMARY = "纯截图：使用显式接口截取指定编辑器区域或渲染结果；对象聚焦截图请优先用 viewport builtin"
TAGS = ["screenshot", "viewport", "render", "image", "png", "截图", "渲染", "图像"]
SIDE_EFFECTS = "mixed"
RESULT_TYPES = ["image", "object"]
OPERATIONS = [
    {
        "name": "capture_viewport",
        "signature": "capture_viewport(area_type='VIEW_3D', width=None, height=None) -> str",
        "summary": "严格截取指定编辑器区域；区域不存在时返回错误，不静默截取其他窗口",
        "tags": ["viewport", "area", "png", "截图"],
        "side_effects": "read",
        "result_types": ["image"],
        "requires_ui_context": True,
        "cost": "medium",
    },
    {
        "name": "capture_render",
        "signature": "capture_render(frame=None, width=None, height=None) -> str",
        "summary": "在受限分辨率下渲染当前或指定帧，并恢复帧号与渲染设置",
        "tags": ["render", "frame", "png", "渲染"],
        "side_effects": "mixed",
        "result_types": ["image"],
        "cost": "high",
    },
    {
        "name": "as_result",
        "signature": "as_result(image_b64, message='当前截图', **extra_fields) -> dict",
        "summary": "将 PNG base64 包装为 MCP 图片结果，并保护保留字段",
        "tags": ["result", "image", "mcp"],
        "side_effects": "read",
        "result_types": ["object"],
        "cost": "low",
    },
]

DESCRIPTION = """
screenshot builtin — Viewport 画面截取

函数：
    capture_viewport(area_type='VIEW_3D', width=None, height=None) -> str
        截取指定类型的编辑器区域，返回 base64 PNG 字符串。
        area_type 仅接受 Blender 的精确区域类型名称：
            'VIEW_3D' | 'IMAGE_EDITOR' | 'NODE_EDITOR'
        若找不到对应区域会返回明确错误，不会静默截取其他窗口。
        单轴最大 4096 像素，总像素最大约 16.8 百万。

    capture_render(frame=None, width=None, height=None) -> str
        渲染当前帧（或指定帧），返回 base64 PNG 字符串。
        注意：这会触发真实渲染，可能耗时较长。
        渲染前即应用目标尺寸，避免先按超大场景分辨率渲染后再缩小。

    as_result(image_b64, message='当前截图', **extra_fields) -> dict
        将 base64 图片包装成 MCP 可识别的返回结构。

说明：
    - screenshot 只负责截图，不负责对象聚焦或对象可见性控制。
    - 若需要“聚焦零件后截图”，优先使用 viewport builtin。
    - 不提供别名；请严格使用文档中的函数名、参数名和 area_type 枚举值。

示例：
    screenshot = get_builtin('screenshot')

    img_b64 = screenshot.capture_viewport(area_type='VIEW_3D', width=1024, height=768)
    __result__ = screenshot.as_result(img_b64, message='当前 3D 视图截图')

    uv_b64 = screenshot.capture_viewport(area_type='IMAGE_EDITOR', width=1024, height=768)
    __result__ = screenshot.as_result(uv_b64, message='当前图像编辑器截图')
"""


_ALLOWED_AREA_TYPES = {
    "VIEW_3D",
    "IMAGE_EDITOR",
    "NODE_EDITOR",
}
_MAX_IMAGE_DIMENSION = 4096
_MAX_IMAGE_PIXELS = 16 * 1024 * 1024


def _encode_file_as_base64(path: str) -> str:
    with open(path, "rb") as handle:
        return base64.b64encode(handle.read()).decode("utf-8")


def _resize_image_file(
    path: str,
    width: int | None,
    height: int | None,
) -> None:
    image = bpy.data.images.load(path, check_existing=False)
    try:
        original_width, original_height = image.size
        if original_width <= 0 or original_height <= 0:
            raise RuntimeError(f"Captured image has invalid dimensions: {image.size[:]}")

        target_width, target_height = _resolve_dimensions(
            width,
            height,
            original_width,
            original_height,
        )
        if (target_width, target_height) != (original_width, original_height):
            image.scale(target_width, target_height)
            image.save(filepath=path)
    finally:
        bpy.data.images.remove(image, do_unlink=True)


def _resolve_dimensions(
    width: int | None,
    height: int | None,
    original_width: int,
    original_height: int,
) -> tuple[int, int]:
    if original_width <= 0 or original_height <= 0:
        raise ValueError("Original image dimensions must be positive")

    for label, value in (("width", width), ("height", height)):
        if value is not None and (isinstance(value, bool) or int(value) != value or value <= 0):
            raise ValueError(f"{label} must be a positive integer")

    if width is None and height is None:
        target_width, target_height = original_width, original_height
    elif width is None:
        assert height is not None
        target_height = int(height)
        target_width = max(1, round(original_width * (target_height / original_height)))
    elif height is None:
        target_width = int(width)
        target_height = max(1, round(original_height * (target_width / original_width)))
    else:
        target_width, target_height = int(width), int(height)

    if target_width > _MAX_IMAGE_DIMENSION or target_height > _MAX_IMAGE_DIMENSION:
        raise ValueError(
            f"Image dimensions {target_width}x{target_height} exceed the "
            f"{_MAX_IMAGE_DIMENSION}-pixel per-axis limit"
        )
    if target_width * target_height > _MAX_IMAGE_PIXELS:
        raise ValueError(
            f"Image dimensions {target_width}x{target_height} exceed the "
            f"{_MAX_IMAGE_PIXELS}-pixel limit"
        )
    return target_width, target_height


def _capture_area_to_file(area_type: str, path: str) -> None:
    normalized_area_type = area_type.strip().upper()
    if normalized_area_type not in _ALLOWED_AREA_TYPES:
        raise ValueError(
            f"Unsupported area_type '{area_type}'. Supported values: {sorted(_ALLOWED_AREA_TYPES)}"
        )

    assert bpy.context.window_manager is not None
    target_window = None
    target_area = None
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == normalized_area_type:
                target_window = window
                target_area = area
                break
        if target_area is not None:
            break

    if target_area is None or target_window is None:
        raise RuntimeError(
            f"No '{normalized_area_type}' editor area is available for capture"
        )

    with bpy.context.temp_override(window=target_window, screen=target_window.screen, area=target_area):
        screenshot_operator = bpy.ops.screen.screenshot_area
        if not getattr(screenshot_operator, "poll")():
            raise RuntimeError(
                f"Screenshot operator is unavailable for '{normalized_area_type}' "
                "in the current Blender context"
            )
        bpy.ops.screen.screenshot_area(filepath=path)


def capture_viewport(
    area_type: str = "VIEW_3D",
    width: int | None = None,
    height: int | None = None,
) -> str:
    """截取指定编辑器区域，返回 base64 PNG。"""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        tmp_path = handle.name

    try:
        _capture_area_to_file(area_type=area_type, path=tmp_path)
        _resize_image_file(tmp_path, width=width, height=height)
        return _encode_file_as_base64(tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def capture_render(
    frame: int | None = None,
    width: int | None = None,
    height: int | None = None,
) -> str:
    """渲染当前帧（或指定帧），返回 base64 PNG。"""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
        tmp_path = handle.name

    try:
        assert bpy.context.scene is not None
        scene = bpy.context.scene
        original_frame = scene.frame_current
        original_path = scene.render.filepath
        original_format = scene.render.image_settings.file_format
        original_width = scene.render.resolution_x
        original_height = scene.render.resolution_y
        original_percentage = scene.render.resolution_percentage

        try:
            if frame is not None:
                scene.frame_set(frame)

            effective_width = max(1, round(original_width * original_percentage / 100))
            effective_height = max(1, round(original_height * original_percentage / 100))
            target_width, target_height = _resolve_dimensions(
                width,
                height,
                effective_width,
                effective_height,
            )
            scene.render.filepath = tmp_path
            scene.render.image_settings.file_format = "PNG"
            scene.render.resolution_x = target_width
            scene.render.resolution_y = target_height
            scene.render.resolution_percentage = 100
            bpy.ops.render.render(write_still=True)

            return _encode_file_as_base64(tmp_path)
        finally:
            scene.render.filepath = original_path
            scene.render.image_settings.file_format = original_format
            scene.render.resolution_x = original_width
            scene.render.resolution_y = original_height
            scene.render.resolution_percentage = original_percentage
            if frame is not None:
                scene.frame_set(original_frame)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def as_result(image_b64: str, message: str = "当前截图", **extra_fields) -> dict:
    """将图片 base64 包装为 MCP 可识别的截图结果。"""
    reserved = {"screenshot", "message"}.intersection(extra_fields)
    if reserved:
        raise ValueError(f"extra_fields cannot override reserved fields: {sorted(reserved)}")
    result = {
        "screenshot": image_b64,
        "message": message,
    }
    result.update(extra_fields)
    return result
