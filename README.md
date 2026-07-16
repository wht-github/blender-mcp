# Blender Agent — Streamable HTTP MCP bridge

外部 AI 客户端通过 MCP 发送 Python 脚本，插件把脚本调度到 Blender 主线程执行，
再将 `__result__`（以及可选截图）作为 MCP tool result 返回。

项目的阶段目标、优先级和发布标准见 [`ROADMAP.md`](ROADMAP.md)。
当前最高优先级的解释器能力设计见
[`docs/RUNTIME_CONTEXT.md`](docs/RUNTIME_CONTEXT.md)。

## 架构

```text
MCP client
    │  Streamable HTTP /mcp
    ▼
official MCP Python SDK + Uvicorn (background thread)
    │  FIFO execution queue
    ▼
bpy.app.timers (Blender main thread)
    │
    ▼
Runtime Context
    ├── runtime.search / describe / load / unload / list
    ├── tools.<loaded_builtin>
    └── bpy API
```

服务只暴露一个 tool：`eval_python_code(code: str)`。脚本必须把返回值赋给
`__result__`；返回 dict 中的 `screenshot` 字段会转换为 MCP image content。

```python
def execute():
    runtime.load("builtin.scene_info", "builtin.viewport")
    bare = tools.scene_info.find_objects(type="MESH", no_material=True)
    if not bare:
        return "所有 Mesh 对象均已有材质"

    image = tools.viewport.capture_objects([bare[0]], width=1024)
    return tools.viewport.as_result(
        image,
        message="缺少材质对象截图",
        objects=bare,
    )

__result__ = execute()
```

不确定需要哪个 builtin 时，先执行
`__result__ = runtime.search("聚焦没有材质的对象并截图")`，再用
`runtime.describe("builtin.viewport")` 获取精确 API。完整文档只在描述或首次加载时
披露，不会常驻在 MCP tool description 中。旧的 `get_builtin()` /
`get_builtin_doc()` 接口仍然兼容。

## 构建 Blender 安装包

运行时使用官方 `mcp` SDK。它和 Uvicorn、Pydantic 等依赖会在构建时安装到
`blender_agent/libs/`，随后一起写入安装 zip。用户不需要在 Blender 中运行 pip。

默认目标是 Blender 5.x 使用的 Python 3.13，以及当前构建机的平台：

```powershell
uv lock
uv export --frozen --no-dev --no-emit-project --no-hashes `
  --format requirements-txt --output-file runtime-requirements.txt
uv run --no-project --python 3.13 python package.py
```

生成文件为 `blender_agent.zip`。依赖包含原生扩展，所以不同 Python 版本和平台
应分别构建 zip：

```powershell
# Windows x64 / Blender 5.x
uv run --no-project --python 3.13 python package.py `
  --python-version 3.13 `
  --python-platform x86_64-pc-windows-msvc `
  --output blender_agent-blender5-windows-x64.zip

# 依赖已经生成，仅重新打包
uv run --no-project --python 3.13 python package.py --skip-dependencies
```

`runtime-requirements.txt` 由 `uv.lock` 生成并固定传递依赖版本。修改
`pyproject.toml` 后应重新执行上面的 `uv lock` 和 `uv export`。

## 安装与连接

1. Blender → Edit → Preferences → Add-ons → Install，选择生成的 zip。
2. 启用 “Blender AI Agent (MCP)”。
3. 在 3D Viewport → Sidebar → AI Agent 中启动 Server。
4. 将 MCP 客户端连接到 `http://127.0.0.1:8400/mcp`，transport 选择 HTTP。

插件启用了 SDK 的 DNS rebinding 防护，并默认只监听 `127.0.0.1`。这个 tool
可以执行任意 Python，只应连接受信任的本机 MCP 客户端。

## 开发验证

```powershell
uv run python -m unittest discover -s tests -v
uv run --no-project --python 3.13 python package.py
```

主要源码：

- `blender_agent/mcp_server.py`：Streamable HTTP 生命周期和 MCP tool
- `blender_agent/eval_core.py`：Blender 主线程执行队列
- `blender_agent/builtin_loader.py`：builtin 发现和按需加载
- `blender_agent/runtime_context.py`：能力搜索、逻辑加载状态和 `tools` 代理
- `bundle_dependencies.py`：为目标 Blender Python/平台准备运行时依赖
- `package.py`：生成包含运行时依赖的安装 zip
