"""
agent_panel.py — Blender UI 面板

位置：3D Viewport > Sidebar (N) > AI Agent
提供：聊天输入框、对话历史展示、API 设置
"""

import threading
from typing import TYPE_CHECKING, Any, Protocol, cast

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy.types import AddonPreferences, Operator, Panel

from .agent_loop import AgentConfig, get_agent


class AgentScene(Protocol):
    agent_input: str
    agent_output: str
    agent_running: bool
    agent_last_code: str



# ── 偏好设置（API Key 等）─────────────────────────────────────────────────────

class BlenderAgentPreferences(AddonPreferences):
    bl_idname = __package__

    if TYPE_CHECKING:
        api_key: str
        base_url: str
        model: str
        system_prompt_extra: str
    else:
        api_key: StringProperty(
            name="API Key",
            description="LLM 服务商 API Key",
            subtype="PASSWORD",
            default="",
        )
        base_url: StringProperty(
            name="Base URL",
            description="OpenAI 兼容 API 地址",
            default="https://api.openai.com/v1",
        )
        model: StringProperty(
            name="Model",
            description="模型名称，例如 gpt-4o、claude-opus-4-5",
            default="gpt-4o",
        )
        system_prompt_extra: StringProperty(
            name="额外系统提示",
            description="追加到系统提示的自定义指令",
            default="",
        )

    def draw(self, context):
        assert self.layout is not None
        layout = self.layout
        layout.prop(self, "api_key")
        layout.prop(self, "base_url")
        layout.prop(self, "model")
        layout.prop(self, "system_prompt_extra")


# ── 场景属性（对话状态）──────────────────────────────────────────────────────

def register_scene_props():
    scene_type = cast(Any, bpy.types.Scene)
    scene_type.agent_input = StringProperty(
        name="",
        description="输入你的指令",
        default="",
    )
    scene_type.agent_output = StringProperty(
        name="对话历史",
        default="",
    )
    scene_type.agent_running = BoolProperty(default=False)
    scene_type.agent_last_code = StringProperty(default="")


def unregister_scene_props():
    scene_type = cast(Any, bpy.types.Scene)
    del scene_type.agent_input
    del scene_type.agent_output
    del scene_type.agent_running
    del scene_type.agent_last_code


# ── Operators ────────────────────────────────────────────────────────────────

class AGENT_OT_Send(Operator):
    bl_idname = "agent.send"
    bl_label = "发送"
    bl_description = "发送指令给 AI Agent"

    def execute(self, context):
        assert context.scene is not None
        scene = cast(AgentScene, context.scene)
        user_msg = scene.agent_input.strip()
        if not user_msg:
            return {"CANCELLED"}

        scene.agent_input = ""
        scene.agent_running = True

        prefs = context.preferences.addons[__package__].preferences
        config = AgentConfig(
            api_key=prefs.api_key,
            base_url=prefs.base_url,
            model=prefs.model,
            system_prompt_extra=prefs.system_prompt_extra,
        )

        def _run():
            agent = get_agent(config)

            def on_tool_call(name, code):
                scene.agent_last_code = code
                _append_output(scene, f"\n[执行代码]\n```python\n{code}\n```\n")

            def on_tool_result(result):
                _append_output(scene, f"[结果] {result[:500]}{'...' if len(result)>500 else ''}\n")

            def on_text(text):
                _append_output(scene, f"\n**AI**: {text}\n")

            _append_output(scene, f"\n**你**: {user_msg}\n")
            try:
                agent.chat(
                    user_msg,
                    on_text=on_text,
                    on_tool_call=on_tool_call,
                    on_tool_result=on_tool_result,
                )
            except Exception as e:
                _append_output(scene, f"\n[错误] {e}\n")
            finally:
                scene.agent_running = False

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        return {"FINISHED"}


class AGENT_OT_Reset(Operator):
    bl_idname = "agent.reset"
    bl_label = "清空对话"
    bl_description = "清空对话历史，开始新会话"

    def execute(self, context):
        prefs = context.preferences.addons[__package__].preferences
        config = AgentConfig(
            api_key=prefs.api_key,
            base_url=prefs.base_url,
            model=prefs.model,
        )
        agent = get_agent(config)
        agent.reset()
        assert context.scene is not None
        scene = cast(AgentScene, context.scene)
        scene.agent_output = ""
        scene.agent_last_code = ""
        return {"FINISHED"}


def _append_output(scene, text: str):
    scene.agent_output = (scene.agent_output + text)[-8000:]  # 保留最近 8000 字符


# ── UI Panel ─────────────────────────────────────────────────────────────────

class AGENT_PT_Main(Panel):
    bl_label = "AI Agent"
    bl_idname = "AGENT_PT_main"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "AI Agent"

    def draw(self, context):
        assert self.layout is not None
        layout = self.layout
        assert context.scene is not None
        scene = cast(AgentScene, context.scene)

        # 对话历史
        box = layout.box()
        box.label(text="对话历史：")
        if scene.agent_output:
            for line in scene.agent_output.split("\n")[-30:]:  # 只显示最近 30 行
                box.label(text=line[:80])
        else:
            box.label(text="（暂无对话）", icon="INFO")

        layout.separator()

        # 输入区
        row = layout.row(align=True)
        row.prop(scene, "agent_input")
        row.operator("agent.send", text="", icon="PLAY" if not scene.agent_running else "PAUSE")

        if scene.agent_running:
            layout.label(text="Agent 运行中...", icon="SORTTIME")

        # 清空按钮
        layout.operator("agent.reset", icon="TRASH")

        # 最近执行的代码（折叠）
        if scene.agent_last_code:
            col = layout.column()
            col.label(text="最近执行的代码：", icon="SCRIPT")
            box2 = col.box()
            for line in scene.agent_last_code.split("\n")[:15]:
                box2.label(text=line[:80])


# ── 注册 ─────────────────────────────────────────────────────────────────────

_classes = [
    BlenderAgentPreferences,
    AGENT_OT_Send,
    AGENT_OT_Reset,
    AGENT_PT_Main,
]


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    register_scene_props()


def unregister():
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
    unregister_scene_props()
