from __future__ import annotations

import struct
import tempfile
import unittest
import base64
from pathlib import Path

import numpy as np

from onmyoji_motion import (
    AnimationMetadata,
    DecodedMotion,
    _linear_interpolation,
    compose_global_positions,
    compose_global_row_matrices,
    cp932_field,
    find_animation_metadata,
    inverse_affine_row_matrix4,
    matrix4_multiply,
    normalized_bone_name,
    trs_row_matrix4,
    quaternion_delta,
    read_motion_header,
    skeleton_display_mask,
    trim_motion_to_animation_metadata,
)
from onmyoji_rigged_mesh_gui import read_skeleton_hierarchy


class MotionHeaderTests(unittest.TestCase):
    def test_reads_rawanima_v0_metadata_and_names(self) -> None:
        skeleton = b"../hero_test.skeleton"
        names = (b"idle", b"root", b"child")
        name_payload = struct.pack("<I", len(names)) + b"".join(
            struct.pack("<I", len(value)) + value for value in names
        )
        body = (
            b"RAWANIMA"
            + b"\0" * 8
            + struct.pack("<II", 0, 32)
            + b"\0" * 16
            + struct.pack("<I", len(skeleton))
            + skeleton
            + b"HEAD"
            + struct.pack("<Iff", 32, 60.0, 2.5)
            + b"\0" * 24
            + b"DATA\0\0\0\0"
            + b"NAME"
            + struct.pack("<I", len(name_payload))
            + name_payload
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.rawanimation"
            path.write_bytes(body)
            header = read_motion_header(path)
        self.assertEqual(header.version, 0)
        self.assertEqual(header.skeleton_name, "hero_test")
        self.assertEqual(header.action, "idle")
        self.assertEqual(header.bone_names, ("root", "child"))
        self.assertEqual(header.sample_rate, 60.0)
        self.assertEqual(header.duration, 2.5)

    def test_bone_name_normalization_and_vmd_byte_limit(self) -> None:
        self.assertEqual(normalized_bone_name("Bip01_L-Forearm"), "bip01lforearm")
        self.assertEqual(len(cp932_field("很长的骨骼名称", 15)), 15)
        interpolation = _linear_interpolation()
        self.assertEqual(len(interpolation), 64)
        self.assertEqual(interpolation[:16], bytes((20,) * 8 + (107,) * 8))

    def test_finds_xml_by_action_and_exact_joint_collection(self) -> None:
        header = read_motion_header
        # Build a header directly so this test covers XML association only.
        from onmyoji_motion import MotionHeader

        motion_header = MotionHeader(
            Path("hero.rawanimation"), 0, "hero.skeleton", "skill", ("root", "hand"), 30.0, 1.0
        )
        poses = np.zeros((2, 10), dtype="<f4")
        poses[:, 6] = 1.0
        poses[:, 7:10] = 1.0
        encoded = base64.b64encode(poses.tobytes()).decode("ascii")
        xml = f'''<NeoX><Name Name="skill"/><Property ExtractedJointIndex="-1" TranslationMode="None"/>
<CachedPose><JointNames Value="hand,root"/><CachedPoseTrack><Pose Time="0" Value="{encoded}"/></CachedPoseTrack></CachedPose></NeoX>'''
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one_skill_Animation_a.xml").write_text(xml, encoding="utf-8")
            # Same action but a partial rig must not be treated as authoritative.
            (root / "two_skill_Animation_b.xml").write_text(
                xml.replace("hand,root", "hand,other"), encoding="utf-8"
            )
            metadata = find_animation_metadata(root, motion_header)
        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(metadata.cached_poses.shape, (1, 2, 10))
        self.assertEqual(metadata.cached_pose_times, (0.0,))
        # XML was hand/root, RAWANIMA was root/hand: parser must reorder.
        self.assertEqual(metadata.joint_names, ("hand", "root"))
        self.assertEqual(metadata.property_map["ExtractedJointIndex"], "-1")

    def test_trims_only_the_xml_verified_rawanimation_preroll(self) -> None:
        from onmyoji_motion import MotionHeader

        header = MotionHeader(
            Path("hero.rawanimation"), 0, "hero.skeleton", "skill", ("root", "hand"), 30.0, 0.2
        )
        frames = np.zeros((7, 2, 10), dtype=np.float32)
        frames[:, :, 6] = 1.0
        frames[:, :, 7:10] = 1.0
        frames[:, :, 0] = np.arange(7, dtype=np.float32)[:, None] * 0.5
        motion = DecodedMotion(header, frames, 30.0, 0.2, False)
        metadata = AnimationMetadata(
            Path("skill_Animation.xml"),
            "skill",
            ("root", "hand"),
            (("StartTime", "0"), ("EndTime", "0.1")),
            (0.0, 1.0 / 30.0),
            np.asarray((frames[3], frames[4])),
        )
        trimmed, alignment = trim_motion_to_animation_metadata(motion, metadata)
        self.assertIsNotNone(alignment)
        assert alignment is not None
        self.assertEqual(alignment.start_frame, 3)
        self.assertEqual(trimmed.sample_count, 4)
        np.testing.assert_allclose(trimmed.frames, frames[3:])

    def test_does_not_trim_an_ambiguous_cached_pose(self) -> None:
        from onmyoji_motion import MotionHeader

        header = MotionHeader(
            Path("hero.rawanimation"), 0, "hero.skeleton", "idle", ("root",), 30.0, 0.2
        )
        frames = np.zeros((7, 1, 10), dtype=np.float32)
        frames[:, :, 6] = 1.0
        frames[:, :, 7:10] = 1.0
        motion = DecodedMotion(header, frames, 30.0, 0.2, False)
        metadata = AnimationMetadata(
            Path("idle_Animation.xml"),
            "idle",
            ("root",),
            (("StartTime", "0"), ("EndTime", "0.1")),
            (0.0,),
            frames[:1].copy(),
        )
        unchanged, alignment = trim_motion_to_animation_metadata(motion, metadata)
        self.assertIsNone(alignment)
        self.assertIs(unchanged, motion)

    def test_row_vector_bind_skin_matrix_keeps_bind_pose_stable(self) -> None:
        bind = np.eye(4, dtype=np.float32)
        bind[3, 0] = 5.0
        point = np.asarray((1.0, 2.0, 3.0, 1.0), dtype=np.float32)
        identity_skin = matrix4_multiply(inverse_affine_row_matrix4(bind), bind)
        np.testing.assert_allclose(point @ identity_skin, point, atol=1.0e-6)
        current = bind.copy()
        current[3, 0] = 8.0
        moved = point @ matrix4_multiply(inverse_affine_row_matrix4(bind), current)
        np.testing.assert_allclose(moved[:3], (4.0, 2.0, 3.0), atol=1.0e-6)

    def test_row_vector_global_composition_preserves_parent_bind(self) -> None:
        local = np.zeros((2, 10), dtype=np.float32)
        local[:, 6] = 1.0
        local[:, 7:10] = 1.0
        local[0, :3] = (3.0, 0.0, 0.0)
        local[1, :3] = (0.0, 2.0, 0.0)
        matrices = compose_global_row_matrices(local, (-1, 0))
        np.testing.assert_allclose(matrices[0], trs_row_matrix4(local[0]))
        np.testing.assert_allclose(matrices[1][3, :3], (3.0, 2.0, 0.0))


class SkeletonCompositionTests(unittest.TestCase):
    def test_reads_two_byte_aligned_skeleton_bind_transforms(self) -> None:
        """Character Skeletons place their TRS data two bytes after parents."""
        transforms = (
            (1.0, 2.0, 3.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0),
            (0.0, 4.0, 0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0),
        )
        data_marker = 8
        parent_offset = data_marker + 16 + len(transforms) * 4
        bind_offset = parent_offset + len(transforms) * 2 + 2
        names = ("hero", "root", "child")
        name_payload = b"".join(
            struct.pack("<I", len(name.encode("utf-8"))) + name.encode("utf-8")
            for name in names
        )
        name_offset = bind_offset + len(transforms) * 10 * 4
        data = bytearray(name_offset)
        data[:8] = b"SKELETON"
        data[data_marker : data_marker + 4] = b"DATA"
        struct.pack_into("<HH", data, parent_offset, 0xFFFF, 0)
        struct.pack_into("<20f", data, bind_offset, *(value for row in transforms for value in row))
        data.extend(b"NAME" + struct.pack("<II", 0, len(names)) + name_payload)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "hero.skeleton"
            path.write_bytes(data)
            hierarchy = read_skeleton_hierarchy(path)
        self.assertIsNotNone(hierarchy)
        assert hierarchy is not None
        self.assertEqual(hierarchy.bone_parents, (-1, 0))
        self.assertEqual(hierarchy.bone_bind_transforms, transforms)

    def test_child_translation_is_composed_with_parent(self) -> None:
        transforms = np.zeros((2, 10), dtype=np.float32)
        transforms[:, 6] = 1.0
        transforms[:, 7:10] = 1.0
        transforms[0, :3] = (10.0, 0.0, 0.0)
        transforms[1, :3] = (0.0, 5.0, 0.0)
        positions = compose_global_positions(transforms, (-1, 0))
        np.testing.assert_allclose(positions, ((-10.0, 0.0, 0.0), (-10.0, 5.0, 0.0)))

    def test_detached_helper_branch_is_hidden_from_preview(self) -> None:
        positions = np.asarray(
            ((0, 0, 0), (0, 1, 0), (0, 2, 0), (0, 3, 0),
             (1, 2, 0), (-1, 2, 0), (0, 300, 0), (0, 301, 0)),
            dtype=np.float32,
        )
        parents = (-1, 0, 1, 2, 2, 2, 0, 6)
        mask = skeleton_display_mask(positions, parents)
        self.assertTrue(mask[:6].all())
        self.assertFalse(mask[6:].any())

    def test_quaternion_delta_keeps_reference_pose_at_identity(self) -> None:
        reference = np.asarray((0.2, -0.3, 0.1, 0.92736185), dtype=np.float32)
        reference /= np.linalg.norm(reference)
        delta = quaternion_delta(reference, reference)
        np.testing.assert_allclose(delta, (0.0, 0.0, 0.0, 1.0), atol=1.0e-6)


if __name__ == "__main__":
    unittest.main()
