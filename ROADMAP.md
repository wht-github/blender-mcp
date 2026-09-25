# Blender Agent Roadmap

更新日期：2026-09-25

## 产品定位

Blender Agent 是一个可信、本地优先的 Blender MCP 执行桥：外部 AI 客户端负责
对话、模型选择和 Agent 循环；插件负责把 MCP 请求可靠地调度到 Blender 主线程，
执行 `bpy` 脚本并返回结构化结果或截图。

当前优先保证执行对象正确、断线可恢复、修改范围明确，并完成真实事件循环验证。
Runtime Context 保留能力发现和按需加载；跨调用状态及 runtime function 先通过任务实验
证明收益，再扩大实现。完整设计见 [`docs/RUNTIME_CONTEXT.md`](docs/RUNTIME_CONTEXT.md)。

项目不计划重新内置 LLM 聊天客户端，也不把任意 Python 执行描述为安全沙箱。
安全边界来自可信客户端、受限网络、用户确认、可观察性和可恢复性。

## 当前基线：v0.3 Alpha

已完成：

- 使用官方 MCP Python SDK 的 Streamable HTTP，默认端点为 `/mcp`。
- MCP Server 在后台线程运行，`bpy` 调用通过 Blender 主线程执行。
- 单槽调度改为 FIFO 队列，并发请求不再互相覆盖。
- MCP、Uvicorn、Pydantic 和原生扩展随 Blender 安装包分发。
- Windows x64 / Blender 5.1 / Python 3.13 安装包构建通过。
- 已有协议级测试、队列测试和真实 Blender 5.1 端到端 smoke test。
- builtin 摘要通过 AST 读取，不再为了发现文档而执行模块。
- Runtime Context R1 已完成：支持能力搜索、精确描述、逻辑加载/卸载和 `tools`
  代理，MCP 常驻 description 不再枚举全部 builtin。
- builtin 搜索已细化到函数级操作 ID，并披露签名、副作用、UI context、成本和
  结果类型；核心 builtin 增加 Blender 5.1 行为集成测试。

当前仍属于 Alpha：任意 Python 具有完整 Blender 和本机权限，执行中的 Python
无法被安全强制中断，也尚未提供场景 checkpoint、操作确认和正式发布流程。

## 路线图总览

| 里程碑 | 建议周期 | 目标 | 发布判断 |
|---|---:|---|---|
| R：Runtime Context | 按实验结果 | 能力发现与最小复用原型 | R1 完成，R2–R4 由消融实验决定 |
| M1：执行可靠性 | 1 周 | 所有请求有明确状态、错误和容量边界 | 可供个人日常使用 |
| M2：安全与恢复 | 1–2 周 | 连接受控，场景修改可确认、可恢复 | 可邀请小范围测试 |
| M3：测试与兼容 | 1–2 周 | 真实 Blender 任务可重复验证 | 可发布 Beta |
| M4：正式打包发布 | 1 周 | 多平台包、manifest、许可证和发布自动化 | 可公开发布 |
| M5：能力扩展 | 持续 | 按真实失败数据增加高价值 builtins | 稳定迭代 |

## R：Runtime Context 与渐进式能力披露

优先级：P1；M1 执行边界、M2 恢复与 M3 验证优先

目标不是动态堆积顶层 MCP tools，而是在 `eval_python_code` 的 Blender Python
解释器环境内提供一个显式、可观察、可清理的 Runtime Context。

核心能力：

- `runtime.search/describe/load/unload/list`：按任务发现和加载最少 builtin。
- `tools.<name>`：直接调用当前 runtime 已加载的 builtin。
- 候选最小原型 `runtime.define/call/delete`：封装重复代码，显式启用；调用次数仅作统计。
- `functions.<name>`：使用参数调用已封装函数，不重复生成完整 Python。
- runtime ID、revision 和紧凑 manifest：在 stateless HTTP 调用之间显式延续状态。
- runtime function 显式声明 builtin 依赖，不保存 Blender RNA 对象引用。
- session 函数经过多次验证后可由用户批准持久化；高频稳定函数再晋升为正式 builtin。

实施顺序：

1. R1：统一能力目录与渐进披露（已完成，2026-07-16）。
2. R2：比较每次独立加载与跨调用状态，仅在收益明确时增加 runtime ID。
3. R3：比较直接 eval+builtin 与 define/call/delete 原型，不自动标记 verified 或晋升。
4. R4：持久化与 builtin 晋升延期，需正确性证据和明确用户需求。
5. R5：仅在明确需要时发布为动态 MCP tool。

退出标准：

- builtin 数量增长不再线性扩大 `eval_python_code` 的常驻 description。
- Agent 可以先搜索、再披露精确文档、按需加载能力，不需要猜测 API。
- 临时变量不会跨 eval 泄漏，加载状态和函数可以在显式 runtime 内复用。
- Agent 可以把成功代码封装成带参数和依赖的函数，并在后续任务中直接调用。
- 不同 runtime 相互隔离；runtime 过期和卸载不会破坏其他执行。
- 未经用户批准的函数不会跨 Blender 重启持久化。

## M1：执行可靠性

优先级：P0

目标是把“能执行”提升为“每次执行都能解释发生了什么”。

任务：

- 为任务增加 `queued / running / succeeded / failed / timed_out / cancelled` 状态。
- 超时区分“仍在队列”和“已经在 Blender 主线程执行”。
- 设置最大队列长度、最大代码长度和最大文本/图片返回大小。
- 为每次请求生成 request ID，记录排队时间、执行时间和结果类型。
- 统一异常结构，避免合法 `None` 与失败结果混淆。
- 完善启动失败回滚、端口占用提示和停止服务时的在途请求处理。
- 明确取消语义：可以取消排队任务，但不承诺强制终止正在执行的 Python。
- Blender 面板只显示当前任务、队列长度和最近一次结果；完整历史保留在运行时诊断
  数据中，不把 Sidebar 扩展为日志浏览器。

退出标准：

- 100 次并发/排队测试无请求丢失或错配。
- 所有超时和停止场景都返回结构化错误。
- 服务启动失败后不残留 timer、线程或被占用端口。
- M1 相关自动化测试必须连续完整通过 3 次，才视为达到本阶段退出标准。

当前实现（2026-07-13）：

- 已加入完整任务状态、request ID、排队/执行耗时和最近 50 条内存历史。
- 已区分排队超时与执行超时；前者不再执行，后者明确提示底层 Python 仍会继续。
- 队列上限为 32，代码上限为 256 KiB，文本结果上限为 1 MiB，图片结果上限为 10 MiB。
- 停止服务时取消排队任务；端口占用在启动前返回明确错误。
- 当前代码已经具备最近任务历史数据与临时面板展示；实现 Runtime Context 时将
  Sidebar 收敛为当前任务、队列和最近结果，完整历史继续供诊断和测试使用。
- 100 请求并发/容量边界测试无结果丢失或错配；10 项 M1 自动化测试连续通过 3 次，
  并通过 Blender 5.1 真实 Streamable HTTP smoke test。

M1 可靠性补充（2026-09-12）：

- HTTP 调用直接提交有界队列并异步等待，移除默认线程池中的隐藏排队；执行前再次
  检查请求截止时间，避免等待协程尚未恢复时执行过期任务。
- 主线程校验并复制返回值，拒绝 RNA/自定义对象、循环引用及超限结果。
- HTTP 断开和协程取消会取消排队任务；运行中的 Python 继续记录实际完成状态。
- 新增独立 `get_execution_status` 工具，查询不会被场景执行队列阻塞；迟到结果使用
  有过期时间及容量限制的缓存，任务历史只保存小型状态快照。
- 用户脚本 `SystemExit` 等异常被转换为执行错误；文档提取失败保留脚本实际结果，
  队列收尾通过 `finally` 保证。停服时先关闭入口并取消队列，再等待 HTTP 退出。
- 验证：40 项自动化测试连续通过 3 次（含真实 HTTP 并发、断开连接和停服），
  并通过 Blender 5.1.0 打包后 MCP smoke 与 headless builtin 行为测试。
  此轮未重跑交互式 VIEW_3D 截图成功路径。

设计边界修正（2026-09-25）：

- 任务绑定文档代次，文件加载前取消旧队列，加载失败后也恢复入口。
- 客户端可预先指定请求 ID；保留窗口内去重、冲突检测和过期保护，无持久化承诺。
- 业务数据与执行错误分离，MCP 返回固定结构化字段。
- 每次 eval 独立加载状态；模块缓存共享，文档显式查询，不保存全局“已读”状态。
- 共享材质数据默认拒绝修改，显式选择复制或共享作用域。
- 打包复用依赖核对版本、平台和锁定依赖摘要；已验证组合收敛到 Blender 5.1 / Windows x64。
- 增加真实事件循环、插件重启、文件切换和材质作用域测试；移除执行 timer 时事件循环测试必须失败。
- 验证：60 项自动化测试连续三轮通过；Blender 5.1 打包 HTTP、builtin、文件加载与真实事件循环测试通过；定时器消融按预期失败，材质作用域 9 项通过。

后续实验用同一组 golden tasks 比较固定能力索引与搜索流程、显式文档与隐式去重、
直接 eval 与注册函数，记录成功率、修改范围、修复轮数、token 与端到端耗时。
没有收益证据的状态管理和自动晋升机制不进入默认实现。

## M2：安全、确认与恢复

优先级：P0

任意 `bpy` Python 无法在同一 Blender 进程中变成可靠沙箱，因此本阶段不尝试用
关键字过滤制造虚假安全感，而是建立真实的连接边界和恢复机制。

任务：

- 普通模式固定监听 `127.0.0.1:8400`；局域网监听放入显式高级设置并显示风险警告。
- Host、Port 和自动启动选项保存在 Blender 用户级 Add-on Preferences 中，不随
  `.blend` 文件变化；默认自动启动，使 MCP 客户端可以长期复用固定 URL。
- bearer token 在首次启用时生成并持久化，后续启动复用；提供显式轮换 token 和
  复制客户端配置入口，不要求用户每次启动 Blender 都重新填写连接参数。
- MCP 客户端采用一次性用户级配置：Streamable HTTP URL 固定为
  `http://127.0.0.1:8400/mcp`，认证信息由客户端持久保存。
- 端口占用时明确报错，不静默切换端口；Alpha 阶段默认只支持一个活动 Blender
  MCP 实例，多实例路由在出现真实需求后另行设计。
- 扩充 Host/Origin/DNS rebinding 测试。
- 面板显示当前或最近请求的来源、短摘要、执行状态和耗时。
- 为修改型执行提供“执行前确认”模式。
- 执行前创建 Blender undo checkpoint；高风险模式可创建临时 `.blend` 副本。
- 提供“一键撤销上次 Agent 操作”和打开 checkpoint 的入口。
- 明确提示：客户端超时不代表已经运行的 Python 被停止。

关于只读模式：在暴露完整 `bpy` 的情况下无法可靠判断脚本只读。若未来需要强保证，
应新增只暴露查询 builtin 的独立只读工具，而不是静态分析任意 Python。

退出标准：

- 默认配置不能从非本机网络访问。
- 未授权请求无法调用 tool。
- 启用确认模式时，场景修改必须由 Blender 用户批准。
- 典型创建、删除和材质修改操作均可通过 checkpoint 恢复。

## M3：测试、兼容性和成功率

优先级：P1

建立约 20 个 golden tasks，覆盖：

- 查询层级、对象属性和缺失材质。
- 创建/删除对象、批量修改 transform。
- 创建和分配材质、按面设置材质。
- 聚焦对象、截图、渲染截图。
- 空场景、大场景、名称冲突、隐藏对象和无 3D Viewport 等边界情况。
- 语法错误、`bpy.ops` context 错误、超时、并发和撤销。

测试层次：

1. 不依赖 Blender 的协议、队列和结果格式单元测试。
2. Blender `--background` 集成测试。
3. 需要真实 UI context 的交互式 smoke test。
4. 固定 `.blend` 样例上的 golden result/screenshot 测试。

兼容矩阵至少包含：

- 一个 Blender LTS 版本。
- 当前稳定 Blender 版本。
- Windows x64；随后扩展到 macOS Apple Silicon 和 Linux x64。

退出标准：

- golden tasks 成功率不低于 95%。
- 支持矩阵中的 headless smoke test 全部通过。
- 不允许未经记录的 Blender/Python 版本进入发布包。

## M4：正式打包与公开发布

优先级：P1

任务：

- 增加 `blender_manifest.toml`，迁移到 Blender Extension 包结构。
- 使用单一版本源同步插件、Python 项目和发布产物版本。
- 为 Windows、macOS 和 Linux 分别构建包含对应原生 wheel 的 zip。
- 增加 LICENSE、第三方许可证清单、CHANGELOG 和基本贡献指南。
- 自动执行依赖锁定、包构建、zip 内容检查和 Blender smoke test。
- 为产物生成校验和，并在文件名中包含版本、Blender Python ABI 和平台。
- 文档提供主流 MCP 客户端的 Streamable HTTP 配置示例。

退出标准：

- 全新 Blender 环境可以只安装 zip，不运行 pip 即完成启动和调用。
- 每个发布产物都能追溯到锁文件、平台、Python ABI 和测试结果。
- Blender Extension 包能够通过官方 validate 命令。

## M5：能力扩展

优先级：P2

在 M1–M3 完成前暂停大规模新增 builtin。之后根据 golden tasks 和真实失败日志决定
扩展顺序，优先解决高频且模型容易写错的 `bpy` 工作流。

候选方向：

- `geometry_nodes`：节点组创建、socket 设置和 modifier 绑定。
- `uv`：UV 层查询、展开、打包和截图检查。
- `render`：相机、灯光、渲染配置和低成本预览。
- `animation`：关键帧、Action/NLA 查询和基础操作。
- `assets`：本地资产查找、追加、链接和实例化。
- `diagnostics`：context、依赖图、缺失文件和性能诊断。

每个新 builtin 必须同时具备：

- 稳定、精确的 DESCRIPTION 和示例。
- 参数校验和结构化错误。
- 至少一个真实 Blender 集成测试。
- 清晰的状态修改与恢复说明。

## 关键指标

- tool 调用成功率与 golden task 成功率。
- 队列等待时间、执行时间和端到端延迟的 P50/P95。
- 丢失请求、错误匹配请求和无法解释的 `None` 数量必须为零。
- 超时后仍继续执行的任务数量和持续时间。
- 每个版本在支持矩阵中的 smoke test 通过率。
- builtin 首次调用成功率，以及因 API 猜测导致的重试次数。

## 近期执行顺序

1. 完成 R2：显式 runtime ID、干净执行上下文、manifest、过期和隔离。
2. 完成 R3：runtime function 的定义、调用、依赖和生命周期。
3. 在现有 M1 可靠性基础上补齐 Runtime Context 的隔离、容量和失败测试。
4. 增加 token、确认模式和 checkpoint/undo，并用于函数持久化审批。
5. 建立 20 个 golden tasks，验证“搜索 → 加载 → 执行 → 封装 → 复用”完整链路。
6. 再进行 Extension manifest、多平台打包和公开发布。
7. 最后依据 runtime function 使用数据和失败数据增加正式 builtin。
