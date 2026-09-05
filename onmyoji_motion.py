# -*- coding: utf-8 -*-
"""Onmyoji RAWANIMA v0 reader, ACL decoder bridge, and VMD exporter."""

from __future__ import annotations

import base64
import copy
import hashlib
import os
import struct
import subprocess
import threading
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from pathlib import Path

import numpy as np


_ANIMATION_XML_INDEX: dict[Path, dict[str, tuple[Path, ...]]] = {}
_ANIMATION_XML_INDEX_LOCK = threading.Lock()


class MotionFormatError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MotionHeader:
    path: Path
    version: int
    skeleton_ref: str
    action: str
    bone_names: tuple[str, ...]
    sample_rate: float
    duration: float

    @property
    def skeleton_name(self) -> str:
        return Path(self.skeleton_ref.replace("\\", "/")).stem


@dataclass(frozen=True, slots=True)
class AnimationMetadata:
    """Game-authored XML metadata associated with a RAWANIMA clip.

    Cached poses are deliberately exposed as clip data, not a bind-pose
    substitute.  Some clips cache a pose for seeking while their first ACL
    sample is a staged root-motion pose.
    """

    path: Path
    action: str
    joint_names: tuple[str, ...]
    properties: tuple[tuple[str, str], ...]
    cached_pose_times: tuple[float, ...]
    cached_poses: np.ndarray  # [pose, motion-bone, tx ty tz qx qy qz qw sx sy sz]

    @property
    def property_map(self) -> dict[str, str]:
        return dict(self.properties)


@dataclass(slots=True)
class DecodedMotion:
    header: MotionHeader
    frames: np.ndarray  # [sample, bone, tx ty tz qx qy qz qw sx sy sz]
    sample_rate: float
    duration: float
    has_scale: bool

    @property
    def sample_count(self) -> int:
        return int(self.frames.shape[0])


@dataclass(frozen=True, slots=True)
class MotionClipAlignment:
    """Verified game clip range embedded in a longer RAWANIMA stream."""

    start_frame: int
    end_frame: int
    cached_pose_score: float
    next_best_score: float

    @property
    def trimmed_frames(self) -> int:
        return self.end_frame - self.start_frame + 1


def _u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise MotionFormatError("动作文件被截断")
    return struct.unpack_from("<I", data, offset)[0]


def _parse_names(payload: bytes) -> tuple[str, ...]:
    count = _u32(payload, 0)
    if count == 0 or count > 65536:
        raise MotionFormatError(f"NAME 字符串数量异常：{count}")
    result: list[str] = []
    offset = 4
    for _ in range(count):
        length = _u32(payload, offset)
        offset += 4
        if length > len(payload) - offset:
            raise MotionFormatError("NAME 字符串越界")
        raw = payload[offset : offset + length]
        offset += length
        result.append(raw.decode("utf-8", errors="replace").strip().replace(" ", "_"))
    return tuple(result)


def read_motion_header(path: Path) -> MotionHeader:
    """Read metadata without decoding ACL samples."""
    path = Path(path)
    with path.open("rb") as stream:
        prefix = stream.read(512)
        if len(prefix) < 48 or prefix[:8] != b"RAWANIMA":
            raise MotionFormatError(f"{path.name}: 不是 RAWANIMA")
        version = _u32(prefix, 16)
        skeleton_length = _u32(prefix, 40)
        if skeleton_length > 4096 or 44 + skeleton_length > len(prefix):
            raise MotionFormatError(f"{path.name}: Skeleton 路径异常")
        skeleton_ref = prefix[44 : 44 + skeleton_length].decode(
            "utf-8", errors="replace"
        )
        head_at = prefix.find(b"HEAD", 44 + skeleton_length)
        if head_at < 0 or head_at + 16 > len(prefix):
            raise MotionFormatError(f"{path.name}: 缺少 HEAD")
        head_size = _u32(prefix, head_at + 4)
        if head_size < 8:
            raise MotionFormatError(f"{path.name}: HEAD 长度异常")
        sample_rate, duration = struct.unpack_from("<ff", prefix, head_at + 8)

        stream.seek(0, os.SEEK_END)
        file_size = stream.tell()
        tail_size = min(file_size, 512 * 1024)
        stream.seek(file_size - tail_size)
        tail = stream.read(tail_size)
    name_at = tail.rfind(b"NAME")
    if name_at < 0 or name_at + 8 > len(tail):
        raise MotionFormatError(f"{path.name}: 缺少 NAME")
    name_size = _u32(tail, name_at + 4)
    payload = tail[name_at + 8 : name_at + 8 + name_size]
    if len(payload) != name_size:
        raise MotionFormatError(f"{path.name}: NAME 数据不完整")
    names = _parse_names(payload)
    return MotionHeader(
        path=path.resolve(),
        version=version,
        skeleton_ref=skeleton_ref,
        action=names[0] or path.stem,
        bone_names=names[1:],
        sample_rate=float(sample_rate),
        duration=float(duration),
    )


def decoder_path() -> Path:
    return Path(__file__).resolve().parent / "tools" / "onmyoji_acl_decode.exe"


def decode_motion(path: Path, cache_root: Path | None = None) -> DecodedMotion:
    header = read_motion_header(path)
    if header.version != 0:
        raise MotionFormatError(
            f"{Path(path).name}: RAWANIMA v{header.version} 暂不支持（当前支持 v0）"
        )
    decoder = decoder_path()
    if not decoder.is_file():
        raise MotionFormatError("缺少 ACL 解码器：tools/onmyoji_acl_decode.exe")
    stat = Path(path).stat()
    key = hashlib.sha1(
        f"{Path(path).resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    ).hexdigest()
    cache_root = cache_root or (Path(__file__).resolve().parent / ".motion_cache")
    cache_root.mkdir(parents=True, exist_ok=True)
    decoded_path = cache_root / f"{key}.nanim"
    if not decoded_path.is_file():
        temporary = decoded_path.with_suffix(".tmp")
        completed = subprocess.run(
            [str(decoder), str(Path(path).resolve()), str(temporary)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode:
            temporary.unlink(missing_ok=True)
            detail = (completed.stderr or completed.stdout).strip()
            raise MotionFormatError(detail or f"ACL 解码失败（{completed.returncode}）")
        temporary.replace(decoded_path)
    raw = decoded_path.read_bytes()
    if len(raw) < 24 or raw[:8] != b"NANIM001":
        raise MotionFormatError("解码缓存格式异常")
    bone_count, flags, sample_count, sample_rate, duration = struct.unpack_from(
        "<HHIff", raw, 8
    )
    expected = 24 + bone_count * sample_count * 10 * 4
    if expected != len(raw):
        raise MotionFormatError(
            f"解码帧长度异常：应为 {expected}，实际 {len(raw)}"
        )
    if bone_count != len(header.bone_names):
        raise MotionFormatError(
            f"骨骼名称 {len(header.bone_names)} 个，ACL 轨道 {bone_count} 个"
        )
    frames = np.frombuffer(raw, dtype="<f4", offset=24).reshape(
        sample_count, bone_count, 10
    ).copy()
    return DecodedMotion(header, frames, sample_rate, duration, bool(flags & 1))


def normalized_bone_name(name: str) -> str:
    return "".join(ch for ch in name.strip().replace(" ", "_").lower() if ch not in "-_.")


def _xml_animation_candidate(path: Path, header: MotionHeader) -> AnimationMetadata | None:
    """Parse one animation XML when its action and *whole* joint set match."""
    try:
        root = ElementTree.parse(path).getroot()
        name_node = root.find("Name")
        cached = root.find("CachedPose")
        if name_node is None or cached is None:
            return None
        action = (name_node.get("Name") or "").strip()
        if normalized_bone_name(action) != normalized_bone_name(header.action):
            return None
        joint_node = cached.find("JointNames")
        joint_text = joint_node.get("Value") if joint_node is not None else None
        if not joint_text:
            return None
        joint_names = tuple(item.strip() for item in joint_text.split(",") if item.strip())
        source_keys = tuple(normalized_bone_name(item) for item in header.bone_names)
        cached_keys = tuple(normalized_bone_name(item) for item in joint_names)
        # A set match alone is unsafe for duplicate names.  Both resources use
        # unique joint names, so require a one-to-one complete collection.
        if len(cached_keys) != len(source_keys) or set(cached_keys) != set(source_keys):
            return None
        if len(set(cached_keys)) != len(cached_keys):
            return None
        property_node = root.find("Property")
        properties = tuple(sorted((property_node.attrib if property_node is not None else {}).items()))
        pose_times: list[float] = []
        poses: list[np.ndarray] = []
        expected_bytes = len(joint_names) * 10 * 4
        for pose in cached.findall("CachedPoseTrack/Pose"):
            encoded = pose.get("Value", "")
            raw = base64.b64decode(encoded, validate=True)
            if len(raw) != expected_bytes:
                return None
            pose_times.append(float(pose.get("Time", "nan")))
            poses.append(np.frombuffer(raw, dtype="<f4").reshape(len(joint_names), 10).copy())
        if not poses or not np.isfinite(pose_times).all():
            return None
        cached_by_key = {key: index for index, key in enumerate(cached_keys)}
        order = [cached_by_key[key] for key in source_keys]
        return AnimationMetadata(
            path=path.resolve(),
            action=action,
            joint_names=joint_names,
            properties=properties,
            cached_pose_times=tuple(pose_times),
            cached_poses=np.asarray(poses, dtype=np.float32)[:, order, :],
        )
    except (ElementTree.ParseError, OSError, ValueError, TypeError):
        return None


def _animation_xml_index(root: Path) -> dict[str, tuple[Path, ...]]:
    """Index XMLs once by the logical action embedded in their export name."""
    root = root.resolve()
    with _ANIMATION_XML_INDEX_LOCK:
        cached = _ANIMATION_XML_INDEX.get(root)
        if cached is not None:
            return cached
        grouped: dict[str, list[Path]] = {}
        for path in root.rglob("*_Animation_*.xml"):
            marker = path.name.lower().find("_animation_")
            if marker < 0:
                continue
            prefix = path.name[:marker]
            action = prefix.split("_", 1)[1] if "_" in prefix else prefix
            key = normalized_bone_name(action)
            if key:
                grouped.setdefault(key, []).append(path)
        cached = {key: tuple(value) for key, value in grouped.items()}
        _ANIMATION_XML_INDEX[root] = cached
        return cached


def find_animation_metadata(root: Path, header: MotionHeader) -> AnimationMetadata | None:
    """Find the authoritative XML by clip name and exact normalized joint set.

    Package hashes and nearby files are intentionally not used as identity:
    the game can place a clip and its XML in different package buckets.
    """
    root = Path(root)
    if not root.is_dir():
        return None
    candidates: list[AnimationMetadata] = []
    for path in _animation_xml_index(root).get(normalized_bone_name(header.action), ()):
        metadata = _xml_animation_candidate(path, header)
        if metadata is not None:
            candidates.append(metadata)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _metadata_clip_range(
    motion: DecodedMotion, metadata: AnimationMetadata
) -> tuple[int, int] | None:
    """Return the only frame range declared by the game XML, if exact enough."""
    source_keys = tuple(normalized_bone_name(name) for name in motion.header.bone_names)
    cached_keys = tuple(normalized_bone_name(name) for name in metadata.joint_names)
    if (
        len(cached_keys) != len(source_keys)
        or len(set(cached_keys)) != len(cached_keys)
        or set(cached_keys) != set(source_keys)
    ):
        return None
    if metadata.cached_poses.ndim != 3 or metadata.cached_poses.shape[1:] != motion.frames.shape[1:]:
        return None
    if len(metadata.cached_pose_times) != len(metadata.cached_poses):
        return None
    props = metadata.property_map
    try:
        start_time = float(props["StartTime"])
        end_time = float(props["EndTime"])
    except (KeyError, TypeError, ValueError):
        return None
    duration = end_time - start_time
    if not (np.isfinite(duration) and duration > 0.0 and motion.sample_rate > 0.0):
        return None
    # Animation streams hold both endpoints, so a 4.2 second 30 FPS clip has
    # 127 samples.  A half-frame allowance accommodates decimal XML exports.
    frame_count = int(round(duration * motion.sample_rate)) + 1
    if frame_count >= motion.sample_count or frame_count < len(metadata.cached_poses):
        return None
    actual_duration = (frame_count - 1) / motion.sample_rate
    if abs(actual_duration - duration) > 0.5001 / motion.sample_rate:
        return None
    offsets = np.rint(
        (np.asarray(metadata.cached_pose_times, dtype=np.float64) - start_time)
        * motion.sample_rate
    ).astype(np.int64)
    if np.any(offsets < 0) or np.any(offsets >= frame_count) or len(set(offsets.tolist())) != len(offsets):
        return None
    return motion.sample_count - frame_count, frame_count


def _cached_pose_alignment_scores(
    motion: DecodedMotion, metadata: AnimationMetadata, clip_frame_count: int
) -> np.ndarray | None:
    """Score every possible XML clip start against its author-cached poses."""
    props = metadata.property_map
    try:
        start_time = float(props["StartTime"])
    except (KeyError, TypeError, ValueError):
        return None
    pose_offsets = np.rint(
        (np.asarray(metadata.cached_pose_times, dtype=np.float64) - start_time)
        * motion.sample_rate
    ).astype(np.int64)
    starts = np.arange(motion.sample_count - clip_frame_count + 1, dtype=np.int64)
    scores = np.zeros(len(starts), dtype=np.float64)
    for cached, offset in zip(metadata.cached_poses, pose_offsets):
        samples = motion.frames[starts + offset]
        translation_error = np.mean((samples[:, :, :3] - cached[None, :, :3]) ** 2, axis=(1, 2))
        scale_error = np.mean((samples[:, :, 7:10] - cached[None, :, 7:10]) ** 2, axis=(1, 2))
        current_rotation = samples[:, :, 3:7] / np.maximum(
            np.linalg.norm(samples[:, :, 3:7], axis=2, keepdims=True), 1.0e-8
        )
        cached_rotation = cached[None, :, 3:7] / np.maximum(
            np.linalg.norm(cached[None, :, 3:7], axis=2, keepdims=True), 1.0e-8
        )
        rotation_error = np.mean(
            1.0 - np.abs(np.sum(current_rotation * cached_rotation, axis=2)), axis=1
        )
        scores += translation_error + rotation_error + scale_error
    return scores / len(metadata.cached_poses)


def trim_motion_to_animation_metadata(
    motion: DecodedMotion, metadata: AnimationMetadata | None
) -> tuple[DecodedMotion, MotionClipAlignment | None]:
    """Remove a game-authored RAWANIMA pre-roll only when independently proven.

    Some NeoX files contain a staging section before the clip consumed by the
    game.  XML declares the playable duration while CachedPose records its
    first samples.  Both signals are required here; a name match or duration
    match by itself must never alter a motion.
    """
    if metadata is None:
        return motion, None
    clip_range = _metadata_clip_range(motion, metadata)
    if clip_range is None:
        return motion, None
    expected_start, frame_count = clip_range
    scores = _cached_pose_alignment_scores(motion, metadata, frame_count)
    if scores is None or not len(scores) or not np.isfinite(scores).all():
        return motion, None
    best_start = int(np.argmin(scores))
    best_score = float(scores[best_start])
    alternatives = np.delete(scores, best_start)
    next_best = float(np.min(alternatives)) if len(alternatives) else float("inf")
    # Cached poses are quantized, so they need not round-trip bit-identically.
    # A clear global minimum at exactly the XML-derived start is still a strong
    # identity check.  The margin prevents repeated/static poses from trimming.
    if (
        best_start != expected_start
        or best_score > 0.02
        or not (next_best > best_score * 1.05 + 1.0e-7)
    ):
        return motion, None
    end_frame = expected_start + frame_count - 1
    trimmed = DecodedMotion(
        header=motion.header,
        frames=motion.frames[expected_start : end_frame + 1].copy(),
        sample_rate=motion.sample_rate,
        duration=(frame_count - 1) / motion.sample_rate,
        has_scale=motion.has_scale,
    )
    return trimmed, MotionClipAlignment(
        start_frame=expected_start,
        end_frame=end_frame,
        cached_pose_score=best_score,
        next_best_score=next_best,
    )


def matrix4_multiply(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """Multiply row-vector affine matrices (including leading dimensions)."""
    return np.matmul(np.asarray(left, dtype=np.float32), np.asarray(right, dtype=np.float32))


def inverse_affine_row_matrix4(matrix: np.ndarray) -> np.ndarray:
    """Invert row-vector affine 4x4 matrices without changing coordinates."""
    value = np.asarray(matrix, dtype=np.float32)
    linear_inverse = np.linalg.inv(value[..., :3, :3])
    result = np.zeros_like(value)
    result[..., :3, :3] = linear_inverse
    result[..., 3, :3] = -np.matmul(value[..., 3:4, :3], linear_inverse)[..., 0, :]
    result[..., 3, 3] = 1.0
    return result


def neox_to_pmx_matrix4(matrix: np.ndarray) -> np.ndarray:
    """Convert a NeoX row-vector matrix once, at the PMX boundary."""
    conversion = np.diag(np.asarray((-1.0, 1.0, -1.0, 1.0), dtype=np.float32))
    return matrix4_multiply(matrix4_multiply(conversion, matrix), conversion)


def trs_row_matrix4(transform: np.ndarray) -> np.ndarray:
    """Build a NeoX row-vector local matrix from tx/quat/scale ACL channels."""
    tx, ty, tz, x, y, z, w, sx, sy, sz = np.asarray(transform, dtype=np.float32)
    length = float(np.sqrt(x * x + y * y + z * z + w * w))
    if length <= 1.0e-8:
        x = y = z = 0.0
        w = 1.0
    else:
        x, y, z, w = x / length, y / length, z / length, w / length
    # Transpose of the usual column-vector quaternion matrix.  Translation is
    # in the final row because NeoX evaluates vertices as row vectors.
    rotation = np.asarray(
        (
            (1 - 2 * (y * y + z * z), 2 * (x * y + z * w), 2 * (x * z - y * w), 0),
            (2 * (x * y - z * w), 1 - 2 * (x * x + z * z), 2 * (y * z + x * w), 0),
            (2 * (x * z + y * w), 2 * (y * z - x * w), 1 - 2 * (x * x + y * y), 0),
            (tx, ty, tz, 1),
        ),
        dtype=np.float32,
    )
    rotation[0, :3] *= sx
    rotation[1, :3] *= sy
    rotation[2, :3] *= sz
    return rotation


def compose_global_row_matrices(
    local_transforms: np.ndarray, parents: tuple[int, ...] | list[int]
) -> np.ndarray:
    """Compose ACL local TRS in NeoX's row-vector parent order."""
    count = len(local_transforms)
    result = np.zeros((count, 4, 4), dtype=np.float32)
    visiting = np.zeros(count, dtype=np.uint8)

    def visit(index: int) -> None:
        if visiting[index] == 2:
            return
        visiting[index] = 1
        local = trs_row_matrix4(local_transforms[index])
        parent = int(parents[index]) if index < len(parents) else -1
        if 0 <= parent < count and parent != index and visiting[parent] != 1:
            visit(parent)
            result[index] = matrix4_multiply(local, result[parent])
        else:
            result[index] = local
        visiting[index] = 2

    for index in range(count):
        visit(index)
    return result


def cp932_field(value: str, size: int) -> bytes:
    raw = value.encode("cp932", errors="replace")
    while len(raw) > size:
        value = value[:-1]
        raw = value.encode("cp932", errors="replace")
    return raw.ljust(size, b"\0")


def _short_bone_names(names: list[str]) -> list[str]:
    result: list[str] = []
    used: set[bytes] = set()
    for index, name in enumerate(names):
        encoded = name.encode("cp932", errors="replace")
        candidate = name if 0 < len(encoded) <= 15 and encoded not in used else f"B{index:04d}"
        field = cp932_field(candidate, 15).rstrip(b"\0")
        if field in used:
            candidate = f"X{index:04d}"
            field = candidate.encode("ascii")
        used.add(field)
        result.append(candidate)
    return result


def _linear_interpolation() -> bytes:
    # Conventional linear Bezier control points used by MMD bone frames.
    block = bytes((20,) * 8 + (107,) * 8)
    return block * 4


def quaternion_multiply(lhs: np.ndarray, rhs: np.ndarray) -> np.ndarray:
    """Hamilton product for XYZW quaternions, vectorized over leading axes."""
    lhs = np.asarray(lhs, dtype=np.float32)
    rhs = np.asarray(rhs, dtype=np.float32)
    lx, ly, lz, lw = np.moveaxis(lhs, -1, 0)
    rx, ry, rz, rw = np.moveaxis(rhs, -1, 0)
    return np.stack(
        (
            lw * rx + lx * rw + ly * rz - lz * ry,
            lw * ry - lx * rz + ly * rw + lz * rx,
            lw * rz + lx * ry - ly * rx + lz * rw,
            lw * rw - lx * rx - ly * ry - lz * rz,
        ),
        axis=-1,
    )


def quaternion_delta(current: np.ndarray, reference: np.ndarray) -> np.ndarray:
    """Return the rotation that moves a reference orientation to current."""
    inverse_reference = np.asarray(reference, dtype=np.float32).copy()
    inverse_reference[..., :3] *= -1.0
    result = quaternion_multiply(current, inverse_reference)
    length = np.linalg.norm(result, axis=-1, keepdims=True)
    identity = np.zeros_like(result)
    identity[..., 3] = 1.0
    return np.where(length > 1.0e-8, result / np.maximum(length, 1.0e-8), identity)


def export_vmd(
    motion: DecodedMotion,
    pmx_path: Path,
    vmd_path: Path,
    output_fps: float = 30.0,
    reference_transforms: np.ndarray | None = None,
) -> tuple[Path, Path, int, int, bool]:
    """Export VMD and a name-compatible PMX copy.

    VMD bone names are limited to 15 CP932 bytes. Long names are replaced with
    deterministic aliases in a PMX copy; the original PMX remains untouched.
    """
    import pymeshio.pmx.reader
    import pymeshio.pmx.writer

    pmx_path = Path(pmx_path).resolve()
    vmd_path = Path(vmd_path).resolve()
    model = pymeshio.pmx.reader.read_from_file(str(pmx_path))
    pmx_names = [str(bone.name) for bone in model.bones]
    aliases = _short_bone_names(pmx_names)
    raw_by_key = {
        normalized_bone_name(name): index
        for index, name in enumerate(motion.header.bone_names)
    }
    mapping = [raw_by_key.get(normalized_bone_name(name), -1) for name in pmx_names]
    matched = sum(index >= 0 for index in mapping)
    if not matched:
        raise MotionFormatError("动作骨骼与所选 PMX 没有同名骨骼")

    if reference_transforms is None:
        reference_transforms = motion.frames[0]
    reference_transforms = np.asarray(reference_transforms, dtype=np.float32)
    if reference_transforms.shape != motion.frames.shape[1:]:
        raise MotionFormatError(
            "绑定姿势形状与动作骨骼不一致："
            f"{reference_transforms.shape} != {motion.frames.shape[1:]}"
        )

    compatible = copy.deepcopy(model)
    for bone, alias, original in zip(compatible.bones, aliases, pmx_names):
        bone.name = alias
        bone.english_name = original
    compatible.comment = (compatible.comment or "") + "\nVMD-compatible bone aliases; originals are in English names."
    compatible_path = pmx_path.with_name(pmx_path.stem + "_动作兼容.pmx")
    pymeshio.pmx.writer.write_to_file(compatible, str(compatible_path))

    frame_count = max(1, int(round(motion.duration * output_fps)) + 1)
    source_indices = np.clip(
        np.rint(np.arange(frame_count) * motion.sample_rate / output_fps).astype(np.int64),
        0,
        motion.sample_count - 1,
    )
    interpolation = _linear_interpolation()
    records: list[bytes] = []
    previous_rotations: dict[int, np.ndarray] = {}
    for target_frame, source_frame in enumerate(source_indices):
        pose = motion.frames[source_frame]
        for pmx_index, raw_index in enumerate(mapping):
            if raw_index < 0:
                continue
            transform = pose[raw_index]
            source_translation = np.asarray(
                (-transform[0], transform[1], -transform[2]), dtype=np.float32
            )
            reference = reference_transforms[raw_index]
            reference_translation = np.asarray(
                (-reference[0], reference[1], -reference[2]), dtype=np.float32
            )
            translation = source_translation - reference_translation
            current_rotation = np.asarray(
                (-transform[3], transform[4], -transform[5], transform[6]),
                dtype=np.float32,
            )
            reference_rotation = np.asarray(
                (-reference[3], reference[4], -reference[5], reference[6]),
                dtype=np.float32,
            )
            rotation = quaternion_delta(current_rotation, reference_rotation)
            previous = previous_rotations.get(pmx_index)
            if previous is not None and float(np.dot(previous, rotation)) < 0.0:
                rotation = -rotation
            previous_rotations[pmx_index] = rotation
            records.append(
                cp932_field(aliases[pmx_index], 15)
                + struct.pack("<I3f4f", target_frame, *translation, *rotation.tolist())
                + interpolation
            )

    vmd_path.parent.mkdir(parents=True, exist_ok=True)
    with vmd_path.open("wb") as output:
        output.write(b"Vocaloid Motion Data 0002".ljust(30, b"\0"))
        output.write(cp932_field(str(model.name or pmx_path.stem), 20))
        output.write(struct.pack("<I", len(records)))
        output.writelines(records)
        output.write(struct.pack("<I", 0))  # morphs
        output.write(struct.pack("<I", 0))  # cameras
        output.write(struct.pack("<I", 0))  # lights
        output.write(struct.pack("<I", 0))  # self shadows
        output.write(struct.pack("<I", 0))  # IK visibility

    animated_scale = bool(
        motion.has_scale
        and np.max(np.abs(motion.frames[:, :, 7:10] - 1.0)) > 1.0e-4
    )
    return vmd_path, compatible_path, matched, len(pmx_names), animated_scale


def compose_global_transforms(
    local_transforms: np.ndarray, parents: tuple[int, ...] | list[int]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compose ACL local transforms into global position, rotation, and scale."""
    count = len(local_transforms)
    positions = np.zeros((count, 3), dtype=np.float32)
    rotations = np.zeros((count, 4), dtype=np.float32)
    scales = np.ones((count, 3), dtype=np.float32)

    def quat_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
        ax, ay, az, aw = a
        bx, by, bz, bw = b
        return np.asarray(
            (
                aw * bx + ax * bw + ay * bz - az * by,
                aw * by - ax * bz + ay * bw + az * bx,
                aw * bz + ax * by - ay * bx + az * bw,
                aw * bw - ax * bx - ay * by - az * bz,
            ),
            dtype=np.float32,
        )

    def rotate(q: np.ndarray, value: np.ndarray) -> np.ndarray:
        vector = q[:3]
        return value + 2.0 * np.cross(vector, np.cross(vector, value) + q[3] * value)

    visiting = np.zeros(count, dtype=np.uint8)

    def visit(index: int) -> None:
        if visiting[index] == 2:
            return
        visiting[index] = 1
        local_t = local_transforms[index, :3]
        local_q = local_transforms[index, 3:7]
        local_s = local_transforms[index, 7:10]
        parent = int(parents[index]) if index < len(parents) else -1
        if 0 <= parent < count and parent != index and visiting[parent] != 1:
            visit(parent)
            positions[index] = positions[parent] + rotate(
                rotations[parent], local_t * scales[parent]
            )
            rotations[index] = quat_mul(rotations[parent], local_q)
            scales[index] = scales[parent] * local_s
        else:
            positions[index] = local_t
            rotations[index] = local_q
            scales[index] = local_s
        norm = float(np.linalg.norm(rotations[index]))
        if norm > 1.0e-8:
            rotations[index] /= norm
        else:
            rotations[index] = (0.0, 0.0, 0.0, 1.0)
        visiting[index] = 2

    for bone_index in range(count):
        visit(bone_index)
    # NeoX -> PMX coordinate conversion: 180 degrees around the Y axis.
    positions[:, (0, 2)] *= -1.0
    rotations[:, 0] *= -1.0
    rotations[:, 2] *= -1.0
    return positions, rotations, scales


def compose_global_positions(
    local_transforms: np.ndarray, parents: tuple[int, ...] | list[int]
) -> np.ndarray:
    """Compose ACL local transforms and return global joint positions."""
    return compose_global_transforms(local_transforms, parents)[0]


def skeleton_display_mask(
    positions: np.ndarray, parents: tuple[int, ...] | list[int]
) -> np.ndarray:
    """Hide detached helper-bone branches that would ruin preview auto framing.

    Game clips often animate camera targets, effect anchors, or unused attachment
    bones hundreds of model units away. Their data remains intact for decoding
    and export; this mask only keeps them from flattening the visual preview.
    """
    count = len(positions)
    visible = np.isfinite(positions).all(axis=1)
    edges: list[tuple[int, int, float]] = []
    for child, parent in enumerate(parents):
        if 0 <= parent < count and visible[child] and visible[parent]:
            length = float(np.linalg.norm(positions[child] - positions[parent]))
            if np.isfinite(length):
                edges.append((child, parent, length))
    positive = np.asarray([length for _child, _parent, length in edges if length > 1.0e-6])
    if len(positive) < 4:
        return visible
    typical = float(np.median(positive))
    upper_normal = float(np.quantile(positive, 0.75))
    threshold = max(typical * 12.0, upper_normal * 6.0, 1.0e-4)
    for child, _parent, length in edges:
        if length > threshold:
            visible[child] = False

    # A child of a detached helper is detached too, even if its own local edge
    # happens to be short.
    changed = True
    while changed:
        changed = False
        for child, parent in enumerate(parents):
            if 0 <= parent < count and not visible[parent] and visible[child]:
                visible[child] = False
                changed = True
    return visible
