# -*- coding: utf-8 -*-
"""Convert generated Onmyoji PMX skeletons to common MMD bone conventions."""

from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path


REPORT_NAME = "MMD骨架兼容报告.json"
_FULLWIDTH_DIGITS = str.maketrans("0123456789", "０１２３４５６７８９")


@dataclass(slots=True)
class MmdCompatibilityReport:
    source: str
    output: str
    renamed_bones: dict[str, str]
    added_bones: list[str]
    missing_standard_bones: list[str]
    ik_bones: list[str]
    reattached_bones: dict[str, str]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).lower()
    return "".join(character for character in value if character.isalnum())


_ROLE_DEFINITIONS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("all_parent", "全ての親", "master", ("virtualbone", "allparent", "master", "root")),
    ("center", "センター", "center", ("bip01", "center", "centre")),
    ("lower", "下半身", "lower body", ("bip01pelvis", "pelvis", "hips", "lowerbody")),
    ("upper", "上半身", "upper body", ("bip01spine", "spine", "upperbody")),
    ("upper2", "上半身2", "upper body 2", ("bip01spine1", "spine1", "chest", "upperbody2")),
    ("upper3", "上半身3", "upper body 3", ("bip01spine2", "spine2", "upperchest", "upperbody3")),
    ("neck", "首", "neck", ("bip01neck", "neck")),
    ("neck2", "首2", "neck 2", ("bip01neck1", "neck1")),
    ("head", "頭", "head", ("bip01head", "head")),
    ("left_shoulder", "左肩", "shoulder_L", ("bip01lclavicle", "leftshoulder", "lclavicle")),
    ("left_arm", "左腕", "arm_L", ("bip01lupperarm", "leftarm", "lupperarm")),
    ("left_elbow", "左ひじ", "elbow_L", ("bip01lforearm", "leftforearm", "lforearm", "leftelbow")),
    ("left_wrist", "左手首", "wrist_L", ("bip01lhand", "lefthand", "lhand", "leftwrist")),
    ("right_shoulder", "右肩", "shoulder_R", ("bip01rclavicle", "rightshoulder", "rclavicle")),
    ("right_arm", "右腕", "arm_R", ("bip01rupperarm", "rightarm", "rupperarm")),
    ("right_elbow", "右ひじ", "elbow_R", ("bip01rforearm", "rightforearm", "rforearm", "rightelbow")),
    ("right_wrist", "右手首", "wrist_R", ("bip01rhand", "righthand", "rhand", "rightwrist")),
    ("left_leg", "左足", "leg_L", ("bip01lthigh", "leftupleg", "leftthigh", "lthigh")),
    ("left_knee", "左ひざ", "knee_L", ("bip01lcalf", "leftleg", "leftcalf", "lknee")),
    ("left_ankle", "左足首", "ankle_L", ("bip01lfoot", "leftfoot", "lfoot")),
    ("left_toe", "左つま先", "toe_L", ("bip01ltoe0", "lefttoe", "lefttoebase", "ltoe")),
    ("right_leg", "右足", "leg_R", ("bip01rthigh", "rightupleg", "rightthigh", "rthigh")),
    ("right_knee", "右ひざ", "knee_R", ("bip01rcalf", "rightleg", "rightcalf", "rknee")),
    ("right_ankle", "右足首", "ankle_R", ("bip01rfoot", "rightfoot", "rfoot")),
    ("right_toe", "右つま先", "toe_R", ("bip01rtoe0", "righttoe", "righttoebase", "rtoe")),
    ("left_eye", "左目", "eye_L", ("kkleyes", "lefteye", "leye")),
    ("right_eye", "右目", "eye_R", ("kkreyes", "righteye", "reye")),
)

_STANDARD_BY_ROLE = {role: japanese for role, japanese, _english, _aliases in _ROLE_DEFINITIONS}
_ENGLISH_BY_ROLE = {role: english for role, _japanese, english, _aliases in _ROLE_DEFINITIONS}
_ALIASES_BY_ROLE = {
    role: frozenset(_key(value) for value in (japanese, english, *aliases))
    for role, japanese, english, aliases in _ROLE_DEFINITIONS
}


def _role_indices(model) -> dict[str, int]:
    keys = [_key(bone.name) for bone in model.bones]
    result: dict[str, int] = {}
    # An existing standard Japanese name always wins over a broad source alias.
    for role, japanese, _english, _aliases in _ROLE_DEFINITIONS:
        standard_key = _key(japanese)
        index = next((i for i, value in enumerate(keys) if value == standard_key), None)
        if index is None:
            aliases = _ALIASES_BY_ROLE[role]
            index = next((i for i, value in enumerate(keys) if value in aliases), None)
        if index is not None and index not in result.values():
            result[role] = index
    return result


def _finger_standard_name(name: str) -> tuple[str, str] | None:
    key = _key(name)
    match = re.fullmatch(r"(?:bip01)?([lr])finger([0-4])(0?[12])?", key)
    if match is None:
        return None
    side, finger, suffix = match.groups()
    japanese_side = "左" if side == "l" else "右"
    english_side = "L" if side == "l" else "R"
    finger_names = {
        "0": ("親指", "thumb"),
        "1": ("人指", "index"),
        "2": ("中指", "middle"),
        "3": ("薬指", "ring"),
        "4": ("小指", "little"),
    }
    japanese_finger, english_finger = finger_names[finger]
    if finger == "0":
        segment = {None: 0, "01": 1, "1": 1, "02": 2, "2": 2}.get(suffix)
    else:
        segment = {None: 1, "01": 2, "1": 2, "02": 3, "2": 3}.get(suffix)
    if segment is None:
        return None
    return (
        f"{japanese_side}{japanese_finger}{str(segment).translate(_FULLWIDTH_DIGITS)}",
        f"{english_finger}{segment}_{english_side}",
    )


def _twist_standard_name(name: str) -> tuple[str, str] | None:
    key = _key(name)
    match = re.fullmatch(
        r"(?:bone|bip01)?([lr])(?:upperarmtwist|armtwist|forearmtwist|foretwist)(\d*)",
        key,
    )
    if match is None:
        return None
    side, number = match.groups()
    is_wrist = "fore" in key
    japanese_side = "左" if side == "l" else "右"
    english_side = "L" if side == "l" else "R"
    stem = "手捩" if is_wrist else "腕捩"
    english_stem = "wrist twist" if is_wrist else "arm twist"
    # The first source twist is the standard controller; descendants keep a suffix.
    suffix = ""
    if number and int(number) > 0:
        suffix = str(int(number))
    return f"{japanese_side}{stem}{suffix}", f"{english_stem}{suffix}_{english_side}"


def _rename_standard_bones(model, roles: dict[str, int]) -> dict[str, str]:
    renamed: dict[str, str] = {}
    occupied = {bone.name for bone in model.bones}
    for role, index in roles.items():
        target = _STANDARD_BY_ROLE[role]
        bone = model.bones[index]
        if bone.name != target and target not in occupied:
            old_name = bone.name
            occupied.discard(old_name)
            bone.name = target
            bone.english_name = _ENGLISH_BY_ROLE[role]
            occupied.add(target)
            renamed[old_name] = target

    for bone in model.bones:
        target = _finger_standard_name(bone.name) or _twist_standard_name(bone.name)
        if target is None or target[0] in occupied:
            continue
        old_name = bone.name
        occupied.discard(old_name)
        bone.name, bone.english_name = target
        occupied.add(bone.name)
        renamed[old_name] = bone.name
    return renamed


def _new_bone(pmx, common, name: str, english_name: str, position, parent: int, *, ik=None):
    flag = (
        pmx.BONEFLAG_CAN_ROTATE
        | pmx.BONEFLAG_IS_VISIBLE
        | pmx.BONEFLAG_CAN_MANIPULATE
    )
    if name in {"全ての親", "センター", "グルーブ", "左足ＩＫ", "右足ＩＫ", "左つま先ＩＫ", "右つま先ＩＫ"}:
        flag |= pmx.BONEFLAG_CAN_TRANSLATE
    if ik is not None:
        flag |= pmx.BONEFLAG_IS_IK
    return pmx.Bone(
        name=name,
        english_name=english_name,
        position=common.Vector3(position.x, position.y, position.z),
        parent_index=parent,
        layer=0,
        flag=flag,
        tail_position=common.Vector3(0.0, -1.0, 0.0),
        ik=ik,
    )


def _append_helper(model, pmx, common, name: str, english: str, position, parent: int, *, ik=None) -> int:
    existing = next((i for i, bone in enumerate(model.bones) if bone.name == name), None)
    if existing is not None:
        return existing
    model.bones.append(_new_bone(pmx, common, name, english, position, parent, ik=ik))
    return len(model.bones) - 1


def _set_flag(bone, flag: int, enabled: bool = True) -> None:
    bone.setFlag(flag, enabled)


def _weighted_bone_indices(model) -> set[int]:
    """Return bones that carry a non-zero vertex weight."""
    weighted: set[int] = set()
    for vertex in model.vertices:
        deform = vertex.deform
        if hasattr(deform, "index3"):
            for number in range(4):
                if float(getattr(deform, f"weight{number}")) > 1.0e-8:
                    weighted.add(int(getattr(deform, f"index{number}")))
        elif hasattr(deform, "index1"):
            weight0 = float(deform.weight0)
            if weight0 > 1.0e-8:
                weighted.add(int(deform.index0))
            if 1.0 - weight0 > 1.0e-8:
                weighted.add(int(deform.index1))
        elif hasattr(deform, "index0"):
            weighted.add(int(deform.index0))
    return {index for index in weighted if 0 <= index < len(model.bones)}


def _distance(left, right) -> float:
    return math.sqrt(
        (left.x - right.x) ** 2
        + (left.y - right.y) ** 2
        + (left.z - right.z) ** 2
    )


def _reattach_weighted_root_branches(
    model,
    roles: dict[str, int],
) -> dict[str, str]:
    """Attach independently rooted skinned accessories to the standardized body.

    Some NeoX character skeletons animate wings or large accessories as a second
    child of the source root.  Once ``bip01`` becomes MMD's center, leaving that
    branch under ``全ての親`` makes it stay in world space during ordinary VMD
    center motion.  Only branches proven to carry vertex weights are changed.
    """
    all_parent = roles.get("all_parent")
    center = roles.get("center")
    if all_parent is None or center is None:
        return {}

    weighted = _weighted_bone_indices(model)
    branch_roots: set[int] = set()
    for bone_index in weighted:
        current = bone_index
        visited: set[int] = set()
        while 0 <= current < len(model.bones) and current not in visited:
            visited.add(current)
            parent = int(model.bones[current].parent_index)
            if parent == all_parent:
                branch_roots.add(current)
                break
            if parent < 0:
                break
            current = parent

    branch_roots.discard(center)
    torso = next(
        (roles[role] for role in ("upper3", "upper2", "upper") if role in roles),
        center,
    )
    lower = roles.get("lower", center)
    head = roles.get("head", torso)
    extremities = [
        roles[role]
        for role in ("left_wrist", "right_wrist", "left_ankle", "right_ankle")
        if role in roles
    ]
    lower_y = model.bones[lower].position.y
    head_y = model.bones[head].position.y
    reattached: dict[str, str] = {}
    for branch_root in sorted(branch_roots):
        bone = model.bones[branch_root]
        position = bone.position
        if position.y >= head_y - 0.25:
            anchor = head
        elif position.y >= lower_y + 0.25:
            anchor = torso
        else:
            anchor = lower

        default_distance = _distance(position, model.bones[anchor].position)
        if extremities:
            nearest = min(
                extremities,
                key=lambda index: _distance(position, model.bones[index].position),
            )
            nearest_distance = _distance(position, model.bones[nearest].position)
            if nearest_distance < default_distance * 0.65:
                anchor = nearest

        bone.parent_index = anchor
        reattached[bone.name] = model.bones[anchor].name
    return reattached


def _add_standard_helpers(model, roles: dict[str, int]) -> tuple[list[str], list[str]]:
    import pymeshio.common as common
    import pymeshio.pmx as pmx

    added: list[str] = []
    ik_names: list[str] = []
    zero = common.Vector3(0.0, 0.0, 0.0)

    all_parent = roles.get("all_parent")
    if all_parent is None:
        all_parent = _append_helper(model, pmx, common, "全ての親", "master", zero, -1)
        roles["all_parent"] = all_parent
        added.append("全ての親")
    all_bone = model.bones[all_parent]
    all_bone.parent_index = -1
    _set_flag(all_bone, pmx.BONEFLAG_CAN_TRANSLATE)

    center = roles.get("center")
    if center is None:
        anchor_index = roles.get("lower")
        anchor = model.bones[anchor_index].position if anchor_index is not None else zero
        center = _append_helper(model, pmx, common, "センター", "center", anchor, all_parent)
        roles["center"] = center
        added.append("センター")
    center_bone = model.bones[center]
    center_bone.parent_index = all_parent
    _set_flag(center_bone, pmx.BONEFLAG_CAN_TRANSLATE)

    groove = next((i for i, bone in enumerate(model.bones) if bone.name == "グルーブ"), None)
    if groove is None:
        groove = _append_helper(model, pmx, common, "グルーブ", "groove", center_bone.position, center)
        added.append("グルーブ")
    model.bones[groove].parent_index = center
    _set_flag(model.bones[groove], pmx.BONEFLAG_CAN_TRANSLATE)

    waist = next((i for i, bone in enumerate(model.bones) if bone.name == "腰"), None)
    if waist is None:
        waist_anchor = model.bones[roles["lower"]].position if "lower" in roles else center_bone.position
        waist = _append_helper(model, pmx, common, "腰", "waist", waist_anchor, groove)
        added.append("腰")
    model.bones[waist].parent_index = groove

    for role in ("lower", "upper"):
        index = roles.get(role)
        if index is not None:
            model.bones[index].parent_index = waist

    torso_parent = next(
        (roles[role] for role in ("upper3", "upper2", "upper") if role in roles),
        groove,
    )
    for role in ("neck", "left_shoulder", "right_shoulder"):
        index = roles.get(role)
        if index is not None:
            model.bones[index].parent_index = torso_parent
    if "neck2" in roles and "neck" in roles:
        model.bones[roles["neck2"]].parent_index = roles["neck"]
    if "head" in roles:
        head_parent = roles.get("neck2", roles.get("neck", torso_parent))
        model.bones[roles["head"]].parent_index = head_parent

    for side in ("left", "right"):
        leg = roles.get(f"{side}_leg")
        lower = roles.get("lower")
        if leg is not None and lower is not None:
            model.bones[leg].parent_index = lower

        ankle = roles.get(f"{side}_ankle")
        knee = roles.get(f"{side}_knee")
        thigh = roles.get(f"{side}_leg")
        if ankle is None or knee is None or thigh is None:
            continue
        japanese_side = "左" if side == "left" else "右"
        english_side = "L" if side == "left" else "R"
        ik_name = f"{japanese_side}足ＩＫ"
        ik = pmx.Ik(
            target_index=ankle,
            loop=40,
            limit_radian=math.radians(2.0),
            link=[
                pmx.IkLink(
                    knee,
                    1,
                    common.Vector3(-math.pi, 0.0, 0.0),
                    common.Vector3(-math.radians(0.5), 0.0, 0.0),
                ),
                pmx.IkLink(thigh, 0),
            ],
        )
        before = len(model.bones)
        foot_ik = _append_helper(
            model, pmx, common, ik_name, f"leg IK_{english_side}",
            model.bones[ankle].position, all_parent, ik=ik,
        )
        if len(model.bones) > before:
            added.append(ik_name)
        foot_ik_bone = model.bones[foot_ik]
        foot_ik_bone.parent_index = all_parent
        foot_ik_bone.ik = ik
        _set_flag(foot_ik_bone, pmx.BONEFLAG_IS_IK)
        _set_flag(foot_ik_bone, pmx.BONEFLAG_CAN_TRANSLATE)
        ik_names.append(ik_name)

        toe = roles.get(f"{side}_toe")
        if toe is not None:
            toe_name = f"{japanese_side}つま先ＩＫ"
            toe_ik_data = pmx.Ik(
                target_index=toe,
                loop=8,
                limit_radian=math.radians(4.0),
                link=[pmx.IkLink(ankle, 0)],
            )
            before = len(model.bones)
            toe_ik = _append_helper(
                model, pmx, common, toe_name, f"toe IK_{english_side}",
                model.bones[toe].position, foot_ik, ik=toe_ik_data,
            )
            if len(model.bones) > before:
                added.append(toe_name)
            toe_ik_bone = model.bones[toe_ik]
            toe_ik_bone.parent_index = foot_ik
            toe_ik_bone.ik = toe_ik_data
            _set_flag(toe_ik_bone, pmx.BONEFLAG_IS_IK)
            _set_flag(toe_ik_bone, pmx.BONEFLAG_CAN_TRANSLATE)
            ik_names.append(toe_name)

    left_eye = roles.get("left_eye")
    right_eye = roles.get("right_eye")
    if left_eye is not None and right_eye is not None:
        left_position = model.bones[left_eye].position
        right_position = model.bones[right_eye].position
        eye_position = common.Vector3(
            (left_position.x + right_position.x) * 0.5,
            (left_position.y + right_position.y) * 0.5,
            (left_position.z + right_position.z) * 0.5,
        )
        eye_parent = roles.get("head", torso_parent)
        before = len(model.bones)
        both_eyes = _append_helper(model, pmx, common, "両目", "eyes", eye_position, eye_parent)
        if len(model.bones) > before:
            added.append("両目")
        for eye_index in (left_eye, right_eye):
            eye = model.bones[eye_index]
            eye.effect_index = both_eyes
            eye.effect_factor = 1.0
            _set_flag(eye, pmx.BONEFLAG_IS_EXTERNAL_ROTATION)

    return added, ik_names


def _rebuild_display_slots(model) -> None:
    import pymeshio.pmx as pmx

    by_name = {bone.name: index for index, bone in enumerate(model.bones)}
    groups = (
        ("Root", "Root", 1, ("全ての親",)),
        ("表情", "Exp", 1, ()),
        ("センター", "Center", 0, ("センター", "グルーブ", "腰", "下半身")),
        ("体", "Body", 0, ("上半身", "上半身2", "上半身3", "首", "首2", "頭", "両目", "左目", "右目")),
        ("腕", "Arms", 0, ("左肩", "左腕", "左腕捩", "左ひじ", "左手捩", "左手首", "右肩", "右腕", "右腕捩", "右ひじ", "右手捩", "右手首")),
        ("指", "Fingers", 0, tuple(
            f"{side}{finger}{number}"
            for side in ("左", "右")
            for finger in ("親指", "人指", "中指", "薬指", "小指")
            for number in "０１２３"
        )),
        ("足", "Legs", 0, ("左足", "左ひざ", "左足首", "左つま先", "左足ＩＫ", "左つま先ＩＫ", "右足", "右ひざ", "右足首", "右つま先", "右足ＩＫ", "右つま先ＩＫ")),
    )
    used: set[int] = set()
    slots = []
    morph_refs = [
        reference
        for slot in model.display_slots
        for reference in slot.references
        if reference[0] == 1 and 0 <= reference[1] < len(model.morphs)
    ]
    for name, english, special, bone_names in groups:
        references = []
        for bone_name in bone_names:
            index = by_name.get(bone_name)
            if index is not None and index not in used:
                references.append((0, index))
                used.add(index)
        if name == "表情":
            references.extend(dict.fromkeys(morph_refs))
        if references or special:
            slots.append(pmx.DisplaySlot(name, english, special, references))

    remaining = [index for index in range(len(model.bones)) if index not in used]
    for start in range(0, len(remaining), 200):
        number = start // 200 + 1
        slots.append(
            pmx.DisplaySlot(
                f"その他{number}", f"Other{number}", 0,
                [(0, index) for index in remaining[start : start + 200]],
            )
        )
    model.display_slots[:] = slots


def convert_pmx_to_mmd_compatible(source: Path, output: Path) -> MmdCompatibilityReport:
    """Write an MMD-compatible copy without changing vertex weights or materials."""
    import pymeshio.pmx.reader
    import pymeshio.pmx.writer

    source = Path(source).resolve()
    output = Path(output).resolve()
    model = pymeshio.pmx.reader.read_from_file(str(source))
    if not model.bones:
        raise ValueError(f"{source.name} 没有骨骼，无法转换为通用型 MMD PMX")

    roles = _role_indices(model)
    body_roles = {"lower", "upper", "head"}
    limb_roles = {
        f"{side}_{part}"
        for side in ("left", "right")
        for part in ("arm", "elbow", "wrist", "leg", "knee", "ankle")
    }
    if not body_roles.issubset(roles) or len(limb_roles.intersection(roles)) < 8:
        raise ValueError(
            f"{source.name} 未识别到完整的 Bip01/通用人形骨架，不能可靠生成 MMD 标准骨架"
        )
    renamed = _rename_standard_bones(model, roles)
    # Refresh after renaming so standard names are authoritative.
    roles = _role_indices(model)
    added, ik_names = _add_standard_helpers(model, roles)
    reattached = _reattach_weighted_root_branches(model, roles)
    _rebuild_display_slots(model)

    expected = (
        "センター", "グルーブ", "腰", "下半身", "上半身", "首", "頭",
        "左肩", "左腕", "左ひじ", "左手首", "左足", "左ひざ", "左足首",
        "右肩", "右腕", "右ひじ", "右手首", "右足", "右ひざ", "右足首",
    )
    final_names = {bone.name for bone in model.bones}
    missing = [name for name in expected if name not in final_names]
    note = "MMD standard skeleton compatibility: Japanese standard names, center/groove hierarchy and leg IK."
    model.comment = f"{model.comment.rstrip()}\n{note}".strip()
    model.english_comment = f"{model.english_comment.rstrip()}\n{note}".strip()

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".mmdcompat.tmp")
    temporary.unlink(missing_ok=True)
    try:
        pymeshio.pmx.writer.write_to_file(model, str(temporary))
        verified = pymeshio.pmx.reader.read_from_file(str(temporary))
        verified_names = {bone.name for bone in verified.bones}
        if "全ての親" not in verified_names or "センター" not in verified_names:
            raise ValueError("通用型 PMX 写入后验证失败")
        temporary.replace(output)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    return MmdCompatibilityReport(
        source=str(source),
        output=str(output),
        renamed_bones=renamed,
        added_bones=added,
        missing_standard_bones=missing,
        ik_bones=ik_names,
        reattached_bones=reattached,
    )


def convert_model_folder(target: Path) -> list[MmdCompatibilityReport]:
    """Convert every top-level PMX in an already copied model folder in place."""
    target = Path(target).resolve()
    pmx_paths = sorted(target.glob("*.pmx"), key=lambda path: path.name.lower())
    if not pmx_paths:
        raise FileNotFoundError("模型文件夹内没有 PMX 文件。")
    reports = [convert_pmx_to_mmd_compatible(path, path) for path in pmx_paths]
    (target / REPORT_NAME).write_text(
        json.dumps(
            {
                "format": "MMD通用型PMX",
                "schema": 1,
                "models": [report.to_dict() for report in reports],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return reports
