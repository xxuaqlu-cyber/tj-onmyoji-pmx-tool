# -*- coding: utf-8 -*-
"""Index and export character voice samples stored in FMOD FSB5 banks.

The game keeps the voice data in FSB5 containers.  This module deliberately
only parses the small FSB metadata table; decoding is delegated to
``vgmstream-cli`` so the GUI does not need a native Vorbis/FSB dependency.
"""

from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


class AudioExportError(RuntimeError):
    """A user-actionable failure while locating or decoding voice audio."""


@dataclass(frozen=True)
class AudioSample:
    bank: Path
    index: int
    name: str
    codec: int = 0


@dataclass(frozen=True)
class AudioMatch:
    sample: AudioSample
    score: int


def _read_u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise AudioExportError("FSB5 头部不完整")
    return struct.unpack_from("<I", data, offset)[0]


def parse_fsb5_samples(path: Path) -> tuple[int, list[str]]:
    """Return ``(codec, sample names)`` from an FSB5 file.

    FSB5 stores the sample names after the per-sample metadata.  The metadata
    chunks are length-prefixed, so walking them is enough to find the name
    table without decoding any audio payload.
    """
    path = Path(path)
    with path.open("rb") as handle:
        data = handle.read(16 * 1024 * 1024)
    if data[:4] != b"FSB5":
        raise AudioExportError(f"不是 FSB5 音频容器：{path.name}")
    version = _read_u32(data, 4)
    count = _read_u32(data, 8)
    shsize = _read_u32(data, 12)
    namesize = _read_u32(data, 16)
    codec = _read_u32(data, 24)
    base = 0x3C if version == 1 else 0x40
    if count > 100_000 or shsize > len(data) or namesize > len(data):
        raise AudioExportError(f"FSB5 元数据尺寸异常：{path.name}")
    metadata_end = base + shsize
    names_end = metadata_end + namesize
    if metadata_end > len(data) or names_end > len(data):
        raise AudioExportError(
            f"FSB5 名称表超出已读取范围（文件过大或损坏）：{path.name}"
        )
    cursor = base
    for _ in range(count):
        if cursor + 8 > metadata_end:
            raise AudioExportError(f"FSB5 样本表损坏：{path.name}")
        chunk = struct.unpack_from("<Q", data, cursor)[0]
        cursor += 8
        has_chunks = bool(chunk & 1)
        while has_chunks:
            if cursor + 4 > metadata_end:
                raise AudioExportError(f"FSB5 样本扩展表损坏：{path.name}")
            value = _read_u32(data, cursor)
            cursor += 4
            has_chunks = bool(value & 1)
            chunk_size = (value >> 1) & 0xFFFFFF
            cursor += chunk_size
            if cursor > metadata_end:
                raise AudioExportError(f"FSB5 样本扩展表越界：{path.name}")
    names_blob = data[metadata_end:names_end]
    offsets: list[int] = []
    cursor = 0
    while cursor + 4 <= len(names_blob) and len(offsets) < count:
        offsets.append(struct.unpack_from("<I", names_blob, cursor)[0])
        cursor += 4
    if len(offsets) < count:
        raise AudioExportError(f"FSB5 名称偏移表不完整：{path.name}")
    # Offsets in FSB5 are relative to the beginning of the name-size block,
    # which includes the offset table itself (the first string above starts at
    # offset ``count * 4`` in ordinary banks).
    names_start = 0
    result: list[str] = []
    for offset in offsets:
        start = names_start + offset
        if start < names_start or start >= len(names_blob):
            result.append(f"sample_{len(result) + 1:04d}")
            continue
        end = names_blob.find(b"\0", start)
        if end < 0:
            end = len(names_blob)
        raw = names_blob[start:end]
        result.append(raw.decode("utf-8", errors="replace") or f"sample_{len(result) + 1:04d}")
    return codec, result


def find_sound_root(workspace: Path) -> Path | None:
    """Find the loose ``res/sound/output`` directory from a pulled game tree."""
    workspace = Path(workspace)
    direct = workspace / "yys" / "com.netease.onmyoji.wyzymnqsd_cps" / "files" / "netease" / "onmyoji" / "res" / "sound" / "output"
    if direct.is_dir():
        return direct
    for candidate in workspace.glob("**/res/sound/output"):
        if candidate.is_dir():
            return candidate
    return None


def _fingerprint(root: Path) -> list[list[Any]]:
    result: list[list[Any]] = []
    for path in sorted(root.glob("*.fsb")):
        try:
            stat = path.stat()
        except OSError:
            continue
        result.append([path.name, stat.st_size, stat.st_mtime_ns])
    return result


def build_audio_index(
    sound_root: Path,
    cache_path: Path | None = None,
    *,
    force: bool = False,
) -> list[AudioSample]:
    """Parse all FSB names, reusing a small JSON cache when possible."""
    root = Path(sound_root)
    if not root.is_dir():
        raise AudioExportError(f"没有找到音频目录：{root}")
    fingerprint = _fingerprint(root)
    if cache_path is not None and not force and Path(cache_path).is_file():
        try:
            cached = json.loads(Path(cache_path).read_text(encoding="utf-8"))
            if (
                cached.get("root") == str(root.resolve())
                and cached.get("fingerprint") == fingerprint
            ):
                return [
                    AudioSample(Path(item["bank"]), int(item["index"]), str(item["name"]), int(item.get("codec", 0)))
                    for item in cached.get("samples", [])
                ]
        except (OSError, ValueError, KeyError, TypeError):
            pass
    samples: list[AudioSample] = []
    for bank_name, _size, _mtime in fingerprint:
        bank = root / bank_name
        try:
            codec, names = parse_fsb5_samples(bank)
        except (OSError, AudioExportError):
            continue
        samples.extend(AudioSample(bank, index, name, codec) for index, name in enumerate(names, 1))
    if cache_path is not None:
        cache = Path(cache_path)
        cache.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "root": str(root.resolve()),
            "fingerprint": fingerprint,
            "samples": [
                {"bank": str(sample.bank), "index": sample.index, "name": sample.name, "codec": sample.codec}
                for sample in samples
            ],
        }
        temporary = cache.with_suffix(cache.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(cache)
    return samples


_GENERIC_ACTIONS = frozenset({
    "idle", "walk", "run", "born", "die", "dead", "hit", "skill0", "skill1", "skill2", "skill3",
    "book", "touch", "touch_body", "touch_head", "touch_leg", "win", "lose", "stun", "show",
})


def _tokens(value: str) -> list[str]:
    return [part for part in re.split(r"[^0-9a-z]+", value.lower()) if part]


def _model_variants(value: str) -> set[str]:
    tokens = _tokens(value)
    # Skin/model prefixes are not part of the FSB voice code.
    stripped = [token for token in tokens if not re.fullmatch(r"(?:s|c|q|j|sp|ss)\d*", token)]
    variants = set(stripped)
    if stripped:
        variants.add("_".join(stripped))
        for token in stripped:
            if len(token) >= 3:
                variants.add(token[0] + "".join(ch for ch in token[1:] if ch not in "eiou"))
    return {item for item in variants if len(item) >= 2}


def _skin_prefix(value: str) -> str:
    tokens = _tokens(value)
    if not tokens:
        return ""
    if len(tokens) >= 2 and (re.fullmatch(r"s\d+", tokens[0]) or tokens[0] in {"q", "sp", "ss", "c1", "c2", "c3"}):
        return tokens[0]
    return ""


def _action_score(action: str, sample_name: str, bank_name: str) -> int:
    action_tokens = _tokens(action)
    if not action_tokens:
        return 0
    sample_tokens = _tokens(sample_name)
    bank_tokens = _tokens(bank_name)
    combined = sample_tokens + bank_tokens
    if all(token in combined for token in action_tokens):
        score = 30
        if any(
            sample_tokens[index:index + len(action_tokens)] == action_tokens
            for index in range(max(0, len(sample_tokens) - len(action_tokens) + 1))
        ):
            score += 25
        return score
    compact_action = "_".join(action_tokens)
    compact_name = "_".join(sample_tokens)
    if compact_action in compact_name or compact_action in "_".join(bank_tokens):
        return 20
    return 0


def find_audio_matches(header: Any, samples: Iterable[AudioSample]) -> list[AudioMatch]:
    """Return the best character/action voice candidates for a motion header."""
    action = str(getattr(header, "action", ""))
    skeleton = str(getattr(header, "skeleton_name", "") or getattr(header, "skeleton_ref", ""))
    model_variants = _model_variants(skeleton)
    desired_prefix = _skin_prefix(skeleton)
    matches: list[AudioMatch] = []
    for sample in samples:
        voice_tokens = set(_tokens(sample.name) + _tokens(sample.bank.stem))
        if not ({"vo", "voice"} & voice_tokens):
            continue
        score = _action_score(action, sample.name, sample.bank.stem)
        if score <= 0:
            continue
        haystack = voice_tokens
        model_score = 0
        for variant in model_variants:
            if variant in haystack:
                model_score = max(model_score, 100 + len(variant))
            elif variant in sample.name.lower() or variant in sample.bank.stem.lower():
                model_score = max(model_score, 80 + len(variant))
        bank_tokens = _tokens(sample.bank.stem)
        bank_prefixes = {
            token for token in bank_tokens
            if re.fullmatch(r"s\d+", token) or token in {"q", "sp", "ss", "c1", "c2", "c3"}
        }
        if model_score and desired_prefix:
            model_score += 12 if desired_prefix in bank_prefixes else -12
        elif model_score and bank_prefixes:
            model_score -= 18
        if model_score:
            score += model_score
        elif _tokens(action)[0] in _GENERIC_ACTIONS:
            continue
        matches.append(AudioMatch(sample, score))
    matches.sort(key=lambda item: (-item.score, str(item.sample.bank).lower(), item.sample.index))
    if not matches:
        return []
    best = matches[0].score
    # Keep equally good language/bank variants, while avoiding every generic
    # ``born`` sample in the whole install.
    return [item for item in matches if item.score >= best - 12]


def find_vgmstream_cli(workspace: Path) -> Path | None:
    workspace = Path(workspace)
    candidates = [
        workspace / "tools" / "vgmstream-win" / "vgmstream-cli.exe",
        workspace / "tools" / "vgmstream-cli.exe",
        workspace / "vgmstream-cli.exe",
        workspace / "tools" / "vgmstream-cli",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    found = shutil.which("vgmstream-cli") or shutil.which("vgmstream-cli.exe")
    return Path(found) if found else None


def export_motion_audio(
    header: Any,
    output_dir: Path,
    *,
    workspace: Path,
    cache_path: Path | None = None,
    decoder: Path | None = None,
    progress=None,
) -> list[Path]:
    """Decode all matched samples for one motion into ``output_dir``."""
    decoder = Path(decoder) if decoder is not None else find_vgmstream_cli(workspace)
    if decoder is None:
        raise AudioExportError(
            "未找到 vgmstream-cli。请将 Windows 发布包完整解压到项目 "
            "tools\\vgmstream-win\\ 目录，保留 EXE 旁的 DLL 后重试。"
        )
    root = find_sound_root(workspace)
    if root is None:
        raise AudioExportError("没有找到解包后的 res\\sound\\output 音频目录。")
    samples = build_audio_index(root, cache_path)
    matches = find_audio_matches(header, samples)
    if not matches:
        raise AudioExportError(
            f"没有找到与角色 {getattr(header, 'skeleton_name', '')} / 动作 {getattr(header, 'action', '')} 匹配的语音样本。"
        )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[Path] = []
    manifest: list[dict[str, Any]] = []
    total = len(matches)
    for ordinal, match in enumerate(matches, 1):
        sample = match.sample
        safe_bank = re.sub(r"[^0-9A-Za-z_-]+", "_", sample.bank.stem).strip("_") or "audio"
        safe_name = re.sub(r"[^0-9A-Za-z_一-鿿-]+", "_", sample.name).strip("_") or f"sample_{sample.index:04d}"
        target = output_dir / f"{ordinal:03d}_{safe_bank}_{safe_name}.wav"
        command = [str(decoder), "-s", str(sample.index), "-o", str(target), str(sample.bank)]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0 or not target.is_file():
            detail = (completed.stderr or completed.stdout or "未知解码错误").strip()
            raise AudioExportError(f"解码 {sample.bank.name}#{sample.index} 失败：{detail}")
        outputs.append(target)
        manifest.append({"file": target.name, "bank": str(sample.bank), "sample_index": sample.index, "sample_name": sample.name, "score": match.score})
        if progress is not None:
            progress(ordinal, total, sample.name)
    (output_dir / "audio_manifest.json").write_text(
        json.dumps({"motion": str(getattr(header, "path", "")), "action": str(getattr(header, "action", "")), "skeleton": str(getattr(header, "skeleton_name", "")), "samples": manifest}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return outputs
