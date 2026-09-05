from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

import onmyoji_rigged_mesh_gui as rigged
import pmx_preview_gui as preview


class PreviewSourceMetadataTests(unittest.TestCase):
    def test_directory_discovery_restores_source_size_from_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "未匹配贴图" / "a" / "a.pmx"
            second = root / "未匹配贴图" / "b" / "b.pmx"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.touch()
            second.touch()
            with (root / "纹理恢复报告.csv").open(
                "w", newline="", encoding="utf-8-sig"
            ) as stream:
                writer = csv.DictWriter(
                    stream, fieldnames=("PMX", "源Mesh", "源Mesh大小")
                )
                writer.writeheader()
                writer.writerow({
                    "PMX": str(first),
                    "源Mesh": "000010_a.mesh",
                    "源Mesh大小": "4096",
                })
                writer.writerow({
                    "PMX": str(second),
                    "源Mesh": "000020_b.mesh",
                    "源Mesh大小": "8192",
                })

            items = preview.discover_items(root, "全部 PMX")
            by_name = {item.path.name: item for item in items}
            self.assertEqual(by_name["a.pmx"].source_size, 4096)
            self.assertEqual(by_name["a.pmx"].source_order, 10)
            self.assertEqual(by_name["b.pmx"].source_size, 8192)


class NpkMaterialEvidenceTests(unittest.TestCase):
    @staticmethod
    def _mesh(positions: list[tuple[float, float, float]]) -> rigged.ParsedMesh:
        return rigged.ParsedMesh(
            version=3,
            submeshes=[(len(positions), 0, 1, 0)],
            bone_parents=[-1],
            bone_names=["root"],
            bone_matrices=[tuple(float(index % 5 == 0) for index in range(16))],
            positions=positions,
            normals=[(0.0, 1.0, 0.0)] * len(positions),
            faces=[],
            uvs=[(0.0, 0.0)] * len(positions),
            joints=[(0, 0, 0, 0)] * len(positions),
            weights=[(1.0, 0.0, 0.0, 0.0)] * len(positions),
        )

    def test_gim_geometry_is_a_hard_mesh_identity_check(self) -> None:
        mesh = self._mesh([(-2.0, 1.0, -1.0), (4.0, 5.0, 3.0)])
        matching = [rigged.GimSubmesh("body", 0, (1.0, 3.0, 1.0), (3.0, 2.0, 2.0))]
        wrong = [rigged.GimSubmesh("body", 0, (9.0, 3.0, 1.0), (3.0, 2.0, 2.0))]
        self.assertTrue(rigged._gim_geometry_matches_mesh(matching, mesh))
        self.assertFalse(rigged._gim_geometry_matches_mesh(wrong, mesh))

    def test_npk_manifest_contributes_content_hash_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            model_root = Path(temporary) / "model"
            resource = model_root / "model2" / "asset.dds"
            resource.parent.mkdir(parents=True)
            resource.touch()
            digest = "1" * 32
            (model_root / "npk_manifest.json").write_text(
                json.dumps({
                    "resources": [{
                        "content_md5": digest,
                        "relative_path": "model2/asset.dds",
                    }]
                }),
                encoding="utf-8",
            )

            by_md5, by_path = rigged._manifest_hash_maps(model_root)
            self.assertEqual(by_md5[digest], resource)
            self.assertEqual(by_path[resource], digest)

    def test_material_aliases_follow_complete_mesh_md5(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canonical = root / "pkg" / "canonical.mesh"
            hot_copy = root / "_hotdeps" / "same-name.mesh"
            unrelated = root / "_hotdeps" / "same-name-other.mesh"
            canonical.parent.mkdir(parents=True)
            hot_copy.parent.mkdir(parents=True)
            canonical.write_bytes(b"same mesh")
            hot_copy.write_bytes(b"same mesh")
            unrelated.write_bytes(b"different mesh")
            digest = "a" * 32
            other_digest = "b" * 32
            material_a = rigged.MaterialDefinition(
                "body", {"tex0": "model/body.ktx"}
            )
            material_b = rigged.MaterialDefinition(
                "body", {"tex0": "model/body_alt.ktx"}
            )
            package_a = rigged.MaterialPackage(
                root / "body_a.xml", 1, "body_a", [material_a],
                [canonical], {}, "THD精确"
            )
            package_b = rigged.MaterialPackage(
                root / "body_b.xml", 2, "body_b", [material_b],
                [canonical], {}, "THD精确"
            )
            by_mesh = {canonical.resolve(): package_a}
            variants = {
                canonical.resolve(): [package_a, package_b]
            }
            md5_by_path = {
                canonical.resolve(): digest,
                hot_copy.resolve(): digest,
                unrelated.resolve(): other_digest,
            }

            added = rigged._merge_material_mesh_content_aliases(
                by_mesh, variants, md5_by_path
            )

            alias = hot_copy.resolve()
            self.assertEqual(added, 1)
            self.assertIn(alias, by_mesh)
            self.assertEqual(by_mesh[alias].mesh_paths, [alias])
            self.assertIsNot(by_mesh[alias], package_a)
            self.assertEqual(
                {
                    rigged._material_variant_signature(item.materials)
                    for item in variants[alias]
                },
                {
                    rigged._material_variant_signature(item.materials)
                    for item in variants[canonical.resolve()]
                },
            )
            self.assertNotIn(unrelated.resolve(), by_mesh)

    def test_stale_gim_label_yields_to_unique_logical_mesh_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base.mesh"
            show = root / "show.mesh"
            base.touch()
            show.touch()
            package = rigged.MaterialPackage(
                root / "stale_gim.xml", 1, "sp_mianlingqi_show", [],
                [base], {}, "THD精确"
            )
            references = {
                "model/sp_mianlingqi/sp_mianlingqi.mesh": base,
                "model/sp_mianlingqi_show/sp_mianlingqi_show.mesh": show,
            }

            renamed = rigged._correct_cross_mesh_package_labels(
                [package],
                [
                    "model/sp_mianlingqi/sp_mianlingqi.gim",
                    "model/sp_mianlingqi_show/sp_mianlingqi_show.mesh",
                ],
                references.get,
            )

            self.assertEqual(renamed, 1)
            self.assertEqual(package.package_name, "sp_mianlingqi")

    def test_ambiguous_logical_mesh_identity_keeps_package_label(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shared = root / "shared.mesh"
            show = root / "show.mesh"
            shared.touch()
            show.touch()
            package = rigged.MaterialPackage(
                root / "gim.xml", 1, "sp_mianlingqi_show", [],
                [shared], {}, "THD精确"
            )
            references = {
                "model/sp_mianlingqi/sp_mianlingqi.mesh": shared,
                "model/sp_mianlingqi_alt/sp_mianlingqi_alt.mesh": shared,
                "model/sp_mianlingqi_show/sp_mianlingqi_show.mesh": show,
            }

            renamed = rigged._correct_cross_mesh_package_labels(
                [package], references, references.get
            )

            self.assertEqual(renamed, 0)
            self.assertEqual(package.package_name, "sp_mianlingqi_show")


if __name__ == "__main__":
    unittest.main()
