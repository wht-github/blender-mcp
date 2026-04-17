"""
mcp_server.py — Blender MCP Server (SSE transport)

使用标准库实现一个轻量 MCP (Model Context Protocol) 服务器，
通过 SSE (Server-Sent Events) 传输与外部 MCP 客户端通信。

唯一暴露的 tool: eval_python_code
  - 接收 Python 代码字符串
  - 在 Blender 主线程中执行（通过 eval_core）
  - 返回 __result__ 的值

MCP 规范参考: https://modelcontextprotocol.io/specification
"""

import json
import queue
import threading
import traceback
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs
import uuid

from .builtin_loader import BuiltinLoader
from . import eval_core


# ── Tool 描述 ─────────────────────────────────────────────────────────────────

def _build_tool_description(loader: BuiltinLoader) -> dict:
    summaries = loader.get_summaries()
    return {
        "name": "eval_python_code",
        "description": (
            "在 Blender 内置 Python 解释器中执行一段 Python 代码。\n"
            "你可以使用 bpy.* 直接操作 Blender 场景与数据。\n"
            "脚本末尾必须将返回值赋给 __result__ 变量。\n"
            "若返回值包含键 'screenshot'，其值应为 base64 PNG 字符串。\n\n"
            f"{summaries}\n\n"
            "约定：\n"
            "  - 通过 get_builtin('name') 获取 builtin 模块\n"
            "  - 通过 get_builtin_doc('name') 查看某个 builtin 的完整 API 文档\n"
            "  - 脚本结尾必须赋值 __result__ = ...\n"
            "  - 建议封装在 def execute(): ... / __result__ = execute() 中"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "要在 Blender 中执行的 Python 代码",
                }
            },
            "required": ["code"],
        },
    }


# ── SSE Session ───────────────────────────────────────────────────────────────

class SSESession:
    """管理一个 SSE 连接的消息队列。"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.messages: queue.Queue[str] = queue.Queue()

    def send_event(self, event: str, data: str):
        self.messages.put(f"event: {event}\ndata: {data}\n\n")

    def send_message(self, msg: dict):
        self.send_event("message", json.dumps(msg, ensure_ascii=False))


# ── MCP Request Handler ──────────────────────────────────────────────────────

class MCPHandler(BaseHTTPRequestHandler):
    """HTTP handler for MCP SSE transport."""

    server: "MCPServer"  # type: ignore[assignment]

    def log_message(self, format, *args):
        # 避免刷屏，只打印关键日志
        pass

    def _send_cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/sse":
            self._handle_sse()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/messages" or parsed.path.startswith("/messages"):
            self._handle_message()
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_sse(self):
        """建立 SSE 连接，发送 endpoint 事件。"""
        session_id = str(uuid.uuid4())
        session = SSESession(session_id)
        self.server.sessions[session_id] = session

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._send_cors_headers()
        self.end_headers()

        # 发送 endpoint 事件，告知客户端 POST 地址
        endpoint = f"/messages?sessionId={session_id}"
        self.wfile.write(f"event: endpoint\ndata: {endpoint}\n\n".encode())
        self.wfile.flush()

        # 持续推送消息
        try:
            while not self.server._stopping:
                try:
                    data = session.messages.get(timeout=5)
                    self.wfile.write(data.encode())
                    self.wfile.flush()
                except queue.Empty:
                    # 发送心跳保持连接
                    self.wfile.write(": heartbeat\n\n".encode())
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.sessions.pop(session_id, None)

    def _handle_message(self):
        """处理 MCP JSON-RPC 请求。"""
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        session_id = params.get("sessionId", [None])[0]

        if not session_id or session_id not in self.server.sessions:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "invalid session"}')
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        request = json.loads(body)

        session = self.server.sessions[session_id]

        # 在后台线程处理请求，避免阻塞 HTTP handler
        threading.Thread(
            target=self._process_request,
            args=(request, session),
            daemon=True,
        ).start()

        # 立即返回 202 Accepted
        self.send_response(202)
        self._send_cors_headers()
        self.end_headers()

    def _process_request(self, request: dict, session: SSESession):
        """处理 JSON-RPC 请求并通过 SSE 返回结果。"""
        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params", {})

        try:
            if method == "initialize":
                result = self._handle_initialize(params)
            elif method == "notifications/initialized":
                return  # 通知类消息，不需要回复
            elif method == "tools/list":
                result = self._handle_tools_list()
            elif method == "tools/call":
                result = self._handle_tools_call(params)
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            elif method == "ping":
                result = {}
            else:
                session.send_message({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                })
                return

            session.send_message({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": result,
            })

        except Exception as e:
            session.send_message({
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(e)},
            })

    def _handle_initialize(self, params: dict) -> dict:
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": "blender-agent",
                "version": "0.2.0",
            },
        }

    def _handle_tools_list(self) -> dict:
        tool = _build_tool_description(self.server.loader)
        return {"tools": [tool]}

    def _handle_tools_call(self, params: dict) -> dict:
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name != "eval_python_code":
            return {
                "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                "isError": True,
            }

        code = arguments.get("code", "")
        if not code:
            return {
                "content": [{"type": "text", "text": "Missing 'code' argument"}],
                "isError": True,
            }

        # 在 Blender 主线程执行代码
        raw_result = eval_core.run_code_from_thread(code, timeout=120.0)

        # 格式化结果
        return self._format_mcp_result(raw_result)

    @staticmethod
    def _format_mcp_result(raw: object) -> dict:
        """将 eval 结果转换为 MCP content 格式。"""
        content = []

        if isinstance(raw, dict):
            if "error" in raw:
                return {
                    "content": [{"type": "text", "text": f"[执行错误]\n{raw['error']}"}],
                    "isError": True,
                }

            if "screenshot" in raw:
                b64 = raw.pop("screenshot")
                content.append({
                    "type": "image",
                    "data": b64,
                    "mimeType": "image/png",
                })
                if raw:
                    content.append({
                        "type": "text",
                        "text": json.dumps(raw, ensure_ascii=False, default=str),
                    })
                return {"content": content}

        text = json.dumps(raw, ensure_ascii=False, default=str) if not isinstance(raw, str) else raw
        return {"content": [{"type": "text", "text": text}]}


# ── MCP Server ────────────────────────────────────────────────────────────────

class MCPServer(ThreadingMixIn, HTTPServer):
    """带有 session 管理和 BuiltinLoader 引用的 HTTP Server。

    使用 ThreadingMixIn 让每个请求在独立线程中处理，
    避免 SSE 长连接阻塞 serve_forever 循环。
    """
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, host: str, port: int, loader: BuiltinLoader):
        self.sessions: dict[str, SSESession] = {}
        self.loader = loader
        self._stopping = False
        super().__init__((host, port), MCPHandler)


# ── 全局控制 ──────────────────────────────────────────────────────────────────

_server: Optional[MCPServer] = None
_thread: Optional[threading.Thread] = None
_loader: Optional[BuiltinLoader] = None


def start(host: str = "127.0.0.1", port: int = 8400):
    """启动 MCP server（后台线程）。"""
    global _server, _thread, _loader

    if _server is not None:
        print(f"[blender-agent] MCP server already running on {host}:{port}")
        return

    _loader = BuiltinLoader()
    eval_core.setup(_loader)
    eval_core.start_timer()

    _server = MCPServer(host, port, _loader)
    _thread = threading.Thread(target=_server.serve_forever, daemon=True)
    _thread.start()

    print(f"[blender-agent] MCP server started on http://{host}:{port}/sse")


def stop():
    """停止 MCP server。"""
    global _server, _thread, _loader

    eval_core.stop_timer()

    if _server is not None:
        _server._stopping = True
        _server.shutdown()
        _server.server_close()
        _server = None
    _thread = None
    _loader = None

    print("[blender-agent] MCP server stopped")


def is_running() -> bool:
    return _server is not None


def get_url() -> str:
    if _server is not None:
        host, port = _server.server_address
        return f"http://{host}:{port}/sse"
    return ""
