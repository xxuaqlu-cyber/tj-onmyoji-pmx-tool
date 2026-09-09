import unittest
from unittest import mock

from pathlib import Path

import onmyoji_rigged_mesh_gui as mesh_gui


class MeshBindPoseTests(unittest.TestCase):
    def test_face_pose_revision_only_invalidates_matching_skeleton_composites(self):
        package = mesh_gui.MaterialPackage(
            xml_path=Path("material.xml"),
            index=0,
            package_name="test",
            materials=[],
            mesh_paths=[],
            texture_map={},
            confidence="test",
        )

        def composite(skeleton_path):
            return mesh_gui.CompositeModel(
                name="test",
                mesh_paths=[Path("body.mesh")],
                packages=[package],
                skeleton_paths=[skeleton_path],
            )

        with mock.patch.object(
            mesh_gui,
            "read_mesh_bone_layout",
            return_value=(("root", "kk_face", "lip"), (-1, 0, 1), 10),
        ):
            self.assertEqual(mesh_gui._composite_geometry_revisions(composite(None)), {})
            self.assertEqual(
                mesh_gui._composite_geometry_revisions(composite(Path("body.skeleton"))),
                {"facial_neutral_pose": mesh_gui.FACIAL_NEUTRAL_POSE_VERSION},
            )

        with mock.patch.object(
            mesh_gui,
            "read_mesh_bone_layout",
            return_value=(("root", "spine", "head"), (-1, 0, 1), 10),
        ):
            self.assertEqual(
                mesh_gui._composite_geometry_revisions(composite(Path("body.skeleton"))),
                {},
            )

    def test_gim_relative_skeleton_reference_is_resolved_from_owner(self):
        self.assertEqual(
            mesh_gui.resolve_gim_relative_reference(
                "model/s9_xuzuozhinan_show/s9_xuzuozhinan_show_toufa1.gim",
                "../s9_xuzuozhinan/s9_xuzuozhinan.skeleton",
            ),
            "model/s9_xuzuozhinan/s9_xuzuozhinan.skeleton",
        )

    def test_local_mesh_bones_expand_to_explicit_gim_skeleton(self):
        identity = (
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        )
        skeleton = mesh_gui.SkeletonHierarchy(
            source=Path("shared.skeleton"),
            name="shared",
            bone_names=("root", "unused", "hair"),
            bone_keys=("root", "unused", "hair"),
            bone_parents=(-1, 0, 0),
            bone_bind_transforms=(
                (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0),
                (0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0),
                (0.0, 2.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0),
            ),
        )
        mesh = mesh_gui.ParsedMesh(
            version=4,
            submeshes=[(1, 0, 1, 0)],
            bone_parents=[-1, 0],
            bone_names=["root", "hair"],
            bone_matrices=[identity, identity],
            positions=[(0.0, 2.0, 0.0)],
            normals=[(0.0, 1.0, 0.0)],
            faces=[],
            uvs=[(0.0, 0.0)],
            joints=[(1, 0, 0, 0)],
            weights=[(1.0, 0.0, 0.0, 0.0)],
        )

        self.assertTrue(mesh_gui._expand_mesh_to_skeleton(mesh, skeleton))
        self.assertEqual(mesh.bone_names, ["root", "unused", "hair"])
        self.assertEqual(mesh.bone_parents, [-1, 0, 0])
        self.assertEqual(mesh.joints[0], (2, 0, 0, 0))

    def test_action_baked_mesh_is_restored_from_skeleton_bind(self):
        identity = (
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        )
        skeleton = mesh_gui.SkeletonHierarchy(
            source=Path("synthetic.skeleton"),
            name="synthetic",
            bone_names=("root", "child"),
            bone_keys=("root", "child"),
            bone_parents=(-1, 0),
            bone_bind_transforms=(
                (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0),
                (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0),
            ),
        )
        bind_globals = mesh_gui._skeleton_bind_global_matrices(skeleton)
        self.assertIsNotNone(bind_globals)
        bind_child = bind_globals[1]
        # A 90-degree Z rotation at the child bone represents an action frame.
        action_child = (
            0.0, 1.0, 0.0, 0.0,
            -1.0, 0.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            1.0, 0.0, 0.0, 1.0,
        )
        skin = mesh_gui._matrix4_multiply(
            mesh_gui._inverse_affine_row_matrix4(bind_child), action_child
        )
        current_position = mesh_gui._transform_row_position((1.0, 1.0, 0.0), skin)
        mesh = mesh_gui.ParsedMesh(
            version=4,
            submeshes=[(1, 0, 0, 0)],
            bone_parents=[-1, 0],
            bone_names=["root", "child"],
            bone_matrices=[identity, action_child],
            positions=[current_position],
            normals=[(0.0, 1.0, 0.0)],
            faces=[],
            uvs=[(0.0, 0.0)],
            joints=[(1, 1, 1, 1)],
            weights=[(1.0, 0.0, 0.0, 0.0)],
        )

        self.assertTrue(mesh_gui._restore_mesh_bind_pose(mesh, skeleton))
        for actual, expected in zip(mesh.positions[0], (1.0, 1.0, 0.0)):
            self.assertAlmostEqual(actual, expected, places=5)
        # The source normal is transformed by the inverse of the action when
        # returning to bind space, so it must point along the bind-space axis.
        for actual, expected in zip(mesh.normals[0], (1.0, 0.0, 0.0)):
            self.assertAlmostEqual(actual, expected, places=5)
        for actual, expected in zip(mesh.bone_matrices[1], bind_child):
            self.assertAlmostEqual(actual, expected, places=5)

    def test_bind_restore_preserves_mesh_neutral_face_pose(self):
        identity = (
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        )
        skeleton = mesh_gui.SkeletonHierarchy(
            source=Path("face.skeleton"),
            name="face",
            bone_names=("root", "body", "kk_face", "lip"),
            bone_keys=("root", "body", "kk_face", "lip"),
            bone_parents=(-1, 0, 0, 2),
            bone_bind_transforms=(
                (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0),
                (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0),
                (0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0),
                (0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0),
            ),
        )
        bind_globals = mesh_gui._skeleton_bind_global_matrices(skeleton)
        self.assertIsNotNone(bind_globals)
        action_body = (
            0.0, 1.0, 0.0, 0.0,
            -1.0, 0.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            1.0, 0.0, 0.0, 1.0,
        )
        body_skin = mesh_gui._matrix4_multiply(
            mesh_gui._inverse_affine_row_matrix4(bind_globals[1]),
            action_body,
        )
        body_bind_position = (1.0, 1.0, 0.0)
        body_action_position = mesh_gui._transform_row_position(
            body_bind_position, body_skin
        )
        neutral_face_position = (0.0, 2.5, 0.0)
        mesh = mesh_gui.ParsedMesh(
            version=4,
            submeshes=[(2, 0, 0, 0)],
            bone_parents=[-1, 0, 0, 2],
            bone_names=["root", "body", "kk_face", "lip"],
            bone_matrices=[
                identity,
                action_body,
                (
                    1.0, 0.0, 0.0, 0.0,
                    0.0, 1.0, 0.0, 0.0,
                    0.0, 0.0, 1.0, 0.0,
                    0.0, 1.2, 0.0, 1.0,
                ),
                (
                    1.0, 0.0, 0.0, 0.0,
                    0.0, 1.0, 0.0, 0.0,
                    0.0, 0.0, 1.0, 0.0,
                    0.0, 2.4, 0.0, 1.0,
                ),
            ],
            positions=[body_action_position, neutral_face_position],
            normals=[(0.0, 1.0, 0.0), (0.0, 0.0, 1.0)],
            faces=[],
            uvs=[(0.0, 0.0), (0.0, 0.0)],
            joints=[(1, 1, 1, 1), (3, 3, 3, 3)],
            weights=[(1.0, 0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0)],
        )

        self.assertTrue(mesh_gui._restore_mesh_bind_pose(mesh, skeleton))
        for actual, expected in zip(mesh.positions[0], body_bind_position):
            self.assertAlmostEqual(actual, expected, places=5)
        self.assertEqual(mesh.positions[1], neutral_face_position)


if __name__ == "__main__":
    unittest.main()
