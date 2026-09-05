"""Audit current material resolution for priority textureless meshes.

This intentionally reuses the production resolver.  It is a verification
artifact only: no resource cache, PMX output, or source manifest is modified.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE))

import onmyoji_rigged_mesh_gui as rigged


MODEL_ROOT = WORKSPACE / "unpacked" / "model"
OUTPUT_ROOT = WORKSPACE / "rigged_models" / "PMX输出"
THD_ROOT = (
    WORKSPACE
    / "yys"
    / "com.netease.onmyoji.wyzymnqsd_cps"
    / "files"
    / "netease"
    / "onmyoji"
    / "Documents"
    / "cloudfilesys3"
    / "thd"
)
AUDIT_PATH = WORKSPACE / "unpacked" / "material_build_audit.json"


def main() -> int:
    def stage(label: str, done: int, total: int) -> None:
        print(f"[{done}/{total}] {label}", flush=True)

    packages, by_mesh, variants_by_mesh = rigged.build_material_packages(
        MODEL_ROOT, thd_dir=THD_ROOT, stage_progress=stage
    )
    priority_path = OUTPUT_ROOT / "白模优先检查_角色主包.csv"
    with priority_path.open("r", newline="", encoding="utf-8-sig") as stream:
        priority_rows = list(csv.DictReader(stream))

    bound: list[dict[str, object]] = []
    ambiguous: list[dict[str, object]] = []
    for row in priority_rows:
        mesh_path = Path(row["物理Mesh路径"]).resolve()
        package = by_mesh.get(mesh_path)
        variants = variants_by_mesh.get(mesh_path, [])
        if package is not None:
            resolved_primary = sum(
                1
                for material in package.materials
                if (reference := rigged.material_primary_texture(material))
                and reference in package.texture_map
            )
            bound.append(
                {
                    "mesh": mesh_path.name,
                    "confidence": package.confidence,
                    "materials": len(package.materials),
                    "resolved_primary_slots": resolved_primary,
                    "variants": len(variants),
                }
            )
        elif variants:
            ambiguous.append(
                {
                    "mesh": mesh_path.name,
                    "variants": len(variants),
                    "confidences": sorted({item.confidence for item in variants}),
                }
            )

    payload = {
        "package_count": len(packages),
        "unique_mesh_count": len(by_mesh),
        "variant_mesh_count": len(variants_by_mesh),
        "p0_total": len(priority_rows),
        "p0_unique_bound": len(bound),
        "p0_variant_ambiguous": len(ambiguous),
        "p0_still_unresolved": len(priority_rows) - len(bound) - len(ambiguous),
        "p0_bound": bound,
        "p0_ambiguous": ambiguous,
    }
    temporary = AUDIT_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(AUDIT_PATH)
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
