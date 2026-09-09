from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

from onmyoji_fbx import decompose_row_matrices, write_animated_fbx
from onmyoji_motion import trs_row_matrix4


class AnimatedFbxTests(unittest.TestCase):
    def test_row_matrix_decomposition_preserves_trs(self) -> None:
        half = np.deg2rad(22.5)
        quaternion = np.asarray((0.0, np.sin(half), 0.0, np.cos(half)))
        matrix = trs_row_matrix4(
            np.concatenate(((1.0, 2.0, 3.0), quaternion, (2.0, 3.0, 4.0)))
        )
        translation, rotation, scaling = decompose_row_matrices(matrix)
        np.testing.assert_allclose(translation, (1.0, 2.0, 3.0), atol=1.0e-5)
        np.testing.assert_allclose(rotation, (0.0, 45.0, 0.0), atol=1.0e-4)
        np.testing.assert_allclose(scaling, (2.0, 3.0, 4.0), atol=1.0e-5)

    def test_binary_fbx_contains_valid_node_offsets_and_animation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "animated.fbx"
            identity = np.eye(4, dtype=np.float32)[None, :, :]
            translation = np.zeros((2, 1, 3), dtype=np.float32)
            translation[1, 0, 0] = 1.0
            rotation = np.zeros_like(translation)
            scaling = np.ones_like(translation)
            write_animated_fbx(
                output,
                model_name="Test",
                positions=np.asarray(((0, 0, 0), (1, 0, 0), (0, 1, 0))),
                normals=np.asarray(((0, 0, 1),) * 3),
                uvs=np.asarray(((0, 0), (1, 0), (0, 1))),
                triangles=np.asarray(((0, 1, 2),)),
                face_materials=np.asarray((0,)),
                material_defs=[{"name": "Material", "diffuse": (1, 1, 1, 1)}],
                joints=np.zeros((3, 4), dtype=np.int32),
                weights=np.asarray(((1, 0, 0, 0),) * 3),
                bone_names=("root",),
                parents=(-1,),
                bind_globals=identity,
                bind_translation=np.zeros((1, 3)),
                bind_rotation=np.zeros((1, 3)),
                bind_scaling=np.ones((1, 3)),
                frame_translation=translation,
                frame_rotation=rotation,
                frame_scaling=scaling,
                fps=30.0,
            )
            data = output.read_bytes()

        self.assertTrue(data.startswith(b"Kaydara FBX Binary  \x00\x1a\x00"))
        self.assertIn(b"AnimationStack", data)
        self.assertIn(b"AnimationCurve", data)
        self.assertIn(b"Deformer", data)

        def parse_node(offset: int, limit: int) -> int:
            end, _count, property_bytes, name_length = struct.unpack_from(
                "<IIIB", data, offset
            )
            self.assertGreater(end, offset)
            self.assertLessEqual(end, limit)
            cursor = offset + 13 + name_length + property_bytes
            while cursor < end:
                if data[cursor : cursor + 13] == b"\0" * 13:
                    cursor += 13
                    break
                cursor = parse_node(cursor, end)
            self.assertEqual(cursor, end)
            return end

        cursor = 27
        footer_start = len(data) - (16 + 120 + 4 + 4 + 16)
        while data[cursor : cursor + 13] != b"\0" * 13:
            cursor = parse_node(cursor, footer_start)
        self.assertEqual(cursor + 13, footer_start)


if __name__ == "__main__":
    unittest.main()
