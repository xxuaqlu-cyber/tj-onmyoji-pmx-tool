# -*- coding: utf-8 -*-
"""
阴阳师 SKPW / WPK 图形化解包器

支持本次验证过的目录结构：
    model.idx + model0.wpk ... modelN.wpk
    model_info.idx + model_info0.wpk ... model_infoN.wpk

用法：
    双击或运行：python onmyoji_wpk_gui.py
    只读自检：  python onmyoji_wpk_gui.py --self-test
"""

from __future__ import annotations

import csv
import io
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import traceback
import zlib
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_TITLE = "阴阳师 WPK 图形化解包器"
IDX_MAGIC = b"SKPW"
IDX_HEADER_SIZE = 0x20
IDX_RECORD_SIZE = 0x1C
VALID_STAGE1_TAGS = (b"PC", b"AC", b"XC")
MAX_ZSTD_OUTPUT = 512 * 1024 * 1024


@dataclass(slots=True)
class IndexRecord:
    index: int
    resource_hash: str
    key_length: int
    offset: int
    package_id: int
    stored_size: int


@dataclass(slots=True)
class ArchiveGroup:
    idx_path: Path
    stem: str
    marker: bytes
    records: list[IndexRecord]
    packages: dict[int, Path]
    invalid_ranges: int = 0
    missing_package_ids: tuple[int, ...] = ()

    @property
    def active_count(self) -> int:
        return sum(
            1 for record in self.records if record_is_active(record)
        )


def load_crypto():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as exc:
        raise RuntimeError(
            "缺少 cryptography。请点击窗口中的“安装依赖”，"
            "或运行：python -m pip install cryptography zstandard"
        ) from exc
    return Cipher, algorithms, modes


def load_zstandard():
    try:
        import zstandard
    except ImportError as exc:
        raise RuntimeError(
            "缺少 zstandard。请点击窗口中的“安装依赖”，"
            "或运行：python -m pip install zstandard"
        ) from exc
    return zstandard


def read_u24_le(data: bytes) -> int:
    return int.from_bytes(data, "little")


def record_read_size(record: IndexRecord) -> int:
    """IDX 的 key_length 是资源实际字节数；stored_size 是分配槽信息。"""
    return record.key_length if record.key_length > 0 else record.stored_size


def record_is_active(record: IndexRecord) -> bool:
    """stored_size 可为 0；只要 key_length/偏移有效，资源仍可能真实存在。"""
    return record.package_id != 0xFF and record_read_size(record) > 0


def parse_idx(idx_path: Path) -> tuple[bytes, list[IndexRecord]]:
    file_size = idx_path.stat().st_size
    with idx_path.open("rb") as stream:
        header = stream.read(IDX_HEADER_SIZE)
        if len(header) != IDX_HEADER_SIZE or header[:4] != IDX_MAGIC:
            raise ValueError(f"{idx_path.name} 不是有效的 SKPW 索引")

        marker = header[4:8]
        file_count = int.from_bytes(header[0x0C:0x10], "little")
        expected_size = IDX_HEADER_SIZE + file_count * IDX_RECORD_SIZE + 4
        if file_size != expected_size:
            raise ValueError(
                f"{idx_path.name} 尺寸不符合旧版阴阳师索引："
                f"实际 {file_size}，应为 {expected_size}"
            )

        records: list[IndexRecord] = []
        for index in range(file_count):
            raw = stream.read(IDX_RECORD_SIZE)
            if len(raw) != IDX_RECORD_SIZE:
                raise EOFError(f"{idx_path.name} 的第 {index} 条索引不完整")

            records.append(
                IndexRecord(
                    index=index,
                    resource_hash=raw[0:16].hex(),
                    key_length=int.from_bytes(raw[16:20], "little"),
                    offset=int.from_bytes(raw[20:24], "little"),
                    package_id=raw[24],
                    stored_size=read_u24_le(raw[25:28]),
                )
            )

        footer = stream.read(4)
        if footer != marker:
            raise ValueError(
                f"{idx_path.name} 尾标记不匹配："
                f"{footer.hex()} != {marker.hex()}"
            )

    return marker, records


def discover_groups(folder: Path) -> list[ArchiveGroup]:
    groups: list[ArchiveGroup] = []
    for idx_path in sorted(folder.glob("*.idx"), key=lambda p: p.name.lower()):
        marker, records = parse_idx(idx_path)
        stem = idx_path.stem
        package_pattern = re.compile(
            rf"^{re.escape(stem)}(\d+)\.wpk$", re.IGNORECASE
        )
        packages: dict[int, Path] = {}
        for candidate in folder.glob("*.wpk"):
            match = package_pattern.fullmatch(candidate.name)
            if match:
                packages[int(match.group(1))] = candidate

        required_ids = {
            record.package_id for record in records if record_is_active(record)
        }
        missing = tuple(sorted(required_ids - packages.keys()))

        invalid_ranges = 0
        for record in records:
            if not record_is_active(record):
                continue
            package_path = packages.get(record.package_id)
            if package_path is None:
                continue
            if record.offset + record_read_size(record) > package_path.stat().st_size:
                invalid_ranges += 1

        groups.append(
            ArchiveGroup(
                idx_path=idx_path,
                stem=stem,
                marker=marker,
                records=records,
                packages=packages,
                invalid_ranges=invalid_ranges,
                missing_package_ids=missing,
            )
        )

    return groups


def derive_key(length: int, t_value: int) -> bytes:
    length &= 0xFFFFFFFF
    v10 = (t_value + length) & 0xFF
    v28 = (
        0x7C2E6B6A00000000
        | ((length << 8) & 0xFFFF0000)
        | (v10 << 8)
        | (length % 0xFD)
    )
    v29 = (
        0x5C74656E00003630
        | (((v10 ^ 0x33) << 16) & 0xFFFFFFFF00FFFFFF)
        | ((v10 | 0x2E) << 24)
    )
    return struct.pack(
        "<QQ",
        v28 & 0xFFFFFFFFFFFFFFFF,
        v29 & 0xFFFFFFFFFFFFFFFF,
    )


def xor_offset(buffer: bytearray, offset: int, wanted: int, seed: int) -> None:
    if wanted <= 0:
        return
    mirror_length = min(offset, wanted)
    for index in range(mirror_length):
        buffer[offset + index] ^= (
            seed + index + buffer[index]
        ) & 0xFF
    for index in range(wanted - mirror_length):
        pos = offset + mirror_length + index
        buffer[pos] ^= (seed + mirror_length + index) & 0xFF


def header_decode(buffer: bytearray) -> None:
    length = min(64, len(buffer))
    left, right = 0, length - 1
    while left < right:
        left_value = buffer[left] ^ 0x5A
        right_value = buffer[right] ^ 0x5A
        buffer[left], buffer[right] = right_value, left_value
        left += 1
        right -= 1
    if left == right and length:
        buffer[left] ^= 0x5A


def decode_stage1(blob: bytes, key_length: int) -> tuple[bytes, str]:
    if len(blob) < 8:
        raise ValueError("资源块不足 8 字节")

    tag = blob[:2]
    p_value = blob[2]
    t_value = blob[3]
    if tag not in VALID_STAGE1_TAGS:
        raise ValueError(f"未知 WPD1 标签：{tag.hex()}")
    if p_value == 0 or p_value > 16:
        raise ValueError(f"不合理的 WPD1 参数 p={p_value}")

    key_body_length = max(0, key_length - 8)
    body = bytearray(blob[8:])
    prefix_length = min(
        len(body),
        key_body_length,
        128 << (p_value - 1),
    )
    seed = (t_value + key_body_length) & 0xFFFFFFFF

    if tag in (b"PC", b"AC"):
        Cipher, algorithms, modes = load_crypto()
        block_length = (prefix_length // 16) * 16
        if block_length:
            cipher = Cipher(algorithms.AES(derive_key(key_body_length, t_value)), modes.ECB())
            decryptor = cipher.decryptor()
            body[:block_length] = (
                decryptor.update(bytes(body[:block_length]))
                + decryptor.finalize()
            )
        remainder = prefix_length - block_length
        if remainder:
            xor_offset(body, block_length, remainder, seed)
    else:
        for index in range(prefix_length):
            body[index] ^= (seed + index) & 0xFF

    header_decode(body)

    # 当 key_length 小于分配槽长度时，槽尾通常是旧数据或填充；
    # 当 key_length 大于槽长度时，资源一般为 DTSZ 压缩，保留全部已存数据。
    if 0 < key_body_length < len(body):
        del body[key_body_length:]

    return bytes(body), tag.decode("ascii", errors="replace")


def decompress_zstd_frame(data: bytes, zstandard_module) -> bytes:
    compressed = data[4:] if data.startswith(b"DTSZ") else data
    decompressor = zstandard_module.ZstdDecompressor()
    try:
        return decompressor.decompress(
            compressed,
            max_output_size=MAX_ZSTD_OUTPUT,
        )
    except Exception:
        with decompressor.stream_reader(
            io.BytesIO(compressed),
            read_across_frames=False,
        ) as reader:
            return reader.read()


def unwrap_payload(data: bytes, zstandard_module) -> tuple[bytes, list[str]]:
    layers: list[str] = []
    for _ in range(8):
        if data.startswith(b"DTSZ"):
            data = decompress_zstd_frame(data, zstandard_module)
            layers.append("DTSZ")
            continue
        if data.startswith(b"\x28\xB5\x2F\xFD"):
            data = decompress_zstd_frame(data, zstandard_module)
            layers.append("ZSTD")
            continue
        if data.startswith(b"BILZ") or data.startswith(b"ZLIB"):
            data = zlib.decompress(data[4:])
            layers.append(data[:0].decode(errors="ignore") or "ZLIB")
            continue
        if data.startswith(b"ENON") or data.startswith(b"NONE"):
            layers.append(data[:4].decode("ascii", errors="replace"))
            data = data[4:]
            continue
        if len(data) >= 2 and data[0] == 0x78 and data[1] in (0x01, 0x5E, 0x9C, 0xDA):
            try:
                data = zlib.decompress(data)
                layers.append("ZLIB_RAW")
                continue
            except zlib.error:
                pass
        break
    return data, layers


def detect_extension(data: bytes) -> str:
    if not data:
        return "empty"
    if data.startswith(b"CocosStudio-UI"):
        return "coc"
    if data.startswith((b"<", b"\xEF\xBB\xBF<")):
        return "xml"
    if data.startswith((b"{", b"[")):
        return "json"
    if data.startswith(b"\x89PNG\r\n\x1A\n"):
        return "png"
    if data.startswith(b"\xFF\xD8\xFF"):
        return "jpg"
    if data.startswith(b"DDS "):
        return "dds"
    if data.startswith(b"\xABKTX 11\xBB\r\n\x1A\n"):
        return "ktx"
    if data.startswith(b"PVR") or data.startswith(b"\x50\x56\x52\x03"):
        return "pvr"
    if data.startswith(b"PKM"):
        return "pkm"
    if data.startswith(b"OggS"):
        return "ogg"
    if data.startswith(b"FSB5"):
        return "fsb"
    if data.startswith(b"BKHD"):
        return "bnk"
    if data.startswith(b"RIFF"):
        if data[8:12] == b"WAVE":
            return "wav"
        return "riff"
    if data.startswith(b"PK\x03\x04"):
        return "zip"
    if data.startswith(b"\x34\x80\xC8\xBB"):
        return "mesh"
    if data.startswith(b"VANT"):
        return "vant"
    if data.startswith(b"RGIS"):
        return "gis"
    if data.startswith(b"NTRK"):
        return "ntrk"
    if data.startswith(b"RAWA"):
        return "rawanimation"
    if data.startswith(b"SKELETON"):
        return "skeleton"

    probe = data[:128 * 1024]
    if b"void main" in probe or b"#include" in probe or b"technique" in probe:
        return "shader"
    try:
        text = probe.decode("utf-8")
        printable = sum(character.isprintable() or character in "\r\n\t" for character in text)
        if text and printable / len(text) > 0.93:
            return "txt"
    except UnicodeDecodeError:
        pass
    return "dat"


def semantic_label(data: bytes) -> str:
    if not data.startswith((b"<", b"\xEF\xBB\xBF<")):
        return ""
    text = data[:64 * 1024].decode("utf-8", errors="ignore")
    type_match = re.search(r'<Head\s+Type="([^"]+)"', text)
    name_match = re.search(r'<Name\s+Name="([^"]+)"', text)
    if not name_match:
        name_match = re.search(r'<FileName\s+Value="([^"]+)"', text)

    parts = []
    if name_match:
        parts.append(Path(name_match.group(1)).stem)
    if type_match:
        parts.append(type_match.group(1))

    label = "_".join(parts)
    label = re.sub(r'[<>:"/\\|?*\x00-\x1F]+', "_", label)
    label = re.sub(r"\s+", "_", label).strip("._ ")
    return label[:72]


def safe_output_path(
    output_root: Path,
    group: ArchiveGroup,
    record: IndexRecord,
    extension: str,
    label: str,
) -> Path:
    bucket = record.resource_hash[:2]
    folder = output_root / group.stem / f"pkg_{record.package_id:02d}" / bucket
    folder.mkdir(parents=True, exist_ok=True)
    label_part = f"_{label}" if label else ""
    filename = (
        f"{record.index:06d}{label_part}_"
        f"{record.resource_hash[:16]}.{extension}"
    )
    return folder / filename


def validate_record_range(group: ArchiveGroup, record: IndexRecord) -> tuple[bool, str]:
    if record.package_id == 0xFF:
        return False, "deleted"
    if record_read_size(record) <= 0:
        return False, "empty_slot"
    package_path = group.packages.get(record.package_id)
    if package_path is None:
        return False, "missing_package"
    read_size = record_read_size(record)
    if record.offset < 0 or read_size <= 0 or record.offset + read_size > package_path.stat().st_size:
        return False, "out_of_range"
    return True, ""


class ExtractorEngine:
    def __init__(
        self,
        log_callback: Callable[[str], None],
        progress_callback: Callable[[int, int, str], None],
        stop_event: threading.Event,
    ):
        self.log = log_callback
        self.progress = progress_callback
        self.stop_event = stop_event

    def extract_groups(
        self,
        groups: Iterable[ArchiveGroup],
        output_root: Path,
        trial_limit: int | None,
        skip_existing: bool,
    ) -> dict[str, int]:
        zstandard_module = load_zstandard()
        load_crypto()

        totals = Counter()
        groups = list(groups)
        total_records = sum(
            min(group.active_count, trial_limit)
            if trial_limit is not None
            else group.active_count
            for group in groups
        )
        completed = 0

        for group in groups:
            if self.stop_event.is_set():
                break
            group_limit = trial_limit
            self.log(
                f"开始处理 {group.idx_path.name}："
                f"{group.active_count:,} 条活动索引"
            )
            group_root = output_root / group.stem
            group_root.mkdir(parents=True, exist_ok=True)
            manifest_path = group_root / "manifest.csv"
            existing_by_hash: dict[str, dict[str, str]] = {}
            if skip_existing and manifest_path.is_file():
                try:
                    with manifest_path.open(
                        "r", newline="", encoding="utf-8-sig"
                    ) as old_manifest:
                        for row in csv.DictReader(old_manifest):
                            digest = (row.get("resource_hash") or "").lower()
                            relative = (row.get("output_path") or "").strip()
                            if (
                                len(digest) == 32
                                and relative
                                and row.get("status") in {"ok", "exists"}
                            ):
                                existing_by_hash[digest] = row
                except OSError:
                    existing_by_hash = {}

            handles = {
                package_id: path.open("rb")
                for package_id, path in group.packages.items()
            }
            processed_in_group = 0
            try:
                with manifest_path.open(
                    "w",
                    newline="",
                    encoding="utf-8-sig",
                ) as manifest_file:
                    writer = csv.writer(manifest_file)
                    writer.writerow(
                        [
                            "index",
                            "resource_hash",
                            "package_id",
                            "offset",
                            "key_length",
                            "stored_size",
                            "status",
                            "stage1_tag",
                            "layers",
                            "extension",
                            "output_path",
                            "error",
                        ]
                    )

                    for record in group.records:
                        if self.stop_event.is_set():
                            break
                        if not record_is_active(record):
                            totals["skipped"] += 1
                            continue
                        if group_limit is not None and processed_in_group >= group_limit:
                            break

                        processed_in_group += 1
                        completed += 1
                        valid, reason = validate_record_range(group, record)
                        if not valid:
                            totals["invalid"] += 1
                            writer.writerow(
                                [
                                    record.index,
                                    record.resource_hash,
                                    record.package_id,
                                    record.offset,
                                    record.key_length,
                                    record.stored_size,
                                    reason,
                                    "",
                                    "",
                                    "",
                                    "",
                                    "",
                                ]
                            )
                            continue

                        existing = existing_by_hash.get(record.resource_hash)
                        if existing is not None:
                            old_relative = (existing.get("output_path") or "").strip()
                            old_path = output_root / Path(
                                old_relative.replace("\\", "/")
                            )
                            if old_path.is_file():
                                totals["exists"] += 1
                                writer.writerow(
                                    [
                                        record.index,
                                        record.resource_hash,
                                        record.package_id,
                                        record.offset,
                                        record.key_length,
                                        record.stored_size,
                                        "exists",
                                        existing.get("stage1_tag") or "",
                                        existing.get("layers") or "",
                                        existing.get("extension") or old_path.suffix.lstrip("."),
                                        old_relative,
                                        "",
                                    ]
                                )
                                if (
                                    completed % 100 == 0
                                    or completed == total_records
                                ):
                                    self.progress(
                                        completed,
                                        total_records,
                                        f"{group.stem}：{processed_in_group:,} 条",
                                    )
                                continue

                        output_path = None
                        stage1_tag = ""
                        layers: list[str] = []
                        extension = ""
                        try:
                            handle = handles[record.package_id]
                            handle.seek(record.offset)
                            read_size = record_read_size(record)
                            blob = handle.read(read_size)
                            if len(blob) != read_size:
                                raise EOFError(
                                    f"应读取 {read_size}，"
                                    f"实际 {len(blob)}"
                                )

                            decoded, stage1_tag = decode_stage1(
                                blob,
                                record.key_length,
                            )
                            decoded, layers = unwrap_payload(
                                decoded,
                                zstandard_module,
                            )
                            extension = detect_extension(decoded)
                            label = semantic_label(decoded)
                            output_path = safe_output_path(
                                output_root,
                                group,
                                record,
                                extension,
                                label,
                            )

                            if skip_existing and output_path.exists():
                                status = "exists"
                                totals["exists"] += 1
                            else:
                                output_path.write_bytes(decoded)
                                status = "ok"
                                totals["ok"] += 1

                            writer.writerow(
                                [
                                    record.index,
                                    record.resource_hash,
                                    record.package_id,
                                    record.offset,
                                    record.key_length,
                                    record.stored_size,
                                    status,
                                    stage1_tag,
                                    ">".join(layers),
                                    extension,
                                    str(output_path.relative_to(output_root)),
                                    "",
                                ]
                            )
                        except Exception as exc:
                            totals["failed"] += 1
                            writer.writerow(
                                [
                                    record.index,
                                    record.resource_hash,
                                    record.package_id,
                                    record.offset,
                                    record.key_length,
                                    record.stored_size,
                                    "failed",
                                    stage1_tag,
                                    ">".join(layers),
                                    extension,
                                    (
                                        str(output_path.relative_to(output_root))
                                        if output_path
                                        else ""
                                    ),
                                    str(exc),
                                ]
                            )
                            if totals["failed"] <= 20:
                                self.log(
                                    f"失败 #{record.index} "
                                    f"pkg={record.package_id}：{exc}"
                                )

                        if completed % 100 == 0 or completed == total_records:
                            self.progress(
                                completed,
                                total_records,
                                f"{group.stem}：{processed_in_group:,} 条",
                            )
            finally:
                for handle in handles.values():
                    handle.close()

            self.log(
                f"{group.stem} 完成：新增 {totals['ok']:,}，"
                f"复用 {totals['exists']:,}，失败 {totals['failed']:,}，"
                f"无效 {totals['invalid']:,}"
            )

        return dict(totals)


class WpkGui:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1080x760")
        self.root.minsize(920, 650)

        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.groups: list[ArchiveGroup] = []

        script_folder = Path(__file__).resolve().parent
        self.folder_var = tk.StringVar(value=str(script_folder))
        self.output_var = tk.StringVar(value=str(script_folder / "unpacked"))
        self.process_all_var = tk.BooleanVar(value=True)
        self.skip_existing_var = tk.BooleanVar(value=True)
        self.trial_count_var = tk.IntVar(value=100)
        self.status_var = tk.StringVar(value="等待扫描")
        self.progress_text_var = tk.StringVar(value="0 / 0")
        self.dependency_var = tk.StringVar(value="正在检查依赖…")

        self._configure_style()
        self._build_ui()
        self._check_dependencies()
        self.root.after(100, self._poll_events)
        self.root.after(350, self.scan_folder)

    def _configure_style(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("TFrame", background="#F4F5F7")
        style.configure("Card.TFrame", background="#FFFFFF")
        style.configure(
            "Title.TLabel",
            background="#111318",
            foreground="#FFFFFF",
            font=("Microsoft YaHei UI", 18, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background="#111318",
            foreground="#AEB4C0",
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Section.TLabel",
            background="#FFFFFF",
            foreground="#1B1E24",
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        style.configure(
            "TLabel",
            background="#F4F5F7",
            foreground="#30343B",
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Primary.TButton",
            font=("Microsoft YaHei UI", 9, "bold"),
            foreground="#FFFFFF",
            background="#15171C",
            padding=(15, 8),
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#30343B"), ("disabled", "#A9ADB5")],
        )
        style.configure(
            "Accent.TButton",
            font=("Microsoft YaHei UI", 9, "bold"),
            foreground="#FFFFFF",
            background="#2F6FED",
            padding=(15, 8),
        )
        style.map(
            "Accent.TButton",
            background=[("active", "#255BCC"), ("disabled", "#A9ADB5")],
        )
        style.configure("TButton", padding=(11, 7))
        style.configure("Treeview", rowheight=28, font=("Microsoft YaHei UI", 9))
        style.configure(
            "Treeview.Heading",
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        style.configure(
            "Horizontal.TProgressbar",
            troughcolor="#E4E7EC",
            background="#2F6FED",
        )

    def _build_ui(self) -> None:
        header = tk.Frame(self.root, bg="#111318", height=82)
        header.pack(fill="x")
        header.pack_propagate(False)
        ttk.Label(header, text=APP_TITLE, style="Title.TLabel").pack(
            anchor="w", padx=26, pady=(15, 0)
        )
        ttk.Label(
            header,
            text="适用于 SKPW 索引 + 分片 WPK；支持 WPD1、DTSZ、ENON、BILZ",
            style="Subtitle.TLabel",
        ).pack(anchor="w", padx=27, pady=(2, 0))

        content = ttk.Frame(self.root, padding=18)
        content.pack(fill="both", expand=True)

        path_card = ttk.Frame(content, style="Card.TFrame", padding=16)
        path_card.pack(fill="x")
        ttk.Label(path_card, text="文件位置", style="Section.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10)
        )
        ttk.Label(path_card, text="资源目录", background="#FFFFFF").grid(
            row=1, column=0, sticky="w", padx=(0, 10)
        )
        ttk.Entry(path_card, textvariable=self.folder_var).grid(
            row=1, column=1, sticky="ew"
        )
        ttk.Button(path_card, text="选择目录", command=self.choose_folder).grid(
            row=1, column=2, padx=(10, 0)
        )

        ttk.Label(path_card, text="输出目录", background="#FFFFFF").grid(
            row=2, column=0, sticky="w", padx=(0, 10), pady=(10, 0)
        )
        ttk.Entry(path_card, textvariable=self.output_var).grid(
            row=2, column=1, sticky="ew", pady=(10, 0)
        )
        ttk.Button(path_card, text="选择输出", command=self.choose_output).grid(
            row=2, column=2, padx=(10, 0), pady=(10, 0)
        )
        path_card.columnconfigure(1, weight=1)

        archive_card = ttk.Frame(content, style="Card.TFrame", padding=16)
        archive_card.pack(fill="both", expand=True, pady=(14, 0))
        top_row = ttk.Frame(archive_card, style="Card.TFrame")
        top_row.pack(fill="x")
        ttk.Label(top_row, text="识别到的资源组", style="Section.TLabel").pack(
            side="left"
        )
        ttk.Label(
            top_row,
            textvariable=self.dependency_var,
            background="#FFFFFF",
            foreground="#68707D",
        ).pack(side="right")

        columns = ("idx", "records", "packages", "invalid", "status")
        self.tree = ttk.Treeview(
            archive_card,
            columns=columns,
            show="headings",
            height=7,
            selectmode="extended",
        )
        headings = {
            "idx": "索引文件",
            "records": "活动记录",
            "packages": "分包",
            "invalid": "越界/旧记录",
            "status": "状态",
        }
        widths = {
            "idx": 190,
            "records": 110,
            "packages": 160,
            "invalid": 110,
            "status": 260,
        }
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="center")
        self.tree.pack(fill="both", expand=True, pady=(10, 10))

        options = ttk.Frame(archive_card, style="Card.TFrame")
        options.pack(fill="x")
        ttk.Checkbutton(
            options,
            text="处理全部索引组",
            variable=self.process_all_var,
        ).pack(side="left")
        ttk.Checkbutton(
            options,
            text="跳过已存在文件",
            variable=self.skip_existing_var,
        ).pack(side="left", padx=(18, 0))
        ttk.Label(options, text="试解数量", background="#FFFFFF").pack(
            side="left", padx=(22, 6)
        )
        ttk.Spinbox(
            options,
            from_=1,
            to=5000,
            textvariable=self.trial_count_var,
            width=8,
        ).pack(side="left")

        buttons = ttk.Frame(content)
        buttons.pack(fill="x", pady=(14, 0))
        self.scan_button = ttk.Button(
            buttons,
            text="扫描目录",
            command=self.scan_folder,
        )
        self.scan_button.pack(side="left")
        self.install_button = ttk.Button(
            buttons,
            text="安装依赖",
            command=self.install_dependencies,
        )
        self.install_button.pack(side="left", padx=(8, 0))
        self.trial_button = ttk.Button(
            buttons,
            text="试解",
            command=self.start_trial,
            style="Primary.TButton",
        )
        self.trial_button.pack(side="right", padx=(8, 0))
        self.full_button = ttk.Button(
            buttons,
            text="开始全部解包",
            command=self.start_full,
            style="Accent.TButton",
        )
        self.full_button.pack(side="right")
        self.stop_button = ttk.Button(
            buttons,
            text="停止",
            command=self.stop,
            state="disabled",
        )
        self.stop_button.pack(side="right", padx=(0, 8))

        progress_frame = ttk.Frame(content)
        progress_frame.pack(fill="x", pady=(14, 0))
        ttk.Label(progress_frame, textvariable=self.status_var).pack(
            side="left"
        )
        ttk.Label(progress_frame, textvariable=self.progress_text_var).pack(
            side="right"
        )
        self.progress_bar = ttk.Progressbar(progress_frame, mode="determinate")
        self.progress_bar.pack(fill="x", pady=(6, 0))

        log_frame = ttk.Frame(content)
        log_frame.pack(fill="both", expand=False, pady=(10, 0))
        self.log_text = tk.Text(
            log_frame,
            height=8,
            bg="#15171C",
            fg="#D8DCE5",
            insertbackground="#FFFFFF",
            relief="flat",
            font=("Consolas", 9),
            padx=10,
            pady=8,
        )
        scrollbar = ttk.Scrollbar(
            log_frame,
            orient="vertical",
            command=self.log_text.yview,
        )
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _check_dependencies(self) -> None:
        missing = []
        try:
            load_crypto()
        except RuntimeError:
            missing.append("cryptography")
        try:
            load_zstandard()
        except RuntimeError:
            missing.append("zstandard")
        if missing:
            self.dependency_var.set("缺少依赖：" + ", ".join(missing))
        else:
            self.dependency_var.set("依赖完整")

    def choose_folder(self) -> None:
        selected = filedialog.askdirectory(
            title="选择包含 IDX/WPK 的目录",
            initialdir=self.folder_var.get(),
        )
        if selected:
            self.folder_var.set(selected)
            self.output_var.set(str(Path(selected) / "unpacked"))
            self.scan_folder()

    def choose_output(self) -> None:
        selected = filedialog.askdirectory(
            title="选择输出目录",
            initialdir=self.output_var.get(),
        )
        if selected:
            self.output_var.set(selected)

    def _log(self, message: str) -> None:
        self.events.put(("log", message))

    def _progress(self, current: int, total: int, label: str) -> None:
        self.events.put(("progress", current, total, label))

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for button in (
            self.scan_button,
            self.install_button,
            self.trial_button,
            self.full_button,
        ):
            button.configure(state=state)
        self.stop_button.configure(state="normal" if busy else "disabled")

    def scan_folder(self) -> None:
        if self.worker and self.worker.is_alive():
            return

        folder = Path(self.folder_var.get()).expanduser()
        if not folder.is_dir():
            messagebox.showerror(APP_TITLE, "资源目录不存在")
            return

        self._set_busy(True)
        self.status_var.set("正在扫描…")

        def task():
            try:
                groups = discover_groups(folder)
                self.events.put(("scan_result", groups))
            except Exception:
                self.events.put(("error", traceback.format_exc()))

        self.worker = threading.Thread(target=task, daemon=True)
        self.worker.start()

    def install_dependencies(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        self._set_busy(True)
        self.status_var.set("正在安装依赖…")
        self._log("执行：python -m pip install cryptography zstandard")

        def task():
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "cryptography",
                        "zstandard",
                    ],
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.events.put(
                    (
                        "install_result",
                        result.returncode,
                        result.stdout,
                        result.stderr,
                    )
                )
            except Exception:
                self.events.put(("error", traceback.format_exc()))

        self.worker = threading.Thread(target=task, daemon=True)
        self.worker.start()

    def _selected_groups(self) -> list[ArchiveGroup]:
        if self.process_all_var.get():
            return self.groups
        selected = {int(item) for item in self.tree.selection()}
        return [group for index, group in enumerate(self.groups) if index in selected]

    def start_trial(self) -> None:
        try:
            limit = max(1, int(self.trial_count_var.get()))
        except (TypeError, ValueError):
            messagebox.showerror(APP_TITLE, "试解数量不正确")
            return
        self._start_extraction(trial_limit=limit)

    def start_full(self) -> None:
        if not messagebox.askyesno(
            APP_TITLE,
            "全量解包可能输出约数 GB、近十万个文件。\n是否继续？",
        ):
            return
        self._start_extraction(trial_limit=None)

    def _start_extraction(self, trial_limit: int | None) -> None:
        if self.worker and self.worker.is_alive():
            return
        groups = self._selected_groups()
        if not groups:
            messagebox.showerror(APP_TITLE, "没有可处理的索引组")
            return
        for group in groups:
            if group.missing_package_ids:
                messagebox.showerror(
                    APP_TITLE,
                    f"{group.idx_path.name} 缺少分包："
                    + ", ".join(map(str, group.missing_package_ids)),
                )
                return
        try:
            load_crypto()
            load_zstandard()
        except RuntimeError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        output_root = Path(self.output_var.get()).expanduser()
        if trial_limit is not None:
            output_root = output_root / "_trial"
        output_root.mkdir(parents=True, exist_ok=True)

        self.stop_event.clear()
        self._set_busy(True)
        self.status_var.set("正在解包…")
        self.progress_bar["value"] = 0
        self.progress_text_var.set("0 / 0")
        mode_text = f"试解每组前 {trial_limit} 条" if trial_limit else "全量解包"
        self._log(f"{mode_text}，输出到：{output_root}")

        def task():
            try:
                engine = ExtractorEngine(
                    self._log,
                    self._progress,
                    self.stop_event,
                )
                result = engine.extract_groups(
                    groups,
                    output_root,
                    trial_limit,
                    self.skip_existing_var.get(),
                )
                self.events.put(("done", result, str(output_root)))
            except Exception:
                self.events.put(("error", traceback.format_exc()))

        self.worker = threading.Thread(target=task, daemon=True)
        self.worker.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.status_var.set("正在停止…")
        self._log("已请求停止，将在当前资源处理完后结束。")

    def _poll_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]

                if kind == "log":
                    self.log_text.insert("end", event[1] + "\n")
                    self.log_text.see("end")
                elif kind == "progress":
                    _, current, total, label = event
                    self.progress_bar["maximum"] = max(1, total)
                    self.progress_bar["value"] = current
                    self.progress_text_var.set(f"{current:,} / {total:,}")
                    self.status_var.set(label)
                elif kind == "scan_result":
                    self.groups = event[1]
                    for item in self.tree.get_children():
                        self.tree.delete(item)
                    for index, group in enumerate(self.groups):
                        package_text = ", ".join(
                            f"{package_id}:{path.name}"
                            for package_id, path in sorted(group.packages.items())
                        )
                        if group.missing_package_ids:
                            status = "缺少分包 " + ", ".join(
                                map(str, group.missing_package_ids)
                            )
                        else:
                            status = "可解包"
                        self.tree.insert(
                            "",
                            "end",
                            iid=str(index),
                            values=(
                                group.idx_path.name,
                                f"{group.active_count:,}",
                                package_text,
                                group.invalid_ranges,
                                status,
                            ),
                        )
                    self.status_var.set(
                        f"扫描完成：识别到 {len(self.groups)} 组索引"
                    )
                    self._log(
                        f"扫描完成：{', '.join(g.idx_path.name for g in self.groups) or '未发现 IDX'}"
                    )
                    self._set_busy(False)
                elif kind == "install_result":
                    _, returncode, stdout, stderr = event
                    if stdout:
                        self._log(stdout.strip())
                    if stderr:
                        self._log(stderr.strip())
                    self._check_dependencies()
                    self.status_var.set(
                        "依赖安装完成" if returncode == 0 else "依赖安装失败"
                    )
                    self._set_busy(False)
                elif kind == "done":
                    _, result, output_root = event
                    self._set_busy(False)
                    stopped = self.stop_event.is_set()
                    self.status_var.set("已停止" if stopped else "解包完成")
                    self._log(
                        "处理结束："
                        + ", ".join(
                            f"{key}={value:,}"
                            for key, value in sorted(result.items())
                        )
                    )
                    if not stopped:
                        messagebox.showinfo(
                            APP_TITLE,
                            f"解包完成。\n输出目录：{output_root}",
                        )
                elif kind == "error":
                    self._set_busy(False)
                    self.status_var.set("发生错误")
                    self._log(event[1])
                    messagebox.showerror(
                        APP_TITLE,
                        "处理失败，详细信息已写入日志区域。",
                    )
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)


def self_test(folder: Path) -> int:
    print(f"[self-test] folder={folder}")
    groups = discover_groups(folder)
    if not groups:
        print("[self-test] 未发现 IDX")
        return 2

    load_crypto()
    overall_bad = 0
    for group in groups:
        print(
            f"[self-test] {group.idx_path.name}: "
            f"records={len(group.records)}, active={group.active_count}, "
            f"packages={sorted(group.packages)}, "
            f"invalid_ranges={group.invalid_ranges}"
        )
        heads = Counter()
        tested = 0
        handles = {
            package_id: path.open("rb")
            for package_id, path in group.packages.items()
        }
        try:
            for record in group.records:
                valid, _ = validate_record_range(group, record)
                if not valid:
                    continue
                handle = handles[record.package_id]
                handle.seek(record.offset)
                blob = handle.read(min(record.stored_size, 4096))
                try:
                    decoded, _ = decode_stage1(blob, record.key_length)
                    heads[decoded[:4]] += 1
                except Exception:
                    heads[b"FAIL"] += 1
                    overall_bad += 1
                tested += 1
                if tested >= 300:
                    break
        finally:
            for handle in handles.values():
                handle.close()

        print(
            "[self-test] decoded heads:",
            ", ".join(
                f"{key!r}={value}" for key, value in heads.most_common(10)
            ),
        )

    return 0 if overall_bad < 20 else 1


def main() -> int:
    if "--self-test" in sys.argv:
        index = sys.argv.index("--self-test")
        if index + 1 < len(sys.argv) and not sys.argv[index + 1].startswith("-"):
            folder = Path(sys.argv[index + 1]).expanduser().resolve()
        else:
            folder = Path(__file__).resolve().parent
        return self_test(folder)

    root = tk.Tk()
    WpkGui(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
