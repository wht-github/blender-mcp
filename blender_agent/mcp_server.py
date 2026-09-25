"""
mcp_server.py — Blender MCP Server (Streamable HTTP transport)

The MCP protocol and HTTP transport are provided by the official Python SDK.
Blender API work is still delegated to eval_core so it runs on Blender's main
thread rather than the ASGI server thread.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import json
import socket
import threading
import time
from typing import Any, Optional, cast

from .builtin_loader import BuiltinLoader
from . import eval_core
from .result_codec import MAX_TEXT_BYTES, snapshot_result, ResultValidationError


_MCP_PATH = "/mcp"
_STARTUP_TIMEOUT = 10.0
_SHUTDOWN_TIMEOUT = 5
_EXECUTION_TIMEOUT = 120.0


def _build_tool_description(_loader: BuiltinLoader) -> str:
    return (
        "在 Blender 内置 Python 解释器中执行一段 Python 代码。\n"
        "你可以使用 bpy.* 直接操作 Blender 场景与数据。\n"
        "脚本末尾必须将返回值赋给 __result__ 变量。\n"
        "返回值只能包含 JSON 数据；Blender 对象请显式提取名称、坐标等字段。\n"
        "修改场景时请预先生成唯一 request_id 并随调用提交；断线后用该 ID 查询。\n"
        "保留窗口内相同 request_id 和代码不会重复执行；不要用同一 ID 提交不同代码。\n"
        "执行超时后，用 get_execution_status(request_id, include_result=True) 查询最终结果，避免重复修改。\n"
        "若返回值包含键 'screenshot'，其值应为 base64 PNG 字符串。\n\n"
        "能力使用顺序：\n"
        "  1. 每次调用使用独立 runtime；模块缓存复用，加载状态不跨调用保留\n"
        "  2. 不确定能力时用 runtime.search(query) 搜索摘要\n"
        "  3. 优先用 runtime.describe('builtin.name.operation') 查看单个操作的精确 API\n"
        "  4. 在使用能力的同一段代码中 runtime.load('builtin.name')，再通过 tools.name 调用\n"
        "文档只由 describe/get_builtin_doc 显式返回，load 不自动附加文档。\n\n"
        "兼容接口：get_builtin('name') 会加载并激活 builtin；"
        "get_builtin_doc('name') 返回完整文档。\n"
        "约定：\n"
        "  - 聚焦对象并截图时优先使用 viewport builtin\n"
        "  - builtin 只保证文档中的规范函数名和参数名，不要猜别名\n"
        "  - 脚本结尾必须赋值 __result__ = ...\n"
        "  - 建议封装在 def execute(): ... / __result__ = execute() 中"
    )


def _format_mcp_result(raw: object, *, error: dict[str, Any] | None = None,
                       metadata: dict[str, Any] | None = None, docs=None):
    """Keep execution errors separate from user data and expose a stable envelope."""
    from mcp.types import CallToolResult, ImageContent, TextContent

    metadata = dict(metadata or {})
    if isinstance(raw, eval_core.TaskOutcome):
        metadata.update(
            request_id=raw.request_id,
            status=raw.status,
            execution_status=raw.execution_status,
            document_generation=raw.document_generation,
            queue_ms=raw.queue_ms,
            execution_ms=raw.execution_ms,
        )
        error, docs, raw = raw.error, raw.docs, raw.result
    elif isinstance(raw, eval_core.ExecutionResult):
        error, docs, raw = raw.error, raw.docs, raw.value

    content = []
    value: Any = raw
    if error is None:
        try:
            value = snapshot_result(raw)
        except (ResultValidationError, UnicodeError) as exc:
            error = {"code": "INVALID_RESULT", "message": str(exc)}
            value = None

    envelope = {**metadata, "result": value, "error": error}
    if error is not None:
        text = f"[执行错误: {error['code']}]\n{error['message']}"
        if error.get("details"):
            text += "\n" + error["details"]
        if error.get("execution_continues"):
            text += "\n客户端停止等待不会中断已经运行的 Python。"
        content.append(TextContent(type="text", text=text))
    else:
        if isinstance(value, dict):
            payload = cast(dict[str, Any], value).copy()
            screenshot = payload.pop("screenshot", None)
            if screenshot is not None:
                value = payload
                content.append(ImageContent(type="image", data=screenshot, mimeType="image/png"))
                envelope["result"] = value
                envelope["image"] = {"content_index": 0, "mime_type": "image/png"}
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, allow_nan=False)
        content.append(TextContent(type="text", text=text))

    if docs:
        docs_text = "\n\n".join(docs)
        if len(docs_text.encode("utf-8")) <= MAX_TEXT_BYTES:
            content.append(TextContent(type="text", text=docs_text))
    # Keep metadata available to clients that only consume text content too.
    if metadata:
        content.append(TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False)))
    return CallToolResult(content=content, structuredContent=envelope, isError=error is not None)


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


def _ensure_port_available(host: str, port: int) -> None:
    """Fail before starting Uvicorn so Blender can show a useful port error."""
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise RuntimeError(f"Invalid MCP host '{host}': {exc}") from exc

    last_error: OSError | None = None
    for family, socktype, proto, _canonname, sockaddr in addresses:
        probe = socket.socket(family, socktype, proto)
        try:
            probe.bind(sockaddr)
            return
        except OSError as exc:
            last_error = exc
        finally:
            probe.close()
    raise RuntimeError(f"MCP address {host}:{port} is unavailable: {last_error}")


async def _execute_request(code: str, timeout: float, request=None, request_id: str | None = None):
    task = eval_core.submit_code(code, timeout, request_id=request_id)
    caller = asyncio.current_task()
    assert caller is not None

    async def watch_disconnect():
        # JSON-response HTTP does not itself cancel the SDK handler on disconnect.
        # The SDK has already consumed the request body before dispatching the tool.
        assert request is not None
        while True:
            if await request.is_disconnected():
                caller.cancel()
                return
            await asyncio.sleep(0.05)

    watcher = asyncio.create_task(watch_disconnect()) if request is not None else None
    try:
        return await task.wait_async(timeout)
    finally:
        if watcher is not None:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher


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
    async def eval_python_code(code: str, request_id: str | None = None):
        """Execute Python in Blender and return __result__."""
        if not code.strip():
            return _format_mcp_result(None, error={"code": "INVALID_CODE", "message": "Code must not be empty"})

        request = mcp.get_context().request_context.request
        raw_result = await _execute_request(code, _EXECUTION_TIMEOUT, request, request_id)
        return _format_mcp_result(raw_result)

    @mcp.tool(
        name="get_execution_status",
        description=(
            "按 request_id 查询执行状态，不经过 Blender 执行队列。"
            "超时后请查询 execution_status，避免重复修改场景。"
            "include_result=True 返回保留的最终结果（最多保留 5 分钟，容量不足时提前淘汰）。"
        ),
        structured_output=False,
    )
    async def get_execution_status(request_id: str, include_result: bool = False):
        state = eval_core.get_execution_status(request_id, include_result)
        value = state.pop("result", None)
        error = state.pop("error", None)
        docs = state.pop("docs", ())
        return _format_mcp_result(value, error=error, metadata=state, docs=docs)

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
        if is_running():
            print(f"[blender-agent] MCP server already running on {get_url()}")
            return
        if _runtime.thread is not None and _runtime.thread.is_alive():
            raise RuntimeError("MCP server is still starting or stopping; retry after it exits")
        stop()

    _ensure_port_available(host, port)

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
    # Release queued waiters before joining the HTTP thread on Blender's main thread.
    # Closing admission first also prevents requests racing with shutdown.
    eval_core.stop_timer()
    if runtime is not None:
        runtime.server.should_exit = True
        if runtime.thread is not None and runtime.thread is not threading.current_thread():
            runtime.thread.join(timeout=_SHUTDOWN_TIMEOUT)
            if runtime.thread.is_alive():
                runtime.server.force_exit = True
                runtime.thread.join(timeout=1.0)
            if runtime.thread.is_alive():
                raise RuntimeError("MCP server is still stopping; retry after pending requests finish")

    _runtime = None
    _loader = None
    print("[blender-agent] MCP server stopped")


def is_running() -> bool:
    return (
        _runtime is not None
        and _runtime.thread is not None
        and _runtime.server.started
        and not _runtime.server.should_exit
        and _runtime.thread.is_alive()
    )


def get_url() -> str:
    if _runtime is None:
        return ""
    return f"http://{_runtime.host}:{_runtime.port}{_MCP_PATH}"
