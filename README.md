# Blender Agent — eval + builtins 架构

LLM 生成 Python 脚本 → Blender 内置解释器执行 → bpy API 直接操控场景。

## 架构概览

```
blender_agent/
├── __init__.py          # Blender Addon 注册入口
├── agent_panel.py       # Blender UI 面板
├── eval_core.py         # Python 代码执行引擎（核心）
├── agent_loop.py        # LLM Agent 对话循环
├── builtin_loader.py    # Builtins 发现 & 懒加载管理器
└── builtins/
    ├── screenshot.py    # 截取 Viewport / Render 画面
    ├── viewport.py      # 聚焦选中零件并截图
    ├── scene_info.py    # 场景层级 / 对象信息查询
    ├── materials.py     # 创建 / 复用材质并分配到对象
    └── blender_log.py   # 获取 Blender 系统日志
```

## 核心设计

### 唯一 Tool：`eval_python_code`

LLM 只有一个工具：提交一段 Python 代码。Agent 在 Blender 主线程中执行它，
返回 `__result__` 变量的值。

```python
# LLM 生成的典型脚本
def execute():
    scene_info = get_builtin('scene_info')
    viewport = get_builtin('viewport')

    # 找出所有没有材质的 Mesh 对象
    bare = scene_info.find_objects(type='MESH', no_material=True)

    if not bare:
        return "所有 Mesh 对象均已有材质"

    # 在同一个 3D 视口里聚焦并截图，避免焦点和截图落在不同窗口
    img = viewport.capture_objects([bare[0]], width=1024)
    return viewport.as_result(img, message='缺少材质对象截图', missing_material=bare)

__result__ = execute()
```

### 新增 builtin：`materials`

适合把高频、脆弱的材质样板代码收敛到一个薄层 builtin：

```python
# LLM 可直接调用的典型脚本
def execute():
    materials = get_builtin('materials')

    materials.create_preset(
        'Truck_Body_Red',
        'painted_metal',
        overrides={'base_color': (0.9, 0.15, 0.05, 1.0), 'metallic': 0.3, 'roughness': 0.3},
    )
    materials.apply_material_to_objects('Truck_Body_Red', ['Car body', 'door-left', 'door-right'])

    materials.create_preset('Truck_Glass', 'glass')
    materials.assign_faces_by_index('Car body', [12, 13, 14], material_name='Truck_Glass')

    return materials.get_material_info('Truck_Body_Red')

__result__ = execute()
```

### 新增 builtin：`viewport`

适合把“选中对象 → 视口构图 → 截图”收敛到一个薄层 builtin：

```python
def execute():
    viewport = get_builtin('viewport')

    img = viewport.capture_selection(
        width=1280,
        height=720,
    )
    return viewport.as_result(
        img,
        message='当前选中零件截图',
        selected=viewport.get_selected_objects(),
    )

__result__ = execute()
```

### Builtins 两级文档

每个 builtin 模块顶部定义：

```python
SUMMARY = "截取 Viewport / Render 画面，返回 base64 图像"   # 始终在上下文
DESCRIPTION = """                                              # 按需加载
capture_viewport(area_type='VIEW_3D', width=None, height=None) -> str
capture_render(frame=None, width=None, height=None) -> str
...
"""
```

框架启动时收集所有 `SUMMARY`，注入到 `eval_python_code` 的 tool description 中。
LLM 按需调用 `get_builtin('xxx')` 触发完整 `DESCRIPTION` 的加载（追加到下一轮上下文）。

## 安装

1. 将 `blender_agent/` 目录打包为 `.zip`
2. Blender → Edit → Preferences → Add-ons → Install
3. 在 Addon 设置中填入 API Key（支持 OpenAI / Anthropic / 本地模型）
4. 3D Viewport 侧边栏 → "AI Agent" 面板开始对话

## 对比传统 tools 模式

| 场景 | tools 模式 | eval + builtins |
|------|-----------|-----------------|
| 遍历场景找问题对象 | 场景树 JSON 全进上下文 | 脚本内遍历，只返回结果 |
| 过滤日志 | 所有日志返回 LLM | 脚本端过滤，只返回相关条目 |
| 多步骤操作 | N 次往返 | 1 次往返 |
| 上下文占用 | 随 tools 数量线性增长 | 仅 summaries（每条 1 行）|
