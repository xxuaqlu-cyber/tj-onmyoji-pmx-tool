from __future__ import annotations

import unittest

import onmyoji_local_catalog as local_catalog
import pmx_role_classifier as classifier


class LocalCharacterCatalogTests(unittest.TestCase):
    def test_skin_names_and_show_models_are_exactly_joined(self) -> None:
        catalog = local_catalog.build_catalog_from_tables(
            hero_brief={600: {"id": 600, "name": "测试式神", "head": "hero_a"}},
            hero={
                600: {"protoid": 600, "name": "测试式神", "rarity": 4},
                1600: {"protoid": 1600, "name": "测试式神", "rarity": 4},
            },
            skin={
                600: {
                    "id": 600,
                    "skin": ["hero_a", "s2_hero_a"],
                    "name": ["默认", "中文皮肤名"],
                    "item": [-1, 123],
                },
                1600: {
                    "id": 1600,
                    "skin": ["legacy_hero_a", "s6_legacy_hero_a"],
                    "name": ["测试式神", "旧命名中文皮肤"],
                    "item": [-1, 456],
                },
            },
            character={
                "hero_a": {
                    "id": "hero_a", "name": "测试式神",
                    "modelId": "hero_a", "show_model": "hero_a_show",
                },
                "hero_a_alias": {
                    "id": "hero_a_alias", "name": "测试式神",
                    "modelId": "s2_hero_a",
                    "show_model": "dbo_s2_hero_a_show",
                },
                "s6_legacy_hero_a": {
                    "id": "s6_legacy_hero_a", "name": "测试式神",
                    "modelId": "s6_legacy_hero_a",
                    "show_model": "s6_legacy_hero_a_show",
                },
            },
        )

        match = catalog.resolve(["model_s2_hero_a_show.pmx", "s_s2_hero_a"])
        self.assertIsNone(match)
        match = catalog.resolve(["s2_hero_a"])
        self.assertIsNotNone(match)
        self.assertEqual(match.skin_name, "中文皮肤名")
        self.assertEqual(
            catalog.resolve(["dbo_s2_hero_a_show"]).model_id, "s2_hero_a"
        )
        legacy = catalog.resolve(["s6_legacy_hero_a_show"])
        self.assertEqual(legacy.hero_id, "600")
        self.assertEqual(legacy.skin_name, "旧命名中文皮肤")

    def test_api_only_rows_supplement_local_and_chinese_fixes_placeholder(self) -> None:
        local_rows = [
            classifier.CharacterMetadata("605", "luotianyi", "SSR", 4, "pic_ss_lty"),
            classifier.CharacterMetadata("600", "市加美", "SSR", 4, "pic_ss_sjm"),
        ]
        api_rows = [
            classifier.CharacterMetadata("605", "洛天依", "SSR", 4, "pic_ss_lty"),
            classifier.CharacterMetadata("608", "石长姬", "SSR", 4, "pic_ss_shchangji"),
        ]
        merged = classifier._merge_character_catalogs(local_rows, api_rows)
        by_id = {item.hero_id: item for item in merged}

        self.assertEqual(by_id["605"].name, "洛天依")
        self.assertEqual(by_id["600"].name, "市加美")
        self.assertEqual(by_id["608"].name, "石长姬")


if __name__ == "__main__":
    unittest.main()
