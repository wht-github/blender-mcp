bl_info = {
    "name": "Blender AI Agent (MCP)",
    "author": "blender-agent",
    "version": (0, 3, 0),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar > AI Agent",
    "description": "MCP Server: expose Blender Python eval as a tool for AI agents",
    "category": "Interface",
}

import os
import site
import sys
from pathlib import Path

from .bundle_contract import validate_bundle


# Runtime wheels are installed into this directory by package.py.  Using
# addsitedir (instead of only sys.path.insert) also processes wheel-provided
# .pth files such as pywin32's bootstrap file.
_libs_dir = os.path.join(os.path.dirname(__file__), "libs")
_dll_handles = []
if os.path.isdir(_libs_dir):
    validate_bundle(Path(_libs_dir), addon_version=".".join(map(str, bl_info["version"])))
    site.addsitedir(_libs_dir)
    if _libs_dir in sys.path:
        sys.path.remove(_libs_dir)
    sys.path.insert(0, _libs_dir)

    if hasattr(os, "add_dll_directory"):
        for dll_dir in (_libs_dir, os.path.join(_libs_dir, "pywin32_system32")):
            if os.path.isdir(dll_dir):
                _dll_handles.append(os.add_dll_directory(dll_dir))

import bpy
from . import mcp_server, agent_panel


def register():
    agent_panel.register()


def unregister():
    mcp_server.stop()
    agent_panel.unregister()
