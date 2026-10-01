from __future__ import annotations

import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from onmyoji_motion import DecodedMotion, MotionHeader
from onmyoji_motion_gui import MotionPreviewApp
import pmx_preview_gui as pmx_browser


class MotionCandidateTests(unittest.TestCase):
    def test_combined_export_recovers_primary_expanded_bind_layout(self) -> None:
        app = MotionPreviewApp.__new__(MotionPreviewApp)
        app.pmx_source_meshes = {}
        identity = np.eye(4, dtype=np.float32).reshape(16)
        expanded = SimpleNamespace(
            bone_names=('root', 'arm', 'cloth'), bone_parents=(-1, 0, 1),
            bone_matrices=(identity, identity, identity),
        )
        app.official_motion_bindings = SimpleNamespace(
            source_mesh_paths=lambda name: (Path(name),),
            source_skeleton_paths=lambda name: (),
        )
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'hero.pmx'
            (path.parent / '.build.json').write_text(
                '{"components": ["prop.mesh", "body.mesh"]}', encoding='utf-8'
            )
            with mock.patch('onmyoji_rigged_mesh_gui.read_mesh_bone_bind_layout', side_effect=[
                (('root',), (-1,), (identity,)),
                (('root', 'arm'), (-1, 0), (identity, identity)),
            ]), mock.patch('onmyoji_rigged_mesh_gui.parse_mesh_for_pmx', return_value=expanded) as parse:
                source, matrices, parents = app._source_mesh_layout_for_pmx(
                    path, ('cloth', 'root', 'arm')
                )
        self.assertEqual(source, Path('body.mesh'))
        self.assertEqual(parents, (2, -1, 1))
        self.assertEqual(matrices.shape, (3, 4, 4))
        parse.assert_called_once_with(Path('body.mesh'), None, expand_skeleton=True)

    def test_single_biped_official_skeleton_uses_absolute_pose(self) -> None:
        app = MotionPreviewApp.__new__(MotionPreviewApp)
        names = ('root', 'bip01_arm')
        header = MotionHeader(Path('walk.rawanimation'), 0, 'hero', 'walk', names, 30, 1)
        frames = np.zeros((1, 2, 10), dtype=np.float32)
        frames[:, :, 6] = 1
        frames[:, :, 7:10] = 1
        frames[0, 1, 0] = 3
        app.motion = DecodedMotion(header, frames, 30, 1, False)
        app.motion_bind_transforms = frames[0].copy()
        app.parents = (-1, 0)
        app.skin = {'bone_names': names, 'mesh_path': Path('hero.mesh')}
        app.official_motion_bindings = SimpleNamespace(source_skeleton_paths=lambda name: (Path('hero.skeleton'),))
        with mock.patch('onmyoji_rigged_mesh_gui.read_skeleton_hierarchy', return_value=SimpleNamespace(name='hero')):
            self.assertTrue(app._uses_source_skeleton(np.array((0, 1))))
        identity = np.repeat(np.eye(4, dtype=np.float32)[None], 2, axis=0)
        app.skin.update(mesh_bind_matrices=identity, mapping=np.array((0, 1)), parents=(-1, 0), absolute_source_pose=True)
        _bind, posed = app._matrix_target_global_poses(0)
        self.assertAlmostEqual(float(posed[1, 3, 0]), -3)

    def test_absolute_body_keeps_facial_mesh_reference(self) -> None:
        app = MotionPreviewApp.__new__(MotionPreviewApp)
        header = MotionHeader(Path('idle.rawanimation'), 0, 'hero', 'idle', ('root', 'face'), 30, 1)
        frames = np.zeros((2, 2, 10), dtype=np.float32)
        frames[:, :, 6] = 1
        frames[:, :, 7:10] = 1
        frames[:, 0, 0] = 3
        frames[:, 1, 0] = (7, 8)
        app.motion = DecodedMotion(header, frames, 30, 1, False)
        app.motion_bind_transforms = frames[0].copy()
        app.parents = (-1, 0)
        bind = np.repeat(np.eye(4, dtype=np.float32)[None], 2, axis=0)
        bind[1, 3, 0] = 2
        app.skin = dict(mesh_bind_matrices=bind, mapping=np.array((0, 1)), parents=(-1, 0), absolute_source_pose=True, facial_source_indices=(1,))
        _bind, first = app._matrix_target_global_poses(0)
        _bind, second = app._matrix_target_global_poses(1)
        self.assertAlmostEqual(float(first[1, 3, 0]), -5)
        self.assertAlmostEqual(float(second[1, 3, 0]), -6)

    def test_action_list_uses_only_current_models_catalog(self) -> None:
        associated = MotionHeader(
            Path("associated.rawanimation"), 0, "hero.skeleton", "idle",
            ("root", "body", "arm_l", "arm_r"), 30.0, 1.0,
        )
        unrelated = MotionHeader(
            Path("unrelated.rawanimation"), 0, "other.skeleton", "run",
            ("root", "body", "arm_l", "arm_r"), 30.0, 1.0,
        )

        class TreeStub:
            def __init__(self) -> None:
                self.rows: list[tuple[object, ...]] = []

            def get_children(self) -> tuple[object, ...]:
                return ()

            def delete(self, *_children) -> None:
                pass

            def insert(self, _parent, _where, **kwargs) -> None:
                self.rows.append(kwargs["values"])

        app = MotionPreviewApp.__new__(MotionPreviewApp)
        app.headers = [associated, unrelated]
        app.model_headers = [associated]
        app.search_var = SimpleNamespace(get=lambda: "")
        app.tree = TreeStub()
        app.skin = {"bone_names": associated.bone_names}
        app.action_count_label = SimpleNamespace(configure=lambda **_kwargs: None)

        app._apply_filter()

        self.assertEqual(app.visible_headers, [associated])
        self.assertEqual(len(app.tree.rows), 1)

    def test_multi_selection_does_not_reload_motion(self) -> None:
        first = MotionHeader(
            Path("first.rawanimation"), 0, "hero.skeleton", "idle",
            ("root", "body", "arm_l", "arm_r"), 30.0, 1.0,
        )
        second = MotionHeader(
            Path("second.rawanimation"), 0, "hero.skeleton", "run",
            ("root", "body", "arm_l", "arm_r"), 30.0, 1.0,
        )

        app = MotionPreviewApp.__new__(MotionPreviewApp)
        app.batch_export_state = None
        app.selected_export_state = None
        app.visible_headers = [first, second]
        app.tree = SimpleNamespace(selection=lambda: ("0", "1"))
        messages: list[str] = []
        app.status_var = SimpleNamespace(set=messages.append)
        loaded: list[Path] = []
        app._load_path = loaded.append

        app._tree_selected()

        self.assertEqual(loaded, [])
        self.assertIn("2", messages[-1])

    def test_action_list_can_show_all_catalog_motions(self) -> None:
        associated = MotionHeader(
            Path("associated.rawanimation"), 0, "hero.skeleton", "idle",
            ("root", "body", "arm_l", "arm_r"), 30.0, 1.0,
        )
        unrelated = MotionHeader(
            Path("unrelated.rawanimation"), 0, "other.skeleton", "run",
            ("root", "body", "leg_l", "leg_r"), 30.0, 1.0,
        )

        class TreeStub:
            def __init__(self) -> None:
                self.rows: list[tuple[object, ...]] = []

            def get_children(self) -> tuple[object, ...]:
                return ()

            def delete(self, *_children) -> None:
                pass

            def insert(self, _parent, _where, **kwargs) -> None:
                self.rows.append(kwargs["values"])

        app = MotionPreviewApp.__new__(MotionPreviewApp)
        app.headers = [associated, unrelated]
        app.model_headers = [associated]
        app.matching_only_var = SimpleNamespace(get=lambda: False)
        app.search_var = SimpleNamespace(get=lambda: "")
        app.tree = TreeStub()
        app.skin = {"bone_names": associated.bone_names}
        app.action_count_label = SimpleNamespace(configure=lambda **_kwargs: None)

        app._apply_filter()

        self.assertEqual(app.visible_headers, [associated, unrelated])
        self.assertEqual(len(app.tree.rows), 2)

    def test_multi_biped_preview_uses_absolute_source_pose(self) -> None:
        header = MotionHeader(
            Path("motion.rawanimation"), 0, "hero.skeleton", "idle",
            ("root",), 30.0, 0.0,
        )
        frames = np.zeros((1, 1, 10), dtype=np.float32)
        frames[0, 0, 0] = 3.0
        frames[0, 0, 6] = 1.0
        frames[0, 0, 7:10] = 1.0
        app = MotionPreviewApp.__new__(MotionPreviewApp)
        app.motion = DecodedMotion(header, frames, 30.0, 0.0, False)
        app.motion_bind_transforms = frames[0].copy()
        app.parents = (-1,)
        identity = np.eye(4, dtype=np.float32)[None, :, :]
        app.skin = {
            "mesh_bind_matrices": identity,
            "mapping": np.asarray((0,), dtype=np.int32),
            "parents": (-1,),
            "absolute_source_pose": True,
            "bind_pmx": identity,
        }

        bind, current = app._matrix_target_global_poses(0)

        np.testing.assert_allclose(bind, identity)
        self.assertAlmostEqual(float(current[0, 3, 0]), -3.0)

    def test_mesh_bind_matrix_path_works_without_skeleton_bind(self) -> None:
        header = MotionHeader(
            Path("motion.rawanimation"), 0, "hero.skeleton", "idle",
            ("root", "arm"), 30.0, 1.0,
        )
        frames = np.zeros((2, 2, 10), dtype=np.float32)
        frames[:, :, 6] = 1.0
        frames[:, :, 7:10] = 1.0
        frames[0, 0, 0:3] = (0.0, 2.0, 0.0)
        frames[:, 1, 0:3] = (2.0, 0.0, 0.0)
        # A 90-degree Z rotation on the child in the second frame.
        frames[1, 1, 5:7] = (np.sqrt(0.5), np.sqrt(0.5))

        app = MotionPreviewApp.__new__(MotionPreviewApp)
        app.motion = DecodedMotion(header, frames, 30.0, 1.0 / 30.0, False)
        app.motion_bind_transforms = None
        app.parents = (-1, 0)
        target_bind = np.zeros((2, 4, 4), dtype=np.float32)
        target_bind[:, :, :] = np.eye(4, dtype=np.float32)
        target_bind[0, 3, 1] = 2.0
        target_bind[1, 3, 0] = 2.0
        app.skin = {
            "mesh_bind_matrices": target_bind,
            "mapping": np.asarray((0, 1), dtype=np.int32),
            "parents": (-1, 0),
        }

        bind, first = app._matrix_target_global_poses(0)
        np.testing.assert_allclose(bind, first, atol=1.0e-5)

        _bind, posed = app._matrix_target_global_poses(1)
        self.assertGreater(float(np.linalg.norm(posed[1, 3, :3] - first[1, 3, :3])), 0.5)

    def test_skin_variant_prefix_matches_the_underlying_skeleton_name(self) -> None:
        model_keys = MotionPreviewApp._model_identity_keys(
            "s6_sp_huaniaojuan_默认组件"
        )
        skeleton_keys = MotionPreviewApp._model_identity_keys("sp_huaniaojuan")

        self.assertTrue(model_keys & skeleton_keys)

    def test_exact_skin_identity_outranks_shared_character_family(self) -> None:
        self.assertEqual(
            MotionPreviewApp._model_identity_match_level(
                "s1_sp_mianlingqi_默认组件_water", "s1_sp_mianlingqi.skeleton"
            ),
            2,
        )
        self.assertEqual(
            MotionPreviewApp._model_identity_match_level(
                "s2_sp_mianlingqi", "s1_sp_mianlingqi.skeleton"
            ),
            1,
        )
        self.assertEqual(
            MotionPreviewApp._model_identity_match_level(
                "s1_sp_mianlingqi_默认组件_water", "sp_mianlingqi_show.skeleton"
            ),
            1,
        )
        self.assertEqual(
            MotionPreviewApp._model_identity_match_level(
                "s2_sp_mianlingqi_show", "s2_sp_mianlingqi.skeleton"
            ),
            1,
        )

    def test_auto_match_prefers_s1_model_over_s2_family_fallback(self) -> None:
        root = Path("unpacked/model")
        app = MotionPreviewApp.__new__(MotionPreviewApp)
        app.motion = SimpleNamespace(
            header=MotionHeader(
                root / "s1.rawanimation", 0, "s1_sp_mianlingqi.skeleton", "idle",
                ("root", "body", "fx_a", "fx_b"), 30.0, 1.0,
            )
        )
        app.official_motion_bindings = None
        s1 = pmx_browser.PreviewItem(Path("s1_sp_mianlingqi.pmx"), "test")
        s2 = pmx_browser.PreviewItem(Path("s2_sp_mianlingqi.pmx"), "test")

        self.assertGreater(app._pmx_match_score(s1), app._pmx_match_score(s2))

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

    def test_awakened_variant_reuses_base_character_motion_with_extra_accessory_bones(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pmx_path = root / "j_lixiaolang.pmx"
            app = MotionPreviewApp.__new__(MotionPreviewApp)
            app.root_var = SimpleNamespace(get=lambda: str(root))
            app.pmx_var = SimpleNamespace(get=lambda: str(pmx_path))
            app.pmx_source_meshes = {}
            app.official_motion_bindings = None
            motion_bones = tuple(f"bone_{index}" for index in range(100))
            target_bones = motion_bones[:95] + tuple(
                f"accessory_{index}" for index in range(25)
            )
            app.skin = {"bone_names": target_bones}
            app.headers = [
                MotionHeader(
                    root / "idle.rawanimation", 0, "lixiaolang.skeleton", "idle",
                    motion_bones, 30.0, 1.0,
                )
            ]

            matches = app._matching_headers_for_current_pmx()

            self.assertEqual(matches, app.headers)

if __name__ == "__main__":
    unittest.main()
