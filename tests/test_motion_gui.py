from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from onmyoji_motion import MotionHeader
from onmyoji_motion_gui import MotionPreviewApp


class MotionCandidateTests(unittest.TestCase):
    def test_skin_variant_prefix_matches_the_underlying_skeleton_name(self) -> None:
        model_keys = MotionPreviewApp._model_identity_keys(
            "s6_sp_huaniaojuan_默认组件"
        )
        skeleton_keys = MotionPreviewApp._model_identity_keys("sp_huaniaojuan")

        self.assertTrue(model_keys & skeleton_keys)

    def test_same_bone_count_without_matching_bones_is_not_a_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pmx_path = root / "s1_sp_huaniaojuan.pmx"
            app = MotionPreviewApp.__new__(MotionPreviewApp)
            app.root_var = SimpleNamespace(get=lambda: str(root))
            app.pmx_var = SimpleNamespace(get=lambda: str(pmx_path))
            app.pmx_source_meshes = {}
            app.official_motion_bindings = None
            app.skin = {
                "bone_names": ("root", "body", "arm_l", "arm_r"),
            }
            compatible = MotionHeader(
                root / "good.rawanimation", 0,
                "s1_sp_huaniaojuan.skeleton", "idle",
                ("root", "body", "arm_l", "arm_r"), 30.0, 1.0,
            )
            wrong_same_count = MotionHeader(
                root / "wrong.rawanimation", 0,
                "other_character.skeleton", "idle",
                ("root", "wing", "tail", "weapon"), 30.0, 1.0,
            )
            app.headers = [compatible, wrong_same_count]

            matches = app._matching_headers_for_current_pmx()

            self.assertEqual(matches, [compatible])


if __name__ == "__main__":
    unittest.main()
