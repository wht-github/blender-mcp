"""
agent_panel.py — Blender UI 面板（MCP Server 控制）

位置：3D Viewport > Sidebar (N) > AI Agent
提供：MCP Server 启动/停止、端口配置、状态显示
"""

import bpy
from bpy.props import BoolProperty, IntProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel

from typing import TYPE_CHECKING

from . import eval_core, mcp_server


# ── 偏好设置 ──────────────────────────────────────────────────────────────────

class BlenderAgentPreferences(AddonPreferences):
    bl_idname = __package__

    if TYPE_CHECKING:
        mcp_host: str
        mcp_port: int
        show_task_history: bool
        task_history_limit: int
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
        show_task_history: BoolProperty(
            name="显示任务历史",
            description="在 3D Viewport 面板中显示最近的 MCP 执行任务",
            default=True,
        )
        task_history_limit: IntProperty(
            name="历史条数",
            description="面板中显示的最近任务数量",
            default=8,
            min=1,
            max=20,
        )

    def draw(self, context):
        assert self.layout is not None
        layout = self.layout
        layout.prop(self, "mcp_host")
        layout.prop(self, "mcp_port")
        layout.prop(self, "show_task_history")
        layout.prop(self, "task_history_limit")


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


class AGENT_OT_ClearTaskHistory(Operator):
    bl_idname = "agent.clear_task_history"
    bl_label = "清空任务历史"
    bl_description = "清空 Blender Agent 当前会话的任务历史记录"

    def execute(self, context):
        eval_core.clear_task_history()
        self.report({"INFO"}, "任务历史已清空")
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
        prefs = context.preferences.addons[__package__].preferences

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
            box.label(text=f"HTTP URL: {mcp_server.get_url()}")
        else:
            box.label(text=f"HTTP URL: http://{prefs.mcp_host}:{prefs.mcp_port}/mcp")
        box.label(text="Tool: eval_python_code")

        layout.separator()
        history_header = layout.row(align=True)
        history_header.prop(
            prefs,
            "show_task_history",
            text="任务历史",
            icon="DOWNARROW_HLT" if prefs.show_task_history else "RIGHTARROW",
            emboss=False,
        )
        if prefs.show_task_history:
            history_header.operator("agent.clear_task_history", text="", icon="TRASH")
            history = eval_core.get_task_history(prefs.task_history_limit)
            if not history:
                layout.label(text="暂无任务", icon="INFO")
            for task in history:
                _draw_task_history_item(layout, task)


_STATUS_ICONS = {
    "queued": "SORTTIME",
    "running": "PLAY",
    "succeeded": "CHECKMARK",
    "failed": "ERROR",
    "timed_out": "TIME",
    "cancelled": "CANCEL",
}


def _draw_task_history_item(layout, task):
    box = layout.box()
    row = box.row(align=True)
    status = task["status"]
    row.label(
        text=f"{task['request_id'][:8]}  {status}",
        icon=_STATUS_ICONS.get(status, "QUESTION"),
    )

    queue_ms = task["queue_ms"]
    execution_ms = task["execution_ms"]
    timing = f"排队 {queue_ms:.0f} ms"
    if execution_ms is not None:
        timing += f" · 执行 {execution_ms:.0f} ms"
    box.label(text=timing)
    box.label(text=task["code_summary"])
    if task["execution_continues"]:
        box.label(text="客户端已超时，Python 仍在执行", icon="ERROR")


# ── 注册 ─────────────────────────────────────────────────────────────────────

_classes = [
    BlenderAgentPreferences,
    AGENT_OT_StartServer,
    AGENT_OT_StopServer,
    AGENT_OT_ClearTaskHistory,
    AGENT_PT_Main,
]


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
