from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np

from pmx_preview_gui import GpuPreviewRenderer, PreviewData


class GpuSkinningTests(unittest.TestCase):
    def test_vertex_shader_applies_row_vector_bone_matrix(self) -> None:
        try:
            renderer = GpuPreviewRenderer()
        except Exception as exc:
            self.skipTest(f"OpenGL 3.3 context unavailable: {exc}")
        positions = np.asarray(
            ((-0.5, -0.5, 0.0), (0.5, -0.5, 0.0), (0.0, 0.5, 0.0)),
            dtype=np.float32,
        )
        preview = PreviewData(
            path=Path("gpu-test.pmx"),
            positions=positions,
            triangles=np.asarray(((0, 1, 2),), dtype=np.int32),
            face_colors=np.zeros((1, 3), dtype=np.uint8),
            texture_paths=[],
            texture_pixels={},
            material_batches=[(0, 3, -1, (255, 255, 255))],
            primary_texture_indices=[],
            normals=np.zeros_like(positions),
            uvs=np.zeros((3, 2), dtype=np.float32),
            center=np.zeros(3, dtype=np.float32),
            radius=1.0,
            vertex_count=3,
            face_count=1,
            material_count=1,
        )
        renderer.prepare(preview)
        joints = np.zeros((3, 4), dtype=np.int32)
        weights = np.zeros((3, 4), dtype=np.float32)
        weights[:, 0] = 1.0
        renderer.prepare_skinning(joints, weights, 1)

        matrix = np.eye(4, dtype=np.float32)[None, :, :]
        renderer.update_bone_matrices(matrix)
        original = np.asarray(renderer.render(128, 128, 0, 0, 1, 0, 0, False))
        matrix[0, 3, 0] = 0.35
        renderer.update_bone_matrices(matrix)
        moved = np.asarray(renderer.render(128, 128, 0, 0, 1, 0, 0, False))

        original_x = np.nonzero(np.any(original != 0, axis=2))[1].mean()
        moved_x = np.nonzero(np.any(moved != 0, axis=2))[1].mean()
        self.assertGreater(moved_x - original_x, 10.0)


if __name__ == "__main__":
    unittest.main()
