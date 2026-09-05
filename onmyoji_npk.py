"""Old PC Onmyoji NXPK reader and incremental resource extractor.

The desktop client uses NeoX 2 archives with 32-byte index records and a
game-specific basic-XOR key (150).  NPK archives do not necessarily carry an
NXFN filename table, so extracted files retain both their physical order and
64-bit resource signature.  Semantic labels found inside XML are added only
as display aids; they are never treated as reconstructed archive paths.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


NPK_MAGIC = b"NXPK"
ONMYOJI_XOR_KEY = 150
MODEL_ARCHIVES = ("model1.npk", "model2.npk", "qmodel.npk")
TEXTURE_ARCHIVES = ("tex_res.npk",)
EXTRACTOR_VERSION = 3
MESH_MAGIC = b"\x34\x80\xC8\xBB"
KTX1_MAGIC = b"\xABKTX 11\xBB\r\n\x1A\n"
KTX2_MAGIC = b"\xABKTX 20\xBB\r\n\x1A\n"


@dataclass(frozen=True, slots=True)
class NpkHeader:
    file_count: int
    var1: int
    encrypt_mode: int
    hash_mode: int
    index_offset: int
    info_size: int


@dataclass(frozen=True, slots=True)
class NpkEntry:
    table_index: int
    signature: int
    offset: int
    packed_size: int
    raw_size: int
    packed_crc: int
    raw_crc: int
    flags: int
    physical_order: int = -1

    @property
    def compression(self) -> int:
        return self.flags & 0xFFFF

    @property
    def encryption(self) -> int:
        return (self.flags >> 16) & 0xFF

    @property
    def encryption_raw(self) -> int:
        return self.flags >> 16


@dataclass(frozen=True, slots=True)
class ExtractedResource:
    archive: str
    table_index: int
    physical_order: int
    signature: int
    extension: str
    relative_path: str
    raw_size: int
    content_md5: str
    semantic_label: str = ""
    image_hash: str = ""


def locate_old_npk_root(selected: Path) -> Path | None:
    """Accept an install root, an individual NPK, or a nearby parent folder."""
    selected = selected.resolve()
    if selected.is_file():
        selected = selected.parent
    candidates = [selected]
    for relative in (Path("Onmyoji"), Path("res"), Path("game")):
        candidates.append(selected / relative)
    for candidate in candidates:
        if any((candidate / name).is_file() for name in MODEL_ARCHIVES) and (
            candidate / "tex_res.npk"
        ).is_file():
            return candidate.resolve()
    return None


def is_old_npk_root(selected: Path) -> bool:
    return locate_old_npk_root(selected) is not None


def _entry_size(path: Path, count: int, var1: int, index_offset: int) -> int:
    file_size = path.stat().st_size
    if count <= 0:
        return 32
    if var1 == 1:
        # NeoX 2 writes the 32-byte table at EOF and may leave the 32-bit header
        # offset truncated for archives larger than 4 GiB.
        return 32
    trailing = file_size - index_offset
    if trailing > 0 and trailing % count == 0:
        size = trailing // count
        if size in {28, 32, 40}:
            return size
    for size in (32, 28, 40):
        if index_offset + count * size <= file_size:
            return size
    raise ValueError(f"{path.name}: cannot determine NPK index record size")


def read_npk_index(path: Path) -> tuple[NpkHeader, list[NpkEntry]]:
    path = path.resolve()
    with path.open("rb") as stream:
        raw_header = stream.read(24)
        if len(raw_header) != 24:
            raise ValueError(f"{path.name}: truncated NPK header")
        magic, count, var1, encrypt_mode, hash_mode, index_offset = struct.unpack(
            "<4s5I", raw_header
        )
        if magic != NPK_MAGIC:
            raise ValueError(f"{path.name}: not an NXPK archive")
        info_size = _entry_size(path, count, var1, index_offset)
        actual_offset = (
            path.stat().st_size - count * info_size if var1 == 1 else index_offset
        )
        if actual_offset < 24:
            raise ValueError(f"{path.name}: invalid NPK index offset")
        stream.seek(actual_offset)
        entries: list[NpkEntry] = []
        for table_index in range(count):
            raw = stream.read(info_size)
            if len(raw) != info_size:
                raise ValueError(f"{path.name}: truncated NPK index")
            if info_size == 28:
                signature, offset, packed, raw_size, zcrc, crc, flags = struct.unpack(
                    "<7I", raw
                )
            elif info_size == 32:
                signature, offset, packed, raw_size, zcrc, crc, flags = struct.unpack(
                    "<Q6I", raw
                )
            else:
                # Some NeoX 2 variants pad the 32-byte form to 40 bytes.
                signature, offset, packed, raw_size, zcrc, crc, flags = struct.unpack(
                    "<Q6I", raw[:32]
                )
            if offset < 24 or packed > actual_offset or offset + packed > actual_offset:
                raise ValueError(f"{path.name}: entry {table_index} points outside archive")
            entries.append(
                NpkEntry(
                    table_index, signature, offset, packed, raw_size, zcrc, crc, flags
                )
            )
    physical_rank = {
        item.table_index: rank
        for rank, item in enumerate(sorted(entries, key=lambda value: value.offset))
    }
    entries = [
        NpkEntry(
            item.table_index,
            item.signature,
            item.offset,
            item.packed_size,
            item.raw_size,
            item.packed_crc,
            item.raw_crc,
            item.flags,
            physical_rank[item.table_index],
        )
        for item in entries
    ]
    return (
        NpkHeader(count, var1, encrypt_mode, hash_mode, index_offset, info_size),
        entries,
    )


def _basic_xor(data: bytes, key: int = ONMYOJI_XOR_KEY) -> bytes:
    prefix = min(128, len(data))
    result = bytearray(data)
    for index in range(prefix):
        result[index] ^= (key + index) & 0xFF
    return bytes(result)


def _advanced_xor(data: bytes, entry: NpkEntry) -> bytes:
    result = bytearray(data)
    length = len(result)
    start = 0
    crypt_size = length
    if length > 128:
        start = (entry.raw_crc >> 1) % (length - 128)
        crypt_size = (2 * entry.raw_size) % 0x60 + 0x20
    key = (entry.raw_crc ^ entry.raw_size) & 0xFF
    for index in range(min(crypt_size, length - start)):
        result[start + index] ^= (key + index) & 0xFF
    return bytes(result)


def _incremental_xor(data: bytes, entry: NpkEntry) -> bytes:
    result = bytearray(data)
    length = len(result)
    start = 0
    crypt_size = length
    if length > 128:
        start = (entry.raw_size >> 1) % (length - 128)
        crypt_size = ((entry.raw_crc << 1) & 0xFFFFFFFF) % 0x60 + 0x20
    key = (entry.raw_size ^ entry.raw_crc) & 0xFF
    for index in range(min(crypt_size, length - start)):
        result[start + index] ^= key
        key = (key + 1) & 0xFF
    return bytes(result)


def decode_entry(packed: bytes, entry: NpkEntry) -> bytes:
    if entry.encryption_raw not in {0, 1, 2, 3, 4}:
        raise ValueError(
            f"unsupported secondary NeoX encode flag 0x{entry.encryption_raw:x}"
        )
    encryption = entry.encryption
    if encryption == 1:
        packed = _basic_xor(packed)
    elif encryption in {2, 3}:
        packed = _advanced_xor(packed, entry)
    elif encryption == 4:
        packed = _incremental_xor(packed, entry)
    elif encryption != 0:
        raise ValueError(f"unsupported NPK encryption mode {encryption}")

    compression = entry.compression
    if compression == 0 or entry.packed_size == entry.raw_size:
        decoded = packed
    elif compression == 1:
        decoded = zlib.decompress(packed)
    elif compression in {2, 5}:
        try:
            import lz4.block
        except ImportError as exc:
            raise RuntimeError("LZ4 NPK entry requires the lz4 package") from exc
        decoded = lz4.block.decompress(packed, uncompressed_size=entry.raw_size)
    else:
        raise ValueError(f"unsupported NPK compression mode {compression}")
    if entry.raw_size and len(decoded) != entry.raw_size:
        raise ValueError(
            f"entry {entry.table_index}: decoded size {len(decoded)} != {entry.raw_size}"
        )
    return decoded


def read_entry(path: Path, entry: NpkEntry) -> bytes:
    with path.open("rb") as stream:
        stream.seek(entry.offset)
        packed = stream.read(entry.packed_size)
    if len(packed) != entry.packed_size:
        raise ValueError(f"{path.name}: entry {entry.table_index} is truncated")
    return decode_entry(packed, entry)


def detect_extension(data: bytes) -> str:
    if data.startswith(MESH_MAGIC):
        return "mesh"
    if data.startswith(KTX1_MAGIC) or data.startswith(KTX2_MAGIC):
        return "ktx"
    if data.startswith(b"DDS "):
        return "dds"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:2] == b"BM":
        return "bmp"
    probe = data[:4096].lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    if (
        probe.startswith(b"<?xml")
        or probe.startswith(b"<NeoX")
        or probe.startswith(b"<Material")
        or probe.startswith(b"<Model")
    ):
        return "xml"
    if b"<MaterialGroup" in probe or b"<GisFiles" in probe or b"<SubMesh" in probe:
        return "xml"
    return "bin"


def _safe_label(data: bytes, extension: str) -> str:
    if extension != "xml":
        return ""
    text = data[:256_000].decode("utf-8", "ignore").replace("\\", "/")
    candidates: list[str] = []
    candidates.extend(re.findall(r"(?:Mesh|Value)=[\"']([^\"']+)[\"']", text, re.I))
    candidates.extend(re.findall(r">\s*([^<>]+\.(?:gis|gim|mesh))\s*<", text, re.I))
    for candidate in candidates:
        normalized = candidate.strip().replace("\\", "/")
        if "/" not in normalized and Path(normalized).suffix.lower() not in {
            ".gis", ".gim", ".mesh", ".tga", ".png", ".dds", ".ktx"
        }:
            continue
        stem = Path(normalized).stem
        stem = re.sub(r"[^0-9A-Za-z_\-]+", "_", stem).strip("_")
        if stem:
            return stem[:64]
    return ""


def image_difference_hash(data: bytes, extension: str) -> str:
    """Return a compact visual identity used to pair low DDS with high KTX."""
    try:
        if extension == "ktx" and data.startswith(KTX1_MAGIC) and len(data) >= 68:
            fields = struct.unpack_from("<13I", data, 12)
            endian, gl_type, _type_size, gl_format = fields[:4]
            width, height = fields[6], fields[7]
            key_value_size = fields[12]
            data_offset = 64 + key_value_size
            image_size = struct.unpack_from("<I", data, data_offset)[0]
            pixels = data[data_offset + 4 : data_offset + 4 + image_size]
            if endian != 0x04030201 or gl_type != 0x1401 or gl_format not in {0x1907, 0x1908}:
                return ""
            channels = 4 if gl_format == 0x1908 else 3
            if width <= 0 or height <= 0 or len(pixels) < width * height * channels:
                return ""
            values: list[int] = []
            for y_index in range(8):
                y = min(height - 1, (y_index * height + height // 2) // 8)
                row: list[int] = []
                for x_index in range(9):
                    x = min(width - 1, (x_index * width + width // 2) // 9)
                    offset = (y * width + x) * channels
                    red, green, blue = pixels[offset : offset + 3]
                    row.append((299 * red + 587 * green + 114 * blue) // 1000)
                values.extend(row[index] > row[index + 1] for index in range(8))
        else:
            from PIL import Image

            with Image.open(io.BytesIO(data)) as source:
                image = source.convert("L")
                width, height = image.size
                sampled: list[int] = []
                for y_index in range(8):
                    y = min(height - 1, (y_index * height + height // 2) // 8)
                    sampled.extend(
                        image.getpixel((
                            min(width - 1, (x_index * width + width // 2) // 9),
                            y,
                        ))
                        for x_index in range(9)
                    )
            values = [
                sampled[row * 9 + column] > sampled[row * 9 + column + 1]
                for row in range(8)
                for column in range(8)
            ]
        result = 0
        for value in values:
            result = (result << 1) | int(value)
        return f"{result:016x}"
    except (OSError, ValueError, struct.error, ImportError):
        return ""


def archive_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{path.name.lower()}:{stat.st_size}:{stat.st_mtime_ns}"


def source_fingerprint(root: Path, include_textures: bool = True) -> str:
    names = list(MODEL_ARCHIVES)
    if include_textures:
        names.extend(TEXTURE_ARCHIVES)
    payload = [f"npk-extractor:{EXTRACTOR_VERSION}"]
    for name in names:
        path = root / name
        if path.is_file():
            payload.append(archive_fingerprint(path))
    return hashlib.sha256("\n".join(payload).encode("utf-8")).hexdigest()


def _load_manifest(path: Path) -> tuple[str, list[ExtractedResource]]:
    if not path.is_file():
        return "", []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = [ExtractedResource(**item) for item in payload.get("resources", [])]
        return str(payload.get("source_fingerprint", "")), rows
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return "", []


def _write_manifest(
    path: Path, fingerprint: str, resources: Iterable[ExtractedResource]
) -> None:
    rows = list(resources)
    payload = {
        "format": "Onmyoji PC NPK",
        "extractor_version": EXTRACTOR_VERSION,
        "source_fingerprint": fingerprint,
        "resources": [
            {
                "archive": item.archive,
                "table_index": item.table_index,
                "physical_order": item.physical_order,
                "signature": item.signature,
                "extension": item.extension,
                "relative_path": item.relative_path,
                "raw_size": item.raw_size,
                "content_md5": item.content_md5,
                "semantic_label": item.semantic_label,
                "image_hash": item.image_hash,
            }
            for item in rows
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_path = path.with_suffix(".csv")
    with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "archive", "table_index", "physical_order", "signature_hex",
                "extension", "relative_path", "raw_size", "content_md5",
                "semantic_label",
                "image_hash",
            ]
        )
        for item in rows:
            writer.writerow(
                [
                    item.archive, item.table_index, item.physical_order,
                    f"{item.signature:016x}", item.extension, item.relative_path,
                    item.raw_size, item.content_md5, item.semantic_label,
                    item.image_hash,
                ]
            )


def extract_resources(
    root: Path,
    cache_root: Path,
    *,
    include_textures: bool,
    log: Callable[[str], None] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> tuple[Path, list[ExtractedResource]]:
    """Incrementally extract model XML/Mesh and, optionally, model textures."""
    root = locate_old_npk_root(root) or root.resolve()
    if locate_old_npk_root(root) is None:
        raise ValueError("selected folder is not an old PC Onmyoji NPK install")
    cache_root = cache_root.resolve()
    model_root = cache_root / "model"
    manifest_path = model_root / (
        "npk_manifest.json" if include_textures else "npk_manifest_models.json"
    )
    fingerprint = source_fingerprint(root, include_textures)
    old_fingerprint, old_rows = _load_manifest(manifest_path)
    if old_fingerprint == fingerprint and old_rows and all(
        (model_root / item.relative_path).is_file() for item in old_rows
    ):
        if log:
            log(f"旧版 NPK 缓存与游戏文件一致，复用 {len(old_rows):,} 个资源。")
        return model_root, old_rows

    names = list(MODEL_ARCHIVES)
    if include_textures:
        names.extend(TEXTURE_ARCHIVES)
    existing = {
        (item.archive.lower(), item.table_index, item.content_md5): item
        for item in old_rows
        if (model_root / item.relative_path).is_file()
    }
    resources: list[ExtractedResource] = []
    model_root.mkdir(parents=True, exist_ok=True)
    for archive_name in names:
        archive_path = root / archive_name
        if not archive_path.is_file():
            if log:
                log(f"跳过缺失资源包：{archive_name}")
            continue
        _header, entries = read_npk_index(archive_path)
        if log:
            log(f"读取 {archive_name}：{len(entries):,} 个 NPK 条目。")
        skipped = 0
        skipped_examples: list[str] = []
        with archive_path.open("rb") as stream:
            # Read in payload order.  The index is signature-sorted and would
            # otherwise turn multi-gigabyte archives into tens of thousands of
            # random seeks on a mechanical disk.
            ordered_entries = sorted(entries, key=lambda item: item.offset)
            for number, entry in enumerate(ordered_entries, 1):
                stream.seek(entry.offset)
                packed = stream.read(entry.packed_size)
                try:
                    data = decode_entry(packed, entry)
                except Exception as exc:
                    skipped += 1
                    if len(skipped_examples) < 5:
                        skipped_examples.append(
                            f"#{entry.table_index} {type(exc).__name__}: {exc}"
                        )
                    if progress and (number % 100 == 0 or number == len(ordered_entries)):
                        progress(archive_name, number, len(ordered_entries))
                    continue
                extension = detect_extension(data)
                wanted = extension in {
                    "mesh", "xml", "ktx", "dds", "png", "jpg", "bmp"
                }
                if archive_name.lower() in TEXTURE_ARCHIVES:
                    wanted = extension in {"ktx", "dds", "png", "jpg", "bmp"}
                if wanted:
                    digest = hashlib.md5(data).hexdigest()
                    previous = existing.get((archive_name.lower(), entry.table_index, digest))
                    if previous is not None:
                        resources.append(previous)
                    else:
                        label = _safe_label(data, extension)
                        image_hash = (
                            image_difference_hash(data, extension)
                            if extension in {"ktx", "dds", "png", "jpg", "bmp"}
                            else ""
                        )
                        label_part = f"_{label}" if label else ""
                        folder = (
                            Path("textures") / f"{entry.signature:016x}"[:2]
                            if archive_name.lower() in TEXTURE_ARCHIVES
                            else Path(archive_path.stem)
                        )
                        filename = (
                            f"{entry.physical_order:06d}{label_part}_"
                            f"{entry.signature:016x}.{extension}"
                        )
                        relative = str(folder / filename)
                        target = model_root / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(data)
                        resources.append(
                            ExtractedResource(
                                archive_name,
                                entry.table_index,
                                entry.physical_order,
                                entry.signature,
                                extension,
                                relative,
                                len(data),
                                digest,
                                label,
                                image_hash,
                            )
                        )
                if progress and (number % 100 == 0 or number == len(ordered_entries)):
                    progress(archive_name, number, len(ordered_entries))
        if skipped and log:
            log(
                f"{archive_name}：有 {skipped:,} 个带二次编码或损坏的条目未导出；"
                + "；".join(skipped_examples)
            )
    resources.sort(key=lambda item: (item.archive.lower(), item.physical_order))
    _write_manifest(manifest_path, fingerprint, resources)
    if log:
        meshes = sum(item.extension == "mesh" for item in resources)
        textures = sum(item.extension in {"ktx", "dds", "png", "jpg", "bmp"} for item in resources)
        log(
            f"旧版 NPK 增量解包完成：Mesh {meshes:,}，贴图 {textures:,}，"
            f"清单 {manifest_path.name}。"
        )
    return model_root, resources
