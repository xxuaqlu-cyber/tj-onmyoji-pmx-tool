# -*- coding: utf-8 -*-
"""批量浏览并内嵌预览 PMX；不依赖 MMD/MikuMikuDance。"""

from __future__ import annotations

import csv
import os
import re
import shutil
import sys
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pmx_role_classifier as role_classifier

try:
    import numpy as np
    from PIL import Image, ImageDraw, ImageTk
    import pymeshio.pmx.reader
except ImportError as exc:  # pragma: no cover - GUI startup message
    raise SystemExit(
        "缺少 PMX 预览依赖。请先运行主工具中的“安装依赖”。\n" + str(exc)
    )

try:
    import moderngl
except ImportError:  # pragma: no cover - software fallback remains available
    moderngl = None


APP_TITLE = "PMX 批量预览器"
MAX_GPU_PREVIEW_FACES = 200_000
MAX_SOFTWARE_PREVIEW_FACES = 4_000
MAX_TEXTURE_EDGE = 1024
GPU_TEXTURE_CACHE_BYTES = 384 * 1024 * 1024
REPORT_MODES = {
    "重点白模：角色主包": "白模优先检查_角色主包.csv",
    "重点白模：单槽": "白模优先检查_单槽.csv",
    "全部大白模": "未匹配贴图_按源Mesh大小.csv",
}
RARITY_ORDER = ("UR", "SP", "SSR", "SR", "R", "N", "呱太", "其他资源", "未分类")
RARITY_RANK = {rarity: index for index, rarity in enumerate(RARITY_ORDER)}


@dataclass(slots=True)
class PreviewItem:
    path: Path
    category: str
    rarity: str = "未分类"
    role: str = "未分类"
    display_name: str = ""
    source_size: int = 0
    source_mesh: str = ""
    source_order: int = -1


@dataclass(slots=True)
class PreviewData:
    path: Path
    positions: np.ndarray
    triangles: np.ndarray
    face_colors: np.ndarray
    texture_paths: list[Path]
    texture_pixels: dict[int, tuple[int, int, bytes]]
    material_batches: list[tuple[int, int, int, tuple[int, int, int]]]
    primary_texture_indices: list[int]
    normals: np.ndarray
    uvs: np.ndarray
    center: np.ndarray
    radius: float
    vertex_count: int
    face_count: int
    material_count: int


def _category_for(path: Path, root: Path) -> str:
    try:
        relative = path.relative_to(root)
        return relative.parts[0] if len(relative.parts) > 1 else "PMX"
    except ValueError:
        return path.parent.name


def _source_order(value: str) -> int:
    """Read the archive record order embedded by the extractor (smaller is newer)."""
    match = re.match(r"(\d{6})_", Path(value).name)
    return int(match.group(1)) if match else -1


def _path_key(path: Path) -> str:
    """生成 Windows 下不受大小写影响的绝对路径键。"""
    return os.path.normcase(os.path.abspath(path))


def _classification_from_path(path: Path) -> tuple[str, str]:
    """分类清单缺失时，从新旧两种“按角色”目录结构恢复分类。"""
    parts = path.parts
    for index, part in enumerate(parts[:-1]):
        if part == "按角色" and index + 1 < len(parts):
            if (
                parts[index + 1] in RARITY_RANK
                and index + 2 < len(parts)
            ):
                return parts[index + 1], parts[index + 2]
            return "未分类", parts[index + 1]
    return "", ""


def _role_from_classified_path(path: Path) -> str:
    """保留旧调用接口；新代码同时读取稀有度与角色名。"""
    return _classification_from_path(path)[1]


def _attach_role_metadata(root: Path, items: list[PreviewItem]) -> list[PreviewItem]:
    """用分类清单、源 Mesh 和目录结构给预览项补充稀有度与角色。"""
    by_path: dict[str, tuple[str, str]] = {}
    classifications_by_mesh: dict[str, set[tuple[str, str]]] = {}
    catalog = root / "角色分类清单.csv"
    if catalog.is_file():
        with catalog.open("r", newline="", encoding="utf-8-sig") as stream:
            for row in csv.DictReader(stream):
                rarity = row.get("稀有度", "").strip() or "未分类"
                role = row.get("角色分类", "").strip()
                if not role:
                    continue
                classification = (rarity, role)
                raw_path = row.get("PMX", "").strip()
                if raw_path:
                    catalog_path = Path(raw_path)
                    if not catalog_path.is_absolute():
                        catalog_path = root / catalog_path
                    by_path[_path_key(catalog_path)] = classification
                source_mesh = Path(row.get("源Mesh", "").strip()).name.lower()
                if source_mesh:
                    classifications_by_mesh.setdefault(source_mesh, set()).add(
                        classification
                    )

    for item in items:
        classification = by_path.get(_path_key(item.path))
        if classification is None and item.source_mesh:
            candidates = classifications_by_mesh.get(
                Path(item.source_mesh).name.lower(), set()
            )
            if len(candidates) == 1:
                classification = next(iter(candidates))
        if classification is None:
            classification = _classification_from_path(item.path)
        item.rarity = classification[0] or "未分类"
        item.role = classification[1] or "未分类"
    return items


def catalog_classifications(root: Path) -> list[tuple[str, str]]:
    catalog = root / "角色分类清单.csv"
    classifications: set[tuple[str, str]] = set()
    if catalog.is_file():
        with catalog.open("r", newline="", encoding="utf-8-sig") as stream:
            for row in csv.DictReader(stream):
                rarity = row.get("稀有度", "").strip() or "未分类"
                role = row.get("角色分类", "").strip()
                if role and role != "未分类":
                    classifications.add((rarity, role))
    return sorted(
        classifications,
        key=lambda value: (
            RARITY_RANK.get(value[0], len(RARITY_RANK)),
            value[1].lower(),
        ),
    )


def catalog_role_names(root: Path) -> list[str]:
    """兼容旧测试和调用方。"""
    return sorted({role for _rarity, role in catalog_classifications(root)}, key=str.lower)


def copy_model_folder(source: Path, destination_root: Path) -> Path:
    """把整个模型目录复制到目标目录；同名时保留双方并给新副本加序号。"""
    source = source.resolve()
    destination_root = destination_root.resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"模型文件夹不存在：{source}")
    if destination_root == source or destination_root.is_relative_to(source):
        raise ValueError("导出目录不能选在当前模型文件夹内部。")
    destination_root.mkdir(parents=True, exist_ok=True)
    target = destination_root / source.name
    suffix = 2
    while target.exists():
        target = destination_root / f"{source.name}_{suffix}"
        suffix += 1
    shutil.copytree(source, target)
    return target


def _read_report_items(
    root: Path,
    report_name: str,
    confidence: str | set[str] | None = None,
    result_value: str | set[str] | None = None,
    verify_files: bool = True,
) -> list[PreviewItem]:
    report = root / report_name
    if not report.is_file():
        return []
    result: list[PreviewItem] = []
    seen: set[Path] = set()
    accepted_confidences = (
        confidence if isinstance(confidence, set) else {confidence}
    ) if confidence is not None else None
    accepted_results = (
        result_value if isinstance(result_value, set) else {result_value}
    ) if result_value is not None else None
    with report.open("r", newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            if (
                accepted_confidences is not None
                and row.get("关联置信度", "") not in accepted_confidences
            ):
                continue
            if (
                accepted_results is not None
                and row.get("结果", "") not in accepted_results
            ):
                continue
            raw = row.get("PMX", "").strip()
            if not raw:
                continue
            path = Path(raw)
            if not path.is_absolute():
                path = root / path
            path = Path(os.path.abspath(path))
            if path in seen or (verify_files and not path.is_file()):
                continue
            seen.add(path)
            try:
                source_size = int(row.get("源Mesh大小", "0") or 0)
            except ValueError:
                source_size = 0
            result.append(
                PreviewItem(
                    path=path,
                    category=row.get("资源范围", "") or _category_for(path, root),
                    display_name=row.get("模型名", "") or path.stem,
                    source_size=source_size,
                    source_mesh=row.get("源Mesh", ""),
                    source_order=_source_order(row.get("源Mesh", "")),
                )
            )
    return result


def _discover_items(root: Path, mode: str) -> list[PreviewItem]:
    if mode == "成品模型":
        items = _read_report_items(
            root,
            "成品模型报告.csv",
            verify_files=False,
        )
        if items:
            return items
    if mode == "待确认材质变体":
        return _read_report_items(
            root,
            "纹理恢复报告.csv",
            confidence={
                "exact-logical-GIM-material-variant",
                "THD-logical-family-GIM-exact",
                "THD-logical-family-GIM-exact-main-texture",
            },
        )
    if mode in {"带贴图", "拆分件审计"}:
        items = _read_report_items(
            root,
            "纹理恢复报告.csv",
            result_value="已绑定主贴图",
            verify_files=False,
        )
        if items:
            return items
    if mode == "完整组合":
        items = _read_report_items(
            root,
            "完整组合报告.csv",
            verify_files=False,
        )
        if items:
            return items
    report_name = REPORT_MODES.get(mode)
    if report_name:
        items = _read_report_items(root, report_name)
        if items:
            return items
    category_folder = {
        "带贴图": "带贴图",
        "未匹配贴图": "未匹配贴图",
        "完整组合": "完整组合",
    }.get(mode)
    search_root = root / category_folder if category_folder else root
    if not search_root.is_dir():
        return []
    result = [
        PreviewItem(path=path.resolve(), category=_category_for(path, root))
        for path in search_root.rglob("*.pmx")
    ]
    return sorted(result, key=lambda item: str(item.path).lower())


def discover_items(root: Path, mode: str) -> list[PreviewItem]:
    return _attach_role_metadata(root, _discover_items(root, mode))


def _rgb(value, default: tuple[int, int, int]) -> tuple[int, int, int]:
    channels = []
    for name, fallback in zip(("r", "g", "b"), default):
        raw = getattr(value, name, fallback / 255.0)
        try:
            raw = float(raw)
        except (TypeError, ValueError):
            raw = fallback / 255.0
        channels.append(max(0, min(255, round(raw * 255))))
    return tuple(channels)


def _load_texture_pixels(
    path: Path,
) -> tuple[tuple[int, int, bytes] | None, tuple[int, int, int] | None]:
    try:
        with Image.open(path) as image:
            rgba = image.convert("RGBA")
            rgba.thumbnail((MAX_TEXTURE_EDGE, MAX_TEXTURE_EDGE), Image.Resampling.LANCZOS)
            average = rgba.convert("RGB").resize((1, 1), Image.Resampling.BOX)
            return (
                (rgba.width, rgba.height, rgba.tobytes()),
                tuple(average.getpixel((0, 0))),
            )
    except Exception:
        return None, None


def load_preview(path: Path) -> PreviewData:
    model = pymeshio.pmx.reader.read_from_file(str(path))
    positions = np.asarray(
        [
            (vertex.position.x, vertex.position.y, vertex.position.z)
            for vertex in model.vertices
        ],
        dtype=np.float32,
    )
    normals = np.asarray(
        [
            (vertex.normal.x, vertex.normal.y, vertex.normal.z)
            for vertex in model.vertices
        ],
        dtype=np.float32,
    )
    uvs = np.asarray(
        [(vertex.uv.x, vertex.uv.y) for vertex in model.vertices],
        dtype=np.float32,
    )
    raw_indices = np.asarray(model.indices, dtype=np.int64)
    usable = len(raw_indices) - len(raw_indices) % 3
    triangles = raw_indices[:usable].reshape((-1, 3))

    texture_paths: list[Path] = []
    for reference in model.textures:
        texture = path.parent / str(reference).replace("\\", os.sep).replace("/", os.sep)
        texture_paths.append(texture.resolve())

    material_colors: list[tuple[int, int, int]] = []
    material_texture_indices: list[int] = []
    face_materials: list[int] = []
    remaining_faces = len(triangles)
    for material_index, material in enumerate(model.materials):
        color = _rgb(material.diffuse_color, (150, 165, 190))
        texture_index = int(getattr(material, "texture_index", -1))
        material_colors.append(color)
        material_texture_indices.append(texture_index)
        count = min(remaining_faces, max(0, int(material.vertex_count) // 3))
        face_materials.extend([material_index] * count)
        remaining_faces -= count
    if not material_colors:
        material_colors.append((150, 165, 190))
        material_texture_indices.append(-1)
    if remaining_faces > 0:
        face_materials.extend([0] * remaining_faces)
    face_material_array = np.asarray(face_materials[: len(triangles)], dtype=np.int32)

    if len(triangles) > MAX_GPU_PREVIEW_FACES:
        selected = np.linspace(
            0, len(triangles) - 1, MAX_GPU_PREVIEW_FACES, dtype=np.int64
        )
        triangles = triangles[selected]
        face_material_array = face_material_array[selected]

    # Keep faces grouped by material so the GPU can draw each primary texture in
    # one call instead of changing texture for every triangle.
    if len(triangles):
        material_order = np.argsort(face_material_array, kind="stable")
        triangles = np.ascontiguousarray(triangles[material_order], dtype=np.int32)
        face_material_array = face_material_array[material_order]

    texture_pixels: dict[int, tuple[int, int, bytes]] = {}
    gpu_material_colors = material_colors.copy()
    primary_texture_indices = sorted(
        {
            index
            for index in material_texture_indices
            if 0 <= index < len(texture_paths)
        }
    )
    texture_averages: dict[int, tuple[int, int, int]] = {}
    for texture_index in primary_texture_indices:
        pixels, average = _load_texture_pixels(texture_paths[texture_index])
        if pixels is not None:
            texture_pixels[texture_index] = pixels
        if average is not None:
            texture_averages[texture_index] = average
    for material_index, texture_index in enumerate(material_texture_indices):
        average = texture_averages.get(texture_index)
        if average is not None:
            material_colors[material_index] = average

    palette = np.asarray(material_colors, dtype=np.uint8)
    face_colors = palette[np.clip(face_material_array, 0, len(palette) - 1)]
    material_batches: list[tuple[int, int, int, tuple[int, int, int]]] = []
    start = 0
    while start < len(face_material_array):
        material_index = int(face_material_array[start])
        end = start + 1
        while end < len(face_material_array) and face_material_array[end] == material_index:
            end += 1
        safe_index = max(0, min(material_index, len(material_colors) - 1))
        material_batches.append(
            (
                start * 3,
                (end - start) * 3,
                material_texture_indices[safe_index],
                gpu_material_colors[safe_index],
            )
        )
        start = end

    if len(positions):
        center = (positions.min(axis=0) + positions.max(axis=0)) * 0.5
        radius = max(float(np.linalg.norm(positions - center, axis=1).max()), 1e-6)
    else:
        center = np.zeros(3, dtype=np.float32)
        radius = 1.0
    return PreviewData(
        path=path,
        positions=positions,
        triangles=triangles,
        face_colors=face_colors,
        texture_paths=texture_paths,
        texture_pixels=texture_pixels,
        material_batches=material_batches,
        primary_texture_indices=primary_texture_indices,
        normals=normals,
        uvs=uvs,
        center=np.asarray(center, dtype=np.float32),
        radius=radius,
        vertex_count=len(model.vertices),
        face_count=usable // 3,
        material_count=len(model.materials),
    )


class GpuPreviewRenderer:
    """Small off-screen OpenGL renderer; Tk only receives the finished bitmap."""

    def __init__(self) -> None:
        if moderngl is None:
            raise RuntimeError("未安装 ModernGL")
        self.ctx = moderngl.create_standalone_context(require=330)
        self.program = self.ctx.program(
            vertex_shader="""
                #version 330
                in vec3 in_position;
                in vec2 in_uv;
                uniform vec3 u_center;
                uniform float u_radius;
                uniform float u_yaw;
                uniform float u_pitch;
                uniform float u_zoom;
                uniform vec2 u_fit;
                uniform vec2 u_pan;
                out vec2 v_uv;
                void main() {
                    vec3 p = in_position - u_center;
                    float cy = cos(u_yaw), sy = sin(u_yaw);
                    float cp = cos(u_pitch), sp = sin(u_pitch);
                    vec3 yrot = vec3(cy * p.x + sy * p.z, p.y, -sy * p.x + cy * p.z);
                    vec3 prot = vec3(yrot.x, cp * yrot.y - sp * yrot.z,
                                     sp * yrot.y + cp * yrot.z);
                    // Zoom affects the screen size only.  Keeping depth independent
                    // prevents rotated vertices from crossing the clip planes.
                    float screen_scale = 0.92 * u_zoom / u_radius;
                    float depth_scale = 0.92 / u_radius;
                    gl_Position = vec4(prot.xy * screen_scale * u_fit + u_pan,
                                       prot.z * depth_scale, 1.0);
                    v_uv = in_uv;
                }
            """,
            fragment_shader="""
                #version 330
                uniform sampler2D u_texture;
                uniform int u_has_texture;
                uniform vec3 u_base_color;
                in vec2 v_uv;
                out vec4 frag_color;
                void main() {
                    vec4 texel = u_has_texture != 0
                        ? texture(u_texture, v_uv)
                        : vec4(1.0);
                    if (texel.a < 0.035) discard;
                    // Preview the texture/material color as-is.  Surviving pixels
                    // are opaque so transparent surfaces cannot hide later parts
                    // through order-dependent depth writes.
                    frag_color = vec4(texel.rgb * u_base_color, 1.0);
                }
            """,
        )
        self.vbo = None
        self.ibo = None
        self.vao = None
        self.fbo = None
        self.color_target = None
        self.depth_target = None
        self.fbo_size = (0, 0)
        self.data: PreviewData | None = None
        self.active_textures: dict[int, object] = {}
        self.texture_cache: OrderedDict[tuple[str, int, int], tuple[object, int]] = OrderedDict()
        self.texture_cache_bytes = 0

    def _release_mesh(self) -> None:
        for resource_name in ("vao", "ibo", "vbo"):
            resource = getattr(self, resource_name)
            if resource is not None:
                resource.release()
                setattr(self, resource_name, None)

    @staticmethod
    def _texture_key(path: Path) -> tuple[str, int, int]:
        try:
            stat = path.stat()
            return str(path), stat.st_mtime_ns, stat.st_size
        except OSError:
            return str(path), 0, 0

    def _get_texture(
        self, path: Path, pixels: tuple[int, int, bytes]
    ):
        key = self._texture_key(path)
        cached = self.texture_cache.pop(key, None)
        if cached is not None:
            self.texture_cache[key] = cached
            return cached[0], key
        width, height, raw = pixels
        texture = self.ctx.texture((width, height), 4, raw)
        texture.build_mipmaps()
        texture.filter = (moderngl.LINEAR_MIPMAP_LINEAR, moderngl.LINEAR)
        texture.repeat_x = True
        texture.repeat_y = True
        try:
            texture.anisotropy = min(8.0, self.ctx.max_anisotropy)
        except Exception:
            pass
        size = width * height * 4
        self.texture_cache[key] = (texture, size)
        self.texture_cache_bytes += size
        return texture, key

    def _trim_texture_cache(self, protected: set[tuple[str, int, int]]) -> None:
        while self.texture_cache_bytes > GPU_TEXTURE_CACHE_BYTES:
            victim = next((key for key in self.texture_cache if key not in protected), None)
            if victim is None:
                break
            texture, size = self.texture_cache.pop(victim)
            texture.release()
            self.texture_cache_bytes -= size

    def prepare(self, data: PreviewData) -> None:
        self._release_mesh()
        packed = np.ascontiguousarray(
            np.column_stack((data.positions, data.uvs)), dtype=np.float32
        )
        indices = np.ascontiguousarray(data.triangles.reshape(-1), dtype=np.int32)
        self.vbo = self.ctx.buffer(packed.tobytes())
        self.ibo = self.ctx.buffer(indices.tobytes())
        self.vao = self.ctx.vertex_array(
            self.program,
            [(self.vbo, "3f 2f", "in_position", "in_uv")],
            self.ibo,
            index_element_size=4,
        )
        self.data = data
        self.active_textures = {}
        protected: set[tuple[str, int, int]] = set()
        for texture_index, pixels in data.texture_pixels.items():
            texture, key = self._get_texture(data.texture_paths[texture_index], pixels)
            self.active_textures[texture_index] = texture
            protected.add(key)
        self._trim_texture_cache(protected)

    def _ensure_fbo(self, width: int, height: int) -> None:
        if self.fbo_size == (width, height):
            return
        for resource_name in ("fbo", "color_target", "depth_target"):
            resource = getattr(self, resource_name)
            if resource is not None:
                resource.release()
                setattr(self, resource_name, None)
        self.color_target = self.ctx.texture((width, height), 4)
        self.depth_target = self.ctx.depth_renderbuffer((width, height))
        self.fbo = self.ctx.framebuffer(self.color_target, self.depth_target)
        self.fbo_size = (width, height)

    def render(
        self, width: int, height: int, yaw: float, pitch: float, zoom: float,
        pan_x: float, pan_y: float, wireframe: bool,
    ) -> Image.Image:
        data = self.data
        if data is None or self.vao is None:
            raise RuntimeError("GPU 模型尚未准备")
        self._ensure_fbo(width, height)
        self.fbo.use()
        self.fbo.clear(0.0, 0.0, 0.0, 1.0, depth=1.0)
        self.ctx.enable(moderngl.DEPTH_TEST)
        self.ctx.disable(moderngl.BLEND)
        self.ctx.wireframe = wireframe
        aspect = width / max(1, height)
        fit = (1.0 / max(1.0, aspect), min(1.0, aspect))
        self.program["u_center"].value = tuple(float(value) for value in data.center)
        self.program["u_radius"].value = data.radius
        self.program["u_yaw"].value = yaw
        self.program["u_pitch"].value = pitch
        self.program["u_zoom"].value = zoom
        self.program["u_fit"].value = fit
        self.program["u_pan"].value = (
            2.0 * pan_x / max(1, width),
            -2.0 * pan_y / max(1, height),
        )
        self.program["u_texture"].value = 0
        for first, count, texture_index, color in data.material_batches:
            texture = self.active_textures.get(texture_index)
            self.program["u_has_texture"].value = int(texture is not None)
            self.program["u_base_color"].value = tuple(channel / 255.0 for channel in color)
            if texture is not None:
                texture.use(location=0)
            self.vao.render(mode=moderngl.TRIANGLES, vertices=count, first=first)
        self.ctx.wireframe = False
        raw = self.fbo.read(components=3, alignment=1)
        return Image.frombytes("RGB", (width, height), raw).transpose(
            Image.Transpose.FLIP_TOP_BOTTOM
        )


class RoleMoveDialog(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Misc,
        current_classification: tuple[str, str],
        classifications: list[tuple[str, str]],
    ):
        super().__init__(parent)
        self.title("移动模型到其他类别")
        self.resizable(False, False)
        self.transient(parent)
        self.result: str | None = None
        current_label = " / ".join(current_classification)
        self.target_roles = {
            f"{rarity} / {role}": role for rarity, role in classifications
        }
        self.role_var = tk.StringVar(value=current_label)

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"当前类别：{current_label}").pack(anchor="w")
        ttk.Label(body, text="目标分类（稀有度 / 中文角色名，也可输入新类别）：").pack(
            anchor="w", pady=(12, 4)
        )
        combo = ttk.Combobox(
            body,
            textvariable=self.role_var,
            values=tuple(self.target_roles),
            width=38,
        )
        combo.pack(fill="x")
        combo.focus_set()
        combo.selection_range(0, "end")

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="移动", command=self.accept).pack(
            side="right", padx=(0, 8)
        )
        self.bind("<Return>", lambda _event: self.accept())
        self.bind("<Escape>", lambda _event: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.grab_set()

    def accept(self) -> None:
        value = self.role_var.get().strip()
        if not value:
            messagebox.showinfo(APP_TITLE, "请输入或选择目标类别。", parent=self)
            return
        self.result = role_classifier.normalize_role(self.target_roles.get(value, value))
        self.destroy()


def ask_target_role(
    parent: tk.Misc,
    current_classification: tuple[str, str],
    classifications: list[tuple[str, str]],
) -> str | None:
    dialog = RoleMoveDialog(parent, current_classification, classifications)
    parent.wait_window(dialog)
    return dialog.result


class PmxPreviewApp(tk.Tk):
    def __init__(self, initial: Path):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1380x820")
        self.minsize(980, 620)

        initial = initial.resolve()
        self.initial_file = initial if initial.is_file() else None
        root = initial.parent if initial.is_file() else initial
        if root.name != "PMX输出" and (root / "PMX输出").is_dir():
            root = root / "PMX输出"
        self.root_var = tk.StringVar(value=str(root))
        self.mode_var = tk.StringVar(value="成品模型")
        self.sort_var = tk.StringVar(value="新到旧")
        self.rarity_var = tk.StringVar(value="全部稀有度")
        self.role_var = tk.StringVar(value="全部角色")
        self.search_var = tk.StringVar()
        self.status_var = tk.StringVar(value="正在读取 PMX 清单……")
        self.wireframe_var = tk.BooleanVar(value=False)

        self.items: list[PreviewItem] = []
        self.visible_items: list[PreviewItem] = []
        self.classifications: list[tuple[str, str]] = []
        self.preview: PreviewData | None = None
        self.preview_image = None
        self.texture_image = None
        self.load_token = 0
        self.yaw = -0.55
        self.pitch = -0.20
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.drag_origin: tuple[
            str, int, int, float, float, float, float
        ] | None = None
        self.render_after_id = None
        self.gpu_renderer: GpuPreviewRenderer | None = None
        self.gpu_error = ""
        self.last_export_dir: Path | None = None

        self._build_ui()
        try:
            self.gpu_renderer = GpuPreviewRenderer()
        except Exception as exc:
            self.gpu_error = f"{type(exc).__name__}: {exc}"
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        self.rarity_var.trace_add("write", lambda *_: self._rarity_changed())
        self.role_var.trace_add("write", lambda *_: self._apply_filter())
        self.mode_var.trace_add("write", lambda *_: self.refresh_items())
        self.sort_var.trace_add("write", lambda *_: self._apply_filter())
        self.after(100, self.refresh_items)

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="PMX 输出目录").pack(side="left")
        ttk.Entry(top, textvariable=self.root_var).pack(
            side="left", fill="x", expand=True, padx=6
        )
        ttk.Button(top, text="选择", command=self.choose_root).pack(side="left")
        ttk.Button(top, text="刷新", command=self.refresh_items).pack(side="left", padx=(6, 0))

        filters = ttk.Frame(self, padding=(8, 0, 8, 8))
        filters.pack(fill="x")
        ttk.Label(filters, text="范围").pack(side="left")
        ttk.Combobox(
            filters,
            textvariable=self.mode_var,
            state="readonly",
            width=23,
            values=(
                "重点白模：角色主包",
                "重点白模：单槽",
                "全部大白模",
                "待确认材质变体",
                "成品模型",
                "未匹配贴图",
                "全部 PMX",
            ),
        ).pack(side="left", padx=6)
        ttk.Label(filters, text="稀有度").pack(side="left", padx=(8, 0))
        self.rarity_combo = ttk.Combobox(
            filters,
            textvariable=self.rarity_var,
            state="readonly",
            width=8,
            values=("全部稀有度",),
        )
        self.rarity_combo.pack(side="left", padx=6)
        ttk.Label(filters, text="角色").pack(side="left", padx=(4, 0))
        self.role_combo = ttk.Combobox(
            filters,
            textvariable=self.role_var,
            width=15,
            values=("全部角色",),
        )
        self.role_combo.pack(side="left", padx=6)
        ttk.Label(filters, text="搜索").pack(side="left", padx=(10, 0))
        ttk.Entry(filters, textvariable=self.search_var, width=22).pack(side="left", padx=6)
        ttk.Label(filters, text="排序").pack(side="left", padx=(10, 0))
        ttk.Combobox(
            filters,
            textvariable=self.sort_var,
            state="readonly",
            width=9,
            values=("新到旧", "旧到新", "角色", "名称", "源大小"),
        ).pack(side="left", padx=6)
        ttk.Checkbutton(
            filters,
            text="线框",
            variable=self.wireframe_var,
            command=self.schedule_render,
        ).pack(side="left", padx=8)
        ttk.Button(filters, text="上一个", command=lambda: self.move_selection(-1)).pack(side="right")
        ttk.Button(filters, text="下一个", command=lambda: self.move_selection(1)).pack(side="right", padx=6)

        panes = ttk.Panedwindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=8)

        left = ttk.Frame(panes)
        center = ttk.Frame(panes)
        right = ttk.Frame(panes, width=250)
        panes.add(left, weight=2)
        panes.add(center, weight=5)
        panes.add(right, weight=2)

        self.tree = ttk.Treeview(
            left,
            columns=("order", "rarity", "role", "category", "size"),
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0", text="模型")
        self.tree.heading("order", text="资源序")
        self.tree.heading("rarity", text="稀有度")
        self.tree.heading("role", text="角色")
        self.tree.heading("category", text="范围")
        self.tree.heading("size", text="源大小")
        self.tree.column("#0", width=230)
        self.tree.column("order", width=58, anchor="e")
        self.tree.column("rarity", width=58, anchor="center")
        self.tree.column("role", width=130)
        self.tree.column("category", width=90)
        self.tree.column("size", width=70, anchor="e")
        scroll = ttk.Scrollbar(left, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Double-1>", lambda _event: self.open_external())
        self.tree.bind("<Button-3>", self.show_item_menu)
        self.item_menu = tk.Menu(self, tearoff=False)
        self.item_menu.add_command(
            label="移动到其他角色分类…", command=self.choose_role_move
        )
        self.item_menu.add_separator()
        self.item_menu.add_command(
            label="导出本模型文件夹…", command=self.choose_export_folder
        )
        self.item_menu.add_command(
            label="导出到上次选择目录", command=self.export_to_last_folder,
            state="disabled",
        )
        self.item_menu.add_separator()
        self.item_menu.add_command(label="打开所在文件夹", command=self.open_folder)

        self.canvas = tk.Canvas(center, background="#000000", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind(
            "<ButtonPress-1>", lambda event: self.begin_drag(event, "rotate")
        )
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind(
            "<Shift-ButtonPress-1>", lambda event: self.begin_drag(event, "pan")
        )
        self.canvas.bind("<Shift-B1-Motion>", self.drag)
        self.canvas.bind(
            "<ButtonPress-2>", lambda event: self.begin_drag(event, "pan")
        )
        self.canvas.bind("<B2-Motion>", self.drag)
        self.canvas.bind(
            "<ButtonPress-3>", lambda event: self.begin_drag(event, "pan")
        )
        self.canvas.bind("<B3-Motion>", self.drag)
        self.canvas.bind("<Double-Button-1>", self.reset_view)
        self.canvas.bind("<MouseWheel>", self.mouse_wheel)
        self.canvas.bind("<Configure>", lambda _event: self.schedule_render())

        ttk.Label(right, text="贴图", font=("Microsoft YaHei UI", 11, "bold")).pack(
            anchor="w", pady=(4, 6)
        )
        self.texture_label = ttk.Label(right, anchor="center")
        self.texture_label.pack(fill="x")
        self.texture_list = tk.Listbox(right, height=12)
        self.texture_list.pack(fill="both", expand=True, pady=6)
        self.texture_list.bind("<<ListboxSelect>>", self.show_selected_texture)
        self.info_label = ttk.Label(right, text="", justify="left", wraplength=240)
        self.info_label.pack(fill="x", pady=6)
        ttk.Button(right, text="用系统程序打开 PMX", command=self.open_external).pack(fill="x", pady=3)
        ttk.Button(right, text="打开所在文件夹", command=self.open_folder).pack(fill="x", pady=3)

        ttk.Label(self, textvariable=self.status_var, anchor="w", padding=8).pack(fill="x")
        self.bind("<Up>", lambda _event: self.move_selection(-1))
        self.bind("<Down>", lambda _event: self.move_selection(1))

    def choose_root(self) -> None:
        value = filedialog.askdirectory(initialdir=self.root_var.get(), title="选择 PMX输出 目录")
        if value:
            self.root_var.set(value)
            self.refresh_items()

    def refresh_items(self) -> None:
        root = Path(self.root_var.get())
        mode = self.mode_var.get()
        self.status_var.set("正在读取 PMX 清单……")

        def worker() -> None:
            try:
                items = discover_items(root, mode)
                self.after(0, lambda: self._finish_refresh(items))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror(APP_TITLE, str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_refresh(self, items: list[PreviewItem]) -> None:
        self.items = items
        self.classifications = sorted(
            {
                (item.rarity, item.role)
                for item in items
                if item.role and item.role != "未分类"
            }
            | set(catalog_classifications(Path(self.root_var.get()))),
            key=lambda value: (
                RARITY_RANK.get(value[0], len(RARITY_RANK)),
                value[1].lower(),
            ),
        )
        present_rarities = {item.rarity for item in items} | {
            rarity for rarity, _role in self.classifications
        }
        rarities = [rarity for rarity in RARITY_ORDER if rarity in present_rarities]
        rarities.extend(
            sorted(present_rarities - set(RARITY_ORDER), key=str.lower)
        )
        self.rarity_combo.configure(values=("全部稀有度", *rarities))
        if self.rarity_var.get() not in {"全部稀有度", *rarities}:
            self.rarity_var.set("全部稀有度")
        self._refresh_role_choices()
        self._apply_filter()
        self.status_var.set(
            f"当前显示 {len(self.visible_items):,}/{len(items):,} 个 PMX；单击列表即可预览"
        )
        if self.initial_file:
            for index, item in enumerate(self.visible_items):
                if item.path == self.initial_file:
                    self.tree.selection_set(str(index))
                    self.tree.see(str(index))
                    self.on_select()
                    self.initial_file = None
                    break
        elif self.visible_items:
            self.tree.selection_set("0")
            self.on_select()

    def _rarity_changed(self) -> None:
        self._refresh_role_choices()
        self._apply_filter()

    def _refresh_role_choices(self) -> None:
        rarity = self.rarity_var.get().strip()
        roles = sorted(
            {
                role
                for item_rarity, role in self.classifications
                if rarity == "全部稀有度" or item_rarity == rarity
            },
            key=str.lower,
        )
        values = ("全部角色", "未分类", *roles)
        self.role_combo.configure(values=values)
        if self.role_var.get().strip() not in values:
            self.role_var.set("全部角色")

    def _apply_filter(self) -> None:
        term = self.search_var.get().strip().lower()
        rarity_term = self.rarity_var.get().strip()
        role_term = self.role_var.get().strip().lower()
        known_roles = {
            str(value).strip().lower()
            for value in self.role_combo.cget("values")
            if str(value).strip().lower() != "全部角色"
        }
        exact_role = role_term in known_roles
        self.visible_items = [
            item for item in self.items
            if (rarity_term == "全部稀有度" or item.rarity == rarity_term)
            and (
                not role_term
                or role_term == "全部角色"
                or (
                    item.role.lower() == role_term
                    if exact_role
                    else role_term in item.role.lower()
                )
            )
            and (
                not term
                or term in item.path.name.lower()
                or term in item.rarity.lower()
                or term in item.role.lower()
                or term in item.source_mesh.lower()
                or term in str(item.path.parent).lower()
            )
        ]
        sort_mode = self.sort_var.get()
        if sort_mode == "新到旧":
            self.visible_items.sort(
                key=lambda item: (
                    item.source_order < 0,
                    item.source_order if item.source_order >= 0 else 0,
                    item.path.name.lower(),
                )
            )
        elif sort_mode == "旧到新":
            self.visible_items.sort(
                key=lambda item: (
                    item.source_order < 0,
                    -item.source_order if item.source_order >= 0 else 0,
                    item.path.name.lower(),
                )
            )
        elif sort_mode == "源大小":
            self.visible_items.sort(
                key=lambda item: (item.source_size, item.path.name.lower()), reverse=True
            )
        elif sort_mode == "角色":
            self.visible_items.sort(
                key=lambda item: (
                    RARITY_RANK.get(item.rarity, len(RARITY_RANK)),
                    item.role.lower(),
                    item.path.name.lower(),
                )
            )
        else:
            self.visible_items.sort(key=lambda item: item.path.name.lower())
        self.tree.delete(*self.tree.get_children())
        for index, item in enumerate(self.visible_items):
            size = f"{item.source_size / 1024:.0f} KB" if item.source_size else ""
            self.tree.insert(
                "", "end", iid=str(index), text=item.display_name or item.path.stem,
                values=(
                    item.source_order if item.source_order >= 0 else "—",
                    item.rarity,
                    item.role,
                    item.category,
                    size,
                ),
            )
        if self.items:
            self.status_var.set(
                f"当前显示 {len(self.visible_items):,}/{len(self.items):,} 个 PMX；"
                "可按稀有度和角色筛选后连续预览"
            )

    def current_item(self) -> PreviewItem | None:
        selected = self.tree.selection()
        if not selected:
            return None
        index = int(selected[0])
        return self.visible_items[index] if 0 <= index < len(self.visible_items) else None

    def on_select(self, _event=None) -> None:
        item = self.current_item()
        if item is None:
            return
        self.load_token += 1
        token = self.load_token
        self.status_var.set(f"正在读取 {item.path.name}……")

        def worker() -> None:
            try:
                data = load_preview(item.path)
                self.after(0, lambda: self._finish_load(token, data))
            except Exception as exc:
                self.after(0, lambda: self._load_error(token, item.path, exc))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_load(self, token: int, data: PreviewData) -> None:
        if token != self.load_token:
            return
        self.preview = data
        if self.gpu_renderer is not None:
            try:
                self.gpu_renderer.prepare(data)
            except Exception as exc:
                self.gpu_error = f"{type(exc).__name__}: {exc}"
                self.gpu_renderer = None
        self.yaw = -0.55
        self.pitch = -0.20
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.texture_list.delete(0, "end")
        primary_indices = set(data.primary_texture_indices)
        for index, path in enumerate(data.texture_paths):
            marker = "" if path.is_file() else " [缺失]"
            role = " [主贴图]" if index in primary_indices else ""
            self.texture_list.insert("end", path.name + role + marker)
        if data.texture_paths:
            initial_texture = (
                data.primary_texture_indices[0] if data.primary_texture_indices else 0
            )
            self.texture_list.selection_set(initial_texture)
            self.texture_list.see(initial_texture)
            self.show_selected_texture()
        else:
            self.texture_label.configure(image="", text="无贴图")
            self.texture_image = None
        self.info_label.configure(
            text=(
                f"分类：{(self.current_item().rarity if self.current_item() else '未分类')} / "
                f"{(self.current_item().role if self.current_item() else '未分类')}\n"
                f"顶点：{data.vertex_count:,}\n"
                f"三角面：{data.face_count:,}\n"
                f"材质：{data.material_count}\n"
                f"主贴图：{len(data.texture_pixels)}/{len(data.primary_texture_indices)}\n"
                f"贴图表：{len(data.texture_paths)}\n\n{data.path}"
            )
        )
        backend = "显卡 UV 贴图（黑底/无光照）"
        if self.gpu_renderer is None:
            backend = f"软件简化预览（黑底/无光照；显卡不可用：{self.gpu_error}）"
        self.status_var.set(
            f"{data.path.name} — {backend}；左拖旋转，右/中拖或 Shift+左拖平移，滚轮缩放，双击重置"
        )
        self.schedule_render()

    def _load_error(self, token: int, path: Path, exc: Exception) -> None:
        if token == self.load_token:
            self.preview = None
            self.canvas.delete("all")
            self.canvas.create_text(
                max(10, self.canvas.winfo_width() // 2),
                max(10, self.canvas.winfo_height() // 2),
                text=f"无法预览\n{path.name}\n\n{type(exc).__name__}: {exc}",
                fill="#f08a8a", justify="center",
            )
            self.status_var.set(f"读取失败：{path.name}")

    def schedule_render(self) -> None:
        if self.render_after_id is not None:
            self.after_cancel(self.render_after_id)
        self.render_after_id = self.after(16, self.render_preview)

    def render_preview(self) -> None:
        self.render_after_id = None
        data = self.preview
        width = max(200, self.canvas.winfo_width())
        height = max(200, self.canvas.winfo_height())
        if data is None or len(data.positions) == 0:
            return
        if self.gpu_renderer is not None:
            try:
                image = self.gpu_renderer.render(
                    width, height, self.yaw, self.pitch, self.zoom,
                    self.pan_x, self.pan_y, self.wireframe_var.get(),
                )
                self.preview_image = ImageTk.PhotoImage(image)
                self.canvas.delete("all")
                self.canvas.create_image(0, 0, image=self.preview_image, anchor="nw")
                return
            except Exception as exc:
                self.gpu_error = f"{type(exc).__name__}: {exc}"
                self.gpu_renderer = None
                self.status_var.set(
                    f"显卡预览异常，已切换软件简化预览：{self.gpu_error}"
                )

        points = data.positions.astype(np.float64, copy=True)
        center = (points.min(axis=0) + points.max(axis=0)) * 0.5
        points -= center

        cy, sy = np.cos(self.yaw), np.sin(self.yaw)
        cp, sp = np.cos(self.pitch), np.sin(self.pitch)
        x = cy * points[:, 0] + sy * points[:, 2]
        z = -sy * points[:, 0] + cy * points[:, 2]
        y = cp * points[:, 1] - sp * z
        depth = sp * points[:, 1] + cp * z
        extent = max(float(np.ptp(x)), float(np.ptp(y)), 1e-6)
        scale = min(width, height) * 0.82 * self.zoom / extent
        screen = np.column_stack(
            (
                width * 0.5 + self.pan_x + x * scale,
                height * 0.52 + self.pan_y - y * scale,
            )
        )
        triangles = data.triangles
        face_colors = data.face_colors
        if len(triangles) > MAX_SOFTWARE_PREVIEW_FACES:
            selected = np.linspace(
                0, len(triangles) - 1, MAX_SOFTWARE_PREVIEW_FACES, dtype=np.int64
            )
            triangles = triangles[selected]
            face_colors = face_colors[selected]
        order = np.argsort(depth[triangles].mean(axis=1))

        image = Image.new("RGB", (width, height), (0, 0, 0))
        draw = ImageDraw.Draw(image)

        wireframe = self.wireframe_var.get()
        for face_index in order:
            face = triangles[face_index]
            polygon = [tuple(screen[index]) for index in face]
            base = face_colors[face_index]
            fill = tuple(int(max(0, min(255, channel))) for channel in base)
            outline = (110, 110, 110) if wireframe else None
            draw.polygon(polygon, fill=fill, outline=outline)
        self.preview_image = ImageTk.PhotoImage(image)
        self.canvas.delete("all")
        self.canvas.create_image(0, 0, image=self.preview_image, anchor="nw")

    def begin_drag(self, event, mode: str = "rotate") -> None:
        self.drag_origin = (
            mode, event.x, event.y, self.yaw, self.pitch,
            self.pan_x, self.pan_y,
        )

    def drag(self, event) -> None:
        if self.drag_origin is None:
            return
        mode, x, y, yaw, pitch, pan_x, pan_y = self.drag_origin
        if mode == "pan":
            self.pan_x = pan_x + event.x - x
            self.pan_y = pan_y + event.y - y
        else:
            self.yaw = yaw + (event.x - x) * 0.01
            self.pitch = max(-1.5, min(1.5, pitch + (event.y - y) * 0.01))
        self.schedule_render()

    def reset_view(self, _event=None) -> None:
        self.yaw = -0.55
        self.pitch = -0.20
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self.schedule_render()

    def mouse_wheel(self, event) -> None:
        self.zoom = max(0.2, min(6.0, self.zoom * (1.1 if event.delta > 0 else 0.9)))
        self.schedule_render()

    def show_selected_texture(self, _event=None) -> None:
        if self.preview is None:
            return
        selected = self.texture_list.curselection()
        if not selected:
            return
        path = self.preview.texture_paths[selected[0]]
        try:
            with Image.open(path) as source:
                image = source.convert("RGBA")
                image.thumbnail((240, 240), Image.Resampling.LANCZOS)
                checker = Image.new("RGBA", image.size, (62, 65, 74, 255))
                checker.alpha_composite(image)
            self.texture_image = ImageTk.PhotoImage(checker.convert("RGB"))
            self.texture_label.configure(image=self.texture_image, text="")
        except Exception as exc:
            self.texture_image = None
            self.texture_label.configure(image="", text=f"无法显示贴图\n{exc}")

    def move_selection(self, delta: int) -> None:
        if not self.visible_items:
            return
        selected = self.tree.selection()
        current = int(selected[0]) if selected else 0
        target = max(0, min(len(self.visible_items) - 1, current + delta))
        self.tree.selection_set(str(target))
        self.tree.focus(str(target))
        self.tree.see(str(target))
        self.on_select()

    def open_external(self) -> None:
        item = self.current_item()
        if item is not None:
            os.startfile(item.path)

    def open_folder(self) -> None:
        item = self.current_item()
        if item is not None:
            os.startfile(item.path.parent)

    def show_item_menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        self.tree.focus(row)
        item = self.current_item()
        can_move = bool(
            item is not None
            and "按角色" in item.path.parts
            and item.path.is_file()
        )
        self.item_menu.entryconfigure(
            "移动到其他角色分类…",
            state="normal" if can_move else "disabled",
        )
        try:
            self.item_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.item_menu.grab_release()

    def choose_role_move(self) -> None:
        item = self.current_item()
        if item is None:
            return
        role = ask_target_role(
            self,
            (item.rarity, item.role),
            self.classifications,
        )
        if role is None or role == item.role:
            return
        output_root = Path(self.root_var.get()).resolve()
        self.status_var.set(
            f"正在把 {item.path.name} 从 {item.rarity} / {item.role} 移动到 {role}……"
        )

        def worker() -> None:
            try:
                new_path, moved, reports = role_classifier.move_pmx_to_role(
                    output_root, item.path, role
                )
                self.after(
                    0,
                    lambda: self._finish_role_move(
                        item.role, role, new_path, moved, reports
                    ),
                )
            except Exception as exc:
                error_message = f"移动分类失败：\n{type(exc).__name__}: {exc}"
                self.after(
                    0,
                    lambda message=error_message: self._role_move_error(message),
                )

        threading.Thread(target=worker, daemon=True).start()

    def _finish_role_move(
        self,
        old_role: str,
        new_role: str,
        new_path: Path,
        moved: int,
        reports: int,
    ) -> None:
        self.initial_file = new_path
        self.rarity_var.set("全部稀有度")
        self.role_var.set(new_role)
        self.status_var.set(
            f"已从 {old_role} 移动到 {new_role}；同步更新 {reports} 份报告"
        )
        self.refresh_items()
        messagebox.showinfo(
            APP_TITLE,
            f"分类已保存。\n\n{old_role} → {new_role}\n"
            f"移动目录：{moved} 个\n更新报告：{reports} 份",
            parent=self,
        )

    def _role_move_error(self, message: str) -> None:
        self.status_var.set("移动角色分类失败")
        messagebox.showerror(APP_TITLE, message, parent=self)

    def choose_export_folder(self) -> None:
        item = self.current_item()
        if item is None:
            return
        initial = self.last_export_dir or Path(self.root_var.get())
        value = filedialog.askdirectory(
            parent=self,
            initialdir=str(initial),
            title=f"选择“{item.path.parent.name}”的导出目录",
            mustexist=True,
        )
        if not value:
            return
        self.last_export_dir = Path(value).resolve()
        self.item_menu.entryconfigure("导出到上次选择目录", state="normal")
        self._export_current_model_folder(self.last_export_dir)

    def export_to_last_folder(self) -> None:
        if self.last_export_dir is not None:
            self._export_current_model_folder(self.last_export_dir)

    def _export_current_model_folder(self, destination_root: Path) -> None:
        item = self.current_item()
        if item is None:
            return
        source = item.path.parent
        try:
            resolved_source = source.resolve()
            resolved_destination = destination_root.resolve()
            if (
                resolved_destination == resolved_source
                or resolved_destination.is_relative_to(resolved_source)
            ):
                raise ValueError("导出目录不能选在当前模型文件夹内部。")
        except Exception as exc:
            messagebox.showerror(APP_TITLE, str(exc), parent=self)
            return

        self.status_var.set(f"正在复制整个模型文件夹：{source.name}……")

        def worker() -> None:
            try:
                target = copy_model_folder(source, destination_root)
                self.after(0, lambda: self._finish_folder_export(target))
            except Exception as exc:
                error_message = f"导出模型文件夹失败：\n{type(exc).__name__}: {exc}"
                self.after(
                    0,
                    lambda message=error_message: self._folder_export_error(message),
                )

        threading.Thread(target=worker, daemon=True).start()

    def _finish_folder_export(self, target: Path) -> None:
        self.status_var.set(f"已导出模型文件夹：{target}")
        messagebox.showinfo(
            APP_TITLE,
            f"已复制整个模型文件夹：\n{target}",
            parent=self,
        )

    def _folder_export_error(self, message: str) -> None:
        self.status_var.set("导出模型文件夹失败")
        messagebox.showerror(APP_TITLE, message, parent=self)


def default_root() -> Path:
    root = Path(__file__).resolve().parent / "rigged_models" / "PMX输出"
    return root


if __name__ == "__main__":
    initial = Path(sys.argv[1]) if len(sys.argv) > 1 else default_root()
    PmxPreviewApp(initial).mainloop()
