"""
mcp_server.py — Blender MCP Server (Streamable HTTP transport)

The MCP protocol and HTTP transport are provided by the official Python SDK.
Blender API work is still delegated to eval_core so it runs on Blender's main
thread rather than the ASGI server thread.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import json
import threading
import time
from typing import Any, Optional

from .builtin_loader import BuiltinLoader
from . import eval_core


_MCP_PATH = "/mcp"
_STARTUP_TIMEOUT = 10.0
_SHUTDOWN_TIMEOUT = 5
_EXECUTION_TIMEOUT = 120.0


def _build_tool_description(loader: BuiltinLoader) -> str:
    summaries = loader.get_summaries()
    return (
        "在 Blender 内置 Python 解释器中执行一段 Python 代码。\n"
        "你可以使用 bpy.* 直接操作 Blender 场景与数据。\n"
        "脚本末尾必须将返回值赋给 __result__ 变量。\n"
        "若返回值包含键 'screenshot'，其值应为 base64 PNG 字符串。\n\n"
        f"{summaries}\n\n"
        "约定：\n"
        "  - 通过 get_builtin('name') 获取 builtin 模块\n"
        "  - 通过 get_builtin_doc('name') 查看某个 builtin 的完整 API 文档\n"
        "  - 聚焦对象并截图时优先使用 viewport builtin\n"
        "  - builtin 只保证文档中的规范函数名和参数名，不要猜别名\n"
        "  - 脚本结尾必须赋值 __result__ = ...\n"
        "  - 建议封装在 def execute(): ... / __result__ = execute() 中"
    )


def _format_mcp_result(raw: object, docs: list[str] | None = None):
    """Convert an eval result into an SDK-native CallToolResult."""
    try:
        from mcp.types import CallToolResult, ImageContent, TextContent
    except ImportError as exc:  # pragma: no cover - handled during server start
        raise RuntimeError("Bundled MCP runtime is unavailable") from exc

    docs = docs or []
    content = []

    if isinstance(raw, dict):
        payload = dict(raw)
        if "error" in payload:
            text = f"[执行错误]\n{payload['error']}"
            if payload.get("timed_out"):
                text += "\n注意：超时不能中断已经在 Blender 主线程中运行的 Python 代码。"
            content.append(TextContent(type="text", text=text))
            return CallToolResult(content=content, isError=True)

        screenshot = payload.pop("screenshot", None)
        if screenshot is not None:
            # Validate early so malformed tool output becomes a clear MCP error.
            try:
                base64.b64decode(str(screenshot), validate=True)
            except (ValueError, TypeError) as exc:
                return CallToolResult(
                    content=[TextContent(type="text", text=f"Invalid screenshot base64: {exc}")],
                    isError=True,
                )
            content.append(ImageContent(type="image", data=str(screenshot), mimeType="image/png"))
        if payload:
            content.append(
                TextContent(type="text", text=json.dumps(payload, ensure_ascii=False, default=str))
            )
    else:
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, default=str)
        content.append(TextContent(type="text", text=text))

    if docs:
        content.append(TextContent(type="text", text="\n\n".join(docs)))
    return CallToolResult(content=content)


def _transport_security(host: str, port: int):
    from mcp.server.transport_security import TransportSecuritySettings

    host_names = {host}
    if host in {"127.0.0.1", "localhost", "::1"}:
        host_names.update({"127.0.0.1", "localhost", "[::1]"})

    allowed_hosts = sorted(f"{name}:{port}" for name in host_names)
    allowed_origins = sorted(f"http://{name}:{port}" for name in host_names)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def _create_app(loader: BuiltinLoader, host: str, port: int):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError(
            "MCP runtime dependencies are missing. Build the add-on with "
            "'python package.py' so blender_agent/libs is included."
        ) from exc

    mcp = FastMCP(
        "blender-agent",
        instructions="Execute trusted Python code in the active Blender session.",
        host=host,
        port=port,
        streamable_http_path=_MCP_PATH,
        json_response=True,
        stateless_http=True,
        transport_security=_transport_security(host, port),
    )

    @mcp.tool(
        name="eval_python_code",
        description=_build_tool_description(loader),
        structured_output=False,
    )
    async def eval_python_code(code: str):
        """Execute Python in Blender and return __result__."""
        if not code.strip():
            return _format_mcp_result({"error": "Missing 'code' argument"})

        raw_result = await asyncio.to_thread(
            eval_core.run_code_from_thread,
            code,
            _EXECUTION_TIMEOUT,
        )
        docs = loader.get_newly_loaded_descriptions()
        return _format_mcp_result(raw_result, docs=docs)

    return mcp.streamable_http_app()


@dataclass
class _Runtime:
    host: str
    port: int
    server: Any
    thread: threading.Thread | None = None
    error: BaseException | None = None


_runtime: Optional[_Runtime] = None
_loader: Optional[BuiltinLoader] = None


def _serve(runtime: _Runtime) -> None:
    try:
        runtime.server.run()
    except BaseException as exc:
        runtime.error = exc


def start(host: str = "127.0.0.1", port: int = 8400):
    """Start the Streamable HTTP server in a background thread."""
    global _runtime, _loader

    if _runtime is not None:
        print(f"[blender-agent] MCP server already running on {get_url()}")
        return

    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError(
            "Uvicorn is missing from the bundled MCP runtime. Rebuild with 'python package.py'."
        ) from exc

    loader = BuiltinLoader()
    app = _create_app(loader, host, port)
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=_SHUTDOWN_TIMEOUT,
    )
    server = uvicorn.Server(config)
    runtime = _Runtime(host=host, port=port, server=server)
    runtime.thread = threading.Thread(
        name="blender-agent-mcp",
        target=_serve,
        args=(runtime,),
        daemon=True,
    )

    _loader = loader
    eval_core.setup(loader)
    eval_core.start_timer()
    _runtime = runtime
    assert runtime.thread is not None
    runtime.thread.start()

    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if server.started:
            print(f"[blender-agent] MCP server started on {get_url()}")
            return
        if runtime.error is not None or not runtime.thread.is_alive():
            break
        time.sleep(0.02)

    error = runtime.error
    stop()
    if error is not None:
        raise RuntimeError(f"MCP server failed to start: {error}") from error
    raise RuntimeError(f"MCP server did not start within {_STARTUP_TIMEOUT:.0f} seconds")


def stop():
    """Stop accepting requests and release the HTTP port."""
    global _runtime, _loader

    runtime = _runtime
    _runtime = None
    if runtime is not None:
        runtime.server.should_exit = True
        if runtime.thread is not None and runtime.thread is not threading.current_thread():
            runtime.thread.join(timeout=_SHUTDOWN_TIMEOUT)
            if runtime.thread.is_alive():
                runtime.server.force_exit = True
                runtime.thread.join(timeout=1.0)

    eval_core.stop_timer()
    _loader = None
    print("[blender-agent] MCP server stopped")


def is_running() -> bool:
    return (
        _runtime is not None
        and _runtime.thread is not None
        and _runtime.server.started
        and _runtime.thread.is_alive()
    )


def get_url() -> str:
    if _runtime is None:
        return ""
    return f"http://{_runtime.host}:{_runtime.port}{_MCP_PATH}"
