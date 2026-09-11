from __future__ import annotations

import unittest

import moba_expk
import onmyoji_rigged_mesh_gui as rigged


class ArenaResourceTests(unittest.TestCase):
    def test_gb2312_neox_xml_is_decoded_before_elementtree_parse(self) -> None:
        text = (
            '<?xml version="1.0" encoding="gb2312"?>'
            '<NeoX><MaterialGroup Name="测试"/></NeoX>'
        )
        root = rigged._parse_neox_xml_bytes(text.encode("gb2312"))
        self.assertEqual(root.tag, "NeoX")
        self.assertEqual(root.find("MaterialGroup").get("Name"), "测试")

    def test_murmur_path_hash_seed_round_trip(self) -> None:
        reference = "hero/1110_guitongwan/1110_d.tga"
        for seed in (0, 333, 4961, 0x12345678, 0xFFFFFFFF):
            value = moba_expk.murmur3_path_hash(reference, seed)
            self.assertEqual(
                moba_expk.recover_murmur3_seed(reference, value),
                seed,
            )

    def test_arena_hero_family_is_read_from_texture_reference(self) -> None:
        self.assertEqual(
            rigged._moba_reference_family(
                r"hero\1110_guitongwan\1110_weapon_d.tga"
            ),
            "1110",
        )
        self.assertEqual(
            rigged._moba_reference_family("model/s2_guiqie/s2_guiqie.png"),
            "",
        )


if __name__ == "__main__":
    unittest.main()
