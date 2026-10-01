import tempfile
import unittest
from pathlib import Path

from onmyoji_rigged_mesh_gui import MaterialDefinition, ParsedMesh, save_obj


class SceneObjWriterTests(unittest.TestCase):
    def test_writes_geometry_material_and_texture_reference(self) -> None:
        mesh = ParsedMesh(
            version=2,
            submeshes=[(0, 1, 1, 0)],
            bone_parents=[-1],
            bone_names=["root"],
            bone_matrices=[
                (1.0, 0.0, 0.0, 0.0,
                 0.0, 1.0, 0.0, 0.0,
                 0.0, 0.0, 1.0, 0.0,
                 0.0, 0.0, 0.0, 1.0)
            ],
            positions=[(1.0, 2.0, 3.0), (0.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
            normals=[(0.0, 0.0, 1.0)] * 3,
            faces=[(0, 1, 2)],
            uvs=[(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)],
            joints=[(0, 0, 0, 0)] * 3,
            weights=[(1.0, 0.0, 0.0, 0.0)] * 3,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            texture = root / "diffuse.png"
            texture.write_bytes(b"png")
            save_obj(
                mesh,
                root / "scene.obj",
                "scene",
                [MaterialDefinition("ground", {"tex0": "diffuse.png"})],
                {"diffuse.png": texture},
            )

            obj = (root / "scene.obj").read_text(encoding="utf-8")
            mtl = (root / "scene.mtl").read_text(encoding="utf-8")
            self.assertIn("v -1 2 -3", obj)
            self.assertIn("f 1/1/1 2/2/2 3/3/3", obj)
            self.assertIn("usemtl ground", obj)
            self.assertIn("map_Kd diffuse.png", mtl)


if __name__ == "__main__":
    unittest.main()
