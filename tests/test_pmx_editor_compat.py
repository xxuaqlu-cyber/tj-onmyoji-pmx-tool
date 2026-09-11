from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pymeshio.pmx as pmx
import pymeshio.pmx.reader
import pymeshio.pmx.writer

from pmx_editor_compat import (
    duplicate_texture_paths,
    read_pmx_texture_paths,
    repair_duplicate_texture_paths,
)


class PmxEditorCompatibilityTests(unittest.TestCase):
    def test_duplicate_texture_paths_are_deduplicated_and_references_remapped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "duplicate.pmx"
            model = pmx.Model(name="test", english_name="test")
            model.textures[:] = ["textures\\same.png", "textures/same.png", "other.png"]
            material = model.materials[0]
            material.texture_index = 1
            material.sphere_texture_index = 2
            material.toon_sharing_flag = 0
            material.toon_texture_index = 1
            pymeshio.pmx.writer.write_to_file(model, str(path))

            self.assertEqual(
                duplicate_texture_paths(path), ("textures/same.png",)
            )
            self.assertEqual(repair_duplicate_texture_paths(path), 1)
            self.assertEqual(
                read_pmx_texture_paths(path),
                ("textures\\same.png", "other.png"),
            )
            repaired = pymeshio.pmx.reader.read_from_file(str(path))
            self.assertEqual(repaired.materials[0].texture_index, 0)
            self.assertEqual(repaired.materials[0].sphere_texture_index, 1)
            self.assertEqual(repaired.materials[0].toon_texture_index, 0)


if __name__ == "__main__":
    unittest.main()
