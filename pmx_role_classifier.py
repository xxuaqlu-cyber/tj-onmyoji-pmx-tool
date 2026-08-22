# -*- coding: utf-8 -*-
"""Persistent role classification for textured PMX output directories."""

from __future__ import annotations

import csv
import json
import re
import shutil
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


RULES_NAME = "角色分类规则.json"
CATALOG_NAME = "角色分类清单.csv"
CLASSIFIED_FOLDER = "按角色"
GENERIC_RESOURCE_DIRS = {
    "common", "comm", "shared", "texture", "textures", "shader", "shadow",
    "effect", "effects", "fx", "model", "models", "public", "default",
}
COMPONENT_SUFFIXES = (
    r"_show\d*", r"_guajian\d*", r"_socket.*", r"_touming", r"_tingyuan",
    r"_tansuo", r"_bat", r"_chongwu", r"_pet", r"_wuqi\d*", r"_weapon\d*",
    r"_texiao.*", r"_sijiaolong", r"_lingdang", r"_longtou.*", r"_qiu",
    r"_taiyang", r"_bianzi", r"_erhuan", r"_luling.*", r"_huaban.*",
    r"_juqing", r"_jq", r"_wtj", r"_damo", r"_mini", r"_boss", r"_mao",
    r"_c\d+",
)


@dataclass(slots=True)
class RoleEntry:
    identity: str
    source_mesh: str
    components: tuple[str, ...]
    model_name: str
    role: str
    automatic_role: str
    evidence: str
    output_dir: Path
    pmx_path: Path
    fingerprint: str
    size_bucket: str
    manual: bool = False


def clean_token(value: str) -> str:
    value = value.strip().replace("\\", "/").lower()
    value = re.sub(r"[^0-9a-z_\-一-鿿]+", "_", value)
    return re.sub(r"_+", "_", value).strip("_-")


def normalize_role(value: str) -> str:
    value = clean_token(Path(value).stem)
    value = re.sub(r"^(?:c|s)\d+_", "", value)
    changed = True
    while value and changed:
        changed = False
        for suffix in COMPONENT_SUFFIXES:
            new_value = re.sub(suffix + r"$", "", value)
            if new_value != value:
                value = new_value.rstrip("_")
                changed = True
                break
    value = re.sub(r"^(?:boss|npc|q|j)_", "", value)
    return value or "_待分类"


def role_from_resource_path(value: str) -> str | None:
    normalized = value.strip().replace("\\", "/").lower().lstrip("/")
    parts = [part for part in normalized.split("/") if part]
    if len(parts) < 2:
        return None
    if parts[0] in {"shader", "shaders"}:
        return None
    if parts[0] in {"levelsets", "natural"}:
        return "_场景资源"
    if parts[0] == "static":
        return "_静态道具"
    if parts[0] == "fx" and (len(parts) < 3 or parts[1] != "model"):
        return "_特效资源"
    if parts[0] == "fx" and len(parts) >= 3 and parts[1] == "model":
        candidate = parts[2]
    elif parts[0] in {"model", "npcmodel"} and len(parts) >= 2:
        candidate = parts[1]
    else:
        candidate = parts[-2]
    candidate = clean_token(candidate)
    if not candidate or candidate in GENERIC_RESOURCE_DIRS:
        return None
    return normalize_role(candidate)


def role_from_label(value: str) -> str | None:
    candidate = normalize_role(value)
    if re.fullmatch(r"[0-9a-f]{16,}", candidate) or candidate.isdigit():
        return None
    if re.fullmatch(r"(?:temp)?material_?\d*", candidate):
        return None
    if candidate in GENERIC_RESOURCE_DIRS or candidate == "_待分类":
        return None
    return candidate


def default_rules() -> dict[str, object]:
    return {
        "schema": 1,
        "说明": "预览器右键保存的稳定身份/自动家族覆盖；移动目录后仍由主程序读取。",
        "identity_rules": {},
        "family_rules": {},
        "updated_at": "",
    }


def load_rules(output_root: Path) -> dict[str, object]:
    path = output_root / RULES_NAME
    if not path.is_file():
        return default_rules()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return default_rules()
    result = default_rules()
    if isinstance(raw, dict):
        for key in ("identity_rules", "family_rules"):
            value = raw.get(key)
            if isinstance(value, dict):
                result[key] = {
                    str(k): normalize_role(str(v)) for k, v in value.items() if str(v).strip()
                }
    result["updated_at"] = str(raw.get("updated_at", "")) if isinstance(raw, dict) else ""
    return result


def save_rules(output_root: Path, rules: dict[str, object]) -> Path:
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / RULES_NAME
    payload = default_rules()
    payload.update(rules)
    payload["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def identity_key(metadata: dict[str, object], output_dir: Path) -> str:
    source = str(metadata.get("source_mesh", "")).strip()
    if source:
        return "mesh:" + source.lower()
    components = metadata.get("components")
    if isinstance(components, list) and components:
        return "components:" + "|".join(sorted(str(item).lower() for item in components))
    fingerprint = str(metadata.get("fingerprint", "")).strip().lower()
    return "fingerprint:" + (fingerprint or output_dir.name.lower())


def infer_role(
    model_name: str,
    texture_paths: list[str],
    material_names: list[str],
    material_variant: str = "",
    component_labels: list[str] | None = None,
) -> tuple[str, str]:
    scores: Counter[str] = Counter()
    reasons: dict[str, list[str]] = {}

    def add(role: str | None, score: int, reason: str) -> None:
        if not role:
            return
        role = normalize_role(role)
        scores[role] += score
        reasons.setdefault(role, []).append(reason)

    for value in texture_paths:
        add(role_from_resource_path(value), 100, f"Tex0路径:{value}")
    for value in material_names:
        add(role_from_label(value), 15, f"材质名:{value}")
    if component_labels:
        for value in component_labels:
            add(role_from_label(value), 45, f"组合名:{value}")
    add(role_from_label(material_variant), 35, f"材质组:{material_variant}")
    add(role_from_label(model_name), 20, f"模型名:{model_name}")
    if not scores:
        return "_待分类", "没有可用资源身份"
    role, _ = max(scores.items(), key=lambda item: (item[1], -len(item[0]), item[0]))
    return role, "；".join(reasons.get(role, [])[:4])


def role_for_export(
    output_root: Path,
    identity: str,
    model_name: str,
    texture_paths: list[str],
    material_names: list[str],
    material_variant: str = "",
    component_labels: list[str] | None = None,
) -> str | None:
    """Resolve a persisted/manual role for a newly exported PMX."""
    rules_path = output_root / RULES_NAME
    if not rules_path.is_file():
        return None
    rules = load_rules(output_root)
    automatic, _ = infer_role(
        model_name, texture_paths, material_names, material_variant, component_labels
    )
    identity_rules = rules.get("identity_rules", {})
    family_rules = rules.get("family_rules", {})
    manual = identity_rules.get(identity) if isinstance(identity_rules, dict) else None
    family = family_rules.get(automatic) if isinstance(family_rules, dict) else None
    return normalize_role(str(manual or family or automatic))


def _read_texture_evidence(output_dir: Path) -> tuple[list[str], list[str]]:
    report = output_dir / "纹理槽位.csv"
    textures: list[str] = []
    materials: list[str] = []
    if not report.is_file():
        return textures, materials
    try:
        with report.open("r", encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                material = row.get("材质名", "").strip()
                original = row.get("原始纹理路径", "").strip()
                purpose = row.get("PMX用途", "")
                if material and material not in materials:
                    materials.append(material)
                if original and purpose.strip() == "主贴图" and original not in textures:
                    textures.append(original)
    except OSError:
        pass
    return textures, materials


def scan_entries(output_root: Path, rules: dict[str, object] | None = None) -> list[RoleEntry]:
    rules = rules or load_rules(output_root)
    identity_rules = rules.get("identity_rules", {})
    family_rules = rules.get("family_rules", {})
    identity_rules = identity_rules if isinstance(identity_rules, dict) else {}
    family_rules = family_rules if isinstance(family_rules, dict) else {}
    category_root = output_root / "带贴图"
    entries: list[RoleEntry] = []
    if not category_root.is_dir():
        return entries
    for metadata_path in category_root.rglob(".build.json"):
        output_dir = metadata_path.parent.resolve()
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        pmx_files = sorted(output_dir.glob("*.pmx"))
        if not pmx_files:
            continue
        pmx_path = pmx_files[0].resolve()
        model_name = pmx_path.stem
        textures, materials = _read_texture_evidence(output_dir)
        variant = str(metadata.get("material_variant", ""))
        components_raw = metadata.get("components", [])
        components = tuple(str(item) for item in components_raw) if isinstance(components_raw, list) else ()
        automatic, evidence = infer_role(
            model_name, textures, materials, variant,
            component_labels=[model_name] if components else None,
        )
        identity = identity_key(metadata, output_dir)
        family_key = automatic
        manual_role = identity_rules.get(identity)
        family_role = family_rules.get(family_key)
        role = normalize_role(str(manual_role or family_role or automatic))
        relative = output_dir.relative_to(category_root.resolve())
        if relative.parts and relative.parts[0] == CLASSIFIED_FOLDER and len(relative.parts) >= 3:
            size_bucket = relative.parts[-2]
        else:
            size_bucket = relative.parts[0] if relative.parts else "未分桶"
        entries.append(RoleEntry(
            identity=identity,
            source_mesh=str(metadata.get("source_mesh", "")),
            components=components,
            model_name=model_name,
            role=role,
            automatic_role=automatic,
            evidence=evidence,
            output_dir=output_dir,
            pmx_path=pmx_path,
            fingerprint=str(metadata.get("fingerprint", "")),
            size_bucket=size_bucket,
            manual=bool(manual_role or family_role),
        ))
    entries.sort(key=lambda item: (item.role, item.model_name, item.identity))
    return entries


def write_catalog(output_root: Path, entries: list[RoleEntry]) -> Path:
    path = output_root / CATALOG_NAME
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow([
            "角色分类", "自动分类", "人工规则", "模型名", "源Mesh", "组件列表",
            "分类依据", "PMX", "构建指纹", "稳定身份",
        ])
        for item in entries:
            writer.writerow([
                item.role, item.automatic_role, "是" if item.manual else "否",
                item.model_name, item.source_mesh, "|".join(item.components),
                item.evidence, str(item.pmx_path), item.fingerprint, item.identity,
            ])
    return path


def _replace_csv_paths(output_root: Path, moves: dict[str, str]) -> int:
    changed_files = 0
    if not moves:
        return changed_files
    for path in output_root.glob("*.csv"):
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.reader(stream))
        except OSError:
            continue
        changed = False
        for row in rows:
            for index, value in enumerate(row):
                updated = value
                if value and ("\\" in value or "/" in value):
                    candidate = Path(value)
                    for parent in (candidate, *candidate.parents):
                        old = str(parent)
                        new = moves.get(old)
                        if new is None:
                            continue
                        suffix = candidate.relative_to(parent)
                        updated = str(Path(new) / suffix)
                        break
                if updated != value:
                    row[index] = updated
                    changed = True
        if not changed:
            continue
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
            csv.writer(stream).writerows(rows)
        temporary.replace(path)
        changed_files += 1
    return changed_files


def apply_classification(output_root: Path, entries: list[RoleEntry]) -> tuple[int, int]:
    category_root = (output_root / "带贴图").resolve()
    classified_root = (category_root / CLASSIFIED_FOLDER).resolve()
    moves: dict[str, str] = {}
    moved = 0
    for item in entries:
        source = item.output_dir.resolve()
        try:
            source.relative_to(category_root)
        except ValueError:
            continue
        target = (classified_root / normalize_role(item.role) / item.size_bucket / source.name).resolve()
        try:
            target.relative_to(classified_root)
        except ValueError:
            continue
        if source == target:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing_meta = target / ".build.json"
            same = False
            if existing_meta.is_file():
                try:
                    same = json.loads(existing_meta.read_text(encoding="utf-8")).get("fingerprint") == item.fingerprint
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
            if same:
                continue
            target = target.with_name(target.name + "_" + item.fingerprint[:8])
        old_text = str(source)
        shutil.move(str(source), str(target))
        moves[old_text] = str(target)
        item.output_dir = target
        item.pmx_path = target / item.pmx_path.name
        moved += 1
    changed_reports = _replace_csv_paths(output_root, moves)
    write_catalog(output_root, entries)
    return moved, changed_reports


def move_pmx_to_role(
    output_root: Path,
    pmx_path: Path,
    new_role: str,
) -> tuple[Path, int, int]:
    """持久化单个 PMX 的人工分类，并同步移动目录、报告和完整分类清单。"""
    output_root = output_root.resolve()
    pmx_path = pmx_path.resolve()
    role = normalize_role(new_role)
    rules = load_rules(output_root)
    entries = scan_entries(output_root, rules)
    matches = [item for item in entries if item.pmx_path.resolve() == pmx_path]
    if len(matches) != 1:
        raise ValueError("当前模型不在带贴图角色分类清单中，不能移动分类。")

    entry = matches[0]
    identity_rules = rules.setdefault("identity_rules", {})
    if not isinstance(identity_rules, dict):
        identity_rules = {}
        rules["identity_rules"] = identity_rules
    identity_rules[entry.identity] = role
    entry.role = role
    entry.manual = True

    moved, changed_reports = apply_classification(output_root, [entry])
    save_rules(output_root, rules)
    refreshed = scan_entries(output_root, rules)
    write_catalog(output_root, refreshed)
    refreshed_match = next(
        (item for item in refreshed if item.identity == entry.identity),
        None,
    )
    return (
        refreshed_match.pmx_path if refreshed_match is not None else entry.pmx_path,
        moved,
        changed_reports,
    )
