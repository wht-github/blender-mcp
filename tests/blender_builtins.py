"""Behavioral tests for builtins, executed by a real Blender process."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import sys
import tempfile
from typing import cast
import zipfile

import bmesh
import bpy


def _zip_argument() -> Path:
    if "--" not in sys.argv:
        raise RuntimeError("Pass the add-on zip after '--'")
    index = sys.argv.index("--")
    return Path(sys.argv[index + 1]).resolve()


def _assert_close(left: float, right: float, tolerance: float = 1e-5) -> None:
    if abs(left - right) > tolerance:
        raise AssertionError(f"{left} != {right}")


def _test_materials(materials) -> dict:
    complex_material = bpy.data.materials.new("Builtin_Complex_Graph")
    complex_material.use_nodes = True
    node_tree = complex_material.node_tree
    assert node_tree is not None
    node_tree.nodes.clear()
    output = node_tree.nodes.new("ShaderNodeOutputMaterial")
    first = node_tree.nodes.new("ShaderNodeBsdfPrincipled")
    second = node_tree.nodes.new("ShaderNodeBsdfPrincipled")
    mix = node_tree.nodes.new("ShaderNodeMixShader")
    node_tree.links.new(first.outputs["BSDF"], mix.inputs[1])
    node_tree.links.new(second.outputs["BSDF"], mix.inputs[2])
    node_tree.links.new(mix.outputs[0], output.inputs["Surface"])

    try:
        materials.ensure_principled_material(
            complex_material.name,
            roughness=0.4,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Complex material graph was not rejected")
    source_type = output.inputs["Surface"].links[0].from_node.type
    if source_type != "MIX_SHADER":
        raise AssertionError(f"Complex graph was rewired to {source_type}")

    bpy.ops.mesh.primitive_cube_add(location=(3, 0, 0))
    mesh_object = bpy.context.object
    assert mesh_object is not None
    mesh = cast(bpy.types.Mesh, mesh_object.data)
    first_material = bpy.data.materials.new("Builtin_Face_First")
    second_material = bpy.data.materials.new("Builtin_Face_Second")
    mesh.materials.append(first_material)
    mesh.materials.append(second_material)

    bpy.ops.object.mode_set(mode="EDIT")
    materials.assign_faces_by_index(
        mesh_object.name,
        [0],
        material_index=1,
    )
    edit_mesh = bmesh.from_edit_mesh(mesh)
    edit_mesh.faces.ensure_lookup_table()
    if edit_mesh.faces[0].material_index != 1:
        raise AssertionError("Edit Mode material assignment did not update BMesh")
    bpy.ops.object.mode_set(mode="OBJECT")
    if mesh.polygons[0].material_index != 1:
        raise AssertionError("Edit Mode material assignment did not persist")

    try:
        materials.assign_faces_by_index(
            mesh_object.name,
            [1],
            material_index=99,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Invalid material slot index was accepted")

    return {
        "complex_graph_preserved": True,
        "edit_mode_assignment": True,
        "invalid_slot_rejected": True,
    }


def _test_scene_info(scene_info) -> dict:
    parent = bpy.data.objects.new("Builtin_Hierarchy_Parent", None)
    child = bpy.data.objects.new("Builtin_Hierarchy_Child", None)
    scene = bpy.context.scene
    assert scene is not None
    scene.collection.objects.link(parent)
    scene.collection.objects.link(child)
    child.parent = parent

    hierarchy = scene_info.get_hierarchy()["hierarchy"]
    parent_node = next(item for item in hierarchy if item["name"] == parent.name)
    child_names = [item["name"] for item in parent_node["children"]]
    if child.name not in child_names:
        raise AssertionError("Hierarchy did not include the parent-child relationship")

    selection = scene_info.select_objects(
        [parent.name, "Builtin_Missing_Object"],
        deselect_all=True,
    )
    if selection != {
        "selected": [parent.name],
        "missing": ["Builtin_Missing_Object"],
    }:
        raise AssertionError(f"Unexpected selection result: {selection}")

    return {
        "hierarchy": True,
        "structured_selection": True,
    }


def _test_viewport(viewport) -> dict:
    bpy.ops.mesh.primitive_cube_add(location=(-3, 0, 0))
    previous = bpy.context.object
    bpy.ops.mesh.primitive_cube_add(location=(0, 3, 0))
    target = bpy.context.object
    assert previous is not None and target is not None

    bpy.ops.object.select_all(action="DESELECT")
    previous.select_set(True)
    view_layer = bpy.context.view_layer
    assert view_layer is not None
    view_layer.objects.active = previous
    target.hide_set(True)

    view_context = viewport._find_view3d_context()
    region_3d = view_context["space_data"].region_3d
    saved_distance = float(region_3d.view_distance)
    saved_location = region_3d.view_location.copy()
    saved_rotation = region_3d.view_rotation.copy()
    saved_perspective = region_3d.view_perspective

    capture_available = True
    try:
        image = viewport.capture_objects([target.name], width=320, height=240)
    except RuntimeError as exc:
        if "screenshot" not in str(exc).lower() or "unavailable" not in str(exc).lower():
            raise
        capture_available = False
    else:
        if len(base64.b64decode(image, validate=True)) < 100:
            raise AssertionError("Viewport capture returned an unexpectedly small image")
    if viewport.get_selected_objects() != [previous.name]:
        raise AssertionError("Viewport capture did not restore selection")
    if view_layer.objects.active != previous:
        raise AssertionError("Viewport capture did not restore the active object")
    if not target.hide_get():
        raise AssertionError("Viewport capture did not restore target visibility")
    _assert_close(float(region_3d.view_distance), saved_distance)
    if (region_3d.view_location - saved_location).length > 1e-5:
        raise AssertionError("Viewport capture did not restore view location")
    if region_3d.view_rotation.rotation_difference(saved_rotation).angle > 1e-5:
        raise AssertionError("Viewport capture did not restore view rotation")
    if region_3d.view_perspective != saved_perspective:
        raise AssertionError("Viewport capture did not restore view perspective")

    return {
        "capture_available": capture_available,
        "failure_path_restored": not capture_available,
        "selection_restored": True,
        "visibility_restored": True,
        "view_restored": True,
    }


def _test_screenshot(screenshot) -> dict:
    if screenshot._resolve_dimensions(None, 200, 800, 400) != (400, 200):
        raise AssertionError("Aspect-ratio dimension resolution failed")
    try:
        screenshot._resolve_dimensions(8192, 8192, 800, 400)
    except ValueError:
        pass
    else:
        raise AssertionError("Oversized image dimensions were accepted")
    try:
        screenshot.as_result("abc", screenshot="override")
    except ValueError:
        pass
    else:
        raise AssertionError("Reserved image result fields were overwritten")

    scene = bpy.context.scene
    assert scene is not None
    original = {
        "frame": scene.frame_current,
        "filepath": scene.render.filepath,
        "format": scene.render.image_settings.file_format,
        "width": scene.render.resolution_x,
        "height": scene.render.resolution_y,
        "percentage": scene.render.resolution_percentage,
    }
    image = screenshot.capture_render(frame=2, width=64, height=32)
    if len(base64.b64decode(image, validate=True)) < 100:
        raise AssertionError("Render capture returned an unexpectedly small image")
    restored = {
        "frame": scene.frame_current,
        "filepath": scene.render.filepath,
        "format": scene.render.image_settings.file_format,
        "width": scene.render.resolution_x,
        "height": scene.render.resolution_y,
        "percentage": scene.render.resolution_percentage,
    }
    if restored != original:
        raise AssertionError(f"Render settings were not restored: {restored} != {original}")
    return {
        "dimension_bounds": True,
        "reserved_fields": True,
        "render_settings_restored": True,
    }


def _test_diagnostics(blender_log, eval_core, BuiltinLoader) -> dict:
    eval_core.setup(BuiltinLoader())
    status = blender_log.get_runtime_status()
    if "queue" not in status or "runtime" not in status:
        raise AssertionError(f"Unexpected runtime diagnostics: {status}")
    deprecated = blender_log.capture_script_output("__result__ = 1")
    if deprecated.get("error", {}).get("code") != "DEPRECATED_NESTED_EVAL":
        raise AssertionError(f"Nested eval was not disabled: {deprecated}")
    return {
        "runtime_status": True,
        "nested_eval_disabled": True,
    }


def main() -> None:
    archive = _zip_argument()
    if not archive.exists():
        raise FileNotFoundError(archive)

    with tempfile.TemporaryDirectory(
        prefix="blender-agent-builtins-",
        ignore_cleanup_errors=True,
    ) as temp_dir:
        with zipfile.ZipFile(archive) as package_zip:
            package_zip.extractall(temp_dir)
        sys.path.insert(0, temp_dir)

        from blender_agent import eval_core
        from blender_agent.builtin_loader import BuiltinLoader
        from blender_agent.builtins import (
            blender_log,
            materials,
            scene_info,
            screenshot,
            viewport,
        )

        result = {
            "materials": _test_materials(materials),
            "scene_info": _test_scene_info(scene_info),
            "screenshot": _test_screenshot(screenshot),
            "viewport": _test_viewport(viewport),
            "diagnostics": _test_diagnostics(
                blender_log,
                eval_core,
                BuiltinLoader,
            ),
        }
        print(f"BLENDER_AGENT_BUILTINS_OK {json.dumps(result, sort_keys=True)}")


if __name__ == "__main__":
    main()
