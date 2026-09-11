"""Fast detection and repair of PMXEditor-incompatible texture tables."""

from __future__ import annotations

import json
import os
import struct
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable


MIGRATION_SCHEMA = 1
MIGRATION_MARKER = ".pmx_editor_compat_v1.json"


def read_pmx_texture_paths(path: Path) -> tuple[str, ...]:
    """Read only the PMX header/vertices/indexes needed to reach its texture table."""
    data = Path(path).read_bytes()
    size = len(data)
    offset = 0

    def take(length: int) -> bytes:
        nonlocal offset
        if length < 0 or offset + length > size:
            raise ValueError("PMX 文件被截断")
        value = data[offset : offset + length]
        offset += length
        return value

    def i32() -> int:
        return struct.unpack("<i", take(4))[0]

    def skip_text() -> None:
        nonlocal offset
        length = i32()
        if length < 0 or length > 256 * 1024 * 1024:
            raise ValueError(f"PMX 文本长度异常：{length}")
        take(length)

    if take(4) != b"PMX ":
        raise ValueError("不是 PMX 文件")
    version = struct.unpack("<f", take(4))[0]
    if not (1.9 <= version <= 2.2):
        raise ValueError(f"不支持的 PMX 版本：{version:g}")
    header_size = take(1)[0]
    globals_data = take(header_size)
    if header_size < 8:
        raise ValueError("PMX 全局设置被截断")
    additional_uvs = globals_data[1]
    vertex_index_size = globals_data[2]
    bone_index_size = globals_data[5]
    if additional_uvs > 4 or vertex_index_size not in (1, 2, 4):
        raise ValueError("PMX 顶点设置异常")
    if bone_index_size not in (1, 2, 4):
        raise ValueError("PMX 骨骼索引宽度异常")

    for _ in range(4):
        skip_text()

    vertex_count = i32()
    if vertex_count < 0:
        raise ValueError("PMX 顶点数量异常")
    fixed_vertex_bytes = 32 + additional_uvs * 16
    deform_bytes = {
        0: bone_index_size,
        1: bone_index_size * 2 + 4,
        2: bone_index_size * 4 + 16,
        3: bone_index_size * 2 + 40,
        4: bone_index_size * 4 + 16,
    }
    for _ in range(vertex_count):
        offset += fixed_vertex_bytes
        if offset >= size:
            raise ValueError("PMX 顶点表被截断")
        deform_type = data[offset]
        offset += 1
        if deform_type not in deform_bytes:
            raise ValueError("PMX 顶点权重类型异常")
        offset += deform_bytes[deform_type] + 4
        if offset > size:
            raise ValueError("PMX 顶点表被截断")

    surface_index_count = i32()
    if surface_index_count < 0:
        raise ValueError("PMX 面索引数量异常")
    take(surface_index_count * vertex_index_size)

    texture_count = i32()
    if texture_count < 0:
        raise ValueError("PMX 贴图数量异常")
    encoding = "utf-16-le" if globals_data[0] == 0 else "utf-8"
    textures: list[str] = []
    for _ in range(texture_count):
        length = i32()
        if length < 0 or length > 256 * 1024 * 1024:
            raise ValueError(f"PMX 贴图路径长度异常：{length}")
        textures.append(take(length).decode(encoding, errors="replace"))
    return tuple(textures)


def duplicate_texture_paths(path: Path) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in read_pmx_texture_paths(path):
        key = value.replace("/", "\\").lower()
        if key in seen:
            duplicates.append(value)
        else:
            seen.add(key)
    return tuple(duplicates)


def repair_duplicate_texture_paths(path: Path) -> int:
    """Deduplicate a generated PMX texture table and atomically replace it."""
    path = Path(path).resolve()
    duplicates = duplicate_texture_paths(path)
    if not duplicates:
        return 0

    import pymeshio.pmx.reader
    import pymeshio.pmx.writer

    model = pymeshio.pmx.reader.read_from_file(str(path))
    old_to_new: dict[int, int] = {}
    unique_paths: list[str] = []
    by_key: dict[str, int] = {}
    for old_index, value in enumerate(model.textures):
        key = str(value).replace("/", "\\").lower()
        new_index = by_key.get(key)
        if new_index is None:
            new_index = len(unique_paths)
            by_key[key] = new_index
            unique_paths.append(str(value))
        old_to_new[old_index] = new_index

    def remap(index: int) -> int:
        return old_to_new.get(int(index), -1) if int(index) >= 0 else -1

    for material in model.materials:
        material.texture_index = remap(material.texture_index)
        material.sphere_texture_index = remap(material.sphere_texture_index)
        if int(material.toon_sharing_flag) == 0:
            material.toon_texture_index = remap(material.toon_texture_index)
    model.textures[:] = unique_paths

    temporary = path.with_suffix(path.suffix + ".pmxeditor.tmp")
    temporary.unlink(missing_ok=True)
    try:
        pymeshio.pmx.writer.write_to_file(model, str(temporary))
        if duplicate_texture_paths(temporary):
            raise ValueError("PMX 贴图表去重验证失败")
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return len(duplicates)


def migrate_pmx_tree(
    root: Path,
    progress: Callable[[int, int, int, int], None] | None = None,
    force: bool = False,
) -> tuple[int, int, int]:
    """One-time migration of existing generated PMXs.

    Returns ``(checked, repaired, failed)``. New PMXs are emitted without
    duplicate texture paths, so a marker safely skips subsequent full scans.
    """
    root = Path(root).resolve()
    marker = root / MIGRATION_MARKER
    if marker.is_file() and not force:
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
            if payload.get("schema") == MIGRATION_SCHEMA:
                return 0, 0, 0
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    paths = list(root.rglob("*.pmx")) if root.is_dir() else []
    repaired = failed = 0

    def repair_one(path: Path) -> tuple[int, int]:
        try:
            return int(repair_duplicate_texture_paths(path) > 0), 0
        except Exception:
            return 0, 1

    workers = min(8, max(2, (os.cpu_count() or 4) // 2))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = executor.map(repair_one, paths)
        for index, (did_repair, did_fail) in enumerate(results, 1):
            repaired += did_repair
            failed += did_fail
            if progress is not None and (index == len(paths) or index % 100 == 0):
                progress(index, len(paths), repaired, failed)
    if failed == 0:
        marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = marker.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema": MIGRATION_SCHEMA,
                    "checked": len(paths),
                    "repaired": repaired,
                    "failed": failed,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(marker)
    return len(paths), repaired, failed
