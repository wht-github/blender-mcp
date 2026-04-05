"""
agent_loop.py — LLM Agent 对话循环

工作流：
  1. 将用户消息加入历史
  2. 构建包含 eval_python_code tool 的请求，发送给 LLM
  3. 若 LLM 调用 tool → 在 Blender 主线程执行代码 → 结果追加到历史 → 再次请求
  4. 若 LLM 直接回复文本 → 返回结果
  5. 每轮检查是否有新加载的 builtin description，追加到 system 消息

支持的 LLM 后端（通过 OpenAI 兼容 API）：
  - OpenAI / GPT-4o
  - Anthropic Claude（通过 openai 兼容层或原生 SDK）
  - 本地模型（Ollama、LM Studio 等）
"""

import json
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional, cast

from .builtin_loader import BuiltinLoader
from . import eval_core

# ── 配置 ─────────────────────────────────────────────────────────────────────

@dataclass
class AgentConfig:
    api_key: str = ""
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-4o"
    max_iterations: int = 20
    system_prompt_extra: str = ""  # 用户可在 Addon 设置中追加


# ── Tool 定义（eval_python_code）────────────────────────────────────────────

EVAL_TOOL_NAME = "eval_python_code"
ToolResultContent = str | list[dict[str, Any]]

def build_tool_definition(summaries: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": EVAL_TOOL_NAME,
            "description": (
                "在 Blender 内置 Python 解释器中执行一段 Python 代码。\n"
                "你可以使用 bpy.* 直接操作 Blender 场景与数据。\n"
                "脚本末尾必须将返回值赋给 __result__ 变量。\n"
                "若返回值包含键 'screenshot'，其值应为 base64 PNG 字符串，框架会将其展示为图像。\n\n"
                f"{summaries}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "要执行的 Python 脚本。\n"
                            "约定：\n"
                            "  - 通过 get_builtin('name') 获取 builtin 模块\n"
                            "  - 通过 get_builtin_doc('name') 查看某个 builtin 的完整 API 文档（不加载模块）\n"
                            "  - 脚本结尾必须赋值 __result__ = ...\n"
                            "  - 建议封装在 def execute(): ... / __result__ = execute() 中"
                        ),
                    }
                },
                "required": ["code"],
            },
        },
    }


# ── 系统提示 ─────────────────────────────────────────────────────────────────

BASE_SYSTEM_PROMPT = """你是一个 Blender 专家 AI，运行在 Blender 内部。
你可以通过 eval_python_code 工具执行 Python 脚本，直接使用 bpy API 操控 Blender。

规范：
1. 优先用一段脚本完成多步骤操作，减少往返次数
2. 在脚本内过滤和处理数据，只把相关结果放入 __result__，不要把大量原始数据返回
3. 需要观察场景时，调用 screenshot builtin 截图
4. 不熟悉某个 builtin 的 API 时，先执行 __result__ = get_builtin_doc('name') 查阅文档，再写调用代码
5. 遇到报错，分析错误信息后修正脚本重试
6. __result__ 若为 dict 且含 'screenshot' 键，值应为 base64 PNG

bpy 常用入口：
  bpy.context.scene            当前场景
  bpy.context.selected_objects 当前选中对象列表
  bpy.context.active_object    活动对象
  bpy.data.objects             所有对象
  bpy.data.materials           所有材质
  bpy.ops.*                    操作符（注意需要正确的 context）
"""


# ── Agent 主循环 ──────────────────────────────────────────────────────────────

class BlenderAgent:
    def __init__(self, config: AgentConfig, loader: BuiltinLoader):
        self.config = config
        self.loader = loader
        self._history: list[dict] = []
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import openai
            except ImportError:
                raise ImportError(
                    "请先安装 openai 库：在 Blender Python 中运行\n"
                    "import subprocess, sys\n"
                    "subprocess.run([sys.executable, '-m', 'pip', 'install', 'openai'])"
                )
            self._client = openai.OpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url,
            )
        return self._client

    def reset(self):
        self._history.clear()
        self.loader.reset_session()

    def chat(
        self,
        user_message: str,
        on_text: Optional[Callable[[str], None]] = None,
        on_tool_call: Optional[Callable[[str, str], None]] = None,
        on_tool_result: Optional[Callable[[str], None]] = None,
    ) -> str:
        """
        发送一条用户消息，执行 Agent 循环，返回最终文本回复。

        on_text(text)              — 每次 LLM 回复文本时的回调（用于流式更新 UI）
        on_tool_call(name, code)   — LLM 请求执行代码时的回调
        on_tool_result(result_str) — 代码执行完毕时的回调
        """
        self._history.append({"role": "user", "content": user_message})

        client = self._get_client()
        system_content = BASE_SYSTEM_PROMPT
        if self.config.system_prompt_extra:
            system_content += f"\n\n{self.config.system_prompt_extra}"

        for _iteration in range(self.config.max_iterations):
            # Fix4: 移除 role:system 的中途注入（部分模型不稳定）
            # Fix1: DESCRIPTION 现在在 tool result 中同轮注入，无需在此预注入
            messages = [{"role": "system", "content": system_content}] + self._history
            tool_def = build_tool_definition(self.loader.get_summaries())

            response = client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                tools=[tool_def],
                tool_choice="auto",
            )

            choice = response.choices[0]
            message = choice.message

            # ── 纯文本回复，结束循环 ───────────────────────────────────────
            if choice.finish_reason == "stop" or not message.tool_calls:
                text = message.content or ""
                self._history.append({"role": "assistant", "content": text})
                if on_text:
                    on_text(text)
                return text

            # ── Tool call：执行代码 ────────────────────────────────────────
            self._history.append(message)  # 保存 assistant 的 tool call 消息

            tool_results = []
            for tool_call in message.tool_calls:
                if tool_call.function.name != EVAL_TOOL_NAME:
                    continue

                args = json.loads(tool_call.function.arguments)
                code = args.get("code", "")

                if on_tool_call:
                    on_tool_call(tool_call.function.name, code)

                # 在 Blender 主线程执行
                raw_result = eval_core.run_code_from_thread(code)

                # 处理返回值：截图转为图像消息 content
                result_content = self._format_result(raw_result)

                # Fix1: 若本次执行首次加载了某些 builtin，将其 API 文档附加进 tool result。
                # 模型在同一轮即可获得正确签名，下次写代码无需盲猜。
                new_docs = self.loader.get_newly_loaded_descriptions()
                if new_docs:
                    doc_text = "\n\n".join(new_docs)
                    if isinstance(result_content, str):
                        result_content = f"{doc_text}\n\n[执行结果]\n{result_content}"
                    else:
                        # 多模态 content：在最前面插入文档文本块
                        result_content = [
                            {"type": "text", "text": f"{doc_text}\n\n[执行结果]"},
                            *result_content,
                        ]

                if on_tool_result:
                    on_tool_result(
                        result_content if isinstance(result_content, str)
                        else json.dumps(result_content, ensure_ascii=False)
                    )

                tool_results.append({
                    "tool_call_id": tool_call.id,
                    "role": "tool",
                    "content": (
                        result_content
                        if isinstance(result_content, str)
                        else json.dumps(result_content, ensure_ascii=False, default=str)
                    ),
                })

            self._history.extend(tool_results)

        return "[已达到最大迭代次数，Agent 停止]"

    @staticmethod
    def _format_result(raw: object) -> ToolResultContent:
        """
        将执行结果转换为可传给 LLM 的格式。
        若结果含 'screenshot'（base64 PNG），构建多模态 content。
        """
        if isinstance(raw, dict):
            raw_dict = cast(dict[str, object], raw)
        else:
            raw_dict = None

        if raw_dict is not None and "screenshot" in raw_dict:
            b64 = raw_dict.pop("screenshot")
            text_part = json.dumps(raw_dict, ensure_ascii=False, default=str) if raw_dict else ""
            content = []
            if text_part:
                content.append({"type": "text", "text": text_part})
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            })
            return content  # OpenAI 多模态 content 格式
        if raw_dict is not None and "error" in raw_dict:
            return f"[执行错误]\n{raw_dict['error']}"
        return json.dumps(raw, ensure_ascii=False, default=str) if not isinstance(raw, str) else raw


# ── 全局实例管理 ─────────────────────────────────────────────────────────────

_agent: Optional[BlenderAgent] = None
_loader: Optional[BuiltinLoader] = None


def setup():
    global _agent, _loader
    if _loader is None:
        _loader = BuiltinLoader()
        eval_core.setup(_loader)
    eval_core.start_timer()


def teardown():
    global _agent, _loader
    eval_core.stop_timer()
    _agent = None
    _loader = None


def get_agent(config: AgentConfig) -> BlenderAgent:
    global _agent, _loader
    if _loader is None:
        setup()
    if _agent is None or _agent.config != config:
        assert _loader is not None
        _agent = BlenderAgent(config, _loader)
    return _agent
