"""Run with Blender --background --factory-startup --python-exit-code 1.

Loads the source builtin directly, so it needs neither a packaged add-on nor a
running MCP server. Every test starts with fresh data in this isolated process.
"""

import importlib.util
from pathlib import Path
import runpy
import unittest

import bmesh
import bpy


def _load_materials():
    path = Path(__file__).resolve().parents[1] / 'blender_agent' / 'builtins' / 'materials.py'
    spec = importlib.util.spec_from_file_location('material_scope_builtin', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


materials = _load_materials()


class MaterialScopeTests(unittest.TestCase):
    def setUp(self):
        if bpy.context.object is not None and bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        for obj in list(bpy.data.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        for mesh in list(bpy.data.meshes):
            bpy.data.meshes.remove(mesh)
        for material in list(bpy.data.materials):
            bpy.data.materials.remove(material)
        mesh = bpy.data.meshes.new('SharedMesh')
        mesh.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
        self.first = bpy.data.objects.new('Target', mesh)
        self.other = bpy.data.objects.new('Other', mesh)
        scene = bpy.context.scene
        assert scene is not None
        scene.collection.objects.link(self.first)
        scene.collection.objects.link(self.other)
        self.base = bpy.data.materials.new('Base')
        self.accent = bpy.data.materials.new('Accent')
        mesh.materials.append(self.base)
        mesh.materials.append(self.accent)

    def snapshot(self):
        return {
            'materials': tuple(bpy.data.materials.keys()),
            'meshes': tuple(bpy.data.meshes.keys()),
            'objects': {
                obj.name: (
                    obj.data.as_pointer(),
                    tuple(slot.material.name if slot.material else None for slot in obj.material_slots),
                    tuple(face.material_index for face in obj.data.polygons),
                )
                for obj in bpy.data.objects if obj.type == 'MESH'
            },
        }

    def assert_rejected_without_changes(self, call, error=ValueError):
        before = self.snapshot()
        with self.assertRaises(error) as raised:
            call()
        self.assertEqual(self.snapshot(), before)
        return str(raised.exception)

    def assignments(self, policy):
        return (
            lambda: materials.apply_material_to_objects('New', [self.first.name], shared_data=policy),
            lambda: materials.ensure_material_slots(self.first.name, ['New'], shared_data=policy),
            lambda: materials.assign_faces_by_index(self.first.name, [0], material_name='New', shared_data=policy),
        )

    def test_default_rejects_each_operation_before_material_creation(self):
        for call in (
            lambda: materials.apply_material_to_objects('New', [self.first.name]),
            lambda: materials.ensure_material_slots(self.first.name, ['New']),
            lambda: materials.assign_faces_by_index(self.first.name, [0], material_name='New'),
        ):
            message = self.assert_rejected_without_changes(call)
            self.assertIn(self.first.name, message)
            self.assertIn(self.other.name, message)

    def test_copy_isolates_each_operation(self):
        for index in range(3):
            with self.subTest(operation=index):
                self.setUp()
                original_data = self.other.data
                result = self.assignments('copy')[index]()
                self.assertEqual(result['affected_objects'], [self.first.name])
                self.assertNotEqual(self.first.data, original_data)
                self.assertEqual(self.other.data, original_data)
                self.assertEqual([mat.name for mat in self.other.data.materials], ['Base', 'Accent'])
                self.assertEqual(self.other.data.polygons[0].material_index, 0)
                if index == 2:
                    self.assertEqual(self.first.data.polygons[0].material_index, 2)
                else:
                    self.assertEqual([mat.name for mat in self.first.data.materials], ['New'])

    def test_allow_reports_and_changes_all_sharing_objects(self):
        for index in range(3):
            with self.subTest(operation=index):
                self.setUp()
                result = self.assignments('allow')[index]()
                self.assertEqual(result['affected_objects'], ['Other', 'Target'])
                self.assertEqual(self.first.data, self.other.data)
                if index == 2:
                    self.assertEqual(self.other.data.polygons[0].material_index, 2)
                else:
                    self.assertEqual([mat.name for mat in self.other.data.materials], ['New'])

    def test_batch_rejects_before_touching_an_earlier_independent_object(self):
        independent = bpy.data.objects.new('Independent', self.first.data.copy())
        scene = bpy.context.scene
        assert scene is not None
        scene.collection.objects.link(independent)
        self.assert_rejected_without_changes(lambda: materials.apply_material_to_objects(
            'New', [independent.name, self.first.name],
        ))

    def test_invalid_arguments_are_checked_before_copying_or_creating(self):
        calls = (
            lambda: materials.apply_material_to_objects('New', [self.first.name], slot=-1, shared_data='copy'),
            lambda: materials.apply_material_to_objects('New', [self.first.name], shared_data='unknown'),
            lambda: materials.ensure_material_slots(self.first.name, ['New', ''], shared_data='copy'),
            lambda: materials.assign_faces_by_index(self.first.name, [0], material_index=99, shared_data='copy'),
            lambda: materials.assign_faces_by_index(self.first.name, [0, 'invalid'], material_name='New', shared_data='copy'),
        )
        for call in calls:
            self.assert_rejected_without_changes(call, (ValueError, TypeError))

    def test_allow_updates_shared_data_once_for_duplicate_batch_targets(self):
        result = materials.apply_material_to_objects(
            'New', [self.first.name, self.other.name, self.first.name],
            replace_all=False, append=True, shared_data='allow',
        )
        self.assertEqual([mat.name for mat in self.first.data.materials], ['Base', 'Accent', 'New'])
        self.assertEqual(len(result['updated']), 2)
        self.assertEqual({item['slot'] for item in result['updated']}, {2})

    def test_no_valid_targets_does_not_create_an_unused_material(self):
        result = materials.apply_material_to_objects('New', ['Missing'])
        self.assertIsNone(bpy.data.materials.get('New'))
        self.assertEqual(result['missing_objects'], ['Missing'])
        self.assertEqual(result['affected_objects'], [])

    def test_edit_mode_rejects_copy_and_allows_explicit_shared_face_assignment(self):
        self.first.select_set(True)
        view_layer = bpy.context.view_layer
        assert view_layer is not None
        view_layer.objects.active = self.first
        bpy.ops.object.mode_set(mode='EDIT')
        for call in self.assignments('copy'):
            message = self.assert_rejected_without_changes(call)
            self.assertIn('Object Mode', message)
        result = materials.assign_faces_by_index(
            self.first.name, [0], material_index=1, shared_data='allow',
        )
        self.assertEqual(result['affected_objects'], ['Other', 'Target'])
        edit_mesh = bmesh.from_edit_mesh(self.first.data)
        edit_mesh.faces.ensure_lookup_table()
        self.assertEqual(edit_mesh.faces[0].material_index, 1)
        bpy.ops.object.mode_set(mode='OBJECT')
        self.assertEqual(self.other.data.polygons[0].material_index, 1)

    def test_existing_unshared_and_edit_mode_material_behavior(self):
        previous_tests = runpy.run_path(str(Path(__file__).with_name('blender_builtins.py')))
        previous_tests['_test_materials'](materials)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(MaterialScopeTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print('BLENDER_AGENT_MATERIAL_SCOPE_OK')
