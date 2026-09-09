# -*- coding: utf-8 -*-
"""Small dependency-free binary FBX 7.4 writer for animated PMX previews."""

from __future__ import annotations

import datetime as _datetime
import math
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import numpy as np


FBX_VERSION = 7400
FBX_TIME_SECOND = 46_186_158_000
_NULL_RECORD = b"\0" * 13
_FOOTER_MAGIC = bytes.fromhex("fabcab09d0c8d466b176fb831cf7267e")


@dataclass(slots=True)
class _Property:
    kind: str
    value: object


@dataclass(slots=True)
class _Node:
    name: str
    properties: list[_Property] = field(default_factory=list)
    children: list["_Node"] = field(default_factory=list)


def _p(kind: str, value: object) -> _Property:
    return _Property(kind, value)


def _n(name: str, *properties: _Property, children: Iterable[_Node] = ()) -> _Node:
    return _Node(name, list(properties), list(children))


def _array_property(kind: str, value: object) -> bytes:
    dtype = {
        "d": "<f8",
        "f": "<f4",
        "i": "<i4",
        "l": "<i8",
        "b": "u1",
    }[kind]
    array = np.ascontiguousarray(np.asarray(value, dtype=dtype).reshape(-1))
    raw = array.tobytes()
    if len(raw) >= 128:
        packed = zlib.compress(raw, 6)
        return kind.encode("ascii") + struct.pack("<III", len(array), 1, len(packed)) + packed
    return kind.encode("ascii") + struct.pack("<III", len(array), 0, len(raw)) + raw


def _property_bytes(prop: _Property) -> bytes:
    kind = prop.kind
    value = prop.value
    if kind in {"d", "f", "i", "l", "b"}:
        return _array_property(kind, value)
    if kind == "C":
        return b"C" + (b"\x01" if value else b"\x00")
    if kind == "I":
        return b"I" + struct.pack("<i", int(value))
    if kind == "L":
        return b"L" + struct.pack("<q", int(value))
    if kind == "D":
        return b"D" + struct.pack("<d", float(value))
    if kind == "F":
        return b"F" + struct.pack("<f", float(value))
    if kind in {"S", "R"}:
        raw = str(value).encode("utf-8") if kind == "S" else bytes(value)
        return kind.encode("ascii") + struct.pack("<I", len(raw)) + raw
    raise ValueError(f"unsupported FBX property kind: {kind}")


def _node_bytes(node: _Node, start: int) -> bytes:
    name = node.name.encode("utf-8")
    properties = b"".join(_property_bytes(prop) for prop in node.properties)
    header_size = 13 + len(name)
    cursor = start + header_size + len(properties)
    child_parts: list[bytes] = []
    for child in node.children:
        payload = _node_bytes(child, cursor)
        child_parts.append(payload)
        cursor += len(payload)
    if node.children:
        child_parts.append(_NULL_RECORD)
        cursor += len(_NULL_RECORD)
    header = struct.pack(
        "<IIIB", cursor, len(node.properties), len(properties), len(name)
    )
    return header + name + properties + b"".join(child_parts)


def _binary_document(nodes: list[_Node]) -> bytes:
    header = b"Kaydara FBX Binary  \x00\x1a\x00" + struct.pack("<I", FBX_VERSION)
    cursor = len(header)
    parts = [header]
    for node in nodes:
        payload = _node_bytes(node, cursor)
        parts.append(payload)
        cursor += len(payload)
    parts.append(_NULL_RECORD)
    parts.append(_FOOTER_MAGIC)
    parts.append(b"\0" * 4)
    parts.append(struct.pack("<I", FBX_VERSION))
    parts.append(b"\0" * 120)
    parts.append(_FOOTER_MAGIC)
    return b"".join(parts)


def _property70(
    name: str, type_name: str, subtype: str, flags: str, *values: object
) -> _Node:
    properties = [_p("S", name), _p("S", type_name), _p("S", subtype), _p("S", flags)]
    for value in values:
        if isinstance(value, int):
            properties.append(_p("L" if type_name == "KTime" else "I", value))
        elif isinstance(value, str):
            properties.append(_p("S", value))
        else:
            properties.append(_p("D", float(value)))
    return _n("P", *properties)


class _Ids:
    def __init__(self) -> None:
        self.value = 1_000_000

    def next(self) -> int:
        self.value += 1
        return self.value


def _safe_names(names: Iterable[str]) -> list[str]:
    result: list[str] = []
    used: set[str] = set()
    for index, raw in enumerate(names):
        base = str(raw).replace("\0", "").strip() or f"Bone_{index:04d}"
        candidate = base
        suffix = 2
        while candidate.lower() in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate.lower())
        result.append(candidate)
    return result


def _matrix_values(matrix: np.ndarray) -> np.ndarray:
    value = np.asarray(matrix, dtype=np.float64).reshape(4, 4).copy()
    value[np.abs(value) < 1.0e-12] = 0.0
    return value.reshape(-1)


def decompose_row_matrices(
    matrices: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Decompose row-vector affine matrices into translation, XYZ Euler and scale."""
    values = np.asarray(matrices, dtype=np.float64)
    original_shape = values.shape[:-2]
    flat = values.reshape(-1, 4, 4)
    translations = flat[:, 3, :3].copy()
    scales = np.linalg.norm(flat[:, :3, :3], axis=2)
    safe_scales = np.where(scales > 1.0e-12, scales, 1.0)
    rotations = flat[:, :3, :3] / safe_scales[:, :, None]
    quaternions = np.zeros((len(flat), 4), dtype=np.float64)
    eulers = np.zeros((len(flat), 3), dtype=np.float64)
    for index, row_rotation in enumerate(rotations):
        if np.linalg.det(row_rotation) < 0.0:
            axis = int(np.argmax(np.abs(scales[index])))
            scales[index, axis] *= -1.0
            row_rotation[axis] *= -1.0
        # FBX's conventional quaternion formulas use column-vector matrices.
        matrix = row_rotation.T
        trace = float(np.trace(matrix))
        if trace > 0.0:
            root = math.sqrt(trace + 1.0) * 2.0
            w = 0.25 * root
            x = (matrix[2, 1] - matrix[1, 2]) / root
            y = (matrix[0, 2] - matrix[2, 0]) / root
            z = (matrix[1, 0] - matrix[0, 1]) / root
        else:
            diagonal = np.diag(matrix)
            axis = int(np.argmax(diagonal))
            if axis == 0:
                root = math.sqrt(max(1.0 + matrix[0, 0] - matrix[1, 1] - matrix[2, 2], 0.0)) * 2.0
                x = 0.25 * root
                y = (matrix[0, 1] + matrix[1, 0]) / max(root, 1.0e-12)
                z = (matrix[0, 2] + matrix[2, 0]) / max(root, 1.0e-12)
                w = (matrix[2, 1] - matrix[1, 2]) / max(root, 1.0e-12)
            elif axis == 1:
                root = math.sqrt(max(1.0 + matrix[1, 1] - matrix[0, 0] - matrix[2, 2], 0.0)) * 2.0
                x = (matrix[0, 1] + matrix[1, 0]) / max(root, 1.0e-12)
                y = 0.25 * root
                z = (matrix[1, 2] + matrix[2, 1]) / max(root, 1.0e-12)
                w = (matrix[0, 2] - matrix[2, 0]) / max(root, 1.0e-12)
            else:
                root = math.sqrt(max(1.0 + matrix[2, 2] - matrix[0, 0] - matrix[1, 1], 0.0)) * 2.0
                x = (matrix[0, 2] + matrix[2, 0]) / max(root, 1.0e-12)
                y = (matrix[1, 2] + matrix[2, 1]) / max(root, 1.0e-12)
                z = 0.25 * root
                w = (matrix[1, 0] - matrix[0, 1]) / max(root, 1.0e-12)
        quaternion = np.asarray((x, y, z, w), dtype=np.float64)
        quaternion /= max(float(np.linalg.norm(quaternion)), 1.0e-12)
        quaternions[index] = quaternion
        x, y, z, w = quaternion
        eulers[index, 0] = math.atan2(
            2.0 * (w * x + y * z), 1.0 - 2.0 * (x * x + y * y)
        )
        eulers[index, 1] = math.asin(
            max(-1.0, min(1.0, 2.0 * (w * y - z * x)))
        )
        eulers[index, 2] = math.atan2(
            2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z)
        )
    return (
        translations.reshape(*original_shape, 3),
        np.degrees(eulers).reshape(*original_shape, 3),
        scales.reshape(*original_shape, 3),
    )


def _model_properties(translation, rotation, scaling) -> _Node:
    return _n(
        "Properties70",
        children=(
            _property70("RotationOrder", "enum", "", "", 0),
            _property70("Lcl Translation", "Lcl Translation", "", "A", *translation),
            _property70("Lcl Rotation", "Lcl Rotation", "", "A", *rotation),
            _property70("Lcl Scaling", "Lcl Scaling", "", "A", *scaling),
            _property70("Visibility", "Visibility", "", "A", 1.0),
        ),
    )


def _curve_node(ids: _Ids, name: str, kind: str, default: np.ndarray) -> tuple[int, _Node]:
    object_id = ids.next()
    props = [_property70(f"d|{axis}", "Number", "", "A", float(default[i])) for i, axis in enumerate("XYZ")]
    node = _n(
        "AnimationCurveNode",
        _p("L", object_id),
        _p("S", f"AnimCurveNode::{name}_{kind}"),
        _p("S", ""),
        children=(_n("Properties70", children=props),),
    )
    return object_id, node


def _curve(ids: _Ids, name: str, times: np.ndarray, values: np.ndarray) -> tuple[int, _Node]:
    object_id = ids.next()
    count = len(times)
    node = _n(
        "AnimationCurve",
        _p("L", object_id),
        _p("S", f"AnimCurve::{name}"),
        _p("S", ""),
        children=(
            _n("Default", _p("F", float(values[0]) if count else 0.0)),
            _n("KeyVer", _p("I", 4008)),
            _n("KeyTime", _p("l", times)),
            _n("KeyValueFloat", _p("f", values)),
            _n("KeyAttrFlags", _p("i", np.asarray((24836,), dtype=np.int32))),
            _n(
                "KeyAttrDataFloat",
                _p("f", np.asarray((0.0, 0.0, 9.419947376596276e-38, 0.0), dtype=np.float32)),
            ),
            _n("KeyAttrRefCount", _p("i", np.asarray((count,), dtype=np.int32))),
        ),
    )
    return object_id, node


def write_animated_fbx(
    output_path: Path,
    *,
    model_name: str,
    positions: np.ndarray,
    normals: np.ndarray,
    uvs: np.ndarray,
    triangles: np.ndarray,
    face_materials: np.ndarray,
    material_defs: list[dict[str, object]],
    joints: np.ndarray,
    weights: np.ndarray,
    bone_names: list[str] | tuple[str, ...],
    parents: list[int] | tuple[int, ...],
    bind_globals: np.ndarray,
    bind_translation: np.ndarray,
    bind_rotation: np.ndarray,
    bind_scaling: np.ndarray,
    frame_translation: np.ndarray,
    frame_rotation: np.ndarray,
    frame_scaling: np.ndarray,
    fps: float,
    progress: Callable[[str, int, int], None] | None = None,
) -> Path:
    """Write a complete skinned binary FBX with baked local animation curves."""
    positions = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    normals = np.asarray(normals, dtype=np.float64).reshape(-1, 3)
    uvs = np.asarray(uvs, dtype=np.float64).reshape(-1, 2)
    triangles = np.asarray(triangles, dtype=np.int32).reshape(-1, 3)
    face_materials = np.asarray(face_materials, dtype=np.int32).reshape(-1)
    joints = np.asarray(joints, dtype=np.int32).reshape(len(positions), -1)
    weights = np.asarray(weights, dtype=np.float64).reshape(len(positions), -1)
    bind_globals = np.asarray(bind_globals, dtype=np.float64).reshape(-1, 4, 4)
    bind_translation = np.asarray(bind_translation, dtype=np.float64).reshape(-1, 3)
    bind_rotation = np.asarray(bind_rotation, dtype=np.float64).reshape(-1, 3)
    bind_scaling = np.asarray(bind_scaling, dtype=np.float64).reshape(-1, 3)
    frame_translation = np.asarray(frame_translation, dtype=np.float64)
    frame_rotation = np.asarray(frame_rotation, dtype=np.float64)
    frame_scaling = np.asarray(frame_scaling, dtype=np.float64)
    bone_names = _safe_names(bone_names)
    bone_count = len(bone_names)
    frame_count = len(frame_translation)
    if not (
        len(normals) == len(positions)
        and len(uvs) == len(positions)
        and len(face_materials) == len(triangles)
        and len(parents) == bone_count
        and len(bind_globals) == bone_count
        and frame_translation.shape == (frame_count, bone_count, 3)
        and frame_rotation.shape == (frame_count, bone_count, 3)
        and frame_scaling.shape == (frame_count, bone_count, 3)
    ):
        raise ValueError("FBX geometry, skeleton, or animation array sizes do not match")
    if fps <= 0 or frame_count == 0:
        raise ValueError("FBX animation requires at least one frame and a positive FPS")

    ids = _Ids()
    document_id = ids.next()
    geometry_id = ids.next()
    mesh_model_id = ids.next()
    skin_id = ids.next()
    bind_pose_id = ids.next()
    bone_model_ids = [ids.next() for _ in bone_names]
    bone_attribute_ids = [ids.next() for _ in bone_names]
    cluster_ids = [ids.next() for _ in bone_names]
    material_ids = [ids.next() for _ in material_defs]
    texture_ids: list[int | None] = []
    video_ids: list[int | None] = []
    for material in material_defs:
        if material.get("texture"):
            texture_ids.append(ids.next())
            video_ids.append(ids.next())
        else:
            texture_ids.append(None)
            video_ids.append(None)
    stack_id = ids.next()
    layer_id = ids.next()

    polygon_indices = triangles.copy()
    polygon_indices[:, 2] = -polygon_indices[:, 2] - 1
    # UVIndex uses ordinary indices, unlike PolygonVertexIndex's negative end marker.
    uv_indices = triangles.reshape(-1)

    geometry = _n(
        "Geometry",
        _p("L", geometry_id),
        _p("S", f"Geometry::{model_name}"),
        _p("S", "Mesh"),
        children=(
            _n("GeometryVersion", _p("I", 124)),
            _n("Vertices", _p("d", positions)),
            _n("PolygonVertexIndex", _p("i", polygon_indices)),
            _n(
                "LayerElementNormal",
                _p("I", 0),
                children=(
                    _n("Version", _p("I", 101)),
                    _n("Name", _p("S", "")),
                    _n("MappingInformationType", _p("S", "ByVertice")),
                    _n("ReferenceInformationType", _p("S", "Direct")),
                    _n("Normals", _p("d", normals)),
                ),
            ),
            _n(
                "LayerElementUV",
                _p("I", 0),
                children=(
                    _n("Version", _p("I", 101)),
                    _n("Name", _p("S", "UVChannel_1")),
                    _n("MappingInformationType", _p("S", "ByPolygonVertex")),
                    _n("ReferenceInformationType", _p("S", "IndexToDirect")),
                    _n("UV", _p("d", uvs)),
                    _n("UVIndex", _p("i", uv_indices)),
                ),
            ),
            _n(
                "LayerElementMaterial",
                _p("I", 0),
                children=(
                    _n("Version", _p("I", 101)),
                    _n("Name", _p("S", "")),
                    _n("MappingInformationType", _p("S", "ByPolygon")),
                    _n("ReferenceInformationType", _p("S", "IndexToDirect")),
                    _n("Materials", _p("i", face_materials)),
                ),
            ),
            _n(
                "Layer",
                _p("I", 0),
                children=(
                    _n("Version", _p("I", 100)),
                    _n("LayerElement", children=(_n("Type", _p("S", "LayerElementNormal")), _n("TypedIndex", _p("I", 0)))),
                    _n("LayerElement", children=(_n("Type", _p("S", "LayerElementMaterial")), _n("TypedIndex", _p("I", 0)))),
                    _n("LayerElement", children=(_n("Type", _p("S", "LayerElementUV")), _n("TypedIndex", _p("I", 0)))),
                ),
            ),
        ),
    )

    objects: list[_Node] = [geometry]
    mesh_model = _n(
        "Model",
        _p("L", mesh_model_id),
        _p("S", f"Model::{model_name}"),
        _p("S", "Mesh"),
        children=(
            _n("Version", _p("I", 232)),
            _model_properties((0, 0, 0), (0, 0, 0), (1, 1, 1)),
            _n("Shading", _p("C", True)),
            _n("Culling", _p("S", "CullingOff")),
        ),
    )
    objects.append(mesh_model)

    for index, name in enumerate(bone_names):
        objects.append(
            _n(
                "NodeAttribute",
                _p("L", bone_attribute_ids[index]),
                _p("S", f"NodeAttribute::{name}"),
                _p("S", "LimbNode"),
                children=(
                    _n("TypeFlags", _p("S", "Skeleton")),
                    _n("Properties70", children=(_property70("Size", "double", "Number", "", 1.0),)),
                ),
            )
        )
        objects.append(
            _n(
                "Model",
                _p("L", bone_model_ids[index]),
                _p("S", f"Model::{name}"),
                _p("S", "LimbNode"),
                children=(
                    _n("Version", _p("I", 232)),
                    _model_properties(bind_translation[index], bind_rotation[index], bind_scaling[index]),
                    _n("Shading", _p("C", True)),
                    _n("Culling", _p("S", "CullingOff")),
                ),
            )
        )

    objects.append(
        _n(
            "Deformer",
            _p("L", skin_id),
            _p("S", f"Deformer::{model_name}_Skin"),
            _p("S", "Skin"),
            children=(_n("Version", _p("I", 101)), _n("Link_DeformAcuracy", _p("D", 50.0))),
        )
    )
    identity = np.eye(4, dtype=np.float64)
    vertex_ids = np.repeat(np.arange(len(positions), dtype=np.int32), joints.shape[1])
    flat_joints = joints.reshape(-1)
    flat_weights = weights.reshape(-1)
    valid_weights = (
        (flat_weights > 1.0e-8)
        & (flat_joints >= 0)
        & (flat_joints < bone_count)
    )
    vertex_ids = vertex_ids[valid_weights]
    flat_joints = flat_joints[valid_weights]
    flat_weights = flat_weights[valid_weights]
    order = np.lexsort((vertex_ids, flat_joints))
    vertex_ids = vertex_ids[order]
    flat_joints = flat_joints[order]
    flat_weights = flat_weights[order]
    cluster_starts = np.searchsorted(
        flat_joints, np.arange(bone_count + 1), side="left"
    )
    for bone_index, name in enumerate(bone_names):
        start, end = int(cluster_starts[bone_index]), int(cluster_starts[bone_index + 1])
        cluster_vertices = vertex_ids[start:end]
        cluster_values = flat_weights[start:end]
        if len(cluster_vertices):
            vertex_indices, inverse = np.unique(cluster_vertices, return_inverse=True)
            bone_weights = np.zeros(len(vertex_indices), dtype=np.float64)
            np.add.at(bone_weights, inverse, cluster_values)
        else:
            vertex_indices = np.empty(0, dtype=np.int32)
            bone_weights = np.empty(0, dtype=np.float64)
        objects.append(
            _n(
                "Deformer",
                _p("L", cluster_ids[bone_index]),
                _p("S", f"SubDeformer::{name}"),
                _p("S", "Cluster"),
                children=(
                    _n("Version", _p("I", 100)),
                    _n("UserData", _p("S", ""), _p("S", "")),
                    _n("Indexes", _p("i", vertex_indices)),
                    _n("Weights", _p("d", bone_weights)),
                    _n("Transform", _p("d", _matrix_values(identity))),
                    _n("TransformLink", _p("d", _matrix_values(bind_globals[bone_index]))),
                ),
            )
        )
        if progress and (bone_index % 50 == 0 or bone_index + 1 == bone_count):
            progress("写入蒙皮权重", bone_index + 1, bone_count)

    pose_nodes = [
        _n("PoseNode", children=(_n("Node", _p("L", mesh_model_id)), _n("Matrix", _p("d", _matrix_values(identity)))))
    ]
    pose_nodes.extend(
        _n(
            "PoseNode",
            children=(
                _n("Node", _p("L", bone_model_ids[index])),
                _n("Matrix", _p("d", _matrix_values(bind_globals[index]))),
            ),
        )
        for index in range(bone_count)
    )
    objects.append(
        _n(
            "Pose",
            _p("L", bind_pose_id),
            _p("S", "Pose::BindPose"),
            _p("S", "BindPose"),
            children=(
                _n("Type", _p("S", "BindPose")),
                _n("Version", _p("I", 100)),
                _n("NbPoseNodes", _p("I", len(pose_nodes))),
                *pose_nodes,
            ),
        )
    )

    for index, material in enumerate(material_defs):
        rgba = tuple(float(value) for value in material.get("diffuse", (0.8, 0.8, 0.8, 1.0)))
        while len(rgba) < 4:
            rgba += (1.0,)
        name = str(material.get("name") or f"Material_{index:03d}")
        objects.append(
            _n(
                "Material",
                _p("L", material_ids[index]),
                _p("S", f"Material::{name}"),
                _p("S", ""),
                children=(
                    _n("Version", _p("I", 102)),
                    _n("ShadingModel", _p("S", "phong")),
                    _n("MultiLayer", _p("I", 0)),
                    _n(
                        "Properties70",
                        children=(
                            _property70("DiffuseColor", "Color", "", "A", *rgba[:3]),
                            _property70("DiffuseFactor", "Number", "", "A", 1.0),
                            _property70("TransparencyFactor", "Number", "", "A", 1.0 - rgba[3]),
                            _property70("Shininess", "Number", "", "A", 0.0),
                        ),
                    ),
                ),
            )
        )
        texture = material.get("texture")
        if texture_ids[index] is not None and video_ids[index] is not None and texture:
            texture_path = Path(str(texture)).resolve()
            relative = str(material.get("relative_texture") or texture_path.name).replace("\\", "/")
            objects.append(
                _n(
                    "Video",
                    _p("L", video_ids[index]),
                    _p("S", f"Video::{texture_path.stem}"),
                    _p("S", "Clip"),
                    children=(
                        _n("Type", _p("S", "Clip")),
                        _n("UseMipMap", _p("I", 0)),
                        _n("Filename", _p("S", str(texture_path))),
                        _n("RelativeFilename", _p("S", relative)),
                    ),
                )
            )
            objects.append(
                _n(
                    "Texture",
                    _p("L", texture_ids[index]),
                    _p("S", f"Texture::{texture_path.stem}"),
                    _p("S", ""),
                    children=(
                        _n("Type", _p("S", "TextureVideoClip")),
                        _n("Version", _p("I", 202)),
                        _n("TextureName", _p("S", f"Texture::{texture_path.stem}")),
                        _n("Media", _p("S", f"Video::{texture_path.stem}")),
                        _n("FileName", _p("S", str(texture_path))),
                        _n("RelativeFilename", _p("S", relative)),
                        _n("ModelUVTranslation", _p("D", 0.0), _p("D", 0.0)),
                        _n("ModelUVScaling", _p("D", 1.0), _p("D", 1.0)),
                        _n("Texture_Alpha_Source", _p("S", "None")),
                        _n("Cropping", _p("I", 0), _p("I", 0), _p("I", 0), _p("I", 0)),
                    ),
                )
            )

    stop_time = int(round((frame_count - 1) * FBX_TIME_SECOND / fps))
    objects.extend(
        (
            _n(
                "AnimationStack",
                _p("L", stack_id),
                _p("S", "AnimStack::Take 001"),
                _p("S", ""),
                children=(
                    _n(
                        "Properties70",
                        children=(
                            _property70("LocalStart", "KTime", "Time", "", 0),
                            _property70("LocalStop", "KTime", "Time", "", stop_time),
                            _property70("ReferenceStart", "KTime", "Time", "", 0),
                            _property70("ReferenceStop", "KTime", "Time", "", stop_time),
                        ),
                    ),
                ),
            ),
            _n(
                "AnimationLayer",
                _p("L", layer_id),
                _p("S", "AnimLayer::BaseLayer"),
                _p("S", ""),
                children=(_n("Properties70"),),
            ),
        )
    )

    connections: list[_Node] = [
        _n("C", _p("S", "OO"), _p("L", geometry_id), _p("L", mesh_model_id)),
        _n("C", _p("S", "OO"), _p("L", mesh_model_id), _p("L", 0)),
        _n("C", _p("S", "OO"), _p("L", skin_id), _p("L", geometry_id)),
        _n("C", _p("S", "OO"), _p("L", layer_id), _p("L", stack_id)),
    ]
    for index in range(bone_count):
        parent = int(parents[index])
        parent_id = bone_model_ids[parent] if 0 <= parent < bone_count and parent != index else 0
        connections.extend(
            (
                _n("C", _p("S", "OO"), _p("L", bone_attribute_ids[index]), _p("L", bone_model_ids[index])),
                _n("C", _p("S", "OO"), _p("L", bone_model_ids[index]), _p("L", parent_id)),
                _n("C", _p("S", "OO"), _p("L", cluster_ids[index]), _p("L", skin_id)),
                _n("C", _p("S", "OO"), _p("L", bone_model_ids[index]), _p("L", cluster_ids[index])),
            )
        )
    for index, material_id in enumerate(material_ids):
        connections.append(_n("C", _p("S", "OO"), _p("L", material_id), _p("L", mesh_model_id)))
        if texture_ids[index] is not None and video_ids[index] is not None:
            connections.extend(
                (
                    _n("C", _p("S", "OO"), _p("L", video_ids[index]), _p("L", texture_ids[index])),
                    _n("C", _p("S", "OP"), _p("L", texture_ids[index]), _p("L", material_id), _p("S", "DiffuseColor")),
                )
            )

    times = np.rint(np.arange(frame_count, dtype=np.float64) * FBX_TIME_SECOND / fps).astype(np.int64)
    animated_groups = 0
    for bone_index, name in enumerate(bone_names):
        defaults_and_frames = (
            ("T", "Lcl Translation", bind_translation[bone_index], frame_translation[:, bone_index]),
            ("R", "Lcl Rotation", bind_rotation[bone_index], frame_rotation[:, bone_index]),
            ("S", "Lcl Scaling", bind_scaling[bone_index], frame_scaling[:, bone_index]),
        )
        for kind, property_name, default, values in defaults_and_frames:
            if np.allclose(values, default[None, :], rtol=0.0, atol=1.0e-6):
                continue
            curve_node_id, curve_node = _curve_node(ids, name, kind, values[0])
            objects.append(curve_node)
            connections.extend(
                (
                    _n("C", _p("S", "OO"), _p("L", curve_node_id), _p("L", layer_id)),
                    _n("C", _p("S", "OP"), _p("L", curve_node_id), _p("L", bone_model_ids[bone_index]), _p("S", property_name)),
                )
            )
            for axis_index, axis in enumerate("XYZ"):
                curve_id, curve = _curve(ids, f"{name}_{kind}_{axis}", times, values[:, axis_index])
                objects.append(curve)
                connections.append(
                    _n("C", _p("S", "OP"), _p("L", curve_id), _p("L", curve_node_id), _p("S", f"d|{axis}"))
                )
            animated_groups += 1
        if progress and (bone_index % 25 == 0 or bone_index + 1 == bone_count):
            progress("写入骨骼动画", bone_index + 1, bone_count)

    object_type_counts = {
        "Geometry": 1,
        "Model": bone_count + 1,
        "NodeAttribute": bone_count,
        "Deformer": bone_count + 1,
        "Pose": 1,
        "Material": len(material_ids),
        "Texture": sum(value is not None for value in texture_ids),
        "Video": sum(value is not None for value in video_ids),
        "AnimationStack": 1,
        "AnimationLayer": 1,
        "AnimationCurveNode": animated_groups,
        "AnimationCurve": animated_groups * 3,
        "Document": 1,
    }
    definition_children = [_n("Version", _p("I", 100))]
    for object_type, count in object_type_counts.items():
        if count:
            definition_children.append(
                _n("ObjectType", _p("S", object_type), children=(_n("Count", _p("I", count)),))
            )

    now = _datetime.datetime.now()
    nodes = [
        _n(
            "FBXHeaderExtension",
            children=(
                _n("FBXHeaderVersion", _p("I", 1003)),
                _n("FBXVersion", _p("I", FBX_VERSION)),
                _n("EncryptionType", _p("I", 0)),
                _n(
                    "CreationTimeStamp",
                    children=(
                        _n("Version", _p("I", 1000)),
                        _n("Year", _p("I", now.year)),
                        _n("Month", _p("I", now.month)),
                        _n("Day", _p("I", now.day)),
                        _n("Hour", _p("I", now.hour)),
                        _n("Minute", _p("I", now.minute)),
                        _n("Second", _p("I", now.second)),
                        _n("Millisecond", _p("I", now.microsecond // 1000)),
                    ),
                ),
                _n("Creator", _p("S", "Onmyoji PMX Motion Tool")),
            ),
        ),
        _n("FileId", _p("R", bytes.fromhex("28b32aebb624ccc2bfc8b02aa92bfcf1"))),
        _n("CreationTime", _p("S", now.isoformat(timespec="milliseconds"))),
        _n("Creator", _p("S", "Onmyoji PMX Motion Tool")),
        _n(
            "GlobalSettings",
            children=(
                _n("Version", _p("I", 1000)),
                _n(
                    "Properties70",
                    children=(
                        _property70("UpAxis", "int", "Integer", "", 1),
                        _property70("UpAxisSign", "int", "Integer", "", 1),
                        _property70("FrontAxis", "int", "Integer", "", 2),
                        _property70("FrontAxisSign", "int", "Integer", "", -1),
                        _property70("CoordAxis", "int", "Integer", "", 0),
                        _property70("CoordAxisSign", "int", "Integer", "", 1),
                        _property70("OriginalUpAxis", "int", "Integer", "", 1),
                        _property70("OriginalUpAxisSign", "int", "Integer", "", 1),
                        _property70("UnitScaleFactor", "double", "Number", "", 1.0),
                        _property70("OriginalUnitScaleFactor", "double", "Number", "", 1.0),
                        _property70("TimeMode", "enum", "", "", 14),
                        _property70("CustomFrameRate", "double", "Number", "", fps),
                    ),
                ),
            ),
        ),
        _n(
            "Documents",
            children=(
                _n("Count", _p("I", 1)),
                _n(
                    "Document",
                    _p("L", document_id),
                    _p("S", "Document::Scene"),
                    _p("S", "Scene"),
                    children=(
                        _n(
                            "Properties70",
                            children=(
                                _property70("SourceObject", "object", "", ""),
                                _property70("ActiveAnimStackName", "KString", "", "", "Take 001"),
                            ),
                        ),
                        _n("RootNode", _p("L", 0)),
                    ),
                ),
            ),
        ),
        _n("References"),
        _n("Definitions", children=definition_children),
        _n("Objects", children=objects),
        _n("Connections", children=connections),
        _n(
            "Takes",
            children=(
                _n("Current", _p("S", "Take 001")),
                _n(
                    "Take",
                    _p("S", "Take 001"),
                    children=(
                        _n("FileName", _p("S", "Take_001.tak")),
                        _n("LocalTime", _p("L", 0), _p("L", stop_time)),
                        _n("ReferenceTime", _p("L", 0), _p("L", stop_time)),
                    ),
                ),
            ),
        ),
    ]
    payload = _binary_document(nodes)
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(output_path)
    if progress:
        progress("FBX 写入完成", 1, 1)
    return output_path
