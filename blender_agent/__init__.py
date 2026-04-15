bl_info = {
    "name": "Blender AI Agent (MCP)",
    "author": "blender-agent",
    "version": (0, 2, 0),
    "blender": (4, 0, 0),
    "location": "View3D > Sidebar > AI Agent",
    "description": "MCP Server: expose Blender Python eval as a tool for AI agents",
    "category": "Interface",
}

import bpy
from . import mcp_server, agent_panel


def register():
    agent_panel.register()


def unregister():
    mcp_server.stop()
    agent_panel.unregister()
