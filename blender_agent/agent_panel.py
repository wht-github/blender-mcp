"""
agent_panel.py — Blender UI 面板（MCP Server 控制）

位置：3D Viewport > Sidebar (N) > AI Agent
提供：MCP Server 启动/停止、端口配置、状态显示
"""

import bpy
from bpy.props import IntProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel

from typing import TYPE_CHECKING

from . import mcp_server


# ── 偏好设置 ──────────────────────────────────────────────────────────────────

class BlenderAgentPreferences(AddonPreferences):
    bl_idname = __package__

    if TYPE_CHECKING:
        mcp_host: str
        mcp_port: int
    else:
        mcp_host: StringProperty(
            name="Host",
            description="MCP Server 监听地址",
            default="127.0.0.1",
        )
        mcp_port: IntProperty(
            name="Port",
            description="MCP Server 监听端口",
            default=8400,
            min=1024,
            max=65535,
        )

    def draw(self, context):
        assert self.layout is not None
        layout = self.layout
        layout.prop(self, "mcp_host")
        layout.prop(self, "mcp_port")


# ── Operators ────────────────────────────────────────────────────────────────

class AGENT_OT_StartServer(Operator):
    bl_idname = "agent.start_server"
    bl_label = "启动 MCP Server"
    bl_description = "启动 MCP Server，供外部 AI 客户端连接"

    def execute(self, context):
        prefs = context.preferences.addons[__package__].preferences
        try:
            mcp_server.start(host=prefs.mcp_host, port=prefs.mcp_port)
            self.report({"INFO"}, f"MCP Server started: {mcp_server.get_url()}")
        except Exception as e:
            self.report({"ERROR"}, f"启动失败: {e}")
        return {"FINISHED"}


class AGENT_OT_StopServer(Operator):
    bl_idname = "agent.stop_server"
    bl_label = "停止 MCP Server"
    bl_description = "停止 MCP Server"

    def execute(self, context):
        mcp_server.stop()
        self.report({"INFO"}, "MCP Server stopped")
        return {"FINISHED"}


# ── UI Panel ─────────────────────────────────────────────────────────────────

class AGENT_PT_Main(Panel):
    bl_label = "AI Agent (MCP)"
    bl_idname = "AGENT_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "AI Agent"

    def draw(self, context):
        assert self.layout is not None
        layout = self.layout

        running = mcp_server.is_running()

        if running:
            url = mcp_server.get_url()
            layout.label(text="Status: Running", icon="CHECKMARK")
            layout.label(text=url)

            layout.separator()
            layout.operator("agent.stop_server", icon="PAUSE")
        else:
            layout.label(text="Status: Stopped", icon="X")

            layout.separator()
            layout.operator("agent.start_server", icon="PLAY")

        layout.separator()
        box = layout.box()
        box.label(text="MCP 客户端配置:", icon="INFO")
        if running:
            box.label(text=f"SSE URL: {mcp_server.get_url()}")
        else:
            prefs = context.preferences.addons[__package__].preferences
            box.label(text=f"SSE URL: http://{prefs.mcp_host}:{prefs.mcp_port}/sse")
        box.label(text="Tool: eval_python_code")


# ── 注册 ─────────────────────────────────────────────────────────────────────

_classes = [
    BlenderAgentPreferences,
    AGENT_OT_StartServer,
    AGENT_OT_StopServer,
    AGENT_PT_Main,
]


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
