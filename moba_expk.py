"""决战平安京 Android EXPK/NPK 资源读取与增量解包。

当前移动端 APK 的建模资源位于 assets/hero*.npk 与 assets/res.npk。
这些包使用 EXPK：28 字节索引和包级流加密；条目本身仍使用 NeoX
flags 指示二次加密与 zlib/LZ4 压缩。
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


EXPK_MAGIC = b"EXPK"
NXPK_MAGIC = b"NXPK"
HEADER_SIZE = 24
ENTRY_SIZE = 28
EXTRACTOR_VERSION = 1
MESH_MAGIC = b"\x34\x80\xC8\xBB"
KTX1_MAGIC = b"\xABKTX 11\xBB\r\n\x1A\n"
KTX2_MAGIC = b"\xABKTX 20\xBB\r\n\x1A\n"

# NetEase MOBA EXPK 的 256-byte 初始置换表。这里只作为格式常量使用。
_MOBA_KEY_SEED = bytes.fromhex(
    "48 5A C5 FD 8F 70 A6 DD 1C 6F B8 86 83 78 B7 F7 "
    "F2 B4 76 7F AB 5C 40 84 CC F8 60 9C 12 5B 80 15 "
    "72 9D 99 42 92 39 D3 BA A7 C4 A9 C7 D4 47 E3 31 "
    "43 EC 20 B3 4C 14 04 D8 A4 8D 73 19 F3 D7 79 36 "
    "F1 2D FB 68 F6 8E AF A0 E4 9B 2E 49 53 B2 65 3B "
    "0A 3A C8 54 ED 00 B5 1D EA 7B 24 71 82 C9 26 95 "
    "56 5F B1 17 74 44 BB 52 F4 21 AC 96 05 1A 10 9E "
    "D9 FF 64 C3 4A 62 E2 50 97 CA A1 6A 27 BD 6D 5D "
    "F5 A8 32 0F 9F 07 FC CB 8B 4B 37 55 0D 41 CE B6 "
    "3E 34 8A 18 13 BC 87 58 46 28 5E 2B EB 63 23 DE "
    "30 8C A5 06 02 57 DA 98 7A 93 38 03 E1 66 E7 F0 "
    "35 D1 6B DB 08 E6 CD 59 01 EE 7C 88 33 D2 FA 25 "
    "89 D0 0C 3D AA DC D6 C6 DF E0 4F 3F 1F 77 A2 75 "
    "B0 E8 94 AD 7D 6C C2 22 F9 BE BF 0B C1 1B 69 EF "
    "29 3C E9 C0 61 E5 6E 2F 9A 51 D5 11 67 16 CF 1E "
    "AE 4E 0E 81 45 2A 91 90 FE A3 09 2C 85 4D B9 7E"
)


@dataclass(frozen=True, slots=True)
class ExpHeader:
    magic: bytes
    file_count: int
    var1: int
    var2: int
    var3: int
    index_offset: int


@dataclass(frozen=True, slots=True)
class ExpEntry:
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


def _keystream(length: int) -> bytes:
    state = list(_MOBA_KEY_SEED)
    result = bytearray(length)
    i = 0
    j = 0
    for position in range(length):
        i = (i + 1) & 0xFF
        old = state[i]
        j = (j + old) & 0xFF
        state[i] = state[j]
        state[j] = old
        result[position] = state[(state[i] + old) & 0xFF]
    return bytes(result)


_KEYSTREAM_CACHE = bytearray()


def stream_decrypt(data: bytes) -> bytes:
    global _KEYSTREAM_CACHE
    if len(_KEYSTREAM_CACHE) < len(data):
        _KEYSTREAM_CACHE = bytearray(_keystream(max(len(data), 2_000_000)))
    return bytes(value ^ _KEYSTREAM_CACHE[index] for index, value in enumerate(data))


def locate_moba_npk_root(selected: Path) -> Path | None:
    selected = selected.resolve()
    if selected.is_file():
        selected = selected.parent
    candidates = [
        selected,
        selected / "npk",
        selected / "com.netease.moba" / "npk",
        selected / "moba" / "com.netease.moba" / "npk",
    ]
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.glob("hero*.npk")):
            return candidate.resolve()
    try:
        for path in selected.glob("**/npk/hero1.npk"):
            return path.parent.resolve()
    except OSError:
        pass
    return None


def archive_names(root: Path, include_textures: bool = True) -> list[str]:
    names = sorted(
        (path.name for path in root.glob("hero*.npk") if path.is_file()),
        key=lambda name: (
            int(re.search(r"(\d+)", name).group(1))
            if re.search(r"(\d+)", name) else 999,
            name.lower(),
        ),
    )
    if include_textures and (root / "res.npk").is_file():
        names.append("res.npk")
    return names


def read_index(path: Path) -> tuple[ExpHeader, list[ExpEntry]]:
    path = path.resolve()
    with path.open("rb") as stream:
        raw = stream.read(HEADER_SIZE)
        if len(raw) != HEADER_SIZE:
            raise ValueError(f"{path.name}: EXPK header truncated")
        magic, count, var1, var2, var3, index_offset = struct.unpack("<4s5I", raw)
        if magic not in {EXPK_MAGIC, NXPK_MAGIC}:
            raise ValueError(f"{path.name}: not EXPK/NXPK")
        file_size = path.stat().st_size
        table_size = count * ENTRY_SIZE
        if count <= 0 or index_offset < HEADER_SIZE or index_offset + table_size > file_size:
            raise ValueError(f"{path.name}: invalid EXPK index range")
        stream.seek(index_offset)
        table = stream.read(table_size)
    if magic == EXPK_MAGIC:
        table = stream_decrypt(table)
    entries: list[ExpEntry] = []
    for index in range(count):
        signature, offset, packed, raw_size, zcrc, crc, flags = struct.unpack_from(
            "<7I", table, index * ENTRY_SIZE
        )
        if offset < HEADER_SIZE or packed > index_offset or offset + packed > index_offset:
            raise ValueError(f"{path.name}: entry {index} outside data area")
        entries.append(ExpEntry(index, signature, offset, packed, raw_size, zcrc, crc, flags))
    order = {
        entry.table_index: rank
        for rank, entry in enumerate(sorted(entries, key=lambda item: item.offset))
    }
    entries = [
        ExpEntry(
            item.table_index, item.signature, item.offset, item.packed_size,
            item.raw_size, item.packed_crc, item.raw_crc, item.flags,
            order[item.table_index],
        )
        for item in entries
    ]
    return ExpHeader(magic, count, var1, var2, var3, index_offset), entries


def _advanced_xor(data: bytes, entry: ExpEntry) -> bytes:
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


def _incremental_xor(data: bytes, entry: ExpEntry) -> bytes:
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


def decode_entry(packed: bytes, entry: ExpEntry, *, expk: bool = True) -> bytes:
    if expk:
        packed = stream_decrypt(packed)
    encryption = entry.encryption
    if encryption == 3:
        packed = _advanced_xor(packed, entry)
    elif encryption == 4:
        packed = _incremental_xor(packed, entry)
    elif encryption not in {0}:
        raise ValueError(f"unsupported EXPK encryption mode {encryption}")

    compression = entry.compression
    if compression == 0 or entry.packed_size == entry.raw_size:
        decoded = packed
    elif compression == 1:
        decoded = zlib.decompress(packed)
    elif compression in {2, 5}:
        try:
            import lz4.block
        except ImportError as exc:
            raise RuntimeError("LZ4 EXPK entry requires package 'lz4'") from exc
        decoded = lz4.block.decompress(packed, uncompressed_size=entry.raw_size)
    else:
        raise ValueError(f"unsupported EXPK compression mode {compression}")
    if entry.raw_size and len(decoded) != entry.raw_size:
        raise ValueError(
            f"entry {entry.table_index}: decoded {len(decoded)} != {entry.raw_size}"
        )
    return decoded


def read_entry(path: Path, header: ExpHeader, entry: ExpEntry) -> bytes:
    with path.open("rb") as stream:
        stream.seek(entry.offset)
        packed = stream.read(entry.packed_size)
    if len(packed) != entry.packed_size:
        raise ValueError(f"{path.name}: entry {entry.table_index} truncated")
    return decode_entry(packed, entry, expk=header.magic == EXPK_MAGIC)


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
    if data.startswith(b"PKM"):
        return "pkm"
    if data.startswith(b"PVR"):
        return "pvr"
    probe = data[:8192].lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    if probe.startswith(b"<") or b"<?xml" in probe[:256]:
        return "xml"
    if probe.startswith(b"{") or probe.startswith(b"["):
        return "json"
    return "bin"


def _semantic_label(data: bytes, extension: str) -> str:
    if extension != "xml":
        return ""
    text = data[:256_000].decode("utf-8", "ignore").replace("\\", "/")
    candidates = re.findall(
        r"(?:Mesh|Value)=[\"']([^\"']+)[\"']|>\s*([^<>]+\.(?:gis|gim|mesh))\s*<",
        text,
        re.I,
    )
    for pair in candidates:
        candidate = next((value for value in pair if value), "").strip()
        if not candidate:
            continue
        stem = Path(candidate).stem
        stem = re.sub(r"[^0-9A-Za-z_\-]+", "_", stem).strip("_")
        if stem:
            return stem[:64]
    return ""


def _image_hash(data: bytes, extension: str) -> str:
    if extension not in {"ktx", "dds", "png", "jpg"}:
        return ""
    try:
        from onmyoji_npk import image_difference_hash
        return image_difference_hash(data, extension)
    except Exception:
        return ""


_U32_MASK = 0xFFFFFFFF
_MURMUR_C1 = 0xCC9E2D51
_MURMUR_C2 = 0x1B873593
_MURMUR_ADD = 0xE6546B64
_MURMUR_F1 = 0x85EBCA6B
_MURMUR_F2 = 0xC2B2AE35


def _rotl32(value: int, bits: int) -> int:
    value &= _U32_MASK
    return ((value << bits) | (value >> (32 - bits))) & _U32_MASK


def _rotr32(value: int, bits: int) -> int:
    value &= _U32_MASK
    return ((value >> bits) | (value << (32 - bits))) & _U32_MASK


def _murmur_mix_k(block: int) -> int:
    value = (block * _MURMUR_C1) & _U32_MASK
    value = _rotl32(value, 15)
    return (value * _MURMUR_C2) & _U32_MASK


def _undo_xor_shift_right(value: int, shift: int) -> int:
    result = value & _U32_MASK
    # Fixed-point iteration converges after ceil(32 / shift) rounds.
    for _ in range(6):
        result = (value ^ (result >> shift)) & _U32_MASK
    return result


def murmur3_path_hash(value: str, seed: int) -> int:
    """NeoX 28-byte NPK path hash (MurmurHash3 x86_32)."""
    data = value.encode("ascii")
    h = seed & _U32_MASK
    full = len(data) // 4 * 4
    for offset in range(0, full, 4):
        block = struct.unpack_from("<I", data, offset)[0]
        h ^= _murmur_mix_k(block)
        h = _rotl32(h, 13)
        h = (h * 5 + _MURMUR_ADD) & _U32_MASK
    tail = data[full:]
    if tail:
        block = 0
        for index, byte in enumerate(tail):
            block |= byte << (8 * index)
        h ^= _murmur_mix_k(block)
    h ^= len(data)
    h ^= h >> 16
    h = (h * _MURMUR_F1) & _U32_MASK
    h ^= h >> 13
    h = (h * _MURMUR_F2) & _U32_MASK
    h ^= h >> 16
    return h & _U32_MASK


def recover_murmur3_seed(value: str, target_hash: int) -> int:
    """Invert NeoX's MurmurHash3 path hash and recover its 32-bit seed."""
    data = value.encode("ascii")
    h = _undo_xor_shift_right(target_hash, 16)
    h = (h * pow(_MURMUR_F2, -1, 1 << 32)) & _U32_MASK
    h = _undo_xor_shift_right(h, 13)
    h = (h * pow(_MURMUR_F1, -1, 1 << 32)) & _U32_MASK
    h = _undo_xor_shift_right(h, 16)
    h ^= len(data)

    full = len(data) // 4 * 4
    tail = data[full:]
    if tail:
        block = 0
        for index, byte in enumerate(tail):
            block |= byte << (8 * index)
        h ^= _murmur_mix_k(block)

    inv5 = pow(5, -1, 1 << 32)
    blocks = [
        struct.unpack_from("<I", data, offset)[0]
        for offset in range(0, full, 4)
    ]
    for block in reversed(blocks):
        h = ((h - _MURMUR_ADD) * inv5) & _U32_MASK
        h = _rotr32(h, 13)
        h ^= _murmur_mix_k(block)
    return h & _U32_MASK


def npk_hash2(value: str) -> int:
    """Legacy NeoX NPK path hash used by public keytool implementations.

    This is intentionally kept separate from the experimental Murmur helper:
    current Arena EXPK signatures can be checked against both without assuming
    that every NeoX generation used the same filename hash.
    """
    data = value.encode("utf-8") + b"\0"

    def rol(value: int, bits: int) -> int:
        value &= _U32_MASK
        return ((value << bits) | (value >> (32 - bits))) & _U32_MASK

    def ror(value: int, bits: int) -> int:
        value &= _U32_MASK
        return ((value >> bits) | (value << (32 - bits))) & _U32_MASK

    def mul32(a: int, b: int) -> tuple[int, int, int]:
        product = (a & _U32_MASK) * (b & _U32_MASK)
        low = product & _U32_MASK
        high = (product >> 32) & _U32_MASK
        return low, high, int(high != 0)

    def adc32(a: int, b: int, carry: int) -> tuple[int, int]:
        result = (a & _U32_MASK) + (b & _U32_MASK) + carry
        return result & _U32_MASK, int(result > _U32_MASK)

    def add32(a: int, b: int) -> tuple[int, int]:
        result = (a & _U32_MASK) + (b & _U32_MASK)
        return result & _U32_MASK, int(result > _U32_MASK)

    def mix(ecx: int, edi: int, ebx: int) -> tuple[int, int]:
        edx = (ebx + edi) & _U32_MASK
        edx = (edx | 0x02040801) & 0xBFEF7FDF
        eax, high, carry = mul32(ecx, edx)
        eax, carry = adc32(eax, high, carry)
        eax, carry = adc32(eax, 0, carry)
        edx = (ebx + ecx) & _U32_MASK
        edx = (edx | 0x00804021) & 0x7DFEFBFF
        low, high, carry = mul32(edi, edx)
        doubled_high, carry = add32(high, high)
        eax2, carry = adc32(low, doubled_high, carry)
        if carry:
            eax2 = (eax2 + 2) & _U32_MASK
        return eax, eax2

    ebp = 0xF4FA8928
    ebx = 0
    ecx = 0x37A8470E
    edi = 0x7758B42B
    eax = 0
    pos = 0

    # Full 4-byte words.  The reference implementation stops as soon as a NUL
    # enters the partially assembled dword and then runs two final avalanche
    # rounds, so build the same little-endian word explicitly.
    while True:
        ebx = 0x267B0B11
        ebp = rol(ebp, 1)
        ebx ^= ebp
        word = 0
        partial = False
        for shift in (0, 8, 16, 24):
            byte = data[pos]
            pos += 1
            if byte == 0:
                partial = True
                break
            word |= byte << shift
        eax = word & _U32_MASK
        if partial:
            break
        ecx ^= eax
        edi ^= eax
        ecx, edi = mix(ecx, edi, ebx)

    ecx ^= eax
    edi ^= eax
    ecx, edi = mix(ecx, edi, ebx)

    ebx = 0x267B0B11
    ebp = rol(ebp, 1)
    ebx ^= ebp
    ecx ^= 0x9BE74448
    edi ^= 0x9BE74448
    ecx, edi = mix(ecx, edi, ebx)

    ebx = 0x267B0B11
    ebp = rol(ebp, 1)
    ebx ^= ebp
    ecx ^= 0x66F42C48
    edi ^= 0x66F42C48
    ecx, edi = mix(ecx, edi, ebx)
    return (edi ^ ecx) & _U32_MASK


def _arena_path_hash_variants(reference: str) -> tuple[str, ...]:
    normalized = reference.strip().replace("\\", "/").lstrip("/")
    values: list[str] = []
    for base in (normalized, normalized.lower()):
        candidates = (
            base,
            base.replace("/", "\\"),
            "res/" + base,
            "res\\" + base.replace("/", "\\"),
        )
        for candidate in candidates:
            if candidate not in values:
                values.append(candidate)
    return tuple(values)


def discover_arena_path_hash_seeds(
    model_folder: Path,
    *,
    minimum_references: int = 3,
    limit: int = 20,
) -> list[tuple[int, int, list[tuple[str, int, str]]]]:
    """Infer Arena's NPK path-hash seed from decoded MaterialGroup references.

    For a fixed path MurmurHash3 is bijective in the 32-bit seed.  Every
    logical texture reference can therefore be paired with every KTX signature
    to recover one candidate seed.  The real game seed is the value repeated
    independently across many distinct references.
    """
    model_folder = model_folder.resolve()
    manifest = model_folder / "npk_manifest.json"
    if not manifest.is_file():
        return []
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        rows = [ExtractedResource(**item) for item in payload.get("resources", ())]
    except (OSError, ValueError, TypeError, KeyError):
        return []

    by_archive: dict[str, list[ExtractedResource]] = {}
    for item in rows:
        if item.archive.lower().startswith("hero"):
            by_archive.setdefault(item.archive.lower(), []).append(item)

    hits: dict[int, dict[str, tuple[str, int, str]]] = {}
    texture_pattern = re.compile(
        r"(?:Value\s*=\s*[\"'])(hero[\\/][^\"']+\.(?:tga|png|dds|jpg|jpeg|bmp|ktx))",
        re.IGNORECASE,
    )
    for archive, archive_rows in by_archive.items():
        signatures = [
            item.signature
            for item in archive_rows
            if Path(item.relative_path).suffix.lower()
            in {".ktx", ".dds", ".png", ".jpg", ".jpeg", ".bmp"}
        ]
        if not signatures:
            continue
        references: set[str] = set()
        for item in archive_rows:
            if Path(item.relative_path).suffix.lower() != ".xml":
                continue
            path = model_folder / item.relative_path
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            text = ""
            for encoding in ("utf-8", "gb18030"):
                try:
                    text = raw.decode(encoding)
                    break
                except UnicodeError:
                    continue
            if not text:
                continue
            references.update(match.group(1) for match in texture_pattern.finditer(text))

        for reference in references:
            for variant in _arena_path_hash_variants(reference):
                try:
                    variant.encode("ascii")
                except UnicodeEncodeError:
                    continue
                for signature in signatures:
                    seed = recover_murmur3_seed(variant, signature)
                    by_reference = hits.setdefault(seed, {})
                    by_reference.setdefault(
                        reference.lower().replace("\\", "/"),
                        (variant, signature, archive),
                    )

    ranked = [
        (seed, len(by_reference), list(by_reference.values()))
        for seed, by_reference in hits.items()
        if len(by_reference) >= minimum_references
    ]
    ranked.sort(key=lambda row: (-row[1], row[0]))
    return ranked[:limit]


def archive_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{path.name.lower()}:{stat.st_size}:{stat.st_mtime_ns}"


def source_fingerprint(root: Path, include_textures: bool = True) -> str:
    root = locate_moba_npk_root(root) or root.resolve()
    payload = [f"moba-expk:{EXTRACTOR_VERSION}"]
    for name in archive_names(root, include_textures):
        payload.append(archive_fingerprint(root / name))
    return hashlib.sha256("\n".join(payload).encode("utf-8")).hexdigest()


def _manifest_path(cache_root: Path, include_textures: bool) -> Path:
    # 与现有 NPK 材质恢复器共用清单字段和文件名，但缓存目录完全独立。
    return cache_root / "model" / (
        "npk_manifest.json" if include_textures else "npk_manifest_models.json"
    )


def _load_manifest(path: Path) -> tuple[str, list[ExtractedResource]]:
    if not path.is_file():
        return "", []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = [ExtractedResource(**item) for item in payload.get("resources", [])]
        return str(payload.get("source_fingerprint", "")), rows
    except (OSError, ValueError, TypeError):
        return "", []


def _write_manifest(path: Path, fingerprint: str, rows: Iterable[ExtractedResource]) -> None:
    resources = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": "Onmyoji Arena Android EXPK",
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
            for item in resources
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with path.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow([
            "archive", "table_index", "physical_order", "signature_hex",
            "extension", "relative_path", "raw_size", "content_md5",
            "semantic_label", "image_hash",
        ])
        for item in resources:
            writer.writerow([
                item.archive, item.table_index, item.physical_order,
                f"{item.signature:08x}", item.extension, item.relative_path,
                item.raw_size, item.content_md5,
                item.semantic_label, item.image_hash,
            ])


def extract_resources(
    root: Path,
    cache_root: Path,
    *,
    include_textures: bool,
    log: Callable[[str], None] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> tuple[Path, list[ExtractedResource]]:
    root = locate_moba_npk_root(root) or root.resolve()
    names = archive_names(root, include_textures)
    if not names:
        raise ValueError("没有找到 hero*.npk；请先运行决战平安京拉取更新")
    cache_root = cache_root.resolve()
    model_root = cache_root / "model"
    manifest_path = _manifest_path(cache_root, include_textures)
    fingerprint = source_fingerprint(root, include_textures)
    old_fingerprint, old_rows = _load_manifest(manifest_path)
    if old_fingerprint == fingerprint and old_rows and all(
        (model_root / item.relative_path).is_file() for item in old_rows
    ):
        if log:
            log(f"平安京 EXPK 缓存一致，复用 {len(old_rows):,} 个资源。")
        return model_root, old_rows

    # 建模链只保存后续需要的资源；其余脚本、声音等不落盘。
    keep = {"mesh", "xml"}
    if include_textures:
        keep.update({"ktx", "dds", "png", "jpg", "pkm", "pvr"})

    rows: list[ExtractedResource] = []
    for archive_number, name in enumerate(names, 1):
        path = root / name
        header, entries = read_index(path)
        if log:
            log(
                f"读取 {name}: {header.file_count:,} 项，"
                f"格式 {header.magic.decode('ascii', 'replace')}。"
            )
        archive_dir = model_root / Path(name).stem
        archive_dir.mkdir(parents=True, exist_ok=True)
        total = len(entries)
        saved = failed = 0
        for number, entry in enumerate(entries, 1):
            try:
                data = read_entry(path, header, entry)
                extension = detect_extension(data)
                if extension not in keep:
                    if progress and (number % 100 == 0 or number == total):
                        progress(name, number, total)
                    continue
                digest = hashlib.md5(data).hexdigest()
                filename = (
                    f"{entry.physical_order:06d}_{entry.signature:08x}.{extension}"
                )
                target = archive_dir / filename
                if not target.is_file() or target.stat().st_size != len(data):
                    target.write_bytes(data)
                relative = target.relative_to(model_root).as_posix()
                rows.append(
                    ExtractedResource(
                        name, entry.table_index, entry.physical_order,
                        entry.signature, extension, relative, len(data), digest,
                        _semantic_label(data, extension),
                        _image_hash(data, extension),
                    )
                )
                saved += 1
            except Exception as exc:
                failed += 1
                if log and failed <= 20:
                    log(f"[{name} #{entry.table_index} 解码失败] {type(exc).__name__}: {exc}")
            if progress and (number % 25 == 0 or number == total):
                progress(name, number, total)
        if log:
            log(
                f"{name} 完成：保存建模相关资源 {saved:,}，"
                f"解码失败 {failed:,}。"
            )
        if progress:
            progress(name, total, total)

    _write_manifest(manifest_path, fingerprint, rows)
    return model_root, rows
