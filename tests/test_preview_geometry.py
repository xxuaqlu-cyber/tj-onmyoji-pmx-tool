from pathlib import Path
from types import SimpleNamespace
import unittest

import numpy as np

from pmx_preview_gui import load_preview


class PreviewGeometryTests(unittest.TestCase):
    def test_large_mesh_preserves_every_face_and_material_boundary(self):
        # Cross the old 200,000-face limit. Include an empty material between
        # two surfaces so visibility must use PMX material IDs, not draw order.
        count = 200_002
        faces = np.tile(np.asarray([[0, 1, 2], [0, 2, 3]]), (count // 2, 1))
        vertices = [
            SimpleNamespace(
                position=SimpleNamespace(x=x, y=y, z=0),
                normal=SimpleNamespace(x=0, y=0, z=1),
                uv=SimpleNamespace(x=x, y=y),
            )
            for x, y in ((0, 0), (1, 0), (1, 1), (0, 1))
        ]
        materials = [
            SimpleNamespace(name=name, vertex_count=face_count * 3,
                            texture_index=-1, diffuse_color=None)
            for name, face_count in (("body", 100_000), ("empty", 0),
                                     ("accessory", count - 100_000))
        ]
        model = SimpleNamespace(vertices=vertices, indices=faces.ravel(),
                                materials=materials, textures=[])
        preview = load_preview(Path("large.pmx"), model=model)
        np.testing.assert_array_equal(preview.triangles, faces)
        self.assertEqual(len(preview.triangles), count)
        self.assertEqual(preview.face_count, count)
        self.assertEqual(sum(batch[1] for batch in preview.material_batches), count * 3)
        self.assertEqual(preview.material_batch_indices, (0, 2))
        np.testing.assert_array_equal(preview.face_materials[:100_000], 0)
        np.testing.assert_array_equal(preview.face_materials[100_000:], 2)


if __name__ == "__main__":
    unittest.main()
