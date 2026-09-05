import unittest

from pathlib import Path

import onmyoji_rigged_mesh_gui as mesh_gui


class MeshBindPoseTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
