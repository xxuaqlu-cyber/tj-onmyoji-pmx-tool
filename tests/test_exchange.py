from __future__ import annotations

import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

from onmyoji_exchange import COLLADA_NS, write_animated_dae, write_binary_stl


class ExchangeFormatTests(unittest.TestCase):
    def test_binary_stl_contains_current_pose_triangles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "pose.stl"
            write_binary_stl(
                output,
                np.asarray(((0, 0, 0), (1, 0, 0), (0, 1, 0)), dtype=np.float32),
                np.asarray(((0, 1, 2),), dtype=np.int32),
            )
            payload = output.read_bytes()

        self.assertEqual(struct.unpack_from("<I", payload, 80)[0], 1)
        self.assertEqual(len(payload), 84 + 50)
        normal = struct.unpack_from("<3f", payload, 84)
        np.testing.assert_allclose(normal, (0.0, 0.0, 1.0), atol=1.0e-6)

    def test_dae_contains_skin_skeleton_and_matrix_animation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "animated.dae"
            bind = np.eye(4, dtype=np.float32)[None, :, :]
            frames = np.repeat(bind[None, :, :, :], 2, axis=0)
            frames[1, 0, 3, 0] = 1.0
            write_animated_dae(
                output,
                model_name="Test Model",
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
                bind_globals=bind,
                bind_locals=bind,
                frame_locals=frames,
                fps=30.0,
            )
            document = ET.parse(output)

        namespace = {"c": COLLADA_NS}
        root = document.getroot()
        self.assertEqual(root.attrib["version"], "1.4.1")
        self.assertIsNotNone(root.find(".//c:controller/c:skin", namespace))
        self.assertIsNotNone(root.find(".//c:node[@type='JOINT']", namespace))
        channel = root.find(".//c:animation/c:channel", namespace)
        self.assertIsNotNone(channel)
        self.assertEqual(channel.attrib["target"], "Bone_0000_root/transform")
        output_values = root.find(
            ".//c:source[@id='Bone_0000_root-animation-output']/c:float_array",
            namespace,
        )
        self.assertIsNotNone(output_values)
        values = np.fromstring(output_values.text or "", sep=" ")
        self.assertEqual(len(values), 32)
        self.assertAlmostEqual(values[16 + 12], 1.0)


if __name__ == "__main__":
    unittest.main()
