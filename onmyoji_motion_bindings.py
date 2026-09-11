"""Verified RAWANIMA-to-model bindings recovered from the official THP graph.

The game stores a model package as a THP parent with its mesh, skeleton,
animation config, and RAWANIMA children.  This is a real resource dependency
edge, unlike similarly named files or similar-looking bone hierarchies.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from thd_resource_index import read_model_thp, read_model_thx


CACHE_SCHEMA = 5
CACHE_FILENAME = "official_motion_bindings_v1.json"


def _path_key(path: Path | str) -> str:
    return str(path).replace("\\", "/").lower()


def _resource_name(value: str) -> str:
    return Path(value.replace("\\", "/")).name.lower()


@dataclass(frozen=True, slots=True)
class OfficialMotionBindings:
    """Direct game-resource links for exported PMX source meshes."""

    motion_to_meshes: dict[str, frozenset[str]]
    mesh_paths: dict[str, tuple[Path, ...]]
    package_count: int
    mesh_bone_names: dict[str, frozenset[str]] = field(default_factory=dict)

    @classmethod
    def load_or_build(cls, workspace: Path) -> "OfficialMotionBindings":
        workspace = workspace.resolve()
        model_root = workspace / "unpacked" / "model"
        thd_root = (
            workspace
            / "yys"
            / "com.netease.onmyoji.wyzymnqsd_cps"
            / "files"
            / "netease"
            / "onmyoji"
            / "Documents"
            / "cloudfilesys3"
            / "thd"
        )
        manifest = model_root / "manifest.csv"
        cache_path = workspace / ".motion_cache" / CACHE_FILENAME
        fingerprint = cls._fingerprint(manifest, thd_root)
        cached = cls._read_cache(cache_path, fingerprint)
        if cached is not None:
            return cached
        result = cls._build(model_root, thd_root)
        cls._write_cache(cache_path, fingerprint, result)
        return result

    @staticmethod
    def _fingerprint(manifest: Path, thd_root: Path) -> dict[str, tuple[int, int]]:
        files = (manifest, thd_root / "model.thx", thd_root / "model.thp")
        return {
            str(path.resolve()): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in files
            if path.is_file()
        }

    @classmethod
    def _read_cache(
        cls, path: Path, fingerprint: dict[str, tuple[int, int]]
    ) -> "OfficialMotionBindings | None":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (
                payload.get("schema") != CACHE_SCHEMA
                or payload.get("fingerprint") != {key: list(value) for key, value in fingerprint.items()}
            ):
                return None
            motions = {
                str(path_key): frozenset(str(name) for name in names)
                for path_key, names in payload["motion_to_meshes"].items()
            }
            meshes = {
                str(name): tuple(Path(value) for value in values)
                for name, values in payload["mesh_paths"].items()
            }
            mesh_bones = {
                str(name): frozenset(str(value) for value in values)
                for name, values in payload.get("mesh_bone_names", {}).items()
            }
            return cls(motions, meshes, int(payload["package_count"]), mesh_bones)
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _write_cache(
        path: Path,
        fingerprint: dict[str, tuple[int, int]],
        result: "OfficialMotionBindings",
    ) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema": CACHE_SCHEMA,
                "fingerprint": {key: list(value) for key, value in fingerprint.items()},
                "motion_to_meshes": {
                    key: sorted(values)
                    for key, values in result.motion_to_meshes.items()
                },
                "mesh_paths": {
                    key: [str(value) for value in values]
                    for key, values in result.mesh_paths.items()
                },
                "mesh_bone_names": {
                    key: sorted(values)
                    for key, values in result.mesh_bone_names.items()
                },
                "package_count": result.package_count,
            }
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError:
            pass

    @classmethod
    def _build(cls, model_root: Path, thd_root: Path) -> "OfficialMotionBindings":
        from onmyoji_motion import normalized_bone_name, read_motion_header
        from onmyoji_rigged_mesh_gui import read_mesh_bone_layout

        manifest = model_root / "manifest.csv"
        thx_path = thd_root / "model.thx"
        thp_path = thd_root / "model.thp"
        if not (manifest.is_file() and thx_path.is_file() and thp_path.is_file()):
            return cls({}, {}, 0)

        paths_by_md5: dict[str, list[str]] = {}
        mesh_paths: dict[str, list[Path]] = {}
        with manifest.open("r", newline="", encoding="utf-8-sig") as stream:
            for row in csv.DictReader(stream):
                if row.get("status") not in {"ok", "exists"}:
                    continue
                digest = str(row.get("resource_hash", "")).strip().lower()
                output_path = _path_key(str(row.get("output_path", "")).strip())
                if len(digest) != 32 or not output_path:
                    continue
                # manifest.csv is rooted at ``unpacked`` (``model/pkg...``),
                # while the motion UI's resource root is ``unpacked/model``.
                # Keep the index in the latter coordinate system throughout.
                relative = output_path.removeprefix("model/")
                if relative == output_path:
                    continue
                paths_by_md5.setdefault(digest, []).append(relative)
                if relative.lower().endswith(".mesh"):
                    name = _resource_name(relative)
                    mesh_paths.setdefault(name, []).append((model_root / relative).resolve())

        # THX identifies dependencies by logical name hash.  The extraction
        # manifest only exposes content MD5, so a reused payload cannot prove
        # which logical resource it came from.  Exclude those ambiguous MD5s
        # instead of turning a byte-identical animation into a false model link.
        unique_paths_by_md5 = {
            digest: tuple(paths)
            for digest, paths in paths_by_md5.items()
            if len(set(paths)) == 1
        }
        records = read_model_thx(thx_path)
        record_by_hash = {record.name_hash: record for record in records}
        dependencies = read_model_thp(thp_path)
        motion_to_meshes: dict[str, set[str]] = {}
        motion_bones: dict[str, frozenset[str]] = {}
        mesh_bones: dict[str, frozenset[str]] = {}

        def motion_signature(relative: str) -> frozenset[str]:
            cached = motion_bones.get(relative)
            if cached is not None:
                return cached
            try:
                header = read_motion_header(model_root / relative)
                cached = frozenset(
                    normalized_bone_name(name) for name in header.bone_names
                )
            except Exception:
                cached = frozenset()
            motion_bones[relative] = cached
            return cached

        def mesh_signature(name: str) -> frozenset[str]:
            cached = mesh_bones.get(name)
            if cached is not None:
                return cached
            signatures: set[frozenset[str]] = set()
            for path in mesh_paths.get(name, ()):
                try:
                    bone_names, _parents, _matrices = read_mesh_bone_layout(path)
                    signature = frozenset(
                        normalized_bone_name(value) for value in bone_names
                    )
                    if signature:
                        signatures.add(signature)
                except Exception:
                    continue
            cached = next(iter(signatures)) if len(signatures) == 1 else frozenset()
            mesh_bones[name] = cached
            return cached

        # A parent is the game's own package-level dependency list.  Mapping an
        # animation to its mesh children preserves exactly that relation.
        for children in dependencies.values():
            motions: set[str] = set()
            meshes: set[str] = set()
            for child_hash in children:
                record = record_by_hash.get(child_hash)
                if record is None:
                    continue
                for relative in unique_paths_by_md5.get(record.content_md5, ()):
                    normalized = _path_key(relative)
                    if normalized.endswith(".rawanimation"):
                        motions.add(normalized)
                    elif normalized.endswith(".mesh"):
                        meshes.add(_resource_name(relative))
            if not motions or not meshes:
                continue
            for motion in motions:
                animation_bones = motion_signature(motion)
                if len(animation_bones) < 4:
                    continue
                compatible = {
                    mesh
                    for mesh in meshes
                    if animation_bones.issubset(mesh_signature(mesh))
                }
                if compatible:
                    motion_to_meshes.setdefault(motion, set()).update(compatible)

        return cls(
            {key: frozenset(value) for key, value in motion_to_meshes.items()},
            {key: tuple(value) for key, value in mesh_paths.items()},
            len(dependencies),
            {key: value for key, value in mesh_bones.items() if value},
        )

    def candidate_meshes_for_motion(self, motion_path: Path, model_root: Path) -> frozenset[str]:
        # This runs once per catalogue entry when exporting all model actions.
        # Path.resolve() touches the filesystem and turns 37k in-memory lookups
        # into tens of seconds on Windows, so calculate the relative key purely
        # lexically after both paths have already been supplied as absolute or
        # workspace-relative values.
        motion_absolute = os.path.abspath(os.fspath(motion_path))
        root_absolute = os.path.abspath(os.fspath(model_root))
        try:
            relative = os.path.relpath(motion_absolute, root_absolute)
        except ValueError:  # Different Windows drive.
            return frozenset()
        if relative == os.pardir or relative.startswith(os.pardir + os.sep):
            return frozenset()
        return self.motion_to_meshes.get(_path_key(relative), frozenset())

    def matches_motion(self, motion_path: Path, model_root: Path, source_mesh: str) -> bool:
        return _resource_name(source_mesh) in self.candidate_meshes_for_motion(
            motion_path, model_root
        )

    def source_mesh_paths(self, source_mesh: str) -> tuple[Path, ...]:
        return self.mesh_paths.get(_resource_name(source_mesh), ())

    def bone_names_for_mesh(self, source_mesh: str) -> frozenset[str]:
        """Return the unpack-time bone signature for a logical source Mesh."""
        return self.mesh_bone_names.get(_resource_name(source_mesh), frozenset())
