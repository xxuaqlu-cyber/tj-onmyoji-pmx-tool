from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class GameProfile:
    key: str
    display_name: str
    package_names: tuple[str, ...]
    package_keywords: tuple[str, ...]
    default_package: str
    local_resource_dir: str
    unpacked_dir: str
    rigged_output_dir: str
    settings_filename: str
    manifest_filename: str
    resource_namespaces: tuple[str, ...]
    apk_hints: tuple[str, ...]
    enable_role_classification: bool = False
    supports_old_npk: bool = False


PROFILES: dict[str, GameProfile] = {
    "onmyoji": GameProfile(
        key="onmyoji",
        display_name="阴阳师",
        package_names=(
            "com.netease.onmyoji.wyzymnqsd_cps",
            "com.netease.onmyoji.wyzymnqsd",
            "com.netease.onmyoji",
        ),
        package_keywords=("onmyoji",),
        default_package="com.netease.onmyoji.wyzymnqsd_cps",
        local_resource_dir="yys",
        unpacked_dir="unpacked",
        rigged_output_dir="rigged_models",
        settings_filename=".resource_pull_settings.json",
        manifest_filename=".yys_sync_manifest.json",
        resource_namespaces=("onmyoji",),
        apk_hints=("onmyoji", "阴阳师"),
        enable_role_classification=True,
        supports_old_npk=True,
    ),
    "moba": GameProfile(
        key="moba",
        display_name="决战平安京",
        package_names=("com.netease.moba",),
        package_keywords=("com.netease.moba", "moba"),
        default_package="com.netease.moba",
        local_resource_dir="moba",
        unpacked_dir="unpacked_moba",
        rigged_output_dir="rigged_models_moba",
        settings_filename=".resource_pull_settings_moba.json",
        manifest_filename=".moba_sync_manifest.json",
        resource_namespaces=("moba",),
        apk_hints=("moba", "平安京"),
        enable_role_classification=False,
        supports_old_npk=False,
    ),
}


def get_game_profile(key: str | None = None) -> GameProfile:
    selected = (key or os.environ.get("NEOX_GAME_PROFILE", "onmyoji")).strip().lower()
    return PROFILES.get(selected, PROFILES["onmyoji"])
