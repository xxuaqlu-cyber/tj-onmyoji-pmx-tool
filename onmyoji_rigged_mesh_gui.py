# -*- coding: utf-8 -*-
r"""
阴阳师 WPK / 旧版 NPK：角色/静态道具 Mesh 与 PMX 转换器

默认扫描：
    新版：当前脚本目录\unpacked\model
    旧版：当前脚本目录\unpacked_npk\model

Mesh 类型：
    bone_exist(uint32) != 0 为带骨模型；bone_exist == 0 为合法静态道具/附件，
    转 PMX 时自动生成 __static_root__，并在明确归属角色时尽量并入主 PMX。

支持本批资源中出现的 mesh v2 / v3 / v4。v4 的父骨骼与顶点骨骼
索引为 uint16；旧版转换器常按 uint8 读取，因此会错位。

用法：
    双击“启动带骨模型工具.bat”
    或 python onmyoji_rigged_mesh_gui.py
    自检 python onmyoji_rigged_mesh_gui.py --self-test
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import traceback
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "阴阳师 PMX 一键解包工具"
# 这里只表示 PMX 文件本身的输出兼容版本。材质匹配规则、报告格式或 GUI
# 调整不应修改它，否则所有 .build.json 会同时失效并触发一次全量重写。
PMX_OUTPUT_FORMAT_VERSION = 33
# 材质 resolver 的输入/规则兼容版本。只在匹配逻辑会改变最终材质包时递增；
# GUI、报告和预览器调整不得递增。
MATERIAL_RESOLVER_VERSION = 43
# 主体/附件组合发现规则版本；只影响“完整组合”，不抬高 PMX 文件格式版本。
COMPOSITE_RESOLVER_VERSION = 6
# 场景 PMX 的布局烘焙版本。只在 SCN 层级、坐标变换、分块或场景材质
# 规则变化时递增；普通角色模型规则变化不应让场景缓存全部失效。
SCENE_PMX_PIPELINE_VERSION = 3
SCENE_PMX_VERTEX_LIMIT = 500_000
_RES_ASSET_PATHS_MEMORY_CACHE: dict[tuple[object, ...], list[str]] = {}
_SCRIPT3_GIM_PATHS_MEMORY_CACHE: dict[tuple[object, ...], list[str]] = {}
_FX_ASSET_PATHS_MEMORY_CACHE: dict[tuple[object, ...], list[str]] = {}
_FX_TEX0_BINDINGS_MEMORY_CACHE: dict[
    tuple[object, ...], dict[str, list[str]]
] = {}
_ORPHAN_MANIFEST_CACHE: dict[
    str, tuple[tuple[object, ...], dict[str, Path], dict[Path, str]]
] = {}
_PMX_BUILD_OUTPUT_CACHE: dict[str, dict[str, list[Path]]] = {}
TRUSTED_MATERIAL_CONFIDENCE = frozenset({
    "旧NPK物理组精确",
    "旧NPK物理组精确主贴图",
    "旧NPK几何组精确",
    "旧NPK几何组精确主贴图",
    "hot-update-directory-exact",
    "hot-update-directory-exact-main-texture",
    "THD-logical-family-GIM-exact",
    "THD-logical-family-GIM-exact-main-texture",
    "THD精确",
    "THD精确主贴图",
    "THD精确部分主贴图",
    "THD精确部分材质",
    "THD精确部分材质主贴图",
    "THD精确部分材质部分主贴图",
    "THD语义材质精确",
    "THD语义材质精确主贴图",
    "THD语义材质精确部分主贴图",
    "THD路径自举",
    "THD路径自举主贴图",
    "THD路径自举部分主贴图",
    "THD共享材质精确",
    "THD共享材质精确主贴图",
    "THD共享材质精确部分主贴图",
    "THD纹理等价精确",
    "THD纹理等价精确主贴图",
    "THD纹理等价精确部分主贴图",
    "骨架路径精确",
    "骨架路径精确主贴图",
    "骨架路径精确部分主贴图",
    "骨架路径共享材质精确",
    "骨架路径共享材质精确主贴图",
    "骨架路径共享材质精确部分主贴图",
    "APK-THD精确",
    "APK-THD精确主贴图",
    "APK-THD精确部分主贴图",
    "GIM路径精确",
    "GIM路径精确主贴图",
    "GIM路径精确部分主贴图",
    "APK-GIM路径精确",
    "APK-GIM路径精确主贴图",
    "APK-GIM路径精确部分主贴图",
    "script3路径精确",
    "script3路径精确主贴图",
    "script3路径精确部分主贴图",
    "APK-script3路径精确",
    "APK-script3路径精确主贴图",
    "APK-script3路径精确部分主贴图",
    "res路径精确",
    "res路径精确主贴图",
    "res路径精确部分主贴图",
    "APK-res路径精确",
    "APK-res路径精确主贴图",
    "APK-res路径精确部分主贴图",
    "res代理GIM精确",
    "res代理GIM精确主贴图",
    "res代理GIM精确部分主贴图",
    "res透明表面精确",
    "res透明表面精确主贴图",
    "res透明表面精确部分主贴图",
    "res庭院合并精确",
    "res庭院合并精确主贴图",
    "res庭院合并精确部分主贴图",
    "res编号合并精确",
    "res编号合并精确主贴图",
    "res编号合并精确部分主贴图",
    "res派生GIM子集精确",
    "res派生GIM子集精确主贴图",
    "res派生GIM子集精确部分主贴图",
    "THD父节点变体精确",
    "THD父节点变体精确主贴图",
    "THD父节点变体精确部分主贴图",
    "THD-single-slot-direct-texture",
    "THD-uniform-direct-texture",
    "THD-nested-gim-exact",
    "historical-thd-exact",
    "historical-thd-exact-main-texture",
    "historical-thd-segment-exact",
    "historical-thd-segment-exact-main-texture",
    "orphan-path-uv-exact",
    "orphan-path-uv-exact-main-texture",
    "exact-render-target-family",
    "THD-GIM-name-material",
    "THD-unique-logical-image",
    "THD-unique-directory-image-UV",
    "supplemental-GIM-name-material",
    "supplemental-embedded-GIM-exact",
    "supplemental-unique-logical-image",
    "supplemental-unique-directory-image",
    "supplemental-FX-Tex0-exact",
    "exact-logical-GIM-material-variant",
    "zhujue-mode-consensus-exact",
    "zhujue-mode-consensus-exact-main-texture",
    "人工验证",
})
MESH_MAGIC = b"\x34\x80\xC8\xBB"
MAX_REASONABLE_COUNT = 2_000_000
IMAGE_SUFFIXES = {".tga", ".png", ".dds", ".jpg", ".jpeg", ".bmp", ".ktx"}
ASTC_BLOCK_SIZES = [
    (4, 4), (5, 4), (5, 5), (6, 5), (6, 6), (8, 5), (8, 6),
    (8, 8), (10, 5), (10, 6), (10, 8), (10, 10), (12, 10), (12, 12),
]

# 只有资源关系可以人工闭环时才登记。静态件会保留独立 PMX，同时额外生成
# “主模型 + 静态道具”组合 PMX；不做目录模糊自动拼接。
VERIFIED_STATIC_ATTACHMENTS = (
    (
        "76da29ebe8f41ba523d0e05f13c7e15a",  # xuzuozhinan_show_touming
        "5ec2adb59a981a4abe13fa9a44f77f87",  # xuzuozhinan_show_guci
        "xuzuozhinan_show_touming_含骨刺",
        None,  # 根骨
    ),
    (
        "3399b1f1590eb3159a6c293394bfebac",  # s5_sp_xuenv
        "7de14f0346a67a8252e247e90cc551b3",  # s5_sp_xuenv_erhuan
        "s5_sp_xuenv_含耳环",
        "bip01_head",
    ),
)

# 多级 Socket 中存在“主体全骨架 -> 独立子骨架 -> 静态件”的情况。
# 只登记已证明子骨架可无损重映射到主体骨架的闭环案例；合并器仍会再次
# 校验父链、有效权重骨与 bind 对齐变换，任一条件不满足就拒绝写出。
VERIFIED_RIGGED_SUBSET_COMPOSITES = (
    (
        "d5e19cf98a878917e96a1be719c30ffe",  # sp_huang 主体
        "153c94f4dd09b60a65d304d9631ff900",  # sp_huang_qiu，3骨子骨架
        "31f1e0bcd33aff3c0dc8d466bd96c53f",  # sp_huang_galaxy，静态
        "sp_huang_含星球银河",
        "bip01_bone29",  # galaxy 在 qiu 中的精确 Socket 目标骨
    ),
)


class MeshFormatError(ValueError):
    pass


@dataclass(slots=True)
class MeshSummary:
    path: Path
    version: int
    bone_count: int
    size: int
    status: str = "可转换"
    source_order: int = -1
    modified_ns: int = 0


@dataclass(slots=True)
class ParsedMesh:
    version: int
    submeshes: list[tuple[int, int, int, int]]
    bone_parents: list[int]
    bone_names: list[str]
    bone_matrices: list[tuple[float, ...]]
    positions: list[tuple[float, float, float]]
    normals: list[tuple[float, float, float]]
    faces: list[tuple[int, int, int]]
    uvs: list[tuple[float, float]]
    joints: list[tuple[int, int, int, int]]
    weights: list[tuple[float, float, float, float]]


@dataclass(slots=True, frozen=True)
class SkeletonHierarchy:
    source: Path
    name: str
    bone_names: tuple[str, ...]
    bone_keys: tuple[str, ...]
    bone_parents: tuple[int, ...]
    bone_bind_transforms: tuple[tuple[float, ...], ...] = ()


@dataclass(slots=True)
class SkeletonHierarchyIndex:
    layouts: tuple[SkeletonHierarchy, ...]
    by_bone: dict[str, frozenset[int]]


_SKELETON_HIERARCHY_CACHE: dict[Path, SkeletonHierarchyIndex] = {}
_SKELETON_HIERARCHY_CACHE_LOCK = threading.Lock()


@dataclass(slots=True)
class MaterialDefinition:
    name: str
    textures: dict[str, str]
    diffuse_color: tuple[float, float, float, float] | None = None


PRIMARY_TEXTURE_SLOTS = (
    "tex0",
    "texdiffuse",
    "diffusetex",
    "diffuse",
    "diffusemap",
    "texalbedo",
    "albedotex",
    "albedomap",
    "base_albedo_texture",
    "texbasecolor",
    "basecolortex",
    "base_color_texture",
    "texcolor",
    "maintex",
    "maintexture",
)


def material_primary_texture(material: MaterialDefinition) -> str | None:
    """返回 NeoX 材质的主颜色贴图；兼容新旧 shader 的槽位命名。"""
    by_slot = {slot.lower(): value for slot, value in material.textures.items()}
    for slot in PRIMARY_TEXTURE_SLOTS:
        value = by_slot.get(slot)
        if value:
            return value
    return None


@dataclass(slots=True)
class GimSubmesh:
    name: str
    material_index: int
    bounding_center: tuple[float, float, float] | None = None
    bounding_half: tuple[float, float, float] | None = None


@dataclass(slots=True)
class MaterialPackage:
    xml_path: Path
    index: int
    package_name: str
    materials: list[MaterialDefinition]
    mesh_paths: list[Path]
    texture_map: dict[str, Path]
    confidence: str


@dataclass(slots=True)
class CompositeModel:
    name: str
    mesh_paths: list[Path]
    packages: list[MaterialPackage]
    # 与 mesh_paths 对齐；仅静态组件使用。None 表示挂角色根骨。
    static_bone_names: list[str | None] | None = None
    # NeoX Socket 的 MatrixToBone，仍按源坐标系的 row-vector 4x4 存储。
    # None 表示组件顶点已经处在主模型空间，无需额外变换。
    static_matrices: list[tuple[float, ...] | None] | None = None
    # 用于审计自动组合为什么成立；不会参与 PMX 数据结构。
    evidence: str = ""
    # True 表示关系唯一且为模型固有组成：成品清单只保留合并结果，隐藏独立件。
    # 可选 Socket 姿态/换装变体保持 False。
    direct_merge: bool = False


@dataclass(slots=True)
class SceneCatalogEntry:
    source_path: Path
    display_name: str
    content_path: str
    model_count: int
    component_group_count: int
    effect_count: int


@dataclass(slots=True)
class SceneModelInstance:
    name: str
    uuid: str
    logical_gim: str
    material_override: str | None
    transform: tuple[float, ...]
    component_group: str | None = None


def package_primary_texture_sources(package: MaterialPackage) -> frozenset[Path]:
    """返回材质真正使用的主颜色贴图实体，忽略法线/混合等辅助纹理。"""
    normalized_map = {
        key.strip().replace("\\", "/").lower(): value
        for key, value in package.texture_map.items()
    }
    result: set[Path] = set()
    for material in package.materials:
        reference = material_primary_texture(material)
        if not reference:
            continue
        source = package.texture_map.get(reference)
        if source is None:
            source = normalized_map.get(
                reference.strip().replace("\\", "/").lower()
            )
        if source is not None:
            result.add(Path(os.path.abspath(source)))
    return frozenset(result)


class Reader:
    def __init__(self, data: bytes, source: Path):
        self.data = data
        self.pos = 0
        self.source = source

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def seek(self, offset: int, whence: int = 0) -> None:
        if whence == 0:
            target = offset
        elif whence == 1:
            target = self.pos + offset
        elif whence == 2:
            target = len(self.data) + offset
        else:
            raise ValueError("无效 whence")
        if target < 0 or target > len(self.data):
            raise MeshFormatError(f"{self.source.name}: 跳转越界 {target}")
        self.pos = target

    def read(self, size: int) -> bytes:
        if size < 0 or self.pos + size > len(self.data):
            raise MeshFormatError(
                f"{self.source.name}: 读取越界，位置 {self.pos}，需要 {size} 字节，"
                f"剩余 {self.remaining()}"
            )
        out = self.data[self.pos : self.pos + size]
        self.pos += size
        return out

    def unpack(self, fmt: str):
        size = struct.calcsize(fmt)
        return struct.unpack(fmt, self.read(size))

    def u8(self) -> int:
        return self.unpack("<B")[0]

    def u16(self) -> int:
        return self.unpack("<H")[0]

    def u32(self) -> int:
        return self.unpack("<I")[0]

    def f32(self) -> float:
        return self.unpack("<f")[0]


def _check_count(value: int, label: str, source: Path) -> None:
    if value < 0 or value > MAX_REASONABLE_COUNT:
        raise MeshFormatError(f"{source.name}: {label} 数量异常：{value}")


def _mesh_expected_end(path: Path, version: int, bone_count: int) -> int | None:
    """读取 mesh 头中的逻辑结束位置，用于识别第一次解包产生的截断文件。"""
    try:
        data = path.read_bytes()
        r = Reader(data, path)
        r.read(12)
        if r.u16() != bone_count:
            return None
        parent_width = 2 if version >= 4 else 1
        r.read(bone_count * parent_width)
        r.read(bone_count * 32)
        has_extra = r.u8()
        if has_extra:
            r.read(bone_count * 28)
        r.read(bone_count * 64)
        r.u8()
        return r.u32()
    except Exception:
        return None


def read_mesh_summary(path: Path) -> MeshSummary | None:
    try:
        with path.open("rb") as stream:
            header = stream.read(16)
        if len(header) < 12 or header[:4] != MESH_MAGIC:
            return None
        version = header[4]
        bone_exist = int.from_bytes(header[8:12], "little")
        size = path.stat().st_size
        if bone_exist == 0:
            if len(header) < 16:
                return None
            expected_end = int.from_bytes(header[12:16], "little")
            bone_count = 0
            if version in (2, 3, 4) and expected_end <= size:
                status = "可转换（静态）"
            else:
                status = "需从 WPK 修复"
        else:
            bone_count = int.from_bytes(header[12:14], "little") if len(header) >= 14 else 0
            expected_end = _mesh_expected_end(path, version, bone_count)
            if version in (2, 3, 4) and (expected_end is None or expected_end > size):
                status = "需从 WPK 修复"
            else:
                status = "可转换" if version in (2, 3, 4) else "未知版本"
        order = archive_index(path)
        try:
            modified_ns = path.stat().st_mtime_ns
        except OSError:
            modified_ns = 0
        return MeshSummary(
            path=path,
            version=version,
            bone_count=bone_count,
            size=size,
            status=status,
            source_order=order if order is not None else -1,
            modified_ns=modified_ns,
        )
    except OSError:
        return None


def scan_rigged_mesh_paths(
    files: list[Path],
    progress: Callable[[int, int], None] | None = None,
) -> list[MeshSummary]:
    rows: list[MeshSummary] = []
    total = len(files)
    for index, path in enumerate(files, 1):
        row = read_mesh_summary(path)
        if row is not None:
            rows.append(row)
        if progress and (index % 50 == 0 or index == total):
            progress(index, total)
    rows.sort(key=lambda row: (row.version, row.bone_count, row.path.name.lower()))
    return rows


def scan_rigged_meshes(
    folder: Path,
    progress: Callable[[int, int], None] | None = None,
) -> list[MeshSummary]:
    return scan_rigged_mesh_paths(list(folder.rglob("*.mesh")), progress)


def _decode_bone_name(raw: bytes, index: int) -> str:
    raw = raw.split(b"\0", 1)[0]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gb18030", errors="replace")
    text = text.strip().replace(" ", "_")
    return text or f"bone_{index:03d}"


def read_mesh_submesh_count(path: Path) -> int:
    """只读取 Mesh 头和子网格表；材质分析无需遍历全部顶点。"""
    with path.open("rb") as stream:
        magic = stream.read(8)
        if len(magic) != 8 or magic[:4] != MESH_MAGIC:
            raise MeshFormatError(f"{path.name}: 不是 NeoX mesh")
        version = magic[4]
        if version not in (2, 3, 4):
            raise MeshFormatError(f"{path.name}: 暂不支持 mesh v{version}")

        raw = stream.read(4)
        if len(raw) != 4:
            raise MeshFormatError(f"{path.name}: Mesh 头不完整")
        bone_exist = int.from_bytes(raw, "little")
        if bone_exist == 0:
            raw = stream.read(4)
            if len(raw) != 4:
                raise MeshFormatError(f"{path.name}: 静态 Mesh 头不完整")
        else:
            if bone_exist > 1:
                raw = stream.read(1)
                if not raw:
                    raise MeshFormatError(f"{path.name}: 扩展骨骼头不完整")
                extra_count = raw[0]
                stream.seek(2 + extra_count * 4, 1)

            raw = stream.read(2)
            if len(raw) != 2:
                raise MeshFormatError(f"{path.name}: 缺少骨骼数量")
            bone_count = int.from_bytes(raw, "little")
            _check_count(bone_count, "骨骼", path)
            if bone_count == 0:
                raise MeshFormatError(f"{path.name}: 骨骼数为 0")

            parent_width = 2 if version >= 4 else 1
            stream.seek(bone_count * parent_width + bone_count * 32, 1)
            raw = stream.read(1)
            if not raw:
                raise MeshFormatError(f"{path.name}: 骨骼区不完整")
            if raw[0]:
                stream.seek(bone_count * 28, 1)
            stream.seek(bone_count * 64 + 5, 1)

        count = 0
        while True:
            marker = stream.read(2)
            if len(marker) != 2:
                raise MeshFormatError(f"{path.name}: 子网格表不完整")
            if int.from_bytes(marker, "little") == 1:
                return count
            rest = stream.read(8)
            if len(rest) != 8:
                raise MeshFormatError(f"{path.name}: 子网格记录不完整")
            count += 1
            if count > 4096:
                raise MeshFormatError(f"{path.name}: 子网格数量异常")


def _read_skeleton_name_table(data: bytes) -> list[str] | None:
    try:
        marker = data.find(b"NAME")
        if marker < 0 or marker + 12 > len(data):
            return None
        _, count = struct.unpack_from("<II", data, marker + 4)
    except struct.error:
        return None
    if not 1 < count < 5000:
        return None
    position = marker + 12
    values: list[str] = []
    for _ in range(count):
        if position + 4 > len(data):
            return None
        length = struct.unpack_from("<I", data, position)[0]
        position += 4
        if length > 1024 or position + length > len(data):
            return None
        raw = data[position : position + length]
        position += length
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError:
            value = raw.decode("gb18030", errors="replace")
        values.append(value.strip().replace(" ", "_"))
    return values


def _valid_skeleton_parent_table(parents: tuple[int, ...]) -> bool:
    """验证 Skeleton 父表是有根无环森林。"""
    count = len(parents)
    if not parents or not any(parent < 0 for parent in parents):
        return False
    if any(
        parent >= count or parent < -1 or parent == index
        for index, parent in enumerate(parents)
    ):
        return False

    states = [0] * count

    def visit(index: int) -> bool:
        if states[index] == 2:
            return True
        if states[index] == 1:
            return False
        states[index] = 1
        parent = parents[index]
        if parent >= 0 and not visit(parent):
            return False
        states[index] = 2
        return True

    return all(visit(index) for index in range(count))


def read_skeleton_hierarchy(path: Path) -> SkeletonHierarchy | None:
    """读取 NeoX Skeleton 中的完整父链。

    Skeleton 是动画骨架的权威数据；Mesh 中的父表有时是为蒙皮
    精简过的版本。NeoX 序列化会在名称数组与 uint16 父表之间
    产生少量可变填充，所以用“索引合法、有根、无环”定位唯一父表。
    """
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data.startswith(b"SKELETON"):
        return None
    values = _read_skeleton_name_table(data)
    if values is None or len(values) < 2:
        return None
    data_marker = data.find(b"DATA")
    name_marker = data.find(b"NAME")
    if data_marker < 0 or name_marker < 0:
        return None

    bone_names = tuple(values[1:])
    bone_count = len(bone_names)
    expected_parent_offset = data_marker + 16 + bone_count * 4
    candidates: dict[tuple[int, ...], tuple[int, int, int]] = {}
    parent_positions: dict[tuple[int, ...], int] = {}
    for delta in range(-16, 18, 2):
        position = expected_parent_offset + delta
        if position < data_marker + 8 or position + bone_count * 2 > name_marker:
            continue
        try:
            raw = struct.unpack_from(f"<{bone_count}H", data, position)
        except struct.error:
            continue
        parents = tuple(-1 if value == 0xFFFF else value for value in raw)
        if not _valid_skeleton_parent_table(parents):
            continue
        root_count = sum(parent < 0 for parent in parents)
        forward_edges = sum(
            parent >= index
            for index, parent in enumerate(parents)
            if parent >= 0
        )
        candidates[parents] = (
            1 if root_count == 1 else 0,
            -forward_edges,
            -abs(delta),
        )
        parent_positions[parents] = position
    if not candidates:
        return None
    ranked = sorted(candidates.items(), key=lambda item: item[1], reverse=True)
    if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
        return None
    parents = ranked[0][0]
    parent_position = parent_positions[parents]
    bind_transforms: tuple[tuple[float, ...], ...] = ()
    # The bytes between the uint16 parent table and local TRS data vary between
    # NeoX exports.  In particular, character Skeletons in this game use a
    # two-byte alignment pad, while some older files have reserved uint32s.
    # Do not assume a single layout: accept the first nearby, internally valid
    # TRS block.  Missing this offset makes the preview fall back to an action's
    # first frame, which is often a visibly different pose from the PMX bind.
    bind_size = bone_count * 10 * 4
    bind_base = parent_position + bone_count * 2
    for padding in range(0, 18, 2):
        bind_offset = bind_base + padding
        if bind_offset + bind_size > name_marker:
            continue
        try:
            raw_bind = struct.unpack_from(
                f"<{bone_count * 10}f", data, bind_offset
            )
        except struct.error:
            continue
        grouped = tuple(
            tuple(raw_bind[index * 10 : (index + 1) * 10])
            for index in range(bone_count)
        )
        quaternion_lengths = tuple(
            math.sqrt(sum(value * value for value in transform[3:7]))
            for transform in grouped
        )
        scale_values = tuple(
            abs(value)
            for transform in grouped
            for value in transform[7:10]
        )
        if (
            all(
                all(math.isfinite(value) for value in transform)
                for transform in grouped
            )
            and all(0.5 <= length <= 1.5 for length in quaternion_lengths)
            and all(1.0e-6 <= value <= 1.0e3 for value in scale_values)
        ):
            bind_transforms = grouped
            break
    bone_keys = tuple(_normalized_bone_key(name) for name in bone_names)
    if len(set(bone_keys)) != len(bone_keys):
        return None
    return SkeletonHierarchy(
        source=path,
        name=values[0].strip(),
        bone_names=bone_names,
        bone_keys=bone_keys,
        bone_parents=parents,
        bone_bind_transforms=bind_transforms,
    )


def read_skeleton_name_and_bones(
    path: Path,
) -> tuple[str, frozenset[str]] | None:
    """读取 NeoX Skeleton 的骨架名和骨名集合。"""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    values = _read_skeleton_name_table(data)
    if values is None or len(values) < 2:
        return None
    name = values[0].strip()
    bones = frozenset(value for value in values[1:] if value)
    if not name or not bones:
        return None
    return name, bones


def read_mesh_bone_layout(
    path: Path,
) -> tuple[tuple[str, ...], tuple[int, ...], int]:
    """轻量读取组合判定所需的骨骼名称/父级和顶点数，不加载顶点数据。"""
    with path.open("rb") as stream:
        magic = stream.read(8)
        if len(magic) != 8 or magic[:4] != MESH_MAGIC:
            raise MeshFormatError(f"{path.name}: 不是 NeoX mesh")
        version = magic[4]
        if version not in (2, 3, 4):
            raise MeshFormatError(f"{path.name}: 暂不支持 mesh v{version}")

        raw = stream.read(4)
        if len(raw) != 4:
            raise MeshFormatError(f"{path.name}: Mesh 头不完整")
        bone_exist = int.from_bytes(raw, "little")
        if bone_exist == 0:
            raw = stream.read(4)
            if len(raw) != 4:
                raise MeshFormatError(f"{path.name}: 静态 Mesh 头不完整")
            bone_names = ("__static_root__",)
            bone_parents = (-1,)
        else:
            if bone_exist > 1:
                raw = stream.read(1)
                if not raw:
                    raise MeshFormatError(f"{path.name}: 扩展骨骼头不完整")
                stream.seek(2 + raw[0] * 4, 1)

            raw = stream.read(2)
            if len(raw) != 2:
                raise MeshFormatError(f"{path.name}: 缺少骨骼数量")
            bone_count = int.from_bytes(raw, "little")
            _check_count(bone_count, "骨骼", path)
            if bone_count == 0:
                raise MeshFormatError(f"{path.name}: 骨骼数为 0")

            parent_width = 2 if version >= 4 else 1
            sentinel = 0xFFFF if parent_width == 2 else 0xFF
            parents: list[int] = []
            for _ in range(bone_count):
                raw = stream.read(parent_width)
                if len(raw) != parent_width:
                    raise MeshFormatError(f"{path.name}: 骨骼父级表不完整")
                parent = int.from_bytes(raw, "little")
                parents.append(-1 if parent == sentinel else parent)
            bone_parents = tuple(parents)

            names: list[str] = []
            for index in range(bone_count):
                raw = stream.read(32)
                if len(raw) != 32:
                    raise MeshFormatError(f"{path.name}: 骨骼名称表不完整")
                names.append(_decode_bone_name(raw, index))
            bone_names = tuple(names)

            raw = stream.read(1)
            if not raw:
                raise MeshFormatError(f"{path.name}: 骨骼区不完整")
            if raw[0]:
                stream.seek(bone_count * 28, 1)
            stream.seek(bone_count * 64 + 5, 1)

        submesh_count = 0
        while True:
            marker = stream.read(2)
            if len(marker) != 2:
                raise MeshFormatError(f"{path.name}: 子网格表不完整")
            if int.from_bytes(marker, "little") == 1:
                break
            rest = stream.read(8)
            if len(rest) != 8:
                raise MeshFormatError(f"{path.name}: 子网格记录不完整")
            submesh_count += 1
            if submesh_count > 4096:
                raise MeshFormatError(f"{path.name}: 子网格数量异常")

        raw = stream.read(8)
        if len(raw) != 8:
            raise MeshFormatError(f"{path.name}: 总顶点/面数缺失")
        vertex_count, face_count = struct.unpack("<II", raw)
        _check_count(vertex_count, "顶点", path)
        _check_count(face_count, "三角面", path)
        return bone_names, bone_parents, vertex_count


def read_mesh_bone_bind_layout(
    path: Path,
) -> tuple[
    tuple[str, ...],
    tuple[int, ...],
    tuple[tuple[float, ...], ...],
]:
    """轻量读取骨名/父链/bind 矩阵，不进入子网格与顶点数据。"""
    with path.open("rb") as stream:
        magic = stream.read(8)
        if len(magic) != 8 or magic[:4] != MESH_MAGIC:
            raise MeshFormatError(f"{path.name}: 不是 NeoX mesh")
        version = magic[4]
        if version not in (2, 3, 4):
            raise MeshFormatError(f"{path.name}: 暂不支持 mesh v{version}")

        raw = stream.read(4)
        if len(raw) != 4:
            raise MeshFormatError(f"{path.name}: Mesh 头不完整")
        bone_exist = int.from_bytes(raw, "little")
        if bone_exist == 0:
            raw = stream.read(4)
            if len(raw) != 4:
                raise MeshFormatError(f"{path.name}: 静态 Mesh 头不完整")
            return (
                ("__static_root__",),
                (-1,),
                ((
                    1.0, 0.0, 0.0, 0.0,
                    0.0, 1.0, 0.0, 0.0,
                    0.0, 0.0, 1.0, 0.0,
                    0.0, 0.0, 0.0, 1.0,
                ),),
            )

        if bone_exist > 1:
            raw = stream.read(1)
            if not raw:
                raise MeshFormatError(f"{path.name}: 扩展骨骼头不完整")
            stream.seek(2 + raw[0] * 4, 1)

        raw = stream.read(2)
        if len(raw) != 2:
            raise MeshFormatError(f"{path.name}: 缺少骨骼数量")
        bone_count = int.from_bytes(raw, "little")
        _check_count(bone_count, "骨骼", path)
        if bone_count == 0:
            raise MeshFormatError(f"{path.name}: 骨骼数为 0")

        parent_width = 2 if version >= 4 else 1
        sentinel = 0xFFFF if parent_width == 2 else 0xFF
        parents: list[int] = []
        for _ in range(bone_count):
            raw = stream.read(parent_width)
            if len(raw) != parent_width:
                raise MeshFormatError(f"{path.name}: 骨骼父级表不完整")
            parent = int.from_bytes(raw, "little")
            parents.append(-1 if parent == sentinel else parent)

        names: list[str] = []
        for index in range(bone_count):
            raw = stream.read(32)
            if len(raw) != 32:
                raise MeshFormatError(f"{path.name}: 骨骼名称表不完整")
            names.append(_decode_bone_name(raw, index))

        raw = stream.read(1)
        if not raw:
            raise MeshFormatError(f"{path.name}: 骨骼区不完整")
        if raw[0]:
            stream.seek(bone_count * 28, 1)

        matrices: list[tuple[float, ...]] = []
        for _ in range(bone_count):
            raw = stream.read(64)
            if len(raw) != 64:
                raise MeshFormatError(f"{path.name}: 骨骼矩阵表不完整")
            matrices.append(struct.unpack("<16f", raw))
        return tuple(names), tuple(parents), tuple(matrices)


def read_mesh_render_layout(
    path: Path,
) -> tuple[int, tuple[tuple[int, int, int, int], ...], int, int]:
    """轻量读取决定渲染几何布局的字段，不加载顶点数组。"""
    with path.open("rb") as stream:
        magic = stream.read(8)
        if len(magic) != 8 or magic[:4] != MESH_MAGIC:
            raise MeshFormatError(f"{path.name}: 不是 NeoX mesh")
        version = magic[4]
        if version not in (2, 3, 4):
            raise MeshFormatError(f"{path.name}: 暂不支持 mesh v{version}")
        raw = stream.read(4)
        if len(raw) != 4:
            raise MeshFormatError(f"{path.name}: Mesh 头不完整")
        bone_exist = int.from_bytes(raw, "little")
        if bone_exist == 0:
            raw = stream.read(4)
            if len(raw) != 4:
                raise MeshFormatError(f"{path.name}: 静态 Mesh 头不完整")
        else:
            if bone_exist > 1:
                raw = stream.read(1)
                if not raw:
                    raise MeshFormatError(f"{path.name}: 扩展骨骼头不完整")
                stream.seek(2 + raw[0] * 4, 1)
            raw = stream.read(2)
            if len(raw) != 2:
                raise MeshFormatError(f"{path.name}: 缺少骨骼数量")
            bone_count = int.from_bytes(raw, "little")
            parent_width = 2 if version >= 4 else 1
            stream.seek(bone_count * parent_width + bone_count * 32, 1)
            raw = stream.read(1)
            if not raw:
                raise MeshFormatError(f"{path.name}: 骨骼区不完整")
            if raw[0]:
                stream.seek(bone_count * 28, 1)
            stream.seek(bone_count * 64 + 5, 1)

        submeshes: list[tuple[int, int, int, int]] = []
        while True:
            marker = stream.read(2)
            if len(marker) != 2:
                raise MeshFormatError(f"{path.name}: 子网格表不完整")
            if int.from_bytes(marker, "little") == 1:
                break
            rest = stream.read(8)
            if len(rest) != 8:
                raise MeshFormatError(f"{path.name}: 子网格记录不完整")
            raw_record = marker + rest
            submeshes.append(
                (
                    int.from_bytes(raw_record[0:4], "little"),
                    int.from_bytes(raw_record[4:8], "little"),
                    raw_record[8],
                    raw_record[9],
                )
            )
            if len(submeshes) > 4096:
                raise MeshFormatError(f"{path.name}: 子网格数量异常")
        raw = stream.read(8)
        if len(raw) != 8:
            raise MeshFormatError(f"{path.name}: 总顶点/面数缺失")
        vertex_count, face_count = struct.unpack("<II", raw)
        return version, tuple(submeshes), vertex_count, face_count


def _mesh_surface_fingerprint(mesh: ParsedMesh) -> str:
    """只比较材质绑定所依赖的表面几何：子网格、位置、法线、面与 UV。"""
    digest = hashlib.sha256()
    for item in mesh.submeshes:
        digest.update(struct.pack("<IIII", *item))
    for item in mesh.positions:
        digest.update(struct.pack("<fff", *item))
    for item in mesh.normals:
        digest.update(struct.pack("<fff", *item))
    for item in mesh.faces:
        digest.update(struct.pack("<HHH", *item))
    for item in mesh.uvs:
        digest.update(struct.pack("<ff", *item))
    return digest.hexdigest()


def _mesh_submesh_surface_fingerprints(mesh: ParsedMesh) -> tuple[str, ...]:
    """逐子网格计算不含蒙皮的表面指纹，用于验证无损合并/拆分 Mesh。"""
    result: list[str] = []
    vertex_offset = 0
    face_offset = 0
    for vertex_count, face_count, uv_count, material_count in mesh.submeshes:
        digest = hashlib.sha256()
        digest.update(
            struct.pack(
                "<IIII", vertex_count, face_count, uv_count, material_count
            )
        )
        for item in mesh.positions[vertex_offset : vertex_offset + vertex_count]:
            digest.update(struct.pack("<fff", *item))
        for item in mesh.normals[vertex_offset : vertex_offset + vertex_count]:
            digest.update(struct.pack("<fff", *item))
        for a, b, c in mesh.faces[face_offset : face_offset + face_count]:
            digest.update(
                struct.pack(
                    "<III",
                    a - vertex_offset,
                    b - vertex_offset,
                    c - vertex_offset,
                )
            )
        for item in mesh.uvs[vertex_offset : vertex_offset + vertex_count]:
            digest.update(struct.pack("<ff", *item))
        result.append(digest.hexdigest())
        vertex_offset += vertex_count
        face_offset += face_count
    return tuple(result)


def _mesh_render_fingerprint(mesh: ParsedMesh) -> str:
    """忽略骨架矩阵/名称，只比较实际送往 PMX 的渲染几何与蒙皮数据。"""
    digest = hashlib.sha256()
    for item in mesh.submeshes:
        digest.update(struct.pack("<IIII", *item))
    for item in mesh.positions:
        digest.update(struct.pack("<fff", *item))
    for item in mesh.normals:
        digest.update(struct.pack("<fff", *item))
    for item in mesh.faces:
        digest.update(struct.pack("<HHH", *item))
    for item in mesh.uvs:
        digest.update(struct.pack("<ff", *item))
    joint_format = "<HHHH" if mesh.version >= 4 else "<BBBB"
    for item in mesh.joints:
        digest.update(struct.pack(joint_format, *item))
    for item in mesh.weights:
        digest.update(struct.pack("<ffff", *item))
    return digest.hexdigest()


def parse_mesh_bytes(data: bytes, source: Path) -> ParsedMesh:
    path = source
    r = Reader(data, path)

    magic = r.read(8)
    if magic[:4] != MESH_MAGIC:
        raise MeshFormatError(f"{path.name}: 不是 NeoX mesh")
    version = magic[4]
    if version not in (2, 3, 4):
        raise MeshFormatError(f"{path.name}: 暂不支持 mesh v{version}")

    bone_exist = r.u32()
    static_mesh = bone_exist == 0
    expected_end: int | None = None
    if static_mesh:
        # 静态 Mesh 没有骨骼块；该 u32 是渲染数据结束的绝对偏移。
        expected_end = r.u32()
        bone_count = 1
        bone_parents = [-1]
        bone_names = ["__static_root__"]
        bone_matrices = [(
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        )]
    else:
        if bone_exist > 1:
            count = r.u8()
            r.read(2 + count * 4)

        bone_count = r.u16()
        _check_count(bone_count, "骨骼", path)
        if bone_count == 0:
            raise MeshFormatError(f"{path.name}: bone_exist 非零但骨骼数为 0")

        parent_width = 2 if version >= 4 else 1
        bone_parents = []
        for _ in range(bone_count):
            parent = r.u16() if parent_width == 2 else r.u8()
            sentinel = 0xFFFF if parent_width == 2 else 0xFF
            bone_parents.append(-1 if parent == sentinel else parent)

        bone_names = [_decode_bone_name(r.read(32), i) for i in range(bone_count)]

        has_extra = r.u8()
        if has_extra:
            r.read(bone_count * 28)

        bone_matrices = [
            tuple(r.f32() for _ in range(16))
            for _ in range(bone_count)
        ]

        # 保留字段；旧版公开转换器要求为 0。当前资源中亦按此布局。
        r.u8()
        expected_end = r.u32()

    submeshes: list[tuple[int, int, int, int]] = []
    while True:
        marker = r.u16()
        if marker == 1:
            break
        r.seek(-2, 1)
        mesh_vertex_count = r.u32()
        mesh_face_count = r.u32()
        uv_layers = r.u8()
        color_layers = r.u8()
        _check_count(mesh_vertex_count, "子网格顶点", path)
        _check_count(mesh_face_count, "子网格三角面", path)
        if uv_layers > 16 or color_layers > 16:
            raise MeshFormatError(
                f"{path.name}: UV/颜色层异常：{uv_layers}/{color_layers}"
            )
        submeshes.append(
            (mesh_vertex_count, mesh_face_count, uv_layers, color_layers)
        )
        if len(submeshes) > 4096:
            raise MeshFormatError(f"{path.name}: 子网格数量异常")

    vertex_count = r.u32()
    face_count = r.u32()
    _check_count(vertex_count, "顶点", path)
    _check_count(face_count, "三角面", path)

    positions = [r.unpack("<fff") for _ in range(vertex_count)]
    normals = [r.unpack("<fff") for _ in range(vertex_count)]

    has_tangent = r.u16()
    if has_tangent:
        r.read(vertex_count * 12)

    faces = [r.unpack("<HHH") for _ in range(face_count)]

    uvs: list[tuple[float, float]] = []
    for mesh_vertex_count, _, uv_layers, _ in submeshes:
        if uv_layers:
            uvs.extend(r.unpack("<ff") for _ in range(mesh_vertex_count))
            if uv_layers > 1:
                r.read(mesh_vertex_count * 8 * (uv_layers - 1))
        else:
            uvs.extend((0.0, 0.0) for _ in range(mesh_vertex_count))

    for mesh_vertex_count, _, _, color_layers in submeshes:
        if color_layers:
            r.read(mesh_vertex_count * 4 * color_layers)

    if static_mesh:
        # PMX 顶点必须有权重；静态道具统一挂到合成根骨。
        joints = [(0, 0, 0, 0) for _ in range(vertex_count)]
        weights = [(1.0, 0.0, 0.0, 0.0) for _ in range(vertex_count)]
        if expected_end is not None and r.pos != expected_end:
            raise MeshFormatError(
                f"{path.name}: 静态 Mesh 渲染数据终点 {r.pos} != {expected_end}"
            )
    else:
        joint_width = 2 if version >= 4 else 1
        joints = []
        if joint_width == 2:
            joints = [r.unpack("<HHHH") for _ in range(vertex_count)]
        else:
            joints = [r.unpack("<BBBB") for _ in range(vertex_count)]

        weights = [r.unpack("<ffff") for _ in range(vertex_count)]

    if sum(item[0] for item in submeshes) != vertex_count:
        raise MeshFormatError(
            f"{path.name}: 子网格顶点和 {sum(x[0] for x in submeshes)} "
            f"!= 总顶点 {vertex_count}"
        )
    if sum(item[1] for item in submeshes) != face_count:
        raise MeshFormatError(
            f"{path.name}: 子网格面数和 {sum(x[1] for x in submeshes)} "
            f"!= 总面数 {face_count}"
        )
    if len(uvs) != vertex_count:
        raise MeshFormatError(f"{path.name}: UV 数量与顶点数不一致")

    return ParsedMesh(
        version=version,
        submeshes=submeshes,
        bone_parents=bone_parents,
        bone_names=bone_names,
        bone_matrices=bone_matrices,
        positions=positions,
        normals=normals,
        faces=faces,
        uvs=uvs,
        joints=joints,
        weights=weights,
    )


def parse_mesh(path: Path) -> ParsedMesh:
    return parse_mesh_bytes(path.read_bytes(), path)


def _safe_bone_index(value: int, bone_count: int, sentinel: int) -> int:
    if value == sentinel or value < 0 or value >= bone_count:
        return 0
    return value


def _order_bones_parent_first(
    raw_parents: list[int],
) -> tuple[list[int], list[int]]:
    """稳定地把父骨排在子骨之前，并清理无效父索引与异常循环。"""
    bone_count = len(raw_parents)
    parents = [
        parent if 0 <= parent < bone_count and parent != index else -1
        for index, parent in enumerate(raw_parents)
    ]
    states = [0] * bone_count
    order: list[int] = []

    def visit(index: int) -> None:
        if states[index] == 2:
            return
        if states[index] == 1:
            parents[index] = -1
            return
        states[index] = 1
        parent = parents[index]
        if parent >= 0:
            if states[parent] == 1:
                parents[index] = -1
            else:
                visit(parent)
        states[index] = 2
        order.append(index)

    for index in range(bone_count):
        visit(index)
    return order, parents


def archive_index(path: Path) -> int | None:
    match = re.match(r"(\d{6})_", path.name)
    return int(match.group(1)) if match else None


def locate_model_resource_root(selected: Path) -> Path | None:
    """接受工具目录、cloudfilesys3/res、游戏包目录或整个 yys 目录。"""
    selected = selected.resolve()
    if (selected / "model.idx").is_file():
        return selected

    relative_candidates = (
        Path("res"),
        Path("cloudfilesys3") / "res",
        Path("Documents") / "cloudfilesys3" / "res",
        Path("files") / "netease" / "onmyoji" / "Documents" / "cloudfilesys3" / "res",
    )
    for relative in relative_candidates:
        candidate = selected / relative
        if (candidate / "model.idx").is_file():
            return candidate.resolve()

    # 用户通常选择 yys；其下一层才是 com.netease... 包目录。
    package_patterns = (
        "*/files/netease/onmyoji/Documents/cloudfilesys3/res/model.idx",
        "*/*/files/netease/onmyoji/Documents/cloudfilesys3/res/model.idx",
    )
    for package_pattern in package_patterns:
        for index_path in selected.glob(package_pattern):
            return index_path.parent.resolve()
    return None


def extracted_resource_label(path: Path) -> str:
    stem = re.sub(r"^\d{6}_", "", path.stem)
    stem = re.sub(r"_[0-9a-fA-F]{16}$", "", stem)
    return stem.strip("_")


def resolve_source_and_model_folder(selected: Path) -> tuple[Path | None, Path]:
    selected = selected.resolve()
    if selected.name.lower() == "model" and (selected / "manifest.csv").exists():
        source = locate_model_resource_root(selected.parent.parent)
        return source, selected

    source = locate_model_resource_root(selected)
    script_model = Path(__file__).resolve().parent / "unpacked" / "model"
    if source is not None:
        # 解包结果始终放在工具旁边，不向游戏本体目录写文件。
        return source, script_model
    if (selected / "unpacked" / "model" / "manifest.csv").exists():
        return locate_model_resource_root(selected), selected / "unpacked" / "model"
    return None, selected


def locate_nearby_onmyoji_apk(selected: Path) -> Path | None:
    """在用户所选目录及其近邻中自动寻找阴阳师 APK。"""
    selected = selected.resolve()
    if selected.is_file() and selected.suffix.lower() == ".apk":
        return selected

    folders: list[Path] = []
    cursor = selected if selected.is_dir() else selected.parent
    for _ in range(5):
        if cursor in folders:
            break
        folders.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent

    preferred: list[Path] = []
    fallback: list[Path] = []
    for folder in folders:
        try:
            for path in folder.glob("*.apk"):
                fallback.append(path)
                lowered = path.name.lower()
                if "onmyoji" in lowered or "阴阳师" in path.name:
                    preferred.append(path)
        except OSError:
            continue
        if preferred:
            break
    choices = preferred or fallback
    if not choices:
        return None
    return max(choices, key=lambda path: (path.stat().st_mtime_ns, path.stat().st_size))


def _parse_idx_bytes(data: bytes):
    """解析 APK 内存中的 SKPW IDX，返回与解包器相同的 IndexRecord。"""
    import onmyoji_wpk_gui as wpk

    if len(data) < wpk.IDX_HEADER_SIZE or data[:4] != wpk.IDX_MAGIC:
        raise ValueError("APK 内的 model.idx 不是有效 SKPW 索引")
    marker = data[4:8]
    count = int.from_bytes(data[0x0C:0x10], "little")
    expected = wpk.IDX_HEADER_SIZE + count * wpk.IDX_RECORD_SIZE + 4
    if len(data) != expected or data[-4:] != marker:
        raise ValueError("APK 内的 model.idx 尺寸或尾标记不匹配")

    records = []
    for index in range(count):
        start = wpk.IDX_HEADER_SIZE + index * wpk.IDX_RECORD_SIZE
        raw = data[start : start + wpk.IDX_RECORD_SIZE]
        records.append(
            wpk.IndexRecord(
                index=index,
                resource_hash=raw[:16].hex(),
                key_length=int.from_bytes(raw[16:20], "little"),
                offset=int.from_bytes(raw[20:24], "little"),
                package_id=raw[24],
                stored_size=int.from_bytes(raw[25:28], "little"),
            )
        )
    return records


def _primary_manifest_hashes(
    model_folder: Path,
    extension: str | None = None,
) -> set[str]:
    result: set[str] = set()
    manifest = model_folder / "manifest.csv"
    if not manifest.is_file():
        return result
    wanted_extension = extension.lower().lstrip(".") if extension else None
    with manifest.open("r", newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            if wanted_extension and (row.get("extension") or "").lower() != wanted_extension:
                continue
            digest = (row.get("resource_hash") or "").strip().lower()
            if len(digest) == 32 and row.get("status") in {"ok", "exists"}:
                result.add(digest)
    return result


def _cache_apk_model_thd(apk_path: Path, cache_root: Path) -> Path | None:
    """缓存 APK 自带的基础 model.thx/model.thp，供热更新 THP 缺项时补关系。"""
    thd_cache = cache_root / "thd"
    with zipfile.ZipFile(apk_path) as archive:
        names = set(archive.namelist())
        required = {
            "assets/thd/model.thx": "model.thx",
            "assets/thd/model.thp": "model.thp",
        }
        if not all(name in names for name in required):
            return None
        thd_cache.mkdir(parents=True, exist_ok=True)
        for source, filename in required.items():
            data = archive.read(source)
            target = thd_cache / filename
            if not target.is_file() or target.read_bytes() != data:
                target.write_bytes(data)
    return thd_cache


def sync_apk_parent_resources(
    apk_path: Path,
    model_folder: Path,
    thd_dir: Path,
    log: Callable[[str], None] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[int, int]:
    """从 APK 基础 model WPK 补出当前 THX 精确引用的 XML/KTX。

    资源必须同时满足“APK 内容 MD5 == 当前 THX 内容 MD5”才会写入缓存；
    因而可安全补齐 GIM、材质和纹理，又不会把两个版本的 IDX 序号混用。
    """
    import onmyoji_wpk_gui as wpk
    from thd_resource_index import read_model_thp, read_model_thx

    apk_path = apk_path.resolve()
    model_folder = model_folder.resolve()
    thd_dir = thd_dir.resolve()
    cache_root = model_folder.parent / "apk_model_parents"
    cache_manifest = cache_root / "manifest.csv"
    cache_meta = cache_root / "cache.json"
    apk_thd_dir = _cache_apk_model_thd(apk_path, cache_root)

    # 缓存按内容 MD5 永久复用。游戏 THX/THP 更新不删除旧缓存；
    # 新目录只补当前缺少的 MD5，因此 APK 通常只需提供一次。
    cached_rows: dict[str, str] = {}
    if cache_manifest.is_file():
        try:
            with cache_manifest.open(
                "r", newline="", encoding="utf-8-sig"
            ) as stream:
                for row in csv.DictReader(stream):
                    digest = (row.get("resource_hash") or "").strip().lower()
                    relative = (row.get("output_path") or "").strip()
                    path = cache_root / Path(relative.replace("\\", "/"))
                    if (
                        len(digest) == 32
                        and relative
                        and row.get("status") == "ok"
                        and path.is_file()
                    ):
                        cached_rows[digest] = relative
        except OSError:
            cached_rows = {}

    primary_hashes = _primary_manifest_hashes(model_folder)
    primary_mesh_hashes = _primary_manifest_hashes(model_folder, "mesh")
    thx_records = read_model_thx(thd_dir / "model.thx")
    current_hashes = {record.content_md5 for record in thx_records}
    # 内容发生版本变化时，MD5 已不能把 APK 旧 Mesh 与当前 Mesh 对上；
    # name_hash 才是稳定的“逻辑路径身份”。用于跨版本 parent 关系恢复。
    current_mesh_name_hashes = {
        record.name_hash
        for record in thx_records
        if record.content_md5 in primary_mesh_hashes
    }

    # 当前热更新 model.thp 会裁掉一部分基础/旧角色依赖，但这些 Mesh 本身
    # 仍可能留在当前 WPK。若 APK 基础 THP 仍引用当前存在的 Mesh，就把该
    # 父 GIM 及整段依赖的 XML/KTX 一并加入精确补充集合。这样不是靠邻近
    # 条目猜材质，而是继续使用官方基础依赖表。
    apk_dependency_hashes: set[str] = set()
    if apk_thd_dir is not None:
        try:
            apk_thx_records = read_model_thx(apk_thd_dir / "model.thx")
            apk_dependencies = read_model_thp(apk_thd_dir / "model.thp")
            apk_record_by_name_hash = {
                record.name_hash: record for record in apk_thx_records
            }
            for parent_hash, dependency_hashes in apk_dependencies.items():
                dependency_records = [
                    apk_record_by_name_hash.get(name_hash)
                    for name_hash in dependency_hashes
                ]
                if not any(
                    record is not None
                    and (
                        record.content_md5 in primary_mesh_hashes
                        or record.name_hash in current_mesh_name_hashes
                    )
                    for record in dependency_records
                ):
                    continue
                parent_record = apk_record_by_name_hash.get(parent_hash)
                if parent_record is not None:
                    apk_dependency_hashes.add(parent_record.content_md5)
                apk_dependency_hashes.update(
                    record.content_md5
                    for record in dependency_records
                    if record is not None
                )
        except Exception:
            apk_dependency_hashes = set()

    wanted = (
        current_hashes | apk_dependency_hashes
    ) - primary_hashes - set(cached_rows)
    if not wanted:
        if log:
            log(f"APK 补充资源缓存可直接复用：{len(cached_rows):,} 个。")
        return 0, len(cached_rows)

    with zipfile.ZipFile(apk_path) as archive:
        names = set(archive.namelist())
        idx_name = "assets/res/model.idx"
        if idx_name not in names:
            raise RuntimeError("APK 中没有 assets/res/model.idx")
        records = [
            record for record in _parse_idx_bytes(archive.read(idx_name))
            if record.resource_hash in wanted
            and wpk.record_is_active(record)
        ]
        by_package: dict[int, list[object]] = {}
        for record in records:
            by_package.setdefault(record.package_id, []).append(record)

        cache_root.mkdir(parents=True, exist_ok=True)

        zstandard_module = wpk.load_zstandard()
        saved_rows: dict[str, str] = dict(cached_rows)
        saved_types: dict[str, int] = {}
        for relative in saved_rows.values():
            extension = Path(relative).suffix.lower().lstrip(".")
            saved_types[extension] = saved_types.get(extension, 0) + 1
        completed = 0
        for package_id, package_records in sorted(by_package.items()):
            package_name = f"assets/res/model{package_id}.wpk"
            if package_name not in names:
                if log:
                    log(f"APK 缺少 {package_name}，跳过对应入口。")
                completed += len(package_records)
                continue
            package_data = archive.read(package_name)
            for record in sorted(package_records, key=lambda item: item.offset):
                completed += 1
                try:
                    read_size = wpk.record_read_size(record)
                    end = record.offset + read_size
                    if end > len(package_data):
                        continue
                    decoded, _ = wpk.decode_stage1(
                        package_data[record.offset:end],
                        record.key_length,
                    )
                    decoded, _ = wpk.unwrap_payload(decoded, zstandard_module)
                    extension = wpk.detect_extension(decoded)
                    if extension not in {"xml", "ktx"}:
                        continue
                    bucket = cache_root / record.resource_hash[:2]
                    bucket.mkdir(parents=True, exist_ok=True)
                    label = wpk.semantic_label(decoded) if extension == "xml" else ""
                    filename = (
                        f"000000_{label}_{record.resource_hash[:16]}.{extension}"
                        if label
                        else f"{record.resource_hash}.{extension}"
                    )
                    output = bucket / filename
                    output.write_bytes(decoded)
                    saved_rows[record.resource_hash] = str(
                        output.relative_to(cache_root)
                    )
                    saved_types[extension] = saved_types.get(extension, 0) + 1
                except Exception:
                    continue
                finally:
                    if progress and (
                        completed % 100 == 0 or completed == len(records)
                    ):
                        progress(completed, len(records))

    with cache_manifest.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(["resource_hash", "output_path", "status"])
        for digest, output in sorted(saved_rows.items()):
            writer.writerow([digest, output, "ok"])
    cache_meta.write_text(
        json.dumps(
            {
                "cache_format": 3,
                "last_apk": str(apk_path),
                "last_apk_size": apk_path.stat().st_size,
                "last_apk_mtime_ns": apk_path.stat().st_mtime_ns,
                "last_added": len(records),
                "saved": len(saved_rows),
                "types": saved_types,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if log:
        log(
            f"APK 增量补全：本次命中 {len(records):,} 个；"
            f"缓存现有 XML {saved_types.get('xml', 0):,} 个、"
            f"KTX {saved_types.get('ktx', 0):,} 个。"
        )
    return len(records), len(saved_rows)


def sync_loose_model_resources(
    source_root: Path,
    model_folder: Path,
    log: Callable[[str], None] | None = None,
) -> Path | None:
    """解码 res/model 下不在 model.idx 中的热更新散文件。"""
    import onmyoji_wpk_gui as wpk

    source = source_root / "model"
    if not source.is_dir():
        return None
    output_root = model_folder.parent / "loose_model"
    output_root.mkdir(parents=True, exist_ok=True)
    zstandard_module = wpk.load_zstandard()
    rows: dict[str, str] = {}
    manifest = output_root / "manifest.csv"
    if manifest.is_file():
        try:
            with manifest.open("r", newline="", encoding="utf-8-sig") as stream:
                for row in csv.DictReader(stream):
                    digest = (row.get("resource_hash") or "").lower()
                    relative = (row.get("output_path") or "").strip()
                    if (
                        len(digest) == 32
                        and relative
                        and (output_root / relative).is_file()
                    ):
                        rows[digest] = relative
        except OSError:
            rows = {}

    added = 0
    for path in source.iterdir():
        digest = path.name.lower()
        if not path.is_file() or len(digest) != 32 or digest in rows:
            continue
        try:
            blob = path.read_bytes()
            decoded, _ = wpk.decode_stage1(blob, len(blob))
            decoded, _ = wpk.unwrap_payload(decoded, zstandard_module)
            if wpk.detect_extension(decoded) != "mesh" or len(decoded) < 12:
                continue
            bucket = output_root / digest[:2]
            bucket.mkdir(parents=True, exist_ok=True)
            target = bucket / f"{digest}.mesh"
            target.write_bytes(decoded)
            rows[digest] = str(target.relative_to(output_root))
            added += 1
        except Exception:
            continue

    with manifest.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(["resource_hash", "output_path", "status"])
        for digest, relative in sorted(rows.items()):
            writer.writerow([digest, relative, "ok"])
    if log:
        log(
            f"热更新散文件：新增 Mesh {added} 个（含静态道具），"
            f"缓存共 {len(rows)} 个。"
        )
    return output_root


def sync_hot_update_zip_resources(
    thd_dir: Path,
    model_folder: Path,
    log: Callable[[str], None] | None = None,
) -> list[Path]:
    """增量解码客户端直接从 ``temp_cache/res.zip`` 读取的 model 资源。

    ``res.zip`` 的条目名是解密后内容 MD5，条目本身沿用 WPK 的 PC 包装；
    ``model.thx`` 则给出当前 model 包实际引用的 MD5 集合。三者可以形成
    完整校验闭环，不需要猜路径，也不会把其它资源包的同名缓存混进来。
    """
    import onmyoji_wpk_gui as wpk
    from thd_resource_index import read_model_thx

    archive_path = thd_dir.parent / "temp_cache" / "res.zip"
    thx_path = thd_dir / "model.thx"
    if not archive_path.is_file() or not thx_path.is_file():
        return []

    output_root = model_folder.parent / "hot_update_model"
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = output_root / "manifest.csv"
    cached: dict[str, tuple[str, str]] = {}
    if manifest_path.is_file():
        try:
            with manifest_path.open(
                "r", newline="", encoding="utf-8-sig"
            ) as stream:
                for row in csv.DictReader(stream):
                    digest = (row.get("resource_hash") or "").strip().lower()
                    relative = (row.get("output_path") or "").strip()
                    extension = (row.get("extension") or "").strip().lower()
                    path = output_root / Path(relative.replace("\\", "/"))
                    if (
                        len(digest) == 32
                        and relative
                        and path.is_file()
                        and path.stat().st_size > 0
                    ):
                        cached[digest] = (relative, extension or path.suffix[1:])
        except OSError:
            cached = {}

    known_by_md5, _ = _manifest_hash_maps(model_folder)
    output_root_resolved = output_root.resolve()
    # 当前 hot_update_model 清单也会被合并进全局索引；这里只把其它来源
    # 视为“已有”，避免缓存把自己误判成可跳过的外部副本。
    existing_by_md5 = {
        digest: path
        for digest, path in known_by_md5.items()
        if not path.resolve().is_relative_to(output_root_resolved)
    }
    current_records: dict[str, object] = {}
    for record in read_model_thx(thx_path):
        current_records.setdefault(record.content_md5.lower(), record)

    zstandard_module = wpk.load_zstandard()
    active: dict[str, tuple[str, str, int]] = {}
    added = 0
    reused_main = 0
    failed = 0
    with zipfile.ZipFile(archive_path) as archive:
        entries: dict[str, zipfile.ZipInfo] = {}
        for info in archive.infolist():
            parts = info.filename.replace("\\", "/").split("/")
            if len(parts) >= 2 and len(parts[-2]) == 2:
                digest = (parts[-2] + parts[-1]).lower()
                if len(digest) == 32:
                    entries[digest] = info

        for digest in sorted(current_records.keys() & entries.keys()):
            existing_path = existing_by_md5.get(digest)
            if existing_path is not None and existing_path.is_file():
                reused_main += 1
                continue

            record = current_records[digest]
            old = cached.get(digest)
            if old is not None:
                active[digest] = (old[0], old[1], int(record.kind))
                continue

            try:
                blob = archive.read(entries[digest])
                decoded, _ = wpk.decode_stage1(blob, len(blob))
                decoded, _ = wpk.unwrap_payload(decoded, zstandard_module)
                actual_digest = hashlib.md5(decoded).hexdigest()
                if actual_digest != digest:
                    raise ValueError(
                        f"内容 MD5 不一致：{actual_digest} != {digest}"
                    )
                extension = wpk.detect_extension(decoded)
                bucket = output_root / digest[:2]
                bucket.mkdir(parents=True, exist_ok=True)
                target = bucket / f"{digest}.{extension}"
                target.write_bytes(decoded)
                relative = str(target.relative_to(output_root))
                active[digest] = (relative, extension, int(record.kind))
                added += 1
            except Exception as exc:
                failed += 1
                if log and failed <= 10:
                    log(
                        f"热更新资源解码失败 {digest}："
                        f"{type(exc).__name__}: {exc}"
                    )

    with manifest_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            ["resource_hash", "output_path", "status", "extension", "kind"]
        )
        for digest, (relative, extension, kind) in sorted(active.items()):
            writer.writerow([digest, relative, "ok", extension, kind])

    mesh_paths = [
        output_root / Path(relative.replace("\\", "/"))
        for relative, extension, _ in active.values()
        if extension == "mesh"
    ]
    if log:
        log(
            f"热更新 ZIP：当前 model 条目 {len(current_records.keys() & entries.keys()):,}；"
            f"其它内容缓存已有 {reused_main:,}；ZIP 补齐 {len(active):,}"
            f"（本次新增 {added:,}，其中 Mesh {len(mesh_paths):,}）"
            + (f"；失败 {failed:,}" if failed else "。")
        )
    return mesh_paths


SUPPLEMENTAL_RIGGED_GROUPS = ("fx_model", "levelsets", "static", "fx", "res")
# levelsets/fx_model 中绝大多数无骨 Mesh 是场景/特效几何，不当作角色道具批量导出。
# static/fx/res 的无骨 Mesh 数量小，且更可能是可独立使用的道具/附件，因此一并保留。
SUPPLEMENTAL_STATIC_GROUPS = frozenset({"static", "fx", "res"})


def sync_supplemental_rigged_resources(
    source_root: Path,
    model_folder: Path,
    log: Callable[[str], None] | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    archive_groups: list[object] | None = None,
) -> list[Path]:
    """增量提取 model 之外资源包中的带骨 Mesh。

    首次运行会识别所选分组的资源类型；随后按内容 MD5 复用识别结果，
    游戏更新时只解码新内容。这里不猜材质归属，额外网格只作为待恢复组件。
    """
    import onmyoji_wpk_gui as wpk

    output_root = model_folder.parent / "extra_rigged"
    output_root.mkdir(parents=True, exist_ok=True)
    if archive_groups is None:
        archive_groups = wpk.discover_groups(
            source_root,
            stems=SUPPLEMENTAL_RIGGED_GROUPS,
        )
    groups = {
        group.stem: group for group in archive_groups
        if group.stem in SUPPLEMENTAL_RIGGED_GROUPS
    }
    zstandard_module = wpk.load_zstandard()
    current_meshes: list[Path] = []

    pending_total = 0
    cached_by_group: dict[str, dict[str, tuple[str, str]]] = {}
    active_by_group: dict[str, list[object]] = {}
    for stem in SUPPLEMENTAL_RIGGED_GROUPS:
        group = groups.get(stem)
        if group is None:
            continue
        group_root = output_root / stem
        manifest = group_root / "manifest.csv"
        known: dict[str, tuple[str, str]] = {}
        if manifest.is_file():
            try:
                with manifest.open("r", newline="", encoding="utf-8-sig") as stream:
                    for row in csv.DictReader(stream):
                        digest = (row.get("resource_hash") or "").strip().lower()
                        status = (row.get("status") or "").strip()
                        relative = (row.get("output_path") or "").strip()
                        if len(digest) == 32 and status:
                            known[digest] = (status, relative)
            except OSError:
                known = {}
        active = [
            record for record in group.records
            if wpk.record_is_active(record)
            and record.package_id in group.packages
        ]
        cached_by_group[stem] = known
        active_by_group[stem] = active
        for record in active:
            cached = known.get(record.resource_hash)
            if cached is None:
                pending_total += 1
            elif cached[0] == "rigged" or (
                cached[0] == "static_mesh" and stem in SUPPLEMENTAL_STATIC_GROUPS
            ):
                path = group_root / Path(cached[1].replace("\\", "/")) if cached[1] else None
                if path is None or not path.is_file():
                    pending_total += 1

    completed = 0
    added = 0
    for stem in SUPPLEMENTAL_RIGGED_GROUPS:
        group = groups.get(stem)
        if group is None:
            continue
        group_root = output_root / stem
        group_root.mkdir(parents=True, exist_ok=True)
        known = cached_by_group[stem]
        active = active_by_group[stem]
        handles = {
            package_id: path.open("rb")
            for package_id, path in group.packages.items()
        }
        try:
            for record in active:
                cached = known.get(record.resource_hash)
                cached_path = (
                    group_root / Path(cached[1].replace("\\", "/"))
                    if cached and cached[1] else None
                )
                needs_decode = (
                    cached is None
                    or (
                        cached[0] == "rigged"
                        and (cached_path is None or not cached_path.is_file())
                    )
                    or (
                        cached[0] == "static_mesh"
                        and stem in SUPPLEMENTAL_STATIC_GROUPS
                        and (cached_path is None or not cached_path.is_file())
                    )
                )
                if needs_decode:
                    completed += 1
                    try:
                        stream = handles[record.package_id]
                        stream.seek(record.offset)
                        blob = stream.read(wpk.record_read_size(record))
                        decoded, _ = wpk.decode_stage1(blob, record.key_length)
                        decoded, _ = wpk.unwrap_payload(decoded, zstandard_module)
                        extension = wpk.detect_extension(decoded)
                        if extension == "mesh" and len(decoded) >= 12:
                            is_rigged = int.from_bytes(decoded[8:12], "little") != 0
                            keep_static = stem in SUPPLEMENTAL_STATIC_GROUPS
                            if is_rigged or keep_static:
                                bucket = group_root / record.resource_hash[:2]
                                bucket.mkdir(parents=True, exist_ok=True)
                                target = bucket / (
                                    f"{record.index:06d}_{record.resource_hash[:16]}.mesh"
                                )
                                target.write_bytes(decoded)
                                relative = str(target.relative_to(group_root))
                                status = "rigged" if is_rigged else "static_mesh"
                                known[record.resource_hash] = (status, relative)
                                cached = known[record.resource_hash]
                                cached_path = target
                                added += 1
                            else:
                                known[record.resource_hash] = ("static_mesh", "")
                                cached = known[record.resource_hash]
                        else:
                            known[record.resource_hash] = ("other", "")
                            cached = known[record.resource_hash]
                    except Exception:
                        # 不缓存失败项，下次运行会自动重试。
                        cached = None
                    if progress and (
                        completed % 100 == 0 or completed == pending_total
                    ):
                        progress(completed, pending_total, stem)
                if cached and cached[0] in {"rigged", "static_mesh"} and cached[1]:
                    path = (
                        cached_path
                        if cached_path is not None
                        else group_root / Path(cached[1].replace("\\", "/"))
                    )
                    if path.is_file():
                        current_meshes.append(path.resolve())
        finally:
            for stream in handles.values():
                stream.close()

        manifest = group_root / "manifest.csv"
        with manifest.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow(["resource_hash", "status", "output_path"])
            for digest, (status, relative) in sorted(known.items()):
                writer.writerow([digest, status, relative])

        rigged_count = sum(
            1 for status, _ in known.values() if status == "rigged"
        )
        static_count = sum(
            1 for status, relative in known.values()
            if status == "static_mesh" and relative
        )
        if log:
            log(
                f"额外包 {stem}：当前带骨组件 {rigged_count:,} 个，"
                f"静态道具 {static_count:,} 个。"
            )

    # 同一内容可能在多个 IDX 槽位出现；PMX 只生成一份。
    unique = {path.name.rsplit("_", 1)[-1]: path for path in current_meshes}
    result = sorted(unique.values(), key=lambda path: path.name.lower())
    if log:
        log(
            f"额外资源包组件：本次新增 {added:,} 个，"
            f"当前版本可用 {len(result):,} 个（含筛选后的静态道具）。"
        )
    return result


def sync_supplemental_material_resources(
    source_root: Path,
    model_folder: Path,
    thd_dir: Path,
    archive_groups: list[object] | None = None,
    log: Callable[[str], None] | None = None,
) -> int:
    """Extract only XML/KTX resources referenced by supplemental logical paths.

    fx_model/levelsets/static use independent THX tables, so model.thx alone
    cannot resolve their MaterialGroup or texture MD5s.  The cache is grouped
    by supplemental package and keyed by content MD5; future runs only add
    newly referenced records.
    """
    import onmyoji_wpk_gui as wpk
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    if archive_groups is None:
        archive_groups = wpk.discover_groups(
            source_root,
            stems=SUPPLEMENTAL_RIGGED_GROUPS,
        )
    groups = {group.stem: group for group in archive_groups}
    try:
        references = set(load_res_asset_paths(thd_dir, model_folder))
        references.update(load_script3_gim_paths(thd_dir, model_folder))
        references.update(load_fx_asset_paths(thd_dir, model_folder))
    except Exception:
        references = set()
    package_prefixes = {
        "fx_model": ("fx/",),
        "levelsets": ("levelsets/",),
        "static": ("static/",),
        "res": ("res/", "model/", "levelsets/", "static/", "fx/", "natural/", "npcmodel/"),
    }
    total_added = 0
    zstandard_module = wpk.load_zstandard()
    for stem, prefixes in package_prefixes.items():
        group = groups.get(stem)
        thx_path = thd_dir / f"{stem}.thx"
        if group is None or not thx_path.is_file():
            continue
        try:
            records = read_model_thx(thx_path)
            seeds = read_thx_namehash_seeds(thx_path)
        except Exception:
            continue
        record_by_hash = {record.name_hash: record for record in records}
        archive_by_digest = {record.resource_hash.lower(): record for record in group.records}
        cache_root = model_folder.parent / "extra_rigged" / stem
        manifest = cache_root / "material_manifest.csv"
        known: dict[str, str] = {}
        if manifest.is_file():
            try:
                with manifest.open("r", newline="", encoding="utf-8-sig") as stream:
                    for row in csv.DictReader(stream):
                        if row.get("status") == "ok" and row.get("output_path"):
                            known[(row.get("resource_hash") or "").lower()] = row["output_path"]
            except OSError:
                known = {}
        wanted: dict[str, object] = {}
        for reference in references:
            if not reference.startswith(prefixes):
                continue
            for variant in _package_reference_variants(reference, stem):
                for seed in seeds:
                    name_hash = cloudfilesys_name_hash(variant, stem, seed)
                    record = record_by_hash.get(name_hash)
                    if record is not None:
                        wanted.setdefault(record.content_md5.lower(), record)
                        break
        handles = {package_id: path.open("rb") for package_id, path in group.packages.items()}
        try:
            for digest, record in wanted.items():
                if digest in known and (cache_root / known[digest]).is_file():
                    continue
                archive_record = archive_by_digest.get(digest)
                if archive_record is None or archive_record.package_id not in handles:
                    continue
                try:
                    stream = handles[archive_record.package_id]
                    stream.seek(archive_record.offset)
                    blob = stream.read(wpk.record_read_size(archive_record))
                    decoded, _ = wpk.decode_stage1(blob, archive_record.key_length)
                    decoded, _ = wpk.unwrap_payload(decoded, zstandard_module)
                    extension = wpk.detect_extension(decoded)
                    if extension not in {"xml", "ktx"}:
                        continue
                    if hashlib.md5(decoded).hexdigest() != digest:
                        continue
                    cache_root.mkdir(parents=True, exist_ok=True)
                    target = cache_root / digest[:2] / f"{digest}.{extension}"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(decoded)
                    known[digest] = str(target.relative_to(cache_root))
                    total_added += 1
                except Exception:
                    continue
        finally:
            for stream in handles.values():
                stream.close()
        with manifest.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow(["resource_hash", "output_path", "status"])
            for digest, relative in sorted(known.items()):
                writer.writerow([digest, relative, "ok"])
        if log and total_added:
            log(f"额外包 {stem}：新增材质 XML/KTX {total_added:,} 个。")
    return total_added


def parse_material_xml(path: Path) -> list[MaterialDefinition]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError, UnicodeError):
        return []
    group = root.find(".//MaterialGroup")
    if group is None:
        return []
    materials: list[MaterialDefinition] = []
    for node in group.findall("./*/Material"):
        textures: dict[str, str] = {}
        tint_color: tuple[float, float, float, float] | None = None
        for param in node.findall(".//*[@Value]"):
            tag = param.tag.rsplit("}", 1)[-1]
            value = (param.get("Value") or "").strip()
            # NeoX 并不只使用 Tex0/Tex1 命名。特效、水体和部分旧 shader
            # 常用 diffuse、DiffuseTex、MaskTex、turbulence1 等参数。
            # 这里保留所有“值确实是图片路径”的参数；哪些是 PMX 主颜色贴图
            # 由 material_primary_texture() 的白名单单独判断，避免把 Mask/Noise
            # 误当 diffuse。
            normalized_value = value.replace("\\", "/")
            if Path(normalized_value).suffix.lower() in IMAGE_SUFFIXES:
                textures[tag] = value
            elif tag.lower() == "tintcolor":
                try:
                    values = [float(item.strip()) for item in value.split(",")]
                except ValueError:
                    values = []
                if len(values) >= 3:
                    if len(values) < 4:
                        values.append(1.0)
                    tint_color = tuple(
                        max(0.0, min(1.0, item)) for item in values[:4]
                    )
        materials.append(
            MaterialDefinition(
                node.get("Name") or f"Material{len(materials)}",
                textures,
                tint_color if not textures else None,
            )
        )
    return materials if any(item.textures or item.diffuse_color for item in materials) else []


def _build_material_packages_unsafe(
    model_folder: Path,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    xml_files = list(model_folder.rglob("*.xml"))
    anchors: list[tuple[int, str, Path, list[MaterialDefinition]]] = []
    for number, path in enumerate(xml_files, 1):
        materials = parse_material_xml(path)
        index = archive_index(path)
        if materials and index is not None:
            package_name = path.parent.parent.name
            anchors.append((index, package_name, path.resolve(), materials))
        if progress and (number % 200 == 0 or number == len(xml_files)):
            progress(number, len(xml_files))

    ktx_by_package: dict[str, list[tuple[int, Path]]] = {}
    mesh_by_package: dict[str, list[tuple[int, Path]]] = {}
    for pattern, target in (("*.ktx", ktx_by_package), ("*.mesh", mesh_by_package)):
        for path in model_folder.rglob(pattern):
            index = archive_index(path)
            if index is not None:
                target.setdefault(path.parent.parent.name, []).append((index, path.resolve()))
    for values in (*ktx_by_package.values(), *mesh_by_package.values()):
        values.sort(key=lambda item: item[0])

    anchors.sort(key=lambda item: (item[1], item[0]))
    packages: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    grouped: dict[str, list[tuple[int, str, Path, list[MaterialDefinition]]]] = {}
    for item in anchors:
        grouped.setdefault(item[1], []).append(item)

    for package_name, package_anchors in grouped.items():
        for position, (start, _, xml_path, materials) in enumerate(package_anchors):
            next_start = (
                package_anchors[position + 1][0]
                if position + 1 < len(package_anchors)
                else start + 65
            )
            end = min(next_start, start + 65)
            mesh_paths = [
                path for index, path in mesh_by_package.get(package_name, [])
                if start < index < end
            ]
            ktx_paths = [
                path for index, path in ktx_by_package.get(package_name, [])
                if start < index < end
            ]
            ordered_refs: list[str] = []
            for material in materials:
                for slot, original in sorted(
                    material.textures.items(),
                    key=lambda item: int(re.search(r"\d+", item[0]).group())
                    if re.search(r"\d+", item[0]) else 999,
                ):
                    if original not in ordered_refs:
                        ordered_refs.append(original)
            # 相邻资源只能作为候选；材质数量必须和 Mesh 子网格数量一致，
            # 否则即使索引很近也可能属于另一个模型，不能自动贴错。
            validated_meshes: list[Path] = []
            for mesh_path in mesh_paths:
                try:
                    if len(parse_mesh(mesh_path).submeshes) == len(materials):
                        validated_meshes.append(mesh_path)
                except Exception:
                    continue
            meaningful_names = []
            for material in materials:
                normalized = re.sub(r"[^0-9a-z]+", "", material.name.lower())
                if normalized and not normalized.startswith("material") and normalized not in {"default", "01default"}:
                    meaningful_names.append(normalized)
            reference_stems = [
                re.sub(
                    r"[^0-9a-z]+", "",
                    Path(value.replace("\\", "/")).stem.lower(),
                )
                for value in ordered_refs
            ]
            name_match = any(
                len(name) >= 4 and (name in stem or stem in name)
                for name in meaningful_names
                for stem in reference_stems
                if stem
            )
            exact = (
                len(validated_meshes) == 1
                and len(ktx_paths) == len(ordered_refs)
                and bool(ordered_refs)
                and name_match
            )
            texture_map = dict(zip(ordered_refs, ktx_paths)) if exact else {}
            package = MaterialPackage(
                xml_path=xml_path,
                index=start,
                package_name=package_name,
                materials=materials,
                mesh_paths=validated_meshes,
                texture_map=texture_map,
                confidence="高" if exact else "未绑定",
            )
            packages.append(package)
            if exact:
                for mesh_path in validated_meshes:
                    old = by_mesh.get(mesh_path)
                    if old is None or abs(archive_index(mesh_path) - start) < abs(archive_index(mesh_path) - old.index):
                        by_mesh[mesh_path] = package
    return packages, by_mesh


# 无 THD 表时保留一个人工确认的兜底。使用内容 MD5 而非 WPK 条目序号，
# 因为同一资源在不同版本 model.idx 中的序号会变化。
VERIFIED_RESOURCE_BINDINGS = (
    {
        # SP荒 Show 银河球：sp_huang_galaxy.gim / xj_sp_huang_galaxy.gim
        # 都精确指向同一静态 Mesh，单槽 Sphere001 的 MtlIdx=0；同一 THP 段
        # 唯一 MaterialGroup 也是单材质 Galaxy。该官方 shader 完全程序化，
        # 没有任何图片参数，因此 PMX 不应伪造 diffuse 贴图。用官方
        # GalaxyCenterTint 作为无法复现 nfx shader 时的纯色降级显示。
        "mesh_md5": "31f1e0bcd33aff3c0dc8d466bd96c53f",
        "gim_md5": "96410a8028158c3b3a749f9531ac4b5d",
        "material_md5": "fa8130bec6af4cf41d03e4d4c84dee7c",
        "package_name": "sp_huang_galaxy",
        "direct_materials": [
            {
                "name": "Galaxy",
                "diffuse_color": [0.435294, 0.294118, 0.407843, 1.0],
            }
        ],
        "textures": {},
    },
    {
        # 运动会通用鼠标挂件：605 个角色 GIM 都明文引用同一
        # q_youyonghuishubiao.gim，Socket 名统一 q_shubiao，目标骨统一
        # bip01 r hand。该单槽 GIM 的 BoundingHalf 与目标 Mesh 精确一致；
        # 全库唯一引用 q_yundonghui atlas 的单材质组 Tex0 也精确哈希到
        # hxs_diannaozhuo.png，因此可闭环恢复，不依赖 WPK 邻近猜测。
        "mesh_md5": "5ef928307c5d0d79eeb8ccf3e08bdc13",
        "gim_md5": "b3a2e9010fb4d0df09acba610c46da07",
        "material_md5": "242ab0a1510a997544b4cd3a321e4864",
        "package_name": "q_youyonghuishubiao",
        "textures": {
            "model\\q_yundonghui\\hxs_diannaozhuo.png": "b61662fda9d184261b722f5d9b922253",
        },
    },
    {
        # tuiche/zhubao01：原先只有物理 MD5、没有路径。现已由 THX name hash
        # 精确反推出 model/tuiche/zhubao01.mesh 与同 stem GIM；GIM 单槽
        # zhubao01 的 BoundingCenter/Half 与目标 Mesh 六个边界值逐项一致，
        # 同名单材质 Tex0=zhubao01.png 也精确命中现存 KTX。
        "mesh_md5": "90266c7461144b6ac3fa2eb18ef9f963",
        "gim_md5": "16e78a03fdb2545a306b90d3db3892aa",
        "material_md5": "0f38e2a7f56f55efbf740c4ef1583257",
        "package_name": "tuiche_zhubao01",
        "textures": {
            "model\\tuiche\\zhubao01.png": "2c764d4149ddcc588772d92132f6ddab",
        },
    },
    {
        # jiubei：目标静态 GIM 单槽名就是 s3_sp_cimutongzi_wan。
        # 与官方 s3_sp_cimutongzi_wan Mesh 比较时，200/200 空间三角形、
        # 200/200 UV 三角形及位置+UV联合三角面完全一致；目标只是去骨后
        # 合并了重复顶点。官方 Wan GIM 的 MtlIdx=0 对应同一材质组，
        # 因此可精确继承 s3_sp_cimutongzi_02.tga。
        "mesh_md5": "e034de30f6d435b0a28494405c115b43",
        "gim_md5": "285e4006c925c125b2a2de2d735aec94",
        "material_md5": "f22d9fcb2f204f800836e4528bc64aad",
        "package_name": "jiubei",
        "textures": {
            "model\\s3_sp_cimutongzi\\s3_sp_cimutongzi_02.tga": "d01edc6589e7fb7fa019b9b0cc26fba6",
        },
    },
    {
        "mesh_md5": "cad2942614bfab058e65c4751122b427",
        "gim_md5": "4ce073016109fd35f02e98cd4c705ee9",
        "material_md5": "87ba944a755797deb1175112aea3a50f",
        "textures": {
            "model/s1_tubi/s1_tubi.tga": "36e59c7b3ac12e154e847555458c8062",
        },
    },
    {
        # s4_sp_huang/sphuang：目标 GIM 有 13 个子网格但只有 12 个不同
        # MtlIdx；old_j_huang_01 与 huan 共同使用 index 5。原版 SP荒
        # MaterialGroup 对 0..11 每个不同索引均由至少一个同名子网格验证。
        "mesh_md5": "5016c8d393212546c31d7d947c9283cb",
        "gim_md5": "3d9cd0532d62e9c29458eabdb052f9a0",
        "material_md5": "c9d1747dc14837798599162d5468108d",
        "textures": {
            "model/sp_huang/sp_huang.tga": "ce21bb02c35d9d468ce73cb808e728f2",
            "model/sp_huang/sp_huang_1.tga": "e10f16ad8fd0f28e342bfd29d0f0f956",
        },
    },
    {
        # s4_sp_huang/s3sphuang：7 个子网格只使用 0/1/2/6/7/8 六个
        # MtlIdx；正式 s3_sp_huang MaterialGroup 对六个索引均有同名验证。
        # 两套官方 MaterialGroup 的辅助 shader 槽不同，但所有目标索引的
        # Tex0 与目标 GIM 自身残留材质都一致指向 s3_sp_huang.tga，故只恢复主贴图。
        "mesh_md5": "ce1e60ea380a1f7204f4f04dc69b63f2",
        "gim_md5": "103cb2d1d7a1c4a185cbc3fb7aaef643",
        "material_md5": "62f22461cda9aa1795e37daa2530b8f0",
        "primary_only": True,
        "textures": {
            "model/s3_sp_huang/s3_sp_huang.tga": "8bd462b30f0da0dd58bf285e6c9bff0a",
        },
    },
    {
        # s3_sp_guiqie_01_guajian：目标单槽 GIM 与正式单材质同名，
        # 多份候选在 Tex0 上一致；只保留已验证主贴图。
        "mesh_md5": "389be1645d039fcfadbcdbcaedbc94ea",
        "gim_md5": "68c3c2609d8c8f1d80906a7579db2d33",
        "material_md5": "64fc12ed43526c80e448bc6c81398fa8",
        "primary_only": True,
        "textures": {
            "model/s3_sp_guiqie/s3_sp_guiqie_01.tga": "b292803015f0626600d2f29d60b543a0",
        },
    },
    {
        "mesh_md5": "1f35945d02232b44c2b2520220dcdbdb",
        "gim_md5": "efe2167126c641019410e493eef907e1",
        "material_md5": "1a8fa6590103d5d176d95d9f2611d69d",
        "textures": {
            "model\\s13_zhujue02\\s13_zhujue02.tga": "c12c018847e58b745c7121ddc0b31272",
        },
    },
    {
        "mesh_md5": "326b0868614fd46cfa6b837c5b4e7a2f",
        "gim_md5": "a35ebee502a1334f3fa05108c949cf7c",
        "material_md5": "8ed71be05f2f8bf19fd6705325c96e74",
        "textures": {
            "model/q_tianjingxia/q_tianjingxia.tga": "60a5414432e2b4d592cb23b30c73c792",
            "model/q_tianjingxia/q_tianjingxia_biaoqing_01.tga": "8f19c83452dcb9d118f106ec0124a616",
        },
    },
    {
        "mesh_md5": "cf15281ad3e3d5558bb59744fd3bbe4a",
        "gim_md5": "dd6e283a5133389f145637e7ebcf0520",
        "material_md5": "7e46927fda91785675f9d4717da3de72",
        "textures": {
            "model/q_huimingdeng/q_huimingdeng_01.tga": "57ebab26ae3ac05930485a1820e8acae",
        },
    },
    {
        "mesh_md5": "5b30ae387468e0ec220d20c6efaee116",
        "gim_md5": "ebda85f785682b869008581fed40c98f",
        "material_md5": "f1095a673b2b17d9b9fff4cdfa81e44f",
        "textures": {
            "model\\q_lbs_daoju\\q_yugan.tga": "880091521be355b396b990c60ec629ce",
        },
    },
    {
        "mesh_md5": "979352ff93a7225ddafa5fdb70f8abb5",
        "gim_md5": "ebda85f785682b869008581fed40c98f",
        "material_md5": "f1095a673b2b17d9b9fff4cdfa81e44f",
        "textures": {
            "model\\q_lbs_daoju\\q_yugan.tga": "880091521be355b396b990c60ec629ce",
        },
    },
    {
        "mesh_md5": "16899695ada315a251a93f6182078841",
        "gim_md5": "3a4382df7036a54c1e278f16a199f45a",
        "material_md5": "23de0f5e9046371d0a8f03fa10574406",
        "package_name": "sp_longyechaji_show_mirror",
        "textures": {},
    },
    {
        "mesh_md5": "2f8d92db20c04ca6a12a6d6f1edb4041",
        "gim_md5": "455895ffb65709b8d61ad0efdaf5dcc9",
        "material_md5": "da7d7dbe2c505d31027d09ed835981e0",
        "package_name": "j_jingyinlinglian_ling",
        "primary_only": True,
        "textures": {
            "model/j_jingyinlinglian/j_jingyinlinglian_01.tga": "0ed697310250a869efbd48153434dac1",
        },
    },
    {
        # boss_seyueshen_show 的两个子网格分别由石化版前 3 / 后 3 个槽合并而来。
        # 空间三角形集合前组 24256/24258 一致，后组 21864/21864 一致；
        # 石化版官方 GIM 又明确证明前组全部使用 _02、后组全部使用基础图。
        "mesh_md5": "8c874be0aa1cc587c335639e103b1869",
        "gim_md5": "478a1646d6117d1eddfdf087daa77dcb",
        "material_md5": "fb0812bd3a1493ecc32b63564cbc1208",
        "package_name": "boss_seyueshen_show",
        "material_positions": [0, 3],
        "primary_only": True,
        "textures": {
            "model\\boss_seyueshen\\boss_seyuershen_02.tga": "7b0dc3ed6ce5705f1034b9d8bb2c8722",
            "model\\boss_seyueshen\\boss_seyuershen.tga": "f6c806d8ac519e108bd50fc3d5bdb597",
        },
    },
    {
        # S1 须佐庭院是 Show 主体 + Show 独立头发的运行时合并版。
        # 目标 290 根骨骼恰好等于 Show 主体与头发骨骼集合并集；主体槽 0-5/7
        # 与源几何逐槽一致，头发槽连几何、蒙皮、顶点颜色也与官方头发一致。
        "mesh_md5": "8d6f2c63d2f485c0c5e3e0d667c78b21",
        "gim_md5": "5dde5509e328b908d8d11cff60aae06f",
        "material_md5": "7dec1804baecc259efef1a197e15203f",
        "package_name": "s1_xuzuozhinan_tingyuan",
        "material_parts": [
            {
                "gim_md5": "5dde5509e328b908d8d11cff60aae06f",
                "material_md5": "7dec1804baecc259efef1a197e15203f",
                "positions": [0, 1, 2, 3, 4, 5],
            },
            {
                "material_md5": "d8b1c18f34fd83f3b308d1a3956de95a",
                "positions": [0],
            },
            {
                "gim_md5": "5dde5509e328b908d8d11cff60aae06f",
                "material_md5": "7dec1804baecc259efef1a197e15203f",
                "positions": [6, 7],
            },
        ],
        "primary_only": True,
        "textures": {
            "model/s1_xuzuozhinan/s1_xuzuozhinan.tga": "c1ce320277e2f61b745752d02581218b",
            "model/s1_xuzuozhinan/s1_xuzuozhinan_03.tga": "8fda1c266ea14513f79241ca686283aa",
            "model/s1_xuzuozhinan/s1_xuzuozhinan_01.tga": "0ff45112a9d539bbf49537d76ca6ca91",
        },
    },
    {
        # c1 须佐庭院同样来自 c1 Show：槽 1-10 的空间三角形与 Show 主体
        # 逐槽 100% 一致；槽 0 与 Show 独立头发三角形 99.77% 一致。
        # Show 主体槽 0 只是 4 顶点白色占位件，庭院用真实头发替换它。
        "mesh_md5": "3d32be6053e2fcf78f631bdb73e276ef",
        "gim_md5": "803f2a83fe49adb9a3ff00f0ae5c8c5d",
        "material_md5": "9d75928cb21c4ec8439a79062ef67f1f",
        "package_name": "c1_xuzuozhinan_tingyuan",
        "material_parts": [
            {
                "material_md5": "fa5ff2ed84d772a2ef2ed932fed45eb0",
                "positions": [0],
            },
            {
                "gim_md5": "803f2a83fe49adb9a3ff00f0ae5c8c5d",
                "material_md5": "9d75928cb21c4ec8439a79062ef67f1f",
                "positions": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            },
        ],
        "primary_only": True,
        "textures": {
            "model\\c1_xuzuozhinan\\c1_xuzuozhinan2.tga": "155b613c6d0d9f2f617ec83e80156974",
            "model\\c1_xuzuozhinan\\c1_xuzuozhinan1.tga": "111f3b4d202e7fc85e76d71b4b4030e9",
        },
    },
    {
        # 火金神 JQ 主 Mesh 是普通 Show 的 9 个子网格重排复用版。
        # 九槽表面指纹全部唯一且字节级一致；JQ 目录无专属纹理，两个 JQ 特效
        # Mesh 还与普通 Show 直接共用物理内容，因此继承对应 Show 主材质无换皮歧义。
        "mesh_md5": "f31ed57ae4333757df8ab8dc81211ab4",
        "gim_md5": "34b358b90ade3ae4909417f871522456",
        "material_md5": "60bcb35ef901ccd162c8fb6f41a21a3a",
        "package_name": "huojinshen_show_jq",
        "material_positions": [7, 8, 9, 10, 11, 0, 1, 3, 4],
        "primary_only": True,
        "textures": {
            "model/huojinshen/huojinshen_01.tga": "2a85039e510ccffca9539221f3387875",
            "model/huojinshen/huojinshen_02.tga": "3b1b3783d6ec81997bb1a068ed8020b5",
        },
    },
    {
        # 阿修罗透明版与原版 Show 使用完全相同的 318 根骨骼；6 个目标槽可按
        # 空间三角形直接回溯到原版 4 个槽，其中 pifu / shengti 被拆成两块。
        # 透明目录无任何专属纹理，因此按原版 Show 主材质恢复。
        "mesh_md5": "947fcb06a12d87c3c803dbe70ec50004",
        "gim_md5": "e2c374adb50c2a0960bdcaf20bacb925",
        "material_md5": "530a56af70e526c461f9959068b38c62",
        "package_name": "axiuluo_show_touming",
        "material_positions": [3, 1, 2, 0, 1, 3],
        "primary_only": True,
        "textures": {
            "model\\axiuluo_show\\axiuluo_01.tga": "cdac7ec1b6f1c097ea1c83e3d425f6bd",
            "model\\axiuluo_show\\axiuluo.tga": "9e0589068bbd71a08f695256cbbf00e8",
        },
    },
    {
        # 须佐透明主体是普通 Show 的裁剪版：6 个目标槽全部可按三角形 100%
        # 回溯到原版槽 [1,2,4,8,9,10]，普通 Show 骨骼也完整包含于透明版。
        # 透明目录唯一专属贴图属于独立 guci Mesh，主体继续使用原版两张贴图。
        "mesh_md5": "76da29ebe8f41ba523d0e05f13c7e15a",
        "gim_md5": "9788ff6b36bacbb34ff5b8450296ae2e",
        "material_md5": "b2e7638ee76be7aa582bf714e8aa4f81",
        "package_name": "xuzuozhinan_show_touming",
        "material_positions": [1, 2, 4, 8, 9, 10],
        "primary_only": True,
        "textures": {
            "model\\xuzuozhinan\\xuzuozhinan2.tga": "366d39aab534b3f67fca0569e55e7352",
            "model\\xuzuozhinan\\xuzuozhinan1.tga": "ec4b36fcfa9dc33fc1ba4cd52e138fc5",
        },
    },
    {
        # 须佐透明版骨刺是合法无骨静态 Mesh；精确逻辑路径位于
        # xuzuozhinan_show_touming，且同目录唯一专属颜色图就是 xuzuozhinan_guci.tga。
        # 单独输出时用合成静态根骨，同时另通过 VERIFIED_STATIC_ATTACHMENTS 并入透明主体。
        "mesh_md5": "5ec2adb59a981a4abe13fa9a44f77f87",
        "package_name": "xuzuozhinan_show_guci",
        "direct_materials": [
            {"name": "xuzuozhinan_guci", "texture": "model/xuzuozhinan_show_touming/xuzuozhinan_guci.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/xuzuozhinan_show_touming/xuzuozhinan_guci.tga": "fc03790a617b95035befbff3f9eff0bc",
        },
    },
    {
        # 静音铃鹿 ling_show 是普通 Show 的同拓扑形变版：5 槽布局、骨骼顺序、
        # 父级、面索引和 UV 全部一致，仅顶点位置/法线变化，因此材质槽沿用 _01。
        "mesh_md5": "85039648f11e98f098deb0316d12ffea",
        "gim_md5": "a638b63d836f181545a1838453f11f98",
        "material_md5": "c831c7aaf0f90fbaf74017470bcf665a",
        "package_name": "jingyinlinglian_ling_show",
        "primary_only": True,
        "textures": {
            "model/jingyinlinglian/jingyinlinglian_01.tga": "669ee22ca94acbacd62a69c6801ccc17",
        },
    },
    {
        # j_静音铃鹿 lian 本体的缺失 GIM 仍直接保留完整 _02 纹理集；同族
        # lian_show 是同样的 5 槽结构，官方五材质全部使用 _02.tga。
        "mesh_md5": "6593a5885bf6347e6a655fddd502a76d",
        "gim_md5": "77fc1cd0f490c752a4c097141115ebf6",
        "material_md5": "b42b2dd5fd054b5b62186678d2b5db9d",
        "package_name": "j_jingyinlinglian_lian",
        "primary_only": True,
        "textures": {
            "model/j_jingyinlinglian/j_jingyinlinglian_02.tga": "125fa0bfe444a8a21ef1f44e1e359223",
        },
    },
    {
        # S3晴明探索版与 S3庭院版的 SubMesh、位置、法线、面、UV、joints、
        # weights 全部逐项一致，仅骨骼名称字符串不同，因此直接沿用庭院两槽材质。
        "mesh_md5": "d97e845d7a2a52bd5c77e4d97ebd7607",
        "gim_md5": "30bfd191a38a7fb1b14efecb601e2a96",
        "material_md5": "b172d78c4564f10fee1d1e2d74e1113e",
        "package_name": "s3_zhujue01_tansuo",
        "primary_only": True,
        "textures": {
            "model\\s3_zhujue01\\s3_zhujue01.tga": "dd3f1586a8734a0c8ecc3004608e3b07",
        },
    },
    {
        # j_猫川大帽两个子网格与 j_maochuan_show 槽 7/8 表面指纹完全一致；
        # 两槽均属于 j_maochuan_02 材质，但大帽目录提供了不同内容的专属 diffuse。
        "mesh_md5": "0cfc184d0eacdeff44c0ed0f24b9aab6",
        "gim_md5": "ec4fb3dc2495e1dfb745e6a4b3c452fc",
        "material_md5": "72090bcabf851126c36c7746fa1e6ba0",
        "package_name": "j_maochuan_damao",
        "material_positions": [7, 8],
        "primary_only": True,
        "texture_overrides": {
            "model/j_maochuan/j_maochuan_02.tga": "model/j_maochuan_damao/j_maochuan_02.tga",
        },
        "textures": {
            "model/j_maochuan_damao/j_maochuan_02.tga": "66d795f88a1b6bb020076f8593d951dc",
        },
    },
    {
        # SP御馔津透明版与普通 Show 使用完全相同的243根骨骼/父级；8个目标槽
        # 按 [8,7,6,5,4,3,0,1] 对应普通 Show。UV三角形重合率为
        # 96.8%-100%，透明目录无专属纹理，因此沿用普通 Show 主材质。
        "mesh_md5": "d3d59b888147dba726300d189765c3a8",
        "gim_md5": "905936362d2668de4f7cc6627a2695e3",
        "material_md5": "43ed81d0e98e986bc5ebdac24a02c781",
        "package_name": "sp_yuzhuanjin_show_touming",
        "material_positions": [8, 7, 6, 5, 4, 3, 0, 1],
        "primary_only": True,
        "textures": {
            "model\\sp_yuzhuanjin\\sp_yuzhuanjin.tga": "869c8aef8b82006e36814bdf942f4c36",
            "model\\sp_yuzhuanjin\\sp_yuzhuanjin_01.tga": "812d13ab8f7fdb64ed061feddb8a694c",
            "model\\sp_yuzhuanjin_show\\sp_yuzhuanjin_wuguan_g.tga": "3b10ecb0d42b63d4dc433311b8d4fad8",
        },
    },
    {
        # S2鲤鱼精 Show 的 GIM 只含一个 MtlIdx=0，并明确复用基础 S2 的
        # skeleton/animconfig；Show 目录没有专属贴图，基础 S2 也只有唯一 diffuse。
        "mesh_md5": "42cdb985e8cdc29d96966123d8bd9073",
        "gim_md5": "3377cbe3f6491244fb61780b1f160825",
        "material_md5": "9a27c8eb58f863a34ce4274bb8152b66",
        "package_name": "s2_liyujing_show",
        "primary_only": True,
        "textures": {
            "model/s2_liyujing/s2_liyujing.tga": "f58e17a734fb5538b16a681f6e9d7573",
        },
    },
    {
        # jinnaluo_show_2 是混合派生：槽0与 j_jinnaluo_show 槽0的 UV三角形
        # 597/597 完全一致且使用相同两根骨；其余7槽分别对应普通 jinnaluo_show
        # 的 [1,0,2,4,3,5,6]，其中前6槽100% UV一致，末槽约98.9%。
        "mesh_md5": "c2799526e2b9657afa2364c86c047f5e",
        "gim_md5": "598b60b8c9b7eafe2202c9bd65ac4704",
        "material_md5": "6783b0fd03b67eb2290e4eaeb0d58818",
        "package_name": "jinnaluo_show_2",
        "material_parts": [
            {
                "gim_md5": "598b60b8c9b7eafe2202c9bd65ac4704",
                "material_md5": "6783b0fd03b67eb2290e4eaeb0d58818",
                "positions": [0],
            },
            {
                "gim_md5": "78b5f8e45bf685ebbb363e29b758e4e9",
                "material_md5": "a8e7441d62f4f2a83250faccdb6e0f8b",
                "positions": [1, 0, 2, 4, 3, 5, 6],
            },
        ],
        "primary_only": True,
        "textures": {
            "model\\j_jinnaluo\\j_jinnaluo_02.tga": "fc683473c65af283f6870174be006c7e",
            "model/jinnaluo/jinnaluo.tga": "6b5d78ba3e62db90cdcd31332f16d7e0",
            "model/jinnaluo/jinnaluo_01.tga": "868df1098ebc1955070b0c6a23bc3d80",
            "model/jinnaluo/jinnaluo_wuguan_g.tga": "d3d8f930c4f84fbfb5ec8f05817ff3e1",
        },
    },
    {
        # 闻人翊悬 JQ 与普通 Show 的6槽布局、骨骼、位置、法线、UV和面索引
        # 全部逐项一致，仅蒙皮权重重绑；JQ目录没有专属纹理，因此继承Show材质。
        "mesh_md5": "b0d3803bf0c8de089176652610814eed",
        "gim_md5": "7170f5717e97790d1fcb32987546b977",
        "material_md5": "13c5be5db75e03b891e20a7289a696d4",
        "package_name": "wenrenyixuan_show_jq",
        "primary_only": True,
        "textures": {
            "model\\s1_wenrenyixuan\\wenrenyixuan_tou.tga": "d98c59ed64cf96193dedb18583a7d057",
            "model\\wenrenyixuan\\wenrenyixuan.tga": "ab068e73a180aab7d13a6477e4e9b40b",
        },
    },
    {
        # J大天狗 BS 是官方基础 J 低模的战斗派生：槽0/1 UV三角形100%一致，
        # 槽2完整包含基础低模槽2的全部7757个UV三角形；59根骨骼也是其70骨子集。
        # 基础官方 GIM 的 MtlIdx=[0,1,1]，因此 BS 三槽明确使用 [01,02,02]。
        "mesh_md5": "20f5959d97e2c0d4f1419ea58d985875",
        "gim_md5": "302f255c45a0d72c77e6cf86ebab9bbc",
        "material_md5": "da539ce6da95f4c268032047f25234a9",
        "package_name": "j_datiangou_bs",
        "primary_only": True,
        "textures": {
            "model/j_datiangou/j_datiangou_01.tga": "212f12e49c264b47a9f97c591b7a5400",
            "model/j_datiangou/j_datiangou_02.tga": "ea08e70b71b7930e6b2764fcdc730cfc",
        },
    },
    {
        # J天照 luomo 目录只有三张 diffuse 且目标恰为3槽。槽0与 J Show 的
        # j_01_toufa 槽 UV三角形9445/9445完全一致；槽1只命中 j_02 atlas，
        # 槽2对全库其它天照UV均0命中，因此对应 luomo 独有 body atlas。
        "mesh_md5": "097b50187ae5353e4fcd36e28c9883a4",
        "gim_md5": "38312fce39ae4b778447e933367f373c",
        "material_md5": "799b673091d47bbd9f3226fdfadd36f0",
        "package_name": "j_tianzhao_luomo",
        "direct_materials": [
            {"name": "j_tianzhao_luomo_01", "texture": "model/j_tianzhao_luomo/j_tianzhao_01.tga"},
            {"name": "j_tianzhao_luomo_02", "texture": "model/j_tianzhao_luomo/j_tianzhao_02.tga"},
            {"name": "j_tianzhao_luomo_body", "texture": "model/j_tianzhao_luomo/j_tianzhao_body.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/j_tianzhao_luomo/j_tianzhao_01.tga": "5f35aa12afbfb65a339e44d92f5e8835",
            "model/j_tianzhao_luomo/j_tianzhao_02.tga": "50b07e2c226d75daa4776a20bf0c01cb",
            "model/j_tianzhao_luomo/j_tianzhao_body.tga": "f5dbbf511f23623ca1ee7d2bce9456da",
        },
    },
    {
        # NPC源赖光的隐藏 GIM 有2个子网格且 MtlIdx 都为0；唯一 THP 依赖
        # 精确反解为 model/npc_yuanlaiguang/npc_yuanlaiguang.tga，_01/_02均不存在。
        "mesh_md5": "cf17690cedd8b5c0f2b655c544ca311c",
        "gim_md5": "fe7a827fca5bf46045b200629007cda1",
        "material_md5": "fe7a827fca5bf46045b200629007cda1",
        "package_name": "npc_yuanlaiguang",
        "direct_materials": [
            {"name": "npc_yuanlaiguang_01", "texture": "model/npc_yuanlaiguang/npc_yuanlaiguang.tga"},
            {"name": "npc_yuanlaiguang", "texture": "model/npc_yuanlaiguang/npc_yuanlaiguang.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/npc_yuanlaiguang/npc_yuanlaiguang.tga": "3dac56ce582d558ccf9054a26b00fda7",
        },
    },
    {
        # c1阿修罗 Show 的实际 Mesh 已扩为6槽，但官方 c1 MaterialGroup 仍是4材质。
        # UV与原4槽模型确定映射为 [shengti,pifu,huo,toufa,pifu,shengti]，
        # 即材质索引 [3,1,2,0,1,3]；c1 自身贴图链完整。
        "mesh_md5": "cc4d34aed2165558a3670acdd7c5df6c",
        "gim_md5": "efc54118a349c296761f7f2480f4d5ff",
        "material_md5": "bfd8fb0418086c7621da91246089d329",
        "package_name": "c1_axiuluo_show",
        "material_positions": [3, 1, 2, 0, 1, 3],
        "primary_only": True,
        "textures": {
            "model\\c1_axiuluo_show\\c1_axiuluo_01.tga": "8a341893c49ca0da09486463485eb827",
            "model\\c1_axiuluo_show\\c1_axiuluo.tga": "0d31466b448d64cf59193bc9a6cc5a87",
        },
    },
    {
        # S1 SP日和坊高模0：槽0只命中本体atlas，槽1只命中_01 atlas；
        # 槽1面数19032又精确等于Show两个_01槽 170+18862 的合并。
        "mesh_md5": "6255e128a50b0eefa8293e7316f5dd89",
        "gim_md5": "cca05db26e9849653cf55c83dc5807d8",
        "material_md5": "ad2776d72300d317f3912f5df912d4d4",
        "package_name": "s1_sp_rihefang0",
        "direct_materials": [
            {"name": "s1_sp_rihefang0", "texture": "model/s1_sp_rihefang/s1_sp_rihefang.tga"},
            {"name": "s1_sp_rihefang0_01", "texture": "model/s1_sp_rihefang/s1_sp_rihefang_01.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/s1_sp_rihefang/s1_sp_rihefang.tga": "e1bc01ed7bb3de4de89929edbf8f9d13",
            "model/s1_sp_rihefang/s1_sp_rihefang_01.tga": "4670b4f775fbe1d0474e73469f8e54a1",
        },
    },
    {
        # S1 SP日和坊高模1：单槽UV只命中Show本体atlas，对_01为0；
        # 使用与高模0相同330骨完整骨架，因此作为独立状态模型输出。
        "mesh_md5": "50067a872e188090bfa386298e7e9a02",
        "gim_md5": "cca05db26e9849653cf55c83dc5807d8",
        "material_md5": "ad2776d72300d317f3912f5df912d4d4",
        "package_name": "s1_sp_rihefang1",
        "direct_materials": [
            {"name": "s1_sp_rihefang1", "texture": "model/s1_sp_rihefang/s1_sp_rihefang.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/s1_sp_rihefang/s1_sp_rihefang.tga": "e1bc01ed7bb3de4de89929edbf8f9d13",
        },
    },
    {
        # S3山风 Show 高模把原Show多槽按两张atlas合并为2槽：目标槽0只命中
        # _02类UV（82.6%），槽1只命中_01类UV（75.2%），反向均为0。
        "mesh_md5": "ea6fabec3ede8f73a1f193deb7419ba0",
        "gim_md5": "6bfefa85ad05709ed7697be4a19375f9",
        "material_md5": "e73ef005bd698bf9e08599943a2f68ce",
        "package_name": "s3_shanfeng_show_high",
        "direct_materials": [
            {"name": "s3_shanfeng_02", "texture": "model/s3_shanfeng/s3_shanfeng_02.tga"},
            {"name": "s3_shanfeng_01", "texture": "model/s3_shanfeng/s3_shanfeng_01.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/s3_shanfeng/s3_shanfeng_02.tga": "6266a4819af5f9b7ab73f1c59b455d67",
            "model/s3_shanfeng/s3_shanfeng_01.tga": "f2b71f08b853dda830d75720201df21f",
        },
    },
    {
        # S3 SP铃鹿御前 Show 高模的13槽按三套atlas连续分组。普通Show本来就是
        # 本体[0..2] / _02[3..4] / _01[5..7]；高模锚点槽0/3/6分别只命中
        # 这三类，且三组顶点/面总数与Show对应组分别约99%/98%/97%。
        # 同代13槽4UV高模的官方GIM也稳定采用同atlas连续分组，因此恢复为
        # 本体×3 / _02×3 / _01×7。
        "mesh_md5": "c38344e63242f9296209100aaefcfe08",
        "gim_md5": "aa3c5893b2246f12084de3c126dec0bc",
        "material_md5": "6162bb9c95ce4a2de4715d3015437fb4",
        "package_name": "s3_sp_lingluyuqian_show_high",
        "direct_materials": [
            {"name": "s3_sp_lingluyuqian_base_0", "texture": "model/s3_sp_lingluyuqian/s3_sp_lingluyuqian.tga"},
            {"name": "s3_sp_lingluyuqian_base_1", "texture": "model/s3_sp_lingluyuqian/s3_sp_lingluyuqian.tga"},
            {"name": "s3_sp_lingluyuqian_base_2", "texture": "model/s3_sp_lingluyuqian/s3_sp_lingluyuqian.tga"},
            {"name": "s3_sp_lingluyuqian_02_0", "texture": "model/s3_sp_lingluyuqian/s3_sp_lingluyuqian_02.tga"},
            {"name": "s3_sp_lingluyuqian_02_1", "texture": "model/s3_sp_lingluyuqian/s3_sp_lingluyuqian_02.tga"},
            {"name": "s3_sp_lingluyuqian_02_2", "texture": "model/s3_sp_lingluyuqian/s3_sp_lingluyuqian_02.tga"},
            {"name": "s3_sp_lingluyuqian_01_0", "texture": "model/s3_sp_lingluyuqian/s3_sp_lingluyuqian_01.tga"},
            {"name": "s3_sp_lingluyuqian_01_1", "texture": "model/s3_sp_lingluyuqian/s3_sp_lingluyuqian_01.tga"},
            {"name": "s3_sp_lingluyuqian_01_2", "texture": "model/s3_sp_lingluyuqian/s3_sp_lingluyuqian_01.tga"},
            {"name": "s3_sp_lingluyuqian_01_3", "texture": "model/s3_sp_lingluyuqian/s3_sp_lingluyuqian_01.tga"},
            {"name": "s3_sp_lingluyuqian_01_4", "texture": "model/s3_sp_lingluyuqian/s3_sp_lingluyuqian_01.tga"},
            {"name": "s3_sp_lingluyuqian_01_5", "texture": "model/s3_sp_lingluyuqian/s3_sp_lingluyuqian_01.tga"},
            {"name": "s3_sp_lingluyuqian_01_6", "texture": "model/s3_sp_lingluyuqian/s3_sp_lingluyuqian_01.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/s3_sp_lingluyuqian/s3_sp_lingluyuqian.tga": "6fa7d8189d5157494c85446491da041c",
            "model/s3_sp_lingluyuqian/s3_sp_lingluyuqian_01.tga": "12471462cdcf5cea4fadecd40bd6d37e",
            "model/s3_sp_lingluyuqian/s3_sp_lingluyuqian_02.tga": "6efce7c52affaf63b8887e29edca442c",
        },
    },
    {
        # S2北觅狐 mask 的旧 GIM/MaterialGroup 仍完整，但当前 Mesh 从11槽收缩为9槽。
        # UV逐槽映射确定新槽材质类别为 [02,02,02,02,01,03,01,03,02]；
        # 其中槽5/7与旧03槽100%一致，槽6为99.26%，其余类别也完全互斥。
        "mesh_md5": "441a77f2ff48993ae227a81cacb69420",
        "gim_md5": "327dcb84dcbc68c7267e1500e8cde9f3",
        "material_md5": "88eabb4712e5c8e13fda5ccbf7de267a",
        "package_name": "s2_beimihu_show_mask",
        "direct_materials": [
            {"name": "s2_beimihu_02_0", "texture": "model/s2_beimihu/s2_beimihu_02.tga"},
            {"name": "s2_beimihu_02_1", "texture": "model/s2_beimihu/s2_beimihu_02.tga"},
            {"name": "s2_beimihu_02_2", "texture": "model/s2_beimihu/s2_beimihu_02.tga"},
            {"name": "s2_beimihu_02_3", "texture": "model/s2_beimihu/s2_beimihu_02.tga"},
            {"name": "s2_beimihu_01_0", "texture": "model/s2_beimihu/s2_beimihu_01.tga"},
            {"name": "s2_beimihu_03_0", "texture": "model/s2_beimihu/s2_beimihu_03.tga"},
            {"name": "s2_beimihu_01_1", "texture": "model/s2_beimihu/s2_beimihu_01.tga"},
            {"name": "s2_beimihu_03_1", "texture": "model/s2_beimihu/s2_beimihu_03.tga"},
            {"name": "s2_beimihu_02_4", "texture": "model/s2_beimihu/s2_beimihu_02.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/s2_beimihu/s2_beimihu_01.tga": "0b11970c984a0f8672b685c2ac4e0351",
            "model/s2_beimihu/s2_beimihu_02.tga": "ca807d17c63ef65ed07016fd8a0d3846",
            "model/s2_beimihu/s2_beimihu_03.tga": "6c3ab87004cd0e7fb570473d6c7dc113",
        },
    },
    {
        # SP千姬石化高模的原GIM已被裁空，但石化目录只存在一张主颜色图
        # sp_qianji_shihua.tga（另两张仅 normal/mix）。官方同类“专属石化
        # diffuse”样本会让全部材质槽共享该专属主图；目标3槽因此分别保留
        # 子网格边界，但 diffuse 全部指向唯一石化主图。
        "mesh_md5": "14e866fe78f54fea5fca134ffefdff65",
        "gim_md5": "50426e7bcba5eb4c6dba23bee390a0f2",
        "material_md5": "0343f7fff4c2d1c208f69cbdb627e600",
        "package_name": "sp_qianji_show_shihua",
        "direct_materials": [
            {"name": "sp_qianji_shihua_0", "texture": "model/sp_qianji_show_shihua/sp_qianji_shihua.tga"},
            {"name": "sp_qianji_shihua_1", "texture": "model/sp_qianji_show_shihua/sp_qianji_shihua.tga"},
            {"name": "sp_qianji_shihua_2", "texture": "model/sp_qianji_show_shihua/sp_qianji_shihua.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/sp_qianji_show_shihua/sp_qianji_shihua.tga": "bbd2e2c90e74f1ae34388f465cd53cc2",
        },
    },
    {
        # 113950_2d24... 的逻辑路径虽未保留在 res，但官方 sp_bujianyue.gim 的
        # THP 依赖直接包含该完整 Mesh MD5。GIM 正好3槽 MtlIdx=[0,1,2]，
        # 同父节点3材质组顺序明确为 [03,01,02]。
        "mesh_md5": "2d24f01cb837da8237ffdee7349720e9",
        "gim_md5": "b652ce57b0bb4c2bf398ffe021b50184",
        "material_md5": "2d40457d1a8b8bc53488903f82452b8b",
        "package_name": "sp_bujianyue",
        "primary_only": True,
        "textures": {
            "model/sp_bujianyue/sp_bujianyue_03.tga": "2e99690b0514a158943a2a99cf562dae",
            "model/sp_bujianyue/sp_bujianyue_01.tga": "7e78d6b2815633939c6362856c97a211",
            "model/sp_bujianyue/sp_bujianyue_02.tga": "da948e1f95449b43ecc248ce79b0f7fb",
        },
    },
    {
        # 天照剧情高模只需要恢复两张剧情atlas。目标13槽与普通Show/JQ的UV二分类
        # 完全互斥：槽0-5只命中02（约97.6%-100%），槽6-12只命中01
        # （约67.7%-100%），反向类别均为0，因此恢复为02×6 / 01×7。
        "mesh_md5": "fd373378e411d6c6656c23aef226ce80",
        "gim_md5": "68841b0ead862430a687bac91eae24a0",
        "material_md5": "550a95465b2933a3ffe3472b331d58eb",
        "package_name": "tianzhao_juqing",
        "direct_materials": [
            {"name": "tianzhao_juqing_02_0", "texture": "model/tianzhao_juqing/tianzhao_02.tga"},
            {"name": "tianzhao_juqing_02_1", "texture": "model/tianzhao_juqing/tianzhao_02.tga"},
            {"name": "tianzhao_juqing_02_2", "texture": "model/tianzhao_juqing/tianzhao_02.tga"},
            {"name": "tianzhao_juqing_02_3", "texture": "model/tianzhao_juqing/tianzhao_02.tga"},
            {"name": "tianzhao_juqing_02_4", "texture": "model/tianzhao_juqing/tianzhao_02.tga"},
            {"name": "tianzhao_juqing_02_5", "texture": "model/tianzhao_juqing/tianzhao_02.tga"},
            {"name": "tianzhao_juqing_01_0", "texture": "model/tianzhao_juqing/tianzhao_01.tga"},
            {"name": "tianzhao_juqing_01_1", "texture": "model/tianzhao_juqing/tianzhao_01.tga"},
            {"name": "tianzhao_juqing_01_2", "texture": "model/tianzhao_juqing/tianzhao_01.tga"},
            {"name": "tianzhao_juqing_01_3", "texture": "model/tianzhao_juqing/tianzhao_01.tga"},
            {"name": "tianzhao_juqing_01_4", "texture": "model/tianzhao_juqing/tianzhao_01.tga"},
            {"name": "tianzhao_juqing_01_5", "texture": "model/tianzhao_juqing/tianzhao_01.tga"},
            {"name": "tianzhao_juqing_01_6", "texture": "model/tianzhao_juqing/tianzhao_01.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/tianzhao_juqing/tianzhao_01.tga": "ba0f96accd7c19d14ec5b038ca0029da",
            "model/tianzhao_juqing/tianzhao_02.tga": "380edec3c58dedde99efacf79f9458dc",
        },
    },
    {
        # S2 SP紧那罗 Show yi 是单槽派生高模。与官方Show三套atlas比较时，
        # 精确UV三角形有78.41%落在01类，02/03均为0；全量UV中心最近分类
        # 93.64%也指向01，因此恢复为 s2_sp_jinnaluo_01.tga。
        "mesh_md5": "8264ccfaf3a47fe9097fb11dc6555a03",
        "gim_md5": "1d90aecd92bdb8f0a07c24ef21c1bfda",
        "material_md5": "0508240f96effc1de9044e39ba8144d1",
        "package_name": "s2_sp_jinnaluo_show_yi",
        "direct_materials": [
            {"name": "s2_sp_jinnaluo_show_yi", "texture": "model/s2_sp_jinnaluo/s2_sp_jinnaluo_01.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/s2_sp_jinnaluo/s2_sp_jinnaluo_01.tga": "1e76be3e35ffab1608f48233fc1255ab",
        },
    },
    {
        # S2 SP紧那罗基础 yi 的身份由 Skeleton 完整确认。它实际使用152根骨，
        # 其中140根（92.1%）与基础02槽一致，且02槽140根骨100%包含在yi中；
        # 对01/03仅重合30/1根。精确UV三角形也只命中02，01/03均为0。
        "mesh_md5": "9043a9287ea5dbc0008262a0ce4ba46d",
        "gim_md5": "db46fd046586f571d160984a8e15327d",
        "material_md5": "18e3d449dcaebadd3e2f7a3120a12c4b",
        "package_name": "s2_sp_jinnaluo_yi",
        "direct_materials": [
            {"name": "s2_sp_jinnaluo_yi", "texture": "model/s2_sp_jinnaluo/s2_sp_jinnaluo_02.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/s2_sp_jinnaluo/s2_sp_jinnaluo_02.tga": "13d8d9e14ed870b5708f7a5c38291959",
        },
    },
    {
        # NPC少年荒透明版的 GIM/Material XML 已被裁空，但隐藏逻辑路径反查中，
        # npc_shaonianhuang* 目录在常见 diffuse/face/hair/body/mask/wuguan 等
        # 完整后缀字典下只命中一张主纹理 npc_shaonianhuang.tga，没有第二atlas。
        # 目标只有3个子网格，因此三槽共用该唯一角色主图。
        "mesh_md5": "f28240941c708fa4e2445f50c9138e96",
        "package_name": "npc_shaonianhuang_show_touming",
        "direct_materials": [
            {"name": "npc_shaonianhuang_0", "texture": "model/npc_shaonianhuang/npc_shaonianhuang.tga"},
            {"name": "npc_shaonianhuang_1", "texture": "model/npc_shaonianhuang/npc_shaonianhuang.tga"},
            {"name": "npc_shaonianhuang_2", "texture": "model/npc_shaonianhuang/npc_shaonianhuang.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/npc_shaonianhuang/npc_shaonianhuang.tga": "3c662634aed775b9bdbac4ba04bb0741",
        },
    },
    {
        # 盛和洲小太鼓与鼓棒是同一套静态演奏道具。两个 GIM 都只有 MtlIdx=0，
        # 资源序列中紧随其后的唯一 MaterialGroup 也只有1材质，Tex0 明确为
        # yaoguaizhili_xiaotaigu.tga；整个目录不存在独立鼓棒颜色图，因此二者共用该 atlas。
        "mesh_md5": "7774047bee6e4e6110aaa7a494de46b4",
        "gim_md5": "d7c1d04c6cdb714e1b3bde7f2186f662",
        "material_md5": "469e39bcd9e678e28ff4a81b64dbc5b6",
        "package_name": "npc_shenghezhou_suti_taigu",
        "direct_materials": [
            {"name": "yaoguaizhili_xiaotaigu", "texture": "model/npc_shenghezhou_suti/yaoguaizhili_xiaotaigu.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/npc_shenghezhou_suti/yaoguaizhili_xiaotaigu.tga": "2d74c9c1c852466f9d8c9d4ae9e27bbf",
        },
    },
    {
        "mesh_md5": "74b93087296424d25257612e8a52e7ad",
        "gim_md5": "3561280a5ae7c7f9a4d59001858a88b1",
        "material_md5": "469e39bcd9e678e28ff4a81b64dbc5b6",
        "package_name": "npc_shenghezhou_suti_taigubang",
        "direct_materials": [
            {"name": "Object001", "texture": "model/npc_shenghezhou_suti/yaoguaizhili_xiaotaigu.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/npc_shenghezhou_suti/yaoguaizhili_xiaotaigu.tga": "2d74c9c1c852466f9d8c9d4ae9e27bbf",
        },
    },
    {
        # S5 SP雪女耳环 GIM 虽已裁掉 XML，但4条 THP 依赖被完整保留并精确反解为：
        # s5_sp_xuenv.tga + byg + normal + yy，即一整套单材质纹理而非4种颜色。
        # 目标静态 Mesh 有2个子网格且没有第二张 diffuse，因此两槽共用本体主图。
        "mesh_md5": "7de14f0346a67a8252e247e90cc551b3",
        "package_name": "s5_sp_xuenv_erhuan",
        "direct_materials": [
            {"name": "s5_sp_xuenv_erhuan_0", "texture": "model/s5_sp_xuenv/s5_sp_xuenv.tga"},
            {"name": "s5_sp_xuenv_erhuan_1", "texture": "model/s5_sp_xuenv/s5_sp_xuenv.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/s5_sp_xuenv/s5_sp_xuenv.tga": "50fbe1bb048b9d4a559c89b6408be803",
        },
    },
    {
        # 027733_3991... 与官方 npc_xi 使用完全相同的140骨；目标槽1 UV三角形
        # 与 npc_xi槽1 100%一致，目标槽0全部7054个UV三角形也100%包含于
        # npc_xi槽0。官方 npc_xi 两槽均使用 npc_xi.tga，因此恢复同一材质。
        "mesh_md5": "3991c0a0029a81a65fb4b397b9ea628a",
        "gim_md5": "9901c008071dc62bd5bbe1ff816ee78b",
        "material_md5": "7d253327d5ded749466141ee7c668243",
        "package_name": "npc_xi_variant",
        "direct_materials": [
            {"name": "npc_xi01", "texture": "model/npc_xi/npc_xi.tga"},
            {"name": "npc_xi02", "texture": "model/npc_xi/npc_xi.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/npc_xi/npc_xi.tga": "012f5d330b3276f51671487baafdf32c",
        },
    },
    {
        # SP小鹿男鹿灵03旧逻辑引用与 S2 官方鹿灵03的表面/UV/95骨骨架完全一致。
        # 官方03 GIM两槽均 MtlIdx=0，唯一 luling 材质指向 s2_sp_luling.tga。
        "mesh_md5": "aefc15ef93fba090d9846d675679164b",
        "gim_md5": "413e3f369b507f5aaf4af9e58138f6cf",
        "material_md5": "21724af3a838ef0ba6d0769a29181474",
        "package_name": "sp_xiaolunan_luling_03",
        "direct_materials": [
            {"name": "luling", "texture": "model/s2_sp_xiaolunan/s2_sp_luling.tga"},
            {"name": "luling_jiao03", "texture": "model/s2_sp_xiaolunan/s2_sp_luling.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/s2_sp_xiaolunan/s2_sp_luling.tga": "78016d812ff8f5376d96b70d37ee9d57",
        },
    },
    {
        # J书翁官方 GIM 为3槽 [0,1,0]，但当前 MaterialGroup 只剩材质0。
        # 因此只恢复有硬证据的槽0/2；槽1保留空材质，不猜已删除的第二材质。
        "mesh_md5": "9872cbef935fe6b6332593bda9a36a57",
        "gim_md5": "e1a89d8840f9c8be752fb6e323c8f90f",
        "material_md5": "a8ed9c464de4bf00359749bc0c4e3052",
        "package_name": "j_shuweng_partial",
        "direct_materials": [
            {"name": "j_shuweng_0", "texture": "model/j_shuweng/j_shuweng.tga"},
            {"name": "j_shuweng_missing_1"},
            {"name": "j_shuweng_2", "texture": "model/j_shuweng/j_shuweng.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/j_shuweng/j_shuweng.tga": "7ec6407986198cb3fd7f2efcbe31b377",
        },
    },
    {
        # 茨木/猫川剧情大猫是单槽 s1_maochuan_02_d；基础及Show官方材质均
        # 明确把这一类02/猫身体材质绑定到 s1_maochuan_02.tga。
        "mesh_md5": "3605904ddfaa173ed1018c9febf1a828",
        "gim_md5": "e0653e823b0e2e06f6763114cc7ce506",
        "material_md5": "b734f5b84ffdf5b431620d50b6c42ac2",
        "package_name": "s1_maochuan_damao",
        "direct_materials": [
            {"name": "s1_maochuan_02_d", "texture": "model/s1_maochuan/s1_maochuan_02.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/s1_maochuan/s1_maochuan_02.tga": "24f2a2ba08f247eae996aeac1759bff0",
        },
    },
    {
        # 男鲛人同一物理 Mesh 被 c1/c2 两套逻辑皮肤复用。两个目录各只有一张
        # 主颜色 atlas，故3个身体子网格分别使用当前逻辑皮肤自己的唯一主图。
        "mesh_md5": "e62ef54525a6edb5c748de764dcde7be",
        "package_name": "c1_npc_nanrenyu",
        "direct_materials": [
            {"name": "c1_npc_nanrenyu_0", "texture": "model/c1_npc_nanrenyu/c1_npc_nanrenyu.tga"},
            {"name": "c1_npc_nanrenyu_1", "texture": "model/c1_npc_nanrenyu/c1_npc_nanrenyu.tga"},
            {"name": "c1_npc_nanrenyu_2", "texture": "model/c1_npc_nanrenyu/c1_npc_nanrenyu.tga"},
        ],
        "primary_only": True,
        "textures": {"model/c1_npc_nanrenyu/c1_npc_nanrenyu.tga": "254132c43c73c9f35df01dd74d3049f3"},
    },
    {
        "mesh_md5": "e62ef54525a6edb5c748de764dcde7be",
        "package_name": "c2_npc_nanrenyu",
        "direct_materials": [
            {"name": "c2_npc_nanrenyu_0", "texture": "model/c2_npc_nanrenyu/c2_npc_nanrenyu.tga"},
            {"name": "c2_npc_nanrenyu_1", "texture": "model/c2_npc_nanrenyu/c2_npc_nanrenyu.tga"},
            {"name": "c2_npc_nanrenyu_2", "texture": "model/c2_npc_nanrenyu/c2_npc_nanrenyu.tga"},
        ],
        "primary_only": True,
        "textures": {"model/c2_npc_nanrenyu/c2_npc_nanrenyu.tga": "eadb7e5538606ac4803e2845be3ed14f"},
    },
    {
        # 女鲛人同样是一份物理 Mesh 对应 c1/c2 两套皮肤，分别保留两个 PMX变体。
        "mesh_md5": "e815281dd55b402bcc49d8260b2bfeb8",
        "package_name": "c1_npc_nvrenyu",
        "direct_materials": [
            {"name": "c1_npc_nvrenyu_0", "texture": "model/c1_npc_nvrenyu/c1_npc_nvrenyu.tga"},
            {"name": "c1_npc_nvrenyu_1", "texture": "model/c1_npc_nvrenyu/c1_npc_nvrenyu.tga"},
            {"name": "c1_npc_nvrenyu_2", "texture": "model/c1_npc_nvrenyu/c1_npc_nvrenyu.tga"},
        ],
        "primary_only": True,
        "textures": {"model/c1_npc_nvrenyu/c1_npc_nvrenyu.tga": "693d1decf51e6fa1b795c23ea7154551"},
    },
    {
        "mesh_md5": "e815281dd55b402bcc49d8260b2bfeb8",
        "package_name": "c2_npc_nvrenyu",
        "direct_materials": [
            {"name": "c2_npc_nvrenyu_0", "texture": "model/c2_npc_nvrenyu/c2_npc_nvrenyu.tga"},
            {"name": "c2_npc_nvrenyu_1", "texture": "model/c2_npc_nvrenyu/c2_npc_nvrenyu.tga"},
            {"name": "c2_npc_nvrenyu_2", "texture": "model/c2_npc_nvrenyu/c2_npc_nvrenyu.tga"},
        ],
        "primary_only": True,
        "textures": {"model/c2_npc_nvrenyu/c2_npc_nvrenyu.tga": "fbe5861df6105b8331e6831b77d75f4b"},
    },
    {
        # 须佐之男 Show 头发1透明件：普通同名 toufa1.gim 为单槽，官方材质
        # toufa01 唯一指向 xuzuozhinan2.tga；透明目录没有任何专属颜色图。
        # 目标骨架含额外头发骨，暂不强行并入221骨的主 Show PMX。
        "mesh_md5": "3d4cad02eaec19ee22dbb900602b8700",
        "gim_md5": "562ba58a718737a78a722e6569867d0e",
        "material_md5": "0f4eb86c06f61c5084e185b7bb90dd6c",
        "package_name": "xuzuozhinan_show_toufa1_touming",
        "direct_materials": [
            {"name": "toufa01", "texture": "model/xuzuozhinan/xuzuozhinan2.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/xuzuozhinan/xuzuozhinan2.tga": "366d39aab534b3f67fca0569e55e7352",
        },
    },
    {
        # 萤草剧情 jq1 有两个单槽物理版本，骨架完全一致且 UV 三角形重合
        # 98.12% / 99.96%；资源清单又保留专属 yingcao_jq1.tga，因此均使用该图。
        "mesh_md5": "f8b0195e9fd35ab3ad95acc8db83502e",
        "package_name": "yingcao_jq1_high",
        "direct_materials": [
            {"name": "yingcao_jq1", "texture": "model/yingcao_jq/yingcao_jq1.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/yingcao_jq/yingcao_jq1.tga": "905596255c9ac7d1a79aa0a6394c024d",
        },
    },
    {
        "mesh_md5": "7a055e41b94aa045ccd39f0aab65209e",
        "package_name": "yingcao_jq1",
        "direct_materials": [
            {"name": "yingcao_jq1", "texture": "model/yingcao_jq/yingcao_jq1.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/yingcao_jq/yingcao_jq1.tga": "905596255c9ac7d1a79aa0a6394c024d",
        },
    },
    {
        # 萤草 JQ 透明版3槽由基础4槽重组：透明槽0完整合并基础槽0+1，
        # 槽1/2分别与基础槽2/3的 UV 三角形100%一致。基础官方4槽全部
        # MtlIdx=0，因此透明版三个槽也统一使用 yingcao_jq.tga。
        "mesh_md5": "ba53ed64c99d219b14aa7f3fc621f63f",
        "gim_md5": "6b62608eddfdec5095d2644dc6856800",
        "package_name": "yingcao_jq_touming",
        "direct_materials": [
            {"name": "yingcao_jq_touming_0", "texture": "model/yingcao_jq/yingcao_jq.tga"},
            {"name": "yingcao_jq_touming_1", "texture": "model/yingcao_jq/yingcao_jq.tga"},
            {"name": "yingcao_jq_touming_2", "texture": "model/yingcao_jq/yingcao_jq.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/yingcao_jq/yingcao_jq.tga": "7a1fbc33ec93d1bed60c0bf98d30f24c",
        },
    },
    {
        # c1彼岸花 Show 的热更新3槽 Mesh 由精确逻辑路径命中。c1目录只有一张
        # 主颜色图 c1_bianhua.tga；官方 c1 Show 5槽、基础本体及相关组件也都
        # 统一使用这张主图，因此3个重组槽全部复用同一 c1 atlas。
        "mesh_md5": "79476c943c2311006c0bde1cc84dcc79",
        "gim_md5": "46b2d4d8f48ec1fec1723dd38ced974d",
        "package_name": "c1_bianhua_show_high",
        "direct_materials": [
            {"name": "c1_bianhua_show_0", "texture": "model/c1_bianhua/c1_bianhua.tga"},
            {"name": "c1_bianhua_show_1", "texture": "model/c1_bianhua/c1_bianhua.tga"},
            {"name": "c1_bianhua_show_2", "texture": "model/c1_bianhua/c1_bianhua.tga"},
        ],
        "primary_only": True,
        "textures": {
            "model/c1_bianhua/c1_bianhua.tga": "505f9d75e88d306bf3d9d8fdb47726ae",
        },
    },
    {
        # 主角02挂件基础版：官方 GIM 只有1槽 j_xueyuqian_shan，并在同一 THP
        # 中直接挂唯一 KTX 777b1125...。没有 MaterialGroup 也不需要猜路径，
        # 直接保留这份原始 KTX 作为 PMX 主纹理。
        "mesh_md5": "5f197c6502873bd208fbc71804dba118",
        "gim_md5": "76cbc2053a18dc9128b8862832beb4c1",
        "package_name": "zhujue02_guajian",
        "direct_materials": [
            {"name": "j_xueyuqian_shan", "texture": "model/zhujue02_guajian/j_xueyuqian_shan.ktx"},
        ],
        "primary_only": True,
        "textures": {
            "model/zhujue02_guajian/j_xueyuqian_shan.ktx": "777b1125fe02eb606f4786fa43c960b5",
        },
    },
    {
        # Show挂件与基础版的子网格、位置、法线、面索引和UV全部逐项相同，
        # 仅骨架/蒙皮不同；因此材质必然沿用基础挂件直挂的同一原始 KTX。
        "mesh_md5": "7bee992a7a3196dd37286d4c5f55c725",
        "gim_md5": "871702f5fa5e147bcf8a4cc2b54e650a",
        "package_name": "zhujue02_guajian_show",
        "direct_materials": [
            {"name": "j_xueyuqian_shan", "texture": "model/zhujue02_guajian/j_xueyuqian_shan.ktx"},
        ],
        "primary_only": True,
        "textures": {
            "model/zhujue02_guajian/j_xueyuqian_shan.ktx": "777b1125fe02eb606f4786fa43c960b5",
        },
    },
    {
        # S3 SP茨木童子火焰基础版：GIM 8槽 MtlIdx=0..7 与8材质完全对应。
        # 该 FX Material 的主颜色字段是 _color，而不是角色材质常用的 Tex0/diffuse；
        # _mask/_noise 仅是特效辅助纹理，因此 PMX 只恢复每槽官方 _color 主图。
        "mesh_md5": "fca37c3e58afe7166934c10c43de372b",
        "gim_md5": "eab11e53af0879bb2e6a04779524764b",
        "material_md5": "61a87532a257d8f9e95096c66276a1ed",
        "package_name": "s3_sp_cimutongzi_huoyan",
        "direct_materials": [
            {"name": "s3_sp_cimutongzi_texiao04", "texture": "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao_f01.png"},
            {"name": "s3_sp_cimutongzi_texiao_2", "texture": "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao.png"},
            {"name": "s3_sp_cimutongzi_texiao03", "texture": "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao03.png"},
            {"name": "s3_sp_cimutongzi_texiao03_2", "texture": "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao02.png"},
            {"name": "s3_sp_cimutongzi_texiao02", "texture": "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao02.png"},
            {"name": "s3_sp_cimutongzi_texiao02_2", "texture": "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao02.png"},
            {"name": "s3_sp_cimutongzi_texiao01", "texture": "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao01.png"},
            {"name": "s3_sp_cimutongzi_texiao01_2", "texture": "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao01.png"},
        ],
        "primary_only": True,
        "textures": {
            "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao_f01.png": "040bb1ac40ea355c1041f4c1623702f2",
            "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao.png": "e42ed533b217ca620b8a378f80b9a40e",
            "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao03.png": "97dbe6ad9e57ce19a5ea00a9d46688fe",
            "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao02.png": "ce5b49d74bab7f343d00cf801d512bb9",
            "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao01.png": "b5c4b9c6f53eaf76a5d379880e793c66",
        },
    },
    {
        # S3 SP茨木童子 Show 火焰版：同样是8槽官方 GIM + 8个 FX Material，
        # 槽顺序由 Show GIM 明确给出；只把每个材质的 _color 写成 PMX 主纹理。
        "mesh_md5": "da2485a91f1fb3164d45b7fde9ff388d",
        "gim_md5": "6db91fafe05bbac01293ac241bdaa1c4",
        "material_md5": "85b3b9e506c220347d042193e15910ac",
        "package_name": "s3_sp_cimutongzi_show_huoyan",
        "direct_materials": [
            {"name": "s3_sp_cimutongzi_texiao04", "texture": "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao_f01.png"},
            {"name": "s3_sp_cimutongzi_texiao02", "texture": "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao02.png"},
            {"name": "s3_sp_cimutongzi_texiao02_2", "texture": "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao02.png"},
            {"name": "s3_sp_cimutongzi_texiao01", "texture": "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao01.png"},
            {"name": "s3_sp_cimutongzi_texiao01_2", "texture": "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao01.png"},
            {"name": "s3_sp_cimutongzi_texiao_2", "texture": "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao.png"},
            {"name": "s3_sp_cimutongzi_texiao03", "texture": "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao03.png"},
            {"name": "s3_sp_cimutongzi_texiao03_2", "texture": "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao02.png"},
        ],
        "primary_only": True,
        "textures": {
            "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao_f01.png": "040bb1ac40ea355c1041f4c1623702f2",
            "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao.png": "e42ed533b217ca620b8a378f80b9a40e",
            "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao03.png": "97dbe6ad9e57ce19a5ea00a9d46688fe",
            "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao02.png": "ce5b49d74bab7f343d00cf801d512bb9",
            "model/s3_sp_cimutongzi/s3_sp_cimutongzi_texiao01.png": "b5c4b9c6f53eaf76a5d379880e793c66",
        },
    },
)


def parse_gim_mesh_reference(path: Path) -> str | None:
    """读取 GIM 明文声明的 Mesh 逻辑路径。

    部分热更新版本会只更新 Mesh 内容而不更新 THP 依赖中的内容 MD5；
    此时 GIM 的 Mesh="model/.../*.mesh" 仍是客户端实际使用的稳定资源键。
    """
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError, UnicodeError):
        return None
    for node in root.iter():
        for key, value in node.attrib.items():
            if key.lower() != "mesh":
                continue
            reference = (value or "").strip().replace("\\", "/")
            if reference.lower().endswith(".mesh"):
                return reference
    return None


def parse_gim_submeshes(path: Path) -> list[GimSubmesh]:
    """读取 GIM 中的子网格顺序和 MtlIdx。

    PMX 材质必须按 Mesh 子网格顺序写入；MtlIdx 才是它在 MaterialGroup
    里的真实下标。多材质模型常见 10,1,0,2... 这种非顺序映射。
    """
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError, UnicodeError):
        return []
    group = root.find(".//SubMesh")
    if group is None:
        return []
    result: list[GimSubmesh] = []
    for node in list(group):
        raw_index = node.get("MtlIdx")
        if raw_index is None:
            continue
        try:
            material_index = int(raw_index)
        except ValueError:
            return []
        bounds: list[tuple[float, float, float] | None] = []
        for attribute in ("BoundingCenter", "BoundingHalf"):
            try:
                values = tuple(
                    float(value.strip())
                    for value in (node.get(attribute) or "").split(",")
                )
            except ValueError:
                values = ()
            bounds.append(values if len(values) == 3 else None)
        result.append(GimSubmesh(
            name=(node.get("Name") or f"Sub{len(result)}").strip(),
            material_index=material_index,
            bounding_center=bounds[0],
            bounding_half=bounds[1],
        ))
    return result


def _mesh_submesh_bounds(
    mesh: ParsedMesh,
) -> list[tuple[tuple[float, float, float], tuple[float, float, float]]]:
    """Calculate per-submesh bounds in the same form stored by GIM XML."""
    result = []
    vertex_offset = 0
    for vertex_count, _face_count, _uv_layers, _color_layers in mesh.submeshes:
        positions = mesh.positions[vertex_offset : vertex_offset + vertex_count]
        vertex_offset += vertex_count
        if not positions:
            return []
        minimum = tuple(min(position[axis] for position in positions) for axis in range(3))
        maximum = tuple(max(position[axis] for position in positions) for axis in range(3))
        center = tuple((minimum[axis] + maximum[axis]) / 2.0 for axis in range(3))
        half = tuple((maximum[axis] - minimum[axis]) / 2.0 for axis in range(3))
        result.append((center, half))
    return result


def _gim_geometry_matches_mesh(
    gim_submeshes: list[GimSubmesh],
    mesh: ParsedMesh,
) -> bool:
    """Require every declared GIM bound to match the corresponding Mesh slot."""
    if len(gim_submeshes) != len(mesh.submeshes):
        return False
    mesh_bounds = _mesh_submesh_bounds(mesh)
    if len(mesh_bounds) != len(gim_submeshes):
        return False
    for gim, (mesh_center, mesh_half) in zip(gim_submeshes, mesh_bounds):
        if gim.bounding_center is None or gim.bounding_half is None:
            return False
        for declared, actual in zip(
            (*gim.bounding_center, *gim.bounding_half),
            (*mesh_center, *mesh_half),
        ):
            tolerance = max(0.002, abs(declared) * 0.0002)
            if abs(declared - actual) > tolerance:
                return False
    return True


def _normalized_material_name(value: str) -> str:
    value = value.lstrip("@").lower()
    return re.sub(r"[^0-9a-z]+", "", value)


def order_materials_by_gim_partial(
    materials: list[MaterialDefinition],
    gim_submeshes: list[GimSubmesh],
) -> tuple[list[MaterialDefinition], int]:
    """按 GIM MtlIdx 展开材质，越界槽位只生成空占位。

    某些官方 GIM 的 MaterialCount 会少于 MtlIdx 最大值，例如
    s2_guiqie_show(6/7)、tanzhilang(3/5)、s4_rihefang(1/2)。
    已落在 MaterialGroup 范围内的索引仍是官方精确关系；越界项不得猜测，
    只保留与子网格同名的空材质占位。
    """
    ordered: list[MaterialDefinition] = []
    valid_count = 0
    for submesh in gim_submeshes:
        index = submesh.material_index
        if 0 <= index < len(materials):
            ordered.append(materials[index])
            valid_count += 1
        else:
            ordered.append(MaterialDefinition(submesh.name, {}))
    return ordered, valid_count


def order_materials_by_gim(
    materials: list[MaterialDefinition],
    gim_submeshes: list[GimSubmesh],
) -> list[MaterialDefinition]:
    """把 MaterialGroup 按 GIM 的 MtlIdx 展开为真实子网格材质顺序。

    名称只适合显示，不能参与判定：同一子网格常用模型总名，而材质名是
    “头发/皮肤/衣服”等部位名。精确性由 THP 依赖段、索引范围和纹理数保证。
    """
    ordered: list[MaterialDefinition] = []
    for submesh in gim_submeshes:
        if submesh.material_index < 0 or submesh.material_index >= len(materials):
            return []
        ordered.append(materials[submesh.material_index])
    return ordered


def build_old_npk_material_packages(
    model_folder: Path,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[
    list[MaterialPackage],
    dict[Path, MaterialPackage],
    dict[Path, list[MaterialPackage]],
]:
    """Restore old-PC materials using a GIM -> Mesh geometry proof.

    Physical proximity narrows the candidates but never proves identity.  A
    candidate Mesh must reproduce every GIM submesh bounding box, and images
    must live inside that verified physical bundle.  This rejects common
    same-slot-count false positives while allowing more unambiguous bundles.
    """
    import onmyoji_npk as npk

    model_folder = model_folder.resolve()
    manifest_path = model_folder / "npk_manifest.json"
    if not manifest_path.is_file():
        return [], {}, {}
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        resources = [npk.ExtractedResource(**item) for item in payload["resources"]]
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return [], {}, {}

    by_archive: dict[str, list[tuple[object, Path]]] = defaultdict(list)
    resource_by_path: dict[Path, object] = {}
    high_textures_by_hash: dict[str, list[Path]] = defaultdict(list)
    model_textures_by_hash: dict[str, list[Path]] = defaultdict(list)
    for item in resources:
        path = (model_folder / item.relative_path).resolve()
        if path.is_file():
            by_archive[item.archive.lower()].append((item, path))
            resource_by_path[path] = item
            if (
                item.archive.lower() in npk.TEXTURE_ARCHIVES
                and item.image_hash
            ):
                high_textures_by_hash[item.image_hash].append(path)
            elif path.suffix.lower() in {".dds", ".ktx", ".png", ".jpg", ".bmp"} and item.image_hash:
                model_textures_by_hash[item.image_hash].append(path)
    for rows in by_archive.values():
        rows.sort(key=lambda pair: pair[0].physical_order)

    image_suffixes = {".ktx", ".dds", ".png", ".jpg", ".jpeg", ".bmp"}
    packages: list[MaterialPackage] = []
    candidates: dict[Path, list[MaterialPackage]] = defaultdict(list)
    material_rows: list[tuple[str, int, object, Path, list[MaterialDefinition]]] = []
    mesh_by_order: dict[str, dict[int, tuple[object, Path]]] = defaultdict(dict)
    gim_by_order: dict[
        str, dict[int, tuple[object, Path, list[GimSubmesh]]]
    ] = defaultdict(dict)
    image_by_order: dict[str, dict[int, Path]] = defaultdict(dict)
    for archive, rows in by_archive.items():
        if archive in npk.TEXTURE_ARCHIVES:
            continue
        for position, (item, path) in enumerate(rows):
            suffix = path.suffix.lower()
            if suffix == ".mesh":
                mesh_by_order[archive][item.physical_order] = (item, path)
                continue
            if suffix in image_suffixes:
                image_by_order[archive][item.physical_order] = path
                continue
            if suffix != ".xml":
                continue
            materials = parse_material_xml(path)
            if materials:
                material_rows.append((archive, position, item, path, materials))
            submeshes = parse_gim_submeshes(path)
            if submeshes:
                gim_by_order[archive][item.physical_order] = (
                    item, path, submeshes
                )

    parsed_mesh_cache: dict[Path, ParsedMesh | None] = {}
    total = len(material_rows)
    for number, (archive, position, item, material_path, materials) in enumerate(
        material_rows, 1
    ):
        material_families = {
            Path(reference.replace("\\", "/")).parent.name.lower()
            for material in materials
            for reference in [material_primary_texture(material)]
            if reference and reference.replace("\\", "/").lower().startswith("model/")
        }
        proven: list[
            tuple[tuple[int, int, int, int], object, Path, list[GimSubmesh], object, Path]
        ] = []
        for gim_order in range(item.physical_order - 12, item.physical_order + 13):
            gim = gim_by_order[archive].get(gim_order)
            if gim is None:
                continue
            gim_item, gim_path, gim_submeshes = gim
            if not any(0 <= submesh.material_index < len(materials) for submesh in gim_submeshes):
                continue
            gim_reference = parse_gim_mesh_reference(gim_path) or ""
            gim_family = (
                Path(gim_reference.replace("\\", "/")).parent.name.lower()
                if gim_reference
                else str(getattr(gim_item, "semantic_label", "")).lower()
            )
            family_match = bool(
                gim_family
                and any(
                    gim_family == family
                    or gim_family in family
                    or family in gim_family
                    for family in material_families
                )
            )
            for mesh_order in range(item.physical_order - 12, item.physical_order + 13):
                mesh_row = mesh_by_order[archive].get(mesh_order)
                if mesh_row is None:
                    continue
                mesh_item, mesh_path = mesh_row
                if mesh_path not in parsed_mesh_cache:
                    try:
                        parsed_mesh_cache[mesh_path] = parse_mesh(mesh_path)
                    except (OSError, MeshFormatError, ValueError):
                        parsed_mesh_cache[mesh_path] = None
                parsed_mesh = parsed_mesh_cache[mesh_path]
                if parsed_mesh is None or not _gim_geometry_matches_mesh(
                    gim_submeshes, parsed_mesh
                ):
                    continue
                bundle_order = gim_order < mesh_order < item.physical_order
                if not family_match and not bundle_order:
                    continue
                span = max(gim_order, mesh_order, item.physical_order) - min(
                    gim_order, mesh_order, item.physical_order
                )
                score = (
                    0 if family_match else 1,
                    0 if bundle_order else 1,
                    span,
                    abs(item.physical_order - gim_order),
                )
                proven.append((
                    score, gim_item, gim_path, gim_submeshes, mesh_item, mesh_path
                ))
        proven.sort(key=lambda value: value[0])
        if not proven or (len(proven) > 1 and proven[0][0] == proven[1][0]):
            if progress and (number % 100 == 0 or number == total):
                progress(number, total)
            continue
        _score, gim_item, gim_path, gim_submeshes, mesh_item, mesh_path = proven[0]
        ordered_materials, valid = order_materials_by_gim_partial(
            materials, gim_submeshes
        )
        if valid == 0:
            if progress and (number % 100 == 0 or number == total):
                progress(number, total)
            continue

        all_refs: list[str] = []
        primary_refs: list[str] = []
        for material in ordered_materials:
            for reference in material.textures.values():
                normalized_reference = reference.replace("\\", "/").lower()
                # Built-in shader/editor textures are shared engine resources,
                # not members of this model's physical bundle.
                is_model_reference = normalized_reference.startswith("model/")
                if is_model_reference and reference not in all_refs:
                    all_refs.append(reference)
            primary = material_primary_texture(material)
            if (
                primary
                and primary.replace("\\", "/").lower().startswith("model/")
                and primary not in primary_refs
            ):
                primary_refs.append(primary)
        gim_order = gim_item.physical_order
        mesh_order = mesh_item.physical_order
        lower = min(gim_order, mesh_order) + 1
        upper = max(gim_order, mesh_order) - 1
        images = [
            image_by_order[archive][order]
            for order in range(lower, upper + 1)
            if order in image_by_order[archive]
        ]
        # model*.npk carries a compact DDS used by the desktop fallback path;
        # tex_res.npk often carries the same picture as a larger KTX.  An exact,
        # globally unique visual hash safely upgrades the candidate without
        # relying on the opaque NPK filename signature.
        upgraded_images: list[Path] = []
        for image_path in images:
            image_item = resource_by_path.get(image_path)
            image_hash = getattr(image_item, "image_hash", "")
            matches = high_textures_by_hash.get(image_hash, [])
            reciprocal = model_textures_by_hash.get(image_hash, [])
            upgraded_images.append(
                matches[0]
                if len(matches) == 1 and len(reciprocal) == 1
                else image_path
            )
        images = upgraded_images
        texture_map: dict[str, Path] = {}
        confidence = ""
        if all_refs and len(images) == len(all_refs):
            texture_map = dict(zip(all_refs, images))
            confidence = "旧NPK几何组精确"
        elif primary_refs and len(images) == len(primary_refs):
            texture_map = dict(zip(primary_refs, images))
            confidence = "旧NPK几何组精确主贴图"
        if not confidence:
            if progress and (number % 100 == 0 or number == total):
                progress(number, total)
            continue

        package_name = (
            getattr(item, "semantic_label", "")
            or getattr(gim_item, "semantic_label", "")
            or f"{archive}-{item.physical_order:06d}"
        )
        package = MaterialPackage(
            xml_path=material_path,
            index=item.physical_order,
            package_name=package_name,
            materials=ordered_materials,
            mesh_paths=[mesh_path],
            texture_map=texture_map,
            confidence=confidence,
        )
        packages.append(package)
        candidates[mesh_path].append(package)
        if progress and (number % 100 == 0 or number == total):
            progress(number, total)

    by_mesh: dict[Path, MaterialPackage] = {}
    variants: dict[Path, list[MaterialPackage]] = {}
    for mesh_path, values in candidates.items():
        unique: dict[tuple[object, ...], MaterialPackage] = {}
        for package in values:
            signature = _material_variant_signature(package.materials)
            old = unique.get(signature)
            if old is None or len(package.texture_map) > len(old.texture_map):
                unique[signature] = package
        variants[mesh_path.resolve()] = list(unique.values())
        if len(unique) == 1:
            by_mesh[mesh_path.resolve()] = next(iter(unique.values()))
    return packages, by_mesh, variants


def _manifest_hash_maps(
    model_folder: Path,
    include_auxiliary: bool = True,
) -> tuple[dict[str, Path], dict[Path, str]]:
    """读取解包清单，建立完整内容 MD5 与实际文件之间的双向映射。"""
    model_folder = model_folder.resolve()
    by_md5: dict[str, Path] = {}
    by_path: dict[Path, str] = {}
    manifest = model_folder / "manifest.csv"
    if manifest.is_file():
        with manifest.open("r", newline="", encoding="utf-8-sig") as stream:
            for row in csv.DictReader(stream):
                digest = (row.get("resource_hash") or "").strip().lower()
                output = (row.get("output_path") or "").strip()
                if (
                    len(digest) != 32
                    or not output
                    or row.get("status") not in {"ok", "exists"}
                ):
                    continue
                relative = Path(output.replace("\\", "/"))
                path = model_folder.parent / relative
                # manifest 由解包器写出；逐条 resolve/stat 十万文件在机械盘上会很慢。
                by_md5[digest] = path
                by_path[path] = digest

    npk_manifest = model_folder / "npk_manifest.json"
    if npk_manifest.is_file():
        try:
            payload = json.loads(npk_manifest.read_text(encoding="utf-8"))
            for row in payload.get("resources", ()):
                digest = str(row.get("content_md5", "")).strip().lower()
                output = str(row.get("relative_path", "")).strip()
                if len(digest) != 32 or not output:
                    continue
                path = model_folder / Path(output.replace("\\", "/"))
                by_md5.setdefault(digest, path)
                by_path.setdefault(path, digest)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    # A few hot-update extraction runs materialize resources successfully but
    # omit their rows from manifest.csv.  Their filenames still carry the
    # first 16 MD5 characters (``name_<md5-prefix>.<ext>``), so recover only
    # those orphan files by verifying the complete content MD5.  This restores
    # current THX -> local-file identity without consulting the APK cache.
    # Normal ``pkg_*`` outputs are already authoritative in manifest.csv.
    # Restrict the expensive orphan walk to the one directory that is allowed
    # to contain deliberately materialized, manifest-less hot dependencies;
    # walking the whole model tree otherwise stats 100k+ files on every run.
    orphan_roots = (model_folder / "_hotdeps",)
    orphan_extensions = {".mesh", ".xml", ".ktx", ".skeleton"}
    orphan_count = 0
    orphan_latest_mtime_ns = 0
    try:
        for root in orphan_roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                orphan_count += 1
                try:
                    orphan_latest_mtime_ns = max(
                        orphan_latest_mtime_ns, path.stat().st_mtime_ns
                    )
                except OSError:
                    continue
    except OSError:
        orphan_count = 0
        orphan_latest_mtime_ns = 0
    try:
        manifest_stat = manifest.stat()
        manifest_stamp = (manifest_stat.st_size, manifest_stat.st_mtime_ns)
    except OSError:
        manifest_stamp = (0, 0)
    orphan_stamp: tuple[object, ...] = (
        1,
        *manifest_stamp,
        orphan_count,
        orphan_latest_mtime_ns,
    )
    cached_orphans = _ORPHAN_MANIFEST_CACHE.get(str(model_folder))
    persistent_cache = model_folder / "orphan_content_index.json"
    if cached_orphans is None and persistent_cache.is_file():
        try:
            payload = json.loads(persistent_cache.read_text(encoding="utf-8"))
            cached_stamp = tuple(payload.get("stamp", ()))
            if cached_stamp == orphan_stamp:
                cached_by_md5: dict[str, Path] = {}
                cached_by_path: dict[Path, str] = {}
                for digest, relative in payload.get("entries", ()):
                    if len(digest) != 32 or not relative:
                        continue
                    path = (model_folder / relative).resolve()
                    cached_by_md5[digest] = path
                    cached_by_path[path] = digest
                cached_orphans = (
                    orphan_stamp, cached_by_md5, cached_by_path
                )
                _ORPHAN_MANIFEST_CACHE[str(model_folder)] = cached_orphans
        except (OSError, ValueError, TypeError):
            cached_orphans = None
    if cached_orphans is not None and cached_orphans[0] == orphan_stamp:
        for digest, path in cached_orphans[1].items():
            by_md5.setdefault(digest, path)
            by_path.setdefault(path, digest)
    else:
        orphan_by_md5: dict[str, Path] = {}
        orphan_by_path: dict[Path, str] = {}
        suffix_pattern = re.compile(r"(?:_([0-9a-fA-F]{16})|([0-9a-fA-F]{32}))(?:\.[^.]+)$")
        try:
            candidates = (
                path for root in orphan_roots if root.is_dir()
                for path in root.rglob("*")
                if path.is_file() and path.suffix.lower() in orphan_extensions
                and path not in by_path
                and suffix_pattern.search(path.name)
            )
            for path in candidates:
                try:
                    digest = hashlib.md5(path.read_bytes()).hexdigest()
                except OSError:
                    continue
                match = suffix_pattern.search(path.name)
                prefix = match.group(1) or match.group(2)
                if not (digest[:16].lower() == prefix.lower() or digest.lower() == prefix.lower()):
                    continue
                orphan_by_md5.setdefault(digest, path.resolve())
                orphan_by_path[path.resolve()] = digest
        except OSError:
            pass
        _ORPHAN_MANIFEST_CACHE[str(model_folder)] = (
            orphan_stamp, orphan_by_md5, orphan_by_path
        )
        try:
            entries = [
                [digest, path.relative_to(model_folder).as_posix()]
                for digest, path in sorted(orphan_by_md5.items())
            ]
            payload = {
                "schema": 1,
                "stamp": list(orphan_stamp),
                "entries": entries,
            }
            temporary = persistent_cache.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(persistent_cache)
        except (OSError, ValueError):
            pass
        for digest, path in orphan_by_md5.items():
            by_md5.setdefault(digest, path)
            by_path.setdefault(path, digest)

    if not include_auxiliary:
        return by_md5, by_path

    # Merge every fully extracted current-client resource group.  ``model``
    # remains authoritative; other groups only fill missing content hashes.
    # This is essential for hot-update GIM/Material/KTX resources that are
    # referenced by model.thx but physically stored in res/fx/levelsets WPKs.
    unpacked_root = model_folder.parent
    excluded_groups = {
        "model", "apk_model_parents", "extra_rigged", "hot_update_model",
        "loose_model", "historical_model_indexes", "cross_package_textures",
        "decoded_png_cache",
    }
    for group_manifest in unpacked_root.glob("*/manifest.csv"):
        group_root = group_manifest.parent
        if group_root.name in excluded_groups:
            continue
        try:
            with group_manifest.open("r", newline="", encoding="utf-8-sig") as stream:
                for row in csv.DictReader(stream):
                    digest = (row.get("resource_hash") or "").strip().lower()
                    output = (row.get("output_path") or "").strip()
                    if (
                        len(digest) != 32
                        or not output
                        or row.get("status") not in {"ok", "exists"}
                    ):
                        continue
                    path = unpacked_root / Path(output.replace("\\", "/"))
                    # The extractor writes a manifest row only after the output
                    # has been materialized.  Trust that contract here just as
                    # we already do for the primary model manifest.  Calling
                    # ``is_file`` for every one of the 400k+ fully extracted
                    # resources turns each material analysis into several
                    # minutes of random filesystem metadata reads.  A stale
                    # row is harmless: the later parser/open simply rejects it.
                    if digest not in by_md5:
                        # ``unpacked_root`` is already absolute, so resolving
                        # every path would only trigger another filesystem
                        # metadata lookup for hundreds of thousands of rows.
                        by_md5[digest] = path
                        by_path[path] = digest
        except (OSError, UnicodeError, csv.Error):
            continue

    # Supplemental WPK groups use the same content-MD5 contract as model.
    # Their material XML/KTX cache is populated incrementally by
    # sync_supplemental_material_resources(); merge it here so all resolvers
    # see one content-addressed namespace.
    supplemental_root = model_folder.parent / "extra_rigged"
    for group_manifest in supplemental_root.glob("*/manifest.csv"):
        group_root = group_manifest.parent
        try:
            with group_manifest.open("r", newline="", encoding="utf-8-sig") as stream:
                for row in csv.DictReader(stream):
                    digest = (row.get("resource_hash") or "").strip().lower()
                    output = (row.get("output_path") or "").strip()
                    if len(digest) != 32 or not output or row.get("status") not in {"rigged", "static_mesh"}:
                        continue
                    path = group_root / Path(output.replace("\\", "/"))
                    by_md5.setdefault(digest, path)
                    by_path[path] = digest
        except OSError:
            continue
    for supplemental_manifest in supplemental_root.glob("*/material_manifest.csv"):
        group_root = supplemental_manifest.parent
        try:
            with supplemental_manifest.open("r", newline="", encoding="utf-8-sig") as stream:
                for row in csv.DictReader(stream):
                    digest = (row.get("resource_hash") or "").strip().lower()
                    output = (row.get("output_path") or "").strip()
                    if len(digest) != 32 or not output or row.get("status") != "ok":
                        continue
                    path = group_root / Path(output.replace("\\", "/"))
                    by_md5.setdefault(digest, path)
                    by_path[path] = digest
        except OSError:
            continue

    # 当前客户端可以绕过 model*.wpk，直接从 temp_cache/res.zip 读取热更新
    # 资源。该缓存仍使用官方内容 MD5，因此与 WPK 资源共用同一索引空间。
    hot_root = model_folder.parent / "hot_update_model"
    hot_manifest = hot_root / "manifest.csv"
    if hot_manifest.is_file():
        try:
            with hot_manifest.open(
                "r", newline="", encoding="utf-8-sig"
            ) as stream:
                for row in csv.DictReader(stream):
                    digest = (row.get("resource_hash") or "").strip().lower()
                    output = (row.get("output_path") or "").strip()
                    if (
                        len(digest) != 32
                        or not output
                        or row.get("status") != "ok"
                    ):
                        continue
                    path = hot_root / Path(output.replace("\\", "/"))
                    by_md5.setdefault(digest, path)
                    by_path[path] = digest
        except OSError:
            pass

    # APK 补充资源独立缓存；只按内容 MD5 合并，不与本体 IDX 序号混用。
    cache_root = model_folder.parent / "apk_model_parents"
    cache_manifest = cache_root / "manifest.csv"
    if cache_manifest.is_file():
        with cache_manifest.open("r", newline="", encoding="utf-8-sig") as stream:
            for row in csv.DictReader(stream):
                digest = (row.get("resource_hash") or "").strip().lower()
                output = (row.get("output_path") or "").strip()
                if len(digest) != 32 or not output or row.get("status") != "ok":
                    continue
                path = cache_root / Path(output.replace("\\", "/"))
                by_md5.setdefault(digest, path)
                by_path[path] = digest
    loose_root = model_folder.parent / "loose_model"
    loose_manifest = loose_root / "manifest.csv"
    if loose_manifest.is_file():
        with loose_manifest.open(
            "r", newline="", encoding="utf-8-sig"
        ) as stream:
            for row in csv.DictReader(stream):
                digest = (row.get("resource_hash") or "").strip().lower()
                output = (row.get("output_path") or "").strip()
                if len(digest) != 32 or not output or row.get("status") != "ok":
                    continue
                path = loose_root / Path(output.replace("\\", "/"))
                by_md5.setdefault(digest, path)
                by_path[path] = digest
    return by_md5, by_path


def _manifest_matches_idx(
    model_folder: Path,
    records: list[object],
) -> bool:
    """比较当前 IDX 的“条目序号 + 内容 MD5”，记录数不变也能发现更新。"""
    manifest = model_folder / "manifest.csv"
    if not manifest.is_file():
        return False
    expected = {
        int(record.index): str(record.resource_hash).lower()
        for record in records
        if int(record.package_id) != 0xFF
        and max(int(record.key_length), int(record.stored_size)) > 0
    }
    seen: dict[int, str] = {}
    try:
        with manifest.open("r", newline="", encoding="utf-8-sig") as stream:
            for row in csv.DictReader(stream):
                try:
                    index = int(row.get("index") or -1)
                except ValueError:
                    continue
                digest = (row.get("resource_hash") or "").strip().lower()
                if index in expected and len(digest) == 32:
                    seen[index] = digest
    except OSError:
        return False
    return len(seen) == len(expected) and all(
        seen.get(index) == digest for index, digest in expected.items()
    )


def _manifest_record_span(model_folder: Path) -> int:
    """返回 manifest 覆盖的 IDX 记录跨度，用于发现用户换了新版游戏目录。"""
    manifest = model_folder / "manifest.csv"
    if not manifest.is_file():
        return 0
    maximum = -1
    with manifest.open("r", newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            try:
                maximum = max(maximum, int(row.get("index") or -1))
            except ValueError:
                continue
    return maximum + 1


def _ordered_texture_references(
    materials: list[MaterialDefinition],
) -> list[str]:
    result: list[str] = []
    for material in materials:
        for _, original in sorted(
            material.textures.items(),
            key=lambda item: int(re.search(r"\d+", item[0]).group())
            if re.search(r"\d+", item[0]) else 999,
        ):
            if original not in result:
                result.append(original)
    return result


def _material_variant_signature(
    materials: list[MaterialDefinition],
) -> tuple[object, ...]:
    """按 PMX 实际可见信息生成材质变体签名。

    当前 PMX 只使用主颜色贴图和纯色 diffuse；normal/mask/ramp 等 NeoX
    辅助 shader 槽不会写入 PMX，因此不应仅因辅助槽不同重复导出同一视觉版本。
    """
    return tuple(
        (
            material.name,
            (
                primary.strip().replace("\\", "/").lower()
                if (primary := material_primary_texture(material))
                else None
            ),
            material.diffuse_color,
        )
        for material in materials
    )


def _learn_thd_texture_hashes(
    by_md5: dict[str, Path],
    record_by_name_hash: dict[int, object],
    dependencies: dict[int, list[int]],
    gim_submeshes_by_path: dict[Path, list[GimSubmesh]],
) -> dict[str, int]:
    """从完整 THP 段学习纹理路径哈希，再用有序锚点补全残缺段。"""
    segments: list[tuple[list[str], list[int]]] = []
    for parent_hash, dependency_hashes in dependencies.items():
        parent = record_by_name_hash.get(parent_hash)
        if parent is None:
            continue
        gim_path = by_md5.get(parent.content_md5)
        gim_submeshes = gim_submeshes_by_path.get(gim_path) if gim_path else None
        if not gim_submeshes:
            continue

        items: list[tuple[int, Path]] = []
        for name_hash in dependency_hashes:
            record = record_by_name_hash.get(name_hash)
            if record is None:
                continue
            path = by_md5.get(record.content_md5)
            if path is not None:
                items.append((name_hash, path))

        mesh_positions = [
            index for index, (_, path) in enumerate(items)
            if path.suffix.lower() == ".mesh"
        ]
        for mesh_number, start in enumerate(mesh_positions):
            end = (
                mesh_positions[mesh_number + 1]
                if mesh_number + 1 < len(mesh_positions)
                else len(items)
            )
            segment = items[start + 1 : end]
            texture_hashes = [
                name_hash for name_hash, path in segment
                if path.suffix.lower() == ".ktx"
            ]
            choices: list[tuple[Path, list[str]]] = []
            seen_materials: set[Path] = set()
            for _, path in segment:
                if path.suffix.lower() != ".xml" or path in seen_materials:
                    continue
                seen_materials.add(path)
                materials = parse_material_xml(path)
                if not materials:
                    continue
                ordered = order_materials_by_gim(materials, gim_submeshes)
                references = _ordered_texture_references(materials)
                if ordered and references:
                    choices.append((path, references))
            if len(choices) == 1:
                segments.append((choices[0][1], texture_hashes))

    mapping: dict[str, int] = {}
    conflicts: set[str] = set()

    def add(reference: str, name_hash: int) -> bool:
        key = reference.lower()
        if key in conflicts:
            return False
        old = mapping.get(key)
        if old is None:
            mapping[key] = name_hash
            return True
        if old != name_hash:
            mapping.pop(key, None)
            conflicts.add(key)
        return False

    # 只有引用数与 THP 纹理数完全相等时，才作为第一轮可靠样本。
    for references, texture_hashes in segments:
        if len(references) != len(texture_hashes):
            continue
        for reference, name_hash in zip(references, texture_hashes):
            add(reference, name_hash)

    # 已知共享纹理充当锚点。两个相邻锚点之间未知项数量相等时，
    # 才能唯一推出新映射；有歧义的段不会参与。
    for _ in range(20):
        added = 0
        for references, texture_hashes in segments:
            refs = [reference.lower() for reference in references]
            anchors: list[tuple[int, int]] = []
            used_hash_positions: set[int] = set()
            last_hash_position = -1
            valid = True
            for ref_position, reference in enumerate(refs):
                known_hash = mapping.get(reference)
                if known_hash is None:
                    continue
                positions = [
                    index for index, value in enumerate(texture_hashes)
                    if value == known_hash and index not in used_hash_positions
                    and index > last_hash_position
                ]
                if not positions:
                    # 本段可以缺少已知的共享依赖；稍后从全局 THX 取回。
                    continue
                if len(positions) != 1:
                    valid = False
                    break
                hash_position = positions[0]
                anchors.append((ref_position, hash_position))
                used_hash_positions.add(hash_position)
                last_hash_position = hash_position
            if not valid:
                continue

            boundaries = [
                (-1, -1),
                *anchors,
                (len(refs), len(texture_hashes)),
            ]
            for (ref_left, hash_left), (ref_right, hash_right) in zip(
                boundaries, boundaries[1:]
            ):
                unknown_refs = [
                    refs[index]
                    for index in range(ref_left + 1, ref_right)
                    if refs[index] not in mapping and refs[index] not in conflicts
                ]
                unused_hashes = [
                    texture_hashes[index]
                    for index in range(hash_left + 1, hash_right)
                    if index not in used_hash_positions
                ]
                if unknown_refs and len(unknown_refs) == len(unused_hashes):
                    for reference, name_hash in zip(unknown_refs, unused_hashes):
                        if add(reference, name_hash):
                            added += 1
        if not added:
            break
    return mapping


def _package_reference_variants(reference: str, package_name: str) -> tuple[str, ...]:
    """Return the canonical and package-local forms used by THX name hashes."""
    normalized = reference.strip().replace("\\", "/").lower().lstrip("/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    variants = [normalized]
    # 一部分旧 MaterialGroup 直接写导出后的 ``foo_ktx.ktx``，而当前
    # THX/WPK 只保留源逻辑名 ``foo.tga``。两者是客户端固定的同源命名
    # 变换；只对这个明确后缀生成回退，不泛化猜测普通扩展名。
    if normalized.endswith("_ktx.ktx"):
        variants.append(normalized[:-8] + ".tga")
    special_prefixes = {
        "fx_texture": "fx/texture/",
        "fx_model": "fx/model/",
    }
    special = special_prefixes.get(package_name.lower())
    if special and normalized.startswith(special):
        variants.append(normalized[len(special):])
    package_prefix = package_name.replace("_", "/").lower() + "/"
    if normalized.startswith(package_prefix):
        variants.append(normalized[len(package_prefix):])
    return tuple(dict.fromkeys(item for item in variants if item))


class CrossPackageTextureResolver:
    """跨 THX/IDX/WPK 精确恢复材质引用的 KTX，只按内容 MD5 缓存。"""

    def __init__(
        self,
        thd_dir: Path,
        model_folder: Path,
        known_by_md5: dict[str, Path],
    ):
        import onmyoji_wpk_gui as wpk
        from thd_resource_index import (
            cloudfilesys_name_hash,
            read_model_thx,
            read_thx_namehash_seeds,
        )

        self.wpk = wpk
        self.cloudfilesys_name_hash = cloudfilesys_name_hash
        self.known_by_md5 = known_by_md5
        self.cache_root = model_folder.parent / "cross_package_textures"
        self.result_cache: dict[str, Path | None] = {}
        self.base_url = ""
        cloud_config = thd_dir.parent / "cloud.json"
        if cloud_config.is_file():
            try:
                value = json.loads(cloud_config.read_text(encoding="utf-8"))
                self.base_url = str(value.get("base_url") or "").strip()
            except (OSError, ValueError, json.JSONDecodeError):
                self.base_url = ""
        self.thx_sources: list[
            tuple[str, dict[int, object], tuple[int, ...]]
        ] = []

        thd_roots = [thd_dir]
        extension_root = thd_dir.parent / "thdext1"
        if extension_root.is_dir():
            thd_roots.append(extension_root)
        for root in thd_roots:
            for thx_path in sorted(root.glob("*.thx")):
                try:
                    records = {
                        record.name_hash: record
                        for record in read_model_thx(thx_path)
                    }
                    seeds = read_thx_namehash_seeds(thx_path)
                except Exception:
                    continue
                self.thx_sources.append((thx_path.stem.lower(), records, seeds))

        source_root = thd_dir.parent / "res"
        self.content_records: dict[
            str, list[tuple[Path, object]]
        ] = {}
        if source_root.is_dir():
            for idx_path in sorted(source_root.glob("*.idx")):
                try:
                    _, records = wpk.parse_idx(idx_path)
                except Exception:
                    continue
                stem = idx_path.stem
                packages: dict[int, Path] = {}
                pattern = re.compile(
                    rf"^{re.escape(stem)}(\d+)\.wpk$", re.IGNORECASE
                )
                for candidate in source_root.glob(f"{stem}*.wpk"):
                    match = pattern.fullmatch(candidate.name)
                    if match:
                        packages[int(match.group(1))] = candidate
                for record in records:
                    if (
                        not wpk.record_is_active(record)
                        or record.package_id not in packages
                    ):
                        continue
                    self.content_records.setdefault(
                        record.resource_hash.lower(), []
                    ).append((packages[record.package_id], record))

    @staticmethod
    def _reference_variants(reference: str, package_name: str) -> tuple[str, ...]:
        return _package_reference_variants(reference, package_name)

    def _read_content(self, digest: str) -> Path | None:
        existing = self.known_by_md5.get(digest)
        if (
            existing is not None
            and existing.is_file()
            and existing.suffix.lower() == ".ktx"
        ):
            return existing

        cached = self.cache_root / digest[:2] / f"{digest}.ktx"
        if cached.is_file():
            self.known_by_md5[digest] = cached
            return cached

        sources = self.content_records.get(digest, [])
        for package_path, record in sources:
            try:
                read_size = self.wpk.record_read_size(record)
                if (
                    record.offset < 0
                    or read_size <= 0
                    or record.offset + read_size > package_path.stat().st_size
                ):
                    continue
                with package_path.open("rb") as stream:
                    stream.seek(record.offset)
                    blob = stream.read(read_size)
                if len(blob) != read_size:
                    continue
                decoded, _ = self.wpk.decode_stage1(blob, record.key_length)
                decoded, _ = self.wpk.unwrap_payload(
                    decoded, self.wpk.load_zstandard()
                )
                if (
                    not decoded.startswith(b"\xABKTX 11\xBB\r\n\x1A\n")
                    or hashlib.md5(decoded).hexdigest() != digest
                ):
                    continue
                cached.parent.mkdir(parents=True, exist_ok=True)
                temporary = cached.with_suffix(".ktx.tmp")
                temporary.write_bytes(decoded)
                temporary.replace(cached)
                self.known_by_md5[digest] = cached
                return cached
            except Exception:
                continue
        return None

    def _record_hits(self, reference: str) -> dict[str, object]:
        hits: dict[str, object] = {}
        for package_name, records, seeds in self.thx_sources:
            for variant in self._reference_variants(reference, package_name):
                for seed in seeds:
                    name_hash = self.cloudfilesys_name_hash(
                        variant, package_name, seed
                    )
                    record = records.get(name_hash)
                    if record is not None:
                        hits.setdefault(record.content_md5, record)
        return hits

    def _download_remote_content(
        self,
        digest: str,
        record: object,
        max_bytes: int = 32 * 1024 * 1024,
    ) -> Path | None:
        """从 cloud.json 指定的官方动态仓库定向补一个已知 THX 内容。"""
        if not self.base_url or len(digest) != 32:
            return None
        expected_size = int(getattr(record, "size", 0) or 0)
        if expected_size < 8 or expected_size > max_bytes:
            return None
        try:
            import urllib.request

            url = (
                self.base_url.rstrip("/")
                + f"/dynamic/{digest[:2]}/{digest[2:]}"
            )
            request = urllib.request.Request(
                url, headers={"User-Agent": "OnmyojiResourceTool/1.0"}
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                blob = response.read(expected_size + 1)
            if len(blob) != expected_size:
                return None
            decoded, _ = self.wpk.decode_stage1(blob, len(blob))
            decoded, _ = self.wpk.unwrap_payload(
                decoded, self.wpk.load_zstandard()
            )
            if (
                not decoded.startswith(b"\xABKTX 11\xBB\r\n\x1A\n")
                or hashlib.md5(decoded).hexdigest() != digest
            ):
                return None
            cached = self.cache_root / digest[:2] / f"{digest}.ktx"
            cached.parent.mkdir(parents=True, exist_ok=True)
            temporary = cached.with_suffix(".ktx.tmp")
            temporary.write_bytes(decoded)
            temporary.replace(cached)
            self.known_by_md5[digest] = cached
            return cached
        except Exception:
            return None

    def fetch_remote(self, reference: str) -> Path | None:
        """仅在当前全部 THX 对引用给出唯一内容时定向下载。"""
        cache_key = reference.strip().replace("\\", "/").lower()
        hits = self._record_hits(reference)
        resolved = [
            path
            for digest in hits
            if (path := self._read_content(digest)) is not None
        ]
        resolved = list(dict.fromkeys(resolved))
        if len(resolved) == 1:
            self.result_cache[cache_key] = resolved[0]
            return resolved[0]
        if resolved or len(hits) != 1:
            return None
        digest, record = next(iter(hits.items()))
        result = self._download_remote_content(digest, record)
        if result is not None:
            self.result_cache[cache_key] = result
        return result

    def resolve(self, reference: str) -> Path | None:
        cache_key = reference.strip().replace("\\", "/").lower()
        if cache_key in self.result_cache:
            return self.result_cache[cache_key]

        hits = self._record_hits(reference)

        # 不同 THX 若把同一路径解析成不同内容，属于版本/包歧义，宁可留白。
        resolved: list[Path] = []
        for digest in hits:
            path = self._read_content(digest)
            if path is not None and path not in resolved:
                resolved.append(path)
        result = resolved[0] if len(resolved) == 1 else None
        self.result_cache[cache_key] = result
        return result


def sync_large_white_model_dependencies(
    output_root: Path,
    thd_dir: Path,
    model_folder: Path,
    log: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Close official THP material dependencies for the large main-model report.

    The report is deliberately the scope limiter: effects and level components
    are not pulled in here.  For each reported Mesh we follow only its current
    ``model.thp`` parent and children, then verify every recovered XML/KTX by
    the THX content MD5.  This is a dependency closure, not a filename or
    archive-neighbour heuristic.
    """
    result = {
        "targets": 0,
        "official_dependencies": 0,
        "logical_gims": 0,
        "reused": 0,
        "local": 0,
        "remote": 0,
        "missing": 0,
    }
    report_path = output_root / "白模优先检查_角色主包.csv"
    thx_path = thd_dir / "model.thx"
    thp_path = thd_dir / "model.thp"
    if not report_path.is_file() or not thx_path.is_file() or not thp_path.is_file():
        return result
    try:
        with report_path.open("r", newline="", encoding="utf-8-sig") as stream:
            report_rows = list(csv.DictReader(stream))
    except (OSError, csv.Error):
        return result

    target_paths: set[Path] = set()
    for row in report_rows:
        raw_path = str(row.get("物理Mesh路径") or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path).resolve()
        try:
            if path.is_file() and path.stat().st_size >= 100 * 1024:
                target_paths.add(path)
        except OSError:
            continue
    if not target_paths:
        return result
    result["targets"] = len(target_paths)

    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thp,
        read_model_thx,
        read_thx_namehash_seeds,
    )


def _material_package_for_mesh(
    package: MaterialPackage,
    mesh_path: Path,
) -> MaterialPackage:
    """为同内容 Mesh 副本创建独立索引视图。

    材质定义本身可以共享，但 ``mesh_paths`` 必须只包含当前别名；否则
    组合模型分析会把规范路径和热更新副本误认为两个组件。
    """
    return MaterialPackage(
        xml_path=package.xml_path,
        index=package.index,
        package_name=package.package_name,
        materials=package.materials,
        mesh_paths=[mesh_path],
        texture_map=dict(package.texture_map),
        confidence=package.confidence,
    )


def _merge_material_mesh_content_aliases(
    by_mesh: dict[Path, MaterialPackage],
    variants_by_mesh: dict[Path, list[MaterialPackage]],
    md5_by_path: dict[Path, str],
) -> int:
    """按完整内容 MD5 把材质包扩展到同一 Mesh 的所有物理副本。

    解包器允许规范包、热更新依赖和 loose_model 同时落盘。同一个官方
    Mesh 因而可能有多个路径，而 THX/GIM 解析只会先命中其中一个路径。
    这里只使用清单已记录或已验证的完整 MD5，绝不通过文件名或几何推断。
    """
    paths_by_md5: dict[str, set[Path]] = defaultdict(set)
    for raw_path, digest in md5_by_path.items():
        digest = str(digest).strip().lower()
        if len(digest) != 32 or raw_path.suffix.lower() != ".mesh":
            continue
        try:
            path = raw_path.resolve()
        except OSError:
            path = raw_path
        paths_by_md5[digest].add(path)

    def digest_for(path: Path) -> str | None:
        digest = md5_by_path.get(path)
        if digest is None:
            try:
                digest = md5_by_path.get(path.resolve())
            except OSError:
                digest = None
        digest = str(digest or "").strip().lower()
        return digest if len(digest) == 32 else None

    added = 0
    # Snapshot the source entries so aliases do not recursively fan out.
    source_by_mesh = list(by_mesh.items())
    for source_path, package in source_by_mesh:
        digest = digest_for(source_path)
        if digest is None:
            continue
        for alias_path in paths_by_md5.get(digest, ()):
            if alias_path == source_path or not alias_path.is_file():
                continue
            if alias_path not in by_mesh:
                by_mesh[alias_path] = _material_package_for_mesh(
                    package, alias_path
                )
                added += 1

    source_variants = list(variants_by_mesh.items())
    for source_path, source_packages in source_variants:
        digest = digest_for(source_path)
        if digest is None:
            continue
        for alias_path in paths_by_md5.get(digest, ()):
            if alias_path == source_path:
                continue
            target = variants_by_mesh.setdefault(alias_path, [])
            signatures = {
                _material_variant_signature(item.materials)
                for item in target
            }
            for package in source_packages:
                signature = _material_variant_signature(package.materials)
                if signature in signatures:
                    continue
                target.append(
                    _material_package_for_mesh(package, alias_path)
                )
                signatures.add(signature)
    return added

    try:
        records = read_model_thx(thx_path)
        dependencies = read_model_thp(thp_path)
        namehash_seeds = read_thx_namehash_seeds(thx_path)
    except (OSError, ValueError, EOFError):
        return result
    record_by_hash = {record.name_hash: record for record in records}
    records_by_md5: dict[str, list[object]] = defaultdict(list)
    parents_by_child: dict[int, list[int]] = defaultdict(list)
    for record in records:
        records_by_md5[record.content_md5].append(record)
    for parent_hash, child_hashes in dependencies.items():
        for child_hash in child_hashes:
            parents_by_child[child_hash].append(parent_hash)

    # Meshes in loose_model/hot-update caches may not be present in the model
    # manifest, so derive their content identity directly.  The report contains
    # only a few dozen P0 paths and this is still an exact MD5 identity check.
    mesh_digests: set[str] = set()
    for path in target_paths:
        try:
            mesh_digests.add(hashlib.md5(path.read_bytes()).hexdigest())
        except OSError:
            continue

    wanted: dict[str, object] = {}
    for mesh_digest in mesh_digests:
        for mesh_record in records_by_md5.get(mesh_digest, []):
            for parent_hash in parents_by_child.get(mesh_record.name_hash, []):
                parent = record_by_hash.get(parent_hash)
                if parent is not None:
                    wanted.setdefault(parent.content_md5, parent)
                for child_hash in dependencies.get(parent_hash, []):
                    child = record_by_hash.get(child_hash)
                    if child is not None:
                        wanted.setdefault(child.content_md5, child)

    # A number of hot-update entries retain their exact logical Mesh identity
    # in res descriptors after model.thp has dropped the parent GIM.  A sibling
    # ``.gim`` lookup through the same THX seeds is still an official identity
    # relation.  It recovers the GIM for later material analysis, but does not
    # invent a MaterialGroup when no dependency table declares one.
    try:
        logical_paths = load_res_asset_paths(thd_dir, model_folder)
    except Exception:
        logical_paths = []
    for raw_reference in logical_paths:
        reference = raw_reference.strip().replace("\\", "/").lower()
        if not reference.startswith("model/") or not reference.endswith(".mesh"):
            continue
        mesh_hits = {
            record.content_md5
            for variant in _package_reference_variants(reference, "model")
            for seed in namehash_seeds
            if (record := record_by_hash.get(
                cloudfilesys_name_hash(variant, "model", seed)
            )) is not None
        }
        if len(mesh_hits) != 1 or next(iter(mesh_hits)) not in mesh_digests:
            continue
        gim_hits = {
            record
            for variant in _package_reference_variants(reference[:-5] + ".gim", "model")
            for seed in namehash_seeds
            if (record := record_by_hash.get(
                cloudfilesys_name_hash(variant, "model", seed)
            )) is not None
        }
        if len(gim_hits) != 1:
            continue
        gim_record = next(iter(gim_hits))
        if gim_record.content_md5 not in wanted:
            wanted[gim_record.content_md5] = gim_record
            result["logical_gims"] += 1
    result["official_dependencies"] = len(wanted)
    if not wanted:
        if log:
            log(
                f"大白模官方依赖闭包：检查 {len(target_paths):,} 个；"
                "当前及已缓存 THP 均未声明父材质链。"
            )
        return result

    import onmyoji_wpk_gui as wpk

    cache_root = model_folder.parent / "extra_rigged" / "model_dependencies"
    cache_manifest = cache_root / "material_manifest.csv"
    known: dict[str, str] = {}
    if cache_manifest.is_file():
        try:
            with cache_manifest.open("r", newline="", encoding="utf-8-sig") as stream:
                for row in csv.DictReader(stream):
                    digest = (row.get("resource_hash") or "").strip().lower()
                    relative = (row.get("output_path") or "").strip()
                    if (
                        len(digest) == 32
                        and relative
                        and row.get("status") == "ok"
                        and (cache_root / Path(relative.replace("\\", "/"))).is_file()
                    ):
                        known[digest] = relative
        except (OSError, csv.Error):
            known = {}

    by_md5, _ = _manifest_hash_maps(model_folder)

    def save_content(digest: str, data: bytes) -> bool:
        extension = wpk.detect_extension(data)
        if extension not in {"xml", "ktx"} or hashlib.md5(data).hexdigest() != digest:
            return False
        target = cache_root / digest[:2] / f"{digest}.{extension}"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(target)
        known[digest] = str(target.relative_to(cache_root))
        return True

    unresolved = set(wanted) - set(known)
    for digest in list(unresolved):
        existing = by_md5.get(digest)
        if existing is None or existing.suffix.lower() not in {".xml", ".ktx"}:
            continue
        try:
            if save_content(digest, existing.read_bytes()):
                unresolved.remove(digest)
                result["reused"] += 1
        except OSError:
            continue

    # A model THP can point at an XML/KTX stored in any current WPK family.
    # Scan IDX records only for the exact still-missing MD5s, then decode the
    # matching slot; we never use its ordinal position as evidence.
    local_sources: dict[str, list[tuple[Path, object]]] = defaultdict(list)
    source_root = thd_dir.parent / "res"
    if unresolved and source_root.is_dir():
        for idx_path in sorted(source_root.glob("*.idx"), key=lambda item: item.name.lower()):
            stem = idx_path.stem
            group = _load_named_wpk_group(source_root, stem)
            if group is None:
                continue
            for item in group.records:
                digest = item.resource_hash.lower()
                if (
                    digest in unresolved
                    and wpk.record_is_active(item)
                    and item.package_id in group.packages
                ):
                    local_sources[digest].append((group.packages[item.package_id], item))

    zstandard_module = wpk.load_zstandard()
    for digest in list(unresolved):
        for package_path, item in local_sources.get(digest, []):
            try:
                read_size = wpk.record_read_size(item)
                with package_path.open("rb") as stream:
                    stream.seek(item.offset)
                    blob = stream.read(read_size)
                decoded, _ = wpk.decode_stage1(blob, item.key_length)
                decoded, _ = wpk.unwrap_payload(decoded, zstandard_module)
                if save_content(digest, decoded):
                    unresolved.remove(digest)
                    result["local"] += 1
                    break
            except Exception:
                continue

    # Remaining entries have an exact THX identity but no local WPK slot.  The
    # dynamic store is contacted only for this finite, official MD5 set.
    base_url = ""
    cloud_config = thd_dir.parent / "cloud.json"
    if unresolved and cloud_config.is_file():
        try:
            base_url = str(json.loads(cloud_config.read_text(encoding="utf-8")).get("base_url") or "").strip()
        except (OSError, ValueError, json.JSONDecodeError):
            base_url = ""
    if unresolved and base_url:
        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            import urllib.request

            remote_candidates = [
                (digest, wanted[digest])
                for digest in unresolved
                if 8 <= int(getattr(wanted[digest], "size", 0) or 0)
                <= 32 * 1024 * 1024
            ]

            def download_one(digest: str, record: object) -> tuple[str, bytes | None]:
                expected_size = int(getattr(record, "size", 0) or 0)
                try:
                    url = base_url.rstrip("/") + f"/dynamic/{digest[:2]}/{digest[2:]}"
                    request = urllib.request.Request(
                        url, headers={"User-Agent": "OnmyojiResourceTool/1.0"}
                    )
                    with urllib.request.urlopen(request, timeout=8) as response:
                        blob = response.read(expected_size + 1)
                    if len(blob) != expected_size:
                        return digest, None
                    decoded, _ = wpk.decode_stage1(blob, len(blob))
                    decoded, _ = wpk.unwrap_payload(decoded, zstandard_module)
                    return digest, decoded
                except Exception:
                    return digest, None

            if remote_candidates:
                with ThreadPoolExecutor(
                    max_workers=min(4, len(remote_candidates))
                ) as executor:
                    futures = [
                        executor.submit(download_one, digest, record)
                        for digest, record in remote_candidates
                    ]
                    for future in as_completed(futures):
                        digest, decoded = future.result()
                        if decoded is not None and save_content(digest, decoded):
                            unresolved.remove(digest)
                            result["remote"] += 1
        except ImportError:
            pass

    result["missing"] = len(unresolved)
    if known:
        cache_root.mkdir(parents=True, exist_ok=True)
        with cache_manifest.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow(["resource_hash", "output_path", "status"])
            for digest, relative in sorted(known.items()):
                writer.writerow([digest, relative, "ok"])
    if log:
        log(
            f"大白模官方依赖闭包：目标 {result['targets']:,}；"
            f"THP/逻辑依赖 {result['official_dependencies']:,}（补回 GIM {result['logical_gims']:,}）；"
            f"复用 {result['reused']:,}、本地补出 {result['local']:,}、"
            f"官方下载 {result['remote']:,}、仍缺 {result['missing']:,}。"
        )
    return result


def sync_large_white_remote_textures(
    output_root: Path,
    thd_dir: Path,
    model_folder: Path,
    log: Callable[[str], None] | None = None,
) -> int:
    """按上次 P0 白模报告，定向补取唯一 THX 主贴图内容。

    只处理已有唯一官方 GIM/MaterialGroup 的大模型；路径无 THX 身份、
    多内容歧义或超过大小上限时均不联网。下载内容还必须通过 PC 解码、
    KTX 魔数和文件名 MD5 三重验证。
    """
    report_path = output_root / "白模优先检查_角色主包.csv"
    if not report_path.is_file():
        return 0
    try:
        with report_path.open("r", newline="", encoding="utf-8-sig") as stream:
            report_rows = list(csv.DictReader(stream))
    except OSError:
        return 0

    target_paths: set[Path] = set()
    for row in report_rows:
        physical = next(
            (
                str(value)
                for value in row.values()
                if value
                and str(value).lower().endswith(".mesh")
                and "unpacked" in str(value).lower()
            ),
            "",
        )
        if physical:
            path = Path(physical).resolve()
            if path.is_file() and path.stat().st_size >= 100 * 1024:
                target_paths.add(path)
    if not target_paths:
        return 0

    from thd_resource_index import read_model_thp, read_model_thx

    by_md5, md5_by_path = _manifest_hash_maps(model_folder)
    records = read_model_thx(thd_dir / "model.thx")
    record_by_hash = {record.name_hash: record for record in records}
    records_by_md5: dict[str, list[object]] = defaultdict(list)
    for record in records:
        records_by_md5[record.content_md5].append(record)
    dependencies = read_model_thp(thd_dir / "model.thp")
    parents_by_child: dict[int, list[int]] = defaultdict(list)
    for parent_hash, child_hashes in dependencies.items():
        for child_hash in child_hashes:
            parents_by_child[child_hash].append(parent_hash)

    references: set[str] = set()
    for mesh_path in target_paths:
        digest = md5_by_path.get(mesh_path)
        if not digest:
            continue
        try:
            expected_submeshes = read_mesh_submesh_count(mesh_path)
        except Exception:
            continue
        for mesh_record in records_by_md5.get(digest, []):
            for parent_hash in parents_by_child.get(mesh_record.name_hash, []):
                parent = record_by_hash.get(parent_hash)
                gim_path = (
                    by_md5.get(parent.content_md5) if parent is not None else None
                )
                if gim_path is None or gim_path.suffix.lower() != ".xml":
                    continue
                gim_submeshes = parse_gim_submeshes(gim_path)
                if not gim_submeshes:
                    continue
                dependency_items: list[tuple[int, Path]] = []
                for child_hash in dependencies[parent_hash]:
                    child = record_by_hash.get(child_hash)
                    path = (
                        by_md5.get(child.content_md5)
                        if child is not None else None
                    )
                    if path is not None:
                        dependency_items.append((child_hash, path))
                starts = [
                    index
                    for index, (child_hash, path) in enumerate(dependency_items)
                    if child_hash == mesh_record.name_hash
                    and path.suffix.lower() == ".mesh"
                ]
                for start in starts:
                    end = len(dependency_items)
                    for index in range(start + 1, len(dependency_items)):
                        _, path = dependency_items[index]
                        if (
                            path.suffix.lower() in {".mesh", ".skeleton"}
                            or parse_gim_submeshes(path)
                        ):
                            end = index
                            break
                    choices: list[list[MaterialDefinition]] = []
                    for _, path in dependency_items[start + 1 : end]:
                        if path.suffix.lower() != ".xml":
                            continue
                        materials = parse_material_xml(path)
                        ordered = order_materials_by_gim(
                            materials, gim_submeshes
                        ) if materials else []
                        if (
                            ordered
                            and len(ordered) == expected_submeshes
                            and _ordered_texture_references(materials)
                        ):
                            choices.append(ordered)
                    if len(choices) != 1:
                        continue
                    for material in choices[0]:
                        primary = material_primary_texture(material)
                        if primary:
                            references.add(primary)

    resolver = CrossPackageTextureResolver(thd_dir, model_folder, by_md5)
    fetched = 0
    unresolved = 0
    for reference in sorted(references):
        if resolver.resolve(reference) is not None:
            continue
        path = resolver.fetch_remote(reference)
        if path is not None:
            fetched += 1
        else:
            unresolved += 1
    if log:
        log(
            f"大白模主贴图定向补全：检查引用 {len(references):,}；"
            f"官方下载新增 {fetched:,}；无唯一 THX 内容 {unresolved:,}。"
        )
    return fetched


def _load_named_wpk_group(source_root: Path, stem: str):
    """只解析指定 IDX/WPK 组，避免 discover_groups() 扫完整资源目录。"""
    import onmyoji_wpk_gui as wpk

    idx_path = source_root / f"{stem}.idx"
    if not idx_path.is_file():
        return None
    try:
        marker, records = wpk.parse_idx(idx_path)
    except (OSError, ValueError):
        return None

    package_pattern = re.compile(
        rf"^{re.escape(stem)}(\d+)\.wpk$", re.IGNORECASE
    )
    packages: dict[int, Path] = {}
    for candidate in source_root.glob(f"{stem}*.wpk"):
        match = package_pattern.fullmatch(candidate.name)
        if match:
            packages[int(match.group(1))] = candidate
    if not packages:
        return None

    required_ids = {
        record.package_id
        for record in records
        if wpk.record_is_active(record)
    }
    return wpk.ArchiveGroup(
        idx_path=idx_path,
        stem=stem,
        marker=marker,
        records=records,
        packages=packages,
        missing_package_ids=tuple(sorted(required_ids - packages.keys())),
    )


def _named_wpk_quick_fingerprint(
    source_root: Path,
    stem: str,
) -> tuple[object, ...]:
    idx_path = source_root / f"{stem}.idx"
    package_pattern = re.compile(
        rf"^{re.escape(stem)}\d+\.wpk$", re.IGNORECASE
    )
    packages = sorted(
        (
            path for path in source_root.glob(f"{stem}*.wpk")
            if package_pattern.fullmatch(path.name)
        ),
        key=lambda path: path.name.lower(),
    )
    return (
        _file_build_stamp(idx_path),
        tuple(_file_build_stamp(path) for path in packages),
    )


def load_script3_gim_paths(
    thd_dir: Path,
    model_folder: Path,
) -> list[str]:
    """从 script3 资源提取 GIM 明文路径，并按 script3 文件指纹增量缓存。"""
    import onmyoji_wpk_gui as wpk

    source_root = thd_dir.parent / "res"
    if not source_root.is_dir():
        return []
    memory_key = (
        "asset-path-scan-v4",
        str(source_root.resolve()),
        str(model_folder.resolve()),
        _named_wpk_quick_fingerprint(source_root, "script3"),
    )
    cached_memory = _SCRIPT3_GIM_PATHS_MEMORY_CACHE.get(memory_key)
    if cached_memory is not None:
        return list(cached_memory)
    group = _load_named_wpk_group(source_root, "script3")
    if group is None:
        return []

    fingerprint = {
        "parser": "asset-path-scan-v4",
        "idx": _file_build_stamp(group.idx_path),
        "packages": [
            _file_build_stamp(path)
            for _, path in sorted(group.packages.items())
        ],
    }
    cache_path = model_folder.parent / "script3_gim_paths.json"
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("fingerprint") == fingerprint:
                paths = cached.get("paths")
                if isinstance(paths, list):
                    result = [str(path) for path in paths]
                    _SCRIPT3_GIM_PATHS_MEMORY_CACHE[memory_key] = result
                    return list(result)
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    # Paths are embedded back-to-back in some SFX records.  Require a token
    # boundary on both sides so a match cannot swallow the preceding effect
    # name and fabricate a non-existent GIM path.
    pattern = re.compile(
        rb"(?:model|levelsets|static|fx|natural|npcmodel)/"
        rb"[A-Za-z0-9_@./\\-]{1,240}?\.gim"
        rb"",
        re.IGNORECASE,
    )
    handles = {
        package_id: path.open("rb")
        for package_id, path in group.packages.items()
    }
    zstandard_module = wpk.load_zstandard()
    found: set[str] = set()
    try:
        for record in group.records:
            if not wpk.record_is_active(record):
                continue
            stream = handles.get(record.package_id)
            if stream is None:
                continue
            try:
                stream.seek(record.offset)
                blob = stream.read(wpk.record_read_size(record))
                if len(blob) != wpk.record_read_size(record):
                    continue
                decoded, _ = wpk.decode_stage1(blob, record.key_length)
                decoded, _ = wpk.unwrap_payload(decoded, zstandard_module)
                for raw in pattern.findall(decoded):
                    try:
                        value = raw.decode("utf-8").replace("\\", "/").lower()
                        # SFX payloads may concatenate a second asset path
                        # immediately after the first extension. Keep only the
                        # first complete logical path.
                        match = re.search(r"\.gim", value, re.IGNORECASE)
                        if match:
                            value = value[: match.end()]
                        if ".sfx" not in value and value.endswith(".gim"):
                            found.add(value)
                    except UnicodeDecodeError:
                        continue
            except Exception:
                continue
    finally:
        for stream in handles.values():
            stream.close()

    paths = sorted(found)
    try:
        cache_path.write_text(
            json.dumps(
                {"fingerprint": fingerprint, "paths": paths},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass
    _SCRIPT3_GIM_PATHS_MEMORY_CACHE[memory_key] = paths
    return list(paths)


def load_res_asset_paths(
    thd_dir: Path,
    model_folder: Path,
) -> list[str]:
    """从 res 资源清单恢复 model/* 的 Mesh/纹理/GIM 明文路径并增量缓存。"""
    import onmyoji_wpk_gui as wpk

    source_root = thd_dir.parent / "res"
    if not source_root.is_dir():
        return []
    memory_key = (
        "asset-path-scan-v4",
        str(source_root.resolve()),
        str(model_folder.resolve()),
        _named_wpk_quick_fingerprint(source_root, "res"),
    )
    cached_memory = _RES_ASSET_PATHS_MEMORY_CACHE.get(memory_key)
    if cached_memory is not None:
        return list(cached_memory)
    group = _load_named_wpk_group(source_root, "res")
    if group is None:
        return []

    fingerprint = {
        "parser": "asset-path-scan-v4",
        "idx": _file_build_stamp(group.idx_path),
        "packages": [
            _file_build_stamp(path)
            for _, path in sorted(group.packages.items())
        ],
    }
    cache_path = model_folder.parent / "res_asset_paths.json"
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("fingerprint") == fingerprint:
                paths = cached.get("paths")
                if isinstance(paths, list):
                    result = [str(path) for path in paths]
                    _RES_ASSET_PATHS_MEMORY_CACHE[memory_key] = result
                    return list(result)
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    pattern = re.compile(
        rb"(?:model|levelsets|static|fx|natural|npcmodel)/"
        rb"[A-Za-z0-9_@./\\-]{1,240}?\."
        rb"(?:mesh|gim|tga|png|dds|ktx)"
        rb"",
        re.IGNORECASE,
    )
    handles = {
        package_id: path.open("rb")
        for package_id, path in group.packages.items()
    }
    zstandard_module = wpk.load_zstandard()
    found: set[str] = set()
    try:
        for record in group.records:
            if not wpk.record_is_active(record):
                continue
            stream = handles.get(record.package_id)
            if stream is None:
                continue
            try:
                stream.seek(record.offset)
                blob = stream.read(wpk.record_read_size(record))
                if len(blob) != wpk.record_read_size(record):
                    continue
                decoded, _ = wpk.decode_stage1(blob, record.key_length)
                decoded, _ = wpk.unwrap_payload(decoded, zstandard_module)
                for raw in pattern.findall(decoded):
                    try:
                        value = raw.decode("utf-8").replace("\\", "/").lower()
                        match = re.search(r"\.(?:mesh|gim|tga|png|dds|ktx)", value, re.IGNORECASE)
                        if match:
                            found.add(value[: match.end()])
                    except UnicodeDecodeError:
                        continue
            except Exception:
                continue
    finally:
        for stream in handles.values():
            stream.close()

    paths = sorted(found)
    try:
        cache_path.write_text(
            json.dumps(
                {"fingerprint": fingerprint, "paths": paths},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass
    _RES_ASSET_PATHS_MEMORY_CACHE[memory_key] = paths
    return list(paths)


def load_fx_asset_paths(
    thd_dir: Path,
    model_folder: Path,
) -> list[str]:
    """Scan the FX descriptor archive for package-local asset references.

    FX descriptors are the authoritative users of ``fx/model`` GIMs and
    ``fx/texture`` images.  The result is content-independent path evidence;
    decoding and material binding still go through the package THX tables.
    A WPK/IDX fingerprint makes this an incremental operation after updates.
    """
    import onmyoji_wpk_gui as wpk

    source_root = thd_dir.parent / "res"
    if not source_root.is_dir():
        return []
    memory_key = (
        "asset-path-scan-v6",
        str(source_root.resolve()),
        str(model_folder.resolve()),
        _named_wpk_quick_fingerprint(source_root, "fx"),
    )
    cached_memory = _FX_ASSET_PATHS_MEMORY_CACHE.get(memory_key)
    if cached_memory is not None:
        return list(cached_memory)
    group = _load_named_wpk_group(source_root, "fx")
    if group is None:
        return []
    fingerprint = {
        "parser": "asset-path-scan-v6",
        "idx": list(_file_build_stamp(group.idx_path)),
        "packages": [
            list(_file_build_stamp(path))
            for _, path in sorted(group.packages.items())
        ],
    }
    cache_path = model_folder.parent / "fx_asset_paths.json"
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("fingerprint") == fingerprint:
                paths = cached.get("paths")
                bindings = cached.get("tex0_bindings")
                if isinstance(paths, list) and isinstance(bindings, dict):
                    result = [str(path) for path in paths]
                    _FX_TEX0_BINDINGS_MEMORY_CACHE[memory_key] = {
                        str(gim): [str(texture) for texture in textures]
                        for gim, textures in bindings.items()
                        if isinstance(textures, list)
                    }
                    _FX_ASSET_PATHS_MEMORY_CACHE[memory_key] = result
                    return list(result)
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    pattern = re.compile(
        rb"(?:model|levelsets|static|fx|natural|npcmodel)/"
        rb"[A-Za-z0-9_@./\\-]{1,240}?\."
        rb"(?:mesh|gim|tga|png|dds|ktx)",
        re.IGNORECASE,
    )
    handles = {
        package_id: path.open("rb")
        for package_id, path in group.packages.items()
    }
    zstandard_module = wpk.load_zstandard()
    found: set[str] = set()
    tex0_by_gim: dict[str, set[str]] = defaultdict(set)
    model_block_pattern = re.compile(rb"<Model\b.*?</Model>", re.IGNORECASE | re.DOTALL)
    model_name_pattern = re.compile(
        rb'\bModelName="([^"]+\.gim)"', re.IGNORECASE
    )
    tex0_pattern = re.compile(
        rb'<Semantic\b.*?\bName="Tex0".*?\bValue="'
        rb'([^"]+\.(?:tga|png|dds|ktx|jpg|jpeg|bmp))".*?/>',
        re.IGNORECASE | re.DOTALL,
    )
    try:
        for record in group.records:
            if not wpk.record_is_active(record):
                continue
            stream = handles.get(record.package_id)
            if stream is None:
                continue
            try:
                stream.seek(record.offset)
                read_size = wpk.record_read_size(record)
                blob = stream.read(read_size)
                if len(blob) != read_size:
                    continue
                decoded, _ = wpk.decode_stage1(blob, record.key_length)
                decoded, _ = wpk.unwrap_payload(decoded, zstandard_module)
                for raw in pattern.findall(decoded):
                    try:
                        value = raw.decode("utf-8").replace("\\", "/").lower()
                        match = re.search(
                            r"\.(?:mesh|gim|tga|png|dds|ktx)",
                            value,
                            re.IGNORECASE,
                        )
                        if match:
                            found.add(value[: match.end()])
                    except UnicodeDecodeError:
                        continue
                for block in model_block_pattern.findall(decoded):
                    model_match = model_name_pattern.search(block)
                    if model_match is None:
                        continue
                    try:
                        gim_reference = (
                            model_match.group(1)
                            .decode("utf-8")
                            .replace("\\", "/")
                            .lower()
                        )
                    except UnicodeDecodeError:
                        continue
                    for texture_match in tex0_pattern.finditer(block):
                        try:
                            texture_reference = (
                                texture_match.group(1)
                                .decode("utf-8")
                                .replace("\\", "/")
                                .lower()
                            )
                        except UnicodeDecodeError:
                            continue
                        tex0_by_gim[gim_reference].add(texture_reference)
            except Exception:
                continue
    finally:
        for stream in handles.values():
            stream.close()

    paths = sorted(found)
    tex0_bindings = {
        gim: sorted(textures) for gim, textures in sorted(tex0_by_gim.items())
    }
    try:
        cache_path.write_text(
            json.dumps(
                {
                    "fingerprint": fingerprint,
                    "paths": paths,
                    "tex0_bindings": tex0_bindings,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass
    _FX_ASSET_PATHS_MEMORY_CACHE[memory_key] = paths
    _FX_TEX0_BINDINGS_MEMORY_CACHE[memory_key] = tex0_bindings
    return list(paths)


def _correct_cross_mesh_package_labels(
    packages: Iterable[MaterialPackage],
    asset_paths: Iterable[str],
    resolve_mesh_reference: Callable[[str], Path | None],
) -> int:
    """Correct a decoded GIM label only when it names another exact Mesh.

    Some APK-extracted GIM XML filenames retain an old semantic label instead
    of their current THX logical key. The label is normally useful for a PMX
    name, but it must not make ``foo.mesh`` appear as ``bar_show`` when the
    latter is an independently indexed Mesh. Build a reverse identity map
    solely from explicit res/script Mesh or GIM paths, then rename only when:

    * the current physical Mesh has one exact logical Mesh identity; and
    * the existing package label itself resolves to a different exact Mesh.

    Ambiguous shared geometry, material-only names, and unindexed labels are
    deliberately left untouched.
    """
    logicals_by_mesh: dict[Path, set[str]] = defaultdict(set)
    meshes_by_stem: dict[str, set[Path]] = defaultdict(set)
    references: set[str] = set()
    for raw_reference in asset_paths:
        reference = raw_reference.strip().replace("\\", "/").lower().lstrip("/")
        if not reference.startswith("model/"):
            continue
        if reference.endswith(".mesh"):
            references.add(reference)
        elif reference.endswith(".gim"):
            # script3 commonly records the GIM but omits its same-stem Mesh.
            # The candidate only becomes evidence if THX resolves it exactly.
            references.add(reference[:-4] + ".mesh")

    for reference in references:
        path = resolve_mesh_reference(reference)
        if path is None:
            continue
        try:
            path = path.resolve()
        except OSError:
            continue
        logicals_by_mesh[path].add(reference)
        meshes_by_stem[Path(reference).stem.lower()].add(path)

    renamed = 0
    seen: set[int] = set()
    for package in packages:
        marker = id(package)
        if marker in seen:
            continue
        seen.add(marker)
        if len(package.mesh_paths) != 1:
            continue
        try:
            mesh_path = package.mesh_paths[0].resolve()
        except OSError:
            continue
        logicals = logicals_by_mesh.get(mesh_path, set())
        if len(logicals) != 1:
            continue
        desired = Path(next(iter(logicals))).stem
        label = package.package_name.strip().lower()
        label_meshes = meshes_by_stem.get(label, set())
        if (
            desired.lower() != label
            and label_meshes
            and mesh_path not in label_meshes
        ):
            package.package_name = desired
            renamed += 1
    return renamed


def load_fx_tex0_bindings(
    thd_dir: Path,
    model_folder: Path,
) -> dict[str, list[str]]:
    """Return exact ModelName GIM -> Tex0 overrides from FX descriptors."""
    source_root = thd_dir.parent / "res"
    memory_key = (
        "asset-path-scan-v6",
        str(source_root.resolve()),
        str(model_folder.resolve()),
        _named_wpk_quick_fingerprint(source_root, "fx"),
    )
    load_fx_asset_paths(thd_dir, model_folder)
    return {
        gim: list(textures)
        for gim, textures in _FX_TEX0_BINDINGS_MEMORY_CACHE.get(
            memory_key, {}
        ).items()
    }


def _build_manual_verified_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    packages: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    for binding in VERIFIED_RESOURCE_BINDINGS:
        mesh_path = by_md5.get(binding["mesh_md5"])
        if mesh_path is None:
            continue
        direct_materials = binding.get("direct_materials")
        gim_path = by_md5.get(binding.get("gim_md5", ""))
        material_path = by_md5.get(binding.get("material_md5", ""))
        if direct_materials is None and (gim_path is None or material_path is None):
            continue
        materials = (
            order_materials_by_gim(
                parse_material_xml(material_path), parse_gim_submeshes(gim_path)
            )
            if gim_path is not None and material_path is not None
            else []
        )
        material_parts = binding.get("material_parts")
        if material_parts:
            merged_materials: list[MaterialDefinition] = []
            valid_parts = True
            for part in material_parts:
                part_material_path = by_md5.get(part.get("material_md5", ""))
                if part_material_path is None:
                    valid_parts = False
                    break
                part_materials = parse_material_xml(part_material_path)
                part_gim_md5 = part.get("gim_md5")
                if part_gim_md5:
                    part_gim_path = by_md5.get(part_gim_md5)
                    if part_gim_path is None:
                        valid_parts = False
                        break
                    part_materials = order_materials_by_gim(
                        part_materials, parse_gim_submeshes(part_gim_path)
                    )
                positions = part.get("positions")
                if positions is not None:
                    try:
                        part_materials = [
                            part_materials[int(index)] for index in positions
                        ]
                    except (IndexError, TypeError, ValueError):
                        valid_parts = False
                        break
                if not part_materials:
                    valid_parts = False
                    break
                merged_materials.extend(part_materials)
            if not valid_parts:
                continue
            materials = merged_materials
        if direct_materials is not None:
            try:
                materials = [
                    MaterialDefinition(
                        str(item.get("name") or f"material_{index}"),
                        ({"Tex0": str(item["texture"])} if item.get("texture") else {}),
                        tuple(item["diffuse_color"])
                        if item.get("diffuse_color") is not None
                        else None,
                    )
                    for index, item in enumerate(direct_materials)
                ]
            except (KeyError, TypeError, ValueError):
                continue
            try:
                if read_mesh_submesh_count(mesh_path) != len(materials):
                    continue
            except (OSError, MeshFormatError, ValueError):
                continue
        if not materials:
            continue
        material_positions = binding.get("material_positions")
        if material_positions is not None:
            try:
                materials = [materials[int(index)] for index in material_positions]
            except (IndexError, TypeError, ValueError):
                continue
        if binding.get("primary_only"):
            # 人工闭环只确认 diffuse/Tex0 时，不把来源 MaterialGroup 中仍有
            # 歧义的法线、mask、环境等辅助槽顺带当成已确认关系。
            materials = [
                MaterialDefinition(
                    material.name,
                    ({"Tex0": primary} if (primary := material_primary_texture(material)) else {}),
                )
                for material in materials
            ]
        texture_overrides = {
            source.strip().replace("\\", "/").lower(): target
            for source, target in binding.get("texture_overrides", {}).items()
        }
        if texture_overrides:
            materials = [
                MaterialDefinition(
                    material.name,
                    {
                        slot: texture_overrides.get(
                            reference.strip().replace("\\", "/").lower(),
                            reference,
                        )
                        for slot, reference in material.textures.items()
                    },
                    material.diffuse_color,
                )
                for material in materials
            ]
        texture_map = {
            original: by_md5[digest]
            for original, digest in binding["textures"].items()
            if digest in by_md5
        }
        if len(texture_map) != len(binding["textures"]):
            continue
        # VERIFIED_RESOURCE_BINDINGS historically mixed slash styles while the
        # source Material XML often keeps Windows backslashes.  Keep the exact
        # verified mapping, but add aliases using the spelling present in each
        # MaterialDefinition so primary-texture lookup can hit the KTX.
        normalized_texture_map = {
            original.replace("\\", "/").lower(): path
            for original, path in texture_map.items()
        }
        for material in materials:
            for original in material.textures.values():
                alias = normalized_texture_map.get(
                    original.replace("\\", "/").lower()
                )
                if alias is not None:
                    texture_map.setdefault(original, alias)
        source_path = material_path or gim_path or mesh_path
        package = MaterialPackage(
            xml_path=source_path,
            index=archive_index(source_path) or 0,
            package_name=(
                str(binding.get("package_name"))
                if binding.get("package_name")
                else extracted_resource_label(gim_path or source_path)
            ),
            materials=materials,
            mesh_paths=[mesh_path],
            texture_map=texture_map,
            confidence="人工验证",
        )
        packages.append(package)
        by_mesh[mesh_path] = package
    return packages, by_mesh


def _build_thp_parent_variant_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    thd_dir: Path,
) -> list[MaterialPackage]:
    """收集同一 GIM 内容由不同 THP parent 提供的官方材质变体。

    只处理“同一 GIM 内容 MD5 对应多个 THP parent”的少量资源。每个 parent
    都独立按自己的依赖顺序解析 Mesh -> MaterialGroup，并要求当前 Mesh 段
    只有一套完整、子网格数匹配的材质组；不做跨 parent 猜测。
    """
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thp,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    thx_path = thd_dir / "model.thx"
    thp_path = thd_dir / "model.thp"
    if not thx_path.is_file() or not thp_path.is_file():
        return []
    try:
        records = read_model_thx(thx_path)
        dependencies = read_model_thp(thp_path)
        seeds = read_thx_namehash_seeds(thx_path)
    except Exception:
        return []

    record_by_hash = {record.name_hash: record for record in records}
    parents_by_gim_md5: dict[str, list[int]] = {}
    for parent_hash in dependencies:
        record = record_by_hash.get(parent_hash)
        path = by_md5.get(record.content_md5) if record is not None else None
        if path is None or path.suffix.lower() != ".xml":
            continue
        if not parse_gim_submeshes(path):
            continue
        parents_by_gim_md5.setdefault(record.content_md5, []).append(parent_hash)

    resolver = CrossPackageTextureResolver(thd_dir, model_folder, by_md5)
    material_cache: dict[Path, list[MaterialDefinition]] = {}
    submesh_count_cache: dict[Path, int | None] = {}
    result: list[MaterialPackage] = []

    for gim_md5, parent_hashes in parents_by_gim_md5.items():
        if len(parent_hashes) < 2:
            continue
        gim_path = by_md5.get(gim_md5)
        if gim_path is None:
            continue
        gim_submeshes = parse_gim_submeshes(gim_path)
        if not gim_submeshes:
            continue

        for parent_hash in parent_hashes:
            dependency_paths: list[Path] = []
            for dependency_hash in dependencies.get(parent_hash, []):
                record = record_by_hash.get(dependency_hash)
                path = by_md5.get(record.content_md5) if record is not None else None
                if path is not None:
                    dependency_paths.append(path.resolve())
            mesh_positions = [
                index
                for index, path in enumerate(dependency_paths)
                if path.suffix.lower() == ".mesh"
            ]
            for mesh_number, start in enumerate(mesh_positions):
                next_mesh = (
                    mesh_positions[mesh_number + 1]
                    if mesh_number + 1 < len(mesh_positions)
                    else len(dependency_paths)
                )
                next_skeleton = next(
                    (
                        index
                        for index in range(start + 1, next_mesh)
                        if dependency_paths[index].suffix.lower() == ".skeleton"
                    ),
                    next_mesh,
                )
                end = min(next_mesh, next_skeleton)
                mesh_path = dependency_paths[start]
                if mesh_path not in submesh_count_cache:
                    try:
                        submesh_count_cache[mesh_path] = read_mesh_submesh_count(
                            mesh_path
                        )
                    except Exception:
                        submesh_count_cache[mesh_path] = None
                expected_submeshes = submesh_count_cache[mesh_path]
                if not expected_submeshes:
                    continue

                choices: dict[
                    tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
                    tuple[Path, list[MaterialDefinition]],
                ] = {}
                for path in dependency_paths[start + 1 : end]:
                    if path.suffix.lower() != ".xml":
                        continue
                    if path not in material_cache:
                        material_cache[path] = parse_material_xml(path)
                    materials = material_cache[path]
                    if not materials:
                        continue
                    ordered = order_materials_by_gim(materials, gim_submeshes)
                    if len(ordered) != expected_submeshes:
                        continue
                    signature = tuple(
                        (material.name, tuple(sorted(material.textures.items())))
                        for material in ordered
                    )
                    choices.setdefault(signature, (path, ordered))
                if len(choices) != 1:
                    continue
                material_path, ordered_materials = next(iter(choices.values()))
                references = _ordered_texture_references(ordered_materials)
                if not references:
                    continue

                texture_map: dict[str, Path] = {}
                for reference in references:
                    texture_path: Path | None = None
                    for seed in seeds:
                        name_hash = cloudfilesys_name_hash(
                            reference, "model", seed
                        )
                        record = record_by_hash.get(name_hash)
                        candidate = (
                            by_md5.get(record.content_md5)
                            if record is not None else None
                        )
                        if (
                            candidate is not None
                            and candidate.suffix.lower() == ".ktx"
                        ):
                            texture_path = candidate
                            break
                    if texture_path is None:
                        texture_path = resolver.resolve(reference)
                    if (
                        texture_path is not None
                        and texture_path.suffix.lower() == ".ktx"
                    ):
                        texture_map[reference] = texture_path

                main_references = {
                    primary
                    for material in ordered_materials
                    if (primary := material_primary_texture(material))
                }
                resolved_main = {
                    reference
                    for reference in main_references
                    if reference in texture_map
                }
                if not main_references or not resolved_main:
                    continue
                if len(texture_map) == len(references):
                    confidence = "THD父节点变体精确"
                elif len(resolved_main) == len(main_references):
                    confidence = "THD父节点变体精确主贴图"
                else:
                    confidence = "THD父节点变体精确部分主贴图"

                # 如果该 parent 的主贴图统一落在一个 model/<目录>/ 下，用它
                # 作为变体名（如 green/red）；否则继续沿用 GIM 资源名。
                texture_directories = {
                    reference.replace("\\", "/").split("/")[1]
                    for reference in main_references
                    if reference.replace("\\", "/").startswith("model/")
                    and len(reference.replace("\\", "/").split("/")) >= 3
                }
                package_name = extracted_resource_label(gim_path)
                if len(texture_directories) == 1:
                    package_name = next(iter(texture_directories))

                result.append(
                    MaterialPackage(
                        xml_path=material_path,
                        index=archive_index(material_path) or 0,
                        package_name=package_name,
                        materials=ordered_materials,
                        mesh_paths=[mesh_path],
                        texture_map=texture_map,
                        confidence=confidence,
                    )
                )
    return result


def _build_zhujue_mode_consensus_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    thd_dir: Path,
    minimum_size: int = 100_000,
    uv_threshold: float = 0.90,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """Recover protagonist gameplay-mode siblings only when multiple official modes agree.

    NeoX packages frequently keep separate ``base / _bat / _tansuo / _tingyuan`` Mesh
    paths for the same protagonist skin. Hot updates may tombstone one mode's GIM and
    MaterialGroup while other modes still retain official GIM+MtlIdx truth. Mode names
    alone are not evidence: some families genuinely use different mode-specific textures.

    A target is accepted only when:
    - it is an unresolved >100KB exact res path matching ``sN_zhujueXX`` plus one of the
      known gameplay mode suffixes;
    - the target has no usable official GIM MaterialGroup of its own;
    - at least two *other* mode GIMs still officially depend on their exact logical Mesh;
    - every source maps one-to-one to the target with >=90% exact UV-triangle coverage;
    - at least two source modes predict the exact same full material definition after the
      UV submesh mapping.

    This multi-source consensus is intentionally stricter than ordinary sibling reuse.
    Families where battle/exploration/courtyard use different textures remain unresolved.
    """
    from collections import defaultdict
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thp,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    thx_path = thd_dir / "model.thx"
    thp_path = thd_dir / "model.thp"
    if not thx_path.is_file() or not thp_path.is_file():
        return [], {}

    try:
        records = read_model_thx(thx_path)
        dependencies = read_model_thp(thp_path)
        seeds = read_thx_namehash_seeds(thx_path)
    except Exception:
        return [], {}
    if not seeds:
        return [], {}

    record_by_hash = {record.name_hash: record for record in records}
    md5_by_path = {path: digest for digest, path in by_md5.items()}

    def norm(value: str) -> str:
        return value.strip().replace("\\", "/").lower()

    mode_pattern = re.compile(
        r"model/(s\d+_zhujue\d+)(?:_(bat|tansuo|tingyuan))?/([^/]+)\.mesh$"
    )

    def parse_family(reference: str) -> tuple[str, str] | None:
        match = mode_pattern.fullmatch(reference)
        if match is None:
            return None
        family, mode, stem = match.groups()
        mode = mode or "base"
        if stem not in {family, f"{family}_{mode}"}:
            return None
        return family, mode

    families: dict[str, dict[str, tuple[str, Path, int]]] = defaultdict(dict)
    for raw_reference in load_res_asset_paths(thd_dir, model_folder):
        reference = norm(raw_reference)
        parsed = parse_family(reference)
        if parsed is None:
            continue
        family, mode = parsed
        for seed in seeds:
            mesh_hash = cloudfilesys_name_hash(reference, "model", seed)
            record = record_by_hash.get(mesh_hash)
            path = by_md5.get(record.content_md5) if record is not None else None
            if path is None or path.suffix.lower() != ".mesh":
                continue
            families[family][mode] = (reference, path, mesh_hash)
            break

    material_cache: dict[Path, list[MaterialDefinition]] = {}
    truth_cache: dict[
        tuple[str, Path, int],
        tuple[Path, list[MaterialDefinition]] | None,
    ] = {}

    def full_material_signature(
        materials: list[MaterialDefinition],
    ) -> tuple[object, ...]:
        return tuple(
            (
                material.name,
                tuple(
                    sorted(
                        (slot, norm(value))
                        for slot, value in material.textures.items()
                    )
                ),
                material.diffuse_color,
            )
            for material in materials
        )

    def official_truth(
        reference: str, mesh_path: Path, mesh_hash: int
    ) -> tuple[Path, list[MaterialDefinition]] | None:
        cache_key = (reference, mesh_path, mesh_hash)
        if cache_key in truth_cache:
            return truth_cache[cache_key]
        gim_reference = reference[:-5] + ".gim"
        choices: dict[
            tuple[object, ...], tuple[Path, list[MaterialDefinition]]
        ] = {}
        mesh_md5 = md5_by_path.get(mesh_path)
        if mesh_md5 is None:
            truth_cache[cache_key] = None
            return None
        for seed in seeds:
            gim_hash = cloudfilesys_name_hash(gim_reference, "model", seed)
            gim_record = record_by_hash.get(gim_hash)
            gim_path = (
                by_md5.get(gim_record.content_md5)
                if gim_record is not None else None
            )
            if gim_path is None or gim_path.suffix.lower() != ".xml":
                continue
            child_hashes = dependencies.get(gim_hash, [])
            # The source GIM must officially depend on this exact logical Mesh content.
            if not any(
                (
                    child_record := record_by_hash.get(child_hash)
                ) is not None
                and child_record.content_md5 == mesh_md5
                for child_hash in child_hashes
            ):
                continue
            gim_submeshes = parse_gim_submeshes(gim_path)
            if not gim_submeshes:
                continue
            for child_hash in child_hashes:
                child_record = record_by_hash.get(child_hash)
                path = (
                    by_md5.get(child_record.content_md5)
                    if child_record is not None else None
                )
                if path is None or path.suffix.lower() != ".xml":
                    continue
                if path not in material_cache:
                    material_cache[path] = parse_material_xml(path)
                materials = material_cache[path]
                if not materials:
                    continue
                ordered = order_materials_by_gim(materials, gim_submeshes)
                if len(ordered) != len(gim_submeshes):
                    continue
                choices.setdefault(
                    full_material_signature(ordered),
                    (path, ordered),
                )
        truth_cache[cache_key] = (
            next(iter(choices.values())) if len(choices) == 1 else None
        )
        return truth_cache[cache_key]

    parsed_cache: dict[Path, ParsedMesh] = {}
    uv_cache: dict[Path, tuple[frozenset[tuple[object, ...]], ...]] = {}

    def uv_sets(path: Path) -> tuple[frozenset[tuple[object, ...]], ...]:
        cached = uv_cache.get(path)
        if cached is not None:
            return cached
        mesh = parsed_cache.get(path)
        if mesh is None:
            mesh = parse_mesh(path)
            parsed_cache[path] = mesh
        output: list[frozenset[tuple[object, ...]]] = []
        face_offset = 0
        for _, face_count, _, _ in mesh.submeshes:
            triangles: set[tuple[object, ...]] = set()
            for a, b, c in mesh.faces[face_offset : face_offset + face_count]:
                triangles.add(
                    tuple(
                        sorted(
                            tuple(round(value, 5) for value in mesh.uvs[index])
                            for index in (a, b, c)
                        )
                    )
                )
            output.append(frozenset(triangles))
            face_offset += face_count
        cached = tuple(output)
        uv_cache[path] = cached
        return cached

    def uv_mapping(target_path: Path, source_path: Path) -> tuple[int, ...] | None:
        try:
            target_sets = uv_sets(target_path)
            source_sets = uv_sets(source_path)
        except Exception:
            return None
        if len(target_sets) != len(source_sets):
            return None
        used: set[int] = set()
        mapping: list[int] = []
        for target_triangles in target_sets:
            scores = [
                len(target_triangles & source_triangles)
                / max(1, len(target_triangles))
                for source_triangles in source_sets
            ]
            matches = [
                index
                for index, score in enumerate(scores)
                if score >= uv_threshold and index not in used
            ]
            if len(matches) != 1:
                return None
            source_index = matches[0]
            used.add(source_index)
            mapping.append(source_index)
        return tuple(mapping)

    resolver = CrossPackageTextureResolver(thd_dir, model_folder, by_md5)
    texture_cache: dict[str, Path | None] = {}

    def resolve_texture(reference: str) -> Path | None:
        key = norm(reference)
        if key in texture_cache:
            return texture_cache[key]
        result: Path | None = None
        for seed in seeds:
            record = record_by_hash.get(
                cloudfilesys_name_hash(reference, "model", seed)
            )
            path = by_md5.get(record.content_md5) if record is not None else None
            if path is not None and path.suffix.lower() == ".ktx":
                result = path
                break
        if result is None:
            candidate = resolver.resolve(reference)
            if candidate is not None and candidate.suffix.lower() == ".ktx":
                result = candidate
        texture_cache[key] = result
        return result

    result: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    for family, modes in families.items():
        if len(modes) < 3:
            continue
        truths = {
            mode: truth
            for mode, (reference, path, mesh_hash) in modes.items()
            if (
                truth := official_truth(reference, path, mesh_hash)
            ) is not None
        }
        if len(truths) < 2:
            continue

        for target_mode, (target_reference, target_path, target_hash) in modes.items():
            if target_path in existing_by_mesh or target_path in by_mesh:
                continue
            try:
                if target_path.stat().st_size < minimum_size:
                    continue
            except OSError:
                continue
            # If target still has official material truth, ordinary THP/GIM rules should
            # own it; consensus is only for genuinely missing mode metadata.
            if official_truth(target_reference, target_path, target_hash) is not None:
                continue

            predictions: dict[
                tuple[object, ...],
                list[tuple[Path, list[MaterialDefinition]]],
            ] = defaultdict(list)
            source_modes_by_signature: dict[tuple[object, ...], set[str]] = defaultdict(set)
            for source_mode, truth in truths.items():
                if source_mode == target_mode:
                    continue
                source_reference, source_path, _ = modes[source_mode]
                mapping = uv_mapping(target_path, source_path)
                if mapping is None:
                    continue
                material_path, source_ordered = truth
                try:
                    predicted = [source_ordered[index] for index in mapping]
                except IndexError:
                    continue
                signature = full_material_signature(predicted)
                predictions[signature].append((material_path, predicted))
                source_modes_by_signature[signature].add(source_mode)

            consensus = [
                signature
                for signature, source_modes in source_modes_by_signature.items()
                if len(source_modes) >= 2
            ]
            if len(consensus) != 1:
                continue
            signature = consensus[0]
            choices = predictions[signature]
            material_path, ordered_materials = choices[0]
            references = _ordered_texture_references(ordered_materials)
            if not references:
                continue
            texture_map: dict[str, Path] = {}
            for reference in references:
                texture_path = resolve_texture(reference)
                if texture_path is not None:
                    texture_map[reference] = texture_path
            main_references = {
                primary
                for material in ordered_materials
                if (primary := material_primary_texture(material))
            }
            if not main_references or not main_references <= texture_map.keys():
                continue
            confidence = (
                "zhujue-mode-consensus-exact"
                if len(texture_map) == len(references)
                else "zhujue-mode-consensus-exact-main-texture"
            )
            package = MaterialPackage(
                xml_path=material_path,
                index=archive_index(material_path) or 0,
                package_name=Path(target_reference).stem,
                materials=list(ordered_materials),
                mesh_paths=[target_path],
                texture_map=texture_map,
                confidence=confidence,
            )
            result.append(package)
            by_mesh[target_path] = package
    return result, by_mesh


def _build_orphan_path_uv_material_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    thd_dir: Path,
    min_size: int = 100_000,
    uv_threshold: float = 0.90,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """Recover important orphan Mesh materials without relying on a live target GIM.

    Hot updates sometimes leave an exact logical ``model/.../*.mesh`` path in THX/res,
    while removing that Mesh from every THP parent and even tombstoning its GIM.  A
    directory-local MaterialGroup is only a *candidate*; it is never enough by itself.

    The candidate becomes trusted only when all of the following are true:
    1. target Mesh has an exact res logical path and zero current THP parents;
    2. target logical directory + submesh count has exactly one MaterialGroup signature;
    3. every primary texture of that MaterialGroup still resolves exactly;
    4. that MaterialGroup is still referenced by an official parent GIM;
    5. an official source Mesh under that GIM maps one-to-one to every target submesh,
       with at least ``uv_threshold`` exact UV triangles from the target contained in
       the source submesh;
    6. all surviving official source parents predict the same ordered material layout.

    UV topology is used only to recover MtlIdx ordering.  The textures themselves always
    come from the target logical directory's unique MaterialGroup, so a geometrically
    related skin cannot donate its own diffuse texture to the target.
    """
    thx_path = thd_dir / "model.thx"
    thp_path = thd_dir / "model.thp"
    if not thx_path.is_file() or not thp_path.is_file():
        return [], {}

    from collections import defaultdict
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thp,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    try:
        records = read_model_thx(thx_path)
        dependencies = read_model_thp(thp_path)
        seeds = read_thx_namehash_seeds(thx_path)
    except Exception:
        return [], {}
    if not seeds:
        return [], {}

    record_by_hash = {record.name_hash: record for record in records}
    records_by_md5: dict[str, list[object]] = defaultdict(list)
    reverse_dependencies: dict[int, list[int]] = defaultdict(list)
    for record in records:
        records_by_md5[record.content_md5].append(record)
    for parent_hash, child_hashes in dependencies.items():
        for child_hash in child_hashes:
            reverse_dependencies[child_hash].append(parent_hash)

    # _manifest_hash_maps() 已返回绝对路径。Windows Path.resolve() 会触发
    # _getfinalpathname 系统调用；对十几万条 manifest 全量 resolve 会白耗数十秒。
    md5_by_path = {path: digest for digest, path in by_md5.items()}

    def norm(value: str) -> str:
        return value.strip().replace("\\", "/").lower()

    # One physical Mesh may have several exact logical aliases.  Keep every alias here;
    # later we accept a physical Mesh only if all strong proposals collapse to one PMX
    # material signature.
    # res 中同一物理 Mesh 可能出现大量逻辑别名。先只做 hash 映射和去重，
    # 再对唯一物理路径 stat 一次；不要在十几万条 res 记录上反复触盘。
    pretargets: dict[tuple[Path, str], str] = {}
    for raw_reference in load_res_asset_paths(thd_dir, model_folder):
        reference = norm(raw_reference)
        if not reference.startswith("model/") or not reference.endswith(".mesh"):
            continue
        for seed in seeds:
            name_hash = cloudfilesys_name_hash(reference, "model", seed)
            record = record_by_hash.get(name_hash)
            if record is None or reverse_dependencies.get(name_hash):
                continue
            path = by_md5.get(record.content_md5)
            if path is None or path.suffix.lower() != ".mesh":
                continue
            if path in existing_by_mesh:
                break
            pretargets[(path, reference)] = reference.rsplit("/", 1)[0] + "/"
            break

    size_cache: dict[Path, int] = {}
    targets: dict[tuple[Path, str], str] = {}
    for key, directory in pretargets.items():
        path = key[0]
        if path not in size_cache:
            try:
                size_cache[path] = path.stat().st_size
            except OSError:
                size_cache[path] = 0
        if size_cache[path] > min_size:
            targets[key] = directory
    if not targets:
        return [], {}

    target_dirs = set(targets.values())
    encoded_dirs = {directory.encode("utf-8") for directory in target_dirs}
    texture_path_pattern = re.compile(
        rb'value="(model/[^"<>]+\.(?:tga|png|dds|jpg|jpeg|bmp|ktx))"'
    )

    def material_signature(
        materials: list[MaterialDefinition],
    ) -> tuple[object, ...]:
        return tuple(
            (
                material.name,
                tuple(sorted((slot, norm(value)) for slot, value in material.textures.items())),
                material.diffuse_color,
            )
            for material in materials
        )

    # kind=13 is important here: hot updates often orphan the target Mesh/GIM but retain
    # the MaterialGroup as an independent kind=13 resource.  kind=1 covers older packs.
    candidate_index: dict[
        tuple[str, int],
        dict[tuple[object, ...], tuple[Path, list[MaterialDefinition]]],
    ] = defaultdict(dict)
    # Keep material names for every directory-local group, including groups whose
    # slot count differs from the orphan target.  A one-slot group that is merely
    # a subset of a surviving multi-slot group is ambiguous: its parent GIM may be
    # describing the larger asset, not the orphan Mesh.
    directory_material_names: dict[str, list[frozenset[str]]] = defaultdict(list)
    candidate_xml_paths = {
        by_md5[record.content_md5]
        for record in records
        if record.kind in {1, 13}
        and record.content_md5 in by_md5
        and by_md5[record.content_md5].suffix.lower() == ".xml"
    }
    for xml_path in candidate_xml_paths:
        try:
            raw = xml_path.read_bytes().lower().replace(b"\\", b"/")
        except OSError:
            continue
        # 从 XML 图片 Value 直接抽目录，只扫描 raw 一次。旧实现对每个 XML
        # 逐一检查所有 target directory，复杂度 O(XML × orphan目录数)。
        raw_directories = {
            reference.rsplit(b"/", 1)[0] + b"/"
            for reference in texture_path_pattern.findall(raw)
            if b"/" in reference
        }
        if not raw_directories.intersection(encoded_dirs):
            continue
        materials = parse_material_xml(xml_path)
        if not materials:
            continue
        primary_refs = [material_primary_texture(item) for item in materials]
        if any(not reference for reference in primary_refs):
            continue
        directories = {
            norm(reference).rsplit("/", 1)[0] + "/"
            for reference in primary_refs
            if reference and "/" in norm(reference)
        }
        if len(directories) != 1:
            continue
        directory = next(iter(directories))
        if directory not in target_dirs:
            continue
        directory_material_names[directory].append(
            frozenset(
                re.sub(r"[^0-9a-z]+", "", item.name.lower())
                for item in materials
                if item.name
            )
        )
        candidate_index[(directory, len(materials))].setdefault(
            material_signature(materials),
            (xml_path, materials),
        )

    parsed_cache: dict[Path, ParsedMesh] = {}
    uv_cache: dict[Path, tuple[frozenset[tuple[object, ...]], ...]] = {}

    def uv_triangle_sets(path: Path) -> tuple[frozenset[tuple[object, ...]], ...]:
        cached = uv_cache.get(path)
        if cached is not None:
            return cached
        mesh = parsed_cache.get(path)
        if mesh is None:
            mesh = parse_mesh(path)
            parsed_cache[path] = mesh
        result: list[frozenset[tuple[object, ...]]] = []
        face_offset = 0
        for _, face_count, _, _ in mesh.submeshes:
            triangles: set[tuple[object, ...]] = set()
            for a, b, c in mesh.faces[face_offset : face_offset + face_count]:
                triangles.add(
                    tuple(
                        sorted(
                            tuple(round(value, 5) for value in mesh.uvs[index])
                            for index in (a, b, c)
                        )
                    )
                )
            result.append(frozenset(triangles))
            face_offset += face_count
        cached = tuple(result)
        uv_cache[path] = cached
        return cached

    resolver = CrossPackageTextureResolver(thd_dir, model_folder, by_md5)
    texture_cache: dict[str, Path | None] = {}

    def resolve_texture(reference: str) -> Path | None:
        key = norm(reference)
        if key in texture_cache:
            return texture_cache[key]
        resolved: Path | None = None
        for seed in seeds:
            record = record_by_hash.get(
                cloudfilesys_name_hash(reference, "model", seed)
            )
            path = by_md5.get(record.content_md5) if record is not None else None
            if path is not None and path.suffix.lower() == ".ktx":
                resolved = path
                break
        if resolved is None:
            candidate = resolver.resolve(reference)
            if candidate is not None and candidate.suffix.lower() == ".ktx":
                resolved = candidate
        texture_cache[key] = resolved
        return resolved

    proposals: dict[
        Path, dict[tuple[object, ...], MaterialPackage]
    ] = defaultdict(dict)
    submesh_count_cache: dict[Path, int | None] = {}

    for (target_path, logical_path), directory in sorted(
        targets.items(),
        key=lambda item: size_cache.get(item[0][0], 0),
        reverse=True,
    ):
        if target_path not in submesh_count_cache:
            try:
                submesh_count_cache[target_path] = read_mesh_submesh_count(target_path)
            except Exception:
                submesh_count_cache[target_path] = None
        submesh_count = submesh_count_cache[target_path]
        if not submesh_count:
            continue
        choices = candidate_index.get((directory, submesh_count), {})
        if len(choices) != 1:
            continue
        material_path, materials = next(iter(choices.values()))

        # Do not let a parent semantic rule select a smaller MaterialGroup when
        # the same target directory also contains a differently-sized group that
        # names one of the same slots.  This is the pattern behind the historical
        # huimingdeng/tuanjinji false positives.
        candidate_names = frozenset(
            re.sub(r"[^0-9a-z]+", "", item.name.lower())
            for item in materials
            if item.name
        )
        if any(
            names != candidate_names
            and names.intersection(candidate_names)
            for names in directory_material_names.get(directory, ())
        ):
            continue

        primary_refs = [material_primary_texture(item) or "" for item in materials]
        primary_map = {reference: resolve_texture(reference) for reference in primary_refs}
        if any(path is None for path in primary_map.values()):
            continue

        material_md5 = md5_by_path.get(material_path)
        if not material_md5:
            continue
        source_cases: list[tuple[Path, list[GimSubmesh]]] = []
        target_family = re.sub(
            r"[^0-9a-z]+", "", Path(directory.rstrip("/")).name.lower()
        )
        for material_record in records_by_md5.get(material_md5, []):
            for parent_hash in reverse_dependencies.get(material_record.name_hash, []):
                parent_record = record_by_hash.get(parent_hash)
                parent_path = (
                    by_md5.get(parent_record.content_md5)
                    if parent_record is not None
                    else None
                )
                gim_submeshes = (
                    parse_gim_submeshes(parent_path)
                    if parent_path is not None
                    and parent_path.suffix.lower() == ".xml"
                    else []
                )
                if len(gim_submeshes) != submesh_count:
                    continue
                parent_family = re.sub(
                    r"[^0-9a-z]+", "", extracted_resource_label(parent_path).lower()
                )
                # Parent GIM semantics are only useful within the same logical
                # asset family.  Permit a parent label that is a strict prefix or
                # suffix of the target (e.g. c3_yuzhuanjin -> yuzhuanjin), but do
                # not cross numbered variants or unrelated q/j weapon families.
                if not (
                    target_family in parent_family
                    or parent_family in target_family
                ):
                    continue
                for child_hash in dependencies.get(parent_hash, []):
                    child_record = record_by_hash.get(child_hash)
                    source_path = (
                        by_md5.get(child_record.content_md5)
                        if child_record is not None
                        else None
                    )
                    if source_path is None or source_path.suffix.lower() != ".mesh":
                        continue
                    if source_path == target_path:
                        continue
                    try:
                        if read_mesh_submesh_count(source_path) != submesh_count:
                            continue
                    except Exception:
                        continue
                    source_cases.append((source_path, gim_submeshes))
        if not source_cases:
            continue

        try:
            target_uv = uv_triangle_sets(target_path)
        except Exception:
            continue
        predictions: dict[tuple[object, ...], list[MaterialDefinition]] = {}
        for source_path, gim_submeshes in source_cases:
            try:
                source_uv = uv_triangle_sets(source_path)
            except Exception:
                continue
            mapping: list[int] = []
            used_source_submeshes: set[int] = set()
            valid = True
            for target_triangles in target_uv:
                scores = [
                    len(target_triangles & source_triangles)
                    / max(1, len(target_triangles))
                    for source_triangles in source_uv
                ]
                matches = [
                    index
                    for index, score in enumerate(scores)
                    if score >= uv_threshold and index not in used_source_submeshes
                ]
                if len(matches) != 1:
                    valid = False
                    break
                source_index = matches[0]
                used_source_submeshes.add(source_index)
                mapping.append(source_index)
            if not valid or len(mapping) != submesh_count:
                continue
            try:
                ordered = [
                    materials[gim_submeshes[source_index].material_index]
                    for source_index in mapping
                ]
            except IndexError:
                continue
            predictions.setdefault(_material_variant_signature(ordered), ordered)
        if len(predictions) != 1:
            continue

        ordered_materials = next(iter(predictions.values()))
        references = _ordered_texture_references(ordered_materials)
        texture_map: dict[str, Path] = {}
        for reference in references:
            path = resolve_texture(reference)
            if path is not None:
                texture_map[reference] = path
        main_references = {
            primary
            for material in ordered_materials
            if (primary := material_primary_texture(material))
        }
        if not main_references or not main_references <= texture_map.keys():
            continue

        confidence = (
            "orphan-path-uv-exact"
            if len(texture_map) == len(references)
            else "orphan-path-uv-exact-main-texture"
        )
        package = MaterialPackage(
            xml_path=material_path,
            index=archive_index(material_path) or 0,
            package_name=Path(logical_path).stem,
            materials=ordered_materials,
            mesh_paths=[target_path],
            texture_map=texture_map,
            confidence=confidence,
        )
        proposals[target_path].setdefault(
            _material_variant_signature(ordered_materials),
            package,
        )

    result: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    for mesh_path, choices in proposals.items():
        if len(choices) != 1:
            continue
        package = next(iter(choices.values()))
        result.append(package)
        by_mesh[mesh_path] = package
    return result, by_mesh


def discover_historical_model_indexes(
    current_thd_dir: Path,
    model_folder: Path,
) -> list[tuple[str, Path, Path]]:
    """Find retained official model.thx/model.thp snapshots.

    The updater keeps the preceding online index in ``_check_preload_``.  We
    also retain snapshots copied by this tool and the APK baseline.  Sources
    are content-deduplicated and the current index itself is excluded.
    """
    candidates: list[tuple[str, Path, Path]] = []
    cloud_root = current_thd_dir.parent
    for static_path in cloud_root.rglob("static.json"):
        try:
            fileinfo = json.loads(
                static_path.read_text(encoding="utf-8")
            )["fileinfo"]
            thx_digest = str(fileinfo["thd/model.thx"]["md5"]).lower()
            thp_digest = str(fileinfo["thd/model.thp"]["md5"]).lower()
        except (OSError, KeyError, TypeError, ValueError):
            continue
        cache_root = static_path.parent / "temp_cache"
        thx_path = cache_root / thx_digest
        thp_path = cache_root / thp_digest
        if thx_path.is_file() and thp_path.is_file():
            candidates.append((f"online-{thx_digest[:8]}", thx_path, thp_path))

    retained_root = model_folder.parent / "historical_model_indexes"
    if retained_root.is_dir():
        for folder in retained_root.iterdir():
            thx_path = folder / "model.thx"
            thp_path = folder / "model.thp"
            if thx_path.is_file() and thp_path.is_file():
                candidates.append((f"retained-{folder.name[:8]}", thx_path, thp_path))

    apk_root = model_folder.parent / "apk_model_parents" / "thd"
    if (apk_root / "model.thx").is_file() and (apk_root / "model.thp").is_file():
        candidates.append(("apk-baseline", apk_root / "model.thx", apk_root / "model.thp"))

    try:
        current_signature = (
            hashlib.md5((current_thd_dir / "model.thx").read_bytes()).hexdigest(),
            hashlib.md5((current_thd_dir / "model.thp").read_bytes()).hexdigest(),
        )
    except OSError:
        current_signature = ("", "")
    result: list[tuple[str, Path, Path]] = []
    seen = {current_signature}
    for label, thx_path, thp_path in candidates:
        try:
            signature = (
                hashlib.md5(thx_path.read_bytes()).hexdigest(),
                hashlib.md5(thp_path.read_bytes()).hexdigest(),
            )
        except OSError:
            continue
        if signature in seen:
            continue
        seen.add(signature)
        result.append((label, thx_path, thp_path))
    return result


def sync_historical_model_indexes(
    current_thd_dir: Path,
    model_folder: Path,
    log: Callable[[str], None] | None = None,
) -> list[tuple[str, Path, Path]]:
    """Persist current/preload official indexes so later updates cannot erase them."""
    source_pairs: list[tuple[str, Path, Path]] = []
    current_thx = current_thd_dir / "model.thx"
    current_thp = current_thd_dir / "model.thp"
    if current_thx.is_file() and current_thp.is_file():
        source_pairs.append(("current", current_thx, current_thp))
    source_pairs.extend(discover_historical_model_indexes(current_thd_dir, model_folder))

    output_root = model_folder.parent / "historical_model_indexes"
    saved = 0
    for label, thx_path, thp_path in source_pairs:
        try:
            thx_digest = hashlib.md5(thx_path.read_bytes()).hexdigest()
            thp_digest = hashlib.md5(thp_path.read_bytes()).hexdigest()
        except OSError:
            continue
        target = output_root / thx_digest
        target.mkdir(parents=True, exist_ok=True)
        target_thx = target / "model.thx"
        target_thp = target / "model.thp"
        if not target_thx.is_file():
            shutil.copy2(thx_path, target_thx)
            saved += 1
        if not target_thp.is_file():
            shutil.copy2(thp_path, target_thp)
        metadata = target / "source.json"
        if not metadata.is_file():
            metadata.write_text(
                json.dumps(
                    {
                        "source": label,
                        "thx_md5": thx_digest,
                        "thp_md5": thp_digest,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
    if log:
        log(f"历史模型索引：新增保存 {saved} 套，现可复用 {len(source_pairs)} 套。")
    return discover_historical_model_indexes(current_thd_dir, model_folder)


def _historical_content_path(
    digest: str,
    by_md5: dict[str, Path],
    prefix_paths: dict[str, list[Path]],
    verified: dict[tuple[str, Path], Path | None],
) -> Path | None:
    direct = by_md5.get(digest)
    if direct is not None and direct.is_file():
        return direct.resolve()
    for candidate in prefix_paths.get(digest[:16], []):
        key = (digest, candidate)
        if key not in verified:
            try:
                verified[key] = (
                    candidate.resolve()
                    if hashlib.md5(candidate.read_bytes()).hexdigest() == digest
                    else None
                )
            except OSError:
                verified[key] = None
        if verified[key] is not None:
            return verified[key]
    return None


def _build_historical_exact_parent_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    current_thd_dir: Path,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """Restore retained Meshes from exact official relationships in old THD indexes.

    This deliberately accepts only a complete structural proof: the historical
    parent is a real GIM, the exact Mesh content still exists, submesh counts
    agree, one texture-equivalent MaterialGroup fits every slot, and every main
    texture resolves through an official THX path hash.
    """
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thp,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    # Historical THX/THP frequently points at GIM/MaterialGroup/KTX files
    # retained only in the APK baseline.  The normal ``model`` manifest remains
    # authoritative for current Mesh bytes; fill only missing content hashes
    # from the APK cache so old parent chains can be replayed without replacing
    # current geometry.
    lookup_by_md5 = dict(by_md5)
    apk_manifest = model_folder.parent / "apk_model_parents" / "manifest.csv"
    if apk_manifest.is_file():
        try:
            with apk_manifest.open("r", newline="", encoding="utf-8-sig") as stream:
                for row in csv.DictReader(stream):
                    digest = (row.get("resource_hash") or "").strip().lower()
                    output = (row.get("output_path") or "").strip()
                    if len(digest) != 32 or not output or row.get("status") != "ok":
                        continue
                    candidate = model_folder.parent / Path(output.replace("\\", "/"))
                    if candidate.is_file() and digest not in lookup_by_md5:
                        lookup_by_md5[digest] = candidate.resolve()
        except (OSError, UnicodeError, csv.Error):
            pass

    sources = discover_historical_model_indexes(current_thd_dir, model_folder)
    if not sources:
        return [], {}
    prefix_paths: dict[str, list[Path]] = defaultdict(list)
    digest_suffix = re.compile(r"_([0-9a-fA-F]{16})(?:\.[^.]+)$")
    # Index content-addressed cache filenames by their 16-hex prefix.  Most
    # extracted resources use ``<logical>_<md5-prefix>.<ext>`` names, and some
    # older extraction runs omitted manifest rows even though the bytes remain
    # on disk.  Including XML/KTX/Skeleton entries lets historical THP chains
    # recover those orphaned material resources while the helper still verifies
    # the full MD5 before accepting a candidate.
    mesh_roots = [
        model_folder,
        model_folder.parent / "loose_model",
        model_folder.parent / "hot_update_model",
        model_folder.parent / "extra_rigged",
    ]
    for mesh_root in mesh_roots:
        if not mesh_root.is_dir():
            continue
        for path in mesh_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {
                ".mesh", ".xml", ".ktx", ".skeleton"
            }:
                continue
            match = digest_suffix.search(path.name)
            if match:
                prefix_paths[match.group(1).lower()].append(path)
            elif len(path.stem) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", path.stem):
                prefix_paths[path.stem[:16].lower()].append(path)

    verified: dict[tuple[str, Path], Path | None] = {}
    resolver = CrossPackageTextureResolver(current_thd_dir, model_folder, by_md5)
    try:
        current_content = {
            record.content_md5
            for record in read_model_thx(current_thd_dir / "model.thx")
        }
    except Exception:
        current_content = set()
    mesh_count_cache: dict[Path, int | None] = {}
    material_cache: dict[Path, list[MaterialDefinition]] = {}
    candidates: dict[Path, list[MaterialPackage]] = defaultdict(list)

    for label, thx_path, thp_path in sources:
        try:
            records = read_model_thx(thx_path)
            dependencies = read_model_thp(thp_path)
            seeds = read_thx_namehash_seeds(thx_path)
        except Exception:
            continue
        by_hash = {record.name_hash: record for record in records}
        inverse: dict[int, set[int]] = defaultdict(set)
        for parent_hash, dependency_hashes in dependencies.items():
            for dependency_hash in dependency_hashes:
                inverse[dependency_hash].add(parent_hash)
        target_parents: set[int] = set()
        for record in records:
            digest = record.content_md5
            # A Mesh may still exist in the current THX while its current THP
            # parent was pruned.  Such records are exactly the historical
            # recovery targets; only skip records whose physical bytes cannot
            # be located in the local caches.
            if digest[:16] not in prefix_paths:
                continue
            candidate = _historical_content_path(
                digest, lookup_by_md5, prefix_paths, verified
            )
            if (
                candidate is not None
                and candidate.suffix.lower() == ".mesh"
                and candidate.resolve() not in existing_by_mesh
            ):
                target_parents.update(inverse.get(record.name_hash, ()))

        for parent_hash in target_parents:
            dependency_hashes = dependencies.get(parent_hash, [])
            parent_record = by_hash.get(parent_hash)
            if parent_record is None:
                continue
            parent_path = _historical_content_path(
                parent_record.content_md5, lookup_by_md5, prefix_paths, verified
            )
            gim_submeshes = (
                parse_gim_submeshes(parent_path)
                if parent_path is not None
                and parent_path.suffix.lower() == ".xml"
                else []
            )
            dependency_paths: list[Path] = []
            dependency_records: list[object] = []
            for dependency_hash in dependency_hashes:
                record = by_hash.get(dependency_hash)
                if record is None:
                    continue
                path = _historical_content_path(
                    record.content_md5, lookup_by_md5, prefix_paths, verified
                )
                if path is not None:
                    dependency_paths.append(path)
                    dependency_records.append(record)

            mesh_positions = [
                index
                for index, path in enumerate(dependency_paths)
                if path.suffix.lower() == ".mesh"
            ]
            for mesh_number, start in enumerate(mesh_positions):
                mesh_path = dependency_paths[start].resolve()
                if mesh_path in existing_by_mesh:
                    continue
                if mesh_path not in mesh_count_cache:
                    try:
                        mesh_count_cache[mesh_path] = read_mesh_submesh_count(mesh_path)
                    except Exception:
                        mesh_count_cache[mesh_path] = None
                expected_count = mesh_count_cache[mesh_path]
                if not expected_count:
                    continue
                if gim_submeshes and expected_count != len(gim_submeshes):
                    continue
                next_mesh = (
                    mesh_positions[mesh_number + 1]
                    if mesh_number + 1 < len(mesh_positions)
                    else len(dependency_paths)
                )
                end = next(
                    (
                        index
                        for index in range(start + 1, next_mesh)
                        if dependency_paths[index].suffix.lower() == ".skeleton"
                        or parse_gim_submeshes(dependency_paths[index])
                    ),
                    next_mesh,
                )
                choices: dict[
                    tuple[tuple[tuple[str, str], ...], ...],
                    tuple[Path, list[MaterialDefinition]],
                ] = {}
                for path in dependency_paths[start + 1 : end]:
                    if path.suffix.lower() != ".xml":
                        continue
                    if path not in material_cache:
                        material_cache[path] = parse_material_xml(path)
                    materials = material_cache[path]
                    ordered = (
                        order_materials_by_gim(materials, gim_submeshes)
                        if gim_submeshes
                        else materials
                    )
                    if len(ordered) != expected_count:
                        continue
                    signature = tuple(
                        tuple(sorted(material.textures.items()))
                        for material in ordered
                    )
                    choices.setdefault(signature, (path, ordered))
                if len(choices) != 1:
                    continue
                material_path, ordered_materials = next(iter(choices.values()))
                references = _ordered_texture_references(ordered_materials)
                if not references:
                    continue
                texture_map: dict[str, Path] = {}
                for reference in references:
                    texture_path: Path | None = None
                    for seed in seeds:
                        record = by_hash.get(
                            cloudfilesys_name_hash(reference, "model", seed)
                        )
                        if record is None:
                            continue
                        candidate = _historical_content_path(
                            record.content_md5, lookup_by_md5, prefix_paths, verified
                        )
                        if candidate is not None and candidate.suffix.lower() == ".ktx":
                            texture_path = candidate
                            break
                    if texture_path is None:
                        texture_path = resolver.resolve(reference)
                    if texture_path is not None and texture_path.suffix.lower() == ".ktx":
                        texture_map[reference] = texture_path
                main_references = {
                    primary
                    for material in ordered_materials
                    if (primary := material_primary_texture(material))
                }
                if not main_references or not main_references.issubset(texture_map):
                    continue
                if gim_submeshes:
                    confidence = (
                        "historical-thd-exact"
                        if len(texture_map) == len(references)
                        else "historical-thd-exact-main-texture"
                    )
                else:
                    confidence = (
                        "historical-thd-segment-exact"
                        if len(texture_map) == len(references)
                        else "historical-thd-segment-exact-main-texture"
                    )
                candidates[mesh_path].append(
                    MaterialPackage(
                        xml_path=material_path,
                        index=archive_index(material_path) or 0,
                        package_name=f"history-{label}",
                        materials=ordered_materials,
                        mesh_paths=[mesh_path],
                        texture_map=texture_map,
                        confidence=confidence,
                    )
                )

    packages: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    for mesh_path, values in candidates.items():
        signatures: dict[tuple[object, ...], MaterialPackage] = {}
        for package in values:
            signature = _material_variant_signature(package.materials)
            old = signatures.get(signature)
            if old is None or len(package.texture_map) > len(old.texture_map):
                signatures[signature] = package
        representatives = list(signatures.values())
        packages.extend(representatives)
        if len(representatives) == 1:
            by_mesh[mesh_path] = representatives[0]
    return packages, by_mesh


def build_material_packages(
    model_folder: Path,
    progress: Callable[[int, int], None] | None = None,
    thd_dir: Path | None = None,
    stage_progress: Callable[[str, int, int], None] | None = None,
) -> tuple[
    list[MaterialPackage],
    dict[Path, MaterialPackage],
    dict[Path, list[MaterialPackage]],
]:
    """恢复默认材质映射，并额外保留同一物理 Mesh 的多套逻辑材质变体。

    APK 逆向确认资源键为：规范化路径去掉 model/ 包名前缀后，
    使用 THX 头内种子计算 XXH64。这样每个材质引用都可直接查回
    精确 KTX，不再依赖同段纹理顺序；多子网格、多材质及共享纹理
    均按原始声明保留。
    """
    stage_names = (
        "读取 THD 索引",
        "分析 THD 精确依赖",
        "补全历史索引关系",
        "补全 APK 基础材质",
        "匹配 GIM 明文路径",
        "匹配 script3 路径",
        "匹配 res 资源路径",
        "匹配 GIM 别名",
        "匹配派生 GIM",
        "匹配透明模型",
        "匹配庭院模型",
        "匹配编号合并模型",
        "匹配 GIM 子集",
        "匹配嵌套 GIM",
        "匹配单材质直连纹理",
        "匹配同目录唯一纹理",
        "匹配逻辑资源族",
        "匹配额外资源族",
        "匹配骨架路径",
        "匹配孤立模型 UV",
        "匹配主角模式共识",
        "匹配同族渲染目标",
        "整理逻辑材质变体",
        "整理 THP 父节点变体",
        "建立最终材质索引",
    )
    stage_number = 0

    def begin_stage(label: str) -> None:
        nonlocal stage_number
        stage_number += 1
        if stage_progress:
            stage_progress(label, stage_number, len(stage_names))

    begin_stage(stage_names[0])
    model_folder = model_folder.resolve()
    by_md5, md5_by_path = _manifest_hash_maps(model_folder)
    fallback_packages, fallback_by_mesh = _build_manual_verified_packages(
        model_folder, by_md5
    )
    if thd_dir is None:
        fallback_variants = {
            path: [package] for path, package in fallback_by_mesh.items()
        }
        return fallback_packages, fallback_by_mesh, fallback_variants

    thx_path = thd_dir / "model.thx"
    thp_path = thd_dir / "model.thp"
    if not thx_path.is_file() or not thp_path.is_file():
        fallback_variants = {
            path: [package] for path, package in fallback_by_mesh.items()
        }
        return fallback_packages, fallback_by_mesh, fallback_variants

    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thp,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    thx_records = read_model_thx(thx_path)
    dependencies = read_model_thp(thp_path)
    namehash_seeds = read_thx_namehash_seeds(thx_path)
    cross_texture_resolver = CrossPackageTextureResolver(
        thd_dir, model_folder, by_md5
    )
    record_by_name_hash = {item.name_hash: item for item in thx_records}
    records_by_md5: dict[str, list[object]] = {}
    for item in thx_records:
        records_by_md5.setdefault(item.content_md5, []).append(item)

    # 只检查 THP 的父资源，而不是遍历并解析全部 XML。
    gim_submeshes_by_path: dict[Path, list[GimSubmesh]] = {}
    for parent_hash in dependencies:
        record = record_by_name_hash.get(parent_hash)
        if record is None:
            continue
        path = by_md5.get(record.content_md5)
        if path is None or path.suffix.lower() != ".xml":
            continue
        submeshes = parse_gim_submeshes(path)
        if submeshes:
            gim_submeshes_by_path[path] = submeshes

    gim_paths = list(gim_submeshes_by_path)
    gim_paths_by_label: dict[str, list[Path]] = {}
    for path in gim_paths:
        label = extracted_resource_label(path).strip().lower()
        if label:
            gim_paths_by_label.setdefault(label, []).append(path)
    # 老版本索引若缺少 namehash 种子，才退回路径自举。当前客户端
    # 的 model.thx 自带 0xA3、0x25 两颗种子，正常不会走此分支。
    texture_hash_by_reference = (
        {}
        if namehash_seeds
        else _learn_thd_texture_hashes(
            by_md5,
            record_by_name_hash,
            dependencies,
            gim_submeshes_by_path,
        )
    )
    texture_record_cache: dict[str, object | None] = {}
    material_cache: dict[Path, list[MaterialDefinition]] = {}
    # kind=1 中存在一小批被多个 GIM 复用、却不再写进各自 THP 的共享
    # MaterialGroup。只预解析这些资源，避免为了找共享材质扫描十几万 XML。
    shared_material_groups: list[tuple[Path, list[MaterialDefinition]]] = []
    for record in thx_records:
        if record.kind != 1:
            continue
        path = by_md5.get(record.content_md5)
        if path is None or path.suffix.lower() != ".xml":
            continue
        materials = parse_material_xml(path)
        if materials:
            material_cache[path] = materials
            shared_material_groups.append((path, materials))
    mesh_submesh_count_cache: dict[Path, int | None] = {}
    packages: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    total = len(gim_paths)

    begin_stage(stage_names[1])
    for number, gim_path in enumerate(gim_paths, 1):
        gim_digest = md5_by_path.get(gim_path)
        parent = next(
            (
                item for item in records_by_md5.get(gim_digest or "", [])
                if item.name_hash in dependencies
            ),
            None,
        )
        if parent is None:
            if progress and (number % 100 == 0 or number == total):
                progress(number, total)
            continue

        dependency_paths: list[Path] = []
        for name_hash in dependencies[parent.name_hash]:
            record = record_by_name_hash.get(name_hash)
            if record is None:
                continue
            path = by_md5.get(record.content_md5)
            if path is not None and path not in dependency_paths:
                dependency_paths.append(path)

        # THP 中资源按“Mesh -> 材质 XML -> 该材质的 KTX”成段排列。
        # 一个 GIM 可以包含多个这样的段，不能把整条依赖里的所有 KTX
        # 混到每个 Mesh 上。
        mesh_positions = [
            index
            for index, path in enumerate(dependency_paths)
            if path.suffix.lower() == ".mesh"
        ]
        gim_submeshes = gim_submeshes_by_path[gim_path]
        for mesh_number, start in enumerate(mesh_positions):
            next_mesh = (
                mesh_positions[mesh_number + 1]
                if mesh_number + 1 < len(mesh_positions)
                else len(dependency_paths)
            )
            # 实际 THP 常见 Mesh -> MaterialGroup -> Skeleton -> 其他附件。
            # Skeleton 是当前 Mesh 材质段的可靠结束标志；若只等到下一个 Mesh，
            # 会把后续附件的 MaterialGroup 也混进来，误判成“材质不唯一”。
            next_structure = next(
                (
                    index
                    for index in range(start + 1, next_mesh)
                    if (
                        dependency_paths[index].suffix.lower() == ".skeleton"
                        or dependency_paths[index] in gim_submeshes_by_path
                    )
                ),
                next_mesh,
            )
            # 子 GIM 与 Skeleton 都是当前 Mesh 材质段的结构边界。
            # THP 常见布局为 Mesh -> 主体 MaterialGroup -> 子 GIM -> 附件材质；
            # 若不在子 GIM 处截断，会把附件材质误当主体候选并制造假歧义。
            end = min(next_mesh, next_structure)
            mesh_path = dependency_paths[start]
            segment = dependency_paths[start + 1 : end]
            texture_paths = [
                path for path in segment if path.suffix.lower() in IMAGE_SUFFIXES
            ]
            material_choices: list[
                tuple[Path, list[MaterialDefinition], list[str]]
            ] = []
            partial_material_choices: list[
                tuple[Path, list[MaterialDefinition], list[str], int]
            ] = []
            for path in segment:
                if path.suffix.lower() != ".xml":
                    continue
                if path not in material_cache:
                    material_cache[path] = parse_material_xml(path)
                materials = material_cache[path]
                if not materials:
                    continue
                ordered = order_materials_by_gim(materials, gim_submeshes)
                # THP 的 KTX 顺序跟随 MaterialGroup 原始声明顺序；
                # MtlIdx 只用于稍后排列 PMX 子网格材质。若先按 MtlIdx
                # 重排纹理路径，会把头、身体等贴图互换。
                references = _ordered_texture_references(materials)
                if ordered and references:
                    material_choices.append((path, ordered, references))
                elif references:
                    partial_ordered, valid_count = order_materials_by_gim_partial(
                        materials, gim_submeshes
                    )
                    # 只有至少一半子网格的 MtlIdx 仍落在官方 MaterialGroup
                    # 范围内，才保留“部分材质”。低覆盖率通常表示这个 XML
                    # 属于同父 GIM 的其他附件，不能拿来给主体强行贴图。
                    if valid_count * 2 >= len(gim_submeshes):
                        partial_material_choices.append(
                            (path, partial_ordered, references, valid_count)
                        )

            if mesh_path not in mesh_submesh_count_cache:
                try:
                    mesh_submesh_count_cache[mesh_path] = read_mesh_submesh_count(
                        mesh_path
                    )
                except Exception:
                    mesh_submesh_count_cache[mesh_path] = None
            expected_submeshes = mesh_submesh_count_cache[mesh_path]

            # 优先使用当前 Mesh 段内唯一且子网格数匹配的 MaterialGroup。
            used_shared_material = False
            used_partial_material = False
            used_texture_equivalent_material = False
            valid_choices = [
                choice
                for choice in material_choices
                if expected_submeshes == len(choice[1])
            ]
            # 同一官方 THP 段里有时会同时保留“正式材质名”和 TempMaterial，
            # 名字不同但逐子网格的全部纹理槽完全相同。对贴图恢复而言这不构成
            # 歧义；仅在所有候选的完整纹理字典逐槽一致时合并，并用 GIM 的
            # 子网格名作为稳定材质名。只要任一纹理槽不同，仍保持不绑定。
            if len(valid_choices) > 1:
                texture_signatures = {
                    tuple(
                        tuple(sorted(material.textures.items()))
                        for material in ordered
                    )
                    for _, ordered, _ in valid_choices
                }
                if len(texture_signatures) == 1:
                    first_path, first_ordered, _ = valid_choices[0]
                    equivalent_ordered = [
                        MaterialDefinition(
                            gim_submeshes[index].name.lstrip("@"),
                            dict(material.textures),
                        )
                        for index, material in enumerate(first_ordered)
                    ]
                    valid_choices = [
                        (
                            first_path,
                            equivalent_ordered,
                            _ordered_texture_references(equivalent_ordered),
                        )
                    ]
                    used_texture_equivalent_material = True
            # 部分材质只作为最后兜底。只要同一父 GIM / 同族 / 共享池里还存在
            # 任意完整 MaterialGroup，就不能因为“部分候选唯一”而抢先绑定。
            eligible_partial = [
                choice
                for choice in partial_material_choices
                if expected_submeshes == len(choice[1])
            ]

            if len(valid_choices) != 1:
                # 有些 GIM 的多个 Mesh 共享父组 MaterialGroup，目标 Mesh 自己
                # 后面只跟 Transform/Skeleton。此时允许在同一官方 THP 父组内
                # 反选，但必须满足 MtlIdx 全合法、子网格数一致且候选唯一。
                parent_choices: list[
                    tuple[Path, list[MaterialDefinition], list[str]]
                ] = []
                seen_material_paths: set[Path] = set()
                for path in dependency_paths:
                    if path.suffix.lower() != ".xml" or path in seen_material_paths:
                        continue
                    seen_material_paths.add(path)
                    if path not in material_cache:
                        material_cache[path] = parse_material_xml(path)
                    materials = material_cache[path]
                    if not materials:
                        continue
                    ordered = order_materials_by_gim(materials, gim_submeshes)
                    references = _ordered_texture_references(materials)
                    if (
                        ordered
                        and references
                        and expected_submeshes == len(ordered)
                    ):
                        parent_choices.append((path, ordered, references))
                if len(parent_choices) > 1:
                    parent_texture_signatures = {
                        tuple(
                            tuple(sorted(material.textures.items()))
                            for material in ordered
                        )
                        for _, ordered, _ in parent_choices
                    }
                    if len(parent_texture_signatures) == 1:
                        first_path, first_ordered, _ = parent_choices[0]
                        equivalent_ordered = [
                            MaterialDefinition(
                                gim_submeshes[index].name.lstrip("@"),
                                dict(material.textures),
                            )
                            for index, material in enumerate(first_ordered)
                        ]
                        valid_choices = [
                            (
                                first_path,
                                equivalent_ordered,
                                _ordered_texture_references(equivalent_ordered),
                            )
                        ]
                        used_texture_equivalent_material = True
                if len(parent_choices) == 1:
                    valid_choices = parent_choices
                elif not valid_choices:
                    # 同一角色可能有多个同名 GIM/LOD：高细节 GIM 携带完整
                    # MaterialGroup，另一份 GIM 只保留 Mesh/Transform。只在
                    # GIM 语义名完全一致、目标 MtlIdx 可完整索引、候选唯一时
                    # 跨父节点共享材质，仍然不使用 WPK 条目邻近关系猜测。
                    family_choices: list[
                        tuple[Path, list[MaterialDefinition], list[str]]
                    ] = []
                    family_seen: set[Path] = set()
                    family_label = extracted_resource_label(gim_path).strip().lower()
                    for family_gim in gim_paths_by_label.get(family_label, []):
                        family_digest = md5_by_path.get(family_gim)
                        family_parent = next(
                            (
                                item
                                for item in records_by_md5.get(family_digest or "", [])
                                if item.name_hash in dependencies
                            ),
                            None,
                        )
                        if family_parent is None:
                            continue
                        for family_hash in dependencies[family_parent.name_hash]:
                            family_record = record_by_name_hash.get(family_hash)
                            family_path = (
                                by_md5.get(family_record.content_md5)
                                if family_record is not None else None
                            )
                            if (
                                family_path is None
                                or family_path.suffix.lower() != ".xml"
                                or family_path in family_seen
                            ):
                                continue
                            family_seen.add(family_path)
                            if family_path not in material_cache:
                                material_cache[family_path] = parse_material_xml(
                                    family_path
                                )
                            materials = material_cache[family_path]
                            if not materials:
                                continue
                            ordered = order_materials_by_gim(
                                materials, gim_submeshes
                            )
                            references = _ordered_texture_references(materials)
                            if (
                                ordered
                                and references
                                and expected_submeshes == len(ordered)
                            ):
                                family_choices.append(
                                    (family_path, ordered, references)
                                )
                    if len(family_choices) == 1:
                        valid_choices = family_choices
                    else:
                        # 少量轻量/剧情 GIM 不再在自己的 THP 中携带 MaterialGroup，
                        # 而是复用 kind=1 的全局材质资源。这里不做模糊名称匹配：
                        # 必须按 GIM 的 MtlIdx 重排后，每个子网格名与材质名逐项
                        # 完全一致；若不同 XML 给出不同材质签名，仍保持不绑定。
                        target_names = tuple(
                            _normalized_material_name(item.name)
                            for item in gim_submeshes
                        )
                        shared_choices_by_signature: dict[
                            tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
                            tuple[Path, list[MaterialDefinition], list[str]],
                        ] = {}
                        for shared_path, shared_materials in shared_material_groups:
                            ordered = order_materials_by_gim(
                                shared_materials, gim_submeshes
                            )
                            if (
                                len(ordered) != expected_submeshes
                                or tuple(
                                    _normalized_material_name(material.name)
                                    for material in ordered
                                ) != target_names
                            ):
                                continue
                            references = _ordered_texture_references(
                                shared_materials
                            )
                            if not references:
                                continue
                            signature = tuple(
                                (
                                    material.name,
                                    tuple(sorted(material.textures.items())),
                                )
                                for material in ordered
                            )
                            shared_choices_by_signature.setdefault(
                                signature,
                                (shared_path, ordered, references),
                            )
                        if len(shared_choices_by_signature) == 1:
                            valid_choices = [
                                next(iter(shared_choices_by_signature.values()))
                            ]
                            used_shared_material = True
                        elif (
                            not shared_choices_by_signature
                            and not parent_choices
                            and not family_choices
                            and len(eligible_partial) == 1
                        ):
                            # 到这里可以确认：完整材质候选数量为 0。此时才允许
                            # 保留当前 Mesh 段中唯一的高覆盖率部分 MaterialGroup；
                            # 越界 MtlIdx 仍然只生成空材质，不做任何复制/猜测。
                            path, ordered, references, _ = eligible_partial[0]
                            valid_choices = [(path, ordered, references)]
                            used_partial_material = True
                        else:
                            continue

            material_path, ordered_materials, references = valid_choices[0]

            texture_map: dict[str, Path] = {}
            used_path_bootstrap = False
            for reference in references:
                reference_key = reference.strip().replace("\\", "/").lower()
                if reference_key not in texture_record_cache:
                    exact_record = None
                    for seed in namehash_seeds:
                        name_hash = cloudfilesys_name_hash(
                            reference, "model", seed
                        )
                        candidate = record_by_name_hash.get(name_hash)
                        candidate_path = (
                            by_md5.get(candidate.content_md5)
                            if candidate is not None else None
                        )
                        if (
                            candidate_path is not None
                            and candidate_path.suffix.lower() in IMAGE_SUFFIXES
                        ):
                            exact_record = candidate
                            break
                    texture_record_cache[reference_key] = exact_record

                record = texture_record_cache[reference_key]
                if record is None:
                    name_hash = texture_hash_by_reference.get(reference_key)
                    record = (
                        record_by_name_hash.get(name_hash)
                        if name_hash is not None else None
                    )
                    used_path_bootstrap = used_path_bootstrap or record is not None
                texture_path = (
                    by_md5.get(record.content_md5)
                    if record is not None else None
                )
                if texture_path is None or texture_path.suffix.lower() not in IMAGE_SUFFIXES:
                    texture_path = cross_texture_resolver.resolve(reference)
                if texture_path is not None and texture_path.suffix.lower() in IMAGE_SUFFIXES:
                    texture_map[reference] = texture_path

            # PMX 显示只依赖每个材质的主颜色贴图。新材质常用 Tex0，
            # 旧 shader 也会使用 TexDiffuse 等命名；法线/遮罩/光泽缺失不影响主贴图。
            main_references = {
                primary
                for material in ordered_materials
                if (primary := material_primary_texture(material))
            }
            # GIM/THP 已经精确确认了 Mesh 与 MaterialGroup 的关系时，
            # 某个主贴图资源缺失不应把整个模型降成“无材质组”。阴阳师部分
            # 材质会把通用 shader 噪声/反射图放在 Tex0；这些资源可能位于
            # 当前客户端未落盘的包里，但人物主体的 diffuse 仍然完全可用。
            # 因此只要求至少一个 Tex0 被精确解析，其余槽位按缺失保留为空。
            if not main_references:
                continue
            resolved_main_references = {
                reference
                for reference in main_references
                if reference in texture_map
            }
            if not resolved_main_references:
                continue

            complete = len(texture_map) == len(references)
            complete_main = len(resolved_main_references) == len(main_references)
            if used_partial_material:
                if complete:
                    confidence = "THD精确部分材质"
                elif complete_main:
                    confidence = "THD精确部分材质主贴图"
                else:
                    confidence = "THD精确部分材质部分主贴图"
            elif used_shared_material:
                if complete:
                    confidence = "THD共享材质精确"
                elif complete_main:
                    confidence = "THD共享材质精确主贴图"
                else:
                    confidence = "THD共享材质精确部分主贴图"
            elif used_texture_equivalent_material:
                if complete:
                    confidence = "THD纹理等价精确"
                elif complete_main:
                    confidence = "THD纹理等价精确主贴图"
                else:
                    confidence = "THD纹理等价精确部分主贴图"
            elif used_path_bootstrap:
                if complete:
                    confidence = "THD路径自举"
                elif complete_main:
                    confidence = "THD路径自举主贴图"
                else:
                    confidence = "THD路径自举部分主贴图"
            else:
                if complete:
                    confidence = "THD精确"
                elif complete_main:
                    confidence = "THD精确主贴图"
                else:
                    confidence = "THD精确部分主贴图"
            package = MaterialPackage(
                xml_path=material_path,
                index=archive_index(material_path) or 0,
                package_name=extracted_resource_label(gim_path),
                materials=ordered_materials,
                mesh_paths=[mesh_path],
                texture_map=texture_map,
                confidence=confidence,
            )
            old_package = by_mesh.get(mesh_path)
            complete_confidence = {
                "THD精确",
                "THD路径自举",
                "THD纹理等价精确",
                "人工验证",
            }
            if (
                old_package is None
                or (
                    package.confidence in complete_confidence,
                    len(package.materials),
                    len(package.texture_map),
                ) > (
                    old_package.confidence in complete_confidence,
                    len(old_package.materials),
                    len(old_package.texture_map),
                )
            ):
                if old_package is not None and old_package in packages:
                    packages.remove(old_package)
                packages.append(package)
                by_mesh[mesh_path] = package

        if progress and (number % 100 == 0 or number == total):
            progress(number, total)

    # Current THP intentionally drops some old parents while their exact Mesh
    # files remain in the local WPK cache.  Reuse the preceding official online
    # indexes (and APK baseline) only when the full GIM/material/main-texture
    # chain is still structurally exact.
    begin_stage(stage_names[2])
    historical_packages, historical_by_mesh = (
        _build_historical_exact_parent_packages(
            model_folder,
            by_md5,
            by_mesh,
            thd_dir,
        )
    )
    for package in historical_packages:
        if any(path not in by_mesh for path in package.mesh_paths):
            packages.append(package)
    for mesh_path, package in historical_by_mesh.items():
        if mesh_path not in by_mesh:
            by_mesh[mesh_path] = package

    # THD 结果优先；人工条目只补齐未覆盖的已确认资源。
    for package in fallback_packages:
        for mesh_path in package.mesh_paths:
            if mesh_path not in by_mesh:
                packages.append(package)
                by_mesh[mesh_path] = package

    # 热更新后的 model.thp 会删掉一些仍留在当前 WPK 的基础角色依赖。
    # APK 自带的基础 THX/THP 是同一套官方资源关系，可安全补这些缺口。
    begin_stage(stage_names[3])
    apk_packages, apk_by_mesh = _build_cached_apk_material_packages(
        model_folder,
        by_md5,
        by_mesh,
        current_thd_dir=thd_dir,
    )
    for mesh_path, package in apk_by_mesh.items():
        if mesh_path not in by_mesh:
            packages.append(package)
            by_mesh[mesh_path] = package

    # THP 中的 Mesh 内容 MD5 可能滞后于热更新，但 GIM XML 常仍明文保存
    # Mesh="model/.../*.mesh"。用这条逻辑路径回查当前 THX，可恢复版本错位漏配。
    apk_thd_dir = model_folder.parent / "apk_model_parents" / "thd"
    begin_stage(stage_names[4])
    gim_path_packages, gim_path_by_mesh = _build_explicit_gim_path_packages(
        model_folder,
        by_md5,
        by_mesh,
        thd_dir,
        [
            (thd_dir, "GIM路径精确"),
            (apk_thd_dir, "APK-GIM路径精确"),
        ],
    )
    for mesh_path, package in gim_path_by_mesh.items():
        if mesh_path not in by_mesh:
            packages.append(package)
            by_mesh[mesh_path] = package

    # script3 内保存了客户端实际使用的大量 GIM 逻辑路径。它能补出
    # “THX 中有 Mesh，但 THP 已不再直接列出该 Mesh”的热更新/旧资源。
    begin_stage(stage_names[5])
    script_packages, script_by_mesh = _build_script3_path_packages(
        model_folder,
        by_md5,
        by_mesh,
        thd_dir,
        [
            (thd_dir, "script3路径精确"),
            (apk_thd_dir, "APK-script3路径精确"),
        ],
        shared_material_groups=shared_material_groups,
    )
    for mesh_path, package in script_by_mesh.items():
        if mesh_path not in by_mesh:
            packages.append(package)
            by_mesh[mesh_path] = package

    # res 的资源清单比 script3 更完整，包含大量 kind=1 派生 Mesh 的明文路径。
    # 这里仍只启用最保守的“同 stem GIM 实体 + 官方 THP 唯一材质组”规则；
    # tingyuan/touming/shihua/jq 等缺失 GIM 的派生格式由后续专门规则处理。
    begin_stage(stage_names[6])
    res_asset_paths = load_res_asset_paths(thd_dir, model_folder)
    res_gim_references = sorted({
        reference[:-5] + ".gim"
        for reference in res_asset_paths
        if reference.startswith("model/") and reference.endswith(".mesh")
    })
    res_packages, res_by_mesh = _build_script3_path_packages(
        model_folder,
        by_md5,
        {},
        thd_dir,
        [
            (thd_dir, "res路径精确"),
            (apk_thd_dir, "APK-res路径精确"),
        ],
        gim_references=res_gim_references,
        preserve_variants=True,
        shared_material_groups=shared_material_groups,
    )
    for mesh_path, package in res_by_mesh.items():
        if mesh_path not in by_mesh:
            packages.append(package)
            by_mesh[mesh_path] = package

    # kind=15 别名 GIM 常只保留 Mesh/子网格语义，真正 MaterialGroup 会挂在
    # 同目录的另一条官方 GIM 关系上。这里直接复用已加载的 res 清单，避免
    # 为这一层规则再次读取大型资源路径表。
    begin_stage(stage_names[7])
    alias_packages, alias_by_mesh = _build_gim_alias_name_material_packages(
        model_folder,
        by_md5,
        by_mesh,
        thd_dir,
        material_cache=material_cache,
        asset_paths=res_asset_paths,
    )
    for mesh_path, package in alias_by_mesh.items():
        if mesh_path not in by_mesh:
            packages.append(package)
            by_mesh[mesh_path] = package

    # 少量 kind=1 派生 GIM 自身实体已裁掉，但 THP 仍直接保存
    # “子 GIM + 同子网格数源 Mesh + MaterialGroup”。这种代理依赖关系
    # 仍是官方结构证据，可用于恢复派生高模的材质布局。
    begin_stage(stage_names[8])
    proxy_packages, proxy_by_mesh = _build_res_proxy_gim_packages(
        model_folder,
        by_md5,
        by_mesh,
        thd_dir,
    )
    for mesh_path, package in proxy_by_mesh.items():
        if mesh_path not in by_mesh:
            packages.append(package)
            by_mesh[mesh_path] = package

    # `_show_touming` 不能按名字直接继承普通 Show。只有目标目录没有专属纹理、
    # 去掉 `_touming` 后的原版 Show 路径能被 THX 精确解析，且表面几何完全一致
    # 时才复用原版材质；蒙皮差异不影响 UV/子网格材质绑定。
    begin_stage(stage_names[9])
    touming_packages, touming_by_mesh = _build_res_touming_packages(
        model_folder,
        by_md5,
        by_mesh,
        thd_dir,
        apk_thd_dir,
    )
    for mesh_path, package in touming_by_mesh.items():
        if mesh_path not in by_mesh:
            packages.append(package)
            by_mesh[mesh_path] = package

    # `_tingyuan` 只接受 Show 父 GIM 直接依赖 Mesh 的逐子网格无损合并。
    # 每个目标子网格都必须唯一落到一个已有强证据源材质，否则整个目标跳过。
    begin_stage(stage_names[10])
    tingyuan_packages, tingyuan_by_mesh = _build_res_tingyuan_packages(
        model_folder,
        by_md5,
        by_mesh,
        thd_dir,
    )
    for mesh_path, package in tingyuan_by_mesh.items():
        if mesh_path not in by_mesh:
            packages.append(package)
            by_mesh[mesh_path] = package

    # 某些派生资源把 foo1/foo2/foo3 三个单子网格 Mesh 合成 foo.mesh，
    # 但 foo.gim 的 XML 已裁掉。只有编号源 Mesh 的表面指纹完整覆盖目标、
    # 源材质签名完全一致，且目标 GIM 自身 THP 仍精确依赖该主贴图时才绑定。
    begin_stage(stage_names[11])
    numbered_packages, numbered_by_mesh = _build_res_numbered_merge_packages(
        model_folder,
        by_md5,
        by_mesh,
        thd_dir,
    )
    for mesh_path, package in numbered_by_mesh.items():
        if mesh_path not in by_mesh:
            packages.append(package)
            by_mesh[mesh_path] = package

    # 某些 `_01` 派生 GIM 只是标准父 GIM 的前缀子集，且自己的目录没有
    # 任何专属纹理。只有子网格名+MtlIdx 逐项等于父 GIM 前缀时才继承父材质。
    begin_stage(stage_names[12])
    subset_packages, subset_by_mesh = _build_res_gim_subset_packages(
        model_folder,
        by_md5,
        by_mesh,
        thd_dir,
    )
    for mesh_path, package in subset_by_mesh.items():
        if mesh_path not in by_mesh:
            packages.append(package)
            by_mesh[mesh_path] = package

    # 对仍未绑定的大 Mesh，再尝试“唯一 Skeleton 名 → 同名逻辑路径”。
    # Skeleton 只用于生成候选路径，最终仍要求当前 THX 的 Mesh/GIM 哈希闭环。
    begin_stage(stage_names[13])
    nested_packages, nested_by_mesh = _build_nested_gim_material_packages(
        model_folder,
        by_md5,
        by_mesh,
        thd_dir,
    )
    for mesh_path, package in nested_by_mesh.items():
        if mesh_path not in by_mesh:
            packages.append(package)
            by_mesh[mesh_path] = package

    begin_stage(stage_names[14])
    direct_packages, direct_by_mesh = _build_single_submesh_direct_texture_packages(
        model_folder,
        by_md5,
        by_mesh,
        thd_dir,
    )
    for mesh_path, package in direct_by_mesh.items():
        if mesh_path not in by_mesh:
            packages.append(package)
            by_mesh[mesh_path] = package

    # Generic single-slot fallback: exact current logical Mesh identity plus one
    # same-stem, non-auxiliary image in the same logical directory. This replaces
    # per-character MD5 entries and remains valid when future resources are added.
    begin_stage(stage_names[15])
    unique_image_packages, unique_image_by_mesh = (
        _build_unique_logical_single_image_packages(
            model_folder,
            by_md5,
            by_mesh,
            thd_dir,
            asset_paths=res_asset_paths,
        )
    )
    for mesh_path, package in unique_image_by_mesh.items():
        if mesh_path not in by_mesh:
            packages.append(package)
            by_mesh[mesh_path] = package

    begin_stage(stage_names[16])
    all_asset_paths = list(dict.fromkeys(
        res_asset_paths
        + load_script3_gim_paths(thd_dir, model_folder)
        + load_fx_asset_paths(thd_dir, model_folder)
    ))
    family_gim_packages, family_gim_by_mesh = (
        _build_logical_family_gim_material_packages(
            model_folder,
            by_md5,
            by_mesh,
            thd_dir,
            all_asset_paths,
        )
    )
    packages.extend(family_gim_packages)
    for mesh_path, package in family_gim_by_mesh.items():
        if mesh_path not in by_mesh:
            by_mesh[mesh_path] = package

    begin_stage(stage_names[17])
    supplemental_packages, supplemental_by_mesh = (
        _build_supplemental_logical_material_packages(
            model_folder,
            by_md5,
            by_mesh,
            thd_dir,
            all_asset_paths,
        )
    )
    for mesh_path, package in supplemental_by_mesh.items():
        if mesh_path not in by_mesh:
            packages.append(package)
            by_mesh[mesh_path] = package

    begin_stage(stage_names[18])
    skeleton_packages, skeleton_by_mesh = _build_skeleton_path_packages(
        model_folder,
        by_md5,
        by_mesh,
        thd_dir,
        shared_material_groups,
    )
    for mesh_path, package in skeleton_by_mesh.items():
        if mesh_path not in by_mesh:
            packages.append(package)
            by_mesh[mesh_path] = package

    # 热更新可能保留精确 Mesh logical path 和独立 MaterialGroup，却裁掉目标
    # GIM/THP parent。只对 >100KB 的重要 orphan Mesh 启用强结构兜底：
    # 目标目录唯一材质组 + 官方源 GIM MtlIdx + >=90% UV 三角形一一同源。
    # UV 只恢复材质索引顺序，贴图始终来自目标 logical directory。
    begin_stage(stage_names[19])
    orphan_packages, orphan_by_mesh = _build_orphan_path_uv_material_packages(
        model_folder,
        by_md5,
        by_mesh,
        thd_dir,
    )
    for mesh_path, package in orphan_by_mesh.items():
        if mesh_path not in by_mesh:
            packages.append(package)
            by_mesh[mesh_path] = package

    # 主角 base/bat/tansuo/tingyuan 是稳定的业务模式资源族，但不同模式并非
    # 永远共用贴图。只在目标模式缺失官方材质、且至少两个其它官方模式通过
    # >=90% UV 一一映射后对完整材质定义达成一致时，才把共识材质恢复给目标。
    begin_stage(stage_names[20])
    zhujue_mode_packages, zhujue_mode_by_mesh = (
        _build_zhujue_mode_consensus_packages(
            model_folder,
            by_md5,
            by_mesh,
            thd_dir,
        )
    )
    for mesh_path, package in zhujue_mode_by_mesh.items():
        if mesh_path not in by_mesh:
            packages.append(package)
            by_mesh[mesh_path] = package

    # 完全相同几何本身不能继承别的皮肤材质；但若同几何已绑定源材质的
    # 所有主贴图路径都明确落在目标 logical directory（允许 fx/model 前缀），
    # 则源材质实际上已经在引用目标资源族，可安全恢复被裁掉的 sibling GIM。
    begin_stage(stage_names[21])
    family_dup_packages, family_dup_by_mesh = (
        _build_exact_render_target_family_packages(
            model_folder,
            by_md5,
            by_mesh,
            thd_dir,
        )
    )
    for mesh_path, package in family_dup_by_mesh.items():
        if mesh_path not in by_mesh:
            packages.append(package)
            by_mesh[mesh_path] = package

    # “全局语义材质”只保留为研究/诊断函数，不再自动绑定：名称 80% 相似
    # 与贴图目录相似仍属于语义推断，且需要扫描数千个 kind=13 XML，既慢又
    # 弱于 THP/路径/共享材质等结构证据。

    # 注意：即使位置/UV/面/蒙皮完全一致，也可能是不同皮肤共享同一几何。
    # 因此“渲染几何精确复用”只保留为研究/诊断函数，不再自动绑定材质。

    # 精确路径和 GIM 都存在、但同目录有多套官方 MaterialGroup 时，不再
    # 任意选择默认皮肤。所有通过 MtlIdx、纹理目录和实体检查的签名作为
    # 独立 PMX 变体保留，供批量预览器连续确认。
    begin_stage(stage_names[22])
    exact_material_variants = _build_exact_logical_gim_material_variants(
        model_folder,
        by_md5,
        by_mesh,
        thd_dir,
        res_asset_paths,
    )
    packages.extend(exact_material_variants)

    # res 明文路径会让同一物理 Mesh 暴露出多个逻辑皮肤。默认 by_mesh 继续
    # 服务旧流程/组合模型；单模型导出另用 variants_by_mesh 保留不同材质签名。
    variants_by_mesh: dict[Path, list[MaterialPackage]] = {}
    variant_signatures: dict[Path, set[tuple[object, ...]]] = {}

    def append_variant(mesh_path: Path, package: MaterialPackage) -> None:
        if package.confidence not in TRUSTED_MATERIAL_CONFIDENCE:
            return
        mesh_path = mesh_path.resolve()
        signature = _material_variant_signature(package.materials)
        signatures = variant_signatures.setdefault(mesh_path, set())
        if signature in signatures:
            return
        signatures.add(signature)
        variants_by_mesh.setdefault(mesh_path, []).append(package)

    # 同一 GIM 内容的多个 THP parent 可能分别挂不同颜色/皮肤材质。
    # 这类变体不一定出现在 res 明文 Mesh 路径表中，因此直接从父节点层补齐。
    begin_stage(stage_names[23])
    thp_parent_variants = _build_thp_parent_variant_packages(
        model_folder,
        by_md5,
        thd_dir,
    )
    begin_stage(stage_names[24])
    for package in thp_parent_variants:
        for mesh_path in package.mesh_paths:
            append_variant(mesh_path, package)
    # 人工严格绑定也可能为同一物理 Mesh 明确给出多套逻辑皮肤（例如
    # c1/c2 NPC 共用同一几何）。全部加入变体池，不能只保留 by_mesh 的最后一套。
    for package in fallback_packages:
        for mesh_path in package.mesh_paths:
            append_variant(mesh_path, package)
    for package in res_packages:
        for mesh_path in package.mesh_paths:
            append_variant(mesh_path, package)
    for package in nested_packages:
        for mesh_path in package.mesh_paths:
            append_variant(mesh_path, package)
    for package in exact_material_variants:
        for mesh_path in package.mesh_paths:
            append_variant(mesh_path, package)
    for package in family_gim_packages:
        for mesh_path in package.mesh_paths:
            append_variant(mesh_path, package)
    for package in historical_packages:
        for mesh_path in package.mesh_paths:
            append_variant(mesh_path, package)
    for mesh_path, package in by_mesh.items():
        append_variant(mesh_path, package)

    def resolve_current_logical_mesh(reference: str) -> Path | None:
        for seed in namehash_seeds:
            record = record_by_name_hash.get(
                cloudfilesys_name_hash(reference, "model", seed)
            )
            path = by_md5.get(record.content_md5) if record is not None else None
            if path is not None and path.suffix.lower() == ".mesh":
                return path
        return None

    # Prefer the exact logical Mesh identity over an APK XML's stale decoded
    # label, but only for a proven cross-Mesh collision.
    _correct_cross_mesh_package_labels(
        (
            package
            for variants in variants_by_mesh.values()
            for package in variants
        ),
        all_asset_paths,
        resolve_current_logical_mesh,
    )

    # 同一官方 Mesh 可能同时存在于规范包、_hotdeps 或 loose_model。材质
    # 解析以内容身份为准，但导出扫描使用实际副本路径；在最终索引层补齐
    # 这些 MD5 别名，避免副本无条件退化成白模。
    _merge_material_mesh_content_aliases(
        by_mesh,
        variants_by_mesh,
        md5_by_path,
    )

    return packages, by_mesh, variants_by_mesh


def _build_cached_apk_material_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    apk_thd_dir: Path | None = None,
    current_thd_dir: Path | None = None,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """用缓存的 APK 基础 THX/THP 补当前热更新依赖表已丢失的模型材质。"""
    if apk_thd_dir is None:
        apk_thd_dir = model_folder.parent / "apk_model_parents" / "thd"
    if current_thd_dir is None:
        current_thd_dir = model_folder.parent.parent / "thd"
    thx_path = apk_thd_dir / "model.thx"
    thp_path = apk_thd_dir / "model.thp"
    if not thx_path.is_file() or not thp_path.is_file():
        return [], {}

    apk_model_root = apk_thd_dir.parent
    try:
        apk_by_md5: dict[str, Path] = {}
        apk_manifest = apk_model_root / "manifest.csv"
        with apk_manifest.open("r", newline="", encoding="utf-8-sig") as stream:
            for row in csv.DictReader(stream):
                digest = (row.get("resource_hash") or "").strip().lower()
                output = (row.get("output_path") or "").strip()
                if len(digest) == 32 and output and row.get("status") == "ok":
                    apk_by_md5[digest] = apk_model_root / Path(
                        output.replace("\\", "/")
                    )
        # The caller's map intentionally merges loose/APK caches for the
        # normal pipeline.  This cross-version resolver must keep the current
        # snapshot authoritative for Mesh/GIM/texture lookups, otherwise an
        # older cache entry with the same content hash can replace it.
        by_md5, _ = _manifest_hash_maps(
            model_folder,
            include_auxiliary=False,
        )
    except (OSError, ValueError, KeyError):
        apk_by_md5 = {}

    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thp,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    try:
        records = read_model_thx(thx_path)
        dependencies = read_model_thp(thp_path)
        seeds = read_thx_namehash_seeds(thx_path)
    except Exception:
        return [], {}

    record_by_hash = {record.name_hash: record for record in records}
    # APK THP 的 parent/dependency 哈希是逻辑路径哈希，不随版本内容变化。
    # 因此当 APK 只保留旧 GIM/材质、当前 THX 却已经换了 Mesh 内容时，
    # 仍可用“同 name_hash → 当前 THX content_md5”把旧官方材质关系接回当前 Mesh。
    current_record_by_hash: dict[int, object] = {}
    current_seeds = seeds
    current_thx = current_thd_dir / "model.thx"
    if current_thx.is_file():
        try:
            current_seeds = read_thx_namehash_seeds(current_thx)
            current_record_by_hash = {
                record.name_hash: record
                for record in read_model_thx(current_thx)
            }
        except Exception:
            current_record_by_hash = {}

    def resolve_dependency_path(name_hash: int) -> Path | None:
        """Resolve one logical dependency across current and APK snapshots.

        A current Mesh must win over the APK's older geometry.  For GIM,
        MaterialGroup, and skeleton dependencies the APK copy is preferred
        because that is the historical parent relation being replayed; the
        current copy remains a fallback when the APK cache lacks that object.
        """
        current_record = current_record_by_hash.get(name_hash)
        apk_record = record_by_hash.get(name_hash)
        current_path = (
            by_md5.get(current_record.content_md5)
            if current_record is not None
            else None
        )
        apk_path = (
            apk_by_md5.get(apk_record.content_md5)
            if apk_record is not None
            else None
        )
        if current_path is not None and current_path.suffix.lower() == ".mesh":
            return current_path.resolve()
        if apk_path is not None:
            return apk_path.resolve()
        return current_path.resolve() if current_path is not None else None
    # 材质关系来自 APK 基础资源，但最终贴图优先从当前版本 THX/IDX/WPK
    # 按逻辑路径解析；这样即使当前 KTX 没被预先解包，也能按 content_md5
    # 从当前 model.wpk 按需精确读取，而不会错误地只在 APK 旧包里找。
    resolver = CrossPackageTextureResolver(current_thd_dir, model_folder, by_md5)
    material_cache: dict[Path, list[MaterialDefinition]] = {}
    mesh_submesh_count_cache: dict[Path, int | None] = {}
    result: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}

    # APK parent 表有数千条，但我们只需要检查当前版本仍然存在、且正式
    # resolver 尚未绑定的大 Mesh。先把当前 Mesh 的稳定 name_hash 建成集合，
    # 避免每次完整构建都重新解析 4000+ 个无关 APK GIM。
    unresolved_mesh_name_hashes: set[int] = set()
    for name_hash, current_record in current_record_by_hash.items():
        path = by_md5.get(current_record.content_md5)
        if path is None or path.suffix.lower() != ".mesh":
            continue
        if path in existing_by_mesh:
            continue
        try:
            if path.stat().st_size <= 100 * 1024:
                continue
        except OSError:
            continue
        unresolved_mesh_name_hashes.add(name_hash)

    for parent_hash, dependency_hashes in dependencies.items():
        if not unresolved_mesh_name_hashes.intersection(dependency_hashes):
            continue
        parent_record = record_by_hash.get(parent_hash)
        if parent_record is None:
            continue
        gim_path = apk_by_md5.get(parent_record.content_md5)
        if gim_path is None:
            current_parent = current_record_by_hash.get(parent_hash)
            if current_parent is not None:
                gim_path = by_md5.get(current_parent.content_md5)
        if gim_path is None or gim_path.suffix.lower() != ".xml":
            continue
        gim_submeshes = parse_gim_submeshes(gim_path)
        if not gim_submeshes:
            continue

        dependency_paths: list[Path] = []
        for name_hash in dependency_hashes:
            # 优先 APK 缓存：GIM/Material/KTX 的旧内容就是我们要复用的
            # 官方关系证据；如果该条资源只存在于当前版本，则回退当前 THX。
            path = resolve_dependency_path(name_hash)
            if path is not None and path not in dependency_paths:
                dependency_paths.append(path)

        mesh_positions = [
            index
            for index, path in enumerate(dependency_paths)
            if path.suffix.lower() == ".mesh"
        ]
        for mesh_number, start in enumerate(mesh_positions):
            next_mesh = (
                mesh_positions[mesh_number + 1]
                if mesh_number + 1 < len(mesh_positions)
                else len(dependency_paths)
            )
            next_skeleton = next(
                (
                    index
                    for index in range(start + 1, next_mesh)
                    if dependency_paths[index].suffix.lower() == ".skeleton"
                ),
                next_mesh,
            )
            end = min(next_mesh, next_skeleton)
            mesh_path = dependency_paths[start].resolve()
            if mesh_path in existing_by_mesh or mesh_path in by_mesh:
                continue
            segment = dependency_paths[start + 1 : end]

            material_choices: list[
                tuple[Path, list[MaterialDefinition], list[str]]
            ] = []
            for path in segment:
                if path.suffix.lower() != ".xml":
                    continue
                if path not in material_cache:
                    material_cache[path] = parse_material_xml(path)
                materials = material_cache[path]
                if not materials:
                    continue
                ordered = order_materials_by_gim(materials, gim_submeshes)
                references = _ordered_texture_references(materials)
                if ordered and references:
                    material_choices.append((path, ordered, references))
            if mesh_path not in mesh_submesh_count_cache:
                try:
                    mesh_submesh_count_cache[mesh_path] = read_mesh_submesh_count(
                        mesh_path
                    )
                except Exception:
                    mesh_submesh_count_cache[mesh_path] = None
            expected_submeshes = mesh_submesh_count_cache[mesh_path]
            valid_choices = [
                choice
                for choice in material_choices
                if expected_submeshes == len(choice[1])
            ]
            if len(valid_choices) != 1:
                parent_choices: list[
                    tuple[Path, list[MaterialDefinition], list[str]]
                ] = []
                seen_material_paths: set[Path] = set()
                for path in dependency_paths:
                    if path.suffix.lower() != ".xml" or path in seen_material_paths:
                        continue
                    seen_material_paths.add(path)
                    if path not in material_cache:
                        material_cache[path] = parse_material_xml(path)
                    materials = material_cache[path]
                    if not materials:
                        continue
                    ordered = order_materials_by_gim(materials, gim_submeshes)
                    references = _ordered_texture_references(materials)
                    if (
                        ordered
                        and references
                        and expected_submeshes == len(ordered)
                    ):
                        parent_choices.append((path, ordered, references))
                if len(parent_choices) != 1:
                    continue
                valid_choices = parent_choices

            material_path, ordered_materials, references = valid_choices[0]

            texture_map: dict[str, Path] = {}
            for reference in references:
                texture_path: Path | None = None
                for seed in current_seeds:
                    name_hash = cloudfilesys_name_hash(reference, "model", seed)
                    record = current_record_by_hash.get(name_hash)
                    candidate = (
                        by_md5.get(record.content_md5)
                        if record is not None else None
                    )
                    if candidate is not None and candidate.suffix.lower() == ".ktx":
                        texture_path = candidate
                        break
                if texture_path is None:
                    texture_path = resolver.resolve(reference)
                if texture_path is not None and texture_path.suffix.lower() == ".ktx":
                    texture_map[reference] = texture_path

            main_references = {
                primary
                for material in ordered_materials
                if (primary := material_primary_texture(material))
            }
            if not main_references:
                continue
            resolved_main = {
                reference
                for reference in main_references
                if reference in texture_map
            }
            if not resolved_main:
                continue

            if len(texture_map) == len(references):
                confidence = "APK-THD精确"
            elif len(resolved_main) == len(main_references):
                confidence = "APK-THD精确主贴图"
            else:
                confidence = "APK-THD精确部分主贴图"
            package = MaterialPackage(
                xml_path=material_path,
                index=archive_index(material_path) or 0,
                package_name=extracted_resource_label(gim_path),
                materials=ordered_materials,
                mesh_paths=[mesh_path],
                texture_map=texture_map,
                confidence=confidence,
            )
            result.append(package)
            by_mesh[mesh_path] = package

    return result, by_mesh


def _build_explicit_gim_path_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    current_thd_dir: Path,
    source_thd_dirs: list[tuple[Path, str]],
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """用 GIM 自带的 Mesh=逻辑路径修复 THP 内容哈希过期造成的漏配。

    这是比“相邻资源猜测”更强的证据：GIM 明文指定实际 Mesh 路径；
    当前 THX 再把该路径哈希解析到当前版本的 Mesh 内容。材质仍只允许来自
    同一个官方 GIM 的 THP 依赖，且候选 MaterialGroup 必须唯一。
    """
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thp,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    current_thx = current_thd_dir / "model.thx"
    if not current_thx.is_file():
        return [], {}
    try:
        current_records = read_model_thx(current_thx)
        current_seeds = read_thx_namehash_seeds(current_thx)
    except Exception:
        return [], {}
    current_by_hash = {record.name_hash: record for record in current_records}
    current_resolver = CrossPackageTextureResolver(
        current_thd_dir, model_folder, by_md5
    )
    material_cache: dict[Path, list[MaterialDefinition]] = {}
    mesh_submesh_cache: dict[Path, int | None] = {}
    candidates_by_mesh: dict[Path, list[MaterialPackage]] = {}

    for source_thd_dir, confidence_prefix in source_thd_dirs:
        thx_path = source_thd_dir / "model.thx"
        thp_path = source_thd_dir / "model.thp"
        if not thx_path.is_file() or not thp_path.is_file():
            continue
        try:
            source_records = read_model_thx(thx_path)
            source_dependencies = read_model_thp(thp_path)
            source_seeds = read_thx_namehash_seeds(thx_path)
        except Exception:
            continue
        source_by_hash = {record.name_hash: record for record in source_records}

        for parent_hash, dependency_hashes in source_dependencies.items():
            parent_record = source_by_hash.get(parent_hash)
            if parent_record is None:
                continue
            gim_path = by_md5.get(parent_record.content_md5)
            if gim_path is None or gim_path.suffix.lower() != ".xml":
                continue
            mesh_reference = parse_gim_mesh_reference(gim_path)
            if not mesh_reference:
                continue
            gim_submeshes = parse_gim_submeshes(gim_path)
            if not gim_submeshes:
                continue

            # GIM 中的路径是客户端稳定资源键。用“当前” THX 解析，允许
            # APK/旧 THP 仍指向旧 Mesh MD5，而热更新后的 Mesh 内容已经变化。
            mesh_path: Path | None = None
            for seed in current_seeds:
                mesh_hash = cloudfilesys_name_hash(
                    mesh_reference, "model", seed
                )
                record = current_by_hash.get(mesh_hash)
                candidate = (
                    by_md5.get(record.content_md5)
                    if record is not None else None
                )
                if candidate is not None and candidate.suffix.lower() == ".mesh":
                    mesh_path = candidate.resolve()
                    break
            if mesh_path is None or mesh_path in existing_by_mesh:
                continue

            if mesh_path not in mesh_submesh_cache:
                try:
                    mesh_submesh_cache[mesh_path] = read_mesh_submesh_count(
                        mesh_path
                    )
                except Exception:
                    mesh_submesh_cache[mesh_path] = None
            if mesh_submesh_cache[mesh_path] != len(gim_submeshes):
                continue

            # 只从这个 GIM 自己的官方 THP 依赖中挑 MaterialGroup。
            # 若多个不同材质组都能满足 MtlIdx，则证据仍有歧义，保持不绑定。
            material_choices: dict[
                tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
                tuple[Path, list[MaterialDefinition]],
            ] = {}
            for dependency_hash in dependency_hashes:
                record = source_by_hash.get(dependency_hash)
                path = by_md5.get(record.content_md5) if record else None
                if path is None or path.suffix.lower() != ".xml":
                    continue
                if path not in material_cache:
                    material_cache[path] = parse_material_xml(path)
                materials = material_cache[path]
                if not materials:
                    continue
                ordered = order_materials_by_gim(materials, gim_submeshes)
                if len(ordered) != len(gim_submeshes):
                    continue
                signature = tuple(
                    (material.name, tuple(sorted(material.textures.items())))
                    for material in ordered
                )
                material_choices.setdefault(signature, (path, ordered))
            if len(material_choices) != 1:
                continue
            material_path, ordered_materials = next(
                iter(material_choices.values())
            )
            references = _ordered_texture_references(ordered_materials)
            if not references:
                continue

            texture_map: dict[str, Path] = {}
            for reference in references:
                texture_path: Path | None = None
                # 优先解析当前客户端内容。
                for seed in current_seeds:
                    name_hash = cloudfilesys_name_hash(reference, "model", seed)
                    record = current_by_hash.get(name_hash)
                    candidate = (
                        by_md5.get(record.content_md5)
                        if record is not None else None
                    )
                    if candidate is not None and candidate.suffix.lower() == ".ktx":
                        texture_path = candidate
                        break
                # 当前 THX 已删除的基础通用纹理，可回退到同一官方源 THX。
                if texture_path is None:
                    for seed in source_seeds:
                        name_hash = cloudfilesys_name_hash(reference, "model", seed)
                        record = source_by_hash.get(name_hash)
                        candidate = (
                            by_md5.get(record.content_md5)
                            if record is not None else None
                        )
                        if candidate is not None and candidate.suffix.lower() == ".ktx":
                            texture_path = candidate
                            break
                if texture_path is None:
                    texture_path = current_resolver.resolve(reference)
                if texture_path is not None and texture_path.suffix.lower() == ".ktx":
                    texture_map[reference] = texture_path

            main_references = {
                primary
                for material in ordered_materials
                if (primary := material_primary_texture(material))
            }
            resolved_main = {
                reference
                for reference in main_references
                if reference in texture_map
            }
            if not main_references or not resolved_main:
                continue
            if len(texture_map) == len(references):
                confidence = confidence_prefix
            elif len(resolved_main) == len(main_references):
                confidence = f"{confidence_prefix}主贴图"
            else:
                confidence = f"{confidence_prefix}部分主贴图"
            package = MaterialPackage(
                xml_path=material_path,
                index=archive_index(material_path) or 0,
                package_name=extracted_resource_label(gim_path),
                materials=ordered_materials,
                mesh_paths=[mesh_path],
                texture_map=texture_map,
                confidence=confidence,
            )
            candidates_by_mesh.setdefault(mesh_path, []).append(package)

    result: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    for mesh_path, candidates in candidates_by_mesh.items():
        # 同一个 Mesh 若被多个 GIM 引用，只在它们最终给出同一套材质定义时绑定；
        # 不让皮肤/场景变体因为共享 Mesh 路径而互相覆盖。
        grouped: dict[
            tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
            list[MaterialPackage],
        ] = {}
        for package in candidates:
            signature = tuple(
                (material.name, tuple(sorted(material.textures.items())))
                for material in package.materials
            )
            grouped.setdefault(signature, []).append(package)
        if len(grouped) != 1:
            continue
        choices = next(iter(grouped.values()))
        package = max(choices, key=lambda item: len(item.texture_map))
        result.append(package)
        by_mesh[mesh_path] = package
    return result, by_mesh


def _build_script3_path_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    current_thd_dir: Path,
    source_thd_dirs: list[tuple[Path, str]],
    gim_references: list[str] | None = None,
    preserve_variants: bool = False,
    shared_material_groups: list[tuple[Path, list[MaterialDefinition]]] | None = None,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """用客户端明文 GIM 路径恢复同名 Mesh 的材质关系。

    默认读取 script3；也可传入 res 等已验证的逻辑 GIM 路径。只有 GIM 路径
    和同 stem Mesh 路径都能被当前 THX 精确解析，且 GIM 的官方 THP 中只有
    一套合法 MaterialGroup 时绑定。
    """
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thp,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    paths = (
        gim_references
        if gim_references is not None
        else load_script3_gim_paths(current_thd_dir, model_folder)
    )
    if not paths:
        return [], {}
    try:
        current_records = read_model_thx(current_thd_dir / "model.thx")
        current_seeds = read_thx_namehash_seeds(
            current_thd_dir / "model.thx"
        )
    except Exception:
        return [], {}
    current_by_hash = {record.name_hash: record for record in current_records}
    current_resolver = CrossPackageTextureResolver(
        current_thd_dir, model_folder, by_md5
    )
    mesh_submesh_cache: dict[Path, int | None] = {}
    material_cache: dict[Path, list[MaterialDefinition]] = {}
    candidates_by_mesh: dict[Path, list[MaterialPackage]] = {}

    source_tables: list[
        tuple[
            str,
            dict[int, object],
            dict[int, list[int]],
            tuple[int, ...],
        ]
    ] = []
    for source_thd_dir, confidence_prefix in source_thd_dirs:
        thx_path = source_thd_dir / "model.thx"
        thp_path = source_thd_dir / "model.thp"
        if not thx_path.is_file() or not thp_path.is_file():
            continue
        try:
            source_records = read_model_thx(thx_path)
            source_tables.append(
                (
                    confidence_prefix,
                    {record.name_hash: record for record in source_records},
                    read_model_thp(thp_path),
                    read_thx_namehash_seeds(thx_path),
                )
            )
        except Exception:
            continue

    for gim_reference in paths:
        if not gim_reference.startswith("model/") or not gim_reference.endswith(".gim"):
            continue
        mesh_reference = gim_reference[:-4] + ".mesh"
        mesh_path: Path | None = None
        for seed in current_seeds:
            mesh_hash = cloudfilesys_name_hash(
                mesh_reference, "model", seed
            )
            record = current_by_hash.get(mesh_hash)
            candidate = (
                by_md5.get(record.content_md5)
                if record is not None else None
            )
            if candidate is not None and candidate.suffix.lower() == ".mesh":
                mesh_path = candidate.resolve()
                break
        if mesh_path is None or mesh_path in existing_by_mesh:
            continue
        if mesh_path not in mesh_submesh_cache:
            try:
                mesh_submesh_cache[mesh_path] = read_mesh_submesh_count(mesh_path)
            except Exception:
                mesh_submesh_cache[mesh_path] = None
        expected_submeshes = mesh_submesh_cache[mesh_path]
        if not expected_submeshes:
            continue

        for confidence_prefix, source_by_hash, dependencies, source_seeds in source_tables:
            gim_record = None
            gim_hash = None
            for seed in source_seeds:
                candidate_hash = cloudfilesys_name_hash(
                    gim_reference, "model", seed
                )
                candidate = source_by_hash.get(candidate_hash)
                if candidate is not None:
                    gim_hash = candidate_hash
                    gim_record = candidate
                    break
            if gim_record is None or gim_hash is None:
                continue
            gim_path = by_md5.get(gim_record.content_md5)
            if gim_path is None or gim_path.suffix.lower() != ".xml":
                continue
            gim_submeshes = parse_gim_submeshes(gim_path)
            if len(gim_submeshes) != expected_submeshes:
                continue

            material_choices: dict[
                tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
                tuple[Path, list[MaterialDefinition]],
            ] = {}
            for dependency_hash in dependencies.get(gim_hash, []):
                record = source_by_hash.get(dependency_hash)
                path = by_md5.get(record.content_md5) if record else None
                if path is None or path.suffix.lower() != ".xml":
                    continue
                if path not in material_cache:
                    material_cache[path] = parse_material_xml(path)
                materials = material_cache[path]
                if not materials:
                    continue
                ordered = order_materials_by_gim(materials, gim_submeshes)
                if len(ordered) != expected_submeshes:
                    continue
                signature = tuple(
                    (material.name, tuple(sorted(material.textures.items())))
                    for material in ordered
                )
                material_choices.setdefault(signature, (path, ordered))

            # 路径精确命中的 GIM 有时自身 THP 已不再挂 MaterialGroup，
            # 但当前 THX 的 kind=1 共享池仍保留正式材质。只有逐子网格按
            # MtlIdx 排列后，材质名与 GIM 名完全一致且最终签名唯一时才采用。
            if not material_choices and shared_material_groups:
                target_names = tuple(
                    _normalized_material_name(item.name)
                    for item in gim_submeshes
                )
                for shared_path, shared_materials in shared_material_groups:
                    ordered = order_materials_by_gim(
                        shared_materials, gim_submeshes
                    )
                    if (
                        len(ordered) != expected_submeshes
                        or tuple(
                            _normalized_material_name(material.name)
                            for material in ordered
                        ) != target_names
                    ):
                        continue
                    signature = tuple(
                        (
                            material.name,
                            tuple(sorted(material.textures.items())),
                        )
                        for material in ordered
                    )
                    material_choices.setdefault(
                        signature, (shared_path, ordered)
                    )

            if len(material_choices) == 1:
                material_path, ordered_materials = next(
                    iter(material_choices.values())
                )
            else:
                # 同一个精确逻辑 GIM 有时同时保留正式材质和 TempMaterial。
                # 如果它们逐子网格的全部纹理槽完全一致，则渲染结果无歧义；
                # 材质名改用 GIM 子网格名。任何纹理槽不同仍直接跳过。
                texture_equivalent: dict[
                    tuple[tuple[tuple[str, str], ...], ...],
                    tuple[Path, list[MaterialDefinition]],
                ] = {}
                for path, ordered in material_choices.values():
                    texture_signature = tuple(
                        tuple(sorted(material.textures.items()))
                        for material in ordered
                    )
                    texture_equivalent.setdefault(
                        texture_signature, (path, ordered)
                    )
                if len(texture_equivalent) != 1:
                    continue
                material_path, source_ordered = next(
                    iter(texture_equivalent.values())
                )
                ordered_materials = [
                    MaterialDefinition(
                        gim_submeshes[index].name.lstrip("@"),
                        dict(material.textures),
                    )
                    for index, material in enumerate(source_ordered)
                ]
            references = _ordered_texture_references(ordered_materials)
            if not references:
                continue

            texture_map: dict[str, Path] = {}
            for reference in references:
                texture_path: Path | None = None
                for seed in current_seeds:
                    name_hash = cloudfilesys_name_hash(reference, "model", seed)
                    record = current_by_hash.get(name_hash)
                    candidate = (
                        by_md5.get(record.content_md5)
                        if record is not None else None
                    )
                    if candidate is not None and candidate.suffix.lower() == ".ktx":
                        texture_path = candidate
                        break
                if texture_path is None:
                    for seed in source_seeds:
                        name_hash = cloudfilesys_name_hash(reference, "model", seed)
                        record = source_by_hash.get(name_hash)
                        candidate = (
                            by_md5.get(record.content_md5)
                            if record is not None else None
                        )
                        if candidate is not None and candidate.suffix.lower() == ".ktx":
                            texture_path = candidate
                            break
                if texture_path is None:
                    texture_path = current_resolver.resolve(reference)
                if texture_path is not None and texture_path.suffix.lower() == ".ktx":
                    texture_map[reference] = texture_path

            main_references = {
                primary
                for material in ordered_materials
                if (primary := material_primary_texture(material))
            }
            resolved_main = {
                reference
                for reference in main_references
                if reference in texture_map
            }
            if not main_references or not resolved_main:
                continue
            if len(texture_map) == len(references):
                confidence = confidence_prefix
            elif len(resolved_main) == len(main_references):
                confidence = f"{confidence_prefix}主贴图"
            else:
                confidence = f"{confidence_prefix}部分主贴图"
            package = MaterialPackage(
                xml_path=material_path,
                index=archive_index(material_path) or 0,
                package_name=Path(gim_reference).stem,
                materials=ordered_materials,
                mesh_paths=[mesh_path],
                texture_map=texture_map,
                confidence=confidence,
            )
            candidates_by_mesh.setdefault(mesh_path, []).append(package)

    result: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    for mesh_path, candidates in candidates_by_mesh.items():
        grouped: dict[
            tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
            list[MaterialPackage],
        ] = {}
        for package in candidates:
            signature = tuple(
                (material.name, tuple(sorted(material.textures.items())))
                for material in package.materials
            )
            grouped.setdefault(signature, []).append(package)
        if preserve_variants:
            representatives = [
                max(choices, key=lambda item: len(item.texture_map))
                for choices in grouped.values()
            ]
            result.extend(representatives)
            if len(representatives) == 1:
                by_mesh[mesh_path] = representatives[0]
            continue
        if len(grouped) != 1:
            continue
        choices = next(iter(grouped.values()))
        package = max(choices, key=lambda item: len(item.texture_map))
        result.append(package)
        by_mesh[mesh_path] = package
    return result, by_mesh


def _build_res_proxy_gim_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    thd_dir: Path,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """恢复 res 中“派生 GIM 实体缺失、但 THP 保留代理材质链”的 Mesh。

    仅接受：目标 Mesh 与同 stem GIM 均能被当前 THX 路径哈希精确确认；
    目标 GIM 自身 XML 已缺失但 THP 仍存在；其直接依赖中存在同子网格数的
    源 Mesh、可见子 GIM 与 MaterialGroup，且最终材质签名唯一。
    """
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thp,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    thx_path = thd_dir / "model.thx"
    thp_path = thd_dir / "model.thp"
    if not thx_path.is_file() or not thp_path.is_file():
        return [], {}
    try:
        records = read_model_thx(thx_path)
        dependencies = read_model_thp(thp_path)
        seeds = read_thx_namehash_seeds(thx_path)
    except Exception:
        return [], {}

    record_by_hash = {record.name_hash: record for record in records}
    resolver = CrossPackageTextureResolver(thd_dir, model_folder, by_md5)
    material_cache: dict[Path, list[MaterialDefinition]] = {}
    submesh_count_cache: dict[Path, int | None] = {}
    candidates_by_mesh: dict[Path, list[MaterialPackage]] = {}

    mesh_references = sorted({
        reference
        for reference in load_res_asset_paths(thd_dir, model_folder)
        if reference.startswith("model/") and reference.endswith(".mesh")
    })
    for mesh_reference in mesh_references:
        mesh_record = None
        for seed in seeds:
            candidate = record_by_hash.get(
                cloudfilesys_name_hash(mesh_reference, "model", seed)
            )
            if candidate is not None:
                mesh_record = candidate
                break
        mesh_path = (
            by_md5.get(mesh_record.content_md5)
            if mesh_record is not None else None
        )
        if (
            mesh_path is None
            or mesh_path.suffix.lower() != ".mesh"
            or mesh_path.resolve() in existing_by_mesh
        ):
            continue
        mesh_path = mesh_path.resolve()

        gim_reference = mesh_reference[:-5] + ".gim"
        gim_hash: int | None = None
        gim_record = None
        for seed in seeds:
            candidate_hash = cloudfilesys_name_hash(
                gim_reference, "model", seed
            )
            candidate = record_by_hash.get(candidate_hash)
            if candidate is not None:
                gim_hash = candidate_hash
                gim_record = candidate
                break
        if gim_record is None or gim_hash is None:
            continue
        # 可见 GIM 已由更强的 res/script3 路径规则处理；这里只处理实体被裁掉的派生 GIM。
        visible_gim = by_md5.get(gim_record.content_md5)
        if visible_gim is not None and visible_gim.suffix.lower() == ".xml":
            continue
        dependency_hashes = dependencies.get(gim_hash, [])
        if not dependency_hashes:
            continue

        if mesh_path not in submesh_count_cache:
            try:
                submesh_count_cache[mesh_path] = read_mesh_submesh_count(mesh_path)
            except Exception:
                submesh_count_cache[mesh_path] = None
        expected_submeshes = submesh_count_cache[mesh_path]
        if not expected_submeshes:
            continue

        dependency_paths: list[Path] = []
        for dependency_hash in dependency_hashes:
            record = record_by_hash.get(dependency_hash)
            path = by_md5.get(record.content_md5) if record is not None else None
            if path is not None:
                dependency_paths.append(path.resolve())

        # 代理链必须直接带一份同子网格数源 Mesh；只靠子 GIM + 材质还不够强。
        source_meshes: list[Path] = []
        child_gims: list[tuple[Path, list[GimSubmesh]]] = []
        material_groups: list[tuple[Path, list[MaterialDefinition]]] = []
        for path in dependency_paths:
            if path.suffix.lower() == ".mesh":
                if path not in submesh_count_cache:
                    try:
                        submesh_count_cache[path] = read_mesh_submesh_count(path)
                    except Exception:
                        submesh_count_cache[path] = None
                if submesh_count_cache[path] == expected_submeshes:
                    source_meshes.append(path)
                continue
            if path.suffix.lower() != ".xml":
                continue
            submeshes = parse_gim_submeshes(path)
            if len(submeshes) == expected_submeshes:
                child_gims.append((path, submeshes))
            if path not in material_cache:
                material_cache[path] = parse_material_xml(path)
            materials = material_cache[path]
            if materials:
                material_groups.append((path, materials))
        if len(source_meshes) != 1 or not child_gims or not material_groups:
            continue

        choices_by_signature: dict[
            tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
            tuple[Path, list[MaterialDefinition]],
        ] = {}
        for child_path, submeshes in child_gims:
            for material_path, materials in material_groups:
                ordered = order_materials_by_gim(materials, submeshes)
                if len(ordered) != expected_submeshes:
                    continue
                signature = tuple(
                    (material.name, tuple(sorted(material.textures.items())))
                    for material in ordered
                )
                choices_by_signature.setdefault(
                    signature, (material_path, ordered)
                )
        if len(choices_by_signature) != 1:
            continue
        material_path, ordered_materials = next(
            iter(choices_by_signature.values())
        )
        references = _ordered_texture_references(ordered_materials)
        if not references:
            continue

        texture_map: dict[str, Path] = {}
        for reference in references:
            texture_path: Path | None = None
            for seed in seeds:
                name_hash = cloudfilesys_name_hash(reference, "model", seed)
                record = record_by_hash.get(name_hash)
                candidate = (
                    by_md5.get(record.content_md5)
                    if record is not None else None
                )
                if candidate is not None and candidate.suffix.lower() == ".ktx":
                    texture_path = candidate
                    break
            if texture_path is None:
                texture_path = resolver.resolve(reference)
            if texture_path is not None and texture_path.suffix.lower() == ".ktx":
                texture_map[reference] = texture_path

        main_references = {
            primary
            for material in ordered_materials
            if (primary := material_primary_texture(material))
        }
        resolved_main = {
            reference for reference in main_references if reference in texture_map
        }
        if not main_references or not resolved_main:
            continue
        if len(texture_map) == len(references):
            confidence = "res代理GIM精确"
        elif len(resolved_main) == len(main_references):
            confidence = "res代理GIM精确主贴图"
        else:
            confidence = "res代理GIM精确部分主贴图"
        package = MaterialPackage(
            xml_path=material_path,
            index=archive_index(material_path) or 0,
            package_name=Path(mesh_reference).stem,
            materials=ordered_materials,
            mesh_paths=[mesh_path],
            texture_map=texture_map,
            confidence=confidence,
        )
        candidates_by_mesh.setdefault(mesh_path, []).append(package)

    result: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    for mesh_path, candidates in candidates_by_mesh.items():
        grouped: dict[
            tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
            list[MaterialPackage],
        ] = {}
        for package in candidates:
            signature = tuple(
                (material.name, tuple(sorted(material.textures.items())))
                for material in package.materials
            )
            grouped.setdefault(signature, []).append(package)
        if len(grouped) != 1:
            continue
        package = max(
            next(iter(grouped.values())),
            key=lambda item: len(item.texture_map),
        )
        result.append(package)
        by_mesh[mesh_path] = package
    return result, by_mesh


def _build_res_touming_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    thd_dir: Path,
    apk_thd_dir: Path,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """严格恢复 `_show_touming` 派生 Mesh 的原版 Show 材质。

    仅接受：目标目录无独立图片资源；目标/原版 Show Mesh 路径都由当前 THX
    精确解析；两者子网格、位置、法线、面与 UV 完全相同；原版 Show GIM
    又能由官方 THP 唯一确定材质。骨骼/蒙皮可以不同，因为它们不改变材质槽。
    """
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    thx_path = thd_dir / "model.thx"
    if not thx_path.is_file():
        return [], {}
    try:
        records = read_model_thx(thx_path)
        seeds = read_thx_namehash_seeds(thx_path)
    except Exception:
        return [], {}
    record_by_hash = {record.name_hash: record for record in records}
    assets = load_res_asset_paths(thd_dir, model_folder)
    image_directories = {
        reference.rsplit("/", 1)[0]
        for reference in assets
        if reference.endswith((".tga", ".png", ".dds", ".ktx"))
    }

    def resolve_mesh(reference: str) -> Path | None:
        for seed in seeds:
            record = record_by_hash.get(
                cloudfilesys_name_hash(reference, "model", seed)
            )
            candidate = (
                by_md5.get(record.content_md5)
                if record is not None else None
            )
            if candidate is not None and candidate.suffix.lower() == ".mesh":
                return candidate.resolve()
        return None

    candidates: list[tuple[str, Path, str, Path]] = []
    parent_gims: set[str] = set()
    surface_cache: dict[Path, str | None] = {}
    for target_reference in assets:
        if (
            not target_reference.startswith("model/")
            or not target_reference.endswith("_show_touming.mesh")
            or "_show_touming/" not in target_reference
        ):
            continue
        target_directory = target_reference.rsplit("/", 1)[0]
        if target_directory in image_directories:
            continue
        parent_reference = (
            target_reference
            .replace("_show_touming/", "_show/")
            .replace("_show_touming.mesh", "_show.mesh")
        )
        target_path = resolve_mesh(target_reference)
        parent_path = resolve_mesh(parent_reference)
        if (
            target_path is None
            or parent_path is None
            or target_path in existing_by_mesh
        ):
            continue
        for path in (target_path, parent_path):
            if path not in surface_cache:
                try:
                    surface_cache[path] = _mesh_surface_fingerprint(parse_mesh(path))
                except Exception:
                    surface_cache[path] = None
        if (
            surface_cache[target_path] is None
            or surface_cache[target_path] != surface_cache[parent_path]
        ):
            continue
        parent_gim = parent_reference[:-5] + ".gim"
        parent_gims.add(parent_gim)
        candidates.append(
            (target_reference, target_path, parent_gim, parent_path)
        )

    if not candidates:
        return [], {}
    _, parent_by_mesh = _build_script3_path_packages(
        model_folder,
        by_md5,
        {},
        thd_dir,
        [
            (thd_dir, "res透明源路径精确"),
            (apk_thd_dir, "APK-res透明源路径精确"),
        ],
        gim_references=sorted(parent_gims),
    )

    result: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    for target_reference, target_path, _, parent_path in candidates:
        source = parent_by_mesh.get(parent_path)
        if source is None:
            continue
        references = _ordered_texture_references(source.materials)
        main_references = {
            primary
            for material in source.materials
            if (primary := material_primary_texture(material))
        }
        resolved_main = {
            reference
            for reference in main_references
            if reference in source.texture_map
        }
        if not main_references or not resolved_main:
            continue
        if len(source.texture_map) == len(references):
            confidence = "res透明表面精确"
        elif len(resolved_main) == len(main_references):
            confidence = "res透明表面精确主贴图"
        else:
            confidence = "res透明表面精确部分主贴图"
        package = MaterialPackage(
            xml_path=source.xml_path,
            index=source.index,
            package_name=Path(target_reference).stem,
            materials=source.materials,
            mesh_paths=[target_path],
            texture_map=dict(source.texture_map),
            confidence=confidence,
        )
        result.append(package)
        by_mesh[target_path] = package
    return result, by_mesh


def _build_res_tingyuan_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    thd_dir: Path,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """恢复 `_tingyuan` 中由 Show 父 GIM 多个源 Mesh 无损合并的派生模型。"""
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thp,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    thx_path = thd_dir / "model.thx"
    thp_path = thd_dir / "model.thp"
    if not thx_path.is_file() or not thp_path.is_file():
        return [], {}
    try:
        records = read_model_thx(thx_path)
        dependencies = read_model_thp(thp_path)
        seeds = read_thx_namehash_seeds(thx_path)
    except Exception:
        return [], {}
    record_by_hash = {record.name_hash: record for record in records}
    assets = load_res_asset_paths(thd_dir, model_folder)
    image_directories = {
        reference.rsplit("/", 1)[0]
        for reference in assets
        if reference.endswith((".tga", ".png", ".dds", ".ktx"))
    }

    def resolve_record(reference: str):
        for seed in seeds:
            candidate_hash = cloudfilesys_name_hash(reference, "model", seed)
            record = record_by_hash.get(candidate_hash)
            if record is not None:
                return candidate_hash, record
        return None, None

    parsed_cache: dict[Path, ParsedMesh | None] = {}
    fingerprint_cache: dict[Path, tuple[str, ...] | None] = {}

    def parsed_mesh(path: Path) -> ParsedMesh | None:
        if path not in parsed_cache:
            try:
                parsed_cache[path] = parse_mesh(path)
            except Exception:
                parsed_cache[path] = None
        return parsed_cache[path]

    def fingerprints(path: Path) -> tuple[str, ...] | None:
        if path not in fingerprint_cache:
            mesh = parsed_mesh(path)
            fingerprint_cache[path] = (
                _mesh_submesh_surface_fingerprints(mesh)
                if mesh is not None else None
            )
        return fingerprint_cache[path]

    result: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    target_references = sorted({
        reference
        for reference in assets
        if (
            reference.startswith("model/")
            and reference.endswith("_tingyuan.mesh")
            and "_tingyuan/" in reference
        )
    })
    for target_reference in target_references:
        target_directory = target_reference.rsplit("/", 1)[0]
        if target_directory in image_directories:
            continue
        _, target_record = resolve_record(target_reference)
        target_path = (
            by_md5.get(target_record.content_md5)
            if target_record is not None else None
        )
        if target_path is None or target_path.suffix.lower() != ".mesh":
            continue
        target_path = target_path.resolve()
        if target_path in existing_by_mesh or target_path in by_mesh:
            continue
        target_fingerprints = fingerprints(target_path)
        if not target_fingerprints:
            continue

        target_stem = Path(target_reference).stem
        if not target_stem.endswith("_tingyuan"):
            continue
        base_stem = target_stem[:-9]
        parent_stem = f"{base_stem}_show"
        parent_gim_reference = (
            f"model/{parent_stem}/{parent_stem}.gim"
        )
        parent_hash, parent_record = resolve_record(parent_gim_reference)
        if parent_record is None or parent_hash is None:
            continue
        parent_gim_path = by_md5.get(parent_record.content_md5)
        if (
            parent_gim_path is None
            or parent_gim_path.suffix.lower() != ".xml"
            or not dependencies.get(parent_hash)
        ):
            continue

        source_paths: list[Path] = []
        for dependency_hash in dependencies[parent_hash]:
            record = record_by_hash.get(dependency_hash)
            path = by_md5.get(record.content_md5) if record is not None else None
            if path is None or path.suffix.lower() != ".mesh":
                continue
            path = path.resolve()
            package = existing_by_mesh.get(path)
            if (
                package is None
                or package.confidence not in TRUSTED_MATERIAL_CONFIDENCE
                or "部分材质" in package.confidence
            ):
                continue
            source_fingerprints = fingerprints(path)
            if (
                not source_fingerprints
                or len(source_fingerprints) != len(package.materials)
            ):
                continue
            source_paths.append(path)
        if len(source_paths) < 2:
            continue

        candidates_by_fingerprint: dict[
            str, list[tuple[Path, int, MaterialDefinition, MaterialPackage]]
        ] = {}
        for source_path in source_paths:
            package = existing_by_mesh[source_path]
            source_fingerprints = fingerprints(source_path) or ()
            for index, fingerprint in enumerate(source_fingerprints):
                candidates_by_fingerprint.setdefault(fingerprint, []).append(
                    (source_path, index, package.materials[index], package)
                )

        target_materials: list[MaterialDefinition] = []
        used_source_paths: set[Path] = set()
        used_packages: list[MaterialPackage] = []
        valid = True
        for fingerprint in target_fingerprints:
            choices = candidates_by_fingerprint.get(fingerprint, [])
            if len(choices) != 1:
                valid = False
                break
            source_path, _, material, package = choices[0]
            target_materials.append(material)
            used_source_paths.add(source_path)
            if package not in used_packages:
                used_packages.append(package)
        if not valid or len(used_source_paths) < 2:
            continue

        texture_map: dict[str, Path] = {}
        for package in used_packages:
            for reference, path in package.texture_map.items():
                old = texture_map.get(reference)
                if old is not None and old.resolve() != path.resolve():
                    valid = False
                    break
                texture_map[reference] = path
            if not valid:
                break
        if not valid:
            continue

        references = _ordered_texture_references(target_materials)
        main_references = {
            primary
            for material in target_materials
            if (primary := material_primary_texture(material))
        }
        resolved_main = {
            reference for reference in main_references if reference in texture_map
        }
        if not main_references or not resolved_main:
            continue
        if len(texture_map) >= len(set(references)):
            confidence = "res庭院合并精确"
        elif len(resolved_main) == len(main_references):
            confidence = "res庭院合并精确主贴图"
        else:
            confidence = "res庭院合并精确部分主贴图"
        first_package = used_packages[0]
        package = MaterialPackage(
            xml_path=first_package.xml_path,
            index=first_package.index,
            package_name=target_stem,
            materials=target_materials,
            mesh_paths=[target_path],
            texture_map=texture_map,
            confidence=confidence,
        )
        result.append(package)
        by_mesh[target_path] = package
    return result, by_mesh


def _build_res_numbered_merge_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    thd_dir: Path,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """恢复 foo1/foo2/... 被合并成 foo.mesh 的派生资源。

    这是严格结构规则，不按名称猜材质：
    1) res 同时存在 foo.mesh 与连续编号的 foo1/foo2/...mesh；
    2) 每个编号源 Mesh 都只有一个子网格，且其表面指纹多重集合与目标完全一致；
    3) 所有编号源 Mesh 已有可信且完全相同的单材质签名；
    4) foo.gim 的当前 THP 仍直接依赖该材质的每一张主贴图。
    只有四项全部满足时，才把同一官方材质复制到目标各子网格。
    """
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thp,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    thx_path = thd_dir / "model.thx"
    thp_path = thd_dir / "model.thp"
    if not thx_path.is_file() or not thp_path.is_file():
        return [], {}
    try:
        records = read_model_thx(thx_path)
        dependencies = read_model_thp(thp_path)
        seeds = read_thx_namehash_seeds(thx_path)
    except Exception:
        return [], {}
    record_by_hash = {record.name_hash: record for record in records}

    mesh_references = {
        reference
        for reference in load_res_asset_paths(thd_dir, model_folder)
        if reference.startswith("model/") and reference.endswith(".mesh")
    }
    numbered: dict[str, list[tuple[int, str]]] = {}
    for reference in mesh_references:
        match = re.match(r"^(.*?)(\d+)\.mesh$", reference)
        if match is None:
            continue
        base_reference = match.group(1) + ".mesh"
        if base_reference not in mesh_references:
            continue
        numbered.setdefault(base_reference, []).append(
            (int(match.group(2)), reference)
        )

    def resolve_mesh(reference: str) -> Path | None:
        found: list[Path] = []
        for seed in seeds:
            record = record_by_hash.get(
                cloudfilesys_name_hash(reference, "model", seed)
            )
            path = by_md5.get(record.content_md5) if record else None
            if path is not None and path.suffix.lower() == ".mesh":
                resolved = path.resolve()
                if resolved not in found:
                    found.append(resolved)
        return found[0] if len(found) == 1 else None

    result: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    for target_reference, numbered_references in numbered.items():
        target_path = resolve_mesh(target_reference)
        if (
            target_path is None
            or target_path in existing_by_mesh
            or target_path in by_mesh
        ):
            continue
        try:
            target_mesh = parse_mesh(target_path)
        except Exception:
            continue
        source_references = [
            reference for _, reference in sorted(numbered_references)
        ]
        if (
            len(target_mesh.submeshes) < 2
            or len(source_references) != len(target_mesh.submeshes)
        ):
            continue

        target_fingerprints = Counter(
            _mesh_submesh_surface_fingerprints(target_mesh)
        )
        source_fingerprints: Counter[str] = Counter()
        source_packages: list[MaterialPackage] = []
        material_signature: tuple[
            str, tuple[tuple[str, str], ...]
        ] | None = None
        valid = True
        for source_reference in source_references:
            source_path = resolve_mesh(source_reference)
            if source_path is None:
                valid = False
                break
            try:
                source_mesh = parse_mesh(source_path)
            except Exception:
                valid = False
                break
            if len(source_mesh.submeshes) != 1:
                valid = False
                break
            source_fingerprints.update(
                _mesh_submesh_surface_fingerprints(source_mesh)
            )
            package = existing_by_mesh.get(source_path)
            if (
                package is None
                or package.confidence not in TRUSTED_MATERIAL_CONFIDENCE
                or "部分材质" in package.confidence
                or len(package.materials) != 1
            ):
                valid = False
                break
            material = package.materials[0]
            signature = (
                material.name,
                tuple(sorted(material.textures.items())),
            )
            if material_signature is None:
                material_signature = signature
            elif material_signature != signature:
                valid = False
                break
            source_packages.append(package)
        if not valid or source_fingerprints != target_fingerprints:
            continue
        if material_signature is None or not source_packages:
            continue

        gim_reference = target_reference[:-5] + ".gim"
        gim_hashes: list[int] = []
        for seed in seeds:
            gim_hash = cloudfilesys_name_hash(gim_reference, "model", seed)
            if gim_hash in record_by_hash and gim_hash not in gim_hashes:
                gim_hashes.append(gim_hash)
        if len(gim_hashes) != 1:
            continue
        target_dependencies = set(dependencies.get(gim_hashes[0], []))
        if not target_dependencies:
            continue

        source_material = source_packages[0].materials[0]
        main_references = {
            primary
            for package in source_packages
            for material in package.materials
            if (primary := material_primary_texture(material))
        }
        if not main_references:
            continue
        all_main_referenced_by_target = True
        for reference in main_references:
            if not any(
                cloudfilesys_name_hash(reference, "model", seed)
                in target_dependencies
                for seed in seeds
            ):
                all_main_referenced_by_target = False
                break
        if not all_main_referenced_by_target:
            continue

        texture_map: dict[str, Path] = {}
        for package in source_packages:
            for reference, path in package.texture_map.items():
                old = texture_map.get(reference)
                if old is not None and old.resolve() != path.resolve():
                    valid = False
                    break
                texture_map[reference] = path
            if not valid:
                break
        if not valid:
            continue

        target_materials = [
            MaterialDefinition(
                source_material.name,
                dict(source_material.textures),
            )
            for _ in target_mesh.submeshes
        ]
        references = _ordered_texture_references(target_materials)
        resolved_main = {
            reference for reference in main_references if reference in texture_map
        }
        if not resolved_main:
            continue
        if len(texture_map) >= len(set(references)):
            confidence = "res编号合并精确"
        elif len(resolved_main) == len(main_references):
            confidence = "res编号合并精确主贴图"
        else:
            confidence = "res编号合并精确部分主贴图"
        first_package = source_packages[0]
        package = MaterialPackage(
            xml_path=first_package.xml_path,
            index=first_package.index,
            package_name=Path(target_reference).stem,
            materials=target_materials,
            mesh_paths=[target_path],
            texture_map=texture_map,
            confidence=confidence,
        )
        result.append(package)
        by_mesh[target_path] = package
    return result, by_mesh


def _build_res_gim_subset_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    thd_dir: Path,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """恢复无专属纹理的 `_NN` 派生 GIM 前缀子集。

    目标目录和 Mesh stem 都必须以同一个 `_数字` 结尾；去掉后得到父资源。
    目标 GIM 必须有实体，且其 `(规范化子网格名, MtlIdx)` 序列严格等于
    父 GIM 的前缀。目标目录不得出现任何独立图片资源。这样才允许复用父
    GIM 对应前缀的官方材质，避免把真正换皮的 `_01/_02` 误继承成原版。
    """
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    thx_path = thd_dir / "model.thx"
    if not thx_path.is_file():
        return [], {}
    try:
        records = read_model_thx(thx_path)
        seeds = read_thx_namehash_seeds(thx_path)
    except Exception:
        return [], {}
    record_by_hash = {record.name_hash: record for record in records}
    asset_paths = load_res_asset_paths(thd_dir, model_folder)
    mesh_references = {
        reference
        for reference in asset_paths
        if reference.startswith("model/") and reference.endswith(".mesh")
    }
    image_references = {
        reference
        for reference in asset_paths
        if Path(reference).suffix.lower() in IMAGE_SUFFIXES
    }

    def resolve_content(reference: str, suffix: str) -> Path | None:
        found: list[Path] = []
        for seed in seeds:
            record = record_by_hash.get(
                cloudfilesys_name_hash(reference, "model", seed)
            )
            path = by_md5.get(record.content_md5) if record else None
            if path is not None and path.suffix.lower() == suffix:
                resolved = path.resolve()
                if resolved not in found:
                    found.append(resolved)
        return found[0] if len(found) == 1 else None

    result: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    for target_reference in sorted(mesh_references):
        target_path = resolve_content(target_reference, ".mesh")
        if (
            target_path is None
            or target_path in existing_by_mesh
            or target_path in by_mesh
        ):
            continue
        target_posix = Path(target_reference)
        directory = target_posix.parent.name
        stem = target_posix.stem
        dir_match = re.fullmatch(r"(.+)_([0-9]+)", directory)
        stem_match = re.fullmatch(r"(.+)_([0-9]+)", stem)
        if (
            dir_match is None
            or stem_match is None
            or dir_match.group(2) != stem_match.group(2)
        ):
            continue
        parent_dir_name = dir_match.group(1)
        parent_stem = stem_match.group(1)
        parent_reference = str(
            target_posix.parent.parent
            / parent_dir_name
            / f"{parent_stem}.mesh"
        ).replace("\\", "/")
        if parent_reference not in mesh_references:
            continue

        target_dir_prefix = str(target_posix.parent).replace("\\", "/") + "/"
        if any(
            reference.startswith(target_dir_prefix)
            for reference in image_references
        ):
            continue

        target_gim_reference = target_reference[:-5] + ".gim"
        parent_gim_reference = parent_reference[:-5] + ".gim"
        target_gim_path = resolve_content(target_gim_reference, ".xml")
        parent_gim_path = resolve_content(parent_gim_reference, ".xml")
        parent_path = resolve_content(parent_reference, ".mesh")
        if (
            target_gim_path is None
            or parent_gim_path is None
            or parent_path is None
        ):
            continue
        parent_package = existing_by_mesh.get(parent_path)
        if (
            parent_package is None
            or parent_package.confidence not in TRUSTED_MATERIAL_CONFIDENCE
            or "部分材质" in parent_package.confidence
        ):
            continue
        target_submeshes = parse_gim_submeshes(target_gim_path)
        parent_submeshes = parse_gim_submeshes(parent_gim_path)
        if not target_submeshes or len(parent_submeshes) < len(target_submeshes):
            continue
        try:
            if len(parse_mesh(target_path).submeshes) != len(target_submeshes):
                continue
        except Exception:
            continue
        if len(parent_package.materials) != len(parent_submeshes):
            continue
        target_layout = tuple(
            (_normalized_material_name(item.name), item.material_index)
            for item in target_submeshes
        )
        parent_layout = tuple(
            (_normalized_material_name(item.name), item.material_index)
            for item in parent_submeshes[: len(target_submeshes)]
        )
        if target_layout != parent_layout:
            continue

        target_materials = [
            MaterialDefinition(material.name, dict(material.textures))
            for material in parent_package.materials[: len(target_submeshes)]
        ]
        references = _ordered_texture_references(target_materials)
        main_references = {
            primary
            for material in target_materials
            if (primary := material_primary_texture(material))
        }
        resolved_main = {
            reference
            for reference in main_references
            if reference in parent_package.texture_map
        }
        if not main_references or not resolved_main:
            continue
        if all(
            reference in parent_package.texture_map
            for reference in set(references)
        ):
            confidence = "res派生GIM子集精确"
        elif len(resolved_main) == len(main_references):
            confidence = "res派生GIM子集精确主贴图"
        else:
            confidence = "res派生GIM子集精确部分主贴图"
        package = MaterialPackage(
            xml_path=parent_package.xml_path,
            index=parent_package.index,
            package_name=stem,
            materials=target_materials,
            mesh_paths=[target_path],
            texture_map=dict(parent_package.texture_map),
            confidence=confidence,
        )
        result.append(package)
        by_mesh[target_path] = package
    return result, by_mesh


def _build_nested_gim_material_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    thd_dir: Path,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """恢复 THP 父依赖中的“子 GIM -> 子 MaterialGroup”模型。

    主 THD 循环只把 THP parent 当作 GIM 入口，但实际资源常把附件写成：
    parent GIM -> child GIM -> child MaterialGroup。child GIM 自身的 Mesh= 明文
    是稳定逻辑路径；这里要求它精确哈希回当前 Mesh，MtlIdx 能完整展开唯一材质
    签名，并且所有主贴图都可解析，才加入自动绑定/材质变体。
    """
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thp,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    thx_path = thd_dir / "model.thx"
    thp_path = thd_dir / "model.thp"
    if not thx_path.is_file() or not thp_path.is_file():
        return [], {}
    try:
        records = read_model_thx(thx_path)
        dependencies = read_model_thp(thp_path)
        seeds = read_thx_namehash_seeds(thx_path)
    except Exception:
        return [], {}

    record_by_hash = {record.name_hash: record for record in records}
    resolver = CrossPackageTextureResolver(thd_dir, model_folder, by_md5)
    material_cache: dict[Path, list[MaterialDefinition]] = {}
    gim_cache: dict[Path, list[GimSubmesh]] = {}
    mesh_count_cache: dict[Path, int | None] = {}
    candidates_by_mesh: dict[Path, list[MaterialPackage]] = {}

    for dependency_hashes in dependencies.values():
        entries: list[tuple[object, Path]] = []
        for dependency_hash in dependency_hashes:
            record = record_by_hash.get(dependency_hash)
            path = by_md5.get(record.content_md5) if record is not None else None
            if record is not None and path is not None:
                entries.append((record, path.resolve()))

        for index, (record, gim_path) in enumerate(entries):
            if (
                getattr(record, "kind", None) not in {1, 15}
                or gim_path.suffix.lower() != ".xml"
            ):
                continue
            if gim_path not in gim_cache:
                gim_cache[gim_path] = parse_gim_submeshes(gim_path)
            gim_submeshes = gim_cache[gim_path]
            if not gim_submeshes:
                continue
            mesh_reference = parse_gim_mesh_reference(gim_path)
            if not mesh_reference:
                continue

            mesh_record = None
            for seed in seeds:
                candidate = record_by_hash.get(
                    cloudfilesys_name_hash(mesh_reference, "model", seed)
                )
                if candidate is not None:
                    mesh_record = candidate
                    break
            if mesh_record is None:
                continue
            mesh_path = by_md5.get(mesh_record.content_md5)
            if mesh_path is None or mesh_path.suffix.lower() != ".mesh":
                continue
            mesh_path = mesh_path.resolve()
            if mesh_path not in mesh_count_cache:
                try:
                    mesh_count_cache[mesh_path] = read_mesh_submesh_count(mesh_path)
                except Exception:
                    mesh_count_cache[mesh_path] = None
            if mesh_count_cache[mesh_path] != len(gim_submeshes):
                continue

            end = len(entries)
            for next_index in range(index + 1, len(entries)):
                next_record, next_path = entries[next_index]
                if (
                    next_path.suffix.lower() in {".mesh", ".skeleton"}
                    or (
                        getattr(next_record, "kind", None) in {1, 15}
                        and next_path.suffix.lower() == ".xml"
                        and bool(parse_gim_submeshes(next_path))
                    )
                ):
                    end = next_index
                    break

            material_choices: dict[
                tuple[object, ...],
                tuple[Path, list[MaterialDefinition]],
            ] = {}
            for _, material_path in entries[index + 1 : end]:
                if material_path.suffix.lower() != ".xml":
                    continue
                if material_path not in material_cache:
                    material_cache[material_path] = parse_material_xml(material_path)
                source_materials = material_cache[material_path]
                if not source_materials:
                    continue
                ordered = order_materials_by_gim(source_materials, gim_submeshes)
                if len(ordered) != len(gim_submeshes):
                    continue
                signature = _material_variant_signature(ordered)
                material_choices.setdefault(signature, (material_path, ordered))
            if len(material_choices) != 1:
                continue

            material_path, ordered_materials = next(iter(material_choices.values()))
            references = _ordered_texture_references(ordered_materials)
            texture_map: dict[str, Path] = {}
            for reference in references:
                texture_path = None
                for seed in seeds:
                    texture_record = record_by_hash.get(
                        cloudfilesys_name_hash(reference, "model", seed)
                    )
                    candidate = (
                        by_md5.get(texture_record.content_md5)
                        if texture_record is not None else None
                    )
                    if candidate is not None and candidate.suffix.lower() == ".ktx":
                        texture_path = candidate.resolve()
                        break
                if texture_path is None:
                    resolved = resolver.resolve(reference)
                    if resolved is not None and resolved.suffix.lower() == ".ktx":
                        texture_path = resolved.resolve()
                if texture_path is not None:
                    texture_map[reference] = texture_path

            main_references = {
                primary
                for material in ordered_materials
                if (primary := material_primary_texture(material))
            }
            has_pure_color = any(
                material.diffuse_color is not None for material in ordered_materials
            )
            if main_references:
                if any(reference not in texture_map for reference in main_references):
                    continue
            elif not has_pure_color:
                continue

            package = MaterialPackage(
                xml_path=material_path,
                index=archive_index(material_path) or 0,
                package_name=Path(mesh_reference).stem,
                materials=ordered_materials,
                mesh_paths=[mesh_path],
                texture_map=texture_map,
                confidence="THD-nested-gim-exact",
            )
            candidates_by_mesh.setdefault(mesh_path, []).append(package)

    result: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    for mesh_path, candidates in candidates_by_mesh.items():
        grouped: dict[tuple[object, ...], list[MaterialPackage]] = {}
        for package in candidates:
            signature = _material_variant_signature(package.materials)
            grouped.setdefault(signature, []).append(package)
        representatives = [
            max(choices, key=lambda item: len(item.texture_map))
            for choices in grouped.values()
        ]
        result.extend(representatives)
        if len(representatives) == 1 and mesh_path not in existing_by_mesh:
            by_mesh[mesh_path] = representatives[0]

    return result, by_mesh


def _build_single_submesh_direct_texture_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    thd_dir: Path,
    minimum_size: int = 100 * 1024,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """恢复“单子网格 + GIM 直挂唯一 KTX”的派生资源。

    只接受同 stem Mesh/GIM 都能由当前 THX 精确解析、GIM 依赖中没有
    MaterialGroup 且恰好只有一张实际 KTX 的情况。单子网格不存在材质槽
    排序歧义；图片路径还必须能由当前 res 路径表反查，且不能是法线、
    mask、mix、noise 等明显辅助纹理。
    """
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thp,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    thx_path = thd_dir / "model.thx"
    thp_path = thd_dir / "model.thp"
    if not thx_path.is_file() or not thp_path.is_file():
        return [], {}
    try:
        records = read_model_thx(thx_path)
        dependencies = read_model_thp(thp_path)
        seeds = read_thx_namehash_seeds(thx_path)
    except Exception:
        return [], {}

    record_by_hash = {record.name_hash: record for record in records}
    asset_paths = load_res_asset_paths(thd_dir, model_folder)
    if not asset_paths:
        return [], {}

    image_refs_by_md5: dict[str, list[str]] = {}
    for reference in asset_paths:
        normalized = reference.strip().replace("\\", "/").lower()
        if Path(normalized).suffix.lower() not in IMAGE_SUFFIXES:
            continue
        for seed in seeds:
            record = record_by_hash.get(
                cloudfilesys_name_hash(reference, "model", seed)
            )
            if record is None:
                continue
            path = by_md5.get(record.content_md5)
            if path is not None and path.suffix.lower() == ".ktx":
                refs = image_refs_by_md5.setdefault(record.content_md5, [])
                if reference not in refs:
                    refs.append(reference)
            break

    def is_auxiliary_texture(reference: str) -> bool:
        normalized = reference.strip().replace("\\", "/").lower()
        stem = normalized.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        markers = (
            "normal",
            "_n_",
            "_n",
            "byg",
            "mask",
            "mix",
            "noise",
            "rough",
            "metal",
            "emiss",
            "specular",
            "_ao",
            "_yy",
            "star",
            "ramp",
        )
        return any(marker in stem for marker in markers)

    result: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    material_cache: dict[Path, bool] = {}

    mesh_references = sorted({
        reference
        for reference in asset_paths
        if reference.startswith("model/") and reference.endswith(".mesh")
    })
    for mesh_reference in mesh_references:
        mesh_record = None
        for seed in seeds:
            mesh_record = record_by_hash.get(
                cloudfilesys_name_hash(mesh_reference, "model", seed)
            )
            if mesh_record is not None:
                break
        if mesh_record is None:
            continue
        mesh_path = by_md5.get(mesh_record.content_md5)
        if (
            mesh_path is None
            or mesh_path.suffix.lower() != ".mesh"
            or mesh_path.stat().st_size < minimum_size
        ):
            continue
        mesh_path = mesh_path.resolve()
        if mesh_path in existing_by_mesh or mesh_path in by_mesh:
            continue
        try:
            submesh_count = read_mesh_submesh_count(mesh_path)
            if submesh_count < 1:
                continue
        except Exception:
            continue

        gim_reference = mesh_reference[:-5] + ".gim"
        gim_hash = None
        gim_record = None
        for seed in seeds:
            candidate_hash = cloudfilesys_name_hash(gim_reference, "model", seed)
            candidate = record_by_hash.get(candidate_hash)
            if candidate is not None:
                gim_hash = candidate_hash
                gim_record = candidate
                break
        if gim_hash is None or gim_record is None:
            continue

        gim_path = by_md5.get(gim_record.content_md5)
        gim_submeshes: list[GimSubmesh] = []
        if gim_path is not None and gim_path.suffix.lower() == ".xml":
            gim_submeshes = parse_gim_submeshes(gim_path)
        if submesh_count == 1:
            if gim_submeshes and len(gim_submeshes) != 1:
                continue
        else:
            # 多子网格只有在 GIM 明确显示所有槽共用同一个 MtlIdx 时才允许
            # 继承唯一 KTX；否则“唯一纹理依赖”不足以证明每个槽的材质关系。
            if (
                len(gim_submeshes) != submesh_count
                or len({item.material_index for item in gim_submeshes}) != 1
            ):
                continue

        ktx_paths: list[tuple[str, Path]] = []
        has_material_group = False
        for dependency_hash in dependencies.get(gim_hash, []):
            record = record_by_hash.get(dependency_hash)
            path = by_md5.get(record.content_md5) if record is not None else None
            if path is None:
                continue
            if path.suffix.lower() == ".ktx":
                ktx_paths.append((record.content_md5, path.resolve()))
            elif path.suffix.lower() == ".xml":
                if path not in material_cache:
                    material_cache[path] = bool(parse_material_xml(path))
                if material_cache[path]:
                    has_material_group = True
                    break
        if has_material_group or len(ktx_paths) != 1:
            continue

        texture_md5, texture_path = ktx_paths[0]
        references = image_refs_by_md5.get(texture_md5, [])
        if not references:
            continue
        mesh_dir = mesh_reference.rsplit("/", 1)[0].lower()
        same_dir = [
            reference
            for reference in references
            if reference.replace("\\", "/").rsplit("/", 1)[0].lower() == mesh_dir
        ]
        if len(same_dir) == 1:
            texture_reference = same_dir[0]
        elif len(references) == 1:
            texture_reference = references[0]
        else:
            continue
        if is_auxiliary_texture(texture_reference):
            continue

        stem = Path(mesh_reference).stem
        materials = [
            MaterialDefinition(
                stem if submesh_count == 1 else f"{stem}_{index}",
                {"Tex0": texture_reference},
            )
            for index in range(submesh_count)
        ]
        package = MaterialPackage(
            xml_path=(
                gim_path.resolve()
                if gim_path is not None and gim_path.is_file()
                else texture_path
            ),
            index=archive_index(mesh_path) or 0,
            package_name=stem,
            materials=materials,
            mesh_paths=[mesh_path],
            texture_map={texture_reference: texture_path},
            confidence=(
                "THD-single-slot-direct-texture"
                if submesh_count == 1
                else "THD-uniform-direct-texture"
            ),
        )
        result.append(package)
        by_mesh[mesh_path] = package

    return result, by_mesh


def _build_gim_alias_name_material_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    thd_dir: Path,
    material_cache: dict[Path, list[MaterialDefinition]] | None = None,
    asset_paths: list[str] | None = None,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """恢复 kind=15“别名/代理 GIM”直接指向的白模材质。

    这一类资源是当前版本最容易被误判为“只能人工一只一只补”的根源：
    q_*_wuqi.mesh 等 Mesh 有精确 res logical path，GIM 也存在，但 GIM 本身
    没有 THP 材质依赖；真正的 MaterialGroup 在同目录的另一条官方 GIM 关系
    中。此时不能按目录第一张图猜材质，而是利用官方 MaterialGroup 的父 GIM
    建立“GIM 子网格名 -> Material 名”的语义索引，再让目标 kind=15 GIM 的
    子网格名唯一命中材质槽。

    只接受同时满足以下条件的目标：
      1. Mesh logical path 可由当前 THX 精确反查；
      2. 同 stem GIM 精确存在且 kind=15；
      3. 目标仍未被更强的官方依赖规则绑定；
      4. 目标 GIM 子网格数与 Mesh 子网格数一致；
      5. 候选 MaterialGroup 本身来自当前 THX 官方资源池；若目标仍残留
         THP 子依赖，则还要求它的官方父 GIM 能逐槽证明
         “子网格名 -> Material 名”；
      6. 每个目标子网格名只命中一个候选 Material；
      7. 所有被选主贴图都能由 THX 精确解析。

    这样新增资源只需重新扫描索引，不需要新增资源名白名单。
    """
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thp,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    thx_path = thd_dir / "model.thx"
    thp_path = thd_dir / "model.thp"
    if not thx_path.is_file() or not thp_path.is_file():
        return [], {}
    try:
        records = read_model_thx(thx_path)
        dependencies = read_model_thp(thp_path)
        seeds = read_thx_namehash_seeds(thx_path)
    except Exception:
        return [], {}
    record_by_hash = {record.name_hash: record for record in records}

    if material_cache is None:
        material_cache = {}
    semantic_materials: dict[str, list[tuple[Path, list[MaterialDefinition]]]] = {}
    # 先锁定真正可能进入本规则的 kind=15 目标，再按目标目录反查
    # MaterialGroup。不要为了这 20~几十个候选扫描整个 XML 资源宇宙。
    target_entries: list[
        tuple[str, Path, Path, list[GimSubmesh], bool]
    ] = []
    if asset_paths is None:
        asset_paths = load_res_asset_paths(thd_dir, model_folder)
    for raw_reference in sorted(asset_paths):
        reference = raw_reference.strip().replace("\\", "/")
        if not reference.startswith("model/") or not reference.endswith(".mesh"):
            continue
        mesh_record = None
        for seed in seeds:
            candidate = record_by_hash.get(
                cloudfilesys_name_hash(reference, "model", seed)
            )
            if candidate is not None:
                mesh_record = candidate
                break
        if mesh_record is None:
            continue
        mesh_path = by_md5.get(mesh_record.content_md5)
        if mesh_path is None or mesh_path.suffix.lower() != ".mesh":
            continue
        mesh_path = mesh_path.resolve()
        if mesh_path in existing_by_mesh:
            continue
        gim_reference = reference[:-5] + ".gim"
        gim_record = None
        for seed in seeds:
            candidate = record_by_hash.get(
                cloudfilesys_name_hash(gim_reference, "model", seed)
            )
            if candidate is not None:
                gim_record = candidate
                break
        if gim_record is None or gim_record.kind != 15:
            continue
        gim_path = by_md5.get(gim_record.content_md5)
        if gim_path is None or gim_path.suffix.lower() != ".xml":
            continue
        gim_submeshes = parse_gim_submeshes(gim_path)
        if not gim_submeshes:
            continue
        try:
            if len(gim_submeshes) != read_mesh_submesh_count(mesh_path):
                continue
        except Exception:
            continue
        target_entries.append(
            (
                reference,
                mesh_path,
                gim_path.resolve(),
                gim_submeshes,
                bool(dependencies.get(gim_record.name_hash)),
            )
        )
    if not target_entries:
        return [], {}
    target_directories = {
        reference.rsplit("/", 1)[0].lower() + "/"
        for reference, _, _, _, _ in target_entries
    }

    def semantic_name(value: str) -> str:
        value = value.strip().replace("\\", "/").rsplit("/", 1)[-1]
        value = value.rsplit(".", 1)[0].lstrip("@/").lower()
        value = re.sub(r"(?:_lod\\d+|_lod|_d)$", "", value)
        return re.sub(r"[^0-9a-z]+", "", value)

    def material_name_matches(submesh_name: str, material_name: str) -> bool:
        sub = semantic_name(submesh_name)
        mat = semantic_name(material_name)
        if not sub or not mat:
            return False
        if sub == mat:
            return True
        # 游戏常把派生 GIM 子网格写成 foo_01，而 Material 仍叫 foo。
        # 只允许较长的基础名做包含匹配，避免“a”之类短名误命中。
        if len(mat) >= 5 and mat in sub:
            return True
        if len(sub) >= 5 and sub in mat:
            return True
        return False

    for material_path, materials in material_cache.items():
        material_path = material_path.resolve()
        if not materials:
            continue
        primary_refs = [material_primary_texture(item) for item in materials]
        if any(not reference for reference in primary_refs):
            continue
        directories = {
            reference.strip().replace("\\", "/").lower().rsplit("/", 1)[0] + "/"
            for reference in primary_refs
            if "/" in reference.replace("\\", "/")
        }
        if len(directories) != 1:
            continue
        directory = next(iter(directories))
        if directory in target_directories:
            semantic_materials.setdefault(directory, []).append(
                (material_path, materials)
            )

    # 目标 GIM 可能残留旧 Mesh/材质依赖，但那些依赖未必还能组成完整包。
    # 只对这类新增目标的同目录候选追官方父 GIM；不要让已有的无依赖
    # kind=15 基线多做几千次路径规范化或 XML 解析。
    proof_directories = {
        reference.rsplit("/", 1)[0].lower() + "/"
        for reference, _, _, _, requires_proof in target_entries
        if requires_proof
    }
    proof_materials = {
        material_path: materials
        for directory in proof_directories
        for material_path, materials in semantic_materials.get(directory, [])
    }
    proof_candidates = set(proof_materials)
    records_by_md5: dict[str, list[object]] = defaultdict(list)
    reverse_dependencies: dict[int, list[int]] = defaultdict(list)
    for record in records:
        records_by_md5[record.content_md5].append(record)
    for parent_hash, child_hashes in dependencies.items():
        for child_hash in child_hashes:
            reverse_dependencies[child_hash].append(parent_hash)
    candidate_by_key = {
        os.path.normcase(str(path)): path for path in proof_candidates
    }
    md5_by_candidate: dict[Path, str] = {}
    for digest, path in by_md5.items():
        candidate = candidate_by_key.get(os.path.normcase(str(path)))
        if candidate is not None:
            md5_by_candidate[candidate] = digest
    trusted_semantic_material_paths: set[Path] = set()
    for material_path in proof_candidates:
        materials = proof_materials[material_path]
        digest = md5_by_candidate.get(material_path)
        if not digest or not materials:
            continue
        for material_record in records_by_md5.get(digest, []):
            for parent_hash in reverse_dependencies.get(material_record.name_hash, []):
                parent_record = record_by_hash.get(parent_hash)
                parent_path = (
                    by_md5.get(parent_record.content_md5)
                    if parent_record is not None else None
                )
                if parent_path is None or parent_path.suffix.lower() != ".xml":
                    continue
                parent_submeshes = parse_gim_submeshes(parent_path)
                if len(parent_submeshes) != len(materials):
                    continue
                ordered = order_materials_by_gim(materials, parent_submeshes)
                if len(ordered) != len(materials):
                    continue
                if all(
                    material_name_matches(submesh.name, material.name)
                    for submesh, material in zip(parent_submeshes, ordered)
                ):
                    trusted_semantic_material_paths.add(material_path)
                    break
            if material_path in trusted_semantic_material_paths:
                break

    result: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}

    def resolve_texture_reference(reference: str) -> Path | None:
        for seed in seeds:
            record = record_by_hash.get(
                cloudfilesys_name_hash(reference, "model", seed)
            )
            path = by_md5.get(record.content_md5) if record is not None else None
            if path is not None and path.suffix.lower() == ".ktx":
                return path.resolve()
        return None

    for (
        reference,
        mesh_path,
        _gim_path,
        gim_submeshes,
        requires_parent_proof,
    ) in target_entries:
        if mesh_path in existing_by_mesh or mesh_path in by_mesh:
            continue
        directory = reference.rsplit("/", 1)[0].lower() + "/"
        candidates = semantic_materials.get(directory, [])
        if not candidates:
            continue
        proposals: dict[
            tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
            tuple[Path, list[MaterialDefinition]],
        ] = {}
        for material_path, materials in candidates:
            if (
                requires_parent_proof
                and material_path not in trusted_semantic_material_paths
            ):
                continue
            selected_indices: list[int] = []
            valid = True
            for submesh in gim_submeshes:
                matches = [
                    index
                    for index, material in enumerate(materials)
                    if material_name_matches(submesh.name, material.name)
                ]
                if len(matches) != 1:
                    valid = False
                    break
                selected_indices.append(matches[0])
            if not valid:
                continue
            selected = [materials[index] for index in selected_indices]
            selected_primary = {
                primary
                for material in selected
                if (primary := material_primary_texture(material))
            }
            if not selected_primary:
                continue
            texture_map: dict[str, Path] = {}
            for material in selected:
                for texture_reference in material.textures.values():
                    if not texture_reference:
                        continue
                    resolved = resolve_texture_reference(texture_reference)
                    if resolved is not None:
                        texture_map[texture_reference] = resolved
            if any(reference not in texture_map for reference in selected_primary):
                continue
            signature = tuple(
                (
                    material.name,
                    tuple(sorted(material.textures.items())),
                )
                for material in selected
            )
            proposals.setdefault(signature, (material_path, selected))

        if len(proposals) != 1:
            continue
        material_path, selected = next(iter(proposals.values()))
        texture_map: dict[str, Path] = {}
        for material in selected:
            for texture_reference in material.textures.values():
                if not texture_reference:
                    continue
                resolved = resolve_texture_reference(texture_reference)
                if resolved is not None:
                    texture_map[texture_reference] = resolved
        package = MaterialPackage(
            xml_path=material_path,
            index=archive_index(material_path) or 0,
            package_name=Path(reference).stem,
            materials=[
                MaterialDefinition(material.name, dict(material.textures))
                for material in selected
            ],
            mesh_paths=[mesh_path],
            texture_map=texture_map,
            confidence="THD-GIM-name-material",
        )
        result.append(package)
        by_mesh[mesh_path] = package
    return result, by_mesh


def _build_unique_logical_single_image_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    thd_dir: Path,
    asset_paths: list[str] | None = None,
    minimum_size: int = 100_000,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """Recover a single-slot Mesh from its exact same-stem image identity.

    This is a generic fallback for hot-update assets whose GIM/MaterialGroup was
    removed. It does not choose the first image in a directory. The Mesh and image
    must both resolve through the current THX, the Mesh must have one material slot,
    all logical aliases must agree on one directory, and exactly one
    non-auxiliary image must have a normalized stem equal to one Mesh alias.

    A held-out benchmark against surviving official GIM/MaterialGroup truth currently
    gives 31/31 exact primary-texture matches. Multi-slot assets never enter this rule.

    If the unique image does not share the Mesh stem, it is accepted only when the
    same image basename is already an official Tex0 of a related logical family and
    a source submesh has at least 70% bidirectional UV-triangle overlap.  This covers
    generated summons/attachments while keeping directory proximity alone insufficient.
    """
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    thx_path = thd_dir / "model.thx"
    if not thx_path.is_file():
        return [], {}
    try:
        records = read_model_thx(thx_path)
        seeds = read_thx_namehash_seeds(thx_path)
        current_by_md5, _ = _manifest_hash_maps(
            model_folder,
            include_auxiliary=False,
        )
    except Exception:
        return [], {}
    record_by_hash = {record.name_hash: record for record in records}
    if asset_paths is None:
        asset_paths = load_res_asset_paths(thd_dir, model_folder)

    def norm(value: str) -> str:
        return value.strip().replace("\\", "/").lower()

    def stem_key(value: str) -> str:
        stem = Path(norm(value)).stem
        stem = re.sub(r"(?:_ktx|_texture|_tex|_diffuse)$", "", stem)
        return re.sub(r"[^0-9a-z]+", "", stem)

    def auxiliary(value: str) -> bool:
        stem = Path(norm(value)).stem
        markers = (
            "normal", "_n", "byg", "mask", "mix", "noise", "rough",
            "metal", "emiss", "specular", "_ao", "_yy", "star", "ramp",
            "shadow", "detail", "flow", "distort", "light",
        )
        return any(marker in stem for marker in markers)

    def related_directories(target: str, source: str) -> bool:
        target_name = Path(target.rstrip("/")).name
        source_name = Path(source.rstrip("/")).name
        return (
            target_name == source_name
            or target_name.startswith(source_name + "_")
            or source_name.startswith(target_name + "_")
        )

    parsed_meshes: dict[Path, ParsedMesh] = {}
    uv_cache: dict[tuple[Path, int], set[tuple[object, ...]]] = {}

    def submesh_uv_triangles(
        mesh_path: Path,
        submesh_index: int,
    ) -> set[tuple[object, ...]]:
        key = (mesh_path, submesh_index)
        cached = uv_cache.get(key)
        if cached is not None:
            return cached
        mesh = parsed_meshes.get(mesh_path)
        if mesh is None:
            mesh = parse_mesh(mesh_path)
            parsed_meshes[mesh_path] = mesh
        face_offset = sum(item[1] for item in mesh.submeshes[:submesh_index])
        face_count = mesh.submeshes[submesh_index][1]
        triangles = {
            tuple(sorted(
                tuple(round(value, 5) for value in mesh.uvs[index])
                for index in face
            ))
            for face in mesh.faces[face_offset : face_offset + face_count]
        }
        uv_cache[key] = triangles
        return triangles

    logical_by_mesh: dict[Path, set[str]] = defaultdict(set)
    images_by_directory: dict[str, list[tuple[str, Path]]] = defaultdict(list)
    for raw_reference in asset_paths:
        reference = norm(raw_reference)
        if not reference.startswith("model/") or "/" not in reference:
            continue
        suffix = Path(reference).suffix.lower()
        if suffix != ".mesh" and suffix not in IMAGE_SUFFIXES:
            continue
        for seed in seeds:
            record = record_by_hash.get(
                cloudfilesys_name_hash(reference, "model", seed)
            )
            path = current_by_md5.get(record.content_md5) if record else None
            if path is None:
                continue
            if suffix == ".mesh" and path.suffix.lower() == ".mesh":
                logical_by_mesh[path.resolve()].add(reference)
            elif suffix in IMAGE_SUFFIXES and path.suffix.lower() == ".ktx":
                directory = reference.rsplit("/", 1)[0] + "/"
                item = (reference, path.resolve())
                if item not in images_by_directory[directory]:
                    images_by_directory[directory].append(item)
            break

    result: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    uv_candidate_basenames: set[str] = set()
    for mesh_path, logicals in logical_by_mesh.items():
        if mesh_path in existing_by_mesh:
            continue
        directories = {logical.rsplit("/", 1)[0] + "/" for logical in logicals}
        if len(directories) != 1:
            continue
        candidate_images = [
            item for item in images_by_directory.get(next(iter(directories)), [])
            if not auxiliary(item[0])
        ]
        if len(candidate_images) != 1:
            continue
        candidate_reference = candidate_images[0][0]
        if any(
            stem_key(logical) == stem_key(candidate_reference)
            for logical in logicals
        ):
            continue
        uv_candidate_basenames.add(Path(norm(candidate_reference)).name)
    official_slots_by_basename: dict[
        str, list[tuple[Path, int, str]]
    ] = defaultdict(list)
    for source_mesh, source_package in existing_by_mesh.items():
        matching_primaries = [
            (source_index, primary)
            for source_index, source_material in enumerate(source_package.materials)
            if (primary := material_primary_texture(source_material))
            and Path(norm(primary)).name in uv_candidate_basenames
        ]
        if not matching_primaries:
            continue
        try:
            if read_mesh_submesh_count(source_mesh) != len(source_package.materials):
                continue
        except (OSError, MeshFormatError, ValueError):
            continue
        for source_index, primary in matching_primaries:
            if "/" not in norm(primary):
                continue
            official_slots_by_basename[Path(norm(primary)).name].append(
                (source_mesh, source_index, norm(primary))
            )
    for mesh_path, logicals in logical_by_mesh.items():
        if mesh_path in existing_by_mesh:
            continue
        try:
            if (
                mesh_path.stat().st_size < minimum_size
                or read_mesh_submesh_count(mesh_path) != 1
            ):
                continue
        except (OSError, MeshFormatError, ValueError):
            continue
        directories = {logical.rsplit("/", 1)[0] + "/" for logical in logicals}
        if len(directories) != 1:
            continue
        directory = next(iter(directories))
        images = [
            item for item in images_by_directory.get(directory, [])
            if not auxiliary(item[0])
        ]
        # Other non-auxiliary images may be expression/variant textures.  They
        # are irrelevant when exactly one logical Mesh alias selects one image.
        matching_pairs = [
            (logical, image)
            for logical in logicals
            for image in images
            if stem_key(logical) == stem_key(image[0])
        ]
        confidence = "THD-unique-logical-image"
        if len(matching_pairs) == 1:
            logical, (image_reference, image_path) = matching_pairs[0]
        else:
            if len(images) != 1:
                continue
            image_reference, image_path = images[0]
            basename = Path(norm(image_reference)).name
            target_uv = submesh_uv_triangles(mesh_path, 0)
            if not target_uv:
                continue
            best_score = 0.0
            for source_mesh, source_index, source_primary in (
                official_slots_by_basename.get(basename, [])
            ):
                source_directory = source_primary.rsplit("/", 1)[0] + "/"
                if not related_directories(directory, source_directory):
                    continue
                try:
                    source_uv = submesh_uv_triangles(source_mesh, source_index)
                except (OSError, MeshFormatError, ValueError, IndexError):
                    continue
                if not source_uv:
                    continue
                common = len(target_uv & source_uv)
                score = min(
                    common / len(target_uv),
                    common / len(source_uv),
                )
                best_score = max(best_score, score)
            if best_score < 0.70:
                continue
            logical = sorted(logicals)[0]
            confidence = "THD-unique-directory-image-UV"
        material = MaterialDefinition(
            Path(logical).stem,
            {"Tex0": image_reference},
        )
        package = MaterialPackage(
            xml_path=image_path,
            index=archive_index(mesh_path) or 0,
            package_name=Path(logical).stem,
            materials=[material],
            mesh_paths=[mesh_path],
            texture_map={image_reference: image_path},
            confidence=confidence,
        )
        result.append(package)
        by_mesh[mesh_path] = package
    return result, by_mesh


def _build_supplemental_logical_material_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    thd_dir: Path,
    asset_paths: list[str],
    minimum_size: int = 0,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """Recover extra-WPK Mesh materials from package-local THX identities.

    Supplemental groups do not ship a THP dependency table.  This resolver
    therefore accepts only convergent structures: exact same-stem single image,
    same-stem GIM, or a cached GIM whose explicit Mesh= path resolves through
    the same package THX.  GIM MtlIdx/name sequence must uniquely select one
    MaterialGroup in the same logical texture directory.
    """
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    resolver = CrossPackageTextureResolver(thd_dir, model_folder, by_md5)
    fx_tex0_bindings = load_fx_tex0_bindings(thd_dir, model_folder)
    result: list[MaterialPackage] = []
    result_by_mesh: dict[Path, MaterialPackage] = {}

    def norm(value: str) -> str:
        return value.strip().replace("\\", "/").lower()

    def stem_key(value: str) -> str:
        stem = Path(norm(value)).stem
        stem = re.sub(r"(?:_ktx|_texture|_tex|_diffuse)$", "", stem)
        return re.sub(r"[^0-9a-z]+", "", stem)

    for stem, prefix in (
        ("fx_model", "fx/"),
        ("levelsets", "levelsets/"),
        ("static", "static/"),
        ("res", ("res/", "model/", "levelsets/", "static/", "fx/", "natural/", "npcmodel/")),
    ):
        thx_path = thd_dir / f"{stem}.thx"
        cache_root = model_folder.parent / "extra_rigged" / stem
        material_manifest = cache_root / "material_manifest.csv"
        if not thx_path.is_file() or not material_manifest.is_file():
            continue
        try:
            records = read_model_thx(thx_path)
            seeds = read_thx_namehash_seeds(thx_path)
        except Exception:
            continue
        record_by_hash = {record.name_hash: record for record in records}
        logical_by_path: dict[Path, set[str]] = defaultdict(set)
        image_by_directory: dict[str, list[tuple[str, Path]]] = defaultdict(list)
        for raw_reference in asset_paths:
            reference = norm(raw_reference)
            if isinstance(prefix, tuple):
                if not reference.startswith(prefix):
                    continue
            elif not reference.startswith(prefix):
                continue
            for variant in _package_reference_variants(reference, stem):
                matched = False
                for seed in seeds:
                    record = record_by_hash.get(
                        cloudfilesys_name_hash(variant, stem, seed)
                    )
                    path = by_md5.get(record.content_md5) if record else None
                    if path is None:
                        continue
                    path = path.resolve()
                    if Path(reference).suffix.lower() == ".mesh" and path.suffix.lower() == ".mesh":
                        logical_by_path[path].add(reference)
                    elif Path(reference).suffix.lower() in IMAGE_SUFFIXES and path.suffix.lower() == ".ktx":
                        image_by_directory[reference.rsplit("/", 1)[0] + "/"].append((reference, path))
                    elif Path(reference).suffix.lower() == ".gim" and path.suffix.lower() == ".xml":
                        logical_by_path[path].add(reference)
                        # FX descriptors normally reference a GIM while the
                        # package THX stores the sibling Mesh under the same
                        # stem.  Recover that explicit sibling identity; it
                        # is still a THX hash lookup, never a directory guess.
                        mesh_reference = reference[:-4] + ".mesh"
                        for mesh_variant in _package_reference_variants(mesh_reference, stem):
                            for mesh_seed in seeds:
                                mesh_record = record_by_hash.get(
                                    cloudfilesys_name_hash(mesh_variant, stem, mesh_seed)
                                )
                                mesh_path = (
                                    by_md5.get(mesh_record.content_md5)
                                    if mesh_record is not None
                                    else None
                                )
                                if mesh_path is not None and mesh_path.suffix.lower() == ".mesh":
                                    logical_by_path[mesh_path.resolve()].add(mesh_reference)
                                    break
                            else:
                                continue
                            break
                    matched = True
                    break
                if matched:
                    break

        material_pool: dict[str, list[tuple[Path, list[MaterialDefinition]]]] = defaultdict(list)
        embedded_gims: list[tuple[Path, str, list[GimSubmesh]]] = []
        try:
            manifest_rows = list(csv.DictReader(material_manifest.open("r", newline="", encoding="utf-8-sig")))
        except OSError:
            manifest_rows = []
        for row in manifest_rows:
            output = (row.get("output_path") or "").strip()
            if row.get("status") != "ok" or not output.endswith(".xml"):
                continue
            path = (cache_root / Path(output.replace("\\", "/"))).resolve()
            gim_submeshes = parse_gim_submeshes(path)
            mesh_reference = parse_gim_mesh_reference(path)
            if gim_submeshes and mesh_reference:
                embedded_gims.append((path, norm(mesh_reference), gim_submeshes))
            materials = parse_material_xml(path)
            if not materials:
                continue
            primary = [material_primary_texture(item) for item in materials]
            directories = {
                norm(reference).rsplit("/", 1)[0] + "/"
                for reference in primary if reference and "/" in norm(reference)
            }
            if len(directories) == 1:
                material_pool[next(iter(directories))].append((path, materials))

        gim_by_reference = {
            reference: path
            for path, references in logical_by_path.items()
            if path.suffix.lower() == ".xml"
            for reference in references
            if reference.endswith(".gim")
        }

        # 额外包没有 THP，但 GIM 自己常保存精确 Mesh=".../*.mesh"。
        # 该路径必须经同包 THX name-hash 回查到当前物理 Mesh；材质仍要求
        # MtlIdx 有效、逐槽名字一致、同目录唯一签名、主贴图实体全部存在。
        embedded_proposals: dict[
            Path,
            dict[
                tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
                MaterialPackage,
            ],
        ] = defaultdict(dict)
        for gim_path, mesh_reference, gim_submeshes in embedded_gims:
            mesh_path = None
            for mesh_variant in _package_reference_variants(mesh_reference, stem):
                for mesh_seed in seeds:
                    mesh_record = record_by_hash.get(
                        cloudfilesys_name_hash(mesh_variant, stem, mesh_seed)
                    )
                    candidate_path = (
                        by_md5.get(mesh_record.content_md5)
                        if mesh_record is not None else None
                    )
                    if (
                        candidate_path is not None
                        and candidate_path.suffix.lower() == ".mesh"
                    ):
                        mesh_path = candidate_path.resolve()
                        break
                if mesh_path is not None:
                    break
            if (
                mesh_path is None
                or mesh_path in existing_by_mesh
                or mesh_path in result_by_mesh
            ):
                continue
            try:
                if (
                    mesh_path.stat().st_size < minimum_size
                    or read_mesh_submesh_count(mesh_path) != len(gim_submeshes)
                ):
                    continue
            except Exception:
                continue
            if "/" not in mesh_reference:
                continue
            directory = mesh_reference.rsplit("/", 1)[0] + "/"
            target_names = tuple(
                _normalized_material_name(item.name) for item in gim_submeshes
            )
            for material_path, materials in material_pool.get(directory, []):
                ordered = order_materials_by_gim(materials, gim_submeshes)
                if (
                    len(ordered) != len(gim_submeshes)
                    or tuple(
                        _normalized_material_name(item.name) for item in ordered
                    ) != target_names
                ):
                    continue
                texture_map: dict[str, Path] = {}
                valid = True
                for material in ordered:
                    primary = material_primary_texture(material)
                    if not primary:
                        valid = False
                        break
                    texture_path = resolver.resolve(primary)
                    if texture_path is None:
                        valid = False
                        break
                    texture_map[primary] = texture_path
                if not valid:
                    continue
                package = MaterialPackage(
                    material_path,
                    archive_index(mesh_path) or 0,
                    Path(mesh_reference).stem,
                    ordered,
                    [mesh_path],
                    texture_map,
                    "supplemental-embedded-GIM-exact",
                )
                embedded_proposals[mesh_path].setdefault(
                    _material_variant_signature(ordered), package
                )
        for mesh_path, proposals in embedded_proposals.items():
            if len(proposals) != 1:
                continue
            package = next(iter(proposals.values()))
            result.append(package)
            result_by_mesh[mesh_path] = package

        for mesh_path, logicals in logical_by_path.items():
            if mesh_path.suffix.lower() != ".mesh" or mesh_path in existing_by_mesh or mesh_path in result_by_mesh:
                continue
            try:
                if mesh_path.stat().st_size < minimum_size:
                    continue
                slot_count = read_mesh_submesh_count(mesh_path)
            except Exception:
                continue
            directories = {reference.rsplit("/", 1)[0] + "/" for reference in logicals}
            if len(directories) != 1:
                continue
            directory = next(iter(directories))
            proposals: dict[tuple[tuple[str, tuple[tuple[str, str], ...]], ...], MaterialPackage] = {}
            for logical in logicals:
                gim_path = gim_by_reference.get(logical[:-5] + ".gim")
                gim_submeshes = parse_gim_submeshes(gim_path) if gim_path else []
                if len(gim_submeshes) != slot_count:
                    continue
                target_names = tuple(_normalized_material_name(item.name) for item in gim_submeshes)
                for material_path, materials in material_pool.get(directory, []):
                    ordered = order_materials_by_gim(materials, gim_submeshes)
                    if len(ordered) != slot_count or tuple(_normalized_material_name(item.name) for item in ordered) != target_names:
                        continue
                    texture_map = {}
                    valid = True
                    for material in ordered:
                        primary = material_primary_texture(material)
                        if not primary:
                            valid = False
                            break
                        path = resolver.resolve(primary)
                        if path is None:
                            valid = False
                            break
                        texture_map[primary] = path
                    if not valid:
                        continue
                    package = MaterialPackage(material_path, archive_index(mesh_path) or 0, Path(logical).stem, ordered, [mesh_path], texture_map, "supplemental-GIM-name-material")
                    proposals.setdefault(_material_variant_signature(ordered), package)
            if not proposals and slot_count == 1:
                pairs = [
                    (logical, image)
                    for logical in logicals
                    for image in image_by_directory.get(directory, [])
                    if stem_key(logical) == stem_key(image[0])
                ]
                if len(pairs) == 1:
                    logical, (reference, image_path) = pairs[0]
                    material = MaterialDefinition(Path(logical).stem, {"Tex0": reference})
                    proposals[_material_variant_signature([material])] = MaterialPackage(
                        image_path, archive_index(mesh_path) or 0, Path(logical).stem,
                        [material], [mesh_path], {reference: image_path},
                        "supplemental-unique-logical-image",
                    )
            if not proposals and stem == "fx_model":
                for logical in logicals:
                    gim_reference = logical[:-5] + ".gim"
                    tex0_references = fx_tex0_bindings.get(gim_reference, [])
                    if len(tex0_references) != 1:
                        continue
                    gim_path = gim_by_reference.get(gim_reference)
                    gim_submeshes = (
                        parse_gim_submeshes(gim_path) if gim_path else []
                    )
                    if len(gim_submeshes) != slot_count:
                        continue
                    reference = tex0_references[0]
                    texture_path = resolver.resolve(reference)
                    if texture_path is None:
                        continue
                    materials = [
                        MaterialDefinition(
                            item.name or f"material_{index}",
                            {"Tex0": reference},
                        )
                        for index, item in enumerate(gim_submeshes)
                    ]
                    proposals.setdefault(
                        _material_variant_signature(materials),
                        MaterialPackage(
                            texture_path,
                            archive_index(mesh_path) or 0,
                            Path(logical).stem,
                            materials,
                            [mesh_path],
                            {reference: texture_path},
                            "supplemental-FX-Tex0-exact",
                        ),
                    )
            if (
                not proposals
                and slot_count == 1
                and stem in {"levelsets", "static"}
            ):
                directory_images = image_by_directory.get(directory, [])
                unique_images = {
                    (reference, image_path.resolve())
                    for reference, image_path in directory_images
                }
                if len(unique_images) == 1:
                    reference, image_path = next(iter(unique_images))
                    logical = min(logicals)
                    material = MaterialDefinition(
                        Path(logical).stem, {"Tex0": reference}
                    )
                    proposals[_material_variant_signature([material])] = (
                        MaterialPackage(
                            image_path,
                            archive_index(mesh_path) or 0,
                            Path(logical).stem,
                            [material],
                            [mesh_path],
                            {reference: image_path},
                            "supplemental-unique-directory-image",
                        )
                    )
            if len(proposals) == 1:
                package = next(iter(proposals.values()))
                result.append(package)
                result_by_mesh[mesh_path] = package
    return result, result_by_mesh


def _load_material_directory_variant_index(
    model_folder: Path,
    by_md5: dict[str, Path],
    thd_dir: Path,
) -> dict[str, list[tuple[Path, list[MaterialDefinition]]]]:
    """Index official MaterialGroups by their exact primary-texture directory.

    The first build is intentionally exhaustive.  Later runs reuse a cache tied
    to the current model manifest and THX, so new resources are discovered after
    updates without maintaining character names or content-MD5 allowlists.
    """
    from thd_resource_index import read_model_thx

    thx_path = thd_dir / "model.thx"
    if not thx_path.is_file():
        return {}
    fingerprint = {
        "parser": "material-directory-index-v1",
        "manifest": list(_file_build_stamp(model_folder / "manifest.csv")),
        "thx": list(_file_build_stamp(thx_path)),
        "apk_manifest": list(
            _file_build_stamp(
                model_folder.parent / "apk_model_parents" / "manifest.csv"
            )
        ),
    }
    cache_path = model_folder.parent / "material_directory_index.json"

    def deserialize(entries: object) -> dict[
        str, list[tuple[Path, list[MaterialDefinition]]]
    ]:
        result: dict[
            str, list[tuple[Path, list[MaterialDefinition]]]
        ] = defaultdict(list)
        if not isinstance(entries, dict):
            return result
        for directory, raw_items in entries.items():
            if not isinstance(raw_items, list):
                continue
            for raw_item in raw_items:
                if not isinstance(raw_item, dict):
                    continue
                source = by_md5.get(str(raw_item.get("digest", "")))
                raw_materials = raw_item.get("materials")
                if source is None or not isinstance(raw_materials, list):
                    continue
                materials: list[MaterialDefinition] = []
                for raw_material in raw_materials:
                    if not isinstance(raw_material, dict):
                        materials = []
                        break
                    color = raw_material.get("diffuse_color")
                    materials.append(
                        MaterialDefinition(
                            str(raw_material.get("name", "")),
                            {
                                str(slot): str(reference)
                                for slot, reference in dict(
                                    raw_material.get("textures", {})
                                ).items()
                            },
                            tuple(float(value) for value in color)
                            if isinstance(color, list) and len(color) == 4
                            else None,
                        )
                    )
                if materials:
                    result[str(directory)].append((source, materials))
        return result

    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("fingerprint") == fingerprint:
                return deserialize(cached.get("directories"))
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    records = read_model_thx(thx_path)
    directories: dict[str, list[dict[str, object]]] = defaultdict(list)
    seen_digests: set[str] = set()
    for record in records:
        if record.kind not in {1, 13} or record.content_md5 in seen_digests:
            continue
        seen_digests.add(record.content_md5)
        path = by_md5.get(record.content_md5)
        if path is None or path.suffix.lower() != ".xml":
            continue
        materials = parse_material_xml(path)
        if not materials:
            continue
        primaries = [material_primary_texture(item) for item in materials]
        if any(not reference or "/" not in reference.replace("\\", "/") for reference in primaries):
            continue
        material_directories = {
            reference.strip().replace("\\", "/").lower().rsplit("/", 1)[0] + "/"
            for reference in primaries
            if reference
        }
        if len(material_directories) != 1:
            continue
        directory = next(iter(material_directories))
        directories[directory].append(
            {
                "digest": record.content_md5,
                "materials": [
                    {
                        "name": item.name,
                        "textures": item.textures,
                        "diffuse_color": list(item.diffuse_color)
                        if item.diffuse_color is not None else None,
                    }
                    for item in materials
                ],
            }
        )
    try:
        temporary = cache_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {"fingerprint": fingerprint, "directories": directories},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(cache_path)
    except OSError:
        pass
    return deserialize(directories)


def _build_exact_logical_gim_material_variants(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    thd_dir: Path,
    asset_paths: list[str],
    minimum_size: int = 100 * 1024,
) -> list[MaterialPackage]:
    """Preserve every structurally valid material variant for large white Meshes.

    Ambiguity is represented as multiple PMX outputs instead of selecting an
    arbitrary skin.  Every candidate still needs an exact Mesh/GIM name-hash,
    GIM MtlIdx ordering, an exact texture family directory and real textures.
    """
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    thx_path = thd_dir / "model.thx"
    if not thx_path.is_file():
        return []
    records = read_model_thx(thx_path)
    seeds = read_thx_namehash_seeds(thx_path)
    record_by_hash = {record.name_hash: record for record in records}
    resolver = CrossPackageTextureResolver(thd_dir, model_folder, by_md5)
    directory_index = _load_material_directory_variant_index(
        model_folder, by_md5, thd_dir
    )
    if not directory_index:
        return []

    logicals_by_mesh: dict[Path, set[str]] = defaultdict(set)
    for raw_reference in asset_paths:
        reference = raw_reference.strip().replace("\\", "/").lower()
        if not reference.startswith("model/") or not reference.endswith(".mesh"):
            continue
        for seed in seeds:
            record = record_by_hash.get(
                cloudfilesys_name_hash(reference, "model", seed)
            )
            path = by_md5.get(record.content_md5) if record else None
            if path is not None and path.suffix.lower() == ".mesh":
                logicals_by_mesh[path.resolve()].add(reference)
                break

    result: list[MaterialPackage] = []
    signatures_by_mesh: dict[Path, set[tuple[object, ...]]] = defaultdict(set)
    for mesh_path, logicals in logicals_by_mesh.items():
        if mesh_path in existing_by_mesh:
            continue
        try:
            if mesh_path.stat().st_size < minimum_size:
                continue
            slot_count = read_mesh_submesh_count(mesh_path)
        except Exception:
            continue
        for logical in logicals:
            directory = logical.rsplit("/", 1)[0] + "/"
            gim_reference = logical[:-5] + ".gim"
            gim_path: Path | None = None
            for seed in seeds:
                record = record_by_hash.get(
                    cloudfilesys_name_hash(gim_reference, "model", seed)
                )
                candidate = by_md5.get(record.content_md5) if record else None
                if candidate is not None and candidate.suffix.lower() == ".xml":
                    gim_path = candidate
                    break
            if gim_path is None:
                continue
            gim_submeshes = parse_gim_submeshes(gim_path)
            if len(gim_submeshes) != slot_count:
                continue
            for material_path, materials in directory_index.get(directory, []):
                ordered = order_materials_by_gim(materials, gim_submeshes)
                if len(ordered) != slot_count:
                    continue
                texture_map: dict[str, Path] = {}
                valid = True
                for material in ordered:
                    primary = material_primary_texture(material)
                    if not primary:
                        valid = False
                        break
                    normalized = primary.strip().replace("\\", "/").lower()
                    if not normalized.startswith(directory):
                        valid = False
                        break
                    texture_path = resolver.resolve(primary)
                    if texture_path is None:
                        valid = False
                        break
                    texture_map[primary] = texture_path
                if not valid:
                    continue
                signature = _material_variant_signature(ordered)
                if signature in signatures_by_mesh[mesh_path]:
                    continue
                signatures_by_mesh[mesh_path].add(signature)
                result.append(
                    MaterialPackage(
                        material_path,
                        archive_index(mesh_path) or 0,
                        Path(logical).stem,
                        ordered,
                        [mesh_path],
                        texture_map,
                        "exact-logical-GIM-material-variant",
                    )
                )
    return result


def _build_logical_family_gim_material_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    thd_dir: Path,
    asset_paths: list[str],
    minimum_size: int = 100 * 1024,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """Recover pruned parents from exact logical family identities.

    A live model THX can retain a Mesh, several related GIMs and their
    MaterialGroups while dropping the direct THP edge between them.  Skin
    prefixes such as ``c3_``/``s2_`` also make a strict same-stem lookup miss
    otherwise explicit families.  This resolver removes only that numbered
    skin prefix and then requires all remaining evidence to converge:

    * target Mesh and source GIM both resolve through the current model THX;
    * their normalized logical stems are equal, or the GIM is a named child of
      the target stem (for example ``sp_xiaolunan_luling_03``);
    * GIM slot count equals the physical Mesh slot count;
    * a MaterialGroup comes from a logical texture directory with exactly the
      same normalized family key as the target Mesh directory;
    * GIM ``MtlIdx`` ordering and material names agree, allowing only repeated
      MtlIdx aliases for which another GIM slot explicitly names the material;
    * every visible primary texture resolves to a real current-client entity;
    * all accepted GIM/MaterialGroup combinations produce one material
      signature.  Skin ambiguity therefore remains unbound.

    No character names, archive indexes or content hashes are embedded here.
    """
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    thx_path = thd_dir / "model.thx"
    if not thx_path.is_file():
        return [], {}
    try:
        records = read_model_thx(thx_path)
        seeds = read_thx_namehash_seeds(thx_path)
    except Exception:
        return [], {}
    if not seeds:
        return [], {}

    record_by_hash = {record.name_hash: record for record in records}
    resolver = CrossPackageTextureResolver(thd_dir, model_folder, by_md5)
    directory_index = _load_material_directory_variant_index(
        model_folder, by_md5, thd_dir
    )
    if not directory_index:
        return [], {}

    def norm_reference(value: str) -> str:
        return value.strip().replace("\\", "/").lower().lstrip("/")

    def family_key(value: str) -> str:
        stem = Path(norm_reference(value)).stem
        # Some generated res dictionaries spell ``foo_03.gim``'s companion
        # Mesh as ``foo_03gim.mesh``.  The numeric ``...gim`` suffix is a
        # serializer marker, not part of the asset family name.
        stem = re.sub(r"(?<=\d)gim$", "", stem)
        # c1/c2/c3 and s1/s2/... are official skin/version prefixes.  Do not
        # strip broader q_/j_/npc_ markers because those distinguish assets.
        stem = re.sub(r"^(?:c|s)\d+_", "", stem)
        return re.sub(r"[^0-9a-z]+", "_", stem).strip("_")

    def related_stems(target: str, candidate: str) -> bool:
        return (
            target == candidate
            or candidate.startswith(target + "_")
        )

    def resolve_current(reference: str, suffix: str) -> Path | None:
        for seed in seeds:
            record = record_by_hash.get(
                cloudfilesys_name_hash(reference, "model", seed)
            )
            path = by_md5.get(record.content_md5) if record is not None else None
            if path is not None and path.suffix.lower() == suffix:
                return path.resolve()
        return None

    logicals_by_mesh: dict[Path, set[str]] = defaultdict(set)
    gim_entries: list[tuple[str, str, Path, list[GimSubmesh]]] = []
    seen_gims: set[tuple[str, Path]] = set()
    for raw_reference in asset_paths:
        reference = norm_reference(raw_reference)
        if not reference.startswith("model/"):
            continue
        if reference.endswith(".mesh"):
            path = resolve_current(reference, ".mesh")
            if path is not None:
                logicals_by_mesh[path].add(reference)
            continue
        if not reference.endswith(".gim"):
            continue
        path = resolve_current(reference, ".xml")
        if path is None or (reference, path) in seen_gims:
            continue
        seen_gims.add((reference, path))
        submeshes = parse_gim_submeshes(path)
        if submeshes:
            gim_entries.append(
                (reference, family_key(reference), path, submeshes)
            )

    # The path dictionaries do not always list a GIM that is nevertheless
    # present in the live THX.  Derive only the exact same-stem GIM reference
    # from each already verified Mesh logical path and resolve it normally.
    for logicals in logicals_by_mesh.values():
        for logical in logicals:
            gim_reference = logical[:-5] + ".gim"
            path = resolve_current(gim_reference, ".xml")
            if path is None or (gim_reference, path) in seen_gims:
                continue
            seen_gims.add((gim_reference, path))
            submeshes = parse_gim_submeshes(path)
            if submeshes:
                gim_entries.append(
                    (
                        gim_reference,
                        family_key(gim_reference),
                        path,
                        submeshes,
                    )
                )

    materials_by_family: dict[
        str, list[tuple[str, Path, list[MaterialDefinition]]]
    ] = defaultdict(list)
    for directory, entries in directory_index.items():
        normalized_directory = norm_reference(directory).rstrip("/") + "/"
        key = family_key(normalized_directory.rstrip("/"))
        for material_path, materials in entries:
            materials_by_family[key].append(
                (normalized_directory, material_path, materials)
            )

    result: list[MaterialPackage] = []
    result_by_mesh: dict[Path, MaterialPackage] = {}
    for mesh_path, logicals in logicals_by_mesh.items():
        if mesh_path in existing_by_mesh:
            continue
        try:
            if mesh_path.stat().st_size < minimum_size:
                continue
            slot_count = read_mesh_submesh_count(mesh_path)
        except Exception:
            continue

        proposals: dict[tuple[object, ...], MaterialPackage] = {}
        for logical in logicals:
            target_stem = family_key(logical)
            target_directory = logical.rsplit("/", 1)[0] + "/"
            directory_family = family_key(target_directory.rstrip("/"))
            # The file stem is the primary identity.  The directory is allowed
            # to carry the numbered skin prefix, but not to substitute a
            # different asset stem (important for q_suti body parts).
            if not related_stems(target_stem, directory_family):
                material_families = {directory_family}
            else:
                material_families = {target_stem, directory_family}

            for gim_reference, gim_stem, gim_path, gim_submeshes in gim_entries:
                if (
                    len(gim_submeshes) != slot_count
                    or not related_stems(target_stem, gim_stem)
                ):
                    continue
                for material_family in material_families:
                    for (
                        material_directory,
                        material_path,
                        materials,
                    ) in materials_by_family.get(material_family, []):
                        ordered = order_materials_by_gim(
                            materials, gim_submeshes
                        )
                        if len(ordered) != slot_count:
                            continue

                        # Repeated MtlIdx is a real GIM alias pattern.  A name
                        # mismatch is accepted only if another slot using that
                        # exact index explicitly names the selected material.
                        matched_indices = {
                            item.material_index
                            for item in gim_submeshes
                            if 0 <= item.material_index < len(materials)
                            and _normalized_material_name(item.name)
                            == _normalized_material_name(
                                materials[item.material_index].name
                            )
                        }
                        if any(
                            _normalized_material_name(item.name)
                            != _normalized_material_name(ordered[index].name)
                            and item.material_index not in matched_indices
                            for index, item in enumerate(gim_submeshes)
                        ):
                            continue

                        primary_references = {
                            primary
                            for material in ordered
                            if (primary := material_primary_texture(material))
                        }
                        if not primary_references:
                            continue
                        if any(
                            not norm_reference(reference).startswith(
                                material_directory
                            )
                            for reference in primary_references
                        ):
                            continue

                        texture_map: dict[str, Path] = {}
                        for reference in _ordered_texture_references(ordered):
                            texture_path = resolver.resolve(reference)
                            if (
                                texture_path is not None
                                and texture_path.suffix.lower() == ".ktx"
                            ):
                                texture_map[reference] = texture_path
                        if any(
                            reference not in texture_map
                            for reference in primary_references
                        ):
                            continue

                        all_references = _ordered_texture_references(ordered)
                        confidence = (
                            "THD-logical-family-GIM-exact"
                            if len(texture_map) == len(all_references)
                            else "THD-logical-family-GIM-exact-main-texture"
                        )
                        package = MaterialPackage(
                            material_path,
                            archive_index(material_path) or 0,
                            Path(logical).stem,
                            ordered,
                            [mesh_path],
                            texture_map,
                            confidence,
                        )
                        proposals.setdefault(
                            _material_variant_signature(ordered), package
                        )

        # Multiple official signatures remain useful as explicit skin
        # variants, but only a unique result is safe as the default package.
        result.extend(proposals.values())
        if len(proposals) == 1:
            result_by_mesh[mesh_path] = next(iter(proposals.values()))
    return result, result_by_mesh


def _build_skeleton_path_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    thd_dir: Path,
    shared_material_groups: list[tuple[Path, list[MaterialDefinition]]],
    minimum_size: int = 100 * 1024,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """用“完整骨名集合唯一 Skeleton → 同名逻辑路径”恢复遗漏模型。

    Skeleton 名本身不作为材质证据。只有进一步满足：
    1) model/<Skeleton>/<Skeleton>.mesh 的当前 THX 哈希精确指向该 Mesh；
    2) 同名 GIM 的当前 THX 实体存在；
    3) GIM 子网格与 Mesh 一致；
    4) GIM 自身依赖或 kind=1 共享池给出唯一 MaterialGroup；
    才允许自动绑定。
    """
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thp,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    thx_path = thd_dir / "model.thx"
    thp_path = thd_dir / "model.thp"
    if not thx_path.is_file() or not thp_path.is_file():
        return [], {}
    try:
        records = read_model_thx(thx_path)
        dependencies = read_model_thp(thp_path)
        seeds = read_thx_namehash_seeds(thx_path)
    except Exception:
        return [], {}
    record_by_hash = {record.name_hash: record for record in records}
    resolver = CrossPackageTextureResolver(thd_dir, model_folder, by_md5)

    skeleton_names_by_bones: dict[frozenset[str], set[str]] = {}
    for path in model_folder.rglob("*.skeleton"):
        parsed = read_skeleton_name_and_bones(path)
        if parsed is None:
            continue
        name, bones = parsed
        skeleton_names_by_bones.setdefault(bones, set()).add(name)

    # res 清单不是完整路径表；少量 kind=1 Mesh 仍需从 Skeleton 名和
    # 已知资源目录生成候选。Skeleton 只负责缩小字典，最终必须由当前 THX
    # 的 namehash 精确命中物理 Mesh，绝不直接用骨架相似度绑定材质。
    asset_references = load_res_asset_paths(thd_dir, model_folder)
    name_hashes_by_mesh: dict[Path, set[int]] = {}
    for record in records:
        path = by_md5.get(record.content_md5)
        if path is None or path.suffix.lower() != ".mesh":
            continue
        name_hashes_by_mesh.setdefault(path.resolve(), set()).add(record.name_hash)

    result: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    material_cache: dict[Path, list[MaterialDefinition]] = {}
    for mesh_path in model_folder.rglob("*.mesh"):
        mesh_path = mesh_path.resolve()
        try:
            if (
                mesh_path in existing_by_mesh
                or mesh_path.stat().st_size < minimum_size
            ):
                continue
            mesh = parse_mesh(mesh_path)
        except Exception:
            continue
        skeleton_names = {
            name.strip()
            for name in skeleton_names_by_bones.get(
                frozenset(mesh.bone_names), set()
            )
            if name.strip()
        }
        # 通用 Q 版骨架会同时对应几十个角色，候选字典会失去辨识力。
        if not skeleton_names or len(skeleton_names) > 12:
            continue

        aliases: set[str] = set()
        for name in skeleton_names:
            lowered = name.lower()
            aliases.add(lowered)
            aliases.add(re.sub(r"_[0-9a-f]{6}$", "", lowered))
        aliases.discard("")

        related_references = [
            reference
            for reference in asset_references
            if any(alias in reference.lower() for alias in aliases)
        ]
        candidate_directories = {
            reference.rsplit("/", 1)[0]
            for reference in related_references
            if "/" in reference
        }
        candidate_stems = {
            Path(reference).stem.lower()
            for reference in related_references
        }
        for alias in aliases:
            candidate_directories.add(f"model/{alias}")
            candidate_stems.add(alias)

        # Skeleton 内部名常带随机后缀或 npc_/q_/sN_ 前缀。只用核心词去
        # 扩充已存在的 res 目录，仍然不把这些名称本身当成最终身份。
        cores = {
            re.sub(
                r"^(?:s\d+_|c\d+_|q_|j_|npc_|boss_)",
                "",
                alias,
            )
            for alias in aliases
        }
        cores = {
            re.sub(r"_[0-9a-f]{6}$", "", core)
            for core in cores
            if len(core) >= 4
        }
        for reference in asset_references:
            if "/" not in reference:
                continue
            directory = reference.rsplit("/", 1)[0]
            basename = directory.rsplit("/", 1)[-1].lower()
            if any(core and core in basename for core in cores):
                candidate_directories.add(directory)

        candidate_mesh_references: set[str] = set()
        for directory in candidate_directories:
            directory = directory.lower()
            directory_stem = directory.rsplit("/", 1)[-1]
            candidate_mesh_references.add(
                f"{directory}/{directory_stem}.mesh"
            )
            for stem in candidate_stems:
                candidate_mesh_references.add(f"{directory}/{stem}.mesh")
        # 防止异常宽泛核心词把候选空间炸大；此时宁可保持未绑定。
        if len(candidate_mesh_references) > 10000:
            continue

        target_hashes = name_hashes_by_mesh.get(mesh_path, set())
        verified_references: set[str] = set()
        for mesh_reference in candidate_mesh_references:
            for seed in seeds:
                mesh_hash = cloudfilesys_name_hash(
                    mesh_reference, "model", seed
                )
                if mesh_hash in target_hashes:
                    verified_references.add(mesh_reference)
                    break
        if len(verified_references) != 1:
            continue
        mesh_reference = next(iter(verified_references))
        gim_reference = mesh_reference[:-5] + ".gim"

        gim_hash: int | None = None
        gim_path: Path | None = None
        for seed in seeds:
            candidate_gim_hash = cloudfilesys_name_hash(
                gim_reference, "model", seed
            )
            gim_record = record_by_hash.get(candidate_gim_hash)
            candidate_gim = (
                by_md5.get(gim_record.content_md5)
                if gim_record is not None else None
            )
            if (
                candidate_gim is not None
                and candidate_gim.suffix.lower() == ".xml"
            ):
                gim_hash = candidate_gim_hash
                gim_path = candidate_gim
                break
        if gim_hash is None or gim_path is None:
            continue

        gim_submeshes = parse_gim_submeshes(gim_path)
        if (
            not gim_submeshes
            or len(gim_submeshes) != len(mesh.submeshes)
        ):
            continue

        # 先取同一个 GIM 官方 THP 中的 MaterialGroup。
        own_choices: dict[
            tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
            tuple[Path, list[MaterialDefinition]],
        ] = {}
        for dependency_hash in dependencies.get(gim_hash, []):
            record = record_by_hash.get(dependency_hash)
            path = by_md5.get(record.content_md5) if record else None
            if path is None or path.suffix.lower() != ".xml":
                continue
            if path not in material_cache:
                material_cache[path] = parse_material_xml(path)
            materials = material_cache[path]
            if not materials:
                continue
            ordered = order_materials_by_gim(materials, gim_submeshes)
            if len(ordered) != len(gim_submeshes):
                continue
            signature = tuple(
                (material.name, tuple(sorted(material.textures.items())))
                for material in ordered
            )
            own_choices.setdefault(signature, (path, ordered))

        used_shared_material = False
        if len(own_choices) == 1:
            material_path, ordered_materials = next(
                iter(own_choices.values())
            )
        elif len(own_choices) > 1:
            continue
        else:
            # 若 THP 不再挂材质，只允许使用 kind=1 共享池的逐项同名匹配。
            target_names = tuple(
                _normalized_material_name(item.name)
                for item in gim_submeshes
            )
            shared_choices: dict[
                tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
                tuple[Path, list[MaterialDefinition]],
            ] = {}
            for shared_path, shared_materials in shared_material_groups:
                ordered = order_materials_by_gim(
                    shared_materials, gim_submeshes
                )
                if (
                    len(ordered) != len(gim_submeshes)
                    or tuple(
                        _normalized_material_name(material.name)
                        for material in ordered
                    ) != target_names
                ):
                    continue
                signature = tuple(
                    (material.name, tuple(sorted(material.textures.items())))
                    for material in ordered
                )
                shared_choices.setdefault(
                    signature, (shared_path, ordered)
                )
            if len(shared_choices) != 1:
                continue
            material_path, ordered_materials = next(
                iter(shared_choices.values())
            )
            used_shared_material = True

        references = _ordered_texture_references(ordered_materials)
        if not references:
            continue
        texture_map: dict[str, Path] = {}
        for reference in references:
            texture_path: Path | None = None
            for seed in seeds:
                name_hash = cloudfilesys_name_hash(
                    reference, "model", seed
                )
                record = record_by_hash.get(name_hash)
                candidate = (
                    by_md5.get(record.content_md5)
                    if record is not None else None
                )
                if candidate is not None and candidate.suffix.lower() == ".ktx":
                    texture_path = candidate
                    break
            if texture_path is None:
                texture_path = resolver.resolve(reference)
            if texture_path is not None and texture_path.suffix.lower() == ".ktx":
                texture_map[reference] = texture_path

        main_references = {
            primary
            for material in ordered_materials
            if (primary := material_primary_texture(material))
        }
        resolved_main = {
            reference
            for reference in main_references
            if reference in texture_map
        }
        if not main_references or not resolved_main:
            continue
        complete = len(texture_map) == len(references)
        complete_main = len(resolved_main) == len(main_references)
        prefix = (
            "骨架路径共享材质精确"
            if used_shared_material else "骨架路径精确"
        )
        if complete:
            confidence = prefix
        elif complete_main:
            confidence = f"{prefix}主贴图"
        else:
            confidence = f"{prefix}部分主贴图"
        package = MaterialPackage(
            xml_path=material_path,
            index=archive_index(material_path) or 0,
            package_name=Path(mesh_reference).stem,
            materials=ordered_materials,
            mesh_paths=[mesh_path],
            texture_map=texture_map,
            confidence=confidence,
        )
        result.append(package)
        by_mesh[mesh_path] = package
    return result, by_mesh


def _build_semantic_global_material_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    thd_dir: Path,
    minimum_size: int = 100 * 1024,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """从全局 kind=13 MaterialGroup 恢复被 THP 漏挂的同角色材质。

    仅处理 THP 已明确把 Mesh 挂到某个 GIM、但局部材质关系仍无法建立的
    大 Mesh。候选必须同时满足：MtlIdx 全有效、>=80% 子网格名与材质名
    逐项一致、所有 model/* 主贴图目录与 GIM 语义名一致，且主贴图映射
    签名唯一。这样可区分基础皮肤与 c1/c2 等换皮材质。
    """
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thp,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    thx_path = thd_dir / "model.thx"
    thp_path = thd_dir / "model.thp"
    if not thx_path.is_file() or not thp_path.is_file():
        return [], {}
    try:
        records = read_model_thx(thx_path)
        dependencies = read_model_thp(thp_path)
        seeds = read_thx_namehash_seeds(thx_path)
    except Exception:
        return [], {}
    record_by_hash = {record.name_hash: record for record in records}
    resolver = CrossPackageTextureResolver(thd_dir, model_folder, by_md5)

    # 全局语义池只解析 kind=13 MaterialGroup。相同主贴图映射的低/高 shader
    # 版本稍后归为同一个候选，最终优先保留纹理槽更丰富的那份 XML。
    material_pool: list[tuple[Path, list[MaterialDefinition]]] = []
    for record in records:
        if record.kind != 13:
            continue
        path = by_md5.get(record.content_md5)
        if path is None or path.suffix.lower() != ".xml":
            continue
        materials = parse_material_xml(path)
        if materials:
            material_pool.append((path, materials))

    unresolved = {
        path.resolve()
        for path in model_folder.rglob("*.mesh")
        if path.stat().st_size >= minimum_size
        and path.resolve() not in existing_by_mesh
    }
    if not unresolved:
        return [], {}

    # 只建立 THP 明确列出的 Mesh -> GIM 关系，不从骨架/拓扑推断父 GIM。
    parent_gims_by_mesh: dict[Path, list[Path]] = {}
    for parent_hash, dependency_hashes in dependencies.items():
        parent_record = record_by_hash.get(parent_hash)
        gim_path = (
            by_md5.get(parent_record.content_md5)
            if parent_record is not None else None
        )
        if gim_path is None or gim_path.suffix.lower() != ".xml":
            continue
        if not parse_gim_submeshes(gim_path):
            continue
        for dependency_hash in dependency_hashes:
            record = record_by_hash.get(dependency_hash)
            mesh_path = (
                by_md5.get(record.content_md5)
                if record is not None else None
            )
            if mesh_path is None or mesh_path.suffix.lower() != ".mesh":
                continue
            resolved = mesh_path.resolve()
            if resolved in unresolved:
                parent_gims_by_mesh.setdefault(resolved, []).append(gim_path)

    def normalized_reference(reference: str) -> str:
        return reference.strip().replace("\\", "/").lower().lstrip("/")

    def material_main_signature(
        ordered: list[MaterialDefinition],
    ) -> tuple[tuple[str, str], ...]:
        return tuple(
            (
                _normalized_material_name(material.name),
                normalized_reference(material_primary_texture(material) or ""),
            )
            for material in ordered
        )

    def texture_family_matches(
        ordered: list[MaterialDefinition],
        family_label: str,
    ) -> bool:
        target = _normalized_material_name(family_label)
        families: list[str] = []
        for material in ordered:
            primary = material_primary_texture(material)
            if not primary:
                continue
            parts = normalized_reference(primary).split("/")
            if len(parts) >= 3 and parts[0] == "model":
                families.append(_normalized_material_name(parts[1]))
        return bool(families) and all(item == target for item in families)

    result: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    for mesh_path, gim_paths in parent_gims_by_mesh.items():
        try:
            expected_submeshes = read_mesh_submesh_count(mesh_path)
        except Exception:
            continue
        package_candidates: list[MaterialPackage] = []
        for gim_path in gim_paths:
            gim_submeshes = parse_gim_submeshes(gim_path)
            if len(gim_submeshes) != expected_submeshes:
                continue
            family_label = extracted_resource_label(gim_path)
            if not family_label:
                continue
            required_name_matches = (len(gim_submeshes) * 4 + 4) // 5
            # 以“主贴图映射签名”去重。基础材质的不同 shader 版本可以合并，
            # c1/c2 因贴图目录不同会自然形成不同签名并造成歧义。
            semantic_candidates: dict[
                tuple[tuple[str, str], ...],
                list[tuple[Path, list[MaterialDefinition]]],
            ] = {}
            for material_path, materials in material_pool:
                ordered = order_materials_by_gim(materials, gim_submeshes)
                if len(ordered) != len(gim_submeshes):
                    continue
                name_matches = sum(
                    _normalized_material_name(submesh.name)
                    == _normalized_material_name(material.name)
                    for submesh, material in zip(gim_submeshes, ordered)
                )
                if name_matches < required_name_matches:
                    continue
                if not texture_family_matches(ordered, family_label):
                    continue
                signature = material_main_signature(ordered)
                semantic_candidates.setdefault(signature, []).append(
                    (material_path, ordered)
                )
            if len(semantic_candidates) != 1:
                continue
            choices = next(iter(semantic_candidates.values()))
            # 同一主贴图映射下优先使用附加纹理槽最完整的材质版本。
            material_path, ordered_materials = max(
                choices,
                key=lambda item: sum(
                    len(material.textures) for material in item[1]
                ),
            )
            references = _ordered_texture_references(ordered_materials)
            if not references:
                continue

            texture_map: dict[str, Path] = {}
            for reference in references:
                texture_path: Path | None = None
                for seed in seeds:
                    name_hash = cloudfilesys_name_hash(reference, "model", seed)
                    record = record_by_hash.get(name_hash)
                    candidate = (
                        by_md5.get(record.content_md5)
                        if record is not None else None
                    )
                    if candidate is not None and candidate.suffix.lower() == ".ktx":
                        texture_path = candidate
                        break
                if texture_path is None:
                    texture_path = resolver.resolve(reference)
                if texture_path is not None and texture_path.suffix.lower() == ".ktx":
                    texture_map[reference] = texture_path

            main_references = {
                primary
                for material in ordered_materials
                if (primary := material_primary_texture(material))
            }
            resolved_main = {
                reference
                for reference in main_references
                if reference in texture_map
            }
            if not main_references or not resolved_main:
                continue
            complete = len(texture_map) == len(references)
            complete_main = len(resolved_main) == len(main_references)
            if complete:
                confidence = "THD语义材质精确"
            elif complete_main:
                confidence = "THD语义材质精确主贴图"
            else:
                confidence = "THD语义材质精确部分主贴图"
            package_candidates.append(
                MaterialPackage(
                    xml_path=material_path,
                    index=archive_index(material_path) or 0,
                    package_name=family_label,
                    materials=ordered_materials,
                    mesh_paths=[mesh_path],
                    texture_map=texture_map,
                    confidence=confidence,
                )
            )

        # 同一个 Mesh 若由多个 GIM 指向不同主贴图映射，说明存在皮肤/场景变体，
        # 不自动选其中一个。只有最终主贴图映射唯一时才绑定。
        grouped: dict[
            tuple[tuple[str, str], ...], list[MaterialPackage]
        ] = {}
        for package in package_candidates:
            grouped.setdefault(
                material_main_signature(package.materials), []
            ).append(package)
        if len(grouped) != 1:
            continue
        choices = next(iter(grouped.values()))
        package = max(choices, key=lambda item: len(item.texture_map))
        result.append(package)
        by_mesh[mesh_path] = package
    return result, by_mesh


def _build_exact_render_duplicate_packages(
    model_folder: Path,
    existing_by_mesh: dict[Path, MaterialPackage],
    minimum_size: int = 100 * 1024,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """给渲染几何完全相同的重复 Mesh 复用已确认材质。

    只处理较大的未绑定 Mesh，并先用轻量布局筛选。最终要求位置、法线、
    UV、面索引、子网格、骨骼索引和权重全部逐字节一致；若同一几何对应
    多套不同材质（皮肤变体），则保持不绑定。
    """
    unresolved_by_layout: dict[
        tuple[int, tuple[tuple[int, int, int, int], ...], int, int],
        list[Path],
    ] = {}
    for path in model_folder.rglob("*.mesh"):
        resolved = path.resolve()
        try:
            if resolved in existing_by_mesh or path.stat().st_size < minimum_size:
                continue
            layout = read_mesh_render_layout(path)
        except Exception:
            continue
        unresolved_by_layout.setdefault(layout, []).append(resolved)
    if not unresolved_by_layout:
        return [], {}

    bound_by_layout: dict[
        tuple[int, tuple[tuple[int, int, int, int], ...], int, int],
        list[tuple[Path, MaterialPackage]],
    ] = {}
    wanted_layouts = set(unresolved_by_layout)
    for path, package in existing_by_mesh.items():
        try:
            if not path.is_file() or path.suffix.lower() != ".mesh":
                continue
            layout = read_mesh_render_layout(path)
        except Exception:
            continue
        if layout in wanted_layouts:
            bound_by_layout.setdefault(layout, []).append((path, package))

    result: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    for layout, unresolved_paths in unresolved_by_layout.items():
        bound_candidates = bound_by_layout.get(layout)
        if not bound_candidates:
            continue
        by_fingerprint: dict[str, list[MaterialPackage]] = {}
        for bound_path, package in bound_candidates:
            try:
                fingerprint = _mesh_render_fingerprint(parse_mesh(bound_path))
            except Exception:
                continue
            by_fingerprint.setdefault(fingerprint, []).append(package)

        for mesh_path in unresolved_paths:
            try:
                fingerprint = _mesh_render_fingerprint(parse_mesh(mesh_path))
            except Exception:
                continue
            matches = by_fingerprint.get(fingerprint, [])
            if not matches:
                continue
            grouped: dict[
                tuple[tuple[str, tuple[tuple[str, str], ...]], ...],
                list[MaterialPackage],
            ] = {}
            for package in matches:
                signature = tuple(
                    (material.name, tuple(sorted(material.textures.items())))
                    for material in package.materials
                )
                grouped.setdefault(signature, []).append(package)
            if len(grouped) != 1:
                continue
            choices = next(iter(grouped.values()))
            source = max(choices, key=lambda item: len(item.texture_map))
            package = MaterialPackage(
                xml_path=source.xml_path,
                index=source.index,
                package_name=source.package_name,
                materials=source.materials,
                mesh_paths=[mesh_path],
                texture_map=source.texture_map,
                confidence="渲染几何精确复用",
            )
            result.append(package)
            by_mesh[mesh_path] = package
    return result, by_mesh


def _build_exact_render_target_family_packages(
    model_folder: Path,
    by_md5: dict[str, Path],
    existing_by_mesh: dict[Path, MaterialPackage],
    thd_dir: Path,
    minimum_size: int = 100 * 1024,
) -> tuple[list[MaterialPackage], dict[Path, MaterialPackage]]:
    """Recover orphan siblings when an exact duplicate already references target-family textures.

    Exact geometry alone is intentionally insufficient because many skins share a Mesh.
    This fallback additionally requires every visible primary texture of the matched source
    package to contain the unresolved target's exact logical directory token.  Therefore a
    sibling such as ``..._1`` may donate its official material layout only when that official
    MaterialGroup itself already points at the target family.  Generic white/noise textures
    and unrelated skin directories are rejected.
    """
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    thx_path = thd_dir / "model.thx"
    if not thx_path.is_file():
        return [], {}
    try:
        records = read_model_thx(thx_path)
        seeds = read_thx_namehash_seeds(thx_path)
    except Exception:
        return [], {}
    if not seeds:
        return [], {}

    record_by_hash = {record.name_hash: record for record in records}

    def norm(value: str) -> str:
        return value.strip().replace("\\", "/").lower()

    # Resolve only exact res logical Mesh paths.  Multiple aliases for one physical file are
    # preserved; conflicting accepted material signatures later cause a safe rejection.
    logical_refs_by_path: dict[Path, set[str]] = {}
    size_cache: dict[Path, int] = {}
    for raw_reference in load_res_asset_paths(thd_dir, model_folder):
        reference = norm(raw_reference)
        if not reference.startswith("model/") or not reference.endswith(".mesh"):
            continue
        for seed in seeds:
            record = record_by_hash.get(
                cloudfilesys_name_hash(reference, "model", seed)
            )
            if record is None:
                continue
            path = by_md5.get(record.content_md5)
            if path is None or path.suffix.lower() != ".mesh":
                continue
            if path in existing_by_mesh:
                break
            if path not in size_cache:
                try:
                    size_cache[path] = path.stat().st_size
                except OSError:
                    size_cache[path] = 0
            if size_cache[path] < minimum_size:
                break
            logical_refs_by_path.setdefault(path, set()).add(reference)
            break
    if not logical_refs_by_path:
        return [], {}

    unresolved_by_layout: dict[
        tuple[int, tuple[tuple[int, int, int, int], ...], int, int],
        list[Path],
    ] = {}
    for path in logical_refs_by_path:
        try:
            layout = read_mesh_render_layout(path)
        except Exception:
            continue
        unresolved_by_layout.setdefault(layout, []).append(path)
    if not unresolved_by_layout:
        return [], {}

    wanted_layouts = set(unresolved_by_layout)
    bound_by_layout: dict[
        tuple[int, tuple[tuple[int, int, int, int], ...], int, int],
        list[tuple[Path, MaterialPackage]],
    ] = {}
    for path, package in existing_by_mesh.items():
        if package.confidence not in TRUSTED_MATERIAL_CONFIDENCE:
            continue
        try:
            if path.suffix.lower() != ".mesh" or not path.is_file():
                continue
            layout = read_mesh_render_layout(path)
        except Exception:
            continue
        if layout in wanted_layouts:
            bound_by_layout.setdefault(layout, []).append((path, package))

    fingerprint_cache: dict[Path, str | None] = {}

    def render_fingerprint(path: Path) -> str | None:
        if path not in fingerprint_cache:
            try:
                fingerprint_cache[path] = _mesh_render_fingerprint(parse_mesh(path))
            except Exception:
                fingerprint_cache[path] = None
        return fingerprint_cache[path]

    result: list[MaterialPackage] = []
    by_mesh: dict[Path, MaterialPackage] = {}
    for layout, target_paths in unresolved_by_layout.items():
        bound_candidates = bound_by_layout.get(layout, [])
        if not bound_candidates:
            continue

        sources_by_fingerprint: dict[str, list[MaterialPackage]] = {}
        for source_path, source_package in bound_candidates:
            fingerprint = render_fingerprint(source_path)
            if fingerprint:
                sources_by_fingerprint.setdefault(fingerprint, []).append(source_package)

        for target_path in target_paths:
            fingerprint = render_fingerprint(target_path)
            if not fingerprint:
                continue
            source_packages = sources_by_fingerprint.get(fingerprint, [])
            if not source_packages:
                continue

            accepted: dict[tuple[object, ...], tuple[MaterialPackage, str]] = {}
            for logical_reference in logical_refs_by_path.get(target_path, set()):
                target_directory = logical_reference.rsplit("/", 1)[0] + "/"
                for source in source_packages:
                    primaries = [
                        material_primary_texture(material)
                        for material in source.materials
                    ]
                    if not primaries or any(not primary for primary in primaries):
                        continue
                    normalized_primaries = [norm(primary or "") for primary in primaries]
                    # Important: substring is intentional only across package prefixes, e.g.
                    # ``fx/model/foo/...`` still contains exact ``model/foo/``.  It never
                    # accepts a merely similar directory or filename.
                    if not all(target_directory in primary for primary in normalized_primaries):
                        continue
                    if any(
                        primary not in source.texture_map
                        for primary in primaries
                        if primary is not None
                    ):
                        continue
                    signature = _material_variant_signature(source.materials)
                    accepted.setdefault(signature, (source, logical_reference))

            if len(accepted) != 1:
                continue
            source, logical_reference = next(iter(accepted.values()))
            package = MaterialPackage(
                xml_path=source.xml_path,
                index=source.index,
                package_name=Path(logical_reference).stem,
                materials=list(source.materials),
                mesh_paths=[target_path],
                texture_map=dict(source.texture_map),
                confidence="exact-render-target-family",
            )
            result.append(package)
            by_mesh[target_path] = package
    return result, by_mesh


def _parse_static_socket_bindings(
    path: Path,
) -> list[tuple[str, str, str | None, tuple[float, ...]]]:
    """提取 GIM 中可用于 PMX 的静态 BoundObject Socket。

    返回 (object_gim, socket_name, bone_name, MatrixToBone)。这里只解析结构，不判断目标
    是否真的是静态 Mesh；目标解析和材质可信度在组合阶段再严格验证。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    # 绝大多数 GIM 没有静态 Socket；先用原始字节做极低成本预筛，避免
    # build_composite_models() 为数千个无关 XML 构建 ElementTree。
    lowered = raw.lower()
    if (
        b"<socket_" not in lowered
        or b"boundobjecttype=\"2\"" not in lowered
        or b"matrixtobone=" not in lowered
    ):
        return []
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, UnicodeError):
        return []
    result: list[tuple[str, str, str | None, tuple[float, ...]]] = []
    seen: set[tuple[str, str, tuple[float, ...]]] = set()
    for socket in root.iter():
        if not socket.tag.lower().startswith("socket_"):
            continue
        if socket.get("BoundObjectType") != "2":
            continue
        raw_matrix = (socket.get("MatrixToBone") or "").strip()
        try:
            matrix = tuple(float(value) for value in raw_matrix.split(","))
        except ValueError:
            continue
        if len(matrix) != 16:
            continue
        bone_name = (socket.get("BoneName") or "").strip() or None
        socket_name = (socket.get("Name") or socket.tag).strip()
        for node in socket.findall("./BoundObject/Object"):
            object_gim = (node.get("Name") or "").strip().replace("\\", "/")
            if not object_gim.lower().endswith(".gim"):
                continue
            key = (
                object_gim.lower(),
                _normalized_bone_key(bone_name or ""),
                tuple(round(value, 7) for value in matrix),
            )
            if key in seen:
                continue
            seen.add(key)
            result.append((object_gim, socket_name, bone_name, matrix))
    return result


def _parse_must_show_identity_socket_bindings(
    path: Path,
) -> list[tuple[str, str, tuple[float, ...]]]:
    """提取可安全尝试并入主体的默认模型空间 Socket。

    仅接受 MustShow=true、BindType=263、BoundObjectType=2、无 BoneName、
    单位 MatrixToBone。这些条件表示组件由引擎要求默认显示，且已经位于
    父模型空间；BindType=7 的倒影/石化/水面类表现、有骨目标、非单位变换
    或可选状态都留给显式/人工验证路径，避免把特效替身叠进实体主模型。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    lowered = raw.lower()
    if (
        b"<socket_" not in lowered
        or b"boundobjecttype=\"2\"" not in lowered
        or b"mustshow=\"true\"" not in lowered
        or b"matrixtobone=" not in lowered
    ):
        return []
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, UnicodeError):
        return []
    identity = (
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    )
    result: list[tuple[str, str, tuple[float, ...]]] = []
    seen: set[tuple[str, str]] = set()
    for socket in root.iter():
        if not socket.tag.lower().startswith("socket_"):
            continue
        if socket.get("BoundObjectType") != "2":
            continue
        if (socket.get("BindType") or "").strip() != "263":
            continue
        if (socket.get("MustShow") or "").strip().lower() != "true":
            continue
        if (socket.get("BoneName") or "").strip():
            continue
        raw_matrix = (socket.get("MatrixToBone") or "").strip()
        try:
            matrix = tuple(float(value) for value in raw_matrix.split(","))
        except ValueError:
            continue
        if len(matrix) != 16:
            continue
        if max(abs(a - b) for a, b in zip(matrix, identity)) > 1e-6:
            continue
        socket_name = (socket.get("Name") or socket.tag).strip()
        for node in socket.findall("./BoundObject/Object"):
            object_gim = (node.get("Name") or "").strip().replace("\\", "/")
            if not object_gim.lower().endswith(".gim"):
                continue
            key = (object_gim.lower(), socket_name.lower())
            if key in seen:
                continue
            seen.add(key)
            result.append((object_gim, socket_name, matrix))
    return result


def build_composite_models(
    model_folder: Path,
    by_mesh: dict[Path, MaterialPackage],
    thd_dir: Path | None,
    progress: Callable[[str, int, int], None] | None = None,
) -> list[CompositeModel]:
    """恢复同一上层 GIM 中、共享骨骼结构的主体与附件组合。"""
    if thd_dir is None:
        return []
    from thd_resource_index import (
        cloudfilesys_name_hash,
        read_model_thp,
        read_model_thx,
        read_thx_namehash_seeds,
    )

    by_md5, _ = _manifest_hash_maps(model_folder)
    records = read_model_thx(thd_dir / "model.thx")
    record_by_hash = {record.name_hash: record for record in records}
    dependencies = read_model_thp(thd_dir / "model.thp")
    namehash_seeds = read_thx_namehash_seeds(thd_dir / "model.thx")
    trusted = TRUSTED_MATERIAL_CONFIDENCE

    def report(label: str, done: int, total: int) -> None:
        if progress and (done % 100 == 0 or done == total):
            progress(label, done, total)

    candidates: dict[
        frozenset[Path], tuple[str, list[Path]]
    ] = {}
    dependency_items = list(dependencies.items())
    for dependency_number, (parent_hash, dependency_hashes) in enumerate(
        dependency_items, 1
    ):
        report("收集主体与附件候选", dependency_number, len(dependency_items))
        ordered: list[Path] = []
        for dependency_hash in dependency_hashes:
            record = record_by_hash.get(dependency_hash)
            path = by_md5.get(record.content_md5) if record else None
            resolved = path.resolve() if path and path.suffix.lower() == ".mesh" else None
            package = by_mesh.get(resolved) if resolved else None
            if (
                resolved is not None
                and package is not None
                and package.confidence in trusted
                and resolved not in ordered
            ):
                ordered.append(resolved)
        if len(ordered) < 2:
            continue
        parent_record = record_by_hash.get(parent_hash)
        parent_path = (
            by_md5.get(parent_record.content_md5)
            if parent_record is not None else None
        )
        label = extracted_resource_label(parent_path) if parent_path else "组合模型"
        candidates[frozenset(ordered)] = (label or "组合模型", ordered)

    # 先处理组件更多的集合；只需和已保留的极大集合比较，不再让每个
    # 候选和所有候选做一次平方级比较。
    keys = sorted(candidates, key=len, reverse=True)
    maximal: list[frozenset[Path]] = []
    for key_number, key in enumerate(keys, 1):
        if not any(key < other for other in maximal):
            maximal.append(key)
        report("去重组合候选", key_number, len(keys))
    bone_layout_cache: dict[Path, tuple[tuple[str, ...], tuple[int, ...], int]] = {}

    def cached_bone_layout(
        path: Path,
    ) -> tuple[tuple[str, ...], tuple[int, ...], int]:
        layout = bone_layout_cache.get(path)
        if layout is None:
            layout = read_mesh_bone_layout(path)
            bone_layout_cache[path] = layout
        return layout

    seen_clusters: set[frozenset[Path]] = set()
    result: list[CompositeModel] = []
    for maximal_number, key in enumerate(maximal, 1):
        label, ordered = candidates[key]
        clusters: dict[
            tuple[tuple[str, ...], tuple[int, ...]], list[Path]
        ] = {}
        static_paths: list[Path] = []
        for path in ordered:
            try:
                bone_names, bone_parents, _ = cached_bone_layout(path)
            except Exception:
                continue
            if bone_names == ("__static_root__",):
                static_paths.append(path)
                continue
            signature = (bone_names, bone_parents)
            clusters.setdefault(signature, []).append(path)

        grouped_paths = list(clusters.values())
        if static_paths and len(grouped_paths) == 1:
            # 同一父 GIM 只有一套角色骨架时，静态道具归属无歧义：
            # 直接并入该角色组合，后续统一挂到角色根骨。
            grouped_paths = [grouped_paths[0] + static_paths]
        elif static_paths and not grouped_paths:
            # 纯静态父 GIM 仍可按原始依赖顺序合成一个独立道具 PMX。
            grouped_paths = [static_paths]
        elif len(static_paths) >= 2:
            # 多套角色骨架并存时无法判断静态件属于哪一套，不强行挂角色；
            # 但同一父 GIM 下的静态件仍可彼此组合。
            grouped_paths.append(static_paths)

        cluster_number = 0
        for paths in grouped_paths:
            frozen = frozenset(paths)
            if len(paths) < 2 or frozen in seen_clusters:
                continue
            seen_clusters.add(frozen)
            cluster_number += 1
            name = label if cluster_number == 1 else f"{label}_组合{cluster_number}"
            result.append(
                CompositeModel(
                    name=name,
                    mesh_paths=paths,
                    packages=[by_mesh[path] for path in paths],
                    direct_merge=True,
                )
            )
        report("核对组合骨架", maximal_number, len(maximal))

    # Socket 静态挂件：基础主 PMX 不变，每种 Socket 道具额外生成一个组合变体。
    # 只接受能通过精确逻辑路径哈希解析到“静态 Mesh + 可信材质”的 BoundObject。
    logical_mesh_cache: dict[str, Path | None] = {}

    def resolve_logical_mesh(reference: str) -> Path | None:
        key = reference.strip().replace("\\", "/").lower()
        cached = logical_mesh_cache.get(key)
        if key in logical_mesh_cache:
            return cached
        candidate_references = [key]
        if key.endswith(".gim"):
            candidate_references.insert(0, key[:-4] + ".mesh")
        for candidate in candidate_references:
            for seed in namehash_seeds:
                name_hash = cloudfilesys_name_hash(candidate, "model", seed)
                record = record_by_hash.get(name_hash)
                path = by_md5.get(record.content_md5) if record else None
                if path is None:
                    continue
                if path.suffix.lower() == ".mesh":
                    resolved = path.resolve()
                    logical_mesh_cache[key] = resolved
                    return resolved
                if path.suffix.lower() == ".xml":
                    declared = parse_gim_mesh_reference(path)
                    if declared:
                        resolved = resolve_logical_mesh(declared)
                        logical_mesh_cache[key] = resolved
                        return resolved
        logical_mesh_cache[key] = None
        return None

    socket_composite_signatures: set[tuple[object, ...]] = set()
    for dependency_number, (parent_hash, dependency_hashes) in enumerate(
        dependency_items, 1
    ):
        report("分析静态 Socket", dependency_number, len(dependency_items))
        parent_record = record_by_hash.get(parent_hash)
        parent_path = (
            by_md5.get(parent_record.content_md5)
            if parent_record is not None else None
        )
        if parent_path is None or parent_path.suffix.lower() != ".xml":
            continue
        sockets = _parse_static_socket_bindings(parent_path)
        if not sockets:
            continue

        # 父 GIM 可能还引用武器/特效 Mesh；主角色 Mesh 用 Socket 命名骨覆盖数
        # 选取，覆盖数相同时优先骨骼/顶点更完整的 Mesh。
        rigged_candidates: list[
            tuple[Path, MaterialPackage, tuple[str, ...], int]
        ] = []
        for dependency_hash in dependency_hashes:
            record = record_by_hash.get(dependency_hash)
            path = by_md5.get(record.content_md5) if record else None
            if path is None or path.suffix.lower() != ".mesh":
                continue
            resolved = path.resolve()
            package = by_mesh.get(resolved)
            if package is None or package.confidence not in trusted:
                continue
            try:
                bone_names, _, vertex_count = cached_bone_layout(resolved)
            except Exception:
                continue
            if bone_names == ("__static_root__",):
                continue
            rigged_candidates.append((resolved, package, bone_names, vertex_count))
        if not rigged_candidates:
            continue

        named_socket_bones = [
            bone_name for _, _, bone_name, _ in sockets if bone_name
        ]

        def main_score(
            item: tuple[Path, MaterialPackage, tuple[str, ...], int]
        ) -> tuple[int, int, int]:
            bone_names = item[2]
            bone_keys = {_normalized_bone_key(name) for name in bone_names}
            matched = sum(
                1
                for name in named_socket_bones
                if _normalized_bone_key(name) in bone_keys
            )
            return (matched, len(bone_names), item[3])

        main_path, main_package, main_bone_names, main_vertex_count = max(
            rigged_candidates, key=main_score
        )
        main_bone_keys = {
            _normalized_bone_key(name) for name in main_bone_names
        }
        if named_socket_bones and main_score(
            (main_path, main_package, main_bone_names, main_vertex_count)
        )[0] == 0:
            continue

        socket_groups: dict[
            tuple[str, str],
            list[tuple[Path, MaterialPackage, str | None, tuple[float, ...]]],
        ] = {}
        for object_gim, socket_name, bone_name, matrix in sockets:
            static_path = resolve_logical_mesh(object_gim)
            if static_path is None:
                continue
            static_package = by_mesh.get(static_path)
            if static_package is None or static_package.confidence not in trusted:
                continue
            try:
                static_bone_names, _, _ = cached_bone_layout(static_path)
            except Exception:
                continue
            if static_bone_names != ("__static_root__",):
                continue
            if bone_name and _normalized_bone_key(bone_name) not in main_bone_keys:
                continue
            socket_base = re.sub(
                r"(?:[_\-\s](?:l|r|left|right))$",
                "",
                socket_name.strip().lower(),
            )
            group_key = (object_gim.lower(), socket_base)
            socket_groups.setdefault(group_key, []).append(
                (static_path, static_package, bone_name, matrix)
            )

        label = extracted_resource_label(parent_path) or "组合模型"
        for (object_gim, socket_base), instances in socket_groups.items():
            # 同一对象/插槽组中再次去重，保留左右手等不同矩阵实例。
            deduped: list[
                tuple[Path, MaterialPackage, str | None, tuple[float, ...]]
            ] = []
            local_seen: set[tuple[object, ...]] = set()
            for static_path, static_package, bone_name, matrix in instances:
                instance_key = (
                    static_path,
                    _normalized_bone_key(bone_name or ""),
                    tuple(round(value, 7) for value in matrix),
                )
                if instance_key in local_seen:
                    continue
                local_seen.add(instance_key)
                deduped.append((static_path, static_package, bone_name, matrix))
            if not deduped:
                continue
            # 不把 object_gim 逻辑路径放进签名：热更新/渠道资源中经常有
            # 多个逻辑别名指向完全相同的物理 Mesh（例如 SP荒与 XJ SP荒
            # 的 galaxy）。只要主体、附件物理文件、目标骨和矩阵都一致，
            # 输出 PMX 就完全相同，应视为同一组合，避免同名目录互相覆盖。
            signature = (
                main_path,
                socket_base,
                tuple(
                    (
                        item[0],
                        _normalized_bone_key(item[2] or ""),
                        tuple(round(value, 7) for value in item[3]),
                    )
                    for item in deduped
                ),
            )
            if signature in socket_composite_signatures:
                continue
            socket_composite_signatures.add(signature)
            object_label = Path(object_gim).stem
            result.append(
                CompositeModel(
                    name=f"{label}_Socket_{socket_base or object_label}",
                    mesh_paths=[main_path] + [item[0] for item in deduped],
                    packages=[main_package] + [item[1] for item in deduped],
                    static_bone_names=[None] + [item[2] for item in deduped],
                    static_matrices=[None] + [item[3] for item in deduped],
                )
            )
    # 默认显示的有骨模型空间组件：只处理 MustShow=true、无 BoneName、
    # 单位 MatrixToBone 的 Socket。它们不会直接改写基础 PMX，而是生成
    # “主体 + 默认组件”组合变体；换装/特效等可选状态仍保持独立。
    subset_bind_cache: dict[
        Path,
        tuple[
            tuple[str, ...],
            tuple[int, ...],
            tuple[tuple[float, ...], ...],
        ],
    ] = {}

    def cached_bind_layout(
        path: Path,
    ) -> tuple[
        tuple[str, ...],
        tuple[int, ...],
        tuple[tuple[float, ...], ...],
    ]:
        layout = subset_bind_cache.get(path)
        if layout is None:
            layout = read_mesh_bone_bind_layout(path)
            subset_bind_cache[path] = layout
        return layout

    component_position_safety_cache: dict[tuple[Path, Path], bool] = {}

    def cached_component_positions_safe(main_path: Path, child_path: Path) -> bool:
        key = (main_path.resolve(), child_path.resolve())
        safe = component_position_safety_cache.get(key)
        if safe is None:
            safe = _shared_texture_component_positions_safe(
                key[0], [key[1]]
            )
            component_position_safety_cache[key] = safe
        return safe

    default_rigged_signatures: set[tuple[Path, Path]] = set()
    for dependency_number, (parent_hash, dependency_hashes) in enumerate(
        dependency_items, 1
    ):
        report("分析默认显示组件", dependency_number, len(dependency_items))
        parent_record = record_by_hash.get(parent_hash)
        parent_path = (
            by_md5.get(parent_record.content_md5)
            if parent_record is not None else None
        )
        if parent_path is None or parent_path.suffix.lower() != ".xml":
            continue
        sockets = _parse_must_show_identity_socket_bindings(parent_path)
        if not sockets:
            continue

        resolved_children: list[
            tuple[Path, MaterialPackage, str]
        ] = []
        child_paths: set[Path] = set()
        for object_gim, socket_name, _ in sockets:
            child_path = resolve_logical_mesh(object_gim)
            if child_path is None:
                continue
            child_package = by_mesh.get(child_path)
            if child_package is None or child_package.confidence not in trusted:
                continue
            try:
                child_bones, _, _ = cached_bone_layout(child_path)
            except Exception:
                continue
            if child_bones == ("__static_root__",):
                continue
            resolved_children.append((child_path, child_package, socket_name))
            child_paths.add(child_path)
        if not resolved_children:
            continue

        # 明确排除本轮 Socket 子件后，再从父 GIM 的可信 rigged 依赖中选主体。
        # 若 THP 已裁掉真正主体，宁可跳过，也不能拿另一个子件冒充主模型。
        main_candidates: list[
            tuple[int, int, Path, MaterialPackage]
        ] = []
        for dependency_hash in dependency_hashes:
            record = record_by_hash.get(dependency_hash)
            path = by_md5.get(record.content_md5) if record else None
            if path is None or path.suffix.lower() != ".mesh":
                continue
            resolved = path.resolve()
            if resolved in child_paths:
                continue
            package = by_mesh.get(resolved)
            if package is None or package.confidence not in trusted:
                continue
            try:
                bone_names, _, vertex_count = cached_bone_layout(resolved)
            except Exception:
                continue
            if bone_names == ("__static_root__",):
                continue
            main_candidates.append(
                (len(bone_names), vertex_count, resolved, package)
            )
        if not main_candidates:
            continue
        _, _, main_path, main_package = max(main_candidates)
        label = extracted_resource_label(parent_path) or "组合模型"
        safe_default_by_socket: dict[
            str, dict[Path, MaterialPackage]
        ] = {}

        for child_path, child_package, socket_name in resolved_children:
            signature = (main_path, child_path)
            try:
                main_layout = cached_bind_layout(main_path)
                child_layout = cached_bind_layout(child_path)
                alignment = _rigged_subset_bind_alignment(
                    child_layout, main_layout
                )
                # 完全同骨架的组件在 merge_parsed_meshes 中不会额外变换，
                # 因此这里只接受 bind 也一致的情况。子骨架则会在写出阶段
                # 再按有效权重骨做一次严格校验并应用同一对齐变换。
                if (
                    child_layout[0],
                    child_layout[1],
                ) == (
                    main_layout[0],
                    main_layout[1],
                ) and max(
                    abs(value - expected)
                    for value, expected in zip(
                        alignment,
                        (
                            1.0, 0.0, 0.0, 0.0,
                            0.0, 1.0, 0.0, 0.0,
                            0.0, 0.0, 1.0, 0.0,
                            0.0, 0.0, 0.0, 1.0,
                        ),
                    )
                ) > 1e-4:
                    continue
            except Exception:
                continue

            safe_default_by_socket.setdefault(
                socket_name.strip().lower(), {}
            )[child_path] = child_package
            if signature in default_rigged_signatures:
                continue
            pair = frozenset((main_path, child_path))
            if any(pair <= frozenset(item.mesh_paths) for item in result):
                default_rigged_signatures.add(signature)
                continue
            default_rigged_signatures.add(signature)
            result.append(
                CompositeModel(
                    name=f"{label}_默认组件_{socket_name}",
                    mesh_paths=[main_path, child_path],
                    packages=[main_package, child_package],
                    direct_merge=True,
                )
            )

        # 同一父 GIM 下若多个不同 Socket 都只有一个安全默认对象，则再生成
        # 一个“默认完整”变体。一个 Socket 对应多个物理对象时保持歧义，不选边。
        full_children: list[tuple[Path, MaterialPackage]] = []
        full_seen_paths: set[Path] = set()
        for items in safe_default_by_socket.values():
            if len(items) != 1:
                continue
            child_path, child_package = next(iter(items.items()))
            if child_path in full_seen_paths:
                continue
            full_seen_paths.add(child_path)
            full_children.append((child_path, child_package))
        if len(full_children) >= 2:
            full_paths = [main_path] + [item[0] for item in full_children]
            frozen = frozenset(full_paths)
            if not any(frozen == frozenset(item.mesh_paths) for item in result):
                result.append(
                    CompositeModel(
                        name=f"{label}_默认完整",
                        mesh_paths=full_paths,
                        packages=[main_package] + [item[1] for item in full_children],
                        direct_merge=True,
                    )
                )

    # 资源名已经明确声明为 guajian 的有骨附件。部分热更新资源已失去原始
    # THP/GIM 父边，且附件不一定复用主体主图，但“主体名_guajian*”仍是强语义。
    # 这里只接受唯一兼容主体、唯一物理挂件槽、严格骨架父链/Bind 对齐以及
    # 合并后模型空间包围盒检查全部闭环的对象；同名历史版本先逐个验证，仍有
    # 两个兼容版本时保持歧义，不猜选。
    named_rig_members: dict[
        str, list[tuple[Path, MaterialPackage, str]]
    ] = {}
    material_mesh_items = list(by_mesh.items())
    for mesh_number, (mesh_path, package) in enumerate(material_mesh_items, 1):
        report("整理命名组件", mesh_number, len(material_mesh_items))
        if package.confidence not in trusted:
            continue
        label = safe_model_name(package, mesh_path)
        named_rig_members.setdefault(label.lower(), []).append(
            (mesh_path, package, label)
        )

    def numbered_skin_family(value: str) -> str:
        # 只裁掉官方 cN_/sN_ 皮肤前缀；j_/q_/npc_ 仍是独立资产身份。
        return re.sub(r"^(?:c|s)\d+_", "", value.lower())

    named_rig_families: dict[
        str, list[tuple[Path, MaterialPackage, str]]
    ] = {}
    for label_key, members in named_rig_members.items():
        named_rig_families.setdefault(
            numbered_skin_family(label_key), []
        ).extend(members)

    named_guajian_pattern = re.compile(
        r"^(?P<base>.+?)_(?P<slot>guajian(?:[_-]?\d+)?(?:[_-].*)?)$",
        re.IGNORECASE,
    )
    named_slot_candidates: dict[
        Path,
        tuple[
            MaterialPackage,
            str,
            dict[str, list[tuple[Path, MaterialPackage, str]]],
        ],
    ] = {}
    named_rig_items = list(named_rig_members.items())
    for named_number, (child_label_key, child_members) in enumerate(
        named_rig_items, 1
    ):
        report("匹配命名挂件", named_number, len(named_rig_items))
        match = named_guajian_pattern.match(child_label_key)
        if match is None:
            continue
        main_members = named_rig_members.get(match.group("base"), [])
        if not main_members:
            main_members = named_rig_families.get(
                numbered_skin_family(match.group("base")), []
            )
        if not main_members:
            continue
        slot = match.group("slot").lower()
        child_base_label = re.sub(
            r"_guajian(?:[_-]?\d+)?(?:[_-].*)?$",
            "",
            child_members[0][2],
            flags=re.IGNORECASE,
        )
        child_skin = re.match(r"^((?:c|s)\d+)_", child_label_key)
        for child_path, child_package, _ in child_members:
            compatible_mains: list[
                tuple[Path, MaterialPackage, str, str]
            ] = []
            for main_path, main_package, main_label in main_members:
                if main_path == child_path:
                    continue
                main_skin = re.match(r"^((?:c|s)\d+)_", main_label.lower())
                if (
                    child_skin is not None
                    and main_skin is not None
                    and child_skin.group(1) != main_skin.group(1)
                ):
                    continue
                try:
                    main_layout = cached_bind_layout(main_path)
                    child_layout = cached_bind_layout(child_path)
                    if main_layout[0] == ("__static_root__",):
                        continue
                    if child_layout[0] == ("__static_root__",):
                        # 无骨静态件没有可从骨架推导的位置，仍必须走 Socket 规则。
                        continue
                    alignment = _rigged_subset_bind_alignment(
                        child_layout, main_layout
                    )
                    same_skeleton = (
                        child_layout[0], child_layout[1]
                    ) == (
                        main_layout[0], main_layout[1]
                    )
                    if same_skeleton and max(
                        abs(value - expected)
                        for value, expected in zip(
                            alignment,
                            (
                                1.0, 0.0, 0.0, 0.0,
                                0.0, 1.0, 0.0, 0.0,
                                0.0, 0.0, 1.0, 0.0,
                                0.0, 0.0, 0.0, 1.0,
                            ),
                        )
                    ) > 1e-4:
                        continue
                    if not cached_component_positions_safe(main_path, child_path):
                        continue
                except Exception:
                    continue
                compatible_mains.append(
                    (
                        main_path,
                        main_package,
                        (
                            child_base_label
                            if child_skin is not None and main_skin is None
                            else main_label
                        ),
                        "同骨架/父链/Bind一致"
                        if same_skeleton
                        else "子骨架父链+Bind刚体对齐",
                    )
                )

            # 骨架与空间证据必须把这个物理挂件唯一指向一个物理主体。
            if len(compatible_mains) != 1:
                continue
            main_path, main_package, main_label, alignment_kind = (
                compatible_mains[0]
            )
            entry = named_slot_candidates.setdefault(
                main_path, (main_package, main_label, {})
            )
            entry[2].setdefault(slot, []).append(
                (child_path, child_package, alignment_kind)
            )

    named_children_by_main: dict[
        Path,
        tuple[
            MaterialPackage,
            str,
            dict[str, tuple[Path, MaterialPackage, str]],
        ],
    ] = {}
    for main_path, (main_package, main_label, slots) in (
        named_slot_candidates.items()
    ):
        unique_slots = {
            slot: candidates[0]
            for slot, candidates in slots.items()
            if len({item[0] for item in candidates}) == 1
        }
        if unique_slots:
            named_children_by_main[main_path] = (
                main_package, main_label, unique_slots
            )

    for main_path, (main_package, main_label, children_by_slot) in (
        named_children_by_main.items()
    ):
        children = [children_by_slot[key] for key in sorted(children_by_slot)]
        if not children:
            continue
        component_paths = [main_path] + [item[0] for item in children]
        if any(
            not cached_component_positions_safe(main_path, child_path)
            for child_path in component_paths[1:]
        ):
            continue
        frozen = frozenset(component_paths)
        supersets = [
            item for item in result
            if frozen <= frozenset(item.mesh_paths)
        ]
        evidence = (
            "名称同族挂件；位置证据="
            + ",".join(sorted({item[2] for item in children}))
            + "；模型空间包围盒通过"
        )
        if supersets:
            # 这类组合可能已由 THP/GIM 找到，但旧父标签会丢掉 sN/cN 前缀。
            # 用主体材质包名恢复可辨识名称，同时保留原始组合的额外组件。
            target = min(supersets, key=lambda item: len(item.mesh_paths))
            target.name = main_label
            target.direct_merge = True
            target.evidence = (
                f"{target.evidence}；{evidence}"
                if target.evidence else f"THP/GIM依赖；{evidence}"
            )
            continue
        result.append(
            CompositeModel(
                name=main_label,
                mesh_paths=component_paths,
                packages=[main_package] + [item[1] for item in children],
                evidence=evidence,
                direct_merge=True,
            )
        )

    # 被热更新裁散的角色组件不一定还共享 THP 父节点，但其主图实体、骨架父链
    # 和 bind 矩阵仍会留下三重证据。主贴图只用于找候选；位置必须由 bind 布局
    # 唯一证明。静态件没有可推导的模型空间位置，因此仍只走上面的 Socket 路径。
    texture_members: dict[Path, dict[Path, MaterialPackage]] = {}
    texture_size_cache: dict[Path, int] = {}
    for mesh_number, (mesh_path, package) in enumerate(material_mesh_items, 1):
        report("整理共享贴图组件", mesh_number, len(material_mesh_items))
        if package.confidence not in trusted:
            continue
        for texture_path in package_primary_texture_sources(package):
            if texture_path not in texture_size_cache:
                try:
                    texture_size_cache[texture_path] = texture_path.stat().st_size
                except OSError:
                    texture_size_cache[texture_path] = 0
            # 很小的纯色/遮罩公共图会跨角色复用，不能作为角色归属线索。
            if texture_size_cache[texture_path] < 32 * 1024:
                continue
            texture_members.setdefault(texture_path, {})[mesh_path] = package

    identity_matrix = (
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
    )
    pair_evidence: dict[
        Path,
        dict[Path, tuple[set[Path], set[str], MaterialPackage, MaterialPackage]],
    ] = {}
    derived_name_pattern = re.compile(
        r"(?:^|[_\-])(?:lod\d*|low|shadow|collision|collider)(?:$|[_\-])",
        re.IGNORECASE,
    )

    texture_member_items = list(texture_members.items())
    for texture_number, (texture_path, members) in enumerate(
        texture_member_items, 1
    ):
        report("核对共享贴图位置", texture_number, len(texture_member_items))
        # 超大共享组通常是公共图集；即使骨架偶然同名也不应跨角色拼接。
        if len(members) < 2 or len(members) > 24:
            continue
        infos: list[
            tuple[
                int,
                Path,
                MaterialPackage,
                tuple[tuple[str, ...], tuple[int, ...], tuple[tuple[float, ...], ...]],
            ]
        ] = []
        for mesh_path, package in members.items():
            label = safe_model_name(package, mesh_path)
            if derived_name_pattern.search(label):
                continue
            try:
                bone_names, _, vertex_count = cached_bone_layout(mesh_path)
                bind_layout = cached_bind_layout(mesh_path)
            except Exception:
                continue
            if bone_names == ("__static_root__",):
                continue
            infos.append((vertex_count, mesh_path, package, bind_layout))
        if len(infos) < 2:
            continue

        for child_vertices, child_path, child_package, child_bind in infos:
            choices: list[
                tuple[int, int, Path, MaterialPackage, str]
            ] = []
            for main_vertices, main_path, main_package, main_bind in infos:
                if main_path == child_path or main_vertices < 8_000:
                    continue
                # 只吸收明显的小组件；接近主体大小的模型可能是换装/状态/LOD。
                if child_vertices * 2.2 > main_vertices:
                    continue
                child_names, child_parents, _ = child_bind
                main_names, main_parents, _ = main_bind
                if len(child_names) > len(main_names):
                    continue
                try:
                    alignment = _rigged_subset_bind_alignment(child_bind, main_bind)
                except Exception:
                    continue
                if (child_names, child_parents) == (main_names, main_parents):
                    # merge_parsed_meshes 对完全同骨架组件不施加额外变换，所以只有
                    # bind 已处于同一模型空间时才能无损直接叠加。
                    if max(
                        abs(value - expected)
                        for value, expected in zip(alignment, identity_matrix)
                    ) > 1e-4:
                        continue
                    alignment_kind = "同骨架/父链/Bind一致"
                else:
                    alignment_kind = "子骨架父链+Bind刚体对齐"
                if not cached_component_positions_safe(main_path, child_path):
                    continue
                choices.append(
                    (
                        main_vertices,
                        len(main_names),
                        main_path,
                        main_package,
                        alignment_kind,
                    )
                )
            if not choices:
                continue
            choices.sort(key=lambda item: (item[0], item[1]), reverse=True)
            # 两个大小接近的完整主体都能接收该小件时，角色/状态归属仍有歧义。
            if len(choices) > 1 and choices[0][0] < choices[1][0] * 1.35:
                continue
            _, _, main_path, main_package, alignment_kind = choices[0]
            entry = pair_evidence.setdefault(child_path, {}).get(main_path)
            if entry is None:
                entry = (set(), set(), main_package, child_package)
                pair_evidence[child_path][main_path] = entry
            entry[0].add(texture_path)
            entry[1].add(alignment_kind)

    shared_children_by_main: dict[
        Path,
        list[tuple[Path, MaterialPackage, set[Path], set[str], MaterialPackage]],
    ] = {}
    pair_evidence_items = list(pair_evidence.items())
    for pair_number, (child_path, possible_mains) in enumerate(
        pair_evidence_items, 1
    ):
        report("消除组件归属歧义", pair_number, len(pair_evidence_items))
        # 同一个小件若被不同主体唯一选中，说明共享图/骨架证据仍不唯一。
        if len(possible_mains) != 1:
            continue
        main_path, (textures, alignments, main_package, child_package) = next(
            iter(possible_mains.items())
        )
        shared_children_by_main.setdefault(main_path, []).append(
            (child_path, child_package, textures, alignments, main_package)
        )

    shared_mains = set(shared_children_by_main)
    existing_component_sets = [frozenset(item.mesh_paths) for item in result]
    shared_main_items = list(shared_children_by_main.items())
    for main_number, (main_path, children) in enumerate(shared_main_items, 1):
        report("生成共享贴图组合", main_number, len(shared_main_items))
        # 不把一个同时被更大主体吸收的中间组件再当成独立主体。
        if main_path in pair_evidence:
            continue
        children = [item for item in children if item[0] not in shared_mains]
        if not children:
            continue
        children.sort(key=lambda item: str(item[0]).lower())
        main_package = children[0][4]
        component_paths = [main_path] + [item[0] for item in children]
        if any(
            not cached_component_positions_safe(main_path, child_path)
            for child_path in component_paths[1:]
        ):
            continue
        frozen = frozenset(component_paths)
        if any(frozen == existing for existing in existing_component_sets):
            continue
        texture_names = sorted(
            {texture.name for item in children for texture in item[2]}
        )
        alignment_names = sorted(
            {name for item in children for name in item[3]}
        )
        evidence = (
            "共享主贴图实体=" + ",".join(texture_names[:4])
            + "；位置证据=" + ",".join(alignment_names)
        )
        result.append(
            CompositeModel(
                name=safe_model_name(main_package, main_path),
                mesh_paths=component_paths,
                packages=[main_package] + [item[1] for item in children],
                evidence=evidence,
                direct_merge=True,
            )
        )
        existing_component_sets.append(frozen)

    # 已闭环的多级 Socket：主体全骨架 + 可严格重映射的子骨架 + 静态件。
    # 这里只登记资源层级；真正的骨架兼容性仍由 merge_parsed_meshes 严格校验。
    identity_matrix = (
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        0.0, 0.0, 0.0, 1.0,
    )
    for (
        main_md5,
        child_md5,
        static_md5,
        name,
        static_target_bone,
    ) in VERIFIED_RIGGED_SUBSET_COMPOSITES:
        paths = [by_md5.get(md5) for md5 in (main_md5, child_md5, static_md5)]
        if any(path is None for path in paths):
            continue
        resolved_paths = [path.resolve() for path in paths if path is not None]
        packages = [by_mesh.get(path) for path in resolved_paths]
        if len(packages) != 3 or any(package is None for package in packages):
            continue
        try:
            main_bones, _, _ = cached_bone_layout(resolved_paths[0])
            child_bones, _, _ = cached_bone_layout(resolved_paths[1])
            static_bones, _, _ = cached_bone_layout(resolved_paths[2])
        except Exception:
            continue
        if main_bones == ("__static_root__",):
            continue
        if child_bones == ("__static_root__",):
            continue
        if static_bones != ("__static_root__",):
            continue
        result.append(
            CompositeModel(
                name=name,
                mesh_paths=resolved_paths,
                packages=[package for package in packages if package is not None],
                static_bone_names=[None, None, static_target_bone],
                static_matrices=[None, None, identity_matrix],
                direct_merge=True,
            )
        )

    # 少量 THP/GIM 已裁空，但主模型与静态件的资源关系仍可由精确逻辑路径
    # 人工闭环。仅处理显式 MD5 配对；静态件仍保留独立输出。
    for main_md5, static_md5, name, target_bone_name in VERIFIED_STATIC_ATTACHMENTS:
        main_path = by_md5.get(main_md5)
        static_path = by_md5.get(static_md5)
        if main_path is None or static_path is None:
            continue
        main_path = main_path.resolve()
        static_path = static_path.resolve()
        main_package = by_mesh.get(main_path)
        static_package = by_mesh.get(static_path)
        if main_package is None or static_package is None:
            continue
        try:
            main_bone_names, _, _ = cached_bone_layout(main_path)
            static_bone_names, _, _ = cached_bone_layout(static_path)
        except Exception:
            continue
        if main_bone_names == ("__static_root__",):
            continue
        if static_bone_names != ("__static_root__",):
            continue
        pair = frozenset((main_path, static_path))
        if pair in seen_clusters or any(
            pair <= frozenset(item.mesh_paths) for item in result
        ):
            continue
        seen_clusters.add(pair)
        result.append(
            CompositeModel(
                name=name,
                mesh_paths=[main_path, static_path],
                packages=[main_package, static_package],
                static_bone_names=[None, target_bone_name],
                direct_merge=True,
            )
        )
    return result


def _normalized_bone_key(value: str) -> str:
    return re.sub(r"[\s_]+", "_", value.strip().lower())


def _locate_skeleton_folder(mesh_path: Path) -> Path | None:
    """从 model/extra_rigged/hot_update_model 等来源定位同批 model 骨架。"""
    resolved = mesh_path.resolve()
    for ancestor in resolved.parents:
        if ancestor.name.lower() == "model":
            return ancestor
    for ancestor in resolved.parents:
        candidate = ancestor / "model"
        if candidate.is_dir():
            return candidate
    return None


def _build_skeleton_hierarchy_index(folder: Path) -> SkeletonHierarchyIndex:
    layouts: list[SkeletonHierarchy] = []
    by_bone: dict[str, set[int]] = defaultdict(set)
    for path in folder.rglob("*.skeleton"):
        layout = read_skeleton_hierarchy(path)
        if layout is None:
            continue
        index = len(layouts)
        layouts.append(layout)
        for key in set(layout.bone_keys):
            by_bone[key].add(index)
    return SkeletonHierarchyIndex(
        layouts=tuple(layouts),
        by_bone={key: frozenset(indices) for key, indices in by_bone.items()},
    )


def _get_skeleton_hierarchy_index(folder: Path) -> SkeletonHierarchyIndex:
    folder = folder.resolve()
    with _SKELETON_HIERARCHY_CACHE_LOCK:
        cached = _SKELETON_HIERARCHY_CACHE.get(folder)
        if cached is not None:
            return cached
        index = _build_skeleton_hierarchy_index(folder)
        _SKELETON_HIERARCHY_CACHE[folder] = index
        return index


def _project_skeleton_parents(
    mesh_bone_keys: tuple[str, ...],
    skeleton: SkeletonHierarchy,
) -> tuple[int, ...] | None:
    """投影到 Mesh 现有骨骼，跳过 Skeleton 中未参与蒙皮的中间骨。"""
    mesh_indices = {key: index for index, key in enumerate(mesh_bone_keys)}
    skeleton_indices = {
        key: index for index, key in enumerate(skeleton.bone_keys)
    }
    if any(key not in skeleton_indices for key in mesh_bone_keys):
        return None

    projected: list[int] = []
    for key in mesh_bone_keys:
        parent = skeleton.bone_parents[skeleton_indices[key]]
        while parent >= 0 and skeleton.bone_keys[parent] not in mesh_indices:
            parent = skeleton.bone_parents[parent]
        projected.append(
            mesh_indices[skeleton.bone_keys[parent]] if parent >= 0 else -1
        )
    result = tuple(projected)
    return result if _valid_skeleton_parent_table(result) else None


def restore_mesh_hierarchy_from_skeleton(
    mesh: ParsedMesh,
    mesh_path: Path,
) -> bool:
    """在唯一可证时，用独立 Skeleton 恢复 Mesh 中被压平的父链。"""
    if mesh.bone_names == ["__static_root__"] or not mesh.bone_names:
        return False
    mesh_keys = tuple(_normalized_bone_key(name) for name in mesh.bone_names)
    if len(set(mesh_keys)) != len(mesh_keys):
        return False
    skeleton = _match_skeleton_hierarchy(mesh, mesh_path)
    if skeleton is None:
        return False

    parents = _project_skeleton_parents(mesh_keys, skeleton)
    if parents is None:
        return False
    if parents == tuple(mesh.bone_parents):
        return False
    mesh.bone_parents = list(parents)
    return True


def _match_skeleton_hierarchy(
    mesh: ParsedMesh,
    mesh_path: Path,
) -> SkeletonHierarchy | None:
    """按完整骨名集合定位唯一官方 Skeleton。"""
    if mesh.bone_names == ["__static_root__"] or not mesh.bone_names:
        return None
    mesh_keys = tuple(_normalized_bone_key(name) for name in mesh.bone_names)
    if len(set(mesh_keys)) != len(mesh_keys):
        return None
    folder = _locate_skeleton_folder(mesh_path)
    if folder is None:
        return None
    index = _get_skeleton_hierarchy_index(folder)
    candidate_sets = [index.by_bone.get(key) for key in mesh_keys]
    if any(not candidates for candidates in candidate_sets):
        return None
    candidate_ids = set(min(candidate_sets, key=lambda item: len(item or ())) or ())
    for candidates in candidate_sets:
        candidate_ids.intersection_update(candidates or ())
        if not candidate_ids:
            return None
    if len(candidate_ids) != 1:
        return None
    return index.layouts[next(iter(candidate_ids))]


def _trs_row_matrix4(transform: tuple[float, ...]) -> tuple[float, ...]:
    """将 Skeleton 的 local tx/quat/scale 转为 NeoX row-vector 矩阵。"""
    if len(transform) != 10 or not all(math.isfinite(value) for value in transform):
        raise MeshFormatError("Skeleton bind TRS 无效")
    tx, ty, tz, x, y, z, w, sx, sy, sz = transform
    length = math.sqrt(x * x + y * y + z * z + w * w)
    if length <= 1.0e-8:
        x = y = z = 0.0
        w = 1.0
    else:
        x, y, z, w = x / length, y / length, z / length, w / length
    result = [
        1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y + z * w),
        2.0 * (x * z - y * w), 0.0,
        2.0 * (x * y - z * w), 1.0 - 2.0 * (x * x + z * z),
        2.0 * (y * z + x * w), 0.0,
        2.0 * (x * z + y * w), 2.0 * (y * z - x * w),
        1.0 - 2.0 * (x * x + y * y), 0.0,
        tx, ty, tz, 1.0,
    ]
    result[0:3] = [value * sx for value in result[0:3]]
    result[4:7] = [value * sy for value in result[4:7]]
    result[8:11] = [value * sz for value in result[8:11]]
    return tuple(result)


def _skeleton_bind_global_matrices(
    skeleton: SkeletonHierarchy,
) -> tuple[tuple[float, ...], ...] | None:
    """按官方父链合成 Skeleton bind global 矩阵。"""
    count = len(skeleton.bone_names)
    if len(skeleton.bone_bind_transforms) != count:
        return None
    result: list[tuple[float, ...] | None] = [None] * count
    visiting = [0] * count

    def visit(index: int) -> tuple[float, ...]:
        if result[index] is not None:
            return result[index]  # type: ignore[return-value]
        if visiting[index] == 1:
            raise MeshFormatError("Skeleton 父链存在循环")
        visiting[index] = 1
        local = _trs_row_matrix4(skeleton.bone_bind_transforms[index])
        parent = skeleton.bone_parents[index]
        if 0 <= parent < count:
            value = _matrix4_multiply(local, visit(parent))
        else:
            value = local
        if not all(math.isfinite(item) for item in value):
            raise MeshFormatError("Skeleton bind 矩阵包含非有限值")
        result[index] = value
        visiting[index] = 2
        return value

    try:
        return tuple(visit(index) for index in range(count))  # type: ignore[arg-type]
    except (IndexError, MeshFormatError):
        return None


def _matrix4_max_delta(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return max(abs(a - b) for a, b in zip(left, right))


def _transpose_row_normal_matrix4(
    matrix: tuple[float, ...],
) -> tuple[float, ...]:
    """返回把经过 matrix 的法线逆变换回 bind 空间所需的转置矩阵。"""
    return (
        matrix[0], matrix[4], matrix[8], 0.0,
        matrix[1], matrix[5], matrix[9], 0.0,
        matrix[2], matrix[6], matrix[10], 0.0,
        0.0, 0.0, 0.0, 1.0,
    )


def _restore_mesh_bind_pose(
    mesh: ParsedMesh,
    skeleton: SkeletonHierarchy,
) -> bool:
    """把动作已烘焙进 Mesh 的顶点/骨矩阵还原为官方 bind/T-pose。"""
    bind_globals = _skeleton_bind_global_matrices(skeleton)
    if bind_globals is None:
        return False
    skeleton_indices = {
        key: index for index, key in enumerate(skeleton.bone_keys)
    }
    mesh_to_skeleton: list[int] = []
    for name in mesh.bone_names:
        index = skeleton_indices.get(_normalized_bone_key(name))
        if index is None:
            return False
        mesh_to_skeleton.append(index)
    current_globals = [tuple(matrix) for matrix in mesh.bone_matrices]
    if any(
        len(matrix) != 16 or not all(math.isfinite(value) for value in matrix)
        for matrix in current_globals
    ):
        return False
    weighted_bones = {
        joint
        for joints, weights in zip(mesh.joints, mesh.weights)
        for joint, weight in zip(joints, weights)
        if weight > 1.0e-8 and 0 <= joint < len(mesh.bone_names)
    }
    if not weighted_bones:
        return False
    changed = any(
        _matrix4_max_delta(
            current_globals[index], bind_globals[mesh_to_skeleton[index]]
        ) > 1.0e-3
        for index in weighted_bones
    )
    if not changed:
        return False

    skin_matrices: list[tuple[float, ...]] = []
    try:
        for mesh_index, skeleton_index in enumerate(mesh_to_skeleton):
            current = current_globals[mesh_index]
            bind = bind_globals[skeleton_index]
            skin_matrices.append(
                _matrix4_multiply(_inverse_affine_row_matrix4(bind), current)
            )
        restored_positions: list[tuple[float, float, float]] = []
        restored_normals: list[tuple[float, float, float]] = []
        for position, normal, joints, weights in zip(
            mesh.positions, mesh.normals, mesh.joints, mesh.weights
        ):
            clean_weights = [max(0.0, float(value)) for value in weights]
            total = sum(clean_weights)
            if total <= 1.0e-8:
                clean_weights = [1.0, 0.0, 0.0, 0.0]
            elif abs(total - 1.0) > 1.0e-5:
                clean_weights = [value / total for value in clean_weights]
            weighted = [0.0] * 16
            for joint, weight in zip(joints, clean_weights):
                if weight <= 0.0 or joint < 0 or joint >= len(skin_matrices):
                    continue
                matrix = skin_matrices[joint]
                for component in range(16):
                    weighted[component] += weight * matrix[component]
            inverse_weighted = _inverse_affine_row_matrix4(tuple(weighted))
            restored_positions.append(_transform_row_position(position, inverse_weighted))
            normal_matrix = _transpose_row_normal_matrix4(tuple(weighted))
            restored_normals.append(_transform_row_normal(normal, normal_matrix))
        mesh.positions = restored_positions
        mesh.normals = restored_normals
        mesh.bone_matrices = [
            bind_globals[skeleton_index] for skeleton_index in mesh_to_skeleton
        ]
        return True
    except (MeshFormatError, ValueError, OverflowError):
        return False


def parse_mesh_for_pmx(path: Path) -> ParsedMesh:
    mesh = parse_mesh(path)
    skeleton = _match_skeleton_hierarchy(mesh, path)
    if skeleton is not None:
        restore_mesh_hierarchy_from_skeleton(mesh, path)
        _restore_mesh_bind_pose(mesh, skeleton)
    return mesh


def _matrix4_multiply(
    left: tuple[float, ...], right: tuple[float, ...]
) -> tuple[float, ...]:
    if len(left) != 16 or len(right) != 16:
        raise MeshFormatError("Socket 变换矩阵必须是 4x4")
    return tuple(
        sum(left[row * 4 + k] * right[k * 4 + col] for k in range(4))
        for row in range(4)
        for col in range(4)
    )


def _inverse_affine_row_matrix4(matrix: tuple[float, ...]) -> tuple[float, ...]:
    """求 row-vector 仿射 4x4 逆矩阵；拒绝透视项与奇异矩阵。"""
    if len(matrix) != 16:
        raise MeshFormatError("骨骼变换矩阵必须是 4x4")
    if (
        abs(matrix[3]) > 1e-6
        or abs(matrix[7]) > 1e-6
        or abs(matrix[11]) > 1e-6
        or abs(matrix[15] - 1.0) > 1e-6
    ):
        raise MeshFormatError("骨骼变换不是可安全处理的仿射矩阵")

    a, b, c = matrix[0], matrix[1], matrix[2]
    d, e, f = matrix[4], matrix[5], matrix[6]
    g, h, i = matrix[8], matrix[9], matrix[10]
    determinant = (
        a * (e * i - f * h)
        - b * (d * i - f * g)
        + c * (d * h - e * g)
    )
    if abs(determinant) <= 1e-10:
        raise MeshFormatError("骨骼变换矩阵不可逆")
    inv_det = 1.0 / determinant
    inverse_rotation = (
        (e * i - f * h) * inv_det,
        (c * h - b * i) * inv_det,
        (b * f - c * e) * inv_det,
        (f * g - d * i) * inv_det,
        (a * i - c * g) * inv_det,
        (c * d - a * f) * inv_det,
        (d * h - e * g) * inv_det,
        (b * g - a * h) * inv_det,
        (a * e - b * d) * inv_det,
    )
    tx, ty, tz = matrix[12], matrix[13], matrix[14]
    itx = -(
        tx * inverse_rotation[0]
        + ty * inverse_rotation[3]
        + tz * inverse_rotation[6]
    )
    ity = -(
        tx * inverse_rotation[1]
        + ty * inverse_rotation[4]
        + tz * inverse_rotation[7]
    )
    itz = -(
        tx * inverse_rotation[2]
        + ty * inverse_rotation[5]
        + tz * inverse_rotation[8]
    )
    return (
        inverse_rotation[0], inverse_rotation[1], inverse_rotation[2], 0.0,
        inverse_rotation[3], inverse_rotation[4], inverse_rotation[5], 0.0,
        inverse_rotation[6], inverse_rotation[7], inverse_rotation[8], 0.0,
        itx, ity, itz, 1.0,
    )


def _is_rigid_affine_matrix4(matrix: tuple[float, ...], tolerance: float = 1e-4) -> bool:
    if len(matrix) != 16:
        return False
    if (
        abs(matrix[3]) > tolerance
        or abs(matrix[7]) > tolerance
        or abs(matrix[11]) > tolerance
        or abs(matrix[15] - 1.0) > tolerance
    ):
        return False
    rows = (
        (matrix[0], matrix[1], matrix[2]),
        (matrix[4], matrix[5], matrix[6]),
        (matrix[8], matrix[9], matrix[10]),
    )
    for row in rows:
        if abs(sum(value * value for value in row) - 1.0) > tolerance:
            return False
    for left, right in ((rows[0], rows[1]), (rows[0], rows[2]), (rows[1], rows[2])):
        if abs(sum(a * b for a, b in zip(left, right))) > tolerance:
            return False
    return True


def _rigged_subset_bind_alignment(
    child_layout: tuple[
        tuple[str, ...],
        tuple[int, ...],
        tuple[tuple[float, ...], ...],
    ],
    base_layout: tuple[
        tuple[str, ...],
        tuple[int, ...],
        tuple[tuple[float, ...], ...],
    ],
) -> tuple[float, ...]:
    """仅凭骨架头验证子骨架；要求全部子骨使用同一刚体 bind 对齐。"""
    child_names, child_parents, child_matrices = child_layout
    base_names, base_parents, base_matrices = base_layout
    base_indices: dict[str, list[int]] = {}
    for index, name in enumerate(base_names):
        base_indices.setdefault(_normalized_bone_key(name), []).append(index)

    child_to_base: list[int] = []
    for name in child_names:
        matches = base_indices.get(_normalized_bone_key(name), [])
        if len(matches) != 1:
            raise MeshFormatError(f"子骨架骨名无法唯一映射到主体：{name}")
        child_to_base.append(matches[0])

    for child_index, child_parent in enumerate(child_parents):
        mapped_index = child_to_base[child_index]
        mapped_parent = base_parents[mapped_index]
        if child_parent < 0:
            if mapped_parent >= 0:
                raise MeshFormatError(
                    f"子骨架根骨在主体中不是根骨：{child_names[child_index]}"
                )
            continue
        if child_parent >= len(child_to_base):
            raise MeshFormatError("子骨架父级索引越界")
        if mapped_parent != child_to_base[child_parent]:
            raise MeshFormatError(
                f"子骨架父链与主体不一致：{child_names[child_index]}"
            )

    transforms: list[tuple[float, ...]] = []
    for child_index, child_matrix in enumerate(child_matrices):
        mapped_index = child_to_base[child_index]
        transform = _matrix4_multiply(
            _inverse_affine_row_matrix4(tuple(child_matrix)),
            tuple(base_matrices[mapped_index]),
        )
        if not _is_rigid_affine_matrix4(transform):
            raise MeshFormatError(
                f"子骨架 bind 对齐不是刚体变换：{child_names[child_index]}"
            )
        transforms.append(transform)
    if not transforms:
        raise MeshFormatError("子骨架没有骨骼")

    alignment = transforms[0]
    for transform in transforms[1:]:
        if max(abs(a - b) for a, b in zip(alignment, transform)) > 1e-4:
            raise MeshFormatError("子骨架全部骨骼的 bind 对齐不一致")
    return alignment


def _rigged_subset_alignment(
    mesh: ParsedMesh,
    base: ParsedMesh,
) -> tuple[tuple[float, ...], list[tuple[int, int, int, int]]]:
    """把严格同父链的子骨架安全重映射到主体骨架。

    只有所有实际带权骨都需要同一个刚体 bind 对齐变换时才允许合并。
    这避免仅凭“骨名能对上”就错误吞掉独立坐标系的召唤物/附件。
    """
    base_indices: dict[str, list[int]] = {}
    for index, name in enumerate(base.bone_names):
        base_indices.setdefault(_normalized_bone_key(name), []).append(index)

    child_to_base: list[int] = []
    for name in mesh.bone_names:
        matches = base_indices.get(_normalized_bone_key(name), [])
        if len(matches) != 1:
            raise MeshFormatError(
                f"子骨架骨名无法唯一映射到主体：{name}"
            )
        child_to_base.append(matches[0])

    for child_index, child_parent in enumerate(mesh.bone_parents):
        mapped_index = child_to_base[child_index]
        mapped_parent = base.bone_parents[mapped_index]
        if child_parent < 0:
            if mapped_parent >= 0:
                raise MeshFormatError(
                    f"子骨架根骨在主体中不是根骨：{mesh.bone_names[child_index]}"
                )
            continue
        if child_parent >= len(child_to_base):
            raise MeshFormatError("子骨架父级索引越界")
        if mapped_parent != child_to_base[child_parent]:
            raise MeshFormatError(
                f"子骨架父链与主体不一致：{mesh.bone_names[child_index]}"
            )

    weighted_bones: set[int] = set()
    for joints, weights in zip(mesh.joints, mesh.weights):
        for joint, weight in zip(joints, weights):
            if weight <= 1e-8:
                continue
            if joint < 0 or joint >= len(mesh.bone_names):
                raise MeshFormatError("子骨架存在带权重的无效骨骼索引")
            weighted_bones.add(joint)
    if not weighted_bones:
        raise MeshFormatError("子骨架没有有效权重骨，不能做骨架重映射")

    transforms: list[tuple[float, ...]] = []
    for child_index in sorted(weighted_bones):
        mapped_index = child_to_base[child_index]
        transform = _matrix4_multiply(
            _inverse_affine_row_matrix4(tuple(mesh.bone_matrices[child_index])),
            tuple(base.bone_matrices[mapped_index]),
        )
        if not _is_rigid_affine_matrix4(transform):
            raise MeshFormatError(
                f"子骨架 bind 对齐不是刚体变换：{mesh.bone_names[child_index]}"
            )
        transforms.append(transform)

    alignment = transforms[0]
    for transform in transforms[1:]:
        if max(abs(a - b) for a, b in zip(alignment, transform)) > 1e-4:
            raise MeshFormatError("子骨架不同权重骨需要不同 bind 对齐，不能安全合并")

    root_bone_index = next(
        (index for index, parent in enumerate(base.bone_parents) if parent < 0),
        0,
    )
    remapped_joints: list[tuple[int, int, int, int]] = []
    for joints in mesh.joints:
        remapped_joints.append(
            tuple(
                child_to_base[joint]
                if 0 <= joint < len(child_to_base)
                else root_bone_index
                for joint in joints
            )
        )
    return alignment, remapped_joints


def _transform_row_position(
    value: tuple[float, float, float], matrix: tuple[float, ...]
) -> tuple[float, float, float]:
    x, y, z = value
    return (
        x * matrix[0] + y * matrix[4] + z * matrix[8] + matrix[12],
        x * matrix[1] + y * matrix[5] + z * matrix[9] + matrix[13],
        x * matrix[2] + y * matrix[6] + z * matrix[10] + matrix[14],
    )


def _transform_row_normal(
    value: tuple[float, float, float], matrix: tuple[float, ...]
) -> tuple[float, float, float]:
    x, y, z = value
    nx = x * matrix[0] + y * matrix[4] + z * matrix[8]
    ny = x * matrix[1] + y * matrix[5] + z * matrix[9]
    nz = x * matrix[2] + y * matrix[6] + z * matrix[10]
    length = math.sqrt(nx * nx + ny * ny + nz * nz)
    if length <= 1e-12:
        return value
    return (nx / length, ny / length, nz / length)


def _mesh_position_bounds(
    positions: list[tuple[float, float, float]],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    if not positions:
        raise MeshFormatError("Mesh 没有顶点，无法检查组合空间")
    return (
        tuple(min(value[axis] for value in positions) for axis in range(3)),
        tuple(max(value[axis] for value in positions) for axis in range(3)),
    )


def _matches_full_body_alternative_bounds(
    main_positions: list[tuple[float, float, float]],
    child_positions: list[tuple[float, float, float]],
) -> bool:
    """识别同一完整模型的高低精度版本，避免把 LOD 当成角色附件叠加。"""
    if len(main_positions) < 2_000 or len(child_positions) < 2_000:
        return False
    vertex_ratio = len(child_positions) / len(main_positions)
    if not 0.20 <= vertex_ratio <= 1.05:
        return False

    main_min, main_max = _mesh_position_bounds(main_positions)
    child_min, child_max = _mesh_position_bounds(child_positions)
    main_extents = [
        high - low for low, high in zip(main_min, main_max)
    ]
    main_diagonal = max(
        math.sqrt(sum(value * value for value in main_extents)), 1e-6
    )
    # 真附件可能很大，但几乎不会同时复现主体 AABB 的六个边界。LOD/Show
    # 精简版则常保留完全相同的身高和极值点，只减少中间表面细分。
    for axis in range(3):
        scale = max(main_extents[axis], main_diagonal * 0.10, 1e-6)
        boundary_error = max(
            abs(child_min[axis] - main_min[axis]),
            abs(child_max[axis] - main_max[axis]),
        ) / scale
        if boundary_error > 0.015:
            return False
    return True


def _shared_texture_component_positions_safe(
    main_path: Path,
    child_paths: list[Path],
) -> bool:
    """拒绝错位组件，以及与主体同边界的完整高低精度替代模型。"""
    try:
        main = parse_mesh(main_path)
        main_min, main_max = _mesh_position_bounds(main.positions)
        main_diagonal = max(
            math.sqrt(sum((high - low) ** 2 for low, high in zip(main_min, main_max))),
            1e-6,
        )
        main_signature = (main.bone_names, main.bone_parents)
        for child_path in child_paths:
            child = parse_mesh(child_path)
            positions = child.positions
            if (child.bone_names, child.bone_parents) != main_signature:
                alignment, _ = _rigged_subset_alignment(child, main)
                positions = [
                    _transform_row_position(value, alignment)
                    for value in child.positions
                ]
            child_min, child_max = _mesh_position_bounds(positions)
            values = (*child_min, *child_max)
            if any(not math.isfinite(value) for value in values):
                return False
            if _matches_full_body_alternative_bounds(
                main.positions, positions
            ):
                return False
            separation = [
                max(main_min[axis] - child_max[axis], child_min[axis] - main_max[axis], 0.0)
                for axis in range(3)
            ]
            gap_ratio = math.sqrt(sum(value * value for value in separation)) / main_diagonal
            child_diagonal = math.sqrt(
                sum((high - low) ** 2 for low, high in zip(child_min, child_max))
            )
            if gap_ratio > 2.0 or child_diagonal / main_diagonal > 5.0:
                return False
        return True
    except Exception:
        return False


def merge_parsed_meshes(
    meshes: list[ParsedMesh],
    static_bone_names: list[str | None] | None = None,
    static_matrices: list[tuple[float, ...] | None] | None = None,
) -> ParsedMesh:
    """合并同骨架 Mesh；静态道具可按 NeoX Socket 变换后挂到目标骨。"""
    if not meshes:
        raise MeshFormatError("组合模型没有 Mesh")

    rigged_meshes = [
        mesh for mesh in meshes if mesh.bone_names != ["__static_root__"]
    ]
    # 主体优先使用骨骼最完整的一套。其余 rigged 组件若不是完全同骨架，
    # 后续只能通过严格的“子骨架父链 + bind 对齐”校验后重映射。
    base = (
        max(rigged_meshes, key=lambda item: len(item.bone_names))
        if rigged_meshes
        else meshes[0]
    )
    signature = (base.bone_names, base.bone_parents)

    root_bone_index = next(
        (index for index, parent in enumerate(base.bone_parents) if parent < 0),
        0,
    )
    submeshes: list[tuple[int, int, int, int]] = []
    positions: list[tuple[float, float, float]] = []
    normals: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    uvs: list[tuple[float, float]] = []
    joints: list[tuple[int, int, int, int]] = []
    weights: list[tuple[float, float, float, float]] = []
    if static_bone_names is not None and len(static_bone_names) != len(meshes):
        raise MeshFormatError("静态附件目标骨数量与 Mesh 数量不一致")
    if static_matrices is not None and len(static_matrices) != len(meshes):
        raise MeshFormatError("静态附件变换矩阵数量与 Mesh 数量不一致")

    bone_lookup = {
        _normalized_bone_key(name): index
        for index, name in enumerate(base.bone_names)
    }
    for component_index, mesh in enumerate(meshes):
        vertex_offset = len(positions)
        submeshes.extend(mesh.submeshes)
        component_positions = mesh.positions
        component_normals = mesh.normals
        target_bone_index = root_bone_index
        target_bone_name = (
            static_bone_names[component_index]
            if static_bone_names is not None else None
        )
        if target_bone_name:
            target_bone_index = bone_lookup.get(
                _normalized_bone_key(target_bone_name), -1
            )
            if target_bone_index < 0:
                raise MeshFormatError(
                    f"静态附件目标骨不存在：{target_bone_name}"
                )
        socket_matrix = (
            static_matrices[component_index]
            if static_matrices is not None else None
        )
        remapped_rigged_joints: list[tuple[int, int, int, int]] | None = None
        if (
            mesh.bone_names != ["__static_root__"]
            and (mesh.bone_names, mesh.bone_parents) != signature
        ):
            alignment, remapped_rigged_joints = _rigged_subset_alignment(
                mesh, base
            )
            component_positions = [
                _transform_row_position(value, alignment)
                for value in mesh.positions
            ]
            component_normals = [
                _transform_row_normal(value, alignment)
                for value in mesh.normals
            ]
        elif (
            mesh.bone_names == ["__static_root__"]
            and rigged_meshes
            and socket_matrix is not None
        ):
            # MatrixToBone 把物体局部坐标变成骨局部坐标；有 BoneName 时再乘
            # 该骨的 bind/world 矩阵，得到当前 Mesh 使用的模型空间坐标。
            transform = socket_matrix
            if target_bone_name:
                transform = _matrix4_multiply(
                    socket_matrix,
                    tuple(base.bone_matrices[target_bone_index]),
                )
            component_positions = [
                _transform_row_position(value, transform)
                for value in mesh.positions
            ]
            component_normals = [
                _transform_row_normal(value, transform)
                for value in mesh.normals
            ]
        positions.extend(component_positions)
        normals.extend(component_normals)
        faces.extend(
            (a + vertex_offset, b + vertex_offset, c + vertex_offset)
            for a, b, c in mesh.faces
        )
        uvs.extend(mesh.uvs)
        if mesh.bone_names == ["__static_root__"] and rigged_meshes:
            joints.extend(
                (
                    target_bone_index,
                    target_bone_index,
                    target_bone_index,
                    target_bone_index,
                )
                for _ in mesh.positions
            )
            weights.extend(
                (1.0, 0.0, 0.0, 0.0) for _ in mesh.positions
            )
        else:
            joints.extend(
                remapped_rigged_joints
                if remapped_rigged_joints is not None
                else mesh.joints
            )
            weights.extend(mesh.weights)

    return ParsedMesh(
        version=max(mesh.version for mesh in meshes),
        submeshes=submeshes,
        bone_parents=list(base.bone_parents),
        bone_names=list(base.bone_names),
        bone_matrices=list(base.bone_matrices),
        positions=positions,
        normals=normals,
        faces=faces,
        uvs=uvs,
        joints=joints,
        weights=weights,
    )


class WpkModelReader:
    def __init__(
        self,
        source_root: Path,
        progress: Callable[[str, int, int], None] | None = None,
    ):
        import onmyoji_wpk_gui as wpk

        self.wpk = wpk
        groups = wpk.discover_groups(
            source_root,
            progress=progress,
            stems={"model"},
        )
        self.group = next((item for item in groups if item.stem == "model"), None)
        if self.group is None:
            raise RuntimeError("没有发现 model.idx 对应的 WPK 分组")
        self.handles = {
            package_id: path.open("rb")
            for package_id, path in self.group.packages.items()
        }

    def close(self) -> None:
        for handle in self.handles.values():
            handle.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback_value):
        self.close()

    def read(self, index: int) -> bytes:
        import zstandard

        if index < 0 or index >= len(self.group.records):
            raise RuntimeError(f"model.idx 中找不到资源 #{index}")
        record = self.group.records[index]
        stream = self.handles.get(record.package_id)
        if stream is None:
            raise RuntimeError(
                f"资源 #{index} 对应的 model{record.package_id}.wpk 不存在"
            )
        stream.seek(record.offset)
        read_size = self.wpk.record_read_size(record)
        blob = stream.read(read_size)
        if len(blob) != read_size:
            raise EOFError(f"资源 #{index} 应读取 {read_size}，实际 {len(blob)}")
        decoded, _ = self.wpk.decode_stage1(blob, record.key_length)
        decoded, _ = self.wpk.unwrap_payload(decoded, zstandard)
        return decoded


def read_full_wpk_resource(source_root: Path, index: int) -> bytes:
    with WpkModelReader(source_root) as reader:
        return reader.read(index)


def decode_ktx_to_png(ktx_data: bytes, output_path: Path) -> None:
    try:
        import astc_encoder.pil_codec  # noqa: F401
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("缺少贴图解码依赖 astc-encoder-py / Pillow") from exc
    if len(ktx_data) < 68 or not ktx_data.startswith(b"\xABKTX 11\xBB\r\n\x1A\n"):
        raise ValueError("不是有效的 KTX 1 文件")
    header = struct.unpack("<13I", ktx_data[12:64])
    internal_format = header[4]
    width, height = header[6], header[7]
    key_value_size = header[12]
    image_size_offset = 64 + key_value_size
    if image_size_offset + 4 > len(ktx_data):
        raise ValueError("KTX 头被截断")
    image_size = struct.unpack_from("<I", ktx_data, image_size_offset)[0]
    data_offset = image_size_offset + 4
    payload = ktx_data[data_offset : data_offset + image_size]
    if len(payload) != image_size:
        raise ValueError(f"KTX 数据被截断：应有 {image_size}，实际 {len(payload)}")
    if 0x93B0 <= internal_format <= 0x93BD:
        profile = 1
        block_width, block_height = ASTC_BLOCK_SIZES[internal_format - 0x93B0]
    elif 0x93D0 <= internal_format <= 0x93DD:
        profile = 0
        block_width, block_height = ASTC_BLOCK_SIZES[internal_format - 0x93D0]
    else:
        raise ValueError(f"暂不支持 KTX 内部格式 0x{internal_format:04X}")
    # ASTC 规范允许图片宽高不是 block 尺寸的整数倍；最后一排/列 block
    # 仍按完整 block 存储，只在显示时裁掉越界像素。astc-encoder-py 的
    # Pillow 解码器却强制要求输入尺寸整除 block，因此直接用原始尺寸会把
    # 例如 1022x1024、2046x2048 这类游戏合法贴图误判为失败。
    padded_width = ((width + block_width - 1) // block_width) * block_width
    padded_height = ((height + block_height - 1) // block_height) * block_height
    expected_payload_size = (
        (padded_width // block_width)
        * (padded_height // block_height)
        * 16
    )
    if len(payload) != expected_payload_size:
        raise ValueError(
            f"ASTC 数据尺寸不匹配：{width}x{height} / "
            f"block {block_width}x{block_height} 应为 {expected_payload_size}，"
            f"实际 {len(payload)}"
        )
    image = Image.frombytes(
        "RGBA", (padded_width, padded_height), payload, "astc",
        (profile, block_width, block_height),
    )
    if padded_width != width or padded_height != height:
        image = image.crop((0, 0, width, height))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # optimize=True 会让 Pillow 对每张大贴图做昂贵的单核压缩搜索。
    # PMX 只要求无损 PNG，不要求极限压缩；低压缩级别能显著缩短全量导出。
    image.save(output_path, "PNG", compress_level=1)


class DecodedTextureCache:
    """同一份 KTX/DDS/常规图片只解码一次，再复制为 PNG。"""

    def __init__(self, cache_root: Path):
        self.cache_root = cache_root

    def materialize(self, ktx_data: bytes, output_path: Path) -> None:
        digest = hashlib.md5(ktx_data).hexdigest()
        cached = self.cache_root / digest[:2] / f"{digest}.png"
        if not cached.is_file() or cached.stat().st_size == 0:
            cached.parent.mkdir(parents=True, exist_ok=True)
            temporary = cached.with_suffix(".png.tmp")
            try:
                temporary.unlink(missing_ok=True)
                if ktx_data.startswith((b"\xABKTX 11\xBB\r\n\x1A\n", b"\xABKTX 20\xBB\r\n\x1A\n")):
                    decode_ktx_to_png(ktx_data, temporary)
                else:
                    from PIL import Image

                    with Image.open(io.BytesIO(ktx_data)) as image:
                        image.convert("RGBA").save(
                            temporary, "PNG", compress_level=1
                        )
                if not temporary.is_file() or temporary.stat().st_size == 0:
                    raise RuntimeError("贴图解码结果为空")
                temporary.replace(cached)
            except Exception:
                temporary.unlink(missing_ok=True)
                raise

        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_output = output_path.with_suffix(".png.tmp")
        try:
            temporary_output.unlink(missing_ok=True)
            shutil.copyfile(cached, temporary_output)
            temporary_output.replace(output_path)
        except Exception:
            temporary_output.unlink(missing_ok=True)
            raise


def _file_build_stamp(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
        return str(path), stat.st_size, stat.st_mtime_ns
    except OSError:
        return str(path), -1, -1


def one_click_source_fingerprint(
    source_root: Path | None,
    model_folder: Path,
    thd_dir: Path | None,
    apk_path: Path | None,
) -> str:
    """快速判断游戏源包和材质规则是否发生变化。"""
    inputs: list[tuple[str, int, int]] = []
    if source_root is not None and source_root.is_dir():
        source_files = sorted(
            (
                path for path in source_root.iterdir()
                if path.is_file() and path.suffix.lower() in {".idx", ".wpk"}
            ),
            key=lambda path: path.name.lower(),
        )
        inputs.extend(_file_build_stamp(path) for path in source_files)
    else:
        inputs.append(_file_build_stamp(model_folder / "manifest.csv"))
    if thd_dir is not None and thd_dir.is_dir():
        thd_files = sorted(
            (
                path for path in thd_dir.iterdir()
                if path.is_file() and path.suffix.lower() in {".thx", ".thp"}
            ),
            key=lambda path: path.name.lower(),
        )
        inputs.extend(_file_build_stamp(path) for path in thd_files)
        # res.zip 是客户端直接挂载的动态资源仓库，不属于 res/*.wpk；
        # 它变化时必须重新分析材质和新增 Mesh，但已有 PMX 仍按各自指纹复用。
        hot_archive = thd_dir.parent / "temp_cache" / "res.zip"
        if hot_archive.is_file():
            inputs.append(_file_build_stamp(hot_archive))
    if apk_path is not None:
        inputs.append(_file_build_stamp(apk_path))
    payload = {
        "schema": 2,
        "material_resolver": MATERIAL_RESOLVER_VERSION,
        "composite_resolver": COMPOSITE_RESOLVER_VERSION,
        "pmx_output": PMX_OUTPUT_FORMAT_VERSION,
        "inputs": inputs,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def can_fast_reuse_one_click(output_root: Path, fingerprint: str) -> bool:
    state_path = output_root / ".one_click_state.json"
    report_path = output_root / "纹理恢复报告.csv"
    if not state_path.is_file() or not report_path.is_file():
        return False
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return (
        state.get("fingerprint") == fingerprint
        and state.get("report_stamp") == list(_file_build_stamp(report_path))
    )


def write_one_click_state(output_root: Path, fingerprint: str) -> None:
    report_path = output_root / "纹理恢复报告.csv"
    if not report_path.is_file():
        return
    state_path = output_root / ".one_click_state.json"
    temporary = state_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {
                "fingerprint": fingerprint,
                "report_stamp": list(_file_build_stamp(report_path)),
                "material_resolver": MATERIAL_RESOLVER_VERSION,
                "composite_resolver": COMPOSITE_RESOLVER_VERSION,
                "pmx_output": PMX_OUTPUT_FORMAT_VERSION,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(state_path)


def pmx_build_fingerprint(
    mesh_path: Path,
    package: MaterialPackage | None,
) -> str:
    """PMX 增量构建键：Mesh、材质、纹理或管线版本任一变化才重建。"""
    payload: dict[str, object] = {
        "pipeline": PMX_OUTPUT_FORMAT_VERSION,
        "mesh": _file_build_stamp(mesh_path),
    }
    if package is not None:
        payload["confidence"] = package.confidence
        payload["material_xml"] = _file_build_stamp(package.xml_path)
        payload["materials"] = [
            (
                material.name,
                sorted(material.textures.items()),
                material.diffuse_color,
            )
            for material in package.materials
        ]
        payload["textures"] = [
            (original, _file_build_stamp(path))
            for original, path in sorted(package.texture_map.items())
        ]
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _pmx_build_output_index(category_root: Path) -> dict[str, list[Path]]:
    """Index generated outputs by content fingerprint, independent of folders."""
    category_root = category_root.resolve()
    cache_key = str(category_root).lower()
    cached = _PMX_BUILD_OUTPUT_CACHE.get(cache_key)
    if cached is not None:
        return cached
    result: dict[str, list[Path]] = defaultdict(list)
    if category_root.is_dir():
        for metadata_path in category_root.rglob(".build.json"):
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            fingerprint = str(metadata.get("fingerprint", "")).strip()
            if fingerprint:
                result[fingerprint].append(metadata_path.parent.resolve())
    normalized = dict(result)
    _PMX_BUILD_OUTPUT_CACHE[cache_key] = normalized
    return normalized


def _find_reusable_pmx_output(
    category_root: Path,
    fingerprint: str,
    expected_name: str,
) -> tuple[Path, Path, dict[str, object]] | None:
    for output_dir in _pmx_build_output_index(category_root).get(fingerprint, []):
        pmx_path = output_dir / expected_name
        if not pmx_path.is_file():
            candidates = sorted(output_dir.glob("*.pmx"))
            if len(candidates) != 1:
                continue
            pmx_path = candidates[0]
        metadata_path = output_dir / ".build.json"
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if metadata.get("fingerprint") == fingerprint:
            return pmx_path.resolve(), output_dir, metadata
    return None


def _register_pmx_build_output(
    category_root: Path,
    fingerprint: str,
    output_dir: Path,
) -> None:
    index = _pmx_build_output_index(category_root)
    entries = index.setdefault(fingerprint, [])
    resolved = output_dir.resolve()
    if resolved not in entries:
        entries.append(resolved)


def safe_model_name(package: MaterialPackage | None, mesh_path: Path) -> str:
    if package:
        package_label = re.sub(
            r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", package.package_name
        ).strip("_")
        if package_label:
            return package_label
        for material in package.materials:
            name = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", material.name).strip("_")
            if name:
                return name
    return mesh_path.stem


def write_texture_slot_report(
    output_path: Path,
    materials: list[MaterialDefinition],
    texture_files: dict[str, Path],
) -> None:
    """导出全部材质/纹理槽位；PMX 识别 Tex0/TexDiffuse 等主颜色槽。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            ["子网格序号", "材质名", "纹理槽位", "原始纹理路径", "恢复文件", "PMX用途"]
        )
        for material_index, material in enumerate(materials):
            for slot, original in sorted(
                material.textures.items(),
                key=lambda item: int(re.search(r"\d+", item[0]).group())
                if re.search(r"\d+", item[0]) else 999,
            ):
                recovered = texture_files.get(original)
                writer.writerow(
                    [
                        material_index,
                        material.name,
                        slot,
                        original,
                        str(recovered) if recovered else "",
                        "主贴图"
                        if original == material_primary_texture(material)
                        else "附加纹理（未误绑为主贴图）",
                    ]
                )


def package_texture_summary(
    package: MaterialPackage | None,
) -> str:
    if package is None:
        return ""
    primary_total = sum(
        material_primary_texture(material) is not None
        for material in package.materials
    )
    primary_resolved = sum(
        bool(
            (primary := material_primary_texture(material))
            and primary in package.texture_map
        )
        for material in package.materials
    )
    declared = {
        reference
        for material in package.materials
        for reference in material.textures.values()
    }
    resolved = sum(reference in package.texture_map for reference in declared)
    return (
        f"材质 {len(package.materials)}；"
        f"主贴图 {primary_resolved}/{primary_total}；"
        f"纹理槽 {resolved}/{len(declared)}"
    )


def mesh_size_bucket(size: int) -> str:
    """Return the stable output bucket used to triage large textureless PMX files."""
    kib = 1024
    if size < 50 * kib:
        return "小于50KB"
    if size < 100 * kib:
        return "50-100KB"
    if size < 200 * kib:
        return "100-200KB"
    if size < 500 * kib:
        return "200-500KB"
    if size < 1024 * kib:
        return "500KB-1MB"
    if size < 2 * 1024 * kib:
        return "1-2MB"
    if size < 5 * 1024 * kib:
        return "2-5MB"
    return "大于5MB"


def write_untextured_focus_reports(output_root: Path) -> dict[str, int]:
    """把大白模按资源用途拆开，避免角色检查被特效/关卡组件淹没。"""
    source_path = output_root / "未匹配贴图_按源Mesh大小.csv"
    if not source_path.is_file():
        return {}
    with source_path.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    if not fieldnames:
        return {}

    role_rows: list[dict[str, str]] = []
    single_slot_rows: list[dict[str, str]] = []
    supplemental_rows: list[dict[str, str]] = []
    for row in rows:
        physical = row.get("物理Mesh路径", "").replace("/", "\\").lower()
        try:
            source_size = int(row.get("源Mesh大小", "0") or 0)
        except ValueError:
            source_size = 0
        if "\\extra_rigged\\fx_model\\" in physical:
            scope = "额外包/fx_model"
            priority = "P2"
            advice = "特效组件；角色主包处理后再检查"
            supplemental = True
        elif "\\extra_rigged\\levelsets\\" in physical:
            scope = "额外包/levelsets"
            priority = "P3"
            advice = "关卡或场景组件；默认暂缓"
            supplemental = True
        elif "\\extra_rigged\\" in physical:
            scope = "额外包/其他"
            priority = "P2"
            advice = "额外资源组件；单独排查"
            supplemental = True
        elif "\\loose_model\\" in physical:
            scope = "角色主包/热更新散件"
            priority = "P0" if source_size >= 500 * 1024 else "P1"
            advice = "角色热更新资源；优先检查"
            supplemental = False
        else:
            scope = "角色主包/model"
            priority = "P0" if source_size >= 500 * 1024 else "P1"
            advice = "角色与附件主包；优先检查"
            supplemental = False
        enriched = {
            "检查优先级": priority,
            "资源范围": scope,
            "建议": advice,
            **row,
        }
        if supplemental:
            supplemental_rows.append(enriched)
        else:
            role_rows.append(enriched)
            if str(row.get("子网格数", "")).strip() == "1":
                single_slot_rows.append(enriched)

    output_fields = ["检查优先级", "资源范围", "建议", *fieldnames]

    def write_rows(name: str, selected: list[dict[str, str]]) -> None:
        with (output_root / name).open(
            "w", newline="", encoding="utf-8-sig"
        ) as stream:
            writer = csv.DictWriter(stream, fieldnames=output_fields)
            writer.writeheader()
            writer.writerows(selected)

    write_rows("白模优先检查_角色主包.csv", role_rows)
    write_rows("白模优先检查_单槽.csv", single_slot_rows)
    write_rows("白模暂缓_特效关卡包.csv", supplemental_rows)
    return {
        "角色主包": len(role_rows),
        "角色主包单槽": len(single_slot_rows),
        "额外包": len(supplemental_rows),
    }


def export_mesh_variant(
    mesh_path: Path,
    package: MaterialPackage | None,
    output_root: Path,
    texture_cache: DecodedTextureCache,
    folder_suffix: str = "",
) -> tuple[Path, Path, str, bool, bool, bool, str]:
    """导出一个物理 Mesh 的单套材质变体。

    返回：(PMX路径, 输出目录, 模型名, 是否有主贴图, 是否增量复用, 纹理错误说明)。
    """
    model_name = safe_model_name(package, mesh_path)
    short_hash = mesh_path.stem.rsplit("_", 1)[-1][:8]
    safe_suffix = re.sub(
        r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", folder_suffix
    ).strip("_")
    folder_name = f"{model_name}_{short_hash}"
    if safe_suffix:
        folder_name = f"{model_name}_{safe_suffix}_{short_hash}"

    trusted_package = bool(
        package and package.confidence in TRUSTED_MATERIAL_CONFIDENCE
    )
    predicted_textured = bool(
        trusted_package
        and package
        and any(
            material_primary_texture(material) in package.texture_map
            for material in package.materials
        )
    )
    predicted_colored = bool(
        trusted_package
        and package
        and any(material.diffuse_color is not None for material in package.materials)
    )
    category = (
        "带贴图"
        if predicted_textured
        else ("纯色材质" if predicted_colored else "未匹配贴图")
    )
    try:
        source_size = mesh_path.stat().st_size
    except OSError:
        source_size = 0
    size_bucket = mesh_size_bucket(source_size)
    model_output = output_root / category / size_bucket / folder_name
    if category == "带贴图":
        try:
            import pmx_role_classifier as role_classifier

            primary_paths = [
                primary
                for material in (package.materials if package else [])
                if (primary := material_primary_texture(material))
            ]
            role_path = role_classifier.role_path_for_export(
                output_root,
                "mesh:" + mesh_path.name.lower(),
                model_name,
                primary_paths,
                [material.name for material in (package.materials if package else [])],
                package.package_name if package else "",
            )
            if role_path:
                model_output = (
                    output_root / category / role_classifier.CLASSIFIED_FOLDER
                    / role_path / size_bucket / folder_name
                )
        except Exception:
            # Classification is organizational metadata; export must remain usable
            # if a hand-edited rule file is temporarily invalid.
            pass
    fingerprint = pmx_build_fingerprint(
        mesh_path, package if trusted_package else None
    )
    pmx_path = model_output / f"{model_name}.pmx"
    build_meta_path = model_output / ".build.json"
    build_meta: dict[str, object] = {}
    if build_meta_path.is_file():
        try:
            build_meta = json.loads(
                build_meta_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, json.JSONDecodeError):
            build_meta = {}
    if (
        build_meta.get("fingerprint") == fingerprint
        and pmx_path.is_file()
    ):
        return (
            pmx_path,
            model_output,
            model_name,
            bool(build_meta.get("has_diffuse")),
            bool(build_meta.get("has_color")),
            True,
            "",
        )

    reusable = _find_reusable_pmx_output(
        output_root / category,
        fingerprint,
        f"{model_name}.pmx",
    )
    if reusable is not None:
        reusable_pmx, reusable_output, reusable_meta = reusable
        return (
            reusable_pmx,
            reusable_output,
            model_name,
            bool(reusable_meta.get("has_diffuse")),
            bool(reusable_meta.get("has_color")),
            True,
            "",
        )

    mesh = parse_mesh_for_pmx(mesh_path)
    texture_files: dict[str, Path] = {}
    texture_error = ""
    if trusted_package and package:
        try:
            used_names: set[str] = set()
            materialized_by_source: dict[Path, Path] = {}
            auxiliary_errors: list[str] = []
            for original, ktx_path in package.texture_map.items():
                try:
                    source_key = ktx_path.resolve()
                    existing_png = materialized_by_source.get(source_key)
                    if existing_png is not None:
                        texture_files[original] = existing_png
                        continue
                    texture_index = archive_index(ktx_path)
                    original_name = Path(
                        original.replace("\\", "/")
                    ).stem
                    clean_name = re.sub(
                        r"[^0-9A-Za-z_\-\u4e00-\u9fff]+",
                        "_",
                        original_name,
                    ).strip("_") or "texture"
                    png_name = clean_name + ".png"
                    if png_name.lower() in used_names:
                        suffix = (
                            str(texture_index)
                            if texture_index is not None
                            else ktx_path.stem[-8:]
                        )
                        png_name = f"{clean_name}_{suffix}.png"
                    used_names.add(png_name.lower())
                    png_path = model_output / "textures" / png_name
                    if png_path.is_file() and png_path.stat().st_size > 0:
                        texture_files[original] = png_path
                        materialized_by_source[source_key] = png_path
                        continue
                    ktx_data = ktx_path.read_bytes()
                    texture_cache.materialize(ktx_data, png_path)
                    texture_files[original] = png_path
                    materialized_by_source[source_key] = png_path
                except Exception as exc:
                    auxiliary_errors.append(
                        f"{original}: {type(exc).__name__}: {exc}"
                    )
            if auxiliary_errors:
                texture_error = (
                    f"跳过 {len(auxiliary_errors)} 个辅助纹理；"
                    + " | ".join(auxiliary_errors[:3])
                )
        except Exception as exc:
            texture_error = f"{type(exc).__name__}: {exc}"
            texture_files = {}
            if model_output.exists():
                shutil.rmtree(model_output)
            model_output = (
                output_root / "未匹配贴图" / size_bucket / folder_name
            )

    pmx_path = model_output / f"{model_name}.pmx"
    save_pmx(
        mesh,
        pmx_path,
        model_name,
        package.materials if package else None,
        texture_files,
    )
    if package:
        write_texture_slot_report(
            model_output / "纹理槽位.csv",
            package.materials,
            texture_files,
        )
    has_diffuse = bool(
        package
        and any(
            material_primary_texture(material) in texture_files
            for material in package.materials
        )
    )
    has_color = bool(
        package
        and any(material.diffuse_color is not None for material in package.materials)
    )
    build_meta_path = model_output / ".build.json"
    build_meta_path.write_text(
        json.dumps(
            {
                "fingerprint": fingerprint,
                "has_diffuse": has_diffuse,
                "has_color": has_color,
                "source_mesh": mesh_path.name,
                "material_variant": package.package_name if package else "",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _register_pmx_build_output(
        output_root / model_output.relative_to(output_root).parts[0],
        fingerprint,
        model_output,
    )
    return (
        pmx_path,
        model_output,
        model_name,
        has_diffuse,
        has_color,
        False,
        texture_error,
    )


def save_pmx(
    mesh: ParsedMesh,
    output_path: Path,
    model_name: str,
    materials: list[MaterialDefinition] | None = None,
    texture_files: dict[str, Path] | None = None,
) -> None:
    try:
        import pymeshio.common as common
        import pymeshio.pmx as pmx
        import pymeshio.pmx.writer
    except ImportError as exc:
        raise RuntimeError(
            "缺少 PMX 依赖。请点“安装 PMX 依赖”，或运行："
            "python -m pip install pymeshio"
        ) from exc

    model = pmx.Model(
        name=model_name,
        english_name=model_name,
        comment="Converted from Onmyoji NeoX mesh",
        english_comment="Converted from Onmyoji NeoX mesh",
    )
    # pymeshio 会自动放入一个示例骨骼和示例材质；转换真实资源时必须移除。
    model.bones.clear()
    model.materials.clear()
    model.display_slots.clear()

    bone_count = len(mesh.bone_names)
    bone_order, sanitized_parents = _order_bones_parent_first(
        mesh.bone_parents
    )
    old_to_new_bone_index = {
        source_index: output_index
        for output_index, source_index in enumerate(bone_order)
    }
    for source_index in bone_order:
        name = mesh.bone_names[source_index]
        matrix = mesh.bone_matrices[source_index]
        parent = sanitized_parents[source_index]
        parent_index = (
            old_to_new_bone_index[parent] if parent >= 0 else -1
        )
        x, y, z = matrix[12], matrix[13], matrix[14]
        bone = pmx.Bone(
            name=name,
            english_name=name,
            position=common.Vector3(-x, y, -z),
            parent_index=parent_index,
            layer=0,
            flag=0,
        )
        for flag_name in (
            "BONEFLAG_CAN_ROTATE",
            "BONEFLAG_IS_VISIBLE",
            "BONEFLAG_CAN_MANIPULATE",
        ):
            flag_value = getattr(pmx, flag_name, None)
            if flag_value is not None:
                bone.setFlag(flag_value, True)
        model.bones.append(bone)

    if bone_count:
        model.display_slots.append(
            pmx.DisplaySlot("Root", "Root", 1, [(0, 0)])
        )
        # PMX 编辑器对单个显示框中的项目数存在兼容性差异，按 200 根分组。
        for start in range(1, bone_count, 200):
            stop = min(start + 200, bone_count)
            frame_number = (start - 1) // 200 + 1
            model.display_slots.append(
                pmx.DisplaySlot(
                    f"骨骼{frame_number}",
                    f"Bones{frame_number}",
                    0,
                    [(0, index) for index in range(start, stop)],
                )
            )

    sentinel = 0xFFFF if mesh.version >= 4 else 0xFF
    for position, normal, uv, joints, weights in zip(
        mesh.positions, mesh.normals, mesh.uvs, mesh.joints, mesh.weights
    ):
        joint_values = [
            old_to_new_bone_index[
                _safe_bone_index(value, bone_count, sentinel)
            ]
            for value in joints
        ]
        clean_weights = [max(0.0, float(value)) for value in weights]
        weight_sum = sum(clean_weights)
        if weight_sum <= 1e-8:
            clean_weights = [1.0, 0.0, 0.0, 0.0]
        elif abs(weight_sum - 1.0) > 1e-5:
            clean_weights = [value / weight_sum for value in clean_weights]

        x, y, z = position
        nx, ny, nz = normal
        u, v = uv
        model.vertices.append(
            pmx.Vertex(
                common.Vector3(-x, y, -z),
                common.Vector3(-nx, ny, -nz),
                common.Vector2(u, v),
                pmx.Bdef4(*joint_values, *clean_weights),
                0.0,
            )
        )

    for face in mesh.faces:
        model.indices.extend(face)

    texture_indices: dict[str, int] = {}
    for original, path in (texture_files or {}).items():
        relative = os.path.relpath(path, output_path.parent).replace("/", "\\")
        texture_indices[original] = len(model.textures)
        model.textures.append(relative)

    for index, (_, mesh_face_count, _, _) in enumerate(mesh.submeshes):
        material_def = materials[index] if materials and index < len(materials) else None
        material_name = material_def.name if material_def else f"材质{index}"
        primary_texture = (
            material_primary_texture(material_def) if material_def else None
        )
        texture_index = texture_indices.get(primary_texture, -1)
        diffuse_color = (
            material_def.diffuse_color
            if material_def and material_def.diffuse_color is not None
            else (1.0, 1.0, 1.0, 1.0)
        )
        note = (
            f"Recovered texture: {primary_texture}"
            if texture_index >= 0
            else (
                "Recovered NeoX TintColor"
                if material_def and material_def.diffuse_color is not None
                else "Texture mapping unavailable or confidence too low"
            )
        )
        model.materials.append(
            pmx.Material(
                material_name,
                material_name,
                common.RGB(*diffuse_color[:3]),
                diffuse_color[3],
                1.0,
                common.RGB(0.2, 0.2, 0.2),
                common.RGB(0.5, 0.5, 0.5),
                0,
                common.RGBA(0.0, 0.0, 0.0, 1.0),
                0.0,
                texture_index,
                -1,
                pmx.MATERIALSPHERE_NONE,
                0,
                -1,
                note,
                mesh_face_count * 3,
            )
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    pymeshio.pmx.writer.write_to_file(model, str(output_path))


def save_composite_pmx(
    composite: CompositeModel,
    output_root: Path,
    texture_cache: DecodedTextureCache | None = None,
) -> tuple[Path, bool]:
    """生成一个主体+附件 PMX；返回路径和是否增量复用。"""
    clean_name = re.sub(
        r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", composite.name
    ).strip("_") or "组合模型"
    # 组合输出目录必须区分“同一物理组件、不同 Socket 骨位/矩阵”的变体。
    # 只 hash Mesh MD5 会让两个合法挂载姿态写进同一路径并互相覆盖。
    component_key = json.dumps(
        {
            "meshes": sorted(
                path.stem.rsplit("_", 1)[-1] for path in composite.mesh_paths
            ),
            "static_bone_names": composite.static_bone_names,
            "static_matrices": composite.static_matrices,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    short_hash = hashlib.sha256(component_key.encode("utf-8")).hexdigest()[:8]
    try:
        composite_size = sum(path.stat().st_size for path in composite.mesh_paths)
    except OSError:
        composite_size = 0
    size_bucket = mesh_size_bucket(composite_size)
    model_output = (
        output_root
        / ("带贴图" if composite.direct_merge else "完整组合")
        / size_bucket
        / f"{clean_name}_{short_hash}"
    )
    if composite.direct_merge:
        try:
            import pmx_role_classifier as role_classifier

            primary_paths = [
                primary
                for package in composite.packages
                for material in package.materials
                if (primary := material_primary_texture(material))
            ]
            component_identity = "components:" + "|".join(
                sorted(path.name.lower() for path in composite.mesh_paths)
            )
            role_path = role_classifier.role_path_for_export(
                output_root,
                component_identity,
                clean_name,
                primary_paths,
                [
                    material.name
                    for package in composite.packages
                    for material in package.materials
                ],
                clean_name,
                component_labels=[clean_name],
            )
            if role_path:
                model_output = (
                    output_root / "带贴图" / role_classifier.CLASSIFIED_FOLDER
                    / role_path / size_bucket / f"{clean_name}_{short_hash}"
                )
        except Exception:
            pass
    pmx_path = model_output / f"{clean_name}.pmx"

    fingerprints = [
        pmx_build_fingerprint(path, package)
        for path, package in zip(composite.mesh_paths, composite.packages)
    ]
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "pipeline": PMX_OUTPUT_FORMAT_VERSION,
                "components": fingerprints,
                "static_bone_names": composite.static_bone_names,
                "static_matrices": composite.static_matrices,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    meta_path = model_output / ".build.json"
    if meta_path.is_file() and pmx_path.is_file():
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
            if metadata.get("fingerprint") == fingerprint:
                return pmx_path, True
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    reusable = _find_reusable_pmx_output(
        output_root / ("带贴图" if composite.direct_merge else "完整组合"),
        fingerprint,
        f"{clean_name}.pmx",
    )
    if reusable is not None:
        return reusable[0], True

    # 骨骼管线升级时保留组合模型已有贴图，只重写 PMX。
    meshes = [parse_mesh_for_pmx(path) for path in composite.mesh_paths]
    merged = merge_parsed_meshes(
        meshes,
        composite.static_bone_names,
        composite.static_matrices,
    )

    materials: list[MaterialDefinition] = []
    texture_sources: dict[str, Path] = {}
    for component_index, package in enumerate(composite.packages):
        for material in package.materials:
            textures: dict[str, str] = {}
            for slot, original in material.textures.items():
                source = package.texture_map.get(original)
                key = original
                if (
                    source is not None
                    and key in texture_sources
                    and texture_sources[key] != source
                ):
                    key = f"{original}#部件{component_index + 1}"
                if source is not None:
                    texture_sources[key] = source
                textures[slot] = key
            materials.append(
                MaterialDefinition(
                    material.name,
                    textures,
                    material.diffuse_color,
                )
            )

    texture_files: dict[str, Path] = {}
    used_names: set[str] = set()
    for original, ktx_path in texture_sources.items():
        source_name = original.split("#", 1)[0]
        clean_texture_name = re.sub(
            r"[^0-9A-Za-z_\-\u4e00-\u9fff]+",
            "_",
            Path(source_name.replace("\\", "/")).stem,
        ).strip("_") or "texture"
        png_name = clean_texture_name + ".png"
        if png_name.lower() in used_names:
            png_name = f"{clean_texture_name}_{ktx_path.stem[-8:]}.png"
        used_names.add(png_name.lower())
        png_path = model_output / "textures" / png_name
        if not png_path.is_file() or png_path.stat().st_size == 0:
            if ktx_path.suffix.lower() == ".png":
                # 专项增量审计可直接复用独立 PMX 已验证过的解码贴图，避免
                # 为了补写组合再次扫描全量 THX/KTX 材质索引。
                png_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = png_path.with_suffix(".png.tmp")
                shutil.copyfile(ktx_path, temporary)
                temporary.replace(png_path)
            else:
                ktx_data = ktx_path.read_bytes()
                if texture_cache is not None:
                    texture_cache.materialize(ktx_data, png_path)
                else:
                    decode_ktx_to_png(ktx_data, png_path)
        texture_files[original] = png_path

    save_pmx(merged, pmx_path, clean_name, materials, texture_files)
    write_texture_slot_report(
        model_output / "纹理槽位.csv", materials, texture_files
    )
    meta_path.write_text(
        json.dumps(
            {
                "fingerprint": fingerprint,
                "components": [path.name for path in composite.mesh_paths],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    _register_pmx_build_output(
        output_root / ("带贴图" if composite.direct_merge else "完整组合"),
        fingerprint,
        model_output,
    )
    return pmx_path, False


def write_finished_model_report(output_root: Path) -> tuple[int, int, int]:
    """生成唯一成品清单：确定组合替代其全部独立组件。

    物理独立输出仍作为可重建/审计缓存保留，但不会再作为另一个“版本”出现
    在默认预览里。可选 Socket 组合不带“直接合并”标记，因此不会替代主体。
    返回：(成品总数, 直接组合数, 被隐藏的独立 PMX 数)。
    """
    independent_report = output_root / "纹理恢复报告.csv"
    if not independent_report.is_file():
        return 0, 0, 0
    with independent_report.open(
        "r", newline="", encoding="utf-8-sig"
    ) as stream:
        independent_rows = list(csv.DictReader(stream))

    direct_candidates: list[tuple[int, dict[str, str], frozenset[str]]] = []
    report_specs = (
        (4, output_root / "完整组合报告.csv", True),
        (3, output_root / "名称同族挂件组合报告.csv", False),
        (2, output_root / "共享贴图组合报告.csv", False),
    )
    for priority, report_path, requires_flag in report_specs:
        if not report_path.is_file():
            continue
        with report_path.open(
            "r", newline="", encoding="utf-8-sig"
        ) as stream:
            for row in csv.DictReader(stream):
                if requires_flag and row.get("直接合并", "") != "是":
                    continue
                pmx_path = Path(row.get("PMX", ""))
                components = frozenset(
                    value for value in row.get("组件列表", "").split("|") if value
                )
                if len(components) < 2 or not pmx_path.is_file():
                    continue
                direct_candidates.append((priority, row, components))

    # 同一组件集合优先采用本轮主流程结果；再去掉已被更完整集合包含的子组合。
    exact_best: dict[frozenset[str], tuple[int, dict[str, str]]] = {}
    for priority, row, components in direct_candidates:
        existing = exact_best.get(components)
        if existing is None or priority > existing[0]:
            exact_best[components] = (priority, row)
    selected: list[tuple[frozenset[str], dict[str, str]]] = []
    for components, (_, row) in sorted(
        exact_best.items(), key=lambda item: len(item[0]), reverse=True
    ):
        if any(components < other for other, _ in selected):
            continue
        selected.append((components, row))

    replaced_components = set().union(
        *(components for components, _ in selected)
    ) if selected else set()
    finished_rows: list[list[object]] = []
    hidden_independent = 0
    for row in independent_rows:
        if row.get("结果") != "已绑定主贴图":
            continue
        pmx_path = Path(row.get("PMX", ""))
        if not pmx_path.is_file():
            continue
        source_mesh = row.get("源Mesh", "")
        if source_mesh in replaced_components:
            hidden_independent += 1
            continue
        finished_rows.append(
            [
                source_mesh,
                row.get("模型名", "") or pmx_path.stem,
                str(pmx_path),
                row.get("源Mesh大小", "0") or 0,
                "独立成品",
                1,
                source_mesh,
            ]
        )

    for components, row in selected:
        pmx_path = Path(row["PMX"])
        model_name = re.sub(
            r"_(?:共享贴图完整|完整)$", "", row.get("模型名", pmx_path.stem)
        )
        finished_rows.append(
            [
                row.get("源Mesh", ""),
                model_name,
                str(pmx_path),
                row.get("源Mesh大小", "0") or 0,
                "已直接合并",
                len(components),
                "|".join(sorted(components)),
            ]
        )

    unique_by_pmx: dict[str, list[object]] = {}
    for row in finished_rows:
        key = os.path.normcase(os.path.abspath(str(row[2])))
        existing = unique_by_pmx.get(key)
        if existing is None or (
            row[4] == "已直接合并" and existing[4] != "已直接合并"
        ):
            unique_by_pmx[key] = row
    finished_rows = list(unique_by_pmx.values())
    finished_rows.sort(
        key=lambda row: (
            archive_index(Path(str(row[0]))) is None,
            archive_index(Path(str(row[0])))
            if archive_index(Path(str(row[0]))) is not None else 0,
            str(row[1]).lower(),
        )
    )
    finished_report = output_root / "成品模型报告.csv"
    with finished_report.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "源Mesh", "模型名", "PMX", "源Mesh大小",
                "成品类型", "组件数", "组件列表",
            ]
        )
        writer.writerows(finished_rows)
    return len(finished_rows), len(selected), hidden_independent


_SCENE_IDENTITY_MATRIX = (
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
)


def _read_scene_xml_root(path: Path) -> ET.Element:
    """读取新旧 SCN XML；旧资源中的中文筛选名可能是 GBK/混合编码。"""
    data = path.read_bytes()
    try:
        return ET.fromstring(data)
    except (ET.ParseError, UnicodeError):
        # 部分旧 SCN 没有 XML encoding 声明，甚至夹有损坏的编辑器中文标签。
        # 这些文字不参与模型布局；替换坏字符可完整保留结构与数值属性。
        return ET.fromstring(data.decode("gb18030", errors="replace"))


def _scene_float_vector(
    raw: str | None,
    count: int,
    default: tuple[float, ...],
) -> tuple[float, ...]:
    if not raw:
        return default
    try:
        values = tuple(float(item.strip()) for item in raw.split(","))
    except ValueError:
        return default
    return values if len(values) == count else default


def _scene_node_matrix(node: ET.Element) -> tuple[float, ...]:
    position = _scene_float_vector(
        node.get("Position"), 3, (0.0, 0.0, 0.0)
    )
    scale = _scene_float_vector(node.get("Scale"), 3, (1.0, 1.0, 1.0))
    rotation = _scene_float_vector(
        node.get("Rotation"), 16, _SCENE_IDENTITY_MATRIX
    )
    scale_matrix = (
        scale[0], 0.0, 0.0, 0.0,
        0.0, scale[1], 0.0, 0.0,
        0.0, 0.0, scale[2], 0.0,
        0.0, 0.0, 0.0, 1.0,
    )
    translation = (
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        position[0], position[1], position[2], 1.0,
    )
    # NeoX SCN 使用 row-vector：局部点依次经过 Scale、Rotation、Position。
    return _matrix4_multiply(
        _matrix4_multiply(scale_matrix, tuple(rotation)), translation
    )


def _scene_linear_determinant(matrix: tuple[float, ...]) -> float:
    a, b, c = matrix[0], matrix[1], matrix[2]
    d, e, f = matrix[4], matrix[5], matrix[6]
    g, h, i = matrix[8], matrix[9], matrix[10]
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def _scene_normal_matrix(matrix: tuple[float, ...]) -> tuple[float, ...]:
    inverse = _inverse_affine_row_matrix4(matrix)
    # row-vector 法线使用 (L^-1)^T；平移必须清零。
    return (
        inverse[0], inverse[4], inverse[8], 0.0,
        inverse[1], inverse[5], inverse[9], 0.0,
        inverse[2], inverse[6], inverse[10], 0.0,
        0.0, 0.0, 0.0, 1.0,
    )


def _scene_file_table(parent: ET.Element | None) -> dict[int, str]:
    result: dict[int, str] = {}
    if parent is None:
        return result
    for node in list(parent):
        match = re.fullmatch(r"File_(\d+)", node.tag, re.IGNORECASE)
        if not match:
            continue
        value = (node.get("Path") or "").strip().replace("\\", "/")
        if value:
            result[int(match.group(1))] = value
    return result


def _scene_display_identity(root: ET.Element, path: Path) -> tuple[str, str]:
    global_node = root.find(".//Global")
    content_path = (
        (global_node.get("ContentPath") or "").strip().replace("\\", "/")
        if global_node is not None else ""
    )
    display = content_path
    if display.lower().endswith("_content"):
        display = display[:-8]
    display = display.strip("/") or extracted_resource_label(path) or path.stem
    return display, content_path


def scan_scene_catalog(
    scene_root: Path,
    progress: Callable[[int, int], None] | None = None,
) -> list[SceneCatalogEntry]:
    files = sorted(scene_root.rglob("*.xml"), key=lambda item: item.name.lower())
    entries: list[SceneCatalogEntry] = []
    for number, path in enumerate(files, 1):
        try:
            root = _read_scene_xml_root(path)
            entities = root.find(".//Entities")
            if entities is None:
                continue
            models_node = entities.find("Models")
            direct_count = (
                sum(1 for node in list(models_node) if node.tag.lower() == "model")
                if models_node is not None else 0
            )
            component_groups_node = entities.find("ComponentGroups/Groups")
            component_groups = (
                list(component_groups_node)
                if component_groups_node is not None else []
            )
            component_count = sum(
                len(group.findall("model")) for group in component_groups
            )
            model_count = direct_count + component_count
            if not model_count:
                continue
            fx_node = entities.find("Fxes")
            effect_count = len(list(fx_node)) if fx_node is not None else 0
            effect_count += sum(
                len(group.findall("sfx")) for group in component_groups
            )
            display, content_path = _scene_display_identity(root, path)
            entries.append(
                SceneCatalogEntry(
                    source_path=path.resolve(),
                    display_name=display,
                    content_path=content_path,
                    model_count=model_count,
                    component_group_count=len(component_groups),
                    effect_count=effect_count,
                )
            )
        except (OSError, ET.ParseError, UnicodeError, ValueError):
            continue
        finally:
            if progress and (number % 50 == 0 or number == len(files)):
                progress(number, len(files))
    entries.sort(key=lambda item: item.display_name.lower())
    return entries


def load_scene_instances(entry: SceneCatalogEntry) -> list[SceneModelInstance]:
    root = _read_scene_xml_root(entry.source_path)
    entities = root.find(".//Entities")
    if entities is None:
        return []
    files = _scene_file_table(entities.find("AllFiles"))
    result: list[SceneModelInstance] = []

    def add_model(
        node: ET.Element,
        parent_matrix: tuple[float, ...] | None = None,
        component_group: str | None = None,
    ) -> None:
        try:
            file_index = int(node.get("FilePathIndex") or "-1")
        except ValueError:
            return
        logical_gim = files.get(file_index, "")
        if not logical_gim.lower().endswith(".gim"):
            return
        transform = _scene_node_matrix(node)
        if parent_matrix is not None:
            transform = _matrix4_multiply(transform, parent_matrix)
        material_override = (node.get("MaterialGroupFile") or "").strip()
        result.append(
            SceneModelInstance(
                name=(node.get("Name") or Path(logical_gim).stem).strip(),
                uuid=(node.get("UUID") or "").strip(),
                logical_gim=logical_gim,
                material_override=material_override or None,
                transform=transform,
                component_group=component_group,
            )
        )

    models_node = entities.find("Models")
    if models_node is not None:
        for node in list(models_node):
            if node.tag.lower() == "model":
                add_model(node)

    component_groups = entities.find("ComponentGroups/Groups")
    if component_groups is not None:
        for group in list(component_groups):
            group_matrix = _scene_node_matrix(group)
            group_name = (group.get("Name") or group.tag).strip()
            for node in group.findall("model"):
                add_model(node, group_matrix, group_name)
    return result


def ensure_scene_cache(
    source_root: Path | None,
    unpacked_root: Path,
    log: Callable[[str], None] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> Path:
    """增量解包 scene 包；无游戏源目录时允许直接使用已有缓存。"""
    scene_root = unpacked_root / "scene"
    if source_root is None:
        if any(scene_root.rglob("*.xml")):
            return scene_root
        raise RuntimeError("没有场景解包缓存；请选择包含 scene.idx 的完整 yys 目录")

    import onmyoji_wpk_gui as wpk

    groups = wpk.discover_groups(
        source_root,
        progress=progress,
        stems={"scene"},
    )
    group = next((item for item in groups if item.stem == "scene"), None)
    if group is None:
        raise RuntimeError("所选游戏资源中没有 scene.idx / scene*.wpk")
    if not _manifest_matches_idx(scene_root, group.records):
        if log:
            log("正在增量解包 scene 场景描述；未变化内容直接复用。")
        engine = wpk.ExtractorEngine(
            log or (lambda _text: None),
            lambda done, total, text: (
                progress("scene", done, total) if progress else None
            ),
            threading.Event(),
        )
        engine.extract_groups([group], unpacked_root, None, True)
    elif log:
        log("scene 场景描述缓存与当前索引一致，直接复用。")
    return scene_root


class SceneResourceResolver:
    """用逻辑路径 THX 哈希精确解析场景的 GIM/Mesh/MTG/KTX。"""

    def __init__(
        self,
        thd_dir: Path,
        source_root: Path | None,
        unpacked_root: Path,
        log: Callable[[str], None] | None = None,
    ):
        import onmyoji_wpk_gui as wpk
        from thd_resource_index import (
            cloudfilesys_name_hash,
            read_model_thx,
            read_thx_namehash_seeds,
        )

        self.wpk = wpk
        self.cloudfilesys_name_hash = cloudfilesys_name_hash
        self.unpacked_root = unpacked_root
        self.cache_root = unpacked_root / "scene_resources"
        self.source_root = source_root
        self.log = log
        self.result_cache: dict[str, Path | None] = {}
        self.physical_by_digest: dict[str, Path] = {}
        self.thx_sources: dict[
            str, tuple[dict[int, object], tuple[int, ...]]
        ] = {}
        self.content_records: dict[str, list[tuple[Path, object]]] = defaultdict(list)
        self.loaded_archive_stems: set[str] = set()
        self.handles: dict[Path, object] = {}
        self.zstandard_module = None
        self.extracted_count = 0
        self.base_url = ""
        cloud_config = thd_dir.parent / "cloud.json"
        if cloud_config.is_file():
            try:
                cloud_value = json.loads(cloud_config.read_text(encoding="utf-8"))
                self.base_url = str(cloud_value.get("base_url") or "").strip()
            except (OSError, ValueError, json.JSONDecodeError):
                self.base_url = ""

        thd_roots = [thd_dir]
        extension_root = thd_dir.parent / "thdext1"
        if extension_root.is_dir():
            thd_roots.append(extension_root)
        for root in thd_roots:
            for thx_path in sorted(root.glob("*.thx")):
                try:
                    records = {
                        record.name_hash: record
                        for record in read_model_thx(thx_path)
                    }
                    seeds = read_thx_namehash_seeds(thx_path)
                except Exception:
                    continue
                self.thx_sources[thx_path.stem.lower()] = (records, seeds)

        # 不能对 unpacked 做 rglob：model/levelsets 常有二十多万个实体文件，
        # 仅为了找十几个清单就遍历全树会让窗口看似卡死数分钟。
        manifest_candidates = list(unpacked_root.glob("*/manifest.csv"))
        manifest_candidates.extend(
            unpacked_root.glob("extra_rigged/*/manifest.csv")
        )
        manifest_candidates.extend(
            unpacked_root.glob("extra_rigged/*/material_manifest.csv")
        )
        for manifest in dict.fromkeys(path.resolve() for path in manifest_candidates):
            self._load_manifest(manifest)
        for path in (unpacked_root / "cross_package_textures").rglob("*.ktx"):
            digest = path.stem.lower()
            if re.fullmatch(r"[0-9a-f]{32}", digest):
                self.physical_by_digest.setdefault(digest, path.resolve())
        for path in self.cache_root.glob("*/*"):
            digest = path.stem.lower()
            if path.is_file() and re.fullmatch(r"[0-9a-f]{32}", digest):
                self.physical_by_digest.setdefault(digest, path.resolve())

    def _load_manifest(self, manifest: Path) -> None:
        try:
            with manifest.open("r", newline="", encoding="utf-8-sig") as stream:
                for row in csv.DictReader(stream):
                    digest = (row.get("resource_hash") or "").strip().lower()
                    relative = (row.get("output_path") or "").strip()
                    status = (row.get("status") or "").strip().lower()
                    if not re.fullmatch(r"[0-9a-f]{32}", digest) or not relative:
                        continue
                    if status and status not in {"ok", "rigged", "static_mesh"}:
                        continue
                    rel = Path(relative.replace("\\", "/"))
                    # 标准解包清单写 ``levelsets/pkg_...``，额外缓存清单写
                    # 相对本组目录的路径。这里可由首段直接确定，无需为十几万行
                    # 逐个调用 Path.is_file()；实体存在性留到真正解析该 MD5 时检查。
                    if rel.parts and rel.parts[0].lower() == manifest.parent.name.lower():
                        path = manifest.parent.parent / rel
                    else:
                        path = manifest.parent / rel
                    self.physical_by_digest.setdefault(
                        digest, Path(os.path.abspath(path))
                    )
        except (OSError, UnicodeError, csv.Error):
            return

    @staticmethod
    def _preferred_packages(reference: str) -> tuple[str, ...]:
        normalized = reference.strip().replace("\\", "/").lower().lstrip("/")
        prefix = normalized.split("/", 1)[0]
        table = {
            "levelsets": ("levelsets", "res"),
            "lbslevelsets": ("levelsets", "res"),
            "model": ("model", "res"),
            "static": ("static", "res"),
            "scene": ("scene", "res"),
            "fx": ("fx_model", "fx", "fx_texture", "res"),
            "natural": ("res", "levelsets"),
        }
        return table.get(prefix, (prefix, "res"))

    def _record_hits(
        self, reference: str, packages: tuple[str, ...] | None = None
    ) -> dict[str, object]:
        hits: dict[str, object] = {}
        names = packages or tuple(self.thx_sources)
        for package_name in names:
            source = self.thx_sources.get(package_name)
            if source is None:
                continue
            records, seeds = source
            for variant in _package_reference_variants(reference, package_name):
                for seed in seeds:
                    name_hash = self.cloudfilesys_name_hash(
                        variant, package_name, seed
                    )
                    record = records.get(name_hash)
                    if record is not None:
                        hits.setdefault(record.content_md5.lower(), record)
        return hits

    def _load_archive_stem(self, stem: str) -> None:
        stem = stem.lower()
        if stem in self.loaded_archive_stems or self.source_root is None:
            return
        self.loaded_archive_stems.add(stem)
        idx_path = self.source_root / f"{stem}.idx"
        if not idx_path.is_file():
            return
        try:
            _marker, records = self.wpk.parse_idx(idx_path)
        except Exception:
            return
        pattern = re.compile(rf"^{re.escape(stem)}(\d+)\.wpk$", re.IGNORECASE)
        packages: dict[int, Path] = {}
        for path in self.source_root.glob(f"{stem}*.wpk"):
            match = pattern.fullmatch(path.name)
            if match:
                packages[int(match.group(1))] = path.resolve()
        for record in records:
            package = packages.get(record.package_id)
            if not self.wpk.record_is_active(record) or package is None:
                continue
            self.content_records[record.resource_hash.lower()].append(
                (package, record)
            )

    def _extract_digest(
        self,
        digest: str,
        preferred_packages: tuple[str, ...],
        thx_record: object | None = None,
    ) -> Path | None:
        existing = self.physical_by_digest.get(digest)
        if existing is not None and existing.is_file():
            return existing
        for stem in preferred_packages:
            self._load_archive_stem(stem)
            if self.content_records.get(digest):
                break
        if not self.content_records.get(digest):
            for stem in self.thx_sources:
                self._load_archive_stem(stem)
                if self.content_records.get(digest):
                    break
        for package_path, record in self.content_records.get(digest, []):
            try:
                stream = self.handles.get(package_path)
                if stream is None:
                    stream = package_path.open("rb")
                    self.handles[package_path] = stream
                read_size = self.wpk.record_read_size(record)
                stream.seek(record.offset)
                blob = stream.read(read_size)
                if len(blob) != read_size:
                    continue
                decoded, _ = self.wpk.decode_stage1(blob, record.key_length)
                if self.zstandard_module is None:
                    self.zstandard_module = self.wpk.load_zstandard()
                decoded, _ = self.wpk.unwrap_payload(
                    decoded, self.zstandard_module
                )
                if hashlib.md5(decoded).hexdigest() != digest:
                    continue
                extension = self.wpk.detect_extension(decoded)
                target = self.cache_root / digest[:2] / f"{digest}.{extension}"
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(target.suffix + ".tmp")
                temporary.write_bytes(decoded)
                temporary.replace(target)
                self.physical_by_digest[digest] = target.resolve()
                self.extracted_count += 1
                return target.resolve()
            except Exception:
                continue
        return self._download_digest(digest, thx_record)

    def _download_digest(
        self, digest: str, thx_record: object | None
    ) -> Path | None:
        """本地 WPK 缺项时，按 THX 内容 MD5 从官方动态仓库精确补取。"""
        if not self.base_url or thx_record is None:
            return None
        expected_size = int(getattr(thx_record, "size", 0) or 0)
        if expected_size < 8 or expected_size > 512 * 1024 * 1024:
            return None
        try:
            import urllib.request

            url = self.base_url.rstrip("/") + f"/dynamic/{digest[:2]}/{digest[2:]}"
            request = urllib.request.Request(
                url, headers={"User-Agent": "OnmyojiResourceTool/1.0"}
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                blob = response.read(expected_size + 1)
            if len(blob) != expected_size:
                return None
            decoded, _ = self.wpk.decode_stage1(blob, len(blob))
            if self.zstandard_module is None:
                self.zstandard_module = self.wpk.load_zstandard()
            decoded, _ = self.wpk.unwrap_payload(decoded, self.zstandard_module)
            if hashlib.md5(decoded).hexdigest() != digest:
                return None
            extension = self.wpk.detect_extension(decoded)
            target = self.cache_root / digest[:2] / f"{digest}.{extension}"
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_bytes(decoded)
            temporary.replace(target)
            self.physical_by_digest[digest] = target.resolve()
            self.extracted_count += 1
            return target.resolve()
        except Exception:
            return None

    def resolve(self, reference: str) -> Path | None:
        key = reference.strip().replace("\\", "/").lower()
        if key in self.result_cache:
            return self.result_cache[key]

        preferred = self._preferred_packages(reference)
        hits: dict[str, object] = {}
        chosen_packages = preferred
        # 路径首段就是 NeoX 包身份。先逐包按优先级解析，避免旧资源同时在
        # res 兼容池中保留另一版本时，把本应明确的 levelsets/static 判成歧义。
        for package_name in preferred:
            package_hits = self._record_hits(reference, (package_name,))
            if package_hits:
                hits = package_hits
                chosen_packages = (package_name,)
                break
        if not hits:
            hits = self._record_hits(reference)
            chosen_packages = tuple(self.thx_sources)
        # 精确逻辑路径若在多个包中指向不同内容，不能擅自选一个。
        if len(hits) != 1:
            self.result_cache[key] = None
            return None
        digest = next(iter(hits))
        result = self._extract_digest(digest, chosen_packages, hits[digest])
        self.result_cache[key] = result
        return result

    def close(self) -> None:
        for stream in self.handles.values():
            try:
                stream.close()
            except Exception:
                pass
        self.handles.clear()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback_value):
        self.close()


def _scene_reference(reference: str, anchor: str) -> str:
    value = reference.strip().replace("\\", "/")
    if not value:
        return ""
    # 清理编辑器遗留绝对路径，只接受其中 res 后面的稳定资源逻辑名。
    drive_match = re.match(r"^[A-Za-z]:/(?:.*?/)?res/(.+)$", value, re.IGNORECASE)
    if drive_match:
        value = drive_match.group(1)
    known_prefixes = (
        "levelsets/", "lbslevelsets/", "model/", "static/", "scene/",
        "fx/", "natural/", "res/",
    )
    if value.lower().startswith(known_prefixes):
        return value
    parent = anchor.replace("\\", "/").rsplit("/", 1)[0]
    return f"{parent}/{value}" if parent else value


def _scene_gim_material_reference(path: Path) -> str | None:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError, UnicodeError):
        return None
    for node in root.iter():
        for key, value in node.attrib.items():
            if key.lower() not in {"materialgroup", "materialgroupfile", "mtg"}:
                continue
            normalized = (value or "").strip().replace("\\", "/")
            if normalized.lower().endswith(".mtg"):
                return normalized
    return None


def _scene_materials_for_instance(
    instance: SceneModelInstance,
    mesh: ParsedMesh,
    gim_path: Path,
    resolver: SceneResourceResolver,
) -> tuple[
    list[MaterialDefinition], dict[str, Path], str | None, str | None
]:
    gim_submeshes = parse_gim_submeshes(gim_path)
    mtg_reference = instance.material_override
    if mtg_reference:
        mtg_reference = _scene_reference(mtg_reference, instance.logical_gim)
    if not mtg_reference:
        declared = _scene_gim_material_reference(gim_path)
        mtg_reference = (
            _scene_reference(declared, instance.logical_gim)
            if declared else instance.logical_gim[:-4] + ".mtg"
        )
    mtg_path = resolver.resolve(mtg_reference)
    source_materials = parse_material_xml(mtg_path) if mtg_path else []
    issue = None if source_materials else "找不到精确 MaterialGroup"

    if gim_submeshes and len(gim_submeshes) == len(mesh.submeshes):
        ordered, _ = order_materials_by_gim_partial(
            source_materials, gim_submeshes
        )
    else:
        ordered = list(source_materials[: len(mesh.submeshes)])
    while len(ordered) < len(mesh.submeshes):
        sub_index = len(ordered)
        sub_name = (
            gim_submeshes[sub_index].name
            if sub_index < len(gim_submeshes)
            else f"{instance.name}_材质{sub_index}"
        )
        ordered.append(MaterialDefinition(sub_name, {}))

    textures: dict[str, Path] = {}
    for material in ordered:
        primary = material_primary_texture(material)
        if not primary:
            continue
        logical = _scene_reference(primary, mtg_reference)
        source = resolver.resolve(logical)
        if source is not None:
            # save_pmx 按材质里保留的原始键查找。
            textures[primary] = source
    expected_primary = len({
        material_primary_texture(material)
        for material in ordered
        if material_primary_texture(material)
    })
    if expected_primary and len(textures) < expected_primary:
        issue = (
            f"主贴图未完全解析：{len(textures)}/"
            f"{expected_primary}"
        )
    return ordered, textures, mtg_reference if mtg_path else None, issue


def _scene_empty_chunk() -> dict[str, object]:
    return {
        "version": 2,
        "submeshes": [],
        "positions": [],
        "normals": [],
        "faces": [],
        "uvs": [],
        "materials": [],
        "textures": {},
        "instances": [],
    }


def _append_scene_instance(
    chunk: dict[str, object],
    mesh: ParsedMesh,
    instance: SceneModelInstance,
    materials: list[MaterialDefinition],
    textures: dict[str, Path],
) -> None:
    positions: list[tuple[float, float, float]] = chunk["positions"]  # type: ignore[assignment]
    normals: list[tuple[float, float, float]] = chunk["normals"]  # type: ignore[assignment]
    faces: list[tuple[int, int, int]] = chunk["faces"]  # type: ignore[assignment]
    vertex_offset = len(positions)
    normal_matrix = _scene_normal_matrix(instance.transform)
    positions.extend(
        _transform_row_position(value, instance.transform)
        for value in mesh.positions
    )
    for value in mesh.normals:
        if not all(math.isfinite(item) for item in value):
            value = (0.0, 1.0, 0.0)
        transformed = _transform_row_normal(value, normal_matrix)
        normals.append(
            transformed
            if all(math.isfinite(item) for item in transformed)
            else (0.0, 1.0, 0.0)
        )
    reverse_winding = _scene_linear_determinant(instance.transform) < 0.0
    if reverse_winding:
        faces.extend(
            (a + vertex_offset, c + vertex_offset, b + vertex_offset)
            for a, b, c in mesh.faces
        )
    else:
        faces.extend(
            (a + vertex_offset, b + vertex_offset, c + vertex_offset)
            for a, b, c in mesh.faces
        )
    chunk["uvs"].extend(mesh.uvs)  # type: ignore[union-attr]
    chunk["submeshes"].extend(mesh.submeshes)  # type: ignore[union-attr]
    # 两个不同目录的 MTG 可能都写相对 ``diffuse.tga``。PMX 的贴图表以
    # 字符串为键，必须在实体不同时改成内部唯一键，否则后加入者会覆盖前者。
    chunk_textures: dict[str, Path] = chunk["textures"]  # type: ignore[assignment]
    remapped_materials: list[MaterialDefinition] = []
    for material in materials:
        material_textures = dict(material.textures)
        for slot, original in list(material_textures.items()):
            source = textures.get(original)
            if source is None:
                continue
            key = original
            existing = chunk_textures.get(key)
            if (
                existing is not None
                and os.path.normcase(os.path.abspath(existing))
                != os.path.normcase(os.path.abspath(source))
            ):
                suffix = hashlib.md5(str(source).encode("utf-8")).hexdigest()[:8]
                key = f"{original}#场景{suffix}"
                material_textures[slot] = key
            chunk_textures[key] = source
        remapped_materials.append(
            MaterialDefinition(
                material.name, material_textures, material.diffuse_color
            )
        )
    chunk["materials"].extend(remapped_materials)  # type: ignore[union-attr]
    chunk["instances"].append(instance)  # type: ignore[union-attr]
    chunk["version"] = max(int(chunk["version"]), mesh.version)


def _scene_chunk_mesh(chunk: dict[str, object]) -> ParsedMesh:
    positions = list(chunk["positions"])
    return ParsedMesh(
        version=int(chunk["version"]),
        submeshes=list(chunk["submeshes"]),
        bone_parents=[-1],
        bone_names=["__scene_root__"],
        bone_matrices=[_SCENE_IDENTITY_MATRIX],
        positions=positions,
        normals=list(chunk["normals"]),
        faces=list(chunk["faces"]),
        uvs=list(chunk["uvs"]),
        joints=[(0, 0, 0, 0) for _ in positions],
        weights=[(1.0, 0.0, 0.0, 0.0) for _ in positions],
    )


def _optimize_scene_material_batches(chunk: dict[str, object]) -> None:
    """按 PMX 最终可见材质合并面段，避免一实例一次 draw call。"""
    submeshes: list[tuple[int, int, int, int]] = list(chunk["submeshes"])
    materials: list[MaterialDefinition] = list(chunk["materials"])
    faces: list[tuple[int, int, int]] = list(chunk["faces"])
    texture_sources: dict[str, Path] = dict(chunk["textures"])
    buckets: dict[
        tuple[object, ...], tuple[MaterialDefinition, list[tuple[int, int, int]]]
    ] = {}
    face_offset = 0
    for index, submesh in enumerate(submeshes):
        face_count = submesh[1]
        segment = faces[face_offset : face_offset + face_count]
        face_offset += face_count
        material = (
            materials[index]
            if index < len(materials)
            else MaterialDefinition(f"材质{index}", {})
        )
        primary = material_primary_texture(material)
        source = texture_sources.get(primary) if primary else None
        if source is not None:
            texture_identity: object = os.path.normcase(os.path.abspath(source))
        elif primary:
            # 未解析的两个逻辑贴图不能因为都呈白色就被误认为同一材质。
            texture_identity = "missing:" + primary.replace("\\", "/").lower()
        else:
            texture_identity = None
        color = material.diffuse_color or (1.0, 1.0, 1.0, 1.0)
        signature = (
            texture_identity,
            tuple(round(float(value), 6) for value in color),
        )
        bucket = buckets.get(signature)
        if bucket is None:
            buckets[signature] = (material, list(segment))
        else:
            bucket[1].extend(segment)

    if face_offset < len(faces):
        material = MaterialDefinition("未分配材质", {})
        signature = (None, (1.0, 1.0, 1.0, 1.0))
        bucket = buckets.get(signature)
        if bucket is None:
            buckets[signature] = (material, list(faces[face_offset:]))
        else:
            bucket[1].extend(faces[face_offset:])

    optimized_faces: list[tuple[int, int, int]] = []
    optimized_submeshes: list[tuple[int, int, int, int]] = []
    optimized_materials: list[MaterialDefinition] = []
    for material, segment in buckets.values():
        if not segment:
            continue
        optimized_faces.extend(segment)
        optimized_submeshes.append((0, len(segment), 1, 0))
        optimized_materials.append(material)
    chunk["faces"] = optimized_faces
    chunk["submeshes"] = optimized_submeshes
    chunk["materials"] = optimized_materials


def _scene_export_fingerprint(entry: SceneCatalogEntry, thd_dir: Path) -> str:
    source_digest = hashlib.md5(entry.source_path.read_bytes()).hexdigest()
    thx_stamps = [
        _file_build_stamp(path)
        for path in sorted(thd_dir.glob("*.thx"), key=lambda item: item.name.lower())
    ]
    payload = json.dumps(
        {
            "pipeline": SCENE_PMX_PIPELINE_VERSION,
            "pmx": PMX_OUTPUT_FORMAT_VERSION,
            "scene": source_digest,
            "thx": thx_stamps,
            "vertex_limit": SCENE_PMX_VERTEX_LIMIT,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def export_scene_pmx(
    entry: SceneCatalogEntry,
    output_root: Path,
    resolver: SceneResourceResolver,
    thd_dir: Path,
    texture_cache: DecodedTextureCache,
    fast_reuse: bool = True,
    progress: Callable[[str, int, int], None] | None = None,
) -> tuple[Path, int, int, int, bool]:
    """导出一个摆放完成的静态场景；返回目录/分块/成功实例/失败实例/复用。"""
    clean_name = re.sub(
        r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", entry.display_name
    ).strip("_") or "scene"
    scene_key = hashlib.sha1(
        str(entry.source_path).encode("utf-8", errors="replace")
    ).hexdigest()[:8]
    scene_output = output_root / f"{clean_name}_{scene_key}"
    meta_path = scene_output / ".build.json"
    fingerprint = _scene_export_fingerprint(entry, thd_dir)
    old_metadata: dict[str, object] = {}
    if meta_path.is_file():
        try:
            old_metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            old_metadata = {}
    old_outputs = [
        scene_output / str(value)
        for value in old_metadata.get("outputs", [])
        if isinstance(value, str)
    ]
    if (
        fast_reuse
        and old_metadata.get("fingerprint") == fingerprint
        and int(old_metadata.get("failed_instances", 0)) == 0
        and int(old_metadata.get("resource_warnings", 0)) == 0
        and old_outputs
        and all(path.is_file() for path in old_outputs)
    ):
        return (
            scene_output,
            len(old_outputs),
            int(old_metadata.get("resolved_instances", 0)),
            int(old_metadata.get("failed_instances", 0)),
            True,
        )

    instances = load_scene_instances(entry)
    mesh_cache: dict[str, tuple[Path, ParsedMesh, Path] | None] = {}
    material_cache: dict[
        tuple[str, str], tuple[
            list[MaterialDefinition], dict[str, Path], str | None, str | None
        ]
    ] = {}
    chunks: list[dict[str, object]] = []
    chunk = _scene_empty_chunk()
    layout_rows: list[dict[str, object]] = []
    missing_rows: list[list[object]] = []
    resolved_count = 0
    failed_instance_count = 0
    resource_warning_count = 0

    for number, instance in enumerate(instances, 1):
        reason = ""
        mesh_path: Path | None = None
        mtg_reference: str | None = None
        try:
            cached_mesh = mesh_cache.get(instance.logical_gim)
            if instance.logical_gim not in mesh_cache:
                gim_path = resolver.resolve(instance.logical_gim)
                if gim_path is None:
                    mesh_cache[instance.logical_gim] = None
                else:
                    declared_mesh = parse_gim_mesh_reference(gim_path)
                    mesh_reference = (
                        _scene_reference(declared_mesh, instance.logical_gim)
                        if declared_mesh
                        else instance.logical_gim[:-4] + ".mesh"
                    )
                    mesh_path = resolver.resolve(mesh_reference)
                    if mesh_path is None:
                        mesh_cache[instance.logical_gim] = None
                    else:
                        parsed_mesh = parse_mesh(mesh_path)
                        if any(
                            not all(math.isfinite(value) for value in position)
                            for position in parsed_mesh.positions
                        ):
                            mesh_cache[instance.logical_gim] = None
                        else:
                            mesh_cache[instance.logical_gim] = (
                                gim_path, parsed_mesh, mesh_path
                            )
                cached_mesh = mesh_cache.get(instance.logical_gim)
            if cached_mesh is None:
                raise MeshFormatError("找不到精确 GIM 或 Mesh")
            gim_path, mesh, mesh_path = cached_mesh
            if abs(_scene_linear_determinant(instance.transform)) <= 1e-10:
                raise MeshFormatError("实例变换矩阵不可逆")

            material_key = (
                instance.logical_gim.lower(),
                (instance.material_override or "").replace("\\", "/").lower(),
            )
            cached_material = material_cache.get(material_key)
            if cached_material is None:
                cached_material = _scene_materials_for_instance(
                    instance, mesh, gim_path, resolver
                )
                material_cache[material_key] = cached_material
            materials, textures, mtg_reference, material_issue = cached_material

            if (
                chunk["positions"]
                and len(chunk["positions"]) + len(mesh.positions)
                > SCENE_PMX_VERTEX_LIMIT
            ):
                chunks.append(chunk)
                chunk = _scene_empty_chunk()
            chunk_index = len(chunks) + 1
            _append_scene_instance(chunk, mesh, instance, materials, textures)
            resolved_count += 1
            if material_issue:
                resource_warning_count += 1
                missing_rows.append(
                    [
                        "材质/贴图", instance.name, instance.uuid,
                        instance.logical_gim, instance.material_override or "",
                        material_issue,
                    ]
                )
            layout_rows.append(
                {
                    "name": instance.name,
                    "uuid": instance.uuid,
                    "gim": instance.logical_gim,
                    "mesh": str(mesh_path),
                    "material_group": mtg_reference,
                    "component_group": instance.component_group,
                    "chunk": chunk_index,
                    "source_transform": list(instance.transform),
                    "pmx_coordinate_rule": "(-x, y, -z)",
                    "status": (
                        f"ok; {material_issue}" if material_issue else "ok"
                    ),
                }
            )
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            failed_instance_count += 1
            missing_rows.append(
                [
                    "几何", instance.name, instance.uuid,
                    instance.logical_gim, instance.material_override or "", reason,
                ]
            )
            layout_rows.append(
                {
                    "name": instance.name,
                    "uuid": instance.uuid,
                    "gim": instance.logical_gim,
                    "mesh": str(mesh_path) if mesh_path else None,
                    "material_group": mtg_reference,
                    "component_group": instance.component_group,
                    "chunk": None,
                    "source_transform": list(instance.transform),
                    "pmx_coordinate_rule": "(-x, y, -z)",
                    "status": reason,
                }
            )
        if progress and (number % 10 == 0 or number == len(instances)):
            progress("解析并摆放模型", number, len(instances))
    if chunk["positions"]:
        chunks.append(chunk)
    if not chunks:
        raise RuntimeError(f"{entry.display_name} 没有可导出的模型实例")

    for current in chunks:
        _optimize_scene_material_batches(current)

    scene_output.mkdir(parents=True, exist_ok=True)
    materialized: dict[tuple[str, str], Path] = {}
    texture_total = len({
        (original, str(source))
        for current in chunks
        for original, source in current["textures"].items()
    })
    texture_done = 0
    output_paths: list[Path] = []
    for index, current in enumerate(chunks, 1):
        texture_files: dict[str, Path] = {}
        for original, source in current["textures"].items():
            cache_key = (original, str(source))
            png_path = materialized.get(cache_key)
            if png_path is None:
                clean_texture = re.sub(
                    r"[^0-9A-Za-z_\-\u4e00-\u9fff]+",
                    "_",
                    Path(original.replace("\\", "/")).stem,
                ).strip("_") or "texture"
                suffix = hashlib.md5(str(source).encode("utf-8")).hexdigest()[:8]
                png_path = scene_output / "textures" / f"{clean_texture}_{suffix}.png"
                if source.suffix.lower() == ".png":
                    png_path.parent.mkdir(parents=True, exist_ok=True)
                    if not png_path.is_file():
                        shutil.copyfile(source, png_path)
                else:
                    texture_cache.materialize(source.read_bytes(), png_path)
                materialized[cache_key] = png_path
                texture_done += 1
                if progress:
                    progress("解码场景贴图", texture_done, texture_total)
            texture_files[original] = png_path

        pmx_name = (
            f"{clean_name}.pmx" if len(chunks) == 1
            else f"{clean_name}_分块{index:03d}.pmx"
        )
        pmx_path = scene_output / pmx_name
        save_pmx(
            _scene_chunk_mesh(current),
            pmx_path,
            clean_name if len(chunks) == 1 else f"{clean_name}_分块{index:03d}",
            list(current["materials"]),
            texture_files,
        )
        output_paths.append(pmx_path)
        if progress:
            progress("写入 PMX 分块", index, len(chunks))

    layout_path = scene_output / "场景布局.json"
    layout_path.write_text(
        json.dumps(
            {
                "scene": entry.display_name,
                "content_path": entry.content_path,
                "source_xml": str(entry.source_path),
                "source_coordinate": "NeoX row-vector",
                "pmx_coordinate_rule": "(-x, y, -z)",
                "vertex_limit_per_chunk": SCENE_PMX_VERTEX_LIMIT,
                "instances": layout_rows,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    missing_path = scene_output / "缺失资源.csv"
    with missing_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(["类型", "实例名", "UUID", "GIM", "材质覆盖", "原因"])
        writer.writerows(missing_rows)

    current_set = {path.resolve() for path in output_paths}
    for old_path in old_outputs:
        try:
            if old_path.resolve() not in current_set and old_path.is_file():
                old_path.unlink()
        except OSError:
            pass
    meta_path.write_text(
        json.dumps(
            {
                "fingerprint": fingerprint,
                "pipeline": SCENE_PMX_PIPELINE_VERSION,
                "outputs": [path.name for path in output_paths],
                "resolved_instances": resolved_count,
                "failed_instances": failed_instance_count,
                "resource_warnings": resource_warning_count,
                "source_scene": str(entry.source_path),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return (
        scene_output, len(output_paths), resolved_count, len(missing_rows), False
    )


def install_pmx_dependency() -> None:
    subprocess.check_call(
        [
            sys.executable, "-m", "pip", "install", "--upgrade",
            "pymeshio", "Pillow", "astc-encoder-py",
            "cryptography", "zstandard", "numpy", "moderngl",
            "lz4",
        ]
    )


def human_size(value: int) -> str:
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


class SceneSelectionDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, entries: list[SceneCatalogEntry]):
        super().__init__(parent)
        self.title("选择要导出的场景")
        self.geometry("960x620")
        self.minsize(720, 420)
        self.transient(parent)
        self.entries = entries
        self.visible_entries: list[SceneCatalogEntry] = []
        self.result: list[SceneCatalogEntry] = []
        self.search_var = tk.StringVar()

        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=10)
        ttk.Label(top, text="搜索内部场景路径").pack(side="left")
        search = ttk.Entry(top, textvariable=self.search_var)
        search.pack(side="left", fill="x", expand=True, padx=(8, 0))
        self.search_var.trace_add("write", lambda *_args: self._refresh())

        table = ttk.Frame(self)
        table.pack(fill="both", expand=True, padx=10)
        columns = ("name", "models", "groups", "effects")
        self.tree = ttk.Treeview(
            table,
            columns=columns,
            show="headings",
            selectmode="extended",
        )
        self.tree.heading("name", text="内部场景路径")
        self.tree.heading("models", text="模型实例")
        self.tree.heading("groups", text="组件组")
        self.tree.heading("effects", text="特效（仅报告）")
        self.tree.column("name", width=610, anchor="w")
        self.tree.column("models", width=90, anchor="center")
        self.tree.column("groups", width=80, anchor="center")
        self.tree.column("effects", width=100, anchor="center")
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _event: self._accept())

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", padx=10, pady=10)
        ttk.Label(
            bottom,
            text="可按 Ctrl/Shift 多选；大场景会自动拆成保持同一坐标原点的 PMX。",
        ).pack(side="left")
        ttk.Button(bottom, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(bottom, text="导出所选", command=self._accept).pack(
            side="right", padx=(0, 8)
        )

        self._refresh()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.grab_set()
        search.focus_set()

    def _refresh(self) -> None:
        query = self.search_var.get().strip().lower()
        self.visible_entries = [
            entry for entry in self.entries
            if not query
            or query in entry.display_name.lower()
            or query in entry.content_path.lower()
        ]
        self.tree.delete(*self.tree.get_children())
        for index, entry in enumerate(self.visible_entries):
            self.tree.insert(
                "", "end", iid=str(index),
                values=(
                    entry.display_name,
                    entry.model_count,
                    entry.component_group_count,
                    entry.effect_count,
                ),
            )

    def _accept(self) -> None:
        selected: list[SceneCatalogEntry] = []
        for item in self.tree.selection():
            try:
                selected.append(self.visible_entries[int(item)])
            except (ValueError, IndexError):
                continue
        if not selected:
            messagebox.showinfo(APP_TITLE, "请先选择至少一个场景。", parent=self)
            return
        self.result = selected
        self.destroy()


class RiggedMeshApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("980x625")
        self.minsize(760, 500)

        root = Path(__file__).resolve().parent
        default_input = root / "yys" if (root / "yys").is_dir() else root
        default_output = root / "rigged_models"

        self.source_mode_var = tk.StringVar(value="wpk")
        self.input_var = tk.StringVar(value=str(default_input))
        self.output_var = tk.StringVar(value=str(default_output))
        self.fast_reuse_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="选择阴阳师目录，然后选择一种一键解包方式。")
        self.progress_var = tk.DoubleVar(value=0.0)

        self.rows: list[MeshSummary] = []
        self.visible_rows: list[MeshSummary] = []
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.busy = False
        self.action_buttons: list[ttk.Button] = []

        self._build_ui()
        self.after(100, self._drain_events)

    def _build_ui(self):
        pad = {"padx": 8, "pady": 5}

        paths = ttk.LabelFrame(self, text="目录")
        paths.pack(fill="x", **pad)

        ttk.Label(paths, text="客户端版本").grid(row=0, column=0, sticky="w", **pad)
        mode_frame = ttk.Frame(paths)
        mode_frame.grid(row=0, column=1, columnspan=2, sticky="w", **pad)
        ttk.Radiobutton(
            mode_frame,
            text="新版 WPK（移动端）",
            variable=self.source_mode_var,
            value="wpk",
            command=self._source_mode_changed,
        ).pack(side="left")
        ttk.Radiobutton(
            mode_frame,
            text="旧版 NPK（桌面端）",
            variable=self.source_mode_var,
            value="npk",
            command=self._source_mode_changed,
        ).pack(side="left", padx=(18, 0))

        ttk.Label(paths, text="阴阳师目录").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(paths, textvariable=self.input_var).grid(
            row=1, column=1, sticky="ew", **pad
        )
        ttk.Button(paths, text="选择", command=self.choose_input).grid(
            row=1, column=2, **pad
        )

        ttk.Label(paths, text="输出目录").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(paths, textvariable=self.output_var).grid(
            row=2, column=1, sticky="ew", **pad
        )
        ttk.Button(paths, text="选择", command=self.choose_output).grid(
            row=2, column=2, **pad
        )
        paths.columnconfigure(1, weight=1)

        actions = ttk.LabelFrame(self, text="功能")
        actions.pack(fill="x", **pad)
        white_button = ttk.Button(
            actions, text="一键解包 PMX 白模", command=self.start_white_pmx
        )
        white_button.pack(side="left", fill="x", expand=True, padx=8, pady=10)
        textured_button = ttk.Button(
            actions, text="一键解包带贴图 PMX", command=self.start_one_click
        )
        textured_button.pack(side="left", fill="x", expand=True, padx=8, pady=10)
        scene_button = ttk.Button(
            actions, text="解包带贴图场景 PMX", command=self.start_scene_pmx
        )
        scene_button.pack(side="left", fill="x", expand=True, padx=8, pady=10)
        install_button = ttk.Button(
            actions, text="安装依赖", command=self.start_install
        )
        install_button.pack(side="left", padx=8, pady=10)
        self.action_buttons = [
            white_button, textured_button, scene_button, install_button
        ]

        status = ttk.Frame(self)
        status.pack(fill="x", **pad)
        ttk.Progressbar(status, variable=self.progress_var, maximum=100).pack(
            fill="x", expand=True, side="left", padx=(0, 8)
        )
        ttk.Label(status, textvariable=self.status_var, width=48).pack(side="right")

        log_frame = ttk.LabelFrame(self, text="日志")
        log_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log = tk.Text(log_frame, height=15, wrap="word")
        self.log.pack(fill="both", expand=True, padx=5, pady=5)
        self._log(
            "先选择新版 WPK 或旧版桌面 NPK。新版选择完整 yys 目录（也兼容 "
            "cloudfilesys3/res），旧版选择包含 model1.npk、model2.npk、qmodel.npk、"
            "tex_res.npk 的游戏目录。“PMX 白模”只解包并转换"
            "模型；“带贴图 PMX”会继续分析 THD、恢复材质贴图并组合确定附件；"
            "“场景 PMX”按 SCN 中的原始位置、旋转、缩放还原静态场景。"
        )

    def _default_output_for_mode(self, mode: str | None = None) -> Path:
        root = Path(__file__).resolve().parent
        return root / ("rigged_models_npk" if (mode or self.source_mode_var.get()) == "npk" else "rigged_models")

    def _model_folder_for_mode(self) -> Path:
        root = Path(__file__).resolve().parent
        if self.source_mode_var.get() == "npk":
            return root / "unpacked_npk" / "model"
        return resolve_source_and_model_folder(Path(self.input_var.get()))[1]

    def _source_mode_changed(self) -> None:
        root = Path(__file__).resolve().parent
        known_defaults = {root / "rigged_models", root / "rigged_models_npk"}
        current = Path(self.output_var.get()).resolve()
        if current in {path.resolve() for path in known_defaults}:
            self.output_var.set(str(self._default_output_for_mode()))
        mode_label = "旧版桌面 NPK" if self.source_mode_var.get() == "npk" else "新版移动端 WPK"
        self.status_var.set(f"已选择{mode_label}；请确认游戏目录。")

    def choose_input(self):
        old_mode = self.source_mode_var.get() == "npk"
        value = filedialog.askdirectory(
            title=(
                "选择包含 model1.npk / tex_res.npk 的旧版阴阳师目录"
                if old_mode else "选择完整 yys 目录或 cloudfilesys3/res"
            ),
            initialdir=self.input_var.get(),
        )
        if value:
            self.input_var.set(value)
            import onmyoji_npk as npk
            if npk.is_old_npk_root(Path(value)) and not old_mode:
                self.source_mode_var.set("npk")
                self._source_mode_changed()

    def choose_output(self):
        value = filedialog.askdirectory(
            title="选择输出目录", initialdir=self.output_var.get()
        )
        if value:
            self.output_var.set(value)

    def open_output(self):
        path = Path(self.output_var.get())
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)

    def start_pmx_preview(self):
        script = Path(__file__).resolve().with_name("pmx_preview_gui.py")
        if not script.is_file():
            messagebox.showerror(APP_TITLE, f"找不到预览器：\n{script}")
            return
        output_root = Path(self.output_var.get()) / "PMX输出"
        output_root.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(
                [sys.executable, "-X", "utf8", str(script), str(output_root)],
                cwd=str(script.parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"无法启动 PMX 预览器：\n{exc}")

    def clear_log(self):
        self.log.delete("1.0", "end")

    def _log(self, text: str):
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")

    def _set_busy(self, value: bool):
        self.busy = value
        state = "disabled" if value else "normal"
        for button in self.action_buttons:
            button.configure(state=state)

    def _run_worker(self, target):
        if self.busy:
            messagebox.showinfo(APP_TITLE, "当前任务尚未完成。")
            return
        self._set_busy(True)
        self.progress_var.set(0)
        threading.Thread(target=target, daemon=True).start()

    def start_scan(self):
        folder = self._model_folder_for_mode()
        if not folder.exists():
            messagebox.showerror(
                APP_TITLE,
                f"尚未找到解包结果：\n{folder}\n\n可直接点击“一键生成带贴图 PMX”自动处理。",
            )
            return

        def worker():
            try:
                self.events.put(("status", "正在扫描 .mesh..."))

                def progress(done: int, total: int):
                    percent = done * 100 / total if total else 100
                    self.events.put(("progress", percent))
                    self.events.put(("status", f"扫描 {done}/{total}"))

                rows = scan_rigged_meshes(folder, progress)
                loose_folder = folder.parent / "loose_model"
                if loose_folder.is_dir():
                    rows.extend(scan_rigged_meshes(loose_folder))
                extra_folder = folder.parent / "extra_rigged"
                if extra_folder.is_dir():
                    rows.extend(scan_rigged_meshes(extra_folder))
                if loose_folder.is_dir() or extra_folder.is_dir():
                    rows.sort(
                        key=lambda row: (
                            row.version,
                            row.bone_count,
                            row.path.name.lower(),
                        )
                    )
                self.events.put(("scan_done", rows))
            except Exception:
                self.events.put(("error", traceback.format_exc()))
            finally:
                self.events.put(("busy", False))

        self._run_worker(worker)

    def _apply_filter(self):
        try:
            min_bones = max(0, int(self.min_bones_var.get()))
        except (ValueError, tk.TclError):
            min_bones = 0
        search = self.search_var.get().strip().lower()
        version = self.version_var.get()

        visible: list[MeshSummary] = []
        for row in self.rows:
            if row.bone_count < min_bones:
                continue
            if version != "全部" and f"v{row.version}" != version:
                continue
            if search and search not in row.path.name.lower():
                continue
            visible.append(row)

        sort_mode = self.sort_var.get()
        if sort_mode == "新到旧":
            visible.sort(
                key=lambda row: (
                    row.source_order < 0,
                    (
                        row.source_order
                        if row.source_order >= 0
                        else -row.modified_ns
                    ),
                    row.path.name.lower(),
                )
            )
        elif sort_mode == "旧到新":
            visible.sort(
                key=lambda row: (
                    row.source_order < 0,
                    (
                        -row.source_order
                        if row.source_order >= 0
                        else row.modified_ns
                    ),
                    row.path.name.lower(),
                )
            )
        elif sort_mode == "骨骼数":
            visible.sort(
                key=lambda row: (row.bone_count, row.size, row.path.name.lower()),
                reverse=True,
            )
        elif sort_mode == "文件大小":
            visible.sort(
                key=lambda row: (row.size, row.bone_count, row.path.name.lower()),
                reverse=True,
            )
        else:
            visible.sort(key=lambda row: row.path.name.lower())

        self.visible_rows = visible
        self.tree.delete(*self.tree.get_children())
        base = self._model_folder_for_mode()
        for index, row in enumerate(visible):
            try:
                folder = str(row.path.parent.relative_to(base))
            except ValueError:
                folder = str(row.path.parent)
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    row.source_order if row.source_order >= 0 else "—",
                    f"v{row.version}",
                    row.bone_count,
                    human_size(row.size),
                    row.path.name,
                    folder,
                    row.status,
                ),
            )
        self.status_var.set(
            f"带骨总数 {len(self.rows)}；当前显示 {len(self.visible_rows)}"
        )

    def _selected_rows(self) -> list[MeshSummary]:
        rows: list[MeshSummary] = []
        for item in self.tree.selection():
            index = int(item)
            if 0 <= index < len(self.visible_rows):
                rows.append(self.visible_rows[index])
        return rows

    def start_scene_pmx(self):
        if self.source_mode_var.get() == "npk":
            messagebox.showinfo(
                APP_TITLE,
                "旧版 NPK 当前支持角色/附件模型解包；场景导出仍请选择新版 WPK。",
            )
            return
        selected_input = Path(self.input_var.get()).resolve()
        source_root, _model_folder = resolve_source_and_model_folder(selected_input)
        unpacked_root = Path(__file__).resolve().parent / "unpacked"
        cached_scene = unpacked_root / "scene"
        if source_root is None and not any(cached_scene.rglob("*.xml")):
            messagebox.showerror(
                APP_TITLE,
                "请选择完整 yys 目录，或先准备工具旁的 unpacked/scene 场景缓存。",
            )
            return

        def worker():
            payload = None
            try:
                try:
                    import pymeshio  # noqa: F401
                    import astc_encoder.pil_codec  # noqa: F401
                    from PIL import Image  # noqa: F401
                except ImportError:
                    self.events.put(("log", "首次运行：正在安装场景 PMX 与贴图依赖……"))
                    self.events.put(("status", "正在安装依赖"))
                    install_pmx_dependency()

                scene_root = ensure_scene_cache(
                    source_root,
                    unpacked_root,
                    log=lambda text: self.events.put(("log", text)),
                    progress=lambda stem, done, total: (
                        self.events.put((
                            "progress", done * 100 / total if total else 100
                        )),
                        self.events.put((
                            "status", f"准备场景资源 {stem} {done}/{total}"
                        )),
                    ),
                )
                self.events.put(("status", "正在建立场景清单"))
                entries = scan_scene_catalog(
                    scene_root,
                    progress=lambda done, total: (
                        self.events.put((
                            "progress", done * 100 / total if total else 100
                        )),
                        self.events.put((
                            "status", f"扫描场景 {done}/{total}"
                        )),
                    ),
                )
                if not entries:
                    raise RuntimeError("没有找到包含模型实例的 SCN 场景")

                thd_dir = source_root.parent / "thd" if source_root else None
                if thd_dir is None or not (thd_dir / "scene.thx").is_file():
                    candidates = list(selected_input.rglob("scene.thx"))
                    if not candidates:
                        candidates = list(
                            (Path(__file__).resolve().parent / "yys").rglob(
                                "scene.thx"
                            )
                        )
                    thd_dir = candidates[0].parent if candidates else None
                if thd_dir is None:
                    raise RuntimeError("找不到 cloudfilesys3/thd/scene.thx")
                self.events.put((
                    "log",
                    f"场景清单完成：{len(entries):,} 个可导出场景。",
                ))
                payload = (entries, source_root, thd_dir.resolve())
            except Exception:
                self.events.put(("error", traceback.format_exc()))
            finally:
                self.events.put(("busy", False))
            if payload is not None:
                self.events.put(("scene_catalog_ready", payload))

        self._run_worker(worker)

    def _choose_scene_entries(
        self,
        entries: list[SceneCatalogEntry],
        source_root: Path | None,
        thd_dir: Path,
    ) -> None:
        dialog = SceneSelectionDialog(self, entries)
        self.wait_window(dialog)
        if dialog.result:
            self._start_scene_export(dialog.result, source_root, thd_dir)

    def _start_scene_export(
        self,
        entries: list[SceneCatalogEntry],
        source_root: Path | None,
        thd_dir: Path,
    ) -> None:
        selected_output = Path(self.output_var.get()).resolve()
        fast_reuse = bool(self.fast_reuse_var.get())
        unpacked_root = Path(__file__).resolve().parent / "unpacked"

        def worker():
            try:
                output_root = selected_output / "场景PMX"
                output_root.mkdir(parents=True, exist_ok=True)
                texture_cache = DecodedTextureCache(
                    unpacked_root / "decoded_png_cache"
                )
                total_scenes = len(entries)
                generated = reused = failed_scenes = 0
                total_instances = missing_instances = total_chunks = 0
                self.events.put(("status", "正在建立场景资源索引"))
                self.events.put((
                    "log",
                    "正在读取 THX 与解包清单；只按需补取所选场景引用的资源。",
                ))
                with SceneResourceResolver(
                    thd_dir,
                    source_root,
                    unpacked_root,
                    log=lambda text: self.events.put(("log", text)),
                ) as resolver:
                    for scene_number, entry in enumerate(entries, 1):
                        self.events.put((
                            "log",
                            f"开始场景 {scene_number}/{total_scenes}："
                            f"{entry.display_name}（{entry.model_count:,} 个实例）",
                        ))

                        def report_stage(label: str, done: int, total: int) -> None:
                            inner = done / total if total else 1.0
                            overall = (
                                (scene_number - 1 + inner) * 100 / total_scenes
                            )
                            self.events.put(("progress", overall))
                            self.events.put((
                                "status",
                                f"场景 {scene_number}/{total_scenes} · "
                                f"{label} {done}/{total}",
                            ))

                        try:
                            folder, chunks, resolved, missing, was_reused = (
                                export_scene_pmx(
                                    entry,
                                    output_root,
                                    resolver,
                                    thd_dir,
                                    texture_cache,
                                    fast_reuse=fast_reuse,
                                    progress=report_stage,
                                )
                            )
                            total_chunks += chunks
                            total_instances += resolved
                            missing_instances += missing
                            if was_reused:
                                reused += 1
                            else:
                                generated += 1
                            self.events.put((
                                "log",
                                f"场景完成：{entry.display_name}；分块 {chunks}，"
                                f"实例 {resolved}，缺失 {missing}；输出 {folder}",
                            ))
                        except Exception as exc:
                            failed_scenes += 1
                            self.events.put((
                                "log",
                                f"[场景失败] {entry.display_name}: "
                                f"{type(exc).__name__}: {exc}",
                            ))
                        self.events.put((
                            "progress", scene_number * 100 / total_scenes
                        ))

                    targeted = resolver.extracted_count

                summary = (
                    f"场景 PMX 完成：新生成 {generated}，增量复用 {reused}，"
                    f"失败场景 {failed_scenes}；共 {total_chunks} 个 PMX 分块，"
                    f"成功摆放 {total_instances:,} 个实例，缺失 {missing_instances:,} 个。\n"
                    f"按需补取资源 {targeted:,} 个。\n输出：{output_root}"
                )
                self.events.put(("progress", 100))
                self.events.put(("status", "场景 PMX 导出完成"))
                self.events.put(("log", summary.replace("\n", "；")))
                self.events.put(("done_message", summary))
            except Exception:
                self.events.put(("error", traceback.format_exc()))
            finally:
                self.events.put(("busy", False))

        self._run_worker(worker)

    def start_white_pmx(self):
        """一键增量解包全部可用 Mesh，并输出不做材质匹配的 PMX 白模。"""
        selected_input = Path(self.input_var.get()).resolve()
        selected_output = Path(self.output_var.get()).resolve()
        old_mode = self.source_mode_var.get() == "npk"
        old_root = None
        if old_mode:
            import onmyoji_npk as npk

            old_root = npk.locate_old_npk_root(selected_input)
            source_root = None
            model_folder = Path(__file__).resolve().parent / "unpacked_npk" / "model"
        else:
            source_root, model_folder = resolve_source_and_model_folder(selected_input)
        has_cache = (
            any(
                (model_folder / name).exists()
                for name in ("npk_manifest_models.json", "npk_manifest.json")
            )
            if old_mode else (model_folder / "manifest.csv").exists()
        )
        if (old_mode and old_root is None and not has_cache) or (
            not old_mode and source_root is None and not has_cache
        ):
            messagebox.showerror(
                APP_TITLE,
                (
                    "请选择包含 model1.npk、model2.npk、qmodel.npk 和 tex_res.npk "
                    "的旧版阴阳师目录。"
                    if old_mode else
                    "请选择完整 yys 目录，或包含 model.idx、model*.wpk 的 res 目录。"
                ),
            )
            return

        def worker():
            try:
                try:
                    import pymeshio  # noqa: F401
                except ImportError:
                    self.events.put(("log", "首次运行：正在自动安装解包与 PMX 依赖……"))
                    self.events.put(("status", "正在安装依赖"))
                    install_pmx_dependency()

                if old_mode and old_root is not None:
                    import onmyoji_npk as npk

                    self.events.put(("log", "正在增量解包旧版 model1/model2/qmodel NPK……"))
                    npk.extract_resources(
                        old_root,
                        model_folder.parent,
                        include_textures=False,
                        log=lambda text: self.events.put(("log", text)),
                        progress=lambda stem, done, total: (
                            self.events.put(("progress", done * 100 / total if total else 100)),
                            self.events.put(("status", f"解包 {stem} {done}/{total}")),
                        ),
                    )

                archive_groups = None
                if source_root is not None:
                    import onmyoji_wpk_gui as wpk

                    archive_groups = wpk.discover_groups(
                        source_root,
                        progress=lambda stem, done, total: (
                            self.events.put(
                                ("progress", done * 100 / total if total else 100)
                            ),
                            self.events.put(
                                (
                                    "status",
                                    f"校验资源索引 {stem} {done}/{total}",
                                )
                            ),
                        ),
                        stems={"model", *SUPPLEMENTAL_RIGGED_GROUPS},
                    )
                    group = next(
                        (item for item in archive_groups if item.stem == "model"),
                        None,
                    )
                    if group is None:
                        raise RuntimeError("没有发现 model.idx 对应的 WPK 分组")
                    if not _manifest_matches_idx(model_folder, group.records):
                        self.events.put(("log", "正在增量解包 model.idx / model*.wpk……"))
                        engine = wpk.ExtractorEngine(
                            lambda text: self.events.put(("log", text)),
                            lambda done, total, text: (
                                self.events.put(
                                    ("progress", done * 100 / total if total else 100)
                                ),
                                self.events.put(
                                    ("status", f"解包 {done}/{total}：{text}")
                                ),
                            ),
                            threading.Event(),
                        )
                        engine.extract_groups(
                            [group], model_folder.parent, None, True
                        )
                    else:
                        self.events.put(("log", "model 解包缓存与当前索引一致，直接复用。"))

                loose_folder = None
                supplemental_paths: list[Path] = []
                hot_update_paths: list[Path] = []
                thd_dir = source_root.parent / "thd" if source_root is not None else None
                if source_root is not None:
                    loose_folder = sync_loose_model_resources(
                        source_root,
                        model_folder,
                        log=lambda text: self.events.put(("log", text)),
                    )
                    supplemental_paths = sync_supplemental_rigged_resources(
                        source_root,
                        model_folder,
                        log=lambda text: self.events.put(("log", text)),
                        archive_groups=archive_groups,
                        progress=lambda done, total, stem: (
                            self.events.put(
                                ("progress", done * 100 / total if total else 100)
                            ),
                            self.events.put(
                                ("status", f"识别额外包 {stem} {done}/{total}")
                            ),
                        ),
                    )
                else:
                    cached_loose = model_folder.parent / "loose_model"
                    loose_folder = cached_loose if cached_loose.is_dir() else None
                    cached_extra = model_folder.parent / "extra_rigged"
                    if cached_extra.is_dir():
                        supplemental_paths = list(cached_extra.rglob("*.mesh"))

                if thd_dir is not None and (thd_dir / "model.thx").is_file():
                    hot_update_paths = sync_hot_update_zip_resources(
                        thd_dir,
                        model_folder,
                        log=lambda text: self.events.put(("log", text)),
                    )
                else:
                    cached_hot = model_folder.parent / "hot_update_model"
                    if cached_hot.is_dir():
                        hot_update_paths = list(cached_hot.rglob("*.mesh"))

                self.events.put(("status", "正在扫描全部可用 Mesh"))

                def white_scan_progress(label: str):
                    return lambda done, total: (
                        self.events.put(
                            ("progress", done * 100 / total if total else 100)
                        ),
                        self.events.put(
                            ("status", f"扫描{label} Mesh {done}/{total}")
                        ),
                    )

                rows = scan_rigged_meshes(
                    model_folder,
                    white_scan_progress("model"),
                )
                if loose_folder is not None:
                    rows.extend(
                        scan_rigged_meshes(
                            loose_folder,
                            white_scan_progress("热更新散文件"),
                        )
                    )
                if hot_update_paths:
                    rows.extend(
                        scan_rigged_mesh_paths(
                            hot_update_paths,
                            white_scan_progress("热更新 ZIP"),
                        )
                    )
                if supplemental_paths:
                    rows.extend(
                        scan_rigged_mesh_paths(
                            supplemental_paths,
                            white_scan_progress("额外资源包"),
                        )
                    )
                unique_rows = {
                    str(row.path.resolve()).lower(): row for row in rows
                }
                rows = sorted(
                    unique_rows.values(),
                    key=lambda row: (
                        row.source_order < 0,
                        row.source_order if row.source_order >= 0 else 0,
                        row.path.name.lower(),
                    ),
                )
                if not rows:
                    raise RuntimeError("没有发现可转换的 Mesh")
                self.events.put(("scan_done", rows))

                white_root = selected_output / "PMX白模"
                white_root.mkdir(parents=True, exist_ok=True)
                report_rows: list[list[object]] = []
                converted = 0
                reused = 0
                failed = 0
                total = len(rows)
                for index, row in enumerate(rows, 1):
                    try:
                        if row.status == "需从 WPK 修复":
                            raise MeshFormatError("源 Mesh 数据不完整")
                        model_name = extracted_resource_label(row.path) or row.path.stem
                        model_name = re.sub(
                            r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", model_name
                        ).strip("_") or "model"
                        fingerprint = pmx_build_fingerprint(row.path, None)
                        folder_name = f"{model_name}_{fingerprint[:8]}"
                        model_output = (
                            white_root / mesh_size_bucket(row.size) / folder_name
                        )
                        pmx_path = model_output / f"{model_name}.pmx"
                        meta_path = model_output / ".build.json"
                        metadata: dict[str, object] = {}
                        if meta_path.is_file():
                            try:
                                metadata = json.loads(meta_path.read_text(encoding="utf-8"))
                            except (OSError, ValueError, json.JSONDecodeError):
                                metadata = {}
                        was_reused = (
                            metadata.get("fingerprint") == fingerprint
                            and pmx_path.is_file()
                        )
                        if not was_reused:
                            reusable = _find_reusable_pmx_output(
                                white_root, fingerprint, f"{model_name}.pmx"
                            )
                            if reusable is not None:
                                pmx_path, model_output, _ = reusable
                                was_reused = True
                        if not was_reused:
                            mesh = parse_mesh_for_pmx(row.path)
                            save_pmx(mesh, pmx_path, model_name)
                            meta_path = model_output / ".build.json"
                            meta_path.write_text(
                                json.dumps(
                                    {
                                        "fingerprint": fingerprint,
                                        "source_mesh": row.path.name,
                                        "source_path": str(row.path),
                                        "white_only": True,
                                        "pipeline": PMX_OUTPUT_FORMAT_VERSION,
                                    },
                                    ensure_ascii=False,
                                    indent=2,
                                ),
                                encoding="utf-8",
                            )
                            _register_pmx_build_output(
                                white_root, fingerprint, model_output
                            )
                            converted += 1
                        else:
                            reused += 1
                        report_rows.append(
                            [
                                row.path.name,
                                model_name,
                                str(pmx_path),
                                row.version,
                                row.bone_count,
                                row.size,
                                mesh_size_bucket(row.size),
                                "复用" if was_reused else "新生成",
                                str(row.path),
                            ]
                        )
                    except Exception as exc:
                        failed += 1
                        if failed <= 30:
                            self.events.put(
                                (
                                    "log",
                                    f"[白模失败] {row.path.name}: "
                                    f"{type(exc).__name__}: {exc}",
                                )
                            )
                    if index % 20 == 0 or index == total:
                        self.events.put(("progress", index * 100 / total))
                        self.events.put(
                            (
                                "status",
                                f"白模 {index}/{total}：新生成 {converted}，"
                                f"复用 {reused}，失败 {failed}",
                            )
                        )

                report_path = white_root / "白模清单.csv"
                with report_path.open("w", newline="", encoding="utf-8-sig") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(
                        [
                            "源Mesh", "模型名", "PMX", "版本", "骨骼数",
                            "源Mesh大小", "大小分桶", "结果", "物理Mesh路径",
                        ]
                    )
                    writer.writerows(report_rows)
                summary = (
                    f"PMX 白模完成：当前资源 {total}，新生成 {converted}，"
                    f"增量复用 {reused}，失败 {failed}。\n输出：{white_root}"
                )
                self.events.put(("log", summary.replace("\n", "；")))
                self.events.put(("status", "PMX 白模解包完成"))
                self.events.put(("done_message", summary))
            except Exception:
                self.events.put(("error", traceback.format_exc()))
            finally:
                self.events.put(("busy", False))

        self._run_worker(worker)

    def start_one_click(self):
        selected_input = Path(self.input_var.get()).resolve()
        selected_output = Path(self.output_var.get()).resolve()
        fast_reuse = bool(self.fast_reuse_var.get())
        old_mode = self.source_mode_var.get() == "npk"
        old_root = None
        if old_mode:
            import onmyoji_npk as npk

            old_root = npk.locate_old_npk_root(selected_input)
            source_root = None
            model_folder = Path(__file__).resolve().parent / "unpacked_npk" / "model"
        else:
            source_root, model_folder = resolve_source_and_model_folder(selected_input)
        has_cache = (
            (model_folder / "npk_manifest.json").exists()
            if old_mode else (model_folder / "manifest.csv").exists()
        )
        if (old_mode and old_root is None and not has_cache) or (
            not old_mode and source_root is None and not has_cache
        ):
            messagebox.showerror(
                APP_TITLE,
                (
                    "请选择包含 model1.npk、model2.npk、qmodel.npk 和 tex_res.npk "
                    "的旧版阴阳师目录。"
                    if old_mode else
                    "请选择完整 yys 目录，或包含 model.idx、model*.wpk 的 res 目录。"
                ),
            )
            return

        def worker():
            wpk_reader = None
            archive_groups = None
            try:
                try:
                    import pymeshio  # noqa: F401
                    import astc_encoder.pil_codec  # noqa: F401
                    from PIL import Image  # noqa: F401
                except ImportError:
                    self.events.put(("log", "首次运行：正在自动安装 PMX 与贴图解码依赖……"))
                    self.events.put(("status", "正在安装依赖"))
                    install_pmx_dependency()

                preflight_thd_dir = (
                    source_root.parent / "thd" if source_root is not None else None
                )
                if old_mode and old_root is not None:
                    old_thd_candidates = (
                        old_root / "Documents" / "cloudfilesys3" / "thd",
                        old_root / "Documents" / "cloudfilesys3" / "_check_preload_" / "thd",
                        old_root / "thd",
                    )
                    preflight_thd_dir = next(
                        (
                            candidate
                            for candidate in old_thd_candidates
                            if (candidate / "model.thx").is_file()
                            and (candidate / "model.thp").is_file()
                        ),
                        None,
                    )
                preflight_apk_path = (
                    None if old_mode else locate_nearby_onmyoji_apk(selected_input)
                )
                output_root = selected_output / "PMX输出"
                if old_mode:
                    import onmyoji_npk as npk

                    old_archive_fingerprint = (
                        npk.source_fingerprint(old_root, True)
                        if old_root is not None else
                        hashlib.sha256((model_folder / "npk_manifest.json").read_bytes()).hexdigest()
                    )
                    old_thd_fingerprint = ""
                    if preflight_thd_dir is not None:
                        old_thd_fingerprint = ":".join(
                            f"{path.name}:{path.stat().st_size}:{path.stat().st_mtime_ns}"
                            for path in (
                                preflight_thd_dir / "model.thx",
                                preflight_thd_dir / "model.thp",
                            )
                        )
                    source_fingerprint = hashlib.sha256(
                        (
                            f"old-npk-material-v3:{old_archive_fingerprint}:"
                            f"{old_thd_fingerprint}:"
                            f"{MATERIAL_RESOLVER_VERSION}:"
                            f"{COMPOSITE_RESOLVER_VERSION}:"
                            f"{PMX_OUTPUT_FORMAT_VERSION}"
                        ).encode("utf-8")
                    ).hexdigest()
                else:
                    source_fingerprint = one_click_source_fingerprint(
                        source_root,
                        model_folder,
                        preflight_thd_dir,
                        preflight_apk_path,
                    )
                if (
                    fast_reuse
                    and can_fast_reuse_one_click(output_root, source_fingerprint)
                ):
                    focus_counts = write_untextured_focus_reports(output_root)
                    summary = (
                        "游戏资源、材质规则和上次报告都没有变化，"
                        "已直接复用现有 PMX；没有重新扫描或重写。\n"
                        f"输出：{output_root}"
                    )
                    if focus_counts:
                        summary += (
                            f"\n角色主包大白模 {focus_counts['角色主包']} 个，"
                            f"其中单槽 {focus_counts['角色主包单槽']} 个。"
                        )
                    self.events.put(("progress", 100))
                    self.events.put(("log", summary.replace("\n", "；")))
                    self.events.put(("status", "资源未变化：已直接复用现有结果"))
                    self.events.put(("done_message", summary))
                    return

                if old_mode and old_root is not None:
                    import onmyoji_npk as npk

                    self.events.put((
                        "log",
                        "正在增量解包旧版 model1/model2/qmodel/tex_res NPK；"
                        "首次处理大包耗时较长，后续会按原包指纹直接复用。",
                    ))
                    npk.extract_resources(
                        old_root,
                        model_folder.parent,
                        include_textures=True,
                        log=lambda text: self.events.put(("log", text)),
                        progress=lambda stem, done, total: (
                            self.events.put(("progress", done * 100 / total if total else 100)),
                            self.events.put(("status", f"解包 {stem} {done}/{total}")),
                        ),
                    )

                if source_root is not None:
                    import onmyoji_wpk_gui as wpk

                    archive_groups = wpk.discover_groups(
                        source_root,
                        progress=lambda stem, done, total: (
                            self.events.put(
                                ("progress", done * 100 / total if total else 100)
                            ),
                            self.events.put(
                                (
                                    "status",
                                    f"校验资源索引 {stem} {done}/{total}",
                                )
                            ),
                        ),
                        stems={"model", *SUPPLEMENTAL_RIGGED_GROUPS},
                    )
                    group = next((
                        item for item in archive_groups if item.stem == "model"
                    ), None)
                    if group is None:
                        raise RuntimeError("没有发现 model.idx 对应的 WPK 分组")
                    current_span = _manifest_record_span(model_folder)
                    expected_span = len(group.records)
                    manifest_current = _manifest_matches_idx(
                        model_folder, group.records
                    )
                    if not manifest_current:
                        reason = (
                            "没有解包结果"
                            if current_span == 0
                            else "检测到 model.idx 内容变化"
                            f"（旧记录跨度 {current_span} / 新 {expected_span}）"
                        )
                        self.events.put(
                            (
                                "log",
                                reason
                                + "，开始按内容 MD5 增量同步 model 资源；"
                                "未变化文件直接复用。",
                            )
                        )
                        engine = wpk.ExtractorEngine(
                            lambda text: self.events.put(("log", text)),
                            lambda done, total, text: (
                                self.events.put(
                                    ("progress", done * 100 / total if total else 100)
                                ),
                                self.events.put(
                                    ("status", f"解包 {done}/{total}：{text}")
                                ),
                            ),
                            threading.Event(),
                        )
                        engine.extract_groups(
                            [group], model_folder.parent, None, True
                        )
                    else:
                        self.events.put(
                            ("log", f"解包结果与当前 model.idx 一致（{expected_span} 条），直接复用。")
                        )
                else:
                    # 旧版 NPK 使用自己的 JSON 清单，不存在 WPK 的 manifest.csv。
                    # 这里必须按当前来源检查对应清单，否则 NPK 已成功解包后仍会
                    # 被误判为“找不到 model.idx”。
                    expected_manifest = model_folder / (
                        "npk_manifest.json" if old_mode else "manifest.csv"
                    )
                    if not expected_manifest.exists():
                        if old_mode:
                            raise RuntimeError("缺少旧版 NPK 解包清单 npk_manifest.json")
                        raise RuntimeError("缺少解包结果，也找不到 model.idx")

                thd_dir = preflight_thd_dir
                apk_path = preflight_apk_path
                hot_update_mesh_paths: list[Path] = []
                if (
                    apk_path is not None
                    and thd_dir is not None
                    and (thd_dir / "model.thx").is_file()
                    and (thd_dir / "model.thp").is_file()
                ):
                    self.events.put(
                        ("log", f"发现 APK 基础资源：{apk_path.name}，正在补齐模型 XML/KTX……")
                    )
                    self.events.put(("status", "正在从 APK 补齐缺失模型资源"))
                    sync_apk_parent_resources(
                        apk_path,
                        model_folder,
                        thd_dir,
                        log=lambda text: self.events.put(("log", text)),
                        progress=lambda done, total: (
                            self.events.put(
                                (
                                    "progress",
                                    done * 100 / total if total else 100,
                                )
                            ),
                            self.events.put(
                                ("status", f"APK 补全 {done}/{total}")
                            ),
                        ),
                    )
                elif old_mode and thd_dir is not None:
                    self.events.put((
                        "log",
                        f"发现旧版资源依赖表：{thd_dir}；"
                        "将按 THX/THP 内容哈希精确关联 NPK 材质与 DDS。",
                    ))
                elif thd_dir is not None:
                    self.events.put(
                        (
                            "log",
                            "附近未发现阴阳师 APK：已有 APK 内容缓存仍会继续复用；"
                            "只有当前 THX 出现缓存和本体都没有的新资源时，才需要新版 APK。",
                        )
                    )

                loose_model_folder = None
                supplemental_mesh_paths: list[Path] = []
                if source_root is not None:
                    loose_model_folder = sync_loose_model_resources(
                        source_root,
                        model_folder,
                        log=lambda text: self.events.put(("log", text)),
                    )
                    self.events.put(
                        (
                            "log",
                            "正在增量识别 fx_model / levelsets 等额外包；"
                            "首次较慢，后续只处理新增或变化内容。",
                        )
                    )
                    supplemental_mesh_paths = sync_supplemental_rigged_resources(
                        source_root,
                        model_folder,
                        log=lambda text: self.events.put(("log", text)),
                        archive_groups=archive_groups,
                        progress=lambda done, total, stem: (
                            self.events.put(
                                (
                                    "progress",
                                    done * 100 / total if total else 100,
                                )
                            ),
                            self.events.put(
                                (
                                    "status",
                                    f"识别额外包 {stem} {done}/{total}",
                                )
                            ),
                        ),
                    )
                    self.events.put(("status", "正在补齐额外资源包材质"))
                    sync_supplemental_material_resources(
                        source_root,
                        model_folder,
                        thd_dir,
                        archive_groups=archive_groups,
                        log=lambda text: self.events.put(("log", text)),
                    )

                # loose_model 先同步，随后 ZIP 只补其它来源仍缺失的内容 MD5；
                # 同一热更新 Mesh 因而不会以两个物理路径重复进入 PMX 队列。
                if (
                    thd_dir is not None
                    and (thd_dir / "model.thx").is_file()
                ):
                    self.events.put(
                        ("status", "正在增量读取热更新 ZIP 资源")
                    )
                    hot_update_mesh_paths = sync_hot_update_zip_resources(
                        thd_dir,
                        model_folder,
                        log=lambda text: self.events.put(("log", text)),
                    )
                    self.events.put(
                        ("status", "正在保存并复用历史模型索引")
                    )
                    sync_historical_model_indexes(
                        thd_dir,
                        model_folder,
                        log=lambda text: self.events.put(("log", text)),
                    )
                    self.events.put(
                        ("status", "正在闭合大白模官方材质依赖")
                    )
                    sync_large_white_model_dependencies(
                        output_root,
                        thd_dir,
                        model_folder,
                        log=lambda text: self.events.put(("log", text)),
                    )
                    self.events.put(
                        ("status", "正在定向补齐大白模主贴图")
                    )
                    sync_large_white_remote_textures(
                        output_root,
                        thd_dir,
                        model_folder,
                        log=lambda text: self.events.put(("log", text)),
                    )

                self.events.put(("status", "正在扫描带骨模型"))

                def report_mesh_scan(label: str):
                    def callback(done: int, total: int) -> None:
                        self.events.put(
                            ("progress", done * 100 / total if total else 100)
                        )
                        self.events.put(
                            ("status", f"扫描{label} Mesh {done}/{total}")
                        )

                    return callback

                rows = scan_rigged_meshes(
                    model_folder,
                    report_mesh_scan("model"),
                )
                if loose_model_folder is not None:
                    rows.extend(
                        scan_rigged_meshes(
                            loose_model_folder,
                            report_mesh_scan("热更新散文件"),
                        )
                    )
                if hot_update_mesh_paths:
                    rows.extend(
                        scan_rigged_mesh_paths(
                            hot_update_mesh_paths,
                            report_mesh_scan("热更新 ZIP"),
                        )
                    )
                if supplemental_mesh_paths:
                    rows.extend(
                        scan_rigged_mesh_paths(
                            supplemental_mesh_paths,
                            report_mesh_scan("额外资源包"),
                        )
                    )
                if (
                    loose_model_folder is not None
                    or hot_update_mesh_paths
                    or supplemental_mesh_paths
                ):
                    rows.sort(
                        key=lambda row: (
                            row.version,
                            row.bone_count,
                            row.path.name.lower(),
                        )
                    )
                if not rows:
                    raise RuntimeError("没有发现带骨 mesh")
                self.events.put(("scan_done", rows))

                self.events.put(("log", "正在分析材质 XML 与贴图资源分组……"))
                material_stage = {"label": "", "number": 1, "total": 1}

                def report_material_stage(label: str, number: int, total: int) -> None:
                    material_stage.update(
                        {"label": label, "number": number, "total": total}
                    )
                    self.events.put(("progress", (number - 1) * 100 / total))
                    self.events.put(
                        ("status", f"材质匹配 [{number}/{total}]：{label}")
                    )

                def report_thd_dependency(done: int, total: int) -> None:
                    stage_number = int(material_stage["number"])
                    stage_total = int(material_stage["total"])
                    fraction = done / total if total else 1.0
                    self.events.put(
                        (
                            "progress",
                            ((stage_number - 1) + fraction) * 100 / stage_total,
                        )
                    )
                    self.events.put(
                        (
                            "status",
                            f"材质匹配 [{stage_number}/{stage_total}]："
                            f"THD 精确依赖 {done}/{total}",
                        )
                    )

                if old_mode and thd_dir is None:
                    packages, by_mesh, variants_by_mesh = (
                        build_old_npk_material_packages(
                            model_folder,
                            progress=report_thd_dependency,
                        )
                    )
                else:
                    packages, by_mesh, variants_by_mesh = build_material_packages(
                        model_folder,
                        progress=report_thd_dependency,
                        thd_dir=thd_dir,
                        stage_progress=report_material_stage,
                    )
                self.events.put(("progress", 100))
                verified_packages = sum(
                    1 for item in packages
                    if item.confidence in TRUSTED_MATERIAL_CONFIDENCE
                )
                complete_packages = sum(
                    1 for item in packages
                    if item.confidence
                    in {"THD精确", "THD路径自举", "人工验证"}
                )
                main_only_packages = sum(
                    1 for item in packages
                    if item.confidence.endswith("主贴图")
                )
                bootstrapped_packages = sum(
                    1 for item in packages
                    if item.confidence.startswith("THD路径自举")
                )
                self.events.put(
                    (
                        "log",
                        f"材质分析完成：可信资源组 {verified_packages} 个；"
                        f"全部纹理槽完整 {complete_packages} 个；"
                        f"主贴图完整但辅助槽缺失 {main_only_packages} 个；"
                        f"旧索引路径自举 {bootstrapped_packages} 个。"
                        "多材质严格按 GIM 的 MtlIdx 排列，"
                        "仍有歧义的资源不会自动绑定。",
                    )
                )
                variant_meshes = sum(
                    len(items) > 1 for items in variants_by_mesh.values()
                )
                variant_total = sum(
                    len(items) for items in variants_by_mesh.values()
                )
                self.events.put(
                    (
                        "log",
                        f"逻辑材质变体：{variant_meshes} 个物理 Mesh 存在多套材质；"
                        f"共保留 {variant_total} 套去重后的严格材质签名。",
                    )
                )
                # 组合模型仍使用单一默认材质。存在多皮肤变体的物理 Mesh 暂不
                # 自动参与组合，避免把任意一套皮肤悄悄拼进主体+附件。
                composite_materials = {
                    path: package
                    for path, package in by_mesh.items()
                    if len(variants_by_mesh.get(path.resolve(), [])) <= 1
                }
                self.events.put(("progress", 0))
                self.events.put(("status", "正在分析主体与附件组合关系"))

                def report_composite_analysis(
                    label: str, done: int, total: int
                ) -> None:
                    self.events.put(
                        ("progress", done * 100 / total if total else 100)
                    )
                    self.events.put(
                        ("status", f"组合分析：{label} {done}/{total}")
                    )

                composite_models = build_composite_models(
                    model_folder,
                    composite_materials,
                    thd_dir,
                    progress=report_composite_analysis,
                )
                self.events.put(
                    (
                        "log",
                        f"发现可安全组装的主体+附件模型 {len(composite_models)} 组；"
                        "确定关系直接并入普通成品，可选 Socket 变体单独保留审计。",
                    )
                )

                if source_root is not None:
                    self.events.put(("progress", 0))
                    self.events.put(("status", "正在校验 model.idx / WPK 原包"))

                    def report_wpk_validation(
                        stem: str, done: int, total: int
                    ) -> None:
                        self.events.put(
                            ("progress", done * 100 / total if total else 100)
                        )
                        self.events.put(
                            (
                                "status",
                                f"校验 {stem}.idx / WPK {done}/{total}",
                            )
                        )

                    wpk_reader = WpkModelReader(
                        source_root,
                        progress=report_wpk_validation,
                    )

                output_root = selected_output / "PMX输出"
                output_root.mkdir(parents=True, exist_ok=True)
                try:
                    import pmx_role_classifier as role_classifier

                    character_catalog, catalog_refreshed = (
                        role_classifier.prepare_character_catalog(
                            output_root, refresh=True
                        )
                    )
                    self.events.put(
                        (
                            "log",
                            f"角色元数据：可用 {len(character_catalog)} 条"
                            + (
                                "，已从官方列表更新本地缓存。"
                                if catalog_refreshed
                                else "，使用本地缓存或当前可用列表。"
                            ),
                        )
                    )
                except Exception as exc:
                    self.events.put(
                        (
                            "log",
                            "角色元数据暂不可用，未确认资源将保留内部分类："
                            f"{type(exc).__name__}: {exc}",
                        )
                    )
                texture_cache = DecodedTextureCache(
                    model_folder.parent / "decoded_png_cache"
                )
                self.events.put(
                    (
                        "log",
                        "贴图解码加速已启用：相同 KTX 只解码一次，"
                        "后续模型直接复用 PNG 缓存。",
                    )
                )
                existing_dirs: dict[str, list[Path]] = {}
                existing_direct_composite_dirs: dict[
                    frozenset[str], set[Path]
                ] = {}
                output_categories = ("带贴图", "纯色材质", "未匹配贴图")
                indexed_directory_count = 0
                self.events.put(("progress", 0))
                for category_number, category in enumerate(output_categories, 1):
                    category_root = output_root / category
                    if not category_root.is_dir():
                        self.events.put(
                            (
                                "progress",
                                category_number * 100 / len(output_categories),
                            )
                        )
                        continue
                    self.events.put(
                        (
                            "status",
                            f"索引已有输出 [{category_number}/{len(output_categories)}]："
                            f"{category}（已检查 {indexed_directory_count:,} 个目录）",
                        )
                    )
                    # os.walk 已将目录名和文件名分开，避免旧实现对数万个普通
                    # PMX/PNG 文件再次调用 is_dir()，网络盘和杀毒开启时尤为明显。
                    for current_root, directory_names, _ in os.walk(category_root):
                        directory_names.sort(key=str.lower)
                        for directory_name in directory_names:
                            indexed_directory_count += 1
                            if indexed_directory_count % 250 == 0:
                                self.events.put(
                                    (
                                        "status",
                                        f"索引已有输出 "
                                        f"[{category_number}/{len(output_categories)}]："
                                        f"{category}（已检查 "
                                        f"{indexed_directory_count:,} 个目录）",
                                    )
                                )
                            child = Path(current_root) / directory_name
                            match = re.search(
                                r"_([0-9a-fA-F]{8})$", directory_name
                            )
                            if not match:
                                continue
                            existing_dirs.setdefault(
                                match.group(1).lower(), []
                            ).append(child)
                            build_path = child / ".build.json"
                            if not build_path.is_file():
                                continue
                            try:
                                build_payload = json.loads(
                                    build_path.read_text(encoding="utf-8")
                                )
                                component_names = frozenset(
                                    str(value).lower()
                                    for value in build_payload.get("components", [])
                                    if isinstance(value, str) and value
                                )
                            except (OSError, ValueError, TypeError):
                                continue
                            if len(component_names) > 1:
                                existing_direct_composite_dirs.setdefault(
                                    component_names, set()
                                ).add(child)
                    self.events.put(
                        (
                            "progress",
                            category_number * 100 / len(output_categories),
                        )
                    )
                self.events.put(
                    (
                        "log",
                        f"已有输出索引完成：检查目录 "
                        f"{indexed_directory_count:,} 个。",
                    )
                )
                report_rows: list[list[object]] = []
                ok = failed = textured = reused = 0
                for number, row in enumerate(rows, 1):
                    row_had_failure = False
                    desired_outputs: set[Path] = set()
                    short_hash = row.path.stem.rsplit("_", 1)[-1][:8].lower()
                    try:
                        mesh_index = archive_index(row.path)
                        if row.status == "需从 WPK 修复":
                            if wpk_reader is None or mesh_index is None:
                                raise RuntimeError("mesh 被截断且缺少原始 WPK")
                            full_mesh = wpk_reader.read(mesh_index)
                            if not full_mesh.startswith(MESH_MAGIC):
                                raise MeshFormatError("从 WPK 重读后不是 mesh")
                            temporary = row.path.with_suffix(".mesh.repairing")
                            temporary.write_bytes(full_mesh)
                            temporary.replace(row.path)

                        resolved_mesh = row.path.resolve()
                        row_packages: list[MaterialPackage | None] = list(
                            variants_by_mesh.get(resolved_mesh, [])
                        )
                        if not row_packages:
                            row_packages = [by_mesh.get(resolved_mesh)]
                        row_packages.sort(
                            key=lambda item: (
                                safe_model_name(item, row.path).lower()
                                if item is not None else ""
                            )
                        )
                        name_counts = Counter(
                            safe_model_name(item, row.path)
                            for item in row_packages
                        )

                        for package in row_packages:
                            model_name_hint = safe_model_name(package, row.path)
                            folder_suffix = ""
                            if name_counts[model_name_hint] > 1 and package is not None:
                                signature_payload = [
                                    (
                                        material.name,
                                        sorted(material.textures.items()),
                                        material.diffuse_color,
                                    )
                                    for material in package.materials
                                ]
                                folder_suffix = "v" + hashlib.sha256(
                                    json.dumps(
                                        signature_payload,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                    ).encode("utf-8")
                                ).hexdigest()[:6]
                            try:
                                (
                                    pmx_path,
                                    model_output,
                                    model_name,
                                    has_diffuse,
                                    has_color,
                                    was_reused,
                                    texture_error,
                                ) = export_mesh_variant(
                                    row.path,
                                    package,
                                    output_root,
                                    texture_cache,
                                    folder_suffix=folder_suffix,
                                )
                                desired_outputs.add(model_output)
                                ok += 1
                                if was_reused:
                                    reused += 1
                                if has_diffuse:
                                    textured += 1
                                note_parts = []
                                if was_reused:
                                    note_parts.append("增量复用")
                                summary_text = package_texture_summary(package)
                                if summary_text:
                                    note_parts.append(summary_text)
                                if texture_error:
                                    note_parts.append(texture_error)
                                report_rows.append(
                                    [
                                        row.path.name,
                                        model_name,
                                        str(pmx_path),
                                        (
                                            "已绑定主贴图"
                                            if has_diffuse
                                            else ("已绑定纯色材质" if has_color else "仅模型")
                                        ),
                                        package.confidence if package else "无材质组",
                                        str(package.xml_path) if package else "",
                                        "；".join(note_parts),
                                    ]
                                )
                            except Exception as exc:
                                row_had_failure = True
                                failed += 1
                                report_rows.append(
                                    [
                                        row.path.name,
                                        model_name_hint,
                                        "",
                                        "失败",
                                        package.confidence if package else "",
                                        str(package.xml_path) if package else "",
                                        f"{type(exc).__name__}: {exc}",
                                    ]
                                )
                                if failed <= 30:
                                    self.events.put(
                                        (
                                            "log",
                                            f"[失败] {row.path.name}/{model_name_hint}: "
                                            f"{type(exc).__name__}: {exc}",
                                        )
                                    )

                        # 同一物理 Mesh 的多个材质变体共享短 hash。只有本轮全部
                        # 变体都成功时才清理旧目录，避免一个失败变体误删旧成果。
                        if not row_had_failure:
                            for stale in existing_dirs.get(short_hash, []):
                                if stale not in desired_outputs and stale.exists():
                                    shutil.rmtree(stale)
                            existing_dirs[short_hash] = sorted(desired_outputs)
                    except Exception as exc:
                        failed += 1
                        row_had_failure = True
                        report_rows.append(
                            [
                                row.path.name,
                                "",
                                "",
                                "失败",
                                "",
                                "",
                                f"{type(exc).__name__}: {exc}",
                            ]
                        )
                        if failed <= 30:
                            self.events.put(
                                (
                                    "log",
                                    f"[失败] {row.path.name}: "
                                    f"{type(exc).__name__}: {exc}",
                                )
                            )
                    self.events.put(("progress", number * 100 / len(rows)))
                    self.events.put(
                        (
                            "status",
                            f"生成源 Mesh {number}/{len(rows)}；PMX {ok}；"
                            f"带主贴图 {textured}；复用 {reused}；失败 {failed}",
                        )
                    )

                composite_ok = composite_reused = composite_failed = 0
                composite_report_rows: list[list[object]] = []
                successful_direct_components: set[Path] = set()
                for composite_number, composite in enumerate(
                    composite_models, 1
                ):
                    try:
                        composite_path, was_reused = save_composite_pmx(
                            composite, output_root, texture_cache
                        )
                        composite_ok += 1
                        if was_reused:
                            composite_reused += 1
                        if composite.direct_merge:
                            successful_direct_components.update(
                                path.resolve() for path in composite.mesh_paths
                            )
                        component_sizes = []
                        for component_path in composite.mesh_paths:
                            try:
                                component_sizes.append(component_path.stat().st_size)
                            except OSError:
                                component_sizes.append(0)
                        indexed_components = [
                            path
                            for path in composite.mesh_paths
                            if archive_index(path) is not None
                        ]
                        newest_component = (
                            min(indexed_components, key=archive_index)
                            if indexed_components
                            else composite.mesh_paths[0]
                        )
                        composite_report_rows.append(
                            [
                                newest_component.name,
                                composite.name,
                                str(composite_path),
                                sum(component_sizes),
                                composite.evidence or "既有 THP/GIM/Socket 严格组合规则",
                                len(composite.mesh_paths),
                                "|".join(path.name for path in composite.mesh_paths),
                                "是" if composite.direct_merge else "否",
                            ]
                        )
                    except Exception as exc:
                        composite_failed += 1
                        if composite_failed <= 20:
                            self.events.put(
                                (
                                    "log",
                                    f"[组合失败] {composite.name}: "
                                    f"{type(exc).__name__}: {exc}",
                                )
                            )
                    self.events.put(
                        (
                            "status",
                            f"生成合并成品 {composite_number}/"
                            f"{len(composite_models)}",
                        )
                    )
                self.events.put(
                    (
                        "log",
                        f"合并模型：成功 {composite_ok}，"
                        f"增量复用 {composite_reused}，失败 {composite_failed}。",
                    )
                )
                # 独立 PMX 已在本轮前半段先恢复。现在清理规则升级后不再成立的
                # 直接组合，防止旧的 LOD 叠加成品继续留在预览器和角色目录里。
                desired_direct_component_sets = {
                    frozenset(path.name.lower() for path in composite.mesh_paths)
                    for composite in composite_models
                    if composite.direct_merge
                }
                removed_stale_composites = 0
                for component_set, old_dirs in existing_direct_composite_dirs.items():
                    if component_set in desired_direct_component_sets:
                        continue
                    for stale in old_dirs:
                        if stale.exists():
                            shutil.rmtree(stale)
                            removed_stale_composites += 1
                if removed_stale_composites:
                    self.events.put(
                        (
                            "log",
                            "组合规则已更新：清理失效旧组合目录 "
                            f"{removed_stale_composites} 个；独立模型已恢复。",
                        )
                    )
                # 只有直接合并 PMX 已成功落盘后才移除其独立输出，避免组合失败
                # 时丢掉可用模型。原始 Mesh/材质缓存不删，随时可重新构建。
                replaced_names = {
                    path.name for path in successful_direct_components
                }
                removed_independent_dirs = 0
                for component_path in successful_direct_components:
                    component_hash = component_path.stem.rsplit("_", 1)[-1][
                        :8
                    ].lower()
                    for stale in existing_dirs.get(component_hash, []):
                        if stale.exists():
                            shutil.rmtree(stale)
                            removed_independent_dirs += 1
                if replaced_names:
                    report_rows = [
                        row for row in report_rows if row[0] not in replaced_names
                    ]
                self.events.put(
                    (
                        "log",
                        f"确定组合已直接替代独立件：组件 {len(replaced_names)} 个，"
                        f"清理旧独立输出目录 {removed_independent_dirs} 个。",
                    )
                )
                composite_report_path = output_root / "完整组合报告.csv"
                with composite_report_path.open(
                    "w", newline="", encoding="utf-8-sig"
                ) as stream:
                    writer = csv.writer(stream)
                    writer.writerow(
                        [
                            "源Mesh",
                            "模型名",
                            "PMX",
                            "源Mesh大小",
                            "组合依据",
                            "组件数",
                            "组件列表",
                            "直接合并",
                        ]
                    )
                    writer.writerows(composite_report_rows)

                report_path = output_root / "纹理恢复报告.csv"
                with report_path.open("w", newline="", encoding="utf-8-sig") as stream:
                    writer = csv.writer(stream)
                    writer.writerow(
                        [
                            "源Mesh",
                            "模型名",
                            "PMX",
                            "结果",
                            "关联置信度",
                            "材质XML",
                            "源Mesh大小",
                            "大小分桶",
                            "说明",
                        ]
                    )
                    size_by_name = {}
                    path_by_name = {}
                    submesh_count_by_name = {}
                    for item in rows:
                        path_by_name[item.path.name] = str(item.path)
                        try:
                            size_by_name[item.path.name] = item.path.stat().st_size
                        except OSError:
                            size_by_name[item.path.name] = 0
                        try:
                            submesh_count_by_name[item.path.name] = (
                                read_mesh_submesh_count(item.path)
                            )
                        except (OSError, MeshFormatError, ValueError):
                            submesh_count_by_name[item.path.name] = ""
                    enriched_rows = []
                    for report_row in report_rows:
                        source_size = size_by_name.get(report_row[0], 0)
                        enriched_rows.append(
                            report_row[:6]
                            + [source_size, mesh_size_bucket(source_size), report_row[6]]
                        )
                    writer.writerows(enriched_rows)

                finished_total, direct_total, hidden_total = (
                    write_finished_model_report(output_root)
                )
                self.events.put(
                    (
                        "log",
                        f"统一成品清单：{finished_total} 个；确定组合 {direct_total} 个"
                        f"已直接替代 {hidden_total} 个独立部件。",
                    )
                )

                # Keep the next investigation focused on large PMX files that have
                # no recovered diffuse texture.  This report is intentionally
                # separate from the full report so it can be sorted/reviewed alone.
                untextured_path = output_root / "未匹配贴图_按源Mesh大小.csv"
                untextured_rows = [
                    row
                    for row in enriched_rows
                    if row[3] == "仅模型" and row[6] >= 100 * 1024
                ]
                untextured_rows.sort(key=lambda row: row[6], reverse=True)
                with untextured_path.open(
                    "w", newline="", encoding="utf-8-sig"
                ) as stream:
                    writer = csv.writer(stream)
                    writer.writerow(
                        [
                            "源Mesh",
                            "模型名",
                            "PMX",
                            "源Mesh大小",
                            "大小分桶",
                            "关联置信度",
                            "材质XML",
                            "说明",
                            "子网格数",
                            "物理Mesh路径",
                        ]
                    )
                    writer.writerows(
                        [
                            [
                                row[0],
                                row[1],
                                row[2],
                                row[6],
                                row[7],
                                row[4],
                                row[5],
                                row[8],
                                submesh_count_by_name.get(row[0], ""),
                                path_by_name.get(row[0], ""),
                            ]
                            for row in untextured_rows
                        ]
                    )
                self.events.put(
                    (
                        "log",
                        f"大体积无贴图清单：{len(untextured_rows)} 个，"
                        f"已写入 {untextured_path.name}",
                    )
                )
                focus_counts = write_untextured_focus_reports(output_root)
                if focus_counts:
                    self.events.put(
                        (
                            "log",
                            "白模检查已拆分：角色主包 "
                            f"{focus_counts['角色主包']} 个（其中单槽 "
                            f"{focus_counts['角色主包单槽']} 个），额外特效/关卡包 "
                            f"{focus_counts['额外包']} 个。",
                        )
                    )
                try:
                    import pmx_role_classifier as role_classifier

                    self.events.put(
                        ("status", "正在按稀有度和中文角色名整理成品")
                    )
                    role_entries = role_classifier.scan_entries(
                        output_root,
                        progress=lambda done, total: self.events.put(
                            (
                                "status",
                                (
                                    f"读取角色分类元数据 {done}/{total}"
                                    if total
                                    else f"读取角色分类元数据：已发现 {done} 个"
                                ),
                            )
                        ),
                    )
                    role_moved, role_reports = role_classifier.apply_classification(
                        output_root,
                        role_entries,
                        progress=lambda done, total: (
                            self.events.put(
                                (
                                    "progress",
                                    done * 100 / total if total else 100,
                                )
                            ),
                            self.events.put(
                                (
                                    "status",
                                    f"整理角色目录 {done}/{total}",
                                )
                            ),
                        ),
                    )
                    self.events.put(
                        (
                            "log",
                            f"角色分类：共 {len(role_entries)} 个带贴图成品；"
                            f"移动 {role_moved} 个目录；同步 {role_reports} 份报告。",
                        )
                    )
                except Exception as exc:
                    self.events.put(
                        (
                            "log",
                            "按稀有度/中文名整理失败，PMX 本身不受影响："
                            f"{type(exc).__name__}: {exc}",
                        )
                    )
                write_one_click_state(output_root, source_fingerprint)
                summary = (
                    f"一键处理完成：PMX {ok}，其中已绑定主贴图 {textured}，"
                    f"增量复用 {reused}，失败 {failed}；"
                    f"成品 {finished_total}（直接合并 {direct_total}）。\n"
                    f"输出：{output_root}"
                )
                self.events.put(("log", summary.replace("\n", "；")))
                self.events.put(
                    (
                        "status",
                        f"完成：PMX {ok}，带主贴图 {textured}，复用 {reused}",
                    )
                )
                self.events.put(("done_message", summary))
            except Exception:
                self.events.put(("error", traceback.format_exc()))
            finally:
                if wpk_reader is not None:
                    wpk_reader.close()
                self.events.put(("busy", False))

        self._run_worker(worker)

    def start_repair_from_wpk(self):
        rows = [row for row in self.rows if row.status == "需从 WPK 修复"]
        if not rows:
            messagebox.showinfo(APP_TITLE, "没有检测到被截断的带骨 mesh。")
            return

        source_hint, input_folder = resolve_source_and_model_folder(
            Path(self.input_var.get())
        )
        input_folder = input_folder.resolve()
        candidates = [
            source_hint,
            input_folder.parent.parent,
            Path(__file__).resolve().parent,
            input_folder.parent,
        ]
        candidates = [path for path in candidates if path is not None]
        source_root = next(
            (path for path in candidates if (path / "model.idx").exists()),
            None,
        )
        if source_root is None:
            messagebox.showerror(
                APP_TITLE,
                "找不到 model.idx。请保持本工具、model.idx、model*.wpk 在同一根目录，"
                "或把输入目录设为该根目录下的 unpacked/model。",
            )
            return
        if not messagebox.askyesno(
            APP_TITLE,
            f"检测到 {len(rows)} 个截断的带骨 mesh。\n"
            "将从 model.idx + model*.wpk 重新读取完整数据并覆盖这些解包结果。\n"
            "是否继续？",
        ):
            return

        def worker():
            handles = {}
            try:
                import zstandard
                import onmyoji_wpk_gui as wpk

                groups = wpk.discover_groups(
                    source_root,
                    progress=lambda stem, done, total: (
                        self.events.put(
                            ("progress", done * 100 / total if total else 100)
                        ),
                        self.events.put(
                            ("status", f"校验 {stem}.idx {done}/{total}")
                        ),
                    ),
                    stems={"model"},
                )
                group = next((item for item in groups if item.stem == "model"), None)
                if group is None:
                    raise RuntimeError("没有发现 model.idx 对应的 WPK 分组")
                handles = {
                    package_id: path.open("rb")
                    for package_id, path in group.packages.items()
                }

                ok = 0
                errors = 0
                for number, row in enumerate(rows, 1):
                    try:
                        index_text = row.path.name.split("_", 1)[0]
                        record = group.records[int(index_text)]
                        handle = handles[record.package_id]
                        handle.seek(record.offset)
                        read_size = wpk.record_read_size(record)
                        blob = handle.read(read_size)
                        if len(blob) != read_size:
                            raise EOFError(f"应读取 {read_size}，实际 {len(blob)}")
                        decoded, _ = wpk.decode_stage1(blob, record.key_length)
                        decoded, _ = wpk.unwrap_payload(decoded, zstandard)
                        if not decoded.startswith(MESH_MAGIC):
                            raise MeshFormatError("重新读取后的数据不是 mesh")

                        temp_path = row.path.with_suffix(row.path.suffix + ".repairing")
                        temp_path.write_bytes(decoded)
                        temp_path.replace(row.path)
                        ok += 1
                    except Exception as exc:
                        errors += 1
                        self.events.put(
                            ("log", f"[修复失败] {row.path.name}: {type(exc).__name__}: {exc}")
                        )
                    if number % 5 == 0 or number == len(rows):
                        self.events.put(("progress", number * 100 / len(rows)))
                        self.events.put(
                            ("status", f"修复 {number}/{len(rows)}，成功 {ok}，失败 {errors}")
                        )

                self.events.put(
                    ("log", f"WPK 修复完成：成功 {ok}，失败 {errors}。正在重新扫描。")
                )
                repaired_rows = scan_rigged_meshes(input_folder)
                self.events.put(("scan_done", repaired_rows))
                self.events.put(("status", f"修复完成：成功 {ok}，失败 {errors}"))
            except Exception:
                self.events.put(("error", traceback.format_exc()))
            finally:
                for handle in handles.values():
                    handle.close()
                self.events.put(("busy", False))

        self._run_worker(worker)

    def start_copy_visible(self):
        rows = list(self.visible_rows)
        if not rows:
            messagebox.showinfo(APP_TITLE, "当前筛选结果为空。")
            return
        if not messagebox.askyesno(
            APP_TITLE, f"整理当前可见的 {len(rows)} 个带骨 mesh？"
        ):
            return

        def worker():
            out_dir = Path(self.output_var.get()) / "mesh"
            manifest_rows = []
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                for index, row in enumerate(rows, 1):
                    target = out_dir / row.path.name
                    shutil.copy2(row.path, target)
                    manifest_rows.append(
                        [row.path.name, row.version, row.bone_count, row.size, str(row.path)]
                    )
                    if index % 10 == 0 or index == len(rows):
                        self.events.put(("progress", index * 100 / len(rows)))
                        self.events.put(("status", f"整理 Mesh {index}/{len(rows)}"))

                with (Path(self.output_var.get()) / "rigged_manifest.csv").open(
                    "w", newline="", encoding="utf-8-sig"
                ) as stream:
                    writer = csv.writer(stream)
                    writer.writerow(
                        ["file", "mesh_version", "bone_count", "size", "source"]
                    )
                    writer.writerows(manifest_rows)
                self.events.put(
                    ("log", f"已整理 {len(rows)} 个带骨 mesh 到：{out_dir}")
                )
                self.events.put(("status", "整理完成"))
            except Exception:
                self.events.put(("error", traceback.format_exc()))
            finally:
                self.events.put(("busy", False))

        self._run_worker(worker)

    def start_convert_selected(self):
        rows = self._selected_rows()
        if not rows:
            messagebox.showinfo(APP_TITLE, "请先在列表中选择一个或多个模型。")
            return
        self._start_convert(rows)

    def start_convert_visible(self):
        rows = list(self.visible_rows)
        if not rows:
            messagebox.showinfo(APP_TITLE, "当前筛选结果为空。")
            return
        if not messagebox.askyesno(
            APP_TITLE,
            f"将转换当前可见的 {len(rows)} 个模型。\n"
            "建议先按版本/骨骼数筛选，或先选一个测试。是否继续？",
        ):
            return
        self._start_convert(rows)

    def _start_convert(self, rows: list[MeshSummary]):
        truncated = [row for row in rows if row.status == "需从 WPK 修复"]
        if truncated:
            messagebox.showwarning(
                APP_TITLE,
                f"所选范围中有 {len(truncated)} 个 mesh 数据被截断。\n"
                "请先点击“先修复截断 Mesh”，修复完成后再转换。",
            )
            return

        def worker():
            out_dir = Path(self.output_var.get()) / "pmx"
            ok = 0
            errors = 0
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                for index, row in enumerate(rows, 1):
                    try:
                        mesh = parse_mesh_for_pmx(row.path)
                        output = out_dir / f"{row.path.stem}.pmx"
                        save_pmx(mesh, output, row.path.stem)
                        ok += 1
                    except Exception as exc:
                        errors += 1
                        self.events.put(
                            ("log", f"[失败] {row.path.name}: {type(exc).__name__}: {exc}")
                        )
                    self.events.put(("progress", index * 100 / len(rows)))
                    self.events.put(
                        ("status", f"转换 {index}/{len(rows)}，成功 {ok}，失败 {errors}")
                    )

                self.events.put(
                    (
                        "log",
                        f"PMX 转换完成：成功 {ok}，失败 {errors}。输出：{out_dir}",
                    )
                )
                self.events.put(("status", f"完成：PMX {ok}，失败 {errors}"))
            except Exception:
                self.events.put(("error", traceback.format_exc()))
            finally:
                self.events.put(("busy", False))

        self._run_worker(worker)

    def start_install(self):
        def worker():
            try:
                self.events.put(("status", "正在安装 PMX 与贴图解码依赖..."))
                install_pmx_dependency()
                self.events.put(("log", "PMX 与贴图解码依赖安装完成。"))
                self.events.put(("status", "依赖安装完成"))
            except Exception:
                self.events.put(("error", traceback.format_exc()))
            finally:
                self.events.put(("busy", False))

        self._run_worker(worker)

    def _drain_events(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "progress":
                    self.progress_var.set(float(payload))
                elif kind == "log":
                    self._log(str(payload))
                elif kind == "scan_done":
                    self.rows = list(payload)
                    versions: dict[int, int] = {}
                    for row in self.rows:
                        versions[row.version] = versions.get(row.version, 0) + 1
                    self._log(
                        "扫描完成："
                        + "，".join(
                            f"v{version}={count}"
                            for version, count in sorted(versions.items())
                        )
                        + f"，带骨合计 {len(self.rows)}。"
                    )
                elif kind == "done_message":
                    messagebox.showinfo(APP_TITLE, str(payload))
                elif kind == "scene_catalog_ready":
                    entries, source_root, thd_dir = payload
                    self._choose_scene_entries(entries, source_root, thd_dir)
                elif kind == "error":
                    self._log(str(payload))
                    messagebox.showerror(APP_TITLE, "任务失败，详情见日志。")
                elif kind == "busy":
                    self._set_busy(bool(payload))
        except queue.Empty:
            pass
        self.after(100, self._drain_events)


def self_test(folder: Path) -> int:
    if not folder.exists():
        print(f"ERROR: folder not found: {folder}")
        return 2

    rows = scan_rigged_meshes(folder)
    truncated = [row for row in rows if row.status == "需从 WPK 修复"]
    complete = [row for row in rows if row.status != "需从 WPK 修复"]
    print(f"rigged={len(rows)} complete={len(complete)} truncated={len(truncated)}")
    versions: dict[int, int] = {}
    for row in rows:
        versions[row.version] = versions.get(row.version, 0) + 1
    print("versions=" + ",".join(f"v{k}:{v}" for k, v in sorted(versions.items())))

    parsed = 0
    failures: list[str] = []
    samples: list[MeshSummary] = []
    for version in sorted(versions):
        samples.extend([row for row in complete if row.version == version][:10])

    for row in samples:
        try:
            mesh = parse_mesh(row.path)
            parsed += 1
            print(
                f"OK v{mesh.version} bones={len(mesh.bone_names)} "
                f"vertices={len(mesh.positions)} faces={len(mesh.faces)} "
                f"{row.path.name}"
            )
        except Exception as exc:
            failures.append(f"{row.path}: {type(exc).__name__}: {exc}")
            print(f"FAIL {failures[-1]}")

    if parsed:
        first = next(
            (
                row for row in samples
                if row.version in (2, 3, 4)
                and all(text not in str(row.path) for text in failures)
            ),
            None,
        )
        if first is not None:
            try:
                import pymeshio.pmx.reader

                mesh = parse_mesh_for_pmx(first.path)
                with tempfile.TemporaryDirectory(prefix="onmyoji_pmx_test_") as temp:
                    target = Path(temp) / "test.pmx"
                    save_pmx(mesh, target, "self_test")
                    header = target.read_bytes()[:4]
                    if header != b"PMX ":
                        raise RuntimeError(f"PMX 头错误：{header!r}")
                    written = pymeshio.pmx.reader.read_from_file(str(target))
                    order, parents = _order_bones_parent_first(mesh.bone_parents)
                    old_to_new = {
                        old_index: new_index
                        for new_index, old_index in enumerate(order)
                    }
                    if len(written.bones) != len(order):
                        raise RuntimeError("PMX 骨骼数与源 Mesh 不一致")
                    for new_index, old_index in enumerate(order):
                        expected_parent = (
                            old_to_new[parents[old_index]]
                            if parents[old_index] >= 0 else -1
                        )
                        bone = written.bones[new_index]
                        if (
                            bone.name != mesh.bone_names[old_index]
                            or bone.parent_index != expected_parent
                        ):
                            raise RuntimeError(
                                f"PMX 骨骼父链读回不一致：{bone.name}"
                            )
                    print(f"PMX_OK size={target.stat().st_size}")
            except Exception as exc:
                failures.append(f"PMX writer: {type(exc).__name__}: {exc}")
                print(f"FAIL {failures[-1]}")

    print(f"parsed={parsed}/{len(samples)} failures={len(failures)}")
    return 1 if failures else 0


def main() -> int:
    if "--self-test" in sys.argv:
        script_root = Path(__file__).resolve().parent
        default = script_root / "unpacked" / "model"
        index = sys.argv.index("--self-test")
        folder = (
            Path(sys.argv[index + 1])
            if index + 1 < len(sys.argv)
            else default
        )
        return self_test(folder)

    app = RiggedMeshApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
