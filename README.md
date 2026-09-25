# Blender Agent — Streamable HTTP MCP bridge

外部 AI 客户端通过 MCP 发送 Python 脚本，插件把脚本调度到 Blender 主线程执行，
再将 `__result__`（以及可选截图）作为 MCP tool result 返回。

项目的阶段目标、优先级和发布标准见 [`ROADMAP.md`](ROADMAP.md)。
能力发现与后续复用实验的设计见
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

场景执行入口是 `eval_python_code(code: str, request_id: str | None = None)`；另有不经过执行队列的
`get_execution_status(request_id: str, include_result: bool = False)` 用于查询状态。
脚本必须把返回值赋给 `__result__`；返回 dict 中的 `screenshot` 字段会转换为
MCP image content。

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
`runtime.describe("builtin.viewport.capture_objects")` 获取单个操作的签名、副作用、
UI context 要求和成本，再加载 `builtin.viewport`。搜索只返回 ID、加载 ID、摘要和评分；
完整文档通过 `runtime.describe("builtin.viewport")` 显式查询，加载时不自动附加。
每次 eval 使用独立的加载状态，使用 builtin 的同一段脚本必须先 `runtime.load()`；
底层模块缓存仍在进程内复用。旧的 `get_builtin()` / `get_builtin_doc()` 接口仍然兼容。

## 构建 Blender 安装包

运行时使用官方 `mcp` SDK。它和 Uvicorn、Pydantic 等依赖会在构建时安装到
`blender_agent/libs/`，随后一起写入安装 zip。用户不需要在 Blender 中运行 pip。

当前验证的运行组合为 Blender 5.1、Python 3.13、Windows x64。构建默认 Python 3.13 和本机平台；其他平台需要独立验证：

```powershell
uv lock
uv export --frozen --no-dev --no-emit-project --no-hashes `
  --format requirements-txt --output-file runtime-requirements.txt
uv run --no-project --python 3.13 python package.py
```

生成文件为 `blender_agent.zip`。依赖包含原生扩展，所以不同 Python 版本和平台
应分别构建 zip：

```powershell
# Windows x64 / Blender 5.1
uv run --no-project --python 3.13 python package.py `
  --python-version 3.13 `
  --python-platform x86_64-pc-windows-msvc `
  --output blender_agent-blender5-windows-x64.zip

# 依赖已经生成，仅重新打包
uv run --no-project --python 3.13 python package.py --skip-dependencies
```

`runtime-requirements.txt` 由 `uv.lock` 生成并固定传递依赖版本。修改
`pyproject.toml` 后应重新执行上面的 `uv lock` 和 `uv export`。
依赖元数据记录插件版本、Python 版本、平台和 requirements SHA-256。
`--skip-dependencies` 会核对这些字段，目标或依赖不匹配时拒绝复用；旧格式依赖需要重建。
插件导入前也会核对运行环境，避免加载不兼容的原生扩展。
依赖仍与其他 Blender 插件共享 Python 导入空间，尚不提供进程级依赖隔离。

## 安装与连接

1. Blender → Edit → Preferences → Add-ons → Install，选择生成的 zip。
2. 启用 “Blender AI Agent (MCP)”。
3. 在 3D Viewport → Sidebar → AI Agent 中启动 Server。
4. 将 MCP 客户端连接到 `http://127.0.0.1:8400/mcp`，transport 选择 HTTP。

插件启用了 SDK 的 DNS rebinding 防护，并默认只监听 `127.0.0.1`。这个 tool
可以执行任意 Python，只应连接受信任的本机 MCP 客户端。

## 开发验证

### 执行与结果约定

- HTTP 请求直接进入有界队列，最多 32 个排队任务；120 秒截止时间从提交时计算，
  包括排队时间。排队任务到期后不会再执行。
- 同一请求的最后一个等待者断开或取消时，尚未开始的任务取消；已经开始的 Python
  无法强制中断。一个等待者断开不会取消其他等待者。停止服务先关闭任务入口并取消队列。
- 文件加载前暂停接收新任务并取消旧文件的排队任务（`DOCUMENT_CHANGED`）；加载成功或
  失败后恢复服务。任务带 `document_generation`，过时代次不会被执行。
- `__result__` 只接受普通 JSON 数据（tuple 转成 list）。返回 Blender RNA 对象、
  自定义对象、循环引用、非有限浮点数或超限结果会得到 `INVALID_RESULT`。
  请显式提取 `obj.name`、`list(obj.location)` 等数据。校验及复制在主线程完成。
- 文本结果上限为 1 MiB，图片解码后上限为 10 MiB。结果校验失败不撤销已经完成的
  场景修改；脚本异常也不意味着操作已回滚。
- 修改场景前由客户端生成唯一 `request_id`（有效 UTF-8，1–128 字符），随请求提交；
  断线后直接用该 ID 调用 `get_execution_status`。未传 ID 时服务端生成，但断线前可能拿不到。
  同一保留 ID 和完全相同的代码复用任务，首次提交的截止时间不变；代码不同返回
  `REQUEST_ID_CONFLICT`。请求指纹保留最近 256 个 ID，活动任务的 ID 不提前淘汰。
- 状态查询的 `status` 保留原始响应状态；`execution_status` 为实际执行状态：
  `not_started`、`running`、`succeeded` 或 `failed`。完成后重复提交同一 ID 可以返回实际结果。
- `include_result=True` 返回缓存中的实际结果，图片仍作为 MCP image 返回。
  `result_available=False` 表示尚未完成或结果已经淘汰。最近任务历史最多 50 条；
  最终结果最多保留 5 分钟、50 条、合计 16 MiB 编码数据，达到容量时提前淘汰。
  清空历史清除结果但保留请求指纹；若 ID 仍已知而结果已丢失，返回 `RESULT_EXPIRED`，不会重跑。
  ID 淘汰或 Blender 重启后不再有去重保证。新操作使用新 ID；同一操作断线后的查询与重试使用原 ID。
- MCP `structuredContent` 提供 `request_id/status/execution_status/document_generation`、
  耗时、`result` 和 `error` 字段。业务数据中的 `error` 不再代表执行失败：
  `__result__ = {"error": None, "count": 5}` 是成功结果。图片位于 `content` 中，结构化结果
  移除 `screenshot` 并以 `image.content_index` 引用，避免重复传输 base64。

### 材质修改范围

`apply_material_to_objects`、`ensure_material_slots`、`assign_faces_by_index` 默认
`shared_data="reject"`：多个对象共享数据时，在修改前拒绝并列出受影响对象。
显式选择 `shared_data="copy"` 可为目标复制独立数据（需要 Object Mode）；选择
`shared_data="allow"` 才修改共享数据，返回的 `affected_objects` 会列出全部受影响对象。
批量调用会先预检全部有效目标，再创建材质或修改槽；这不等于对任意 Python 提供事务回滚。

### 测试命令

```powershell
uv run python -m unittest discover -s tests -v
uv run --no-project --python 3.13 python package.py

# 使用构建后的 zip 运行真实 Blender builtin 行为测试
blender.exe --background --factory-startup --python-exit-code 1 `
  --python tests/blender_builtins.py -- blender_agent.zip

# 实际文件加载边界与共享材质测试
blender.exe --background --factory-startup --python-exit-code 1 `
  --python tests/blender_document_lifecycle.py -- blender_agent.zip
blender.exe --background --factory-startup --python-exit-code 1 `
  --python tests/blender_material_scope.py

# 真实事件循环：插件启用、HTTP调用、停止、禁用后再启用；自动退出
blender.exe --factory-startup --python-exit-code 1 `
  --python tests/blender_event_loop_smoke.py -- blender_agent.zip
# 消融：在上条命令末尾加 --disable-execution-timer，必须失败并返回退出码1

# 交互模式验证 VIEW_3D 截图成功路径；测试完成后 Blender 会自动退出
blender.exe --factory-startup --python-exit-code 1 `
  --python tests/blender_viewport_smoke.py -- blender_agent.zip
```

主要源码：

- `blender_agent/mcp_server.py`：Streamable HTTP 生命周期和 MCP tool
- `blender_agent/eval_core.py`：Blender 主线程执行队列
- `blender_agent/builtin_loader.py`：builtin 发现和按需加载
- `blender_agent/runtime_context.py`：能力搜索、逻辑加载状态和 `tools` 代理
- `bundle_dependencies.py`：为目标 Blender Python/平台准备运行时依赖
- `package.py`：生成包含运行时依赖的安装 zip
