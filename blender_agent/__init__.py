bl_info = {
    "name": "Blender AI Agent",
    "author": "blender-agent",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > AI Agent",
    "description": "eval + builtins AI Agent for Blender",
    "category": "Interface",
}

import os
import sys

# 将 libs 目录加入 sys.path，使 openai 等依赖可直接 import
# （wheels 解压后包含 .pyd/.so 原生扩展，必须从普通目录加载）
_libs_dir = os.path.join(os.path.dirname(__file__), "libs")
if os.path.isdir(_libs_dir) and _libs_dir not in sys.path:
    sys.path.insert(0, _libs_dir)

import bpy
from . import agent_panel, agent_loop


def register():
    agent_loop.setup()
    agent_panel.register()


def unregister():
    agent_panel.unregister()
    agent_loop.teardown()
