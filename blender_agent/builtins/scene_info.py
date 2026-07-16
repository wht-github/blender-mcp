"""
scene_info.py — 场景层级、对象属性与选择操作

提供场景树查询、对象聚焦、选择等高层操作，
避免 LLM 将整棵场景树写入上下文后再处理。
"""

import bpy
import json
from typing import Optional

SUMMARY = "查询场景层级/对象属性、聚焦并选中对象、保存场景等操作"
TAGS = ["scene", "object", "hierarchy", "selection", "save", "mesh", "material", "场景", "对象", "层级"]
SIDE_EFFECTS = "mixed"
RESULT_TYPES = ["object", "array", "text"]

DESCRIPTION = """
scene_info builtin — 场景信息与操作

查询函数：
  get_hierarchy(collection_name='') -> dict
    返回场景对象树。collection_name 为空时返回全场景。
    结构：{"name": str, "type": str, "components": [...], "children": [...]}
    components 包含：材质、修改器、约束等摘要。

  get_object_info(name: str) -> dict
    返回指定对象的详细属性（位置、旋转、缩放、材质、修改器列表等）。

  find_objects(type=None, no_material=False, no_modifier=False) -> list[str]
    按条件筛选对象名称列表，在脚本端过滤，不污染上下文。
    type: 'MESH'|'LIGHT'|'CAMERA'|'CURVE'|'ARMATURE' 等（None=不限）

操作函数：
    focus_object(name: str) -> bool
        选中并在 3D 视口中聚焦指定对象（等效于 Numpad '.'）。

  select_objects(names: list[str], deselect_all=True)
    选中指定对象列表。

  save_scene() -> str
    保存当前 .blend 文件，返回保存路径。

示例：
  scene_info = get_builtin('scene_info')
  screenshot = get_builtin('screenshot')

  # 找出所有缺少材质的 Mesh，聚焦第一个并截图
  bare = scene_info.find_objects(type='MESH', no_material=True)
  if bare:
      scene_info.focus_object(bare[0])
      img = screenshot.capture_viewport()
      return {"missing_material": bare, "screenshot": img}
  return "所有 Mesh 对象均已有材质"
"""


def get_hierarchy(collection_name: str = "") -> dict:
    """返回场景或指定 Collection 的对象树。"""
    if collection_name:
        coll = bpy.data.collections.get(collection_name)
        objects = list(coll.objects) if coll else []
    else:
        assert bpy.context.scene is not None
        objects = list(bpy.context.scene.objects)
    def obj_to_node(obj):
        node = {
            "name": obj.name,
            "type": obj.type,
            "location": list(obj.location),
            "visible": obj.visible_get(),
            "components": [],
        }
        if obj.type == "MESH" and obj.data:
            node["components"].append({
                "kind": "mesh",
                "vertices": len(obj.data.vertices),
                "polygons": len(obj.data.polygons),
            })
            for mat_slot in obj.material_slots:
                if mat_slot.material:
                    node["components"].append({"kind": "material", "name": mat_slot.material.name})
        for mod in obj.modifiers:
            node["components"].append({"kind": "modifier", "name": mod.name, "type": mod.type})
        # 子对象
        children = [o for o in objects if o.parent == obj]
        node["children"] = [obj_to_node(c) for c in children]
        return node

    roots = [o for o in objects if o.parent is None or o.parent not in objects]
    return {"hierarchy": [obj_to_node(r) for r in roots]}


def get_object_info(name: str) -> dict:
    """返回指定对象的详细属性。"""
    obj = bpy.data.objects.get(name)
    if obj is None:
        return {"error": f"Object '{name}' not found"}

    info = {
        "name": obj.name,
        "type": obj.type,
        "location": list(obj.location),
        "rotation_euler": list(obj.rotation_euler),
        "scale": list(obj.scale),
        "materials": [s.material.name for s in obj.material_slots if s.material],
        "modifiers": [{"name": m.name, "type": m.type} for m in obj.modifiers],
        "constraints": [{"name": c.name, "type": c.type} for c in obj.constraints],
    }
    if obj.type == "MESH" and obj.data:
        info["mesh"] = {
            "vertices": len(obj.data.vertices),
            "edges": len(obj.data.edges),
            "polygons": len(obj.data.polygons),
        }
    return info


def find_objects(
    type: Optional[str] = None,
    no_material: bool = False,
    no_modifier: bool = False,
) -> list:
    """在 Python 端过滤对象，返回名称列表，不污染上下文。"""
    results = []
    assert bpy.context.scene is not None
    for obj in bpy.context.scene.objects:
        if type and obj.type != type:
            continue
        if no_material and obj.type == "MESH":
            if any(s.material for s in obj.material_slots):
                continue
        if no_modifier and obj.modifiers:
            continue
        results.append(obj.name)
    return results


def focus_object(name: str) -> bool:
    """选中并在 3D 视口聚焦指定对象。"""
    obj = bpy.data.objects.get(name)
    if obj is None:
        return False
    # 先确保可见
    obj.hide_set(False)
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    assert bpy.context.view_layer is not None
    bpy.context.view_layer.objects.active = obj
    # 让 3D 视口聚焦
    assert bpy.context.screen is not None
    for area in bpy.context.screen.areas:
        if area.type == "VIEW_3D":
            with bpy.context.temp_override(area=area):
                bpy.ops.view3d.view_selected()
            break
    return True


def select_objects(names: list, deselect_all: bool = True):
    """选中指定对象列表。"""
    if deselect_all:
        bpy.ops.object.select_all(action="DESELECT")
    for name in names:
        obj = bpy.data.objects.get(name)
        if obj:
            obj.select_set(True)
    if names:
        last = bpy.data.objects.get(names[-1])
        if last:
            assert bpy.context.view_layer is not None
            bpy.context.view_layer.objects.active = last


def save_scene() -> str:
    """保存当前 .blend 文件。"""
    path = bpy.data.filepath
    if not path:
        return "文件尚未保存过，请先手动另存为"
    bpy.ops.wm.save_mainfile()
    return f"已保存：{path}"
