# Runtime Context 与渐进式能力披露

状态：最高优先级设计  
更新日期：2026-07-16

## 目标

Blender Agent 不应通过不断增加顶层 MCP tools 来扩展能力，也不应要求模型在每次
调用中重复生成相同的 `bpy` 代码。核心方向是保留稳定、极小的 MCP 接口，在
Blender Python 解释器内部提供一个可按任务成长的 Runtime Context：

```text
固定 MCP tool: eval_python_code
             │
             ▼
       Runtime Context
       ├── 搜索可用能力
       ├── 按需加载 builtin
       ├── 渐进披露文档
       ├── 卸载当前不需要的能力
       └── 保存经过验证的可复用函数
```

Agent 应优先复用已有能力，只在缺少合适能力时使用裸 `bpy` 探索。成功且可泛化的
探索代码可以在当前 runtime 中封装为函数，减少后续代码生成、上下文消耗和 API
猜测。

## 产品边界

- MCP 顶层保持稳定，第一阶段仍只暴露 `eval_python_code`。
- 动态加载、卸载和函数封装发生在解释器 Runtime Context 内部。
- builtin 是经过项目维护和测试的正式能力。
- runtime function 是 Agent 在执行过程中创建的可复用组合能力。
- runtime function 多次验证、完成泛化和测试后，才可能晋升为正式 builtin。
- Runtime Context 不是安全沙箱；任意 Python 仍具有 Blender 和本机权限。
- Blender UI 只负责连接、当前执行状态、确认和恢复，不承担能力或代码历史浏览。

## 为什么不动态修改 MCP tool list

MCP 和当前 Python SDK 支持动态增加、移除 tool 以及
`notifications/tools/list_changed`，但不适合作为本项目的主要能力扩展机制：

- 客户端对动态刷新 tool list 的支持并不一致。
- 当前服务使用 stateless Streamable HTTP，不能依赖隐式连接状态。
- 顶层 tools 持续增长会增加模型上下文、工具选择冲突和缓存失效。
- runtime function 通常只是当前 Blender 实例或当前任务中的组合能力，不应立即
  成为所有客户端可见的公共接口。

因此，动态 MCP tool 发布只作为未来的可选晋升机制；基础调用始终可通过
`eval_python_code` 和 Runtime Context 完成。

## Runtime Context 数据模型

每个 runtime 使用显式 ID 标识，不能依赖 HTTP 连接代表会话：

```python
@dataclass
class RuntimeContext:
    runtime_id: str
    revision: int
    loaded_capabilities: dict[str, LoadedCapability]
    functions: dict[str, RuntimeFunction]
    created_at: float
    last_used_at: float
```

只持久保存明确注册的能力和函数，不永久保存整份 `exec_globals`。每次 eval 仍创建
干净的临时命名空间，避免积累：

- 已被删除的 `bpy` 对象引用；
- 大型临时数组、图片或结果；
- 只对某个场景状态有效的局部变量；
- 难以解释的跨任务全局状态。

执行命名空间注入：

```python
{
    "bpy": bpy,
    "runtime": RuntimeFacade(context),
    "tools": LoadedBuiltinProxy(context),
    "functions": RuntimeFunctionProxy(context),
    "__builtins__": __builtins__,
}
```

## 解释器接口

第一版固定提供以下接口：

```python
runtime.search(query, limit=5)
runtime.describe(*capability_ids)
runtime.load(*capability_ids)
runtime.unload(*capability_ids)
runtime.list()

runtime.define(
    name,
    description,
    source,
    requires=[],
    tags=[],
)
runtime.call(name, **arguments)
runtime.disable(name)
runtime.delete(name)
```

加载后的 builtin 通过代理访问：

```python
tools.scene_info.find_objects(type="MESH", no_material=True)
tools.viewport.capture_objects(names, width=1024)
```

已定义的 runtime function 通过代理访问：

```python
functions.capture_unmaterialed_meshes(width=1280)
```

保留 `get_builtin()` 和 `get_builtin_doc()` 作为兼容接口，待新的 Runtime Context
稳定后再决定是否弃用。

## 渐进式能力披露

### 1. 搜索阶段

`runtime.search()` 只返回最小信息：

```json
{
  "matches": [
    {
      "id": "builtin.scene_info",
      "summary": "查询对象、层级、选择和材质状态",
      "score": 0.91
    },
    {
      "id": "builtin.viewport",
      "summary": "聚焦对象并截取当前 3D Viewport",
      "score": 0.87
    }
  ]
}
```

搜索范围包括：

- 项目内置 builtin；
- 当前 runtime 中的自定义函数；
- 未来安装的扩展能力。

第一版使用名称、`SUMMARY`、`TAGS` 和关键词匹配，不引入 embedding 或向量数据库。

### 2. 描述阶段

只有调用 `runtime.describe()` 或首次 `runtime.load()` 时才披露：

- 精确函数签名；
- 参数和结果结构；
- 一至两个规范示例；
- 是否修改场景；
- 依赖和常见错误。

`eval_python_code` 的固定 description 不再枚举全部 builtin SUMMARY，只说明如何
搜索、加载和使用 Runtime Context，避免 builtin 增长持续扩大常驻上下文。

### 3. 使用阶段

加载操作把能力挂入当前 runtime：

```python
__result__ = runtime.load(
    "builtin.scene_info",
    "builtin.viewport",
)
```

后续调用无需重复获取模块：

```python
names = tools.scene_info.find_objects(type="MESH", no_material=True)
image = tools.viewport.capture_objects(names, width=1024)
__result__ = tools.viewport.as_result(image, objects=names)
```

### 4. 卸载阶段

`runtime.unload()` 只进行逻辑卸载：

- 从 `tools` 可见空间移除；
- 从当前 runtime 的直接活跃能力中删除；
- 不再重复披露其文档。

不要频繁删除 `sys.modules` 或销毁模块对象，因为 runtime function 和其他代码可能
仍然持有依赖。函数执行时声明的依赖可以临时重新激活。

## Runtime Function

### 定义

Agent 在代码已经成功执行并确认可复用后，可以注册函数：

```python
__result__ = runtime.define(
    name="capture_unmaterialed_meshes",
    description="查找没有材质的 Mesh 并聚焦截图",
    tags=["mesh", "material", "viewport", "diagnostics"],
    requires=[
        "builtin.scene_info",
        "builtin.viewport",
    ],
    source="""
def run(width: int = 1024):
    names = tools.scene_info.find_objects(
        type="MESH",
        no_material=True,
    )
    if not names:
        return {
            "message": "所有 Mesh 均已有材质",
            "objects": [],
        }

    image = tools.viewport.capture_objects(names, width=width)
    return tools.viewport.as_result(
        image,
        message="缺少材质的对象",
        objects=names,
    )
""",
)
```

函数定义至少保存：

```python
@dataclass
class RuntimeFunction:
    function_id: str
    name: str
    version: int
    description: str
    tags: list[str]
    requires: list[str]
    source: str
    source_hash: str
    signature: str
    status: str
    success_count: int
    failure_count: int
    created_at: float
    last_used_at: float | None
```

### 依赖

所有函数必须显式声明 `requires`。调用时 runtime 自动加载依赖，不能依赖定义时
恰好存在的解释器状态：

```text
functions.capture_unmaterialed_meshes()
    │
    ├── 确认 scene_info 可用
    ├── 确认 viewport 可用
    ├── 在 Blender 主线程运行 run()
    └── 返回结构化结果
```

runtime function 不得保存 `bpy.types.Object` 等 RNA 对象引用；需要跨调用定位对象时
只保存名称、ID 或其他普通 JSON 数据，并在调用时重新查询。

### 生命周期

```text
draft → verified → active → persistent
           │          │
           └──────────┴──→ disabled
```

- `draft`：刚定义，只能显式调用。
- `verified`：至少成功执行一次。
- `active`：可以被 `runtime.search()` 自动发现。
- `persistent`：写入 Blender 用户配置目录，重启后仍存在。
- `disabled`：保留定义和统计，但禁止执行。

建议策略：

- 一次成功后进入 `verified`；
- 两至三次成功后可以自动进入 `active`；
- 连续失败两次自动禁用；
- 只有用户明确批准才能进入 `persistent`；
- 未持久化函数随 runtime 过期而释放。

不要将 runtime function 存入 `.blend`，避免污染项目、Undo、版本控制，以及打开陌生
文件时加载不可信代码。

## Agent 决策策略

固定说明中应明确要求 Agent 遵循以下优先级：

```text
已有 runtime function
        │ 没有
        ▼
已加载 builtin
        │ 没有
        ▼
runtime.search + runtime.describe/load
        │ 仍不够
        ▼
直接使用 bpy 探索
        │ 成功且可复用
        ▼
runtime.define
```

具体规则：

1. 优先检查已有 runtime function。
2. 其次复用当前已加载 builtin。
3. 不确定能力时先搜索和查看文档，不猜测 API。
4. 只加载当前任务所需的最少能力。
5. 裸 `bpy` 代码必须先成功执行，再考虑封装。
6. 不封装固定对象名、一次性场景状态或调试代码。
7. 不封装尚未验证或无法清楚描述副作用的代码。
8. 任务完成后可以卸载不再需要的直接 builtin。

满足以下任意两个条件时，Agent 可以自主创建 runtime function：

- 相似代码已成功运行两次；
- 代码超过约 15–20 行；
- 涉及容易出错的 `bpy.ops` context；
- 输入和输出可以清楚参数化；
- 当前任务很可能再次使用；
- 组合了两个以上 builtin；
- 已通过结构化结果或截图验证。

## Runtime 标识与结果 manifest

由于 Streamable HTTP 当前为 stateless 模式，跨调用必须显式传递 runtime ID：

```python
eval_python_code(
    code: str,
    runtime_id: str | None = None,
)
```

首次调用不传 ID 时创建 runtime，并在结果中返回：

```json
{
  "runtime_id": "rtx_a82f19",
  "result": {}
}
```

后续调用继续携带该 ID。每次结果附带紧凑 manifest，避免重复完整文档：

```json
{
  "_runtime": {
    "id": "rtx_a82f19",
    "revision": 7,
    "loaded": [
      "scene_info",
      "viewport"
    ],
    "functions": [
      "capture_unmaterialed_meshes(width=1024)"
    ]
  }
}
```

manifest 需要受严格大小限制，只包含名称、版本和签名。

## 验证与安全约束

Runtime Context 仍运行任意 Python，因此不是沙箱。验证的目标是接口确定性、资源
边界和可恢复性：

- 源码必须只定义一个入口函数 `run`；
- 禁止 decorator、`*args` 和 `**kwargs`；
- 参数名、类型和默认值必须可以生成确定的调用 schema；
- 默认值只能使用 JSON 可表达值；
- 限制函数名称、源码长度、参数数量和结果大小；
- 定义时进行 AST 和 compile 检查，但不自动产生场景修改；
- 所有调用继续经过 M1 FIFO、request ID、状态和超时机制；
- 运行结果不得返回 Blender RNA 对象；
- runtime function 不能覆盖系统名称、builtin 或其他版本；
- 所有定义、调用、禁用和持久化操作进入诊断记录；
- 持久化和高风险调用纳入 M2 用户确认与 checkpoint。

## 实施阶段

### R1：统一能力目录与渐进披露

- 新增 `CapabilityRegistry`。
- 为 builtin 增加 `TAGS`、副作用和结果类型元数据。
- 实现 `runtime.search()`、`describe()`、`load()`、`unload()` 和 `list()`。
- 实现 `tools` 代理。
- 从 MCP tool description 移除全量 builtin SUMMARY。
- 保留旧接口兼容测试。

退出标准：

- 增加任意数量 builtin 不会线性扩大常驻 MCP tool description。
- Agent 能通过搜索和描述找到正确 builtin，无需猜测函数名。
- 未加载 builtin 不向当前 runtime 披露完整文档。

### R2：显式 Runtime Context

- 新增 runtime ID、revision、过期和清理机制。
- 每次 eval 使用干净临时 namespace。
- 加载状态跨 eval 调用保留。
- 每次结果返回紧凑 runtime manifest。
- 多 runtime 相互隔离。

退出标准：

- 临时变量不会跨调用泄漏。
- loaded capabilities 能在同一 runtime 内稳定复用。
- 不同 runtime 的加载状态和函数互不污染。

### R3：Runtime Function

- 实现 `define()`、`call()`、`disable()` 和 `delete()`。
- 增加 AST、签名、依赖、版本和结果验证。
- 实现 `functions` 代理。
- 记录成功/失败统计，并按规则更新生命周期。

退出标准：

- Agent 可以将成功代码封装并在后续调用中只传参数复用。
- 卸载直接 builtin 后，runtime function 仍能按声明依赖正常运行。
- 函数不能保存或返回失效的 Blender RNA 引用。

### R4：持久化与晋升

- 经用户批准后将函数保存到 Blender 用户配置目录。
- 启动时只读取和验证定义，不自动执行函数代码。
- 支持导出、导入、禁用和版本升级。
- 建立从高频 runtime function 晋升为项目 builtin 的评审与测试流程。

退出标准：

- 重启 Blender 后，已批准函数可以恢复。
- 打开 `.blend` 文件不会隐式加载项目内代码。
- 持久化函数具备来源、版本、hash 和验证记录。

### R5：可选 MCP Tool 发布

只有出现明确客户端需求时才实现：

- 将成熟 runtime function 发布为带版本的 MCP tool；
- 使用 `add_tool()`/`remove_tool()`；
- 支持 `tools/list_changed`；
- 保留 `runtime.call()` 兼容路径；
- 验证主流 MCP 客户端的动态刷新行为。

此阶段不阻塞核心 Runtime Context 发布。

## 架构演进

```text
裸 bpy 探索代码
      │
      ▼
当前 runtime function
      │ 多次调用和验证
      ▼
持久化用户函数
      │ 高频、泛化、完成集成测试
      ▼
正式 builtin
      │ 可选
      ▼
稳定 MCP tool
```

项目的核心差异化由此变为：

> Agent 在受控、可观察的 Blender 解释器环境中按任务发现和组合能力，并把经过
> 验证的重复工作沉淀为可复用函数，而不是依赖不断膨胀的固定 MCP 工具列表。
