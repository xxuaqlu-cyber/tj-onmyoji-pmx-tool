from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from onmyoji_motion_bindings import OfficialMotionBindings


class OfficialMotionBindingsTests(unittest.TestCase):
    def test_only_direct_resource_group_meshes_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            motion = root / "unpacked" / "model" / "pkg_01" / "hero.rawanimation"
            motion.parent.mkdir(parents=True)
            motion.touch()
            bindings = OfficialMotionBindings(
                {
                    "pkg_01/hero.rawanimation": frozenset(
                        {"010000_hero.mesh"}
                    )
                },
                {},
                1,
            )
            model_root = root / "unpacked" / "model"
            self.assertTrue(
                bindings.matches_motion(motion, model_root, "010000_hero.mesh")
            )
            # Similar role/model text is intentionally not a fallback relation.
            self.assertFalse(
                bindings.matches_motion(
                    motion, model_root, "020000_hero_variant.mesh"
                )
            )


if __name__ == "__main__":
    unittest.main()
