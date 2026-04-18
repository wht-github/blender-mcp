"""
示例：用几个典型的 LLM 生成脚本，展示 eval + builtins 架构的效果。

这些脚本模拟 LLM 会生成的代码，可以直接在 Blender Python 控制台测试。
"""

# ── 示例 1：用纯代码建一座哥特式教堂塔楼 ─────────────────────────────────────
EXAMPLE_GOTHIC_TOWER = """
import bpy
import math

def execute():
    # 清空场景
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete()

    # 主塔体
    bpy.ops.mesh.primitive_cylinder_add(radius=2, depth=12, location=(0, 0, 6))
    tower = bpy.context.active_object
    tower.name = "GothicTower"

    # 材质
    mat = bpy.data.materials.new("Stone")
    mat.use_nodes = True
    bsdf = mat.node_tree.nodes["Principled BSDF"]
    bsdf.inputs["Base Color"].default_value = (0.6, 0.55, 0.5, 1)
    bsdf.inputs["Roughness"].default_value = 0.9
    tower.data.materials.append(mat)

    # 顶部尖顶（锥体）
    bpy.ops.mesh.primitive_cone_add(radius1=2.2, radius2=0, depth=4, location=(0, 0, 14))
    spire = bpy.context.active_object
    spire.name = "Spire"
    spire.data.materials.append(mat)

    # 4 个小圆塔
    for i in range(4):
        angle = i * math.pi / 2
        x = 2.8 * math.cos(angle)
        y = 2.8 * math.sin(angle)
        bpy.ops.mesh.primitive_cylinder_add(radius=0.6, depth=8, location=(x, y, 4))
        turret = bpy.context.active_object
        turret.name = f"Turret_{i}"
        turret.data.materials.append(mat)

        bpy.ops.mesh.primitive_cone_add(radius1=0.7, radius2=0, depth=1.5, location=(x, y, 9.5))
        tc = bpy.context.active_object
        tc.data.materials.append(mat)

    # 灯光
    bpy.ops.object.light_add(type='SUN', location=(5, -5, 15))
    bpy.context.active_object.data.energy = 3

    # 摄像机
    bpy.ops.object.camera_add(location=(10, -10, 8))
    cam = bpy.context.active_object
    cam.rotation_euler = (1.1, 0, 0.785)
    bpy.context.scene.camera = cam

    return {"message": "教堂塔楼构建完成", "objects": [o.name for o in bpy.data.objects]}

__result__ = execute()
"""


# ── 示例 2：找出缺少材质的 Mesh 并截图 ────────────────────────────────────────
EXAMPLE_FIND_BARE_MESH = """
def execute():
    scene_info = get_builtin('scene_info')
    viewport = get_builtin('viewport')

    # 脚本端过滤，不污染上下文
    bare = scene_info.find_objects(type='MESH', no_material=True)
    if not bare:
        return "所有 Mesh 对象均已有材质"

    img = viewport.capture_objects([bare[0]], width=1024)
    return viewport.as_result(
        img,
        message='缺少材质对象截图',
        missing_material_count=len(bare),
        objects=bare,
    )

__result__ = execute()
"""


# ── 示例 3：聚焦当前选中零件并截图 ─────────────────────────────────────────────
EXAMPLE_CAPTURE_SELECTED_PARTS = """
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
"""


# ── 示例 4：分析与 bpy 相关的错误日志 ─────────────────────────────────────────
EXAMPLE_FILTER_LOGS = """
def execute():
    log = get_builtin('blender_log')
    logs = log.get_info_log(limit=100)

    # 脚本端过滤，避免 100 条全进上下文
    relevant = [l for l in logs
                if any(kw in l.get('message', '').lower()
                       for kw in ['error', 'warning', 'missing'])]
    return {
        "total": len(logs),
        "relevant_count": len(relevant),
        "relevant": relevant[:10],   # 最多返回 10 条
    }

__result__ = execute()
"""



# ── 示例 5：用 materials builtin 创建并分配材质 ─────────────────────────────────
EXAMPLE_ASSIGN_MATERIALS = """
def execute():
    materials = get_builtin('materials')

    materials.create_preset(
        'Demo_Body_Red',
        'painted_metal',
        overrides={'base_color': (0.9, 0.15, 0.05, 1.0), 'metallic': 0.3, 'roughness': 0.3},
    )
    materials.create_preset('Demo_Glass', 'glass')
    materials.create_preset('Demo_Rubber', 'rubber')

    materials.apply_material_to_objects('Demo_Body_Red', ['Car body', 'door-left', 'door-right'])
    materials.apply_material_to_objects(
        'Demo_Rubber',
        [
            'wheel-front-right', 'wheel-front-left',
            'wheel-front-right.001', 'wheel-front-left.001',
            'wheel-back-right.001', 'wheel-back-left.001',
        ],
    )

    return {
        'body': materials.get_material_info('Demo_Body_Red'),
        'glass': materials.get_material_info('Demo_Glass'),
        'rubber': materials.get_material_info('Demo_Rubber'),
    }

__result__ = execute()
"""

if __name__ == "__main__":
    print("示例脚本列表：")
    print("  EXAMPLE_GOTHIC_TOWER  - 生成哥特塔楼")
    print("  EXAMPLE_FIND_BARE_MESH - 找无材质对象并截图")
    print("  EXAMPLE_CAPTURE_SELECTED_PARTS - 截图当前选中零件")
    print("  EXAMPLE_FILTER_LOGS    - 过滤日志")
    print("  EXAMPLE_ASSIGN_MATERIALS - 创建并分配材质")
