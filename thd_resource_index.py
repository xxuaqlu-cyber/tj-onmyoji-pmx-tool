# -*- coding: utf-8 -*-
"""阴阳师 cloudfilesys3 的 THD 资源索引解析。

model.thx 保存“资源路径哈希 -> 内容 MD5/类型/大小”；
model.thp 保存“父资源路径哈希 -> 依赖资源路径哈希列表”。
两者合起来可以恢复 GIM、Mesh、材质 XML 与 KTX 的精确资源组，
无需猜测 WPK 中相邻条目。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True, frozen=True)
class ThxRecord:
    name_hash: int
    aux: int
    kind: int
    flags: int
    size: int
    content_md5: str


def _read_u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise ValueError(f"THFB 偏移越界：0x{offset:x}")
    return struct.unpack_from("<I", data, offset)[0]


_MASK64 = 0xFFFFFFFFFFFFFFFF
_XXH64_PRIME1 = 0x9E3779B185EBCA87
_XXH64_PRIME2 = 0xC2B2AE3D27D4EB4F
_XXH64_PRIME3 = 0x165667B19E3779F9
_XXH64_PRIME4 = 0x85EBCA77C2B2AE63
_XXH64_PRIME5 = 0x27D4EB2F165667C5


def _rotl64(value: int, count: int) -> int:
    return ((value << count) | (value >> (64 - count))) & _MASK64


def _xxh64_round(accumulator: int, lane: int) -> int:
    accumulator = (
        accumulator + lane * _XXH64_PRIME2
    ) & _MASK64
    accumulator = _rotl64(accumulator, 31)
    return accumulator * _XXH64_PRIME1 & _MASK64


def xxh64(data: bytes, seed: int = 0) -> int:
    """不依赖第三方库的 XXH64；与 APK 内 cloudfilesys 实现一致。"""
    length = len(data)
    offset = 0
    if length >= 32:
        v1 = (seed + _XXH64_PRIME1 + _XXH64_PRIME2) & _MASK64
        v2 = (seed + _XXH64_PRIME2) & _MASK64
        v3 = seed & _MASK64
        v4 = (seed - _XXH64_PRIME1) & _MASK64
        limit = length - 32
        while offset <= limit:
            v1 = _xxh64_round(v1, struct.unpack_from("<Q", data, offset)[0])
            v2 = _xxh64_round(v2, struct.unpack_from("<Q", data, offset + 8)[0])
            v3 = _xxh64_round(v3, struct.unpack_from("<Q", data, offset + 16)[0])
            v4 = _xxh64_round(v4, struct.unpack_from("<Q", data, offset + 24)[0])
            offset += 32
        value = (
            _rotl64(v1, 1)
            + _rotl64(v2, 7)
            + _rotl64(v3, 12)
            + _rotl64(v4, 18)
        ) & _MASK64
        for lane in (v1, v2, v3, v4):
            value ^= _xxh64_round(0, lane)
            value = (
                value * _XXH64_PRIME1 + _XXH64_PRIME4
            ) & _MASK64
    else:
        value = (seed + _XXH64_PRIME5) & _MASK64

    value = (value + length) & _MASK64
    while offset + 8 <= length:
        lane = _xxh64_round(0, struct.unpack_from("<Q", data, offset)[0])
        value ^= lane
        value = (
            _rotl64(value, 27) * _XXH64_PRIME1 + _XXH64_PRIME4
        ) & _MASK64
        offset += 8
    if offset + 4 <= length:
        value ^= (
            struct.unpack_from("<I", data, offset)[0] * _XXH64_PRIME1
        ) & _MASK64
        value = (
            _rotl64(value, 23) * _XXH64_PRIME2 + _XXH64_PRIME3
        ) & _MASK64
        offset += 4
    while offset < length:
        value ^= data[offset] * _XXH64_PRIME5 & _MASK64
        value = _rotl64(value, 11) * _XXH64_PRIME1 & _MASK64
        offset += 1

    value ^= value >> 33
    value = value * _XXH64_PRIME2 & _MASK64
    value ^= value >> 29
    value = value * _XXH64_PRIME3 & _MASK64
    value ^= value >> 32
    return value & _MASK64


def read_thx_namehash_seeds(path: Path) -> tuple[int, ...]:
    """读取 THX 头中的 namehash 种子向量（当前阴阳师为 0xA3、0x25）。"""
    data = path.read_bytes()
    if data[:4] != b"THFB" or len(data) < 0x58:
        raise ValueError(f"{path.name}: 不是有效的 THFB 索引")
    count = _read_u32(data, 0x54)
    start = 0x58
    end = start + count * 8
    if not 0 < count <= 16 or end > len(data):
        raise ValueError(f"{path.name}: namehash 种子向量无效")
    return struct.unpack_from(f"<{count}Q", data, start)


def cloudfilesys_name_hash(
    resource_path: str,
    package_name: str,
    seed: int,
) -> int:
    """按客户端规则规范化路径、去掉包名前缀并计算 THX 的 64 位键。"""
    normalized = resource_path.strip().replace("\\", "/").lower().lstrip("/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    package_prefix = package_name.strip().replace("\\", "/").lower().strip("/")
    if package_prefix and normalized.startswith(package_prefix + "/"):
        normalized = normalized[len(package_prefix) + 1 :]
    return xxh64(normalized.encode("utf-8"), seed)


def _read_hash_vector(data: bytes) -> tuple[list[int], int]:
    if data[:4] != b"THFB":
        raise ValueError("不是 THFB 文件")
    count = _read_u32(data, 0x74)
    start = 0x78
    end = start + count * 8
    if end > len(data):
        raise ValueError("THFB 哈希向量越界")
    hashes = list(struct.unpack_from(f"<{count}Q", data, start))
    return hashes, count


def _parallel_vector_offset(data: bytes) -> int:
    """读取根表中第二个并行向量的 count 位置。"""
    position = 0x30 + _read_u32(data, 0x30)
    if position + 4 > len(data):
        raise ValueError("THFB 并行向量越界")
    return position


def read_model_thx(path: Path) -> list[ThxRecord]:
    """解析 model.thx，保持文件中的哈希排序顺序。"""
    data = path.read_bytes()
    hashes, count = _read_hash_vector(data)
    count_position = _parallel_vector_offset(data)
    metadata_count = _read_u32(data, count_position)
    if metadata_count != count:
        raise ValueError(
            f"{path.name}: 哈希数 {count} 与元数据数 {metadata_count} 不一致"
        )

    start = count_position + 4
    record_size = 24
    if start + count * record_size > len(data):
        raise ValueError(f"{path.name}: 元数据向量越界")

    records: list[ThxRecord] = []
    for index, name_hash in enumerate(hashes):
        offset = start + index * record_size
        aux, kind, flags, size = struct.unpack_from("<HBBI", data, offset)
        content_md5 = data[offset + 8 : offset + 24].hex()
        records.append(
            ThxRecord(name_hash, aux, kind, flags, size, content_md5)
        )
    return records


def _table_field_offset(data: bytes, table: int, field_index: int) -> int:
    """返回 FlatBuffers 表字段在对象内的偏移；字段缺省时返回 0。"""
    if table < 0 or table + 4 > len(data):
        return 0
    signed_vtable_offset = struct.unpack_from("<i", data, table)[0]
    vtable = table - signed_vtable_offset
    if vtable < 0 or vtable + 4 > len(data):
        return 0
    vtable_size = struct.unpack_from("<H", data, vtable)[0]
    entry = vtable + 4 + field_index * 2
    if entry + 2 > vtable + vtable_size or entry + 2 > len(data):
        return 0
    return struct.unpack_from("<H", data, entry)[0]


def _read_hash_field(data: bytes, table: int, field_index: int) -> list[int]:
    field_offset = _table_field_offset(data, table, field_index)
    if not field_offset:
        return []
    field = table + field_offset
    relative = _read_u32(data, field)
    if not relative:
        return []
    vector = field + relative
    count = _read_u32(data, vector)
    start = vector + 4
    end = start + count * 8
    if end > len(data):
        raise ValueError("THP 依赖向量越界")
    return list(struct.unpack_from(f"<{count}Q", data, start))


def read_model_thp_fields(path: Path) -> dict[int, tuple[list[int], list[int]]]:
    """解析 model.thp，并保留每个父资源的两个原始依赖字段。"""
    data = path.read_bytes()
    parents, count = _read_hash_vector(data)
    count_position = _parallel_vector_offset(data)
    table_count = _read_u32(data, count_position)
    if table_count != count:
        raise ValueError(
            f"{path.name}: 父资源数 {count} 与依赖表数 {table_count} 不一致"
        )

    slots = count_position + 4
    if slots + count * 4 > len(data):
        raise ValueError(f"{path.name}: 依赖表指针越界")

    result: dict[int, tuple[list[int], list[int]]] = {}
    for index, parent_hash in enumerate(parents):
        slot = slots + index * 4
        relative = _read_u32(data, slot)
        if not relative:
            result[parent_hash] = ([], [])
            continue
        table = slot + relative
        result[parent_hash] = (
            _read_hash_field(data, table, 0),
            _read_hash_field(data, table, 1),
        )
    return result


def read_model_thp(path: Path) -> dict[int, list[int]]:
    """解析 model.thp，返回父资源哈希到有序依赖哈希列表。"""
    return {
        parent_hash: first + second
        for parent_hash, (first, second) in read_model_thp_fields(path).items()
    }


def thd_directory_from_resource_root(resource_root: Path) -> Path | None:
    candidate = resource_root.parent / "thd"
    if (candidate / "model.thx").is_file() and (candidate / "model.thp").is_file():
        return candidate
    return None


if __name__ == "__main__":
    import sys

    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    thx = read_model_thx(base / "model.thx")
    thp = read_model_thp(base / "model.thp")
    print(f"THX records: {len(thx):,}")
    print(f"THP parents: {len(thp):,}")
