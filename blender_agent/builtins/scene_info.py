"""
scene_info.py — 场景层级、对象属性与选择操作

提供场景树查询、对象聚焦、选择等高层操作，
避免 LLM 将整棵场景树写入上下文后再处理。
"""

import bpy
from typing import Optional

SUMMARY = "查询场景层级/对象属性、聚焦并选中对象、保存场景等操作"
TAGS = ["scene", "object", "hierarchy", "selection", "save", "mesh", "material", "场景", "对象", "层级"]
SIDE_EFFECTS = "mixed"
RESULT_TYPES = ["object", "array", "text"]
OPERATIONS = [
    {
        "name": "get_hierarchy",
        "signature": "get_hierarchy(collection_name='') -> dict",
        "summary": "用线性父子索引返回场景或 Collection 的对象层级和组件摘要",
        "tags": ["hierarchy", "collection", "tree", "层级"],
        "side_effects": "read",
        "result_types": ["object"],
        "cost": "low",
    },
    {
        "name": "get_object_info",
        "signature": "get_object_info(name) -> dict",
        "summary": "查询对象 transform、材质、修改器、约束和 Mesh 统计",
        "tags": ["object", "inspect", "mesh", "对象"],
        "side_effects": "read",
        "result_types": ["object"],
        "cost": "low",
    },
    {
        "name": "find_objects",
        "signature": "find_objects(type=None, no_material=False, no_modifier=False) -> list[str]",
        "summary": "在当前 View Layer 中按类型、材质和修改器状态筛选对象",
        "tags": ["find", "filter", "view_layer", "筛选"],
        "side_effects": "read",
        "result_types": ["array"],
        "cost": "low",
    },
    {
        "name": "select_objects",
        "signature": "select_objects(names, deselect_all=True) -> dict",
        "summary": "在当前 View Layer 中选择对象并报告缺失名称",
        "tags": ["selection", "objects", "选择"],
        "side_effects": "write",
        "result_types": ["object"],
        "cost": "low",
    },
    {
        "name": "save_scene",
        "signature": "save_scene() -> str",
        "summary": "保存已经具有文件路径的当前 Blend 场景",
        "tags": ["save", "blend", "保存"],
        "side_effects": "write",
        "result_types": ["text"],
        "cost": "medium",
    },
]

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
  select_objects(names: list[str], deselect_all=True) -> dict
    在当前 View Layer 中选中指定对象，并返回 selected / missing。

  save_scene() -> str
    保存当前 .blend 文件，返回保存路径。

示例：
  scene_info = get_builtin('scene_info')
  # 找出所有缺少材质的 Mesh，聚焦第一个并截图
  bare = scene_info.find_objects(type='MESH', no_material=True)
  if bare:
      viewport = get_builtin('viewport')
      img = viewport.capture_objects([bare[0]])
      return {"missing_material": bare, "screenshot": img}
  return "所有 Mesh 对象均已有材质"
"""


def get_hierarchy(collection_name: str = "") -> dict:
    """返回场景或指定 Collection 的对象树。"""
    if collection_name:
        coll = bpy.data.collections.get(collection_name)
        if coll is None:
            raise KeyError(f"Collection not found: {collection_name}")
        objects = list(coll.all_objects)
    else:
        assert bpy.context.scene is not None
        objects = list(bpy.context.scene.objects)
    object_set = set(objects)
    children_by_parent = {obj: [] for obj in objects}
    for obj in objects:
        if obj.parent in object_set:
            children_by_parent[obj.parent].append(obj)

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
        node["children"] = [obj_to_node(child) for child in children_by_parent[obj]]
        return node

    roots = [obj for obj in objects if obj.parent not in object_set]
    return {"hierarchy": [obj_to_node(r) for r in roots]}


def get_object_info(name: str) -> dict:
    """返回指定对象的详细属性。"""
    scene = bpy.context.scene
    if scene is None:
        raise RuntimeError("No active Scene")
    obj = scene.objects.get(name)
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
    view_layer = bpy.context.view_layer
    if view_layer is None:
        raise RuntimeError("No active View Layer")
    for obj in view_layer.objects:
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
    """兼容接口；新代码应使用 viewport.focus_objects([name])。"""
    from . import viewport

    try:
        viewport.focus_objects([name])
    except ValueError:
        return False
    return True


def select_objects(names: list[str], deselect_all: bool = True) -> dict:
    """在当前 View Layer 中选中对象并报告缺失名称。"""
    view_layer = bpy.context.view_layer
    if view_layer is None:
        raise RuntimeError("No active View Layer")
    objects_by_name = {obj.name: obj for obj in view_layer.objects}
    if deselect_all:
        bpy.ops.object.select_all(action="DESELECT")
    selected = []
    missing = []
    for name in names:
        obj = objects_by_name.get(name)
        if obj is None:
            missing.append(name)
            continue
        obj.select_set(True)
        selected.append(obj.name)
    if selected:
        view_layer.objects.active = objects_by_name[selected[-1]]
    return {"selected": selected, "missing": missing}


def save_scene() -> str:
    """保存当前 .blend 文件。"""
    path = bpy.data.filepath
    if not path:
        return "文件尚未保存过，请先手动另存为"
    bpy.ops.wm.save_mainfile()
    return f"已保存：{path}"
