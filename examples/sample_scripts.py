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
    screenshot = get_builtin('screenshot')

    # 脚本端过滤，不污染上下文
    bare = scene_info.find_objects(type='MESH', no_material=True)
    if not bare:
        return "所有 Mesh 对象均已有材质"

    scene_info.focus_object(bare[0])
    img = screenshot.capture_viewport()
    return {
        "missing_material_count": len(bare),
        "objects": bare,
        "screenshot": img,   # 框架识别为图像
    }

__result__ = execute()
"""


# ── 示例 3：分析与 bpy 相关的错误日志 ─────────────────────────────────────────
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

if __name__ == "__main__":
    print("示例脚本列表：")
    print("  EXAMPLE_GOTHIC_TOWER  - 生成哥特塔楼")
    print("  EXAMPLE_FIND_BARE_MESH - 找无材质对象并截图")
    print("  EXAMPLE_FILTER_LOGS    - 过滤日志")
