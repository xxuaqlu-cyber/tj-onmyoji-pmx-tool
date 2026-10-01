"""Shared detection and UV mapping for Q-model facial expression atlases."""

from __future__ import annotations

import re
from pathlib import Path


def is_q_face_material(material_name: str, texture_reference: str) -> bool:
    """Return whether a material points at a Q-model expression atlas."""
    normalized_reference = texture_reference.replace("\\", "/")
    token = f"{material_name}/{normalized_reference}".lower()
    return "biaoqing" in token and re.search(r"(?:^|[_/])q_", token) is not None


def has_transparent_fourth_tile(texture_path: Path) -> bool:
    """Return whether the bottom-right tile of a 2x2 atlas is transparent."""
    if not texture_path.is_file():
        return False
    try:
        from PIL import Image

        with Image.open(texture_path) as image:
            rgba = image.convert("RGBA")
            width, height = rgba.size
            if width < 2 or height < 2 or width % 2 or height % 2:
                return False
            alpha = rgba.getchannel("A")
            half_width, half_height = width // 2, height // 2
            fourth = alpha.crop((half_width, half_height, width, height))
            if fourth.getextrema()[1] > 8:
                return False
            other_tiles = (
                alpha.crop((0, 0, half_width, half_height)),
                alpha.crop((half_width, 0, width, half_height)),
                alpha.crop((0, half_height, half_width, height)),
            )
            return any(tile.getextrema()[1] > 8 for tile in other_tiles)
    except Exception:
        return False


def remap_uv_to_transparent_fourth(u: float, v: float) -> tuple[float, float]:
    """Keep local tile coordinates while selecting the bottom-right tile."""
    u = max(0.0, min(1.0, float(u)))
    v = max(0.0, min(1.0, float(v)))
    local_u = (
        1.0
        if u >= 1.0 - 1.0e-7
        else (u - (0.5 if u >= 0.5 else 0.0)) * 2.0
    )
    local_v = (
        1.0
        if v >= 1.0 - 1.0e-7
        else (v - (0.5 if v >= 0.5 else 0.0)) * 2.0
    )
    return (0.5 + local_u * 0.5, 0.5 + local_v * 0.5)
