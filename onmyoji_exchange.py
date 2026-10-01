# -*- coding: utf-8 -*-
"""Dependency-free COLLADA DAE and binary STL exporters."""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Iterable

import numpy as np


COLLADA_NS = "http://www.collada.org/2005/11/COLLADASchema"
ET.register_namespace("", COLLADA_NS)


def _tag(name: str) -> str:
    return f"{{{COLLADA_NS}}}{name}"


def _safe_ids(values: Iterable[str], prefix: str) -> list[str]:
    result: list[str] = []
    used: set[str] = set()
    for index, value in enumerate(values):
        base = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_.-")
        if not base or not re.match(r"[A-Za-z_]", base):
            base = f"{prefix}_{index:04d}_{base}"
        candidate = base
        suffix = 2
        while candidate.lower() in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used.add(candidate.lower())
        result.append(candidate)
    return result


def _floats(values: object) -> str:
    array = np.asarray(values, dtype=np.float64).reshape(-1)
    return " ".join(f"{float(value):.9g}" for value in array)


def _source(
    parent: ET.Element,
    source_id: str,
    values: object,
    *,
    stride: int,
    params: tuple[str, ...],
    kind: str = "float",
) -> None:
    source = ET.SubElement(parent, _tag("source"), id=source_id)
    array = np.asarray(values).reshape(-1)
    array_id = f"{source_id}-array"
    if kind == "name":
        element = ET.SubElement(
            source, _tag("Name_array"), id=array_id, count=str(len(array))
        )
        element.text = " ".join(str(value) for value in array)
    else:
        element = ET.SubElement(
            source, _tag("float_array"), id=array_id, count=str(len(array))
        )
        element.text = _floats(array)
    technique = ET.SubElement(source, _tag("technique_common"))
    accessor = ET.SubElement(
        technique,
        _tag("accessor"),
        source=f"#{array_id}",
        count=str(len(array) // stride),
        stride=str(stride),
    )
    for param in params:
        ET.SubElement(
            accessor,
            _tag("param"),
            name=param,
            type="Name" if kind == "name" else "float4x4" if stride == 16 else "float",
        )


def _matrix_text(row_matrix: np.ndarray) -> str:
    # COLLADA stores column-vector matrices in column-major order.  Flattening
    # the equivalent row-vector matrix produces that exact byte order.
    return _floats(np.asarray(row_matrix, dtype=np.float64).reshape(4, 4))


def write_binary_stl(
    output_path: Path,
    positions: np.ndarray,
    triangles: np.ndarray,
    *,
    solid_name: str = "Onmyoji posed mesh",
) -> Path:
    """Write one already-skinned mesh pose as binary STL."""
    vertices = np.asarray(positions, dtype=np.float32).reshape(-1, 3)
    faces = np.asarray(triangles, dtype=np.int64).reshape(-1, 3)
    if len(faces) and (faces.min() < 0 or faces.max() >= len(vertices)):
        raise ValueError("STL triangle index is outside the vertex array")
    points = vertices[faces]
    valid = np.isfinite(points).all(axis=(1, 2))
    points = points[valid]
    edges_a = points[:, 1] - points[:, 0]
    edges_b = points[:, 2] - points[:, 0]
    normals = np.cross(edges_a, edges_b)
    lengths = np.linalg.norm(normals, axis=1)
    normals = np.divide(
        normals,
        lengths[:, None],
        out=np.zeros_like(normals),
        where=lengths[:, None] > 1.0e-12,
    )
    records = np.zeros(
        len(points),
        dtype=np.dtype(
            [
                ("normal", "<f4", (3,)),
                ("vertices", "<f4", (3, 3)),
                ("attribute", "<u2"),
            ]
        ),
    )
    records["normal"] = normals
    records["vertices"] = points
    header = solid_name.encode("ascii", errors="replace")[:80].ljust(80, b"\0")
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(header)
        stream.write(struct.pack("<I", len(records)))
        stream.write(records.tobytes())
    temporary.replace(output_path)
    return output_path


def write_animated_dae(
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
    bind_locals: np.ndarray,
    frame_locals: np.ndarray,
    fps: float,
    progress: Callable[[str, int, int], None] | None = None,
) -> Path:
    """Write a COLLADA 1.4.1 skinned mesh with baked matrix animation."""
    positions = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
    normals = np.asarray(normals, dtype=np.float64).reshape(-1, 3)
    uvs = np.asarray(uvs, dtype=np.float64).reshape(-1, 2)
    triangles = np.asarray(triangles, dtype=np.int32).reshape(-1, 3)
    face_materials = np.asarray(face_materials, dtype=np.int32).reshape(-1)
    joints = np.asarray(joints, dtype=np.int32).reshape(len(positions), -1)
    weights = np.asarray(weights, dtype=np.float64).reshape(len(positions), -1)
    bind_globals = np.asarray(bind_globals, dtype=np.float64).reshape(-1, 4, 4)
    bind_locals = np.asarray(bind_locals, dtype=np.float64).reshape(-1, 4, 4)
    frame_locals = np.asarray(frame_locals, dtype=np.float64)
    parents = tuple(int(value) for value in parents)
    bone_count = len(bone_names)
    frame_count = len(frame_locals)
    if fps <= 0.0 or frame_count == 0:
        raise ValueError("DAE animation requires at least one frame and a positive FPS")
    if not (
        len(normals) == len(uvs) == len(positions) == len(joints) == len(weights)
        and len(face_materials) == len(triangles)
        and len(bind_globals) == len(bind_locals) == bone_count
        and frame_locals.shape == (frame_count, bone_count, 4, 4)
    ):
        raise ValueError("DAE geometry, skeleton, or animation array sizes do not match")

    model_id = _safe_ids((f"Model_{model_name}",), "Model")[0]
    bone_ids = _safe_ids(
        (f"Bone_{index:04d}_{name}" for index, name in enumerate(bone_names)),
        "Bone",
    )
    material_ids = _safe_ids(
        (
            f"Material_{index:03d}_{value.get('name') or ''}"
            for index, value in enumerate(material_defs)
        ),
        "Material",
    )
    root = ET.Element(_tag("COLLADA"), version="1.4.1")
    asset = ET.SubElement(root, _tag("asset"))
    ET.SubElement(asset, _tag("contributor"))
    ET.SubElement(asset, _tag("unit"), name="centimeter", meter="0.01")
    ET.SubElement(asset, _tag("up_axis")).text = "Y_UP"

    images = ET.SubElement(root, _tag("library_images"))
    effects = ET.SubElement(root, _tag("library_effects"))
    materials = ET.SubElement(root, _tag("library_materials"))
    for index, definition in enumerate(material_defs):
        material_id = material_ids[index]
        texture = str(definition.get("relative_texture") or "").replace("\\", "/")
        image_id = f"{material_id}-image"
        if texture:
            image = ET.SubElement(images, _tag("image"), id=image_id, name=image_id)
            ET.SubElement(image, _tag("init_from")).text = texture
        effect_id = f"{material_id}-effect"
        effect = ET.SubElement(effects, _tag("effect"), id=effect_id, name=effect_id)
        profile = ET.SubElement(effect, _tag("profile_COMMON"))
        if texture:
            surface_sid = f"{material_id}-surface"
            sampler_sid = f"{material_id}-sampler"
            newparam = ET.SubElement(profile, _tag("newparam"), sid=surface_sid)
            surface = ET.SubElement(newparam, _tag("surface"), type="2D")
            ET.SubElement(surface, _tag("init_from")).text = image_id
            newparam = ET.SubElement(profile, _tag("newparam"), sid=sampler_sid)
            sampler = ET.SubElement(newparam, _tag("sampler2D"))
            ET.SubElement(sampler, _tag("source")).text = surface_sid
        technique = ET.SubElement(profile, _tag("technique"), sid="common")
        phong = ET.SubElement(technique, _tag("phong"))
        diffuse = ET.SubElement(phong, _tag("diffuse"))
        color = tuple(float(value) for value in definition.get("diffuse", (0.8, 0.8, 0.8, 1.0)))
        if texture:
            ET.SubElement(diffuse, _tag("texture"), texture=f"{material_id}-sampler", texcoord="UVMap")
        else:
            ET.SubElement(diffuse, _tag("color")).text = _floats(color)
        material = ET.SubElement(materials, _tag("material"), id=material_id, name=str(definition.get("name") or material_id))
        ET.SubElement(material, _tag("instance_effect"), url=f"#{effect_id}")

    geometries = ET.SubElement(root, _tag("library_geometries"))
    geometry_id = f"{model_id}-geometry"
    geometry = ET.SubElement(geometries, _tag("geometry"), id=geometry_id, name=model_name)
    mesh = ET.SubElement(geometry, _tag("mesh"))
    _source(mesh, f"{geometry_id}-positions", positions, stride=3, params=("X", "Y", "Z"))
    _source(mesh, f"{geometry_id}-normals", normals, stride=3, params=("X", "Y", "Z"))
    _source(mesh, f"{geometry_id}-uvs", uvs, stride=2, params=("S", "T"))
    vertices = ET.SubElement(mesh, _tag("vertices"), id=f"{geometry_id}-vertices")
    ET.SubElement(vertices, _tag("input"), semantic="POSITION", source=f"#{geometry_id}-positions")
    material_count = max(1, len(material_defs))
    for material_index in range(material_count):
        selected = triangles[face_materials == material_index]
        if not len(selected):
            continue
        attrs = {"count": str(len(selected))}
        if material_defs:
            attrs["material"] = f"{material_ids[material_index]}-symbol"
        triangle_node = ET.SubElement(mesh, _tag("triangles"), **attrs)
        ET.SubElement(triangle_node, _tag("input"), semantic="VERTEX", source=f"#{geometry_id}-vertices", offset="0")
        ET.SubElement(triangle_node, _tag("input"), semantic="NORMAL", source=f"#{geometry_id}-normals", offset="1")
        ET.SubElement(triangle_node, _tag("input"), semantic="TEXCOORD", source=f"#{geometry_id}-uvs", offset="2", set="0")
        flat = selected.reshape(-1)
        ET.SubElement(triangle_node, _tag("p")).text = " ".join(
            str(int(index)) for vertex in flat for index in (vertex, vertex, vertex)
        )

    controllers = ET.SubElement(root, _tag("library_controllers"))
    controller_id = f"{model_id}-skin"
    controller = ET.SubElement(controllers, _tag("controller"), id=controller_id)
    skin = ET.SubElement(controller, _tag("skin"), source=f"#{geometry_id}")
    ET.SubElement(skin, _tag("bind_shape_matrix")).text = _matrix_text(np.eye(4))
    _source(skin, f"{controller_id}-joints", bone_ids, stride=1, params=("JOINT",), kind="name")
    inverse_bind = np.linalg.inv(bind_globals)
    _source(skin, f"{controller_id}-bindposes", inverse_bind, stride=16, params=("TRANSFORM",))
    flat_weight_values: list[float] = []
    vertex_pairs: list[int] = []
    vertex_counts: list[int] = []
    for vertex_joints, vertex_weights in zip(joints, weights):
        count = 0
        for joint, weight in zip(vertex_joints, vertex_weights):
            if 0 <= int(joint) < bone_count and float(weight) > 1.0e-8:
                weight_index = len(flat_weight_values)
                flat_weight_values.append(float(weight))
                vertex_pairs.extend((int(joint), weight_index))
                count += 1
        vertex_counts.append(count)
    _source(skin, f"{controller_id}-weights", flat_weight_values or (0.0,), stride=1, params=("WEIGHT",))
    joint_inputs = ET.SubElement(skin, _tag("joints"))
    ET.SubElement(joint_inputs, _tag("input"), semantic="JOINT", source=f"#{controller_id}-joints")
    ET.SubElement(joint_inputs, _tag("input"), semantic="INV_BIND_MATRIX", source=f"#{controller_id}-bindposes")
    vertex_weights_node = ET.SubElement(skin, _tag("vertex_weights"), count=str(len(positions)))
    ET.SubElement(vertex_weights_node, _tag("input"), semantic="JOINT", source=f"#{controller_id}-joints", offset="0")
    ET.SubElement(vertex_weights_node, _tag("input"), semantic="WEIGHT", source=f"#{controller_id}-weights", offset="1")
    ET.SubElement(vertex_weights_node, _tag("vcount")).text = " ".join(map(str, vertex_counts))
    ET.SubElement(vertex_weights_node, _tag("v")).text = " ".join(map(str, vertex_pairs))

    animations = ET.SubElement(root, _tag("library_animations"))
    times = np.arange(frame_count, dtype=np.float64) / float(fps)
    for bone_index, bone_id in enumerate(bone_ids):
        animation_id = f"{bone_id}-animation"
        animation = ET.SubElement(animations, _tag("animation"), id=animation_id)
        _source(animation, f"{animation_id}-input", times, stride=1, params=("TIME",))
        _source(animation, f"{animation_id}-output", frame_locals[:, bone_index], stride=16, params=("TRANSFORM",))
        _source(animation, f"{animation_id}-interpolation", ("LINEAR",) * frame_count, stride=1, params=("INTERPOLATION",), kind="name")
        sampler_id = f"{animation_id}-sampler"
        sampler = ET.SubElement(animation, _tag("sampler"), id=sampler_id)
        ET.SubElement(sampler, _tag("input"), semantic="INPUT", source=f"#{animation_id}-input")
        ET.SubElement(sampler, _tag("input"), semantic="OUTPUT", source=f"#{animation_id}-output")
        ET.SubElement(sampler, _tag("input"), semantic="INTERPOLATION", source=f"#{animation_id}-interpolation")
        ET.SubElement(animation, _tag("channel"), source=f"#{sampler_id}", target=f"{bone_id}/transform")
        if progress and (bone_index % 25 == 0 or bone_index + 1 == bone_count):
            progress("写入 DAE 骨骼动画", bone_index + 1, bone_count)
    clips = ET.SubElement(root, _tag("library_animation_clips"))
    clip = ET.SubElement(
        clips,
        _tag("animation_clip"),
        id=f"{model_id}-clip",
        name="Take 001",
        start="0",
        end=f"{times[-1]:.9g}",
    )
    for bone_id in bone_ids:
        ET.SubElement(clip, _tag("instance_animation"), url=f"#{bone_id}-animation")

    visual_scenes = ET.SubElement(root, _tag("library_visual_scenes"))
    scene_id = f"{model_id}-scene"
    visual_scene = ET.SubElement(visual_scenes, _tag("visual_scene"), id=scene_id, name="Scene")
    children: dict[int, list[int]] = {index: [] for index in range(bone_count)}
    roots: list[int] = []
    for index, parent in enumerate(parents):
        if 0 <= parent < bone_count and parent != index:
            children[parent].append(index)
        else:
            roots.append(index)

    def add_bone(parent_node: ET.Element, index: int) -> None:
        node = ET.SubElement(
            parent_node,
            _tag("node"),
            id=bone_ids[index],
            name=str(bone_names[index]),
            sid=bone_ids[index],
            type="JOINT",
        )
        ET.SubElement(node, _tag("matrix"), sid="transform").text = _matrix_text(bind_locals[index])
        for child in children[index]:
            add_bone(node, child)

    for root_index in roots:
        add_bone(visual_scene, root_index)
    mesh_node = ET.SubElement(visual_scene, _tag("node"), id=f"{model_id}-node", name=model_name, type="NODE")
    instance = ET.SubElement(mesh_node, _tag("instance_controller"), url=f"#{controller_id}")
    for root_index in roots:
        ET.SubElement(instance, _tag("skeleton")).text = f"#{bone_ids[root_index]}"
    if material_defs:
        bind_material = ET.SubElement(instance, _tag("bind_material"))
        technique = ET.SubElement(bind_material, _tag("technique_common"))
        for index, material_id in enumerate(material_ids):
            instance_material = ET.SubElement(
                technique,
                _tag("instance_material"),
                symbol=f"{material_id}-symbol",
                target=f"#{material_id}",
            )
            ET.SubElement(instance_material, _tag("bind_vertex_input"), semantic="UVMap", input_semantic="TEXCOORD", input_set="0")
    scene = ET.SubElement(root, _tag("scene"))
    ET.SubElement(scene, _tag("instance_visual_scene"), url=f"#{scene_id}")

    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    ET.ElementTree(root).write(temporary, encoding="utf-8", xml_declaration=True)
    temporary.replace(output_path)
    if progress:
        progress("DAE 写入完成", 1, 1)
    return output_path
