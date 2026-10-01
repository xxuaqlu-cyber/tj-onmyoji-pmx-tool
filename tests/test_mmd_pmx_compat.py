from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pymeshio.common as common
import pymeshio.pmx as pmx
import pymeshio.pmx.reader
import pymeshio.pmx.writer

import mmd_pmx_compat as compat
import pmx_preview_gui as preview


class MmdPmxCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _write_biped(path: Path) -> None:
        model = pmx.Model(name="test", english_name="test")
        model.bones.clear()
        model.materials.clear()
        model.display_slots.clear()
        rows = (
            ("virtual_bone", -1, (0, 0, 0)),
            ("bip01", 0, (0, 10, 0)),
            ("bip01_pelvis", 1, (0, 10, 0)),
            ("bip01_spine", 2, (0, 12, 0)),
            ("bip01_spine1", 3, (0, 14, 0)),
            ("bip01_neck", 4, (0, 16, 0)),
            ("bip01_head", 5, (0, 17, 0)),
            ("bip01_l_clavicle", 4, (1, 15, 0)),
            ("bip01_l_upperarm", 7, (2, 15, 0)),
            ("bip01_l_forearm", 8, (4, 14, 0)),
            ("bip01_l_hand", 9, (6, 14, 0)),
            ("bip01_r_clavicle", 4, (-1, 15, 0)),
            ("bip01_r_upperarm", 11, (-2, 15, 0)),
            ("bip01_r_forearm", 12, (-4, 14, 0)),
            ("bip01_r_hand", 13, (-6, 14, 0)),
            ("bip01_l_thigh", 2, (1, 10, 0)),
            ("bip01_l_calf", 15, (1, 6, 0.2)),
            ("bip01_l_foot", 16, (1, 1, 0.5)),
            ("bip01_l_toe0", 17, (1, 0.5, -1)),
            ("bip01_r_thigh", 2, (-1, 10, 0)),
            ("bip01_r_calf", 19, (-1, 6, 0.2)),
            ("bip01_r_foot", 20, (-1, 1, 0.5)),
            ("bip01_r_toe0", 21, (-1, 0.5, -1)),
            ("bip01_l_finger1", 10, (6.2, 14, 0)),
            ("bip01_l_finger11", 23, (6.5, 14, 0)),
            ("wing_root", 0, (0, 16.2, 1.7)),
            ("wing_tip", 25, (8, 20, 4)),
        )
        flags = (
            pmx.BONEFLAG_CAN_ROTATE
            | pmx.BONEFLAG_IS_VISIBLE
            | pmx.BONEFLAG_CAN_MANIPULATE
        )
        for name, parent, position in rows:
            model.bones.append(
                pmx.Bone(
                    name,
                    name,
                    common.Vector3(*position),
                    parent,
                    0,
                    flags,
                )
            )
        model.display_slots.append(pmx.DisplaySlot("Root", "Root", 1, [(0, 0)]))
        model.vertices.append(
            pmx.Vertex(
                common.Vector3(8.0, 20.0, 4.0),
                common.Vector3(0.0, 1.0, 0.0),
                common.Vector2(0.0, 0.0),
                pmx.Bdef1(26),
                1.0,
            )
        )
        pymeshio.pmx.writer.write_to_file(model, str(path))

    def test_standardizes_names_hierarchy_and_leg_ik(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.pmx"
            output = Path(temporary) / "output.pmx"
            self._write_biped(source)

            report = compat.convert_pmx_to_mmd_compatible(source, output)
            model = pymeshio.pmx.reader.read_from_file(str(output))
            by_name = {bone.name: (index, bone) for index, bone in enumerate(model.bones)}

            for name in (
                "全ての親", "センター", "グルーブ", "腰", "下半身", "上半身", "上半身2",
                "左腕", "左ひじ", "左手首", "左足", "左ひざ", "左足首",
                "右腕", "右ひじ", "右手首", "右足", "右ひざ", "右足首",
                "左足ＩＫ", "右足ＩＫ", "左つま先ＩＫ", "右つま先ＩＫ",
                "左人指１", "左人指２",
            ):
                self.assertIn(name, by_name)
            self.assertEqual(report.missing_standard_bones, [])

            master_index = by_name["全ての親"][0]
            center_index, center = by_name["センター"]
            groove_index, groove = by_name["グルーブ"]
            waist_index, waist = by_name["腰"]
            self.assertEqual(center.parent_index, master_index)
            self.assertEqual(groove.parent_index, center_index)
            self.assertEqual(waist.parent_index, groove_index)
            self.assertEqual(by_name["下半身"][1].parent_index, waist_index)
            self.assertEqual(by_name["上半身"][1].parent_index, waist_index)
            self.assertTrue(center.getTranslatable())

            left_ik = by_name["左足ＩＫ"][1]
            self.assertTrue(left_ik.getIkFlag())
            self.assertTrue(left_ik.getTranslatable())
            self.assertEqual(left_ik.ik.target_index, by_name["左足首"][0])
            self.assertEqual(
                [link.bone_index for link in left_ik.ik.link],
                [by_name["左ひざ"][0], by_name["左足"][0]],
            )
            self.assertEqual(by_name["wing_root"][1].parent_index, by_name["上半身2"][0])
            self.assertEqual(report.reattached_bones, {"wing_root": "上半身2"})

            frame_names = [slot.name for slot in model.display_slots]
            self.assertEqual(frame_names[:2], ["Root", "表情"])
            self.assertIn("足", frame_names)

    def test_folder_export_keeps_source_untouched_and_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            source_pmx = source / "model.pmx"
            self._write_biped(source_pmx)
            original = source_pmx.read_bytes()

            target, dds_count = preview.copy_model_folder(
                source,
                root / "exports",
                folder_name="通用模型",
                mmd_compatible=True,
            )

            self.assertEqual(dds_count, 0)
            self.assertEqual(source_pmx.read_bytes(), original)
            self.assertTrue((target / compat.REPORT_NAME).is_file())
            payload = json.loads((target / compat.REPORT_NAME).read_text(encoding="utf-8"))
            self.assertEqual(payload["format"], "MMD通用型PMX")
            exported = pymeshio.pmx.reader.read_from_file(str(target / "model.pmx"))
            self.assertIn("センター", {bone.name for bone in exported.bones})


if __name__ == "__main__":
    unittest.main()
