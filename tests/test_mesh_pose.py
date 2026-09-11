import unittest
from unittest import mock

from pathlib import Path

import onmyoji_rigged_mesh_gui as mesh_gui


def translation_matrix(x, y, z):
    return (
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        x, y, z, 1.0,
    )


BODY_BONES = (
    "root",
    "bip01_l_clavicle", "bip01_r_clavicle",
    "bip01_l_upperarm", "bip01_r_upperarm",
    "bip01_l_forearm", "bip01_r_forearm",
    "bip01_l_hand", "bip01_r_hand",
)
NEUTRAL_BODY_POSITIONS = (
    (0.0, 0.0, 0.0),
    (1.0, 5.0, 0.0), (-1.0, 5.0, 0.0),
    (2.0, 5.0, 0.0), (-2.0, 5.0, 0.0),
    (3.0, 5.0, 0.0), (-3.0, 5.0, 0.0),
    (4.0, 5.0, 0.0), (-4.0, 5.0, 0.0),
)
ACTION_BODY_POSITIONS = (
    (0.0, 0.0, 0.0),
    (1.0, 5.0, 0.0), (-1.0, 5.0, 0.0),
    (2.0, 5.0, 0.0), (-1.7, 4.6, 0.5),
    (3.0, 5.0, 0.0), (-1.8, 3.5, 1.0),
    (4.0, 5.0, 0.0), (-1.2, 2.5, 1.5),
)


def body_skeleton(positions=NEUTRAL_BODY_POSITIONS):
    return mesh_gui.SkeletonHierarchy(
        source=Path("synthetic.skeleton"),
        name="synthetic",
        bone_names=BODY_BONES,
        bone_keys=BODY_BONES,
        bone_parents=(-1,) + (0,) * 8,
        bone_bind_transforms=tuple(
            (*position, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0)
            for position in positions
        ),
    )


def body_mesh(positions):
    matrices = [translation_matrix(*position) for position in positions]
    return mesh_gui.ParsedMesh(
        version=4,
        submeshes=[(len(positions), 0, 1, 0)],
        bone_parents=[-1] + [0] * 8,
        bone_names=list(BODY_BONES),
        bone_matrices=matrices,
        positions=list(positions),
        normals=[(0.0, 1.0, 0.0)] * len(positions),
        faces=[],
        uvs=[(0.0, 0.0)] * len(positions),
        joints=[(index, index, index, index) for index in range(len(positions))],
        weights=[(1.0, 0.0, 0.0, 0.0)] * len(positions),
    )


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
        self.assertEqual(mesh.bone_matrices[0], identity)
        self.assertEqual(mesh.bone_matrices[2], identity)
        self.assertEqual(mesh.bone_matrices[1][13], 1.0)

    def test_action_baked_mesh_is_restored_from_skeleton_bind(self):
        skeleton = body_skeleton()
        bind_globals = mesh_gui._skeleton_bind_global_matrices(skeleton)
        self.assertIsNotNone(bind_globals)
        mesh = body_mesh(ACTION_BODY_POSITIONS)

        self.assertTrue(mesh_gui._restore_mesh_bind_pose(mesh, skeleton))
        for actual, expected in zip(mesh.positions, NEUTRAL_BODY_POSITIONS):
            for component, target in zip(actual, expected):
                self.assertAlmostEqual(component, target, places=5)
        for actual, expected in zip(mesh.bone_matrices, bind_globals):
            for component, target in zip(actual, expected):
                self.assertAlmostEqual(component, target, places=5)

    def test_neutral_mesh_is_not_mapped_to_action_skeleton(self):
        skeleton = body_skeleton(ACTION_BODY_POSITIONS)
        mesh = body_mesh(NEUTRAL_BODY_POSITIONS)
        original_positions = list(mesh.positions)
        original_matrices = list(mesh.bone_matrices)

        self.assertFalse(mesh_gui._restore_mesh_bind_pose(mesh, skeleton))
        self.assertEqual(mesh.positions, original_positions)
        self.assertEqual(mesh.bone_matrices, original_matrices)

    def test_bind_restore_preserves_mesh_neutral_face_pose(self):
        neutral_body = body_skeleton()
        skeleton = mesh_gui.SkeletonHierarchy(
            source=Path("face.skeleton"),
            name="face",
            bone_names=BODY_BONES + ("kk_face", "lip"),
            bone_keys=BODY_BONES + ("kk_face", "lip"),
            bone_parents=neutral_body.bone_parents + (0, 9),
            bone_bind_transforms=neutral_body.bone_bind_transforms + (
                (0.0, 6.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0),
                (0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0),
            ),
        )
        mesh = body_mesh(ACTION_BODY_POSITIONS)
        neutral_face_position = (0.0, 5.9, 0.0)
        mesh.bone_names.extend(("kk_face", "lip"))
        mesh.bone_parents.extend((0, 9))
        mesh.bone_matrices.extend((
            translation_matrix(0.0, 5.8, 0.0),
            translation_matrix(0.0, 5.9, 0.0),
        ))
        mesh.positions.append(neutral_face_position)
        mesh.normals.append((0.0, 0.0, 1.0))
        mesh.uvs.append((0.0, 0.0))
        mesh.joints.append((10, 10, 10, 10))
        mesh.weights.append((1.0, 0.0, 0.0, 0.0))

        self.assertTrue(mesh_gui._restore_mesh_bind_pose(mesh, skeleton))
        self.assertEqual(mesh.positions[-1], neutral_face_position)
        self.assertEqual(
            mesh.bone_matrices[-1], translation_matrix(0.0, 5.9, 0.0)
        )


if __name__ == "__main__":
    unittest.main()
