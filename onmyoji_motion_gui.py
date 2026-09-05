# -*- coding: utf-8 -*-
"""Onmyoji RAWANIMA browser with PMX skin preview and VMD export."""

from __future__ import annotations

import math
import queue
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import ImageTk

import pmx_preview_gui as pmx_browser

from onmyoji_motion_bindings import OfficialMotionBindings

from onmyoji_motion import (
    AnimationMetadata,
    DecodedMotion,
    MotionClipAlignment,
    MotionFormatError,
    MotionHeader,
    compose_global_positions,
    compose_global_transforms,
    decode_motion,
    export_vmd,
    find_animation_metadata,
    inverse_affine_row_matrix4,
    matrix4_multiply,
    neox_to_pmx_matrix4,
    normalized_bone_name,
    quaternion_delta,
    quaternion_multiply,
    read_motion_header,
    skeleton_display_mask,
    trim_motion_to_animation_metadata,
    trs_row_matrix4,
)


APP_TITLE = "式神动作预览与 VMD 导出"
DISPLAY_LIMIT = 5000


class MotionPreviewApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1420x860")
        self.minsize(1000, 650)

        base = Path(__file__).resolve().parent
        default_root = base / "unpacked" / "model"
        default_pmx_root = base / "rigged_models" / "PMX输出"
        self.root_var = tk.StringVar(value=str(default_root if default_root.is_dir() else base))
        self.pmx_var = tk.StringVar()
        self.search_var = tk.StringVar()
        self.pmx_root_var = tk.StringVar(
            value=str(default_pmx_root if default_pmx_root.is_dir() else base / "rigged_models")
        )
        self.pmx_mode_var = tk.StringVar(value="成品模型")
        self.pmx_rarity_var = tk.StringVar(value="全部稀有度")
        self.pmx_role_var = tk.StringVar(value="全部角色")
        self.pmx_search_var = tk.StringVar()
        self.pmx_sort_var = tk.StringVar(value="新到旧")
        self.status_var = tk.StringVar(value="请选择一个动作，或扫描资源目录。")
        self.info_var = tk.StringVar(value="尚未载入动作")
        self.loop_var = tk.BooleanVar(value=True)
        self.speed_var = tk.DoubleVar(value=1.0)
        self.timeline_var = tk.DoubleVar(value=0.0)

        self.headers: list[MotionHeader] = []
        self.visible_headers: list[MotionHeader] = []
        self.pmx_items: list[pmx_browser.PreviewItem] = []
        self.visible_pmx_items: list[pmx_browser.PreviewItem] = []
        self.pmx_classifications: list[tuple[str, str]] = []
        self.motion: DecodedMotion | None = None
        self.parents: tuple[int, ...] = ()
        self.playing = False
        self.play_started = 0.0
        self.play_offset = 0.0
        self.yaw = -0.45
        self.pitch = -0.15
        self.zoom = 1.0
        self.drag: tuple[int, int, float, float] | None = None
        self.worker_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.scan_generation = 0
        self.parent_cache: dict[tuple[str, tuple[str, ...]], tuple[int, ...]] = {}
        self.bind_cache: dict[
            tuple[str, tuple[str, ...]], np.ndarray | None
        ] = {}
        self.animation_metadata_cache: dict[
            tuple[str, str, tuple[str, ...]], AnimationMetadata | None
        ] = {}
        self.motion_bind_transforms: np.ndarray | None = None
        self.animation_metadata: AnimationMetadata | None = None
        self.motion_clip_alignment: MotionClipAlignment | None = None
        self.skin: dict[str, object] | None = None
        self.gpu_renderer = None
        self.preview_image = None
        self.pmx_load_generation = 0
        self.motion_filter_active = False
        self.official_motion_bindings: OfficialMotionBindings | None = None
        self.bindings_loading = False
        self.pmx_source_meshes: dict[str, str] = {}
        self.pmx_catalog_loading = False

        self._build_ui()
        # ModernGL context creation is driver-dependent and can be expensive;
        # defer it until the user actually selects a PMX model.
        # Do not start the expensive THP/manifest parse while the window is
        # opening.  On large installs this can otherwise saturate the disk
        # alongside the PMX catalogue scan and make the desktop appear hung.
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        self.pmx_search_var.trace_add("write", lambda *_: self._apply_pmx_filter())
        self.pmx_rarity_var.trace_add("write", lambda *_: self._pmx_rarity_changed())
        self.pmx_role_var.trace_add("write", lambda *_: self._apply_pmx_filter())
        self.pmx_mode_var.trace_add("write", lambda *_: self._refresh_pmx_items())
        self.pmx_sort_var.trace_add("write", lambda *_: self._apply_pmx_filter())
        self.after(40, self._poll_workers)
        self.after(16, self._tick)
        # PMX output trees can contain tens of thousands of files.  Loading
        # that catalogue is now explicitly user-triggered by “刷新模型”, or
        # happens after a motion is selected.

    def _build_ui(self) -> None:
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="动作资源目录").pack(side="left")
        ttk.Entry(top, textvariable=self.root_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(top, text="选择目录", command=self._choose_root).pack(side="left")
        ttk.Button(top, text="扫描动作", command=self._scan).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="打开单个动作", command=self._open_motion).pack(side="left", padx=(6, 0))

        pmx_row = ttk.Frame(self, padding=(8, 0, 8, 8))
        pmx_row.pack(fill="x")
        ttk.Label(pmx_row, text="PMX 输出目录").pack(side="left")
        ttk.Entry(pmx_row, textvariable=self.pmx_root_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(pmx_row, text="选择目录", command=self._choose_pmx_root).pack(side="left")
        ttk.Button(pmx_row, text="刷新模型", command=self._refresh_pmx_items).pack(side="left", padx=(6, 0))
        ttk.Button(pmx_row, text="打开单个 PMX", command=self._choose_pmx).pack(side="left", padx=(6, 0))
        ttk.Button(pmx_row, text="导出 VMD", command=self._export).pack(side="left", padx=(6, 0))

        pane = ttk.Panedwindow(self, orient="horizontal")
        pane.pack(fill="both", expand=True, padx=8)
        left = ttk.Frame(pane, width=360)
        middle = ttk.Frame(pane, width=470)
        right = ttk.Frame(pane)
        pane.add(left, weight=1)
        pane.add(middle, weight=1)
        pane.add(right, weight=3)

        search_row = ttk.Frame(left)
        search_row.pack(fill="x", pady=(0, 6))
        ttk.Label(search_row, text="筛选").pack(side="left")
        ttk.Entry(search_row, textvariable=self.search_var).pack(side="left", fill="x", expand=True, padx=(6, 0))

        self.tree = ttk.Treeview(left, columns=("action", "skeleton", "time"), show="headings")
        self.tree.heading("action", text="动作")
        self.tree.heading("skeleton", text="骨架")
        self.tree.heading("time", text="时长 / FPS")
        self.tree.column("action", width=115)
        self.tree.column("skeleton", width=185)
        self.tree.column("time", width=95, anchor="e")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._tree_selected)
        self.tree.bind("<Double-1>", self._tree_selected)

        pmx_title = ttk.Frame(middle)
        pmx_title.pack(fill="x", pady=(0, 4))
        ttk.Label(pmx_title, text="PMX 模型", font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        self.pmx_count_label = ttk.Label(pmx_title, text="正在读取……")
        self.pmx_count_label.pack(side="right")

        pmx_filters1 = ttk.Frame(middle)
        pmx_filters1.pack(fill="x", pady=(0, 4))
        ttk.Label(pmx_filters1, text="范围").pack(side="left")
        ttk.Combobox(
            pmx_filters1,
            textvariable=self.pmx_mode_var,
            state="readonly",
            width=18,
            values=(
                "重点白模：角色主包",
                "重点白模：单槽",
                "全部大白模",
                "待确认材质变体",
                "成品模型",
                "未匹配贴图",
                pmx_browser.SCENE_MODE,
                "全部 PMX",
            ),
        ).pack(side="left", fill="x", expand=True, padx=(5, 0))
        ttk.Label(pmx_filters1, text="排序").pack(side="left", padx=(8, 0))
        ttk.Combobox(
            pmx_filters1,
            textvariable=self.pmx_sort_var,
            state="readonly",
            width=7,
            values=("新到旧", "旧到新", "角色", "名称", "源大小"),
        ).pack(side="left", padx=(5, 0))

        pmx_filters2 = ttk.Frame(middle)
        pmx_filters2.pack(fill="x", pady=(0, 4))
        ttk.Label(pmx_filters2, text="稀有度").pack(side="left")
        self.pmx_rarity_combo = ttk.Combobox(
            pmx_filters2,
            textvariable=self.pmx_rarity_var,
            state="readonly",
            width=9,
            values=("全部稀有度",),
        )
        self.pmx_rarity_combo.pack(side="left", padx=(5, 8))
        ttk.Label(pmx_filters2, text="角色").pack(side="left")
        self.pmx_role_combo = ttk.Combobox(
            pmx_filters2,
            textvariable=self.pmx_role_var,
            width=15,
            values=("全部角色",),
        )
        self.pmx_role_combo.pack(side="left", fill="x", expand=True, padx=(5, 0))

        pmx_search = ttk.Frame(middle)
        pmx_search.pack(fill="x", pady=(0, 6))
        ttk.Label(pmx_search, text="搜索").pack(side="left")
        ttk.Entry(pmx_search, textvariable=self.pmx_search_var).pack(
            side="left", fill="x", expand=True, padx=(5, 0)
        )
        ttk.Button(
            pmx_search, text="匹配当前动作", command=self._filter_pmx_for_motion
        ).pack(side="left", padx=(5, 0))

        self.pmx_tree = ttk.Treeview(
            middle,
            columns=("match", "rarity", "role", "size"),
            show="tree headings",
            selectmode="browse",
        )
        self.pmx_tree.heading("#0", text="模型")
        self.pmx_tree.heading("match", text="匹配")
        self.pmx_tree.heading("rarity", text="稀有度")
        self.pmx_tree.heading("role", text="角色")
        self.pmx_tree.heading("size", text="源大小")
        self.pmx_tree.column("#0", width=190)
        self.pmx_tree.column("match", width=45, anchor="center")
        self.pmx_tree.column("rarity", width=55, anchor="center")
        self.pmx_tree.column("role", width=105)
        self.pmx_tree.column("size", width=65, anchor="e")
        pmx_scroll = ttk.Scrollbar(middle, orient="vertical", command=self.pmx_tree.yview)
        self.pmx_tree.configure(yscrollcommand=pmx_scroll.set)
        self.pmx_tree.pack(side="left", fill="both", expand=True)
        pmx_scroll.pack(side="right", fill="y")
        self.pmx_tree.bind("<<TreeviewSelect>>", self._pmx_selected)

        ttk.Label(right, textvariable=self.info_var, anchor="w").pack(fill="x", padx=8, pady=(2, 5))
        self.canvas = tk.Canvas(right, background="#10141b", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<MouseWheel>", self._wheel)

        controls = ttk.Frame(right, padding=(8, 8, 8, 4))
        controls.pack(fill="x")
        self.play_button = ttk.Button(controls, text="播放", command=self._toggle_play)
        self.play_button.pack(side="left")
        ttk.Button(controls, text="回到开头", command=self._rewind).pack(side="left", padx=(6, 10))
        ttk.Label(controls, text="速度").pack(side="left")
        ttk.Combobox(
            controls,
            textvariable=self.speed_var,
            values=(0.25, 0.5, 1.0, 1.5, 2.0),
            state="readonly",
            width=5,
        ).pack(side="left", padx=(5, 10))
        ttk.Checkbutton(controls, text="循环", variable=self.loop_var).pack(side="left")
        self.time_label = ttk.Label(controls, text="0.00 / 0.00 秒")
        self.time_label.pack(side="right")

        self.timeline = ttk.Scale(
            right,
            from_=0,
            to=1,
            variable=self.timeline_var,
            command=self._timeline_changed,
        )
        self.timeline.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w", padding=(6, 3)).pack(fill="x")

    def _choose_root(self) -> None:
        value = filedialog.askdirectory(initialdir=self.root_var.get(), title="选择包含 rawanimation 的目录")
        if value:
            self.root_var.set(value)

    def _choose_pmx(self) -> None:
        value = filedialog.askopenfilename(filetypes=(("PMX 模型", "*.pmx"),), title="选择配套 PMX")
        if value:
            self.pmx_var.set(value)
            self._load_pmx(Path(value))

    def _choose_pmx_root(self) -> None:
        value = filedialog.askdirectory(
            initialdir=self.pmx_root_var.get(), title="选择 PMX 输出目录"
        )
        if value:
            root = Path(value)
            if root.name != "PMX输出" and (root / "PMX输出").is_dir():
                root = root / "PMX输出"
            self.pmx_root_var.set(str(root))
            self._refresh_pmx_items()

    def _refresh_pmx_items(self) -> None:
        if self.pmx_catalog_loading:
            return
        root = Path(self.pmx_root_var.get())
        mode = self.pmx_mode_var.get()
        self.pmx_count_label.configure(text="正在读取……")
        self.pmx_catalog_loading = True

        def worker() -> None:
            try:
                items = pmx_browser.discover_items(root, mode)
                classifications = pmx_browser.catalog_classifications(root)
                self.worker_queue.put(("pmx_list_done", (items, classifications)))
            except Exception as exc:
                self.pmx_catalog_loading = False
                self.worker_queue.put(("error", f"PMX 清单读取失败：{exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _load_official_motion_bindings(self) -> None:
        """Build the THP relation cache away from Tk's event loop."""
        if self.bindings_loading or self.official_motion_bindings is not None:
            return
        self.bindings_loading = True
        workspace = Path(__file__).resolve().parent

        def worker() -> None:
            try:
                bindings = OfficialMotionBindings.load_or_build(workspace)
                self.worker_queue.put(("bindings_done", bindings))
            except Exception as exc:
                self.worker_queue.put(("bindings_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_pmx_items(
        self,
        items: list[pmx_browser.PreviewItem],
        catalog: list[tuple[str, str]],
    ) -> None:
        self.pmx_items = items
        self.pmx_source_meshes = {
            str(item.path.resolve()).lower(): item.source_mesh
            for item in items
            if item.source_mesh
        }
        self.pmx_classifications = sorted(
            {
                (item.rarity, item.role)
                for item in items
                if item.role and item.role != "未分类"
            }
            | set(catalog),
            key=lambda value: (
                pmx_browser.RARITY_RANK.get(
                    value[0], len(pmx_browser.RARITY_RANK)
                ),
                value[1].lower(),
            ),
        )
        present = {item.rarity for item in items} | {
            rarity for rarity, _role in self.pmx_classifications
        }
        rarities = [
            rarity for rarity in pmx_browser.RARITY_ORDER if rarity in present
        ]
        rarities.extend(sorted(present - set(pmx_browser.RARITY_ORDER), key=str.lower))
        self.pmx_rarity_combo.configure(values=("全部稀有度", *rarities))
        if self.pmx_rarity_var.get() not in {"全部稀有度", *rarities}:
            self.pmx_rarity_var.set("全部稀有度")
        self._refresh_pmx_roles()
        self._apply_pmx_filter()
        if self.motion_filter_active:
            self._select_best_motion_candidate()

    def _pmx_rarity_changed(self) -> None:
        self._refresh_pmx_roles()
        self._apply_pmx_filter()

    def _refresh_pmx_roles(self) -> None:
        rarity = self.pmx_rarity_var.get().strip()
        roles = sorted(
            {
                role
                for item_rarity, role in self.pmx_classifications
                if rarity == "全部稀有度" or item_rarity == rarity
            },
            key=str.lower,
        )
        values = ("全部角色", "未分类", *roles)
        self.pmx_role_combo.configure(values=values)
        if self.pmx_role_var.get().strip() not in values:
            self.pmx_role_var.set("全部角色")

    def _apply_pmx_filter(self) -> None:
        if not hasattr(self, "pmx_tree"):
            return
        term = self.pmx_search_var.get().strip().lower()
        rarity = self.pmx_rarity_var.get().strip()
        role = self.pmx_role_var.get().strip().lower()
        known_roles = {
            str(value).strip().lower()
            for value in self.pmx_role_combo.cget("values")
            if str(value).strip().lower() != "全部角色"
        }
        exact_role = role in known_roles
        motion_candidates = (
            any(self._pmx_match_score(item) > 0 for item in self.pmx_items)
            if self.motion_filter_active
            else False
        )
        self.visible_pmx_items = [
            item
            for item in self.pmx_items
            if not self.motion_filter_active
            or not motion_candidates
            or self._pmx_match_score(item) > 0
            if (rarity == "全部稀有度" or item.rarity == rarity)
            and (
                not role
                or role == "全部角色"
                or (item.role.lower() == role if exact_role else role in item.role.lower())
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
        sort_mode = self.pmx_sort_var.get()
        if sort_mode == "新到旧":
            self.visible_pmx_items.sort(
                key=lambda item: (
                    item.source_order < 0,
                    item.source_order if item.source_order >= 0 else 0,
                    item.path.name.lower(),
                )
            )
        elif sort_mode == "旧到新":
            self.visible_pmx_items.sort(
                key=lambda item: (
                    item.source_order < 0,
                    -item.source_order if item.source_order >= 0 else 0,
                    item.path.name.lower(),
                )
            )
        elif sort_mode == "源大小":
            self.visible_pmx_items.sort(
                key=lambda item: (item.source_size, item.path.name.lower()),
                reverse=True,
            )
        elif sort_mode == "角色":
            self.visible_pmx_items.sort(
                key=lambda item: (
                    pmx_browser.RARITY_RANK.get(
                        item.rarity, len(pmx_browser.RARITY_RANK)
                    ),
                    item.role.lower(),
                    item.path.name.lower(),
                )
            )
        else:
            self.visible_pmx_items.sort(key=lambda item: item.path.name.lower())

        children = self.pmx_tree.get_children()
        if children:
            self.pmx_tree.delete(*children)
        for index, item in enumerate(self.visible_pmx_items):
            size = f"{item.source_size / 1024:.0f} KB" if item.source_size else ""
            match = self._pmx_match_label(item)
            self.pmx_tree.insert(
                "",
                "end",
                iid=str(index),
                text=item.display_name or item.path.stem,
                values=(match, item.rarity, item.role, size),
            )
        self.pmx_count_label.configure(
            text=f"{len(self.visible_pmx_items):,}/{len(self.pmx_items):,}"
        )

    def _pmx_match_label(self, item: pmx_browser.PreviewItem) -> str:
        if self.motion is None:
            return ""
        score = self._pmx_match_score(item)
        return "官方关联" if score > 0 else ""

    def _pmx_match_score(self, item: pmx_browser.PreviewItem) -> int:
        """Accept only a direct official THP dependency relation.

        Model names, roles, and bone-name similarity intentionally do not take
        part here: they frequently join unrelated characters in this game.
        """
        if self.motion is None or self.official_motion_bindings is None:
            return 0
        model_root = Path(__file__).resolve().parent / "unpacked" / "model"
        return 100 if self.official_motion_bindings.matches_motion(
            self.motion.header.path, model_root, item.source_mesh
        ) else 0

    def _filter_pmx_for_motion(self) -> None:
        if self.motion is None:
            messagebox.showinfo(APP_TITLE, "请先从左侧选择一个动作。")
            return
        self.motion_filter_active = True
        self.pmx_search_var.set("")
        self._apply_pmx_filter()
        self._select_best_motion_candidate()

    def _select_best_motion_candidate(self) -> None:
        if self.motion is None or not self.visible_pmx_items:
            return
        recommended = max(
            range(len(self.visible_pmx_items)),
            key=lambda index: self._pmx_match_score(self.visible_pmx_items[index]),
            default=None,
        )
        if recommended is not None and self._pmx_match_score(self.visible_pmx_items[recommended]) > 0:
            iid = str(recommended)
            self.pmx_tree.selection_set(iid)
            self.pmx_tree.see(iid)
            self._pmx_selected()

    def _pmx_selected(self, _event=None) -> None:
        selected = self.pmx_tree.selection()
        if not selected:
            return
        index = int(selected[0])
        if 0 <= index < len(self.visible_pmx_items):
            item = self.visible_pmx_items[index]
            self.pmx_var.set(str(item.path))
            self._load_pmx(item.path)

    def _source_mesh_layout_for_pmx(
        self,
        pmx_path: Path, pmx_bone_names: tuple[str, ...]
    ) -> tuple[Path | None, np.ndarray | None]:
        """Recover the exported NeoX bind layout from the cached source index."""
        source_name = self.pmx_source_meshes.get(str(pmx_path.resolve()).lower(), "")
        if not source_name:
            metadata_path = pmx_path.parent / ".build.json"
            try:
                source_name = str(
                    json.loads(metadata_path.read_text(encoding="utf-8")).get(
                        "source_mesh", ""
                    )
                )
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
        if not source_name or self.official_motion_bindings is None:
            return None, None
        candidates = self.official_motion_bindings.source_mesh_paths(source_name)
        if len(candidates) != 1:
            return None, None
        try:
            from onmyoji_rigged_mesh_gui import read_mesh_bone_bind_layout

            mesh_names, _mesh_parents, matrices = read_mesh_bone_bind_layout(candidates[0])
            by_name = {normalized_bone_name(name): index for index, name in enumerate(mesh_names)}
            indices = [by_name.get(normalized_bone_name(name), -1) for name in pmx_bone_names]
            if any(index < 0 for index in indices):
                return candidates[0], None
            return candidates[0], np.asarray([matrices[index] for index in indices], dtype=np.float32).reshape(-1, 4, 4)
        except Exception:
            return candidates[0], None

    def _load_pmx(self, path: Path) -> None:
        self.pmx_load_generation += 1
        generation = self.pmx_load_generation
        if self.gpu_renderer is None:
            try:
                from pmx_preview_gui import GpuPreviewRenderer
                self.gpu_renderer = GpuPreviewRenderer()
            except Exception:
                self.gpu_renderer = None
        if self.gpu_renderer is None:
            self.status_var.set("当前显卡/OpenGL 无法载入模型预览；骨架预览和 VMD 导出仍可使用。")
            return
        self.status_var.set(f"正在载入 PMX 模型：{path.name}")

        def worker() -> None:
            try:
                import pymeshio.pmx.reader
                from pmx_preview_gui import load_preview

                model = pymeshio.pmx.reader.read_from_file(str(path))
                preview = load_preview(path, model=model)
                bone_names = tuple(str(bone.name) for bone in model.bones)
                mesh_path, mesh_bind_matrices = self._source_mesh_layout_for_pmx(path, bone_names)
                joints = np.zeros((len(model.vertices), 4), dtype=np.int32)
                weights = np.zeros((len(model.vertices), 4), dtype=np.float32)
                for vertex_index, vertex in enumerate(model.vertices):
                    deform = vertex.deform
                    available: list[tuple[int, float]] = []
                    for slot in range(4):
                        index_name = f"index{slot}"
                        if not hasattr(deform, index_name):
                            continue
                        bone_index = int(getattr(deform, index_name))
                        weight_name = f"weight{slot}"
                        if hasattr(deform, weight_name):
                            weight = float(getattr(deform, weight_name))
                        elif slot == 0 and hasattr(deform, "weight0"):
                            weight = float(deform.weight0)
                        elif slot == 1 and hasattr(deform, "weight0"):
                            weight = 1.0 - float(deform.weight0)
                        else:
                            weight = 1.0 if slot == 0 else 0.0
                        available.append((bone_index, max(0.0, weight)))
                    total = sum(weight for _, weight in available)
                    if total <= 1.0e-8:
                        available = [(0, 1.0)]
                        total = 1.0
                    for slot, (bone_index, weight) in enumerate(available[:4]):
                        joints[vertex_index, slot] = max(0, min(bone_index, len(model.bones) - 1))
                        weights[vertex_index, slot] = weight / total
                payload = {
                    "preview": preview,
                    "base": preview.positions.copy(),
                    "joints": joints,
                    "weights": weights,
                    "bone_names": bone_names,
                    "bind": np.asarray(
                        [(bone.position.x, bone.position.y, bone.position.z) for bone in model.bones],
                        dtype=np.float32,
                    ),
                    "parents": tuple(int(getattr(bone, "parent_index", -1)) for bone in model.bones),
                    "mapping": None,
                    "offset": None,
                    "reference_pose": None,
                    "mesh_path": mesh_path,
                    "mesh_bind_matrices": mesh_bind_matrices,
                }
                self.worker_queue.put(("pmx_done", (generation, path, payload)))
            except Exception as exc:
                self.worker_queue.put(("error", f"PMX 载入失败：{type(exc).__name__}: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _open_motion(self) -> None:
        value = filedialog.askopenfilename(
            initialdir=self.root_var.get(),
            filetypes=(("式神动作", "*.rawanimation"), ("所有文件", "*.*")),
        )
        if value:
            self._load_path(Path(value))

    def _scan(self) -> None:
        root = Path(self.root_var.get())
        if not root.is_dir():
            messagebox.showerror(APP_TITLE, "动作资源目录不存在。")
            return
        self.scan_generation += 1
        generation = self.scan_generation
        self.headers.clear()
        self._apply_filter()
        self.status_var.set("正在扫描动作元数据……")

        def worker() -> None:
            found: list[MotionHeader] = []
            failed = 0
            paths = list(root.rglob("*.rawanimation"))

            def inspect(path: Path) -> MotionHeader | None:
                try:
                    header = read_motion_header(path)
                    if header.version == 0:
                        return header
                except (OSError, MotionFormatError):
                    return None
                return None

            with ThreadPoolExecutor(max_workers=12) as executor:
                results = executor.map(inspect, paths)
                for index, header in enumerate(results, 1):
                    if header is not None:
                        found.append(header)
                    else:
                        failed += 1
                    if index % 1000 == 0:
                        self.worker_queue.put(("scan_progress", (generation, index)))
            found.sort(key=lambda h: (h.skeleton_name.lower(), h.action.lower(), str(h.path)))
            self.worker_queue.put(("scan_done", (generation, found, failed)))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_filter(self) -> None:
        query = self.search_var.get().strip().lower()
        self.visible_headers = [
            header for header in self.headers
            if not query or query in f"{header.action} {header.skeleton_name} {header.path}".lower()
        ]
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        for index, header in enumerate(self.visible_headers[:DISPLAY_LIMIT]):
            self.tree.insert(
                "", "end", iid=str(index),
                values=(header.action, header.skeleton_name, f"{header.duration:.2f}s / {header.sample_rate:g}"),
            )

    def _tree_selected(self, _event=None) -> None:
        selected = self.tree.selection()
        if selected:
            index = int(selected[0])
            if 0 <= index < len(self.visible_headers):
                self._load_path(self.visible_headers[index].path)

    def _load_path(self, path: Path) -> None:
        self.playing = False
        self.play_button.configure(text="播放")
        self.status_var.set(f"正在解码：{path.name}")
        if self.official_motion_bindings is None:
            self._load_official_motion_bindings()
        if not self.pmx_items:
            self._refresh_pmx_items()

        def worker() -> None:
            try:
                motion = decode_motion(path)
                parents = self._find_parents(motion)
                metadata_root = Path(self.root_var.get())
                metadata_key = (
                    str(metadata_root.resolve()),
                    normalized_bone_name(motion.header.action),
                    motion.header.bone_names,
                )
                if metadata_key in self.animation_metadata_cache:
                    metadata = self.animation_metadata_cache[metadata_key]
                else:
                    metadata = find_animation_metadata(metadata_root, motion.header)
                    self.animation_metadata_cache[metadata_key] = metadata
                motion, clip_alignment = trim_motion_to_animation_metadata(
                    motion, metadata
                )
                cache_key = (
                    motion.header.skeleton_name.lower(), motion.header.bone_names
                )
                bind_transforms = self.bind_cache.get(cache_key)
                self.worker_queue.put(
                    (
                        "motion_done",
                        (motion, parents, bind_transforms, metadata, clip_alignment),
                    )
                )
            except Exception as exc:
                self.worker_queue.put(("error", f"{type(exc).__name__}: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _find_parents(self, motion: DecodedMotion) -> tuple[int, ...]:
        root = Path(self.root_var.get())
        cache_key = (motion.header.skeleton_name.lower(), motion.header.bone_names)
        cached = self.parent_cache.get(cache_key)
        if cached is not None:
            self.motion_bind_transforms = self.bind_cache.get(cache_key)
            return cached
        try:
            from onmyoji_rigged_mesh_gui import read_skeleton_hierarchy

            wanted = motion.header.skeleton_name.lower()
            raw_keys = tuple(
                normalized_bone_name(name) for name in motion.header.bone_names
            )
            best_hierarchy = None
            best_raw_to_skeleton: list[int] = []
            best_rank = (-1, -1, -1)
            for skeleton_path in root.rglob("*.skeleton"):
                hierarchy = read_skeleton_hierarchy(skeleton_path)
                if hierarchy is None or not (
                    hierarchy.name.lower() == wanted
                    or hierarchy.name.lower().startswith(wanted + "_")
                ):
                    continue
                by_key = {
                    normalized_bone_name(name): index
                    for index, name in enumerate(hierarchy.bone_names)
                }
                raw_to_skeleton = [by_key.get(key, -1) for key in raw_keys]
                score = sum(value >= 0 for value in raw_to_skeleton)
                selected = {value for value in raw_to_skeleton if value >= 0}
                linked = sum(
                    value >= 0 and hierarchy.bone_parents[value] in selected
                    for value in raw_to_skeleton
                )
                rank = (score, linked, -abs(len(hierarchy.bone_names) - len(raw_keys)))
                if rank > best_rank:
                    best_hierarchy = hierarchy
                    best_raw_to_skeleton = raw_to_skeleton
                    best_rank = rank
                if score == len(raw_keys) and linked >= len(raw_keys) - 1:
                    break
            if best_hierarchy is not None:
                skeleton_to_raw = {
                    value: index
                    for index, value in enumerate(best_raw_to_skeleton)
                    if value >= 0
                }
                result = tuple(
                    skeleton_to_raw.get(best_hierarchy.bone_parents[value], -1)
                    if value >= 0 else -1
                    for value in best_raw_to_skeleton
                )
                mapped_bind = None
                if (
                    len(best_hierarchy.bone_bind_transforms)
                    == len(best_hierarchy.bone_names)
                ):
                    # Keep unmatched animation bones on their first-frame value,
                    # but use the exact Skeleton bind TRS wherever names match.
                    mapped_bind = motion.frames[0].copy()
                    source_bind = np.asarray(
                        best_hierarchy.bone_bind_transforms, dtype=np.float32
                    )
                    for raw_index, skeleton_index in enumerate(
                        best_raw_to_skeleton
                    ):
                        if skeleton_index >= 0:
                            mapped_bind[raw_index] = source_bind[skeleton_index]
                self.parent_cache[cache_key] = result
                self.bind_cache[cache_key] = mapped_bind
                self.motion_bind_transforms = mapped_bind
                return result
        except Exception:
            pass
        result = tuple(-1 for _ in motion.header.bone_names)
        self.parent_cache[cache_key] = result
        self.bind_cache[cache_key] = None
        self.motion_bind_transforms = None
        return result

    def _poll_workers(self) -> None:
        try:
            while True:
                kind, payload = self.worker_queue.get_nowait()
                if kind == "scan_progress":
                    generation, count = payload
                    if generation == self.scan_generation:
                        self.status_var.set(f"正在扫描……已检查 {count:,} 个动作")
                elif kind == "scan_done":
                    generation, headers, failed = payload
                    if generation == self.scan_generation:
                        self.headers = headers
                        self._apply_filter()
                        display_note = (
                            f"；列表最多显示 {DISPLAY_LIMIT:,} 条，请用筛选缩小范围"
                            if len(headers) > DISPLAY_LIMIT else ""
                        )
                        self.status_var.set(
                            f"扫描完成：{len(headers):,} 个可用 v0 动作，跳过 {failed:,} 个异常/新版文件{display_note}。"
                        )
                elif kind == "motion_done":
                    (
                        self.motion,
                        self.parents,
                        self.motion_bind_transforms,
                        self.animation_metadata,
                        self.motion_clip_alignment,
                    ) = payload
                    motion = self.motion
                    self.timeline.configure(to=max(motion.duration, 0.001))
                    self.timeline_var.set(0.0)
                    roots = sum(parent < 0 for parent in self.parents)
                    self.info_var.set(
                        f"{motion.header.action}  ·  {motion.header.skeleton_name}  ·  "
                        f"{motion.sample_count} 帧 @ {motion.sample_rate:g} FPS  ·  "
                        f"{len(motion.header.bone_names)} 骨骼"
                    )
                    suffix = "" if roots < len(self.parents) else "；未找到配套 skeleton，只显示关节点"
                    if self.animation_metadata is not None:
                        props = self.animation_metadata.property_map
                        suffix += f"；已关联官方动画元数据（CachedPose {len(self.animation_metadata.cached_poses)}，ExtractedJointIndex={props.get('ExtractedJointIndex', '?')}）"
                    if self.motion_clip_alignment is not None:
                        clipped = self.motion_clip_alignment.start_frame
                        suffix += f"；已按官方片段裁去 {clipped} 帧预滚（CachedPose 已验证）"
                    self.status_var.set(f"动作载入完成{suffix}")
                    self.motion_filter_active = True
                    self._apply_pmx_filter()
                    self._select_best_motion_candidate()
                    self._connect_skin()
                    self._render(0.0)
                elif kind == "pmx_list_done":
                    items, classifications = payload
                    self.pmx_catalog_loading = False
                    self._finish_pmx_items(items, classifications)
                elif kind == "bindings_done":
                    self.bindings_loading = False
                    self.official_motion_bindings = payload
                    self._apply_pmx_filter()
                    if self.motion_filter_active:
                        self._select_best_motion_candidate()
                    # A PMX selected while the index was loading can now get
                    # its exact NeoX bind matrices without a new user action.
                    loaded = Path(self.pmx_var.get())
                    if loaded.is_file() and self.skin is not None:
                        self._load_pmx(loaded)
                elif kind == "bindings_error":
                    self.bindings_loading = False
                    self.status_var.set(f"官方资源关联索引不可用：{payload}")
                elif kind == "pmx_done":
                    generation, path, skin = payload
                    if generation != self.pmx_load_generation:
                        continue
                    self.skin = skin
                    self.pmx_var.set(str(path))
                    try:
                        self.gpu_renderer.prepare(self.skin["preview"])
                        self._connect_skin()
                        if self.motion is None:
                            self.status_var.set("PMX 模型载入完成；选择动作后将自动套入预览。")
                        self._render(self.timeline_var.get())
                    except Exception as exc:
                        self.skin = None
                        messagebox.showerror(APP_TITLE, f"PMX 模型预览准备失败：\n{exc}")
                elif kind == "error":
                    self.status_var.set("动作载入失败")
                    messagebox.showerror(APP_TITLE, str(payload))
        except queue.Empty:
            pass
        self.after(40, self._poll_workers)

    def _toggle_play(self) -> None:
        if self.motion is None:
            return
        self.playing = not self.playing
        if self.playing:
            self.play_offset = self.timeline_var.get()
            self.play_started = time.perf_counter()
        self.play_button.configure(text="暂停" if self.playing else "播放")

    def _rewind(self) -> None:
        self.timeline_var.set(0.0)
        self.play_offset = 0.0
        self.play_started = time.perf_counter()
        self._render(0.0)

    def _timeline_changed(self, value: str) -> None:
        if self.motion is None:
            return
        current = float(value)
        self.play_offset = current
        self.play_started = time.perf_counter()
        self._render(current)

    def _tick(self) -> None:
        if self.playing and self.motion is not None:
            current = self.play_offset + (time.perf_counter() - self.play_started) * self.speed_var.get()
            if current > self.motion.duration:
                if self.loop_var.get() and self.motion.duration > 0:
                    current %= self.motion.duration
                    self.play_offset = current
                    self.play_started = time.perf_counter()
                else:
                    current = self.motion.duration
                    self.playing = False
                    self.play_button.configure(text="播放")
            self.timeline_var.set(current)
            self._render(current)
        self.after(33, self._tick)

    def _connect_skin(self) -> None:
        if self.skin is None or self.motion is None:
            return
        motion_names = self.motion.header.bone_names
        by_key = {normalized_bone_name(name): index for index, name in enumerate(motion_names)}
        aliases: dict[str, list[int]] = {}
        for index, name in enumerate(motion_names):
            for alias in self._bone_aliases(name):
                aliases.setdefault(alias, []).append(index)
        mapped: list[int] = []
        relaxed = 0
        for name in self.skin["bone_names"]:
            exact = by_key.get(normalized_bone_name(name))
            if exact is not None:
                mapped.append(exact)
                continue
            candidates = {
                value for alias in self._bone_aliases(name)
                for value in aliases.get(alias, ())
            }
            if len(candidates) == 1:
                mapped.append(next(iter(candidates)))
                relaxed += 1
            else:
                mapped.append(-1)
        mapping = np.asarray(mapped, dtype=np.int32)
        self.skin["mapping"] = mapping
        matched = np.flatnonzero(mapping >= 0)
        if not len(matched):
            self.skin["offset"] = None
            self.skin["reference_pose"] = None
            self.status_var.set("所选 PMX 与动作没有可映射骨骼；继续显示骨架预览。")
            return
        reference_local = (
            self.motion_bind_transforms
            if self.motion_bind_transforms is not None
            else self.motion.frames[0]
        )
        self.skin["reference_pose"] = compose_global_transforms(
            reference_local, self.parents
        )
        self.skin["exact_bind"] = self.motion_bind_transforms is not None
        direct_compatible = self._direct_skin_compatible(
            mapping, self.skin["reference_pose"][0]
        )
        self.skin["retarget"] = not direct_compatible
        self.skin["offset"] = np.zeros(3, dtype=np.float32)
        if self.motion_bind_transforms is not None:
            note = "已使用 Skeleton 精确绑定姿势"
        else:
            note = "未找到绑定姿势，暂以动作首帧校准"
        if self.skin.get("mesh_bind_matrices") is not None:
            note += "；预览蒙皮已使用原始 Mesh bind 矩阵"
        self.status_var.set(
            f"PMX 动作匹配完成：{len(matched)}/{len(mapping)} 根骨骼"
            f"（其中皮肤别名映射 {relaxed}）；"
            f"{'启用形态重定向；' if self.skin['retarget'] else ''}{note}。"
        )

    def _direct_skin_compatible(
        self, mapping: np.ndarray, source_bind_positions: np.ndarray
    ) -> bool:
        """Whether global deltas can be applied to this PMX without retargeting.

        Equal bone counts alone are not enough: different character forms can
        retain the same count while changing a parent link or a limb length.
        Those cases need local-space retargeting to keep hands and feet attached
        to the target form's own bind pose.
        """
        if self.skin is None or self.motion is None:
            return False
        target_names = self.skin["bone_names"]
        if len(target_names) != len(self.motion.header.bone_names):
            return False
        if np.any(mapping < 0):
            return False
        target_parents = tuple(int(value) for value in self.skin.get("parents", ()))
        target_bind = np.asarray(self.skin["bind"], dtype=np.float32)
        if len(target_parents) != len(target_bind):
            return False
        for target_index, raw_index in enumerate(mapping):
            parent = target_parents[target_index]
            source_parent = (
                self.parents[int(raw_index)]
                if 0 <= int(raw_index) < len(self.parents)
                else -1
            )
            mapped_parent = (
                int(mapping[parent]) if 0 <= parent < len(mapping) else -1
            )
            if source_parent != mapped_parent:
                return False
            if parent < 0 or not self.skin.get("exact_bind"):
                continue
            source_edge = (
                source_bind_positions[int(raw_index)]
                - source_bind_positions[mapped_parent]
            )
            target_edge = target_bind[target_index] - target_bind[parent]
            source_length = float(np.linalg.norm(source_edge))
            target_length = float(np.linalg.norm(target_edge))
            tolerance = max(0.02, source_length * 0.03)
            if abs(source_length - target_length) > tolerance:
                return False
        return True

    @staticmethod
    def _bone_aliases(name: str) -> set[str]:
        """Generate conservative aliases used by skin variants.

        Skin/export variants commonly prepend a model id or append ``show``/
        ``skin`` to otherwise identical bone names. Only unique aliases are
        accepted by _connect_skin, so ambiguous short names cannot misbind.
        """
        raw = str(name).strip().lower().replace("\\", "_").replace("/", "_")
        parts = [part for part in re.split(r"[_\-\. ]+", raw) if part]
        values = {normalized_bone_name(raw)}
        while len(parts) > 1 and (
            re.fullmatch(r"(?:s|c|j|q|xj|npc)?\d+", parts[0])
            or parts[0] in {"show", "skin", "model", "battle", "default"}
        ):
            parts.pop(0)
            values.add(normalized_bone_name("_".join(parts)))
        while len(parts) > 1 and parts[-1] in {"show", "skin", "model", "battle", "default"}:
            parts.pop()
            values.add(normalized_bone_name("_".join(parts)))
        if parts:
            values.add(normalized_bone_name(parts[-1]))
        return {value for value in values if value}

    @staticmethod
    def _rotate_vectors(quaternions: np.ndarray, values: np.ndarray) -> np.ndarray:
        vector = quaternions[:, :3]
        return values + 2.0 * np.cross(
            vector, np.cross(vector, values) + quaternions[:, 3:4] * values
        )

    def _matrix_skin_positions(self, frame: int) -> np.ndarray | None:
        """Skin through the original NeoX bind matrices when they are available.

        PMX bone points are display aids.  They do not contain each bone's bind
        orientation or non-uniform scale, which is why they cannot correctly
        drive another form of the same character.
        """
        if self.skin is None or self.motion is None or self.motion_bind_transforms is None:
            return None
        target_bind = self.skin.get("mesh_bind_matrices")
        mapping = self.skin.get("mapping")
        if target_bind is None or mapping is None:
            return None
        target_bind = np.asarray(target_bind, dtype=np.float32)
        mapping = np.asarray(mapping, dtype=np.int32)
        count = len(target_bind)
        if target_bind.shape != (count, 4, 4) or len(mapping) != count:
            return None
        target_parents = tuple(int(value) for value in self.skin.get("parents", ()))
        reference = np.asarray(self.motion_bind_transforms, dtype=np.float32)
        source = self.motion.frames[frame]
        if len(reference) != len(source):
            return None
        source_delta = np.empty((len(source), 4, 4), dtype=np.float32)
        for index in range(len(source)):
            source_delta[index] = matrix4_multiply(
                inverse_affine_row_matrix4(trs_row_matrix4(reference[index])),
                trs_row_matrix4(source[index]),
            )
        target_current = np.zeros_like(target_bind)
        visiting = np.zeros(count, dtype=np.uint8)

        def visit(index: int) -> None:
            if visiting[index] == 2:
                return
            visiting[index] = 1
            parent = target_parents[index] if index < len(target_parents) else -1
            if not (0 <= parent < count) or parent == index or visiting[parent] == 1:
                parent = -1
            if parent >= 0:
                visit(parent)
                bind_local = matrix4_multiply(
                    target_bind[index], inverse_affine_row_matrix4(target_bind[parent])
                )
            else:
                bind_local = target_bind[index]
            raw_index = int(mapping[index])
            current_local = bind_local
            if 0 <= raw_index < len(source_delta):
                current_local = matrix4_multiply(bind_local, source_delta[raw_index])
            target_current[index] = (
                matrix4_multiply(current_local, target_current[parent])
                if parent >= 0 else current_local
            )
            visiting[index] = 2

        for index in range(count):
            visit(index)
        bind_pmx = neox_to_pmx_matrix4(target_bind)
        current_pmx = neox_to_pmx_matrix4(target_current)
        skin_matrices = matrix4_multiply(inverse_affine_row_matrix4(bind_pmx), current_pmx)
        base = np.asarray(self.skin["base"], dtype=np.float32)
        points = np.column_stack((base, np.ones(len(base), dtype=np.float32)))
        joints = np.asarray(self.skin["joints"], dtype=np.int32)
        weights = np.asarray(self.skin["weights"], dtype=np.float32)
        result = np.zeros_like(base)
        for slot in range(4):
            bone_indices = joints[:, slot]
            transformed = np.einsum("ni,nij->nj", points, skin_matrices[bone_indices])[:, :3]
            result += transformed * weights[:, slot:slot + 1]
        return result

    def _skin_positions(self, frame: int) -> np.ndarray | None:
        if self.skin is None or self.motion is None:
            return None
        matrix_result = self._matrix_skin_positions(frame)
        if matrix_result is not None:
            return matrix_result
        mapping = self.skin.get("mapping")
        reference_pose = self.skin.get("reference_pose")
        if mapping is None or reference_pose is None:
            return None
        if self.skin.get("retarget"):
            pose_pos, pose_rot, pose_scale = self._retarget_pose(frame)
            reference_pos = np.asarray(self.skin["bind"], dtype=np.float32)
            reference_rot = np.zeros_like(pose_rot)
            reference_rot[:, 3] = 1.0
            reference_scale = np.ones_like(pose_scale)
            target_mode = True
        else:
            pose_pos, pose_rot, pose_scale = compose_global_transforms(
                self.motion.frames[frame], self.parents
            )
            reference_pos, reference_rot, reference_scale = reference_pose
            target_mode = False
        base = self.skin["base"]
        joints = self.skin["joints"]
        weights = self.skin["weights"]
        bind = self.skin["bind"]
        result = np.zeros_like(base)
        for slot in range(4):
            pmx_bones = joints[:, slot]
            raw_bones = mapping[pmx_bones]
            valid = raw_bones >= 0
            if target_mode:
                safe_bones = pmx_bones
            else:
                safe_bones = np.maximum(raw_bones, 0)
            # Skeleton display masking is intentionally not used for skinning.
            # It is only a camera-framing aid for the line skeleton; applying it
            # here freezes vertices bound to helper branches in another form.
            valid &= np.isfinite(pose_pos[safe_bones]).all(axis=1)
            valid &= np.isfinite(pose_rot[safe_bones]).all(axis=1)
            valid &= np.isfinite(pose_scale[safe_bones]).all(axis=1)
            relative = base - bind[pmx_bones]
            rotation_delta = quaternion_delta(
                pose_rot[safe_bones], reference_rot[safe_bones]
            )
            scale_delta = np.divide(
                pose_scale[safe_bones],
                reference_scale[safe_bones],
                out=np.ones_like(pose_scale[safe_bones]),
                where=np.abs(reference_scale[safe_bones]) > 1.0e-8,
            )
            translation_delta = pose_pos[safe_bones] - reference_pos[safe_bones]
            transformed = self._rotate_vectors(
                rotation_delta, relative * scale_delta
            ) + bind[pmx_bones] + translation_delta
            transformed[~valid] = base[~valid]
            result += transformed * weights[:, slot:slot + 1]
        return result

    def _retarget_pose(self, frame: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Apply local motion deltas to the selected PMX shape's own bind pose."""
        assert self.skin is not None and self.motion is not None
        mapping = np.asarray(self.skin["mapping"], dtype=np.int32)
        bind = np.asarray(self.skin["bind"], dtype=np.float32)
        target_parents = tuple(int(value) for value in self.skin.get("parents", ()))
        source = self.motion.frames[frame]
        reference = (
            self.motion_bind_transforms
            if self.motion_bind_transforms is not None
            else self.motion.frames[0]
        )
        count = len(bind)
        positions = bind.copy()
        rotations = np.zeros((count, 4), dtype=np.float32)
        rotations[:, 3] = 1.0
        scales = np.ones((count, 3), dtype=np.float32)
        visiting = np.zeros(count, dtype=np.uint8)

        def converted_translation(value: np.ndarray) -> np.ndarray:
            result = np.asarray(value, dtype=np.float32).copy()
            result[[0, 2]] *= -1.0
            return result

        def converted_rotation(value: np.ndarray) -> np.ndarray:
            result = np.asarray(value, dtype=np.float32).copy()
            result[[0, 2]] *= -1.0
            return result

        def visit(index: int) -> None:
            if visiting[index] == 2:
                return
            visiting[index] = 1
            parent = target_parents[index] if index < len(target_parents) else -1
            if not (0 <= parent < count) or parent == index or visiting[parent] == 1:
                parent = -1
            raw_index = int(mapping[index]) if index < len(mapping) else -1
            if raw_index >= 0:
                local_delta = converted_translation(
                    source[raw_index, :3] - reference[raw_index, :3]
                )
                local_rot = quaternion_delta(
                    converted_rotation(source[raw_index, 3:7]),
                    converted_rotation(reference[raw_index, 3:7]),
                )
                local_scale = np.divide(
                    source[raw_index, 7:10], reference[raw_index, 7:10],
                    out=np.ones(3, dtype=np.float32),
                    where=np.abs(reference[raw_index, 7:10]) > 1.0e-8,
                )
            else:
                local_delta = np.zeros(3, dtype=np.float32)
                local_rot = np.asarray((0, 0, 0, 1), dtype=np.float32)
                local_scale = np.ones(3, dtype=np.float32)
            if parent >= 0:
                visit(parent)
                bind_local = bind[index] - bind[parent]
                positions[index] = positions[parent] + self._rotate_vectors(
                    rotations[parent][None, :],
                    (bind_local + local_delta)[None, :] * scales[parent][None, :],
                )[0]
                rotations[index] = quaternion_multiply(rotations[parent], local_rot)
                scales[index] = scales[parent] * local_scale
            else:
                positions[index] = bind[index] + local_delta
                rotations[index] = local_rot
                scales[index] = local_scale
            norm = float(np.linalg.norm(rotations[index]))
            if norm > 1.0e-8:
                rotations[index] /= norm
            visiting[index] = 2

        for index in range(count):
            visit(index)
        return positions, rotations, scales

    def _render(self, seconds: float) -> None:
        motion = self.motion
        if motion is None or motion.sample_count == 0:
            return
        frame = min(motion.sample_count - 1, max(0, int(round(seconds * motion.sample_rate))))
        skinned = self._skin_positions(frame)
        if skinned is not None and self.gpu_renderer is not None:
            try:
                self.gpu_renderer.update_positions(skinned)
                width = max(100, self.canvas.winfo_width())
                height = max(100, self.canvas.winfo_height())
                image = self.gpu_renderer.render(
                    width, height, self.yaw, self.pitch, self.zoom, 0.0, 0.0, False
                )
                self.preview_image = ImageTk.PhotoImage(image)
                self.canvas.delete("all")
                self.canvas.create_image(0, 0, anchor="nw", image=self.preview_image)
                self.time_label.configure(text=f"{seconds:.2f} / {motion.duration:.2f} 秒  ·  帧 {frame}")
                return
            except Exception as exc:
                self.status_var.set(f"模型动画预览失败，已切回骨架：{exc}")
                self.skin["offset"] = None
                self.skin["reference_pose"] = None
        points = compose_global_positions(motion.frames[frame], self.parents)
        visible = skeleton_display_mask(points, self.parents)
        cy, sy = math.cos(self.yaw), math.sin(self.yaw)
        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        x = cy * points[:, 0] + sy * points[:, 2]
        z = -sy * points[:, 0] + cy * points[:, 2]
        y = cp * points[:, 1] - sp * z
        depth = sp * points[:, 1] + cp * z
        projected = np.column_stack((x, y, depth))
        width = max(100, self.canvas.winfo_width())
        height = max(100, self.canvas.winfo_height())
        fit_points = projected[visible] if visible.any() else projected
        span = np.ptp(fit_points[:, :2], axis=0)
        scale = 0.82 * min(width / max(span[0], 1.0), height / max(span[1], 1.0)) * self.zoom
        center = (fit_points[:, :2].min(axis=0) + fit_points[:, :2].max(axis=0)) * 0.5
        screen = np.empty((len(points), 2), dtype=np.float32)
        screen[:, 0] = (projected[:, 0] - center[0]) * scale + width * 0.5
        screen[:, 1] = height * 0.5 - (projected[:, 1] - center[1]) * scale

        self.canvas.delete("all")
        lines = []
        for index, parent in enumerate(self.parents):
            if 0 <= parent < len(screen) and visible[index] and visible[parent]:
                lines.append((float((depth[index] + depth[parent]) * 0.5), parent, index))
        for _, parent, child in sorted(lines):
            self.canvas.create_line(*screen[parent], *screen[child], fill="#75b9ff", width=2)
        for index in (index for index in np.argsort(depth) if visible[index]):
            px, py = screen[index]
            radius = 2.5
            self.canvas.create_oval(px-radius, py-radius, px+radius, py+radius, fill="#f4d35e", outline="")
        self.time_label.configure(text=f"{seconds:.2f} / {motion.duration:.2f} 秒  ·  帧 {frame}")

    def _drag_start(self, event) -> None:
        self.drag = (event.x, event.y, self.yaw, self.pitch)

    def _drag_move(self, event) -> None:
        if self.drag is None:
            return
        x, y, yaw, pitch = self.drag
        self.yaw = yaw + (event.x - x) * 0.01
        self.pitch = max(-1.45, min(1.45, pitch + (event.y - y) * 0.01))
        self._render(self.timeline_var.get())

    def _wheel(self, event) -> None:
        self.zoom = max(0.25, min(6.0, self.zoom * (1.1 if event.delta > 0 else 1 / 1.1)))
        self._render(self.timeline_var.get())

    def _export(self) -> None:
        if self.motion is None:
            messagebox.showinfo(APP_TITLE, "请先载入一个动作。")
            return
        pmx_path = Path(self.pmx_var.get())
        if not pmx_path.is_file():
            messagebox.showinfo(APP_TITLE, "请先选择与该动作骨架匹配的 PMX。")
            return
        default = f"{self.motion.header.skeleton_name}_{self.motion.header.action}.vmd"
        value = filedialog.asksaveasfilename(
            defaultextension=".vmd",
            initialfile=default,
            filetypes=(("VMD 动作", "*.vmd"),),
        )
        if not value:
            return
        try:
            vmd, compatible, matched, total, scale_lost = export_vmd(
                self.motion,
                pmx_path,
                Path(value),
                reference_transforms=self.motion_bind_transforms,
            )
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"导出失败：\n{type(exc).__name__}: {exc}")
            return
        note = (
            f"已导出 VMD：\n{vmd}\n\n已生成动作兼容 PMX（解决 VMD 15 字节骨名限制）：\n"
            f"{compatible}\n\n骨骼匹配：{matched}/{total}"
        )
        if scale_lost:
            note += "\n\n注意：源动作包含缩放轨道；VMD 不支持骨骼缩放，已省略缩放。"
        messagebox.showinfo(APP_TITLE, note)
        self.status_var.set(f"VMD 导出完成：匹配 {matched}/{total} 根 PMX 骨骼")


def main() -> None:
    MotionPreviewApp().mainloop()


if __name__ == "__main__":
    main()
    compose_global_row_matrices,
    find_animation_metadata,
    inverse_affine_row_matrix4,
    matrix4_multiply,
    neox_to_pmx_matrix4,
