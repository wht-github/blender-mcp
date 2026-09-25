"""End-to-end smoke test run by Blender against a built add-on zip."""

from __future__ import annotations

import asyncio
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import zipfile

import bpy


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _zip_argument() -> Path:
    if "--" not in sys.argv:
        raise RuntimeError("Pass the add-on zip after '--'")
    index = sys.argv.index("--")
    return Path(sys.argv[index + 1]).resolve()


def main() -> None:
    archive = _zip_argument()
    if not archive.exists():
        raise FileNotFoundError(archive)

    # Windows keeps imported .pyd files locked until Blender exits, so cleanup
    # may leave the temporary directory behind after a successful smoke test.
    with tempfile.TemporaryDirectory(
        prefix="blender-agent-smoke-",
        ignore_cleanup_errors=True,
    ) as temp_dir:
        with zipfile.ZipFile(archive) as package_zip:
            package_zip.extractall(temp_dir)
        sys.path.insert(0, temp_dir)

        import blender_agent
        from blender_agent import eval_core, mcp_server

        blender_agent.register()
        port = _free_port()
        mcp_server.start(port=port)

        result_text = []
        errors = []

        def run_client() -> None:
            async def call_tool() -> None:
                from mcp import ClientSession
                from mcp.client.streamable_http import streamable_http_client

                async with streamable_http_client(mcp_server.get_url()) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(
                            "eval_python_code",
                            {
                                "code": (
                                    "import threading\n"
                                    "matches = runtime.search('scene hierarchy')['matches']\n"
                                    "runtime.load('builtin.scene_info')\n"
                                    "__result__ = {\n"
                                    "    'blender': bpy.app.version_string,\n"
                                    "    'main_thread': threading.current_thread() is threading.main_thread(),\n"
                                    "    'found': any(\n"
                                    "        item['id'] == 'builtin.scene_info'\n"
                                    "        or item.get('load_id') == 'builtin.scene_info'\n"
                                    "        for item in matches\n"
                                    "    ),\n"
                                    "    'module': tools.scene_info.__name__,\n"
                                    "    'loaded': [\n"
                                    "        item['id'] for item in runtime.list()['loaded']\n"
                                    "    ],\n"
                                    "}"
                                )
                            },
                        )
                        if result.isError:
                            raise RuntimeError(str(result.content))
                        result_text.extend(
                            getattr(item, "text", "")
                            for item in result.content
                            if item.type == "text"
                        )
                        for code, expected in (
                            ("__result__ = bpy.context.active_object", "INVALID_RESULT"),
                            ("raise SystemExit(0)", "EXECUTION_ERROR"),
                        ):
                            rejected = await session.call_tool("eval_python_code", {"code": code})
                            if not rejected.isError or expected not in str(rejected.content):
                                raise AssertionError(f"Execution boundary failed: {rejected}")
                        recovered = await session.call_tool("eval_python_code", {"code": "__result__ = 42"})
                        if recovered.isError:
                            raise AssertionError(f"Execution did not recover: {recovered}")
                        request_id = recovered.structuredContent["request_id"]
                        state = await session.call_tool(
                            "get_execution_status", {"request_id": request_id, "include_result": True}
                        )
                        if state.isError or "succeeded" not in str(state.content):
                            raise AssertionError(f"Final result lookup failed: {state}")

            try:
                asyncio.run(call_tool())
            except BaseException as exc:
                errors.append(exc)

        client = threading.Thread(target=run_client, name="blender-agent-smoke-client")
        client.start()
        deadline = time.monotonic() + 30.0
        while client.is_alive() and time.monotonic() < deadline:
            # The test script owns Blender's main thread, so service the timer
            # callback directly instead of waiting for Blender's event loop.
            eval_core._timer_callback()
            time.sleep(0.01)
        client.join(timeout=1.0)

        try:
            if client.is_alive():
                raise TimeoutError("MCP smoke test client did not finish")
            if errors:
                raise errors[0]
            combined = "\n".join(result_text)
            if bpy.app.version_string not in combined:
                raise AssertionError(f"Unexpected MCP result: {combined}")
            if '"found": true' not in combined:
                raise AssertionError(f"Capability search failed: {combined}")
            if '"main_thread": true' not in combined:
                raise AssertionError(f"Execution escaped Blender main thread: {combined}")
            if '"builtin.scene_info"' not in combined:
                raise AssertionError(f"Capability load failed: {combined}")
            print(f"BLENDER_AGENT_SMOKE_OK {combined}")
        finally:
            blender_agent.unregister()


if __name__ == "__main__":
    main()
