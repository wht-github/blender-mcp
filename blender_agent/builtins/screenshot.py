"""
screenshot.py — 截取 Blender Viewport / Render 画面

返回 base64 编码的 PNG 图像，可直接作为图像消息传给支持视觉的 LLM。
"""

import bpy
import base64
import tempfile
import os

SUMMARY = "截取 3D Viewport 或渲染结果，返回 base64 PNG 供 LLM 视觉分析"

DESCRIPTION = """
screenshot builtin — Viewport 画面截取

函数：
  capture_viewport(area_type='VIEW_3D') -> str
    截取指定类型的编辑器区域，返回 base64 PNG 字符串。
    area_type 可选值：'VIEW_3D' | 'IMAGE_EDITOR' | 'NODE_EDITOR' | 'UV'
    若找不到对应区域，回退到截取整个窗口。

  capture_render(frame=None) -> str
    渲染当前帧（或指定帧），返回 base64 PNG 字符串。
    注意：这会触发真实渲染，可能耗时较长。

示例：
  screenshot = get_builtin('screenshot')
  img_b64 = screenshot.capture_viewport()
  # 将 img_b64 放入 return 值，框架自动识别为图像消息
  return {"screenshot": img_b64, "message": "当前视口截图"}
"""


def capture_viewport(area_type: str = "VIEW_3D") -> str:
    """截取 Blender 视口，返回 base64 PNG。"""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp_path = f.name

    try:
        # 找到目标区域
        assert bpy.context.window_manager is not None
        target_area = None
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == area_type:
                    target_area = area
                    break
            if target_area:
                break

        if target_area is None:
            # 回退：截取整个窗口
            bpy.ops.screen.screenshot(filepath=tmp_path)
        else:
            # 覆盖上下文截取指定区域
            with bpy.context.temp_override(area=target_area):
                bpy.ops.screen.screenshot_area(filepath=tmp_path)

        with open(tmp_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def capture_render(frame: int | None = None) -> str:
    """渲染当前帧（或指定帧），返回 base64 PNG。"""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        tmp_path = f.name

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

            with open(tmp_path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        finally:
            scene.render.filepath = original_path
            scene.render.image_settings.file_format = original_format
            if frame is not None:
                scene.frame_set(original_frame)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
