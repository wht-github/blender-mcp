"""
screenshot.py — 截取 Blender Viewport / Render 画面

返回 base64 编码的 PNG 图像，可直接作为图像消息传给支持视觉的 LLM。
"""

import base64
import os
import tempfile

import bpy

SUMMARY = "截取 3D Viewport/指定编辑器或渲染结果，支持直觉别名与可选缩放，返回 base64 PNG 供 LLM 视觉分析"

DESCRIPTION = """
screenshot builtin — Viewport 画面截取

函数：
  capture(area='VIEW_3D', width=None, height=None) -> str
    统一截图入口。默认截取 3D 视口。
    area 常用值或别名：
      'VIEW_3D' | '3D' | '3D_VIEW' | 'VIEWPORT'
      'IMAGE_EDITOR' | 'UV' | 'NODE_EDITOR'

  capture_viewport(area_type='VIEW_3D', width=None, height=None) -> str
    截取指定类型的编辑器区域，返回 base64 PNG 字符串。
    若找不到对应区域，回退到截取整个窗口。

  capture_view(width=None, height=None) -> str
  capture_3d_view(width=None, height=None) -> str
    3D 视图截图的直觉别名，兼容常见写法。

  capture_render(frame=None, width=None, height=None) -> str
    渲染当前帧（或指定帧），返回 base64 PNG 字符串。
    注意：这会触发真实渲染，可能耗时较长。

  as_result(image_b64, message='当前截图', **extra_fields) -> dict
    将 base64 图片包装成 MCP 可识别的返回结构。

示例：
  screenshot = get_builtin('screenshot')

  img_b64 = screenshot.capture_3d_view(width=1024, height=768)
  __result__ = screenshot.as_result(img_b64, message='当前 3D 视图截图')

  # 或者使用统一入口
  __result__ = screenshot.as_result(screenshot.capture(area='UV'))
"""


_AREA_ALIASES = {
    "3D": "VIEW_3D",
    "3DVIEW": "VIEW_3D",
    "3D_VIEW": "VIEW_3D",
    "VIEWPORT": "VIEW_3D",
    "VIEW_3D": "VIEW_3D",
    "IMAGE": "IMAGE_EDITOR",
    "IMAGE_EDITOR": "IMAGE_EDITOR",
    "NODE": "NODE_EDITOR",
    "NODE_EDITOR": "NODE_EDITOR",
    "UV": "UV",
    "UV_EDITOR": "UV",
}


def _normalize_area_type(area_type: str) -> str:
    return _AREA_ALIASES.get(area_type.strip().upper(), area_type.strip().upper())


def _encode_file_as_base64(path: str) -> str:
    with open(path, "rb") as handle:
        return base64.b64encode(handle.read()).decode("utf-8")


def _resize_image_file(path: str, width: int | None, height: int | None) -> None:
    if width is None and height is None:
        return

    image = bpy.data.images.load(path, check_existing=False)
    try:
        original_width, original_height = image.size
        if original_width <= 0 or original_height <= 0:
            return

        if width is None:
            assert height is not None
            width = max(1, round(original_width * (height / original_height)))
        if height is None:
            assert width is not None
            height = max(1, round(original_height * (width / original_width)))

        image.scale(max(1, int(width)), max(1, int(height)))
        image.save(filepath=path)
    finally:
        bpy.data.images.remove(image, do_unlink=True)


def _capture_area_to_file(area_type: str, path: str) -> None:
    normalized_area_type = _normalize_area_type(area_type)

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
        bpy.ops.screen.screenshot(filepath=path)
        return

    with bpy.context.temp_override(window=target_window, screen=target_window.screen, area=target_area):
        bpy.ops.screen.screenshot_area(filepath=path)


def capture(area: str = "VIEW_3D", width: int | None = None, height: int | None = None) -> str:
    """统一截图入口，默认截取 3D 视口。"""
    return capture_viewport(area_type=area, width=width, height=height)


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


def capture_view(width: int | None = None, height: int | None = None) -> str:
    """3D 视图截图的短别名。"""
    return capture_viewport(area_type="VIEW_3D", width=width, height=height)


def capture_3d_view(width: int | None = None, height: int | None = None) -> str:
    """兼容常见调用写法的 3D 视图截图别名。"""
    return capture_viewport(area_type="VIEW_3D", width=width, height=height)


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

        try:
            if frame is not None:
                scene.frame_set(frame)

            scene.render.filepath = tmp_path
            scene.render.image_settings.file_format = "PNG"
            bpy.ops.render.render(write_still=True)

            _resize_image_file(tmp_path, width=width, height=height)
            return _encode_file_as_base64(tmp_path)
        finally:
            scene.render.filepath = original_path
            scene.render.image_settings.file_format = original_format
            if frame is not None:
                scene.frame_set(original_frame)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def as_result(image_b64: str, message: str = "当前截图", **extra_fields) -> dict:
    """将图片 base64 包装为 MCP 可识别的截图结果。"""
    result = {
        "screenshot": image_b64,
        "message": message,
    }
    result.update(extra_fields)
    return result
