# -*- coding: utf-8 -*-
"""Build a model/skin/hero index from the game's compiled cdata tables.

The client stores these tables as marshalled Python modules whose payload is a
compact ``bindict`` value.  Nothing in this module executes game bytecode: only
the code object's constants and the bindict payload are read.
"""

from __future__ import annotations

import json
import marshal
import re
import struct
import types
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable


INDEX_NAME = "角色皮肤索引.json"
INDEX_SCHEMA = 4
RARITY_NAMES = {6: "UR", 5: "SP", 4: "SSR", 3: "SR", 2: "R", 1: "N"}
TARGET_MODULES = {
    "hero_brief.py": "hero_brief",
    "hero.py": "hero",
    "skin.py": "skin",
    "character.py": "character",
}
FAMILY_SUFFIXES = (
    r"_show\d*", r"_guajian\d*", r"_socket.*", r"_touming",
    r"_tingyuan", r"_tansuo", r"_bat", r"_chongwu", r"_pet",
    r"_wuqi\d*", r"_weapon\d*", r"_texiao.*", r"_juqing", r"_jq",
)


@dataclass(frozen=True, slots=True)
class LocalHero:
    hero_id: str
    name: str
    rarity: str
    rarity_value: int
    icon: str
    material_type: int = 0


@dataclass(frozen=True, slots=True)
class LocalModel:
    model_id: str
    hero_id: str
    hero_name: str
    skin_name: str
    rarity: str
    rarity_value: int
    material_type: int
    source_model: str
    item_id: int = -1
    acquisition: str = ""


@dataclass(slots=True)
class LocalCatalog:
    heroes: dict[str, LocalHero]
    models: dict[str, LocalModel]
    aliases: dict[str, str]
    source_root: str = ""

    def resolve(self, values: Iterable[str]) -> LocalModel | None:
        """Resolve exact identities, then exact bases of known components."""
        token_groups = [_identity_tokens(value) for value in values]
        for tokens in token_groups:
            for token in tokens:
                canonical = self.aliases.get(token, token)
                match = self.models.get(canonical)
                if match is not None:
                    return match
        # An attachment such as ``s3_x_guajian01`` is not a skin ID, but its
        # stripped base is.  This is still a table-backed lookup rather than a
        # similarity score, and therefore cannot jump to a similarly named hero.
        for tokens in token_groups:
            for token in tokens:
                candidates = [token]
                changed = True
                while changed:
                    changed = False
                    for suffix in FAMILY_SUFFIXES:
                        reduced = re.sub(suffix + r"$", "", candidates[-1]).rstrip("_-")
                        if reduced != candidates[-1]:
                            candidates.append(reduced)
                            changed = True
                            break
                base = re.sub(r"^(?:boss|npc|q)_", "", candidates[-1])
                if base and base != candidates[-1]:
                    candidates.append(base)
                for candidate in candidates[1:]:
                    canonical = self.aliases.get(candidate, candidate)
                    match = self.models.get(canonical)
                    if match is not None:
                        return match
                # Resource components commonly append their own noun after the
                # complete skin model ID (``s1_longjue_xinjing_yedi``).  Walk
                # underscore boundaries from longest to shortest and accept
                # only a prefix that is itself present in the game table.
                parts = token.split("_")
                for end in range(len(parts) - 1, 0, -1):
                    prefix = "_".join(parts[:end]).rstrip("_-")
                    if len(prefix) < 5:
                        continue
                    canonical = self.aliases.get(prefix, prefix)
                    match = self.models.get(canonical)
                    if match is not None:
                        return match
        return None


class BindictReader:
    """Small, bounds-checked reader for NetEase's compiled bindict constants."""

    def __init__(self, data: bytes):
        if len(data) < 12:
            raise ValueError("bindict payload is too short")
        self.data = data
        self.string_count = struct.unpack_from("<I", data)[0]
        if self.string_count > 2_000_000:
            raise ValueError("invalid bindict string count")
        table_end = 4 + (self.string_count + 1) * 4
        if table_end > len(data):
            raise ValueError("truncated bindict string table")
        self.offsets = struct.unpack_from(
            f"<{self.string_count + 1}I", data, 4
        )
        string_base = 8 + 4 * self.string_count
        if string_base + self.offsets[-1] > len(data):
            raise ValueError("invalid bindict string offsets")
        self.strings = [
            data[
                string_base + self.offsets[index]:
                string_base + self.offsets[index + 1]
            ].decode("utf-8", "replace")
            for index in range(self.string_count)
        ]
        self.base = string_base + self.offsets[-1]
        self._field_defs: dict[int, list[tuple[str, int, int | None]]] = {}

    def _uint(self, position: int) -> tuple[int, int]:
        value = 0
        shift = 0
        while position < len(self.data):
            byte = self.data[position]
            position += 1
            value |= (byte & 0x7F) << shift
            if byte < 0x80:
                return value, position
            shift += 7
            if shift >= 70:
                break
        raise ValueError("invalid bindict varint")

    def _field_def(self, position: int):
        relative = position - self.base
        cached = self._field_defs.get(relative)
        if cached is not None:
            return cached, position
        count, position = self._uint(position)
        optional_count, position = self._uint(position)
        fields = []
        for index in range(count):
            string_index, position = self._uint(position)
            if string_index >= len(self.strings) or position >= len(self.data):
                raise ValueError("invalid bindict field definition")
            value_tag = self.data[position]
            position += 1
            fields.append((
                self.strings[string_index], value_tag,
                index if index < optional_count else None,
            ))
        self._field_defs[relative] = fields
        return fields, position

    def _node(self, position: int, tag: int = 0, depth: int = 0):
        if depth > 100 or position < 0 or position >= len(self.data):
            raise ValueError("invalid bindict node")
        if not tag:
            tag = self.data[position]
            position += 1
        kind = tag & 0x0F
        if kind == 11:
            relative, position = self._uint(position)
            return self._node(self.base + relative, depth=depth + 1)[0], position
        if kind == 1:
            value, position = self._uint(position)
            if tag & 0xF0 == 0x10:
                value = -(value & 1) ^ (value >> 1)
            return value, position
        if kind == 2:
            if tag & 0xF0 == 0x10:
                return struct.unpack_from("<f", self.data, position)[0], position + 4
            if tag & 0xF0 == 0x20:
                return struct.unpack_from("<d", self.data, position)[0], position + 8
            raise ValueError(f"invalid bindict float tag 0x{tag:02x}")
        if kind == 3:
            return bool(self.data[position]), position + 1
        if kind == 4:
            return None, position
        if kind == 5:
            string_index, position = self._uint(position)
            if string_index >= len(self.strings):
                raise ValueError("invalid bindict string reference")
            return self.strings[string_index], position
        if kind == 9:
            return self._field_def(position)
        if kind == 6:
            if tag & 0x80:
                definition_offset, position = self._uint(position)
                fields, _ = self._field_def(self.base + definition_offset)
                optional_count = sum(bit is not None for _, _, bit in fields)
                mask_size = (optional_count + 7) // 8
                if tag & 0x40:
                    mask_offset, position = self._uint(position)
                    mask_start = self.base + mask_offset
                    mask = self.data[mask_start:mask_start + mask_size]
                else:
                    mask = self.data[position:position + mask_size]
                    position += mask_size
                if len(mask) != mask_size:
                    raise ValueError("truncated bindict optional-field mask")
                result = {}
                for key, value_tag, bit in fields:
                    if bit is not None and not mask[bit >> 3] & (1 << (bit & 7)):
                        continue
                    value, position = self._node(position, value_tag, depth + 1)
                    result[key] = value
                return result, position
            key_tag = self.data[position] if tag & 0x10 else 0
            position += bool(tag & 0x10)
            value_tag = self.data[position] if tag & 0x20 else 0
            position += bool(tag & 0x20)
            count, position = self._uint(position)
            result = {}
            if tag & 0x40:
                index_position = position
                position += count * 8
                if position > len(self.data):
                    raise ValueError("truncated bindict map index")
                entry_positions = [
                    self.base + struct.unpack_from(
                        "<I", self.data, index_position + index * 8 + 4
                    )[0]
                    for index in range(count)
                ]
            else:
                entry_positions = [None] * count
            for entry_position in entry_positions:
                cursor = position if entry_position is None else entry_position
                key, cursor = self._node(cursor, key_tag, depth + 1)
                value, cursor = self._node(cursor, value_tag, depth + 1)
                result[key] = value
                if entry_position is None:
                    position = cursor
            return result, position
        if kind in (7, 8, 12):
            element_tag = self.data[position] if tag & 0x20 else 0
            position += bool(tag & 0x20)
            count, position = self._uint(position)
            result = []
            stride = 8 if kind == 8 else 4
            if tag & 0x40:
                index_position = position
                position += count * stride
                if position > len(self.data):
                    raise ValueError("truncated bindict sequence index")
                entry_positions = [
                    self.base + struct.unpack_from(
                        "<I", self.data,
                        index_position + index * stride + (4 if kind == 8 else 0),
                    )[0]
                    for index in range(count)
                ]
            else:
                entry_positions = [None] * count
            for entry_position in entry_positions:
                cursor = position if entry_position is None else entry_position
                value, cursor = self._node(cursor, element_tag, depth + 1)
                result.append(value)
                if entry_position is None:
                    position = cursor
            return result, position
        raise ValueError(f"unknown bindict tag 0x{tag:02x}")

    def read(self):
        root_offset = struct.unpack_from("<i", self.data, self.base)[0]
        return self._node(self.base + root_offset)[0]


def _identity_tokens(value: str) -> list[str]:
    value = str(value).strip().replace("\\", "/").lower()
    if not value:
        return []
    parts = [part for part in value.split("/") if part]
    candidates = [value, *reversed(parts)]
    result: list[str] = []
    for candidate in candidates:
        candidate = Path(candidate).stem
        candidate = re.sub(r"[^a-z0-9_\-]+", "_", candidate).strip("_-")
        if candidate and candidate not in result:
            result.append(candidate)
    return result


def _integer(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _string(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _row(table: dict, hero_id: object) -> dict:
    value = table.get(hero_id, table.get(str(hero_id), {}))
    return value if isinstance(value, dict) else {}


def build_catalog_from_tables(
    hero_brief: dict,
    hero: dict,
    skin: dict,
    character: dict,
    source_root: str = "",
) -> LocalCatalog:
    """Join the four client tables without guessing by model-name similarity."""
    heroes: dict[str, LocalHero] = {}
    models: dict[str, LocalModel] = {}
    aliases: dict[str, str] = {}

    for raw_id, brief in hero_brief.items():
        if not isinstance(brief, dict):
            continue
        hero_id = str(brief.get("id", raw_id))
        detail = _row(hero, raw_id)
        name = _string(brief.get("name") or detail.get("name"))
        if not name:
            continue
        rarity_value = _integer(detail.get("rarity", brief.get("rarity", 0)))
        material_type = _integer(
            detail.get("material_type", brief.get("material_type", 0))
        )
        rarity = "呱太" if material_type == 101 else RARITY_NAMES.get(
            rarity_value, "其他资源"
        )
        head = _string(brief.get("head") or detail.get("awakeBefore"))
        icon = head if head.startswith("pic_ss_") else f"pic_ss_{head}"
        heroes[hero_id] = LocalHero(
            hero_id, name, rarity, rarity_value, icon, material_type
        )

    hero_ids_by_name: dict[str, list[str]] = {}
    for item in heroes.values():
        hero_ids_by_name.setdefault(item.name, []).append(item.hero_id)

    for raw_id, skin_row in skin.items():
        if not isinstance(skin_row, dict):
            continue
        hero_id = str(skin_row.get("id", raw_id))
        hero_item = heroes.get(hero_id)
        if hero_item is None:
            # The skin table also contains legacy/alternate IDs.  Their hero
            # table row retains the same Chinese name, which safely joins them
            # back to the one canonical hero_brief record.
            alternate_detail = _row(hero, raw_id)
            alternate_name = _string(alternate_detail.get("name"))
            canonical_ids = hero_ids_by_name.get(alternate_name, [])
            if len(canonical_ids) == 1:
                hero_id = canonical_ids[0]
                hero_item = heroes.get(hero_id)
        if hero_item is None:
            continue
        model_ids = skin_row.get("skin", [])
        names = skin_row.get("name", [])
        items = skin_row.get("item", [])
        ways = skin_row.get("way", [])
        if not isinstance(model_ids, list) or not isinstance(names, list):
            continue
        source_model = _string(model_ids[0]) if model_ids else ""
        for index, raw_model_id in enumerate(model_ids):
            model_id = _string(raw_model_id).lower()
            skin_name = _string(names[index]) if index < len(names) else ""
            if not model_id or not skin_name:
                continue
            item_id = _integer(items[index], -1) if isinstance(items, list) and index < len(items) else -1
            acquisition = _string(ways[index]) if isinstance(ways, list) and index < len(ways) else ""
            models[model_id] = LocalModel(
                model_id=model_id,
                hero_id=hero_id,
                hero_name=hero_item.name,
                skin_name=skin_name,
                rarity=hero_item.rarity,
                rarity_value=hero_item.rarity_value,
                material_type=hero_item.material_type,
                source_model=source_model,
                item_id=item_id,
                acquisition=acquisition,
            )
            aliases[model_id] = model_id

    models_by_hero: dict[str, list[LocalModel]] = {}
    for item in models.values():
        models_by_hero.setdefault(item.hero_id, []).append(item)

    # character.modelId connects alternate table keys and show_model to the
    # canonical skin model.  Some older heroes use a second internal naming
    # family here (for example baizangzhu -> xiaobai), so Chinese character and
    # skin labels are also used as an exact cross-table join.
    for raw_key, char_row in character.items():
        if not isinstance(char_row, dict):
            continue
        key = _string(raw_key).lower()
        model_id = _string(char_row.get("modelId") or key).lower()
        canonical = model_id if model_id in models else key if key in models else ""
        hero_id = models[canonical].hero_id if canonical else ""
        if not hero_id:
            source_model = _string(char_row.get("src_modelId")).lower()
            source_canonical = aliases.get(source_model, source_model)
            source_match = models.get(source_canonical)
            if source_match is not None:
                hero_id = source_match.hero_id
        if not hero_id:
            hero_name = _string(char_row.get("name"))
            ids = hero_ids_by_name.get(hero_name, [])
            if len(ids) == 1:
                hero_id = ids[0]
        if not canonical and hero_id:
            choices = models_by_hero.get(hero_id, [])
            description = _compact_display_text(char_row.get("descContent"))
            named = [
                item for item in choices
                if item.skin_name not in {"默认", "覺醒", "觉醒"}
                and _compact_display_text(item.skin_name)
                and _compact_display_text(item.skin_name) in description
            ]
            if len(named) == 1:
                canonical = named[0].model_id
            elif key.startswith("j_"):
                awakened = [item for item in choices if item.skin_name in {"覺醒", "觉醒"}]
                if len(awakened) == 1:
                    canonical = awakened[0].model_id
            elif _integer(char_row.get("skin_type")) == 0:
                defaults = [item for item in choices if item.skin_name == "默认"]
                if len(defaults) == 1:
                    canonical = defaults[0].model_id
        if not canonical:
            continue
        aliases[key] = canonical
        aliases[model_id] = canonical
        show_model = _string(char_row.get("show_model")).lower()
        if show_model:
            aliases[show_model] = canonical

    return LocalCatalog(heroes, models, aliases, source_root)


def _compact_display_text(value: object) -> str:
    return re.sub(r"[^\u3400-\u9fffA-Za-z0-9]+", "", _string(value))


def _looks_like_table(value: object, required: set[str]) -> bool:
    if not isinstance(value, dict) or len(value) < 10:
        return False
    checked = 0
    for row in value.values():
        if isinstance(row, dict):
            checked += 1
            if required.issubset(row):
                return True
        if checked >= 20:
            break
    return False


def _read_module_table(path: Path, module: str) -> dict:
    raw = path.read_bytes()
    constants: list[bytes] = []
    try:
        code = marshal.loads(raw)
    except (EOFError, TypeError, ValueError):
        code = None
    if isinstance(code, types.CodeType):
        constants = [item for item in code.co_consts if isinstance(item, bytes)]
    else:
        # marshal code objects are Python-version specific.  The game currently
        # ships 3.11 bytecode while this tool also supports Python 3.10.  Bytes
        # constants themselves retain marshal's stable ``s + u32 length`` form,
        # so locate those payloads without interpreting any bytecode fields.
        constants = list(_marshal_bytes_constants(raw))
    requirements = {
        "hero_brief": {"id", "name", "head"},
        "hero": {"protoid", "name", "rarity"},
        "skin": {"id", "skin", "name"},
        "character": {"id", "modelId", "name"},
    }
    for constant in constants:
        if len(constant) < 32:
            continue
        try:
            value = BindictReader(constant).read()
        except (IndexError, KeyError, TypeError, ValueError, struct.error):
            continue
        if _looks_like_table(value, requirements[module]):
            return value
    raise ValueError(f"没有在 {path.name} 中找到 {module} 数据表")


def _marshal_bytes_constants(raw: bytes) -> Iterable[bytes]:
    position = 0
    limit = len(raw) - 5
    while position <= limit:
        # TYPE_STRING (0x73) is the marshal tag used for ``bytes``.  FLAG_REF
        # may occupy its high bit in newer marshal versions.
        if raw[position] & 0x7F != ord("s"):
            position += 1
            continue
        size = struct.unpack_from("<I", raw, position + 1)[0]
        start = position + 5
        end = start + size
        if 32 <= size <= len(raw) and end <= len(raw):
            yield raw[start:end]
        position += 1


def _discover_modules(
    script3_root: Path,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Path]:
    files = list(script3_root.rglob("*.dat"))
    found: dict[str, Path] = {}
    found_stamp: dict[str, int] = {}
    target_bytes = {
        name: ("com\\data\\cdata\\" + name).encode("utf-8")
        for name in TARGET_MODULES
    }
    total = len(files)
    for index, path in enumerate(files, 1):
        if progress is not None and (index % 100 == 0 or index == total):
            progress(index, total)
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        possible = [name for name, marker in target_bytes.items() if marker in raw]
        if not possible:
            continue
        filename = ""
        try:
            code = marshal.loads(raw)
        except (EOFError, TypeError, ValueError):
            code = None
        if isinstance(code, types.CodeType):
            filename = code.co_filename.replace("\\", "/").lower()
        for filename_tail, module in TARGET_MODULES.items():
            if filename:
                matched = filename.endswith("/" + filename_tail)
            else:
                matched = filename_tail in possible
            if not matched:
                continue
            try:
                stamp = path.stat().st_mtime_ns
            except OSError:
                stamp = 0
            if stamp >= found_stamp.get(module, -1):
                found[module] = path
                found_stamp[module] = stamp
    return found


def _source_record(path: Path, root: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "path": path.relative_to(root).as_posix(),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _manifest_record(script3_root: Path) -> dict[str, int]:
    path = script3_root / "manifest.csv"
    try:
        stat = path.stat()
    except OSError:
        return {"size": -1, "mtime_ns": -1}
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _cache_is_current(payload: dict, script3_root: Path) -> bool:
    if payload.get("schema") != INDEX_SCHEMA:
        return False
    if payload.get("manifest") != _manifest_record(script3_root):
        return False
    sources = payload.get("sources")
    if not isinstance(sources, dict) or set(sources) != set(TARGET_MODULES.values()):
        return False
    for record in sources.values():
        if not isinstance(record, dict):
            return False
        path = script3_root / str(record.get("path", ""))
        try:
            stat = path.stat()
        except OSError:
            return False
        if stat.st_size != record.get("size") or stat.st_mtime_ns != record.get("mtime_ns"):
            return False
    return True


def _catalog_from_payload(payload: dict) -> LocalCatalog:
    heroes = {
        str(row["hero_id"]): LocalHero(**row)
        for row in payload.get("heroes", []) if isinstance(row, dict)
    }
    models = {
        str(row["model_id"]): LocalModel(**row)
        for row in payload.get("models", []) if isinstance(row, dict)
    }
    aliases = {
        str(key): str(value)
        for key, value in payload.get("aliases", {}).items()
    }
    return LocalCatalog(heroes, models, aliases, str(payload.get("source_root", "")))


def load_or_build_catalog(
    unpacked_root: Path,
    force: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[LocalCatalog, bool, Path]:
    """Load the persistent index, rebuilding it after script3 changes."""
    unpacked_root = unpacked_root.resolve()
    script3_root = unpacked_root / "script3"
    cache_path = unpacked_root / INDEX_NAME
    if not script3_root.is_dir():
        return LocalCatalog({}, {}, {}, str(unpacked_root)), False, cache_path

    if cache_path.is_file() and not force:
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and _cache_is_current(payload, script3_root):
                return _catalog_from_payload(payload), False, cache_path
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass

    modules = _discover_modules(script3_root, progress)
    missing = sorted(set(TARGET_MODULES.values()) - set(modules))
    if missing:
        raise ValueError("script3 缺少角色索引表：" + "、".join(missing))
    tables = {name: _read_module_table(path, name) for name, path in modules.items()}
    catalog = build_catalog_from_tables(
        tables["hero_brief"], tables["hero"], tables["skin"], tables["character"],
        str(script3_root),
    )
    if len(catalog.heroes) < 100 or len(catalog.models) < 100:
        raise ValueError(
            f"角色皮肤索引条目异常：式神 {len(catalog.heroes)}，皮肤 {len(catalog.models)}"
        )
    payload = {
        "schema": INDEX_SCHEMA,
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_root": str(script3_root),
        "manifest": _manifest_record(script3_root),
        "sources": {
            name: _source_record(path, script3_root)
            for name, path in sorted(modules.items())
        },
        "counts": {
            "heroes": len(catalog.heroes),
            "models": len(catalog.models),
            "aliases": len(catalog.aliases),
        },
        "heroes": [asdict(item) for item in catalog.heroes.values()],
        "models": [asdict(item) for item in catalog.models.values()],
        "aliases": catalog.aliases,
    }
    temporary = cache_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(cache_path)
    return catalog, True, cache_path


def find_unpacked_root(output_root: Path | None = None) -> Path | None:
    """Find the nearby Onmyoji unpack directory without scanning huge trees."""
    candidates: list[Path] = []
    if output_root is not None:
        current = output_root.resolve()
        candidates.extend([current, current.parent])
        candidates.extend(current.parents)
    project_root = Path(__file__).resolve().parent
    if output_root is None:
        candidates.extend([project_root / "unpacked", project_root])
    else:
        try:
            output_root.resolve().relative_to(project_root)
        except ValueError:
            pass
        else:
            candidates.extend([project_root / "unpacked", project_root])
    seen: set[str] = set()
    for candidate in candidates:
        variants = [candidate]
        if candidate.name.lower() != "unpacked":
            variants.append(candidate / "unpacked")
        for root in variants:
            key = str(root).lower()
            if key in seen:
                continue
            seen.add(key)
            if (root / "script3").is_dir():
                return root.resolve()
    return None
