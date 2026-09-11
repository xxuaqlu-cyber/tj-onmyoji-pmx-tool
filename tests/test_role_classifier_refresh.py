from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pmx_role_classifier as classifier


def _payload(name: str) -> bytes:
    return json.dumps({
        "success": True,
        "data": {
            "1": {
                "material_type": 0,
                "interactive": 0,
                "name": name,
                "icon": "pic_ss_test",
                "rarity": 4,
            }
        },
        "total_page": 1,
    }).encode("utf-8")


class _Response:
    def __init__(self, data: bytes):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self.data


class CharacterCatalogRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        classifier._CHARACTER_CACHE.clear()

    def test_force_refresh_does_not_reuse_fresh_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_rows = [
                {
                    "id": str(index),
                    "material_type": 0,
                    "interactive": 0,
                    "name": f"旧名{index}",
                    "icon": f"pic_ss_old{index}",
                    "rarity": 4,
                }
                for index in range(120)
            ]
            (root / classifier.CHARACTER_CACHE_NAME).write_text(
                json.dumps({"characters": old_rows}, ensure_ascii=False),
                encoding="utf-8",
            )
            calls = 0

            def urlopen(_request, timeout=0):
                nonlocal calls
                calls += 1
                # Each of the six rarity requests must contribute enough unique
                # rows for the catalog validity threshold.
                rows = {
                    str(calls * 1000 + index): {
                        "material_type": 0,
                        "interactive": 0,
                        "name": f"新名{calls}_{index}",
                        "icon": f"pic_ss_new{calls}_{index}",
                        "rarity": min(calls, 6),
                    }
                    for index in range(20)
                }
                return _Response(json.dumps({
                    "success": True,
                    "data": rows,
                    "total_page": 1,
                }).encode("utf-8"))

            with mock.patch.object(classifier.urllib.request, "urlopen", urlopen):
                catalog, refreshed = classifier.prepare_character_catalog(
                    root, refresh=True
                )

            self.assertTrue(refreshed)
            self.assertEqual(calls, 6)
            self.assertEqual(len(catalog), 120)
            self.assertTrue(all(item.name.startswith("新名") for item in catalog))


if __name__ == "__main__":
    unittest.main()
