# Runtime Context 与渐进式能力披露

状态：R1 已实现；R2 及以后先做收益实验，暂不扩展状态机
更新日期：2026-09-25

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
       └── 可复用函数原型（待实验）
```

Agent 应优先复用已有能力，只在缺少合适能力时使用裸 `bpy` 探索。成功且可泛化的
探索代码是否值得封装为跨调用函数，需要用真实任务验证代码生成、上下文消耗和
修复次数的收益。当前实现不保存跨 eval 的函数或加载状态。

## 产品边界

- MCP 场景执行入口保持为 `eval_python_code`；`get_execution_status` 是独立的状态
  查询工具，不经过执行队列，也不参与动态能力扩展。
- 动态加载、卸载和函数封装发生在解释器 Runtime Context 内部。
- builtin 是经过项目维护和测试的正式能力。
- runtime function 是 Agent 在执行过程中创建的可复用组合能力。
- 执行成功次数只是统计，不能证明几何结果正确或函数可泛化。正式 builtin 仍需
  独立的行为验证和代码评审。
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

## 当前状态边界与后续模型

每次 eval 创建独立的 Runtime Context 和临时命名空间。`runtime.load()` 和
`runtime.unload()` 只改变这次执行的 `tools` 可见范围；下一次执行必须重新 load。
`BuiltinLoader` 只管理静态能力目录与进程内模块缓存，不保存客户端、文档已读或
执行记录。重复 load 复用模块对象，避免重复导入，但不复用逻辑加载状态。

只有收益实验通过后，才考虑为跨调用函数增加显式 `runtime_id`。不能依赖 HTTP
连接代表会话，也不能仅添加 ID 而继续共享函数或加载状态。以下是待验证的模型：

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

R1 已实现前五个能力管理接口。`define/call/delete` 仅为后续实验原型：

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
    enabled=False,
)
runtime.call(name, **arguments)
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
      "id": "builtin.scene_info.find_objects",
      "load_id": "builtin.scene_info",
      "summary": "在当前 View Layer 中按类型、材质和修改器状态筛选对象",
      "score": 0.91
    },
    {
      "id": "builtin.viewport.capture_objects",
      "load_id": "builtin.viewport",
      "summary": "聚焦对象并截图，随后恢复选择、可见性和视口构图",
      "score": 0.87
    }
  ]
}
```

当前搜索范围为项目内置 builtin。后续有明确需求时再扩展：

- 当前 runtime 中的自定义函数；
- 未来安装的扩展能力。

使用名称、`SUMMARY`、`TAGS` 和关键词匹配，不引入 embedding 或向量数据库。
中文查询使用相邻双字片段和少量常见词映射；操作自身的标签比继承的模块标签
权重更高。模块和模块内操作均可参与搜索；结果只包含 `id`、`load_id`、
`summary`、`score`，签名和副作用由 describe 按需返回。

### 2. 描述阶段

文档只通过显式查询返回，不记录“已经读过”的状态：

- `runtime.describe('builtin.module.operation')` 返回完整签名、摘要、副作用、
  UI context 要求、成本和结果类型。
- `runtime.describe('builtin.module')` 返回完整模块文档，包括参数、示例和限制。
- 重复 describe 始终可用；描述一个操作不会影响之后读取模块文档。
- `runtime.load()` 只激活模块，不自动附加文档。

`eval_python_code` 的固定 description 不再枚举全部 builtin SUMMARY，只说明如何
搜索、加载和使用 Runtime Context，避免 builtin 增长持续扩大常驻上下文。

### 3. 使用阶段

加载与使用发生在同一次 eval：

```python
runtime.load(
    "builtin.scene_info",
    "builtin.viewport",
)
names = tools.scene_info.find_objects(type="MESH", no_material=True)
image = tools.viewport.capture_objects(names, width=1024)
__result__ = tools.viewport.as_result(image, objects=names)
```

下一次 eval 如需继续使用这些能力，应再次调用 `runtime.load()`。模块缓存会复用，
但上一轮或另一客户端的 load/unload 不会改变本轮可见能力。

### 4. 卸载阶段

`runtime.unload()` 只进行逻辑卸载：

- 从 `tools` 可见空间移除；
- 从本次执行的直接活跃能力中删除。

不要频繁删除 `sys.modules` 或销毁模块对象，因为 runtime function 和其他代码可能
仍然持有依赖。函数执行时声明的依赖可以临时重新激活。

## Runtime Function 原型（未实现）

### 定义

实验阶段仅提供 `define/call/delete`，定义时通过 `enabled=True` 显式启用。
不会根据成功或失败次数自动启用、禁用或晋升。以下示例为拟议接口：

```python
__result__ = runtime.define(
    name="capture_unmaterialed_meshes",
    description="查找没有材质的 Mesh 并聚焦截图",
    enabled=True,
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
    enabled: bool
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

### 启用与验证

函数只有显式启用后才能调用。成功/失败次数和最后一次错误用于诊断，不产生
`verified` 或 `active` 之类的正确性承诺，也不在连续失败两次后自动禁用。
未抛异常可能仍生成错误几何；场景条件不满足也可能使正确函数失败。

验证必须针对函数承诺的结果，例如对象属性、几何约束或截图，并覆盖不同场景。
持久化、跨客户端发现和晋升单独延期；实验函数随所属 runtime 清理。

不要将 runtime function 存入 `.blend`，避免污染项目、Undo、版本控制，以及打开陌生
文件时加载不可信代码。

## Agent 决策策略（函数原型阶段）

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

是否注册函数由重复任务中的实际收益决定，以下仅作为判断线索，不以执行次数或
代码行数作为自动门槛：

- 涉及容易出错的 `bpy.ops` context；
- 输入和输出可以清楚参数化；
- 当前任务很可能再次使用；
- 已通过结构化结果或截图验证。

## Runtime 标识与结果 manifest（待收益实验）

当前每次 eval 独立，没有 runtime ID。若实验支持引入跨调用状态，必须显式传递
runtime ID，并将函数、权限范围和过期清理归属于该 ID：

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
- 所有定义、调用和删除操作进入诊断记录；
- 连接认证、恢复和端到端验证先于持久化扩展。

## 实施阶段

### R1：统一能力目录与渐进披露

状态：已完成（2026-07-16）

- 新增 `CapabilityRegistry`。
- 为 builtin 增加 `TAGS`、副作用和结果类型元数据。
- 实现 `runtime.search()`、`describe()`、`load()`、`unload()` 和 `list()`。
- 实现 `tools` 代理。
- 从 MCP tool description 移除全量 builtin SUMMARY。
- 保留旧接口兼容测试。

退出标准：

- 增加任意数量 builtin 不会线性扩大常驻 MCP tool description。
- Agent 能通过搜索和描述找到正确 builtin，无需猜测函数名。
- 完整文档只由显式 describe 获取，不受其他调用或客户端的读取历史影响。

当前实现：

- `BuiltinLoader` 通过 AST 读取 `SUMMARY`、`TAGS`、`SIDE_EFFECTS`、
  `RESULT_TYPES`、`OPERATIONS` 和 `DESCRIPTION`，搜索和描述阶段都不会导入
  builtin。
- `runtime.search()` 可以返回 `builtin.module.operation`；单操作描述只披露签名、
  副作用、UI context 要求、成本和结果类型，`runtime.load()` 仍按模块激活。
- eval 命名空间注入 `runtime` 与 `tools`；只有经过 `runtime.load()` 激活的
  builtin 才能通过 `tools.<name>` 访问。
- `runtime.unload()` 只从当前逻辑可见空间移除能力，不删除 `sys.modules`。
- `eval_python_code` 的固定 description 只说明发现协议，不再枚举 builtin。
- `get_builtin()` 与 `get_builtin_doc()` 继续兼容，并接入同一 Runtime Context。
- 每次 eval 新建 Runtime Context；加载状态只属于当前执行，模块缓存属于进程。
- 去除 loader 中的文档已读和本次加载记录，load 不再自动附加文档。
- 自动化测试覆盖中文真实查询、操作签名、重复文档读取和调用间加载隔离。

### 进入 R2 前：消融实验

用同一组真实 Blender 任务比较，记录成功率、总 token、调用轮数和修复次数：

| 对照 | 要回答的问题 |
| --- | --- |
| 简短能力索引 / search + describe + load | 能力发现是否提高成功率并减少总成本？ |
| 显式文档与每次独立加载 / 隐式已读与跨调用加载 | 节省的上下文是否足以抵偿会话状态复杂度？ |
| eval + builtin / define + call + delete 原型 | 函数复用是否减少重复代码和修复次数？ |

先完成连接认证、请求恢复及真实 Blender 事件循环端到端验证，再进行函数原型。
实验没有明确收益就保留当前较小的接口，不按预定阶段强行增加状态管理。

### R2：显式 Runtime Context

状态：延期，只有跨调用函数收益实验通过后才实施。

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

- 先实现 `define()`、`call()` 和 `delete()` 原型，定义时显式启用。
- 增加 AST、签名、依赖、版本和结果验证。
- 原型只通过 `runtime.call()` 调用；`functions` 代理待收益明确后再考虑。
- 成功/失败只记录统计；不自动验证、启用、禁用或晋升。

退出标准：

- Agent 可以将成功代码封装并在后续调用中只传参数复用。
- 卸载直接 builtin 后，runtime function 仍能按声明依赖正常运行。
- 函数不能保存或返回失效的 Blender RNA 引用。

### R4：持久化与晋升

状态：延期，需原型收益、场景行为验证和明确使用需求，不由调用次数触发。

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
