"""
materials.py — 创建/复用 Principled 材质，并批量分配到对象或面

把 LLM 常见且脆弱的材质样板代码抽到 builtin 中：
- 复用同名材质，避免 .001/.002 漂移
- 统一处理 Principled BSDF 常见输入
- 兼容部分 Blender 版本的 socket 名差异
- 批量分配材质槽，按面索引设置材质
"""

import bpy
from typing import Optional

SUMMARY = "创建/复用 Principled 材质、应用常见预设、批量赋给对象，并按面索引分配材质"
TAGS = ["material", "shader", "principled", "texture", "faces", "材质", "着色器"]
SIDE_EFFECTS = "write"
RESULT_TYPES = ["object"]

DESCRIPTION = """
materials builtin — 材质创建与分配

函数：
  ensure_principled_material(
      name: str,
      reuse=True,
      reset_nodes=False,
      inputs=None,
      base_color=None,
      metallic=None,
      roughness=None,
      transmission=None,
      ior=None,
      alpha=None,
      specular=None,
      emission_color=None,
      emission_strength=None,
      blend_method=None,
      use_screen_refraction=None,
  ) -> dict
    创建或复用一个 Principled 材质，并设置常见输入。
    自动兼容部分 Blender 版本的 socket 名差异，例如 Transmission / Transmission Weight。

  create_preset(name: str, preset: str, reuse=True, reset_nodes=False, overrides=None) -> dict
    用内置预设创建材质。支持：'paint' | 'painted_metal' | 'glass' | 'rubber' | 'metal'
    overrides 可覆盖预设参数，例如 {'base_color': (0.9, 0.1, 0.05, 1.0)}。

  apply_material_to_objects(material_name: str, object_names, replace_all=True, append=False, slot=0) -> dict
    将材质批量赋给对象。
    replace_all=True: 清空已有材质槽，仅保留该材质。
    append=True: 追加到末尾。
    否则写入指定 slot。

  ensure_material_slots(object_name: str, material_names, append=False) -> dict
    确保对象材质槽包含给定材质名。append=False 时会按给定顺序重建材质槽。

  assign_faces_by_index(object_name: str, face_indices, material_name=None, material_index=None) -> dict
    为指定对象的面索引批量设置材质。
    可直接给 material_index，或给 material_name 自动找到/追加到材质槽。

  get_material_info(name: str) -> dict
    返回材质节点与常见 Principled 输入摘要。

示例：
  materials = get_builtin('materials')

  materials.create_preset(
      'Truck_Body_Red',
      'painted_metal',
      overrides={'base_color': (0.9, 0.15, 0.05, 1.0), 'metallic': 0.3, 'roughness': 0.3},
  )
  materials.apply_material_to_objects('Truck_Body_Red', ['Car body', 'door-left', 'door-right'])

  materials.create_preset('Truck_Glass', 'glass')
  materials.assign_faces_by_index('Car body', [12, 13, 14], material_name='Truck_Glass')
"""


_SOCKET_ALIASES = {
    'base_color': ('Base Color',),
    'metallic': ('Metallic',),
    'roughness': ('Roughness',),
    'transmission': ('Transmission Weight', 'Transmission'),
    'ior': ('IOR',),
    'alpha': ('Alpha',),
    'specular': ('Specular IOR Level', 'Specular'),
    'emission_color': ('Emission Color', 'Emission'),
    'emission_strength': ('Emission Strength',),
}

_COMMON_INPUT_ORDER = [
    'base_color',
    'metallic',
    'roughness',
    'transmission',
    'ior',
    'alpha',
    'specular',
    'emission_color',
    'emission_strength',
]

_PRESETS = {
    'paint': {
        'base_color': (0.8, 0.12, 0.08, 1.0),
        'metallic': 0.0,
        'roughness': 0.45,
        'specular': 0.5,
    },
    'painted_metal': {
        'base_color': (0.82, 0.12, 0.08, 1.0),
        'metallic': 0.25,
        'roughness': 0.32,
        'specular': 0.5,
    },
    'glass': {
        'base_color': (0.6, 0.8, 0.95, 1.0),
        'roughness': 0.05,
        'transmission': 0.95,
        'ior': 1.45,
        'blend_method': 'BLEND',
        'use_screen_refraction': True,
    },
    'rubber': {
        'base_color': (0.04, 0.04, 0.04, 1.0),
        'roughness': 0.85,
        'specular': 0.25,
    },
    'metal': {
        'base_color': (0.7, 0.7, 0.73, 1.0),
        'metallic': 1.0,
        'roughness': 0.2,
    },
}


def _coerce_name_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _normalize_input_key(name: str) -> str:
    return name.strip().lower().replace(' ', '_')


def _find_socket(inputs, key: str):
    normalized = _normalize_input_key(key)
    candidates = list(_SOCKET_ALIASES.get(normalized, ()))
    if not candidates:
        candidates.append(key)

    for candidate in candidates:
        socket = inputs.get(candidate)
        if socket is not None:
            return socket

    lowered = {socket.name.lower(): socket for socket in inputs}
    for candidate in candidates:
        socket = lowered.get(candidate.lower())
        if socket is not None:
            return socket

    return None


def _coerce_socket_value(socket, value):
    default_value = getattr(socket, 'default_value', None)
    if isinstance(default_value, (int, float)):
        return float(value)

    if hasattr(default_value, '__len__') and not isinstance(default_value, (str, bytes)):
        values = list(value)
        target_len = len(default_value)
        if target_len == 4 and len(values) == 3:
            values.append(1.0)
        if len(values) != target_len:
            raise ValueError(f"Socket '{socket.name}' expects {target_len} values, got {len(values)}")
        return values

    return value


def _serialize_value(value):
    if hasattr(value, 'to_list'):
        return value.to_list()
    if hasattr(value, '__len__') and not isinstance(value, (str, bytes)):
        return [float(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def _find_principled_bsdf(material):
    if not material.use_nodes or material.node_tree is None:
        return None
    for node in material.node_tree.nodes:
        if node.type == 'BSDF_PRINCIPLED':
            return node
    return None


def _ensure_output_node(node_tree):
    for node in node_tree.nodes:
        if node.type == 'OUTPUT_MATERIAL':
            return node
    node = node_tree.nodes.new('ShaderNodeOutputMaterial')
    node.location = (300, 0)
    return node


def _ensure_principled_setup(material, reset_nodes: bool = False):
    material.use_nodes = True
    node_tree = material.node_tree
    if node_tree is None:
        raise RuntimeError(f"Material '{material.name}' has no node tree")

    if reset_nodes:
        node_tree.nodes.clear()

    output = _ensure_output_node(node_tree)
    bsdf = _find_principled_bsdf(material)
    if bsdf is None:
        bsdf = node_tree.nodes.new('ShaderNodeBsdfPrincipled')
        bsdf.location = (0, 0)

    surface_socket = output.inputs.get('Surface')
    if surface_socket is None:
        raise RuntimeError('Material output node is missing Surface input')

    has_link = False
    for link in node_tree.links:
        if link.to_node == output and link.to_socket == surface_socket and link.from_node == bsdf:
            has_link = True
            break

    if not has_link:
        for link in list(node_tree.links):
            if link.to_node == output and link.to_socket == surface_socket:
                node_tree.links.remove(link)
        node_tree.links.new(bsdf.outputs['BSDF'], surface_socket)

    return bsdf


def _get_or_create_material(name: str, reuse: bool = True):
    existing = bpy.data.materials.get(name)
    if existing is not None and reuse:
        return existing, False
    return bpy.data.materials.new(name=name), True


def _get_material_collection(obj):
    data = getattr(obj, 'data', None)
    if data is None or not hasattr(data, 'materials'):
        raise TypeError(f"Object '{obj.name}' does not support material slots")
    return data.materials


def _set_material_render_options(material, blend_method: Optional[str], use_screen_refraction: Optional[bool]):
    if blend_method is not None and hasattr(material, 'blend_method'):
        material.blend_method = blend_method
    if use_screen_refraction is not None and hasattr(material, 'use_screen_refraction'):
        material.use_screen_refraction = bool(use_screen_refraction)


def ensure_principled_material(
    name: str,
    reuse: bool = True,
    reset_nodes: bool = False,
    inputs: Optional[dict] = None,
    base_color=None,
    metallic: Optional[float] = None,
    roughness: Optional[float] = None,
    transmission: Optional[float] = None,
    ior: Optional[float] = None,
    alpha: Optional[float] = None,
    specular: Optional[float] = None,
    emission_color=None,
    emission_strength: Optional[float] = None,
    blend_method: Optional[str] = None,
    use_screen_refraction: Optional[bool] = None,
) -> dict:
    material, created = _get_or_create_material(name, reuse=reuse)
    bsdf = _ensure_principled_setup(material, reset_nodes=reset_nodes)

    values = dict(inputs or {})
    direct_values = {
        'base_color': base_color,
        'metallic': metallic,
        'roughness': roughness,
        'transmission': transmission,
        'ior': ior,
        'alpha': alpha,
        'specular': specular,
        'emission_color': emission_color,
        'emission_strength': emission_strength,
    }
    for key, value in direct_values.items():
        if value is not None:
            values[key] = value

    for key, value in values.items():
        socket = _find_socket(bsdf.inputs, key)
        if socket is None:
            raise KeyError(f"Principled BSDF input not found: {key}")
        socket.default_value = _coerce_socket_value(socket, value)

    if blend_method is None and hasattr(material, 'blend_method'):
        alpha_value = values.get('alpha')
        transmission_value = values.get('transmission')
        if (alpha_value is not None and float(alpha_value) < 1.0) or (transmission_value is not None and float(transmission_value) > 0.0):
            blend_method = 'BLEND'

    _set_material_render_options(material, blend_method, use_screen_refraction)

    info = get_material_info(material.name)
    info.update({
        'created': created,
        'reused_existing': not created,
    })
    return info


def create_preset(
    name: str,
    preset: str,
    reuse: bool = True,
    reset_nodes: bool = False,
    overrides: Optional[dict] = None,
) -> dict:
    preset_key = preset.strip().lower()
    if preset_key not in _PRESETS:
        raise KeyError(f"Unknown preset '{preset}'. Available: {sorted(_PRESETS)}")

    config = dict(_PRESETS[preset_key])
    if overrides:
        config.update(overrides)

    blend_method = config.pop('blend_method', None)
    use_screen_refraction = config.pop('use_screen_refraction', None)

    info = ensure_principled_material(
        name=name,
        reuse=reuse,
        reset_nodes=reset_nodes,
        inputs=config,
        blend_method=blend_method,
        use_screen_refraction=use_screen_refraction,
    )
    info['preset'] = preset_key
    return info


def ensure_material_slots(object_name: str, material_names, append: bool = False) -> dict:
    obj = bpy.data.objects.get(object_name)
    if obj is None:
        raise KeyError(f"Object not found: {object_name}")

    collection = _get_material_collection(obj)
    names = _coerce_name_list(material_names)
    materials = [ensure_principled_material(name)['name'] if bpy.data.materials.get(name) is None else name for name in names]

    if append:
        existing_names = [slot.material.name for slot in obj.material_slots if slot.material]
        for material_name in materials:
            if material_name not in existing_names:
                collection.append(bpy.data.materials[material_name])
                existing_names.append(material_name)
    else:
        collection.clear()
        for material_name in materials:
            collection.append(bpy.data.materials[material_name])

    return {
        'object': obj.name,
        'slot_count': len(collection),
        'materials': [slot.material.name if slot.material else None for slot in obj.material_slots],
    }


def _assign_material_to_slot(collection, material, slot: int) -> int:
    if slot < 0:
        raise ValueError('slot must be >= 0')

    while len(collection) <= slot:
        collection.append(material)
    collection[slot] = material
    return slot


def apply_material_to_objects(
    material_name: str,
    object_names,
    replace_all: bool = True,
    append: bool = False,
    slot: int = 0,
) -> dict:
    material = bpy.data.materials.get(material_name)
    if material is None:
        material_name = ensure_principled_material(material_name)['name']
        material = bpy.data.materials[material_name]

    updated = []
    missing = []
    skipped = []

    for object_name in _coerce_name_list(object_names):
        obj = bpy.data.objects.get(object_name)
        if obj is None:
            missing.append(object_name)
            continue

        try:
            collection = _get_material_collection(obj)
        except TypeError:
            skipped.append(object_name)
            continue

        if replace_all:
            collection.clear()
            collection.append(material)
            applied_slot = 0
        elif append:
            collection.append(material)
            applied_slot = len(collection) - 1
        else:
            applied_slot = _assign_material_to_slot(collection, material, int(slot))

        updated.append({
            'object': obj.name,
            'slot': applied_slot,
            'materials': [slot.material.name if slot.material else None for slot in obj.material_slots],
        })

    return {
        'material': material.name,
        'updated': updated,
        'missing_objects': missing,
        'skipped_objects': skipped,
    }


def _resolve_material_index(obj, material_name: str) -> tuple[int, bool]:
    collection = _get_material_collection(obj)
    for index, slot in enumerate(obj.material_slots):
        if slot.material and slot.material.name == material_name:
            return index, False

    material = bpy.data.materials.get(material_name)
    if material is None:
        material_name = ensure_principled_material(material_name)['name']
        material = bpy.data.materials[material_name]

    collection.append(material)
    return len(collection) - 1, True


def assign_faces_by_index(
    object_name: str,
    face_indices,
    material_name: Optional[str] = None,
    material_index: Optional[int] = None,
) -> dict:
    obj = bpy.data.objects.get(object_name)
    if obj is None:
        raise KeyError(f"Object not found: {object_name}")
    if obj.type != 'MESH':
        raise TypeError(f"Object '{object_name}' is not a mesh")
    if material_name is None and material_index is None:
        raise ValueError('Either material_name or material_index must be provided')

    resolved_index = material_index
    slot_added = False
    if material_name is not None:
        resolved_index, slot_added = _resolve_material_index(obj, material_name)

    assert resolved_index is not None
    if obj.mode == 'EDIT':
        obj.update_from_editmode()

    mesh = obj.data
    polygons = mesh.polygons
    unique_indices = sorted({int(index) for index in face_indices})
    applied = []
    skipped = []
    for index in unique_indices:
        if 0 <= index < len(polygons):
            polygons[index].material_index = int(resolved_index)
            applied.append(index)
        else:
            skipped.append(index)

    mesh.update()

    return {
        'object': obj.name,
        'material_index': int(resolved_index),
        'material_name': material_name,
        'slot_added': slot_added,
        'applied_faces': applied,
        'skipped_faces': skipped,
    }


def get_material_info(name: str) -> dict:
    material = bpy.data.materials.get(name)
    if material is None:
        return {
            'exists': False,
            'name': name,
        }

    info = {
        'exists': True,
        'name': material.name,
        'use_nodes': bool(material.use_nodes),
        'blend_method': getattr(material, 'blend_method', None),
        'node_count': len(material.node_tree.nodes) if material.node_tree else 0,
        'inputs': {},
    }

    bsdf = _find_principled_bsdf(material)
    if bsdf is None:
        return info

    info['principled_input_names'] = [socket.name for socket in bsdf.inputs]
    for key in _COMMON_INPUT_ORDER:
        socket = _find_socket(bsdf.inputs, key)
        if socket is not None:
            info['inputs'][key] = _serialize_value(socket.default_value)

    return info
