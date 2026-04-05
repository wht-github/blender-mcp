bl_info = {
    "name": "Blender AI Agent",
    "author": "blender-agent",
    "version": (0, 1, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > AI Agent",
    "description": "eval + builtins AI Agent for Blender",
    "category": "Interface",
}

import bpy
from . import agent_panel, agent_loop


def register():
    agent_loop.setup()
    agent_panel.register()


def unregister():
    agent_panel.unregister()
    agent_loop.teardown()
