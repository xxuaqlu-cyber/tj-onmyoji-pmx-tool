from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
import tkinter as tk
from pathlib import Path, PurePosixPath
from tkinter import filedialog, messagebox, ttk

from game_profiles import get_game_profile


APP_DIR = Path(__file__).resolve().parent
GAME_PROFILE = get_game_profile()
APP_TITLE = f"{GAME_PROFILE.display_name}资源拉取工具"
SETTINGS_PATH = APP_DIR / GAME_PROFILE.settings_filename
DEFAULT_DEVICE = "127.0.0.1:7555"
DEFAULT_REMOTE = (
    f"package:{GAME_PROFILE.default_package}"
    if GAME_PROFILE.key == "moba"
    else f"/sdcard/Android/data/{GAME_PROFILE.default_package}"
)
SYNC_MANIFEST_NAME = GAME_PROFILE.manifest_filename
MOBA_APK_ASSET_MANIFEST = ".moba_apk_assets.json"
MOBA_FULL_SYNC_MANIFEST = ".moba_full_sync_manifest.json"
MOBA_ROOT_ANDROID_DATA = "/data/media/0/Android/data"
MOBA_MODEL_ASSET_RE = re.compile(r"^assets/(?:hero\d+|res)\.npk$", re.I)
PULL_BATCH_FILES = 100
PULL_BATCH_CHARS = 24_000


def infer_mumu_dir(adb: str) -> str:
    """从旧版设置中的 adb 路径推断 MuMu 安装目录。"""
    try:
        path = Path(os.path.expandvars(adb)).expanduser()
    except (OSError, ValueError):
        return ""
    for parent in (path, *path.parents):
        if "mumu" in parent.name.lower():
            return str(parent)
    return ""


def find_mumu_adb_candidates(mumu_dir: str) -> list[str]:
    """找出 MuMu 目录内可用的 ADB，优先使用 shell/adb.exe。"""
    value = os.path.expandvars(mumu_dir.strip())
    if not value:
        return []
    root = Path(value).expanduser()
    if root.is_file() and root.name.lower() == "adb.exe":
        return [str(root.resolve())]
    if not root.is_dir():
        return []
    candidates: list[Path] = []
    try:
        candidates = [path for path in root.rglob("adb.exe") if path.is_file()]
    except OSError:
        pass
    candidates.sort(key=lambda path: (0 if path.parent.name.lower() == "shell" else 1, len(path.parts), str(path).lower()))
    result: list[str] = []
    seen: set[str] = set()
    for path in candidates:
        resolved = str(path.resolve())
        if resolved.lower() not in seen:
            seen.add(resolved.lower())
            result.append(resolved)
    return result


def parse_adb_devices(output: str) -> list[dict[str, str]]:
    """解析 `adb devices -l`，只返回当前可用的设备。"""
    devices: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[1] != "device" or parts[0] in seen:
            continue
        model = ""
        for part in parts[2:]:
            if part.startswith("model:"):
                model = part[6:].replace("_", " ")
                break
        seen.add(parts[0])
        devices.append({"serial": parts[0], "model": model})
    return devices


def format_device_label(device: dict[str, str]) -> str:
    serial = device.get("serial", "")
    model = device.get("model", "")
    return f"{serial}（{model}）" if model else serial


def game_package_candidates(package_output: str) -> list[str]:
    packages: list[str] = []
    excluded = {
        "com.netease.yysbwp",  # 阴阳师：百闻牌，不是阴阳师本体
    }
    for line in package_output.splitlines():
        name = line.strip()
        if name.startswith("package:"):
            name = name[8:].strip()
        if not name or any(char.isspace() for char in name):
            continue
        lowered = name.lower()
        if name in excluded:
            continue
        if any(keyword in lowered for keyword in GAME_PROFILE.package_keywords):
            packages.append(name)
    return list(dict.fromkeys([*packages, *GAME_PROFILE.package_names]))


def onmyoji_package_candidates(package_output: str) -> list[str]:
    """兼容旧调用；实际按当前游戏配置筛选包名。"""
    return game_package_candidates(package_output)


def subprocess_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def load_settings() -> dict[str, str]:
    defaults = {
        "adb": shutil.which("adb") or "adb",
        "mumu_dir": "",
        "device": DEFAULT_DEVICE,
        "remote": DEFAULT_REMOTE,
        "output": str(APP_DIR / GAME_PROFILE.local_resource_dir),
    }
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return defaults
    if isinstance(saved, dict):
        for key in defaults:
            value = saved.get(key)
            if isinstance(value, str) and value.strip():
                defaults[key] = value.strip()
    if GAME_PROFILE.key == "moba" and not defaults["remote"].startswith("package:"):
        defaults["remote"] = DEFAULT_REMOTE
    return defaults


def pull_destination(output_root: Path, remote: str) -> tuple[Path, str]:
    """返回本地包目录和用于拉取其内容的 Android 路径。"""
    remote_root = remote.rstrip("/")
    package_name = PurePosixPath(remote_root).name
    if not remote_root.startswith("/") or package_name in {"", ".", ".."}:
        raise ValueError("Android 源目录无效，无法确定本地包目录名。")
    return output_root / package_name, remote_root + "/."


def parse_remote_manifest(output: str, remote: str) -> dict[str, dict[str, int]]:
    """解析 Android stat 输出为相对路径 -> 大小/修改时间。"""
    remote_root = remote.rstrip("/")
    prefix = remote_root + "/"
    result: dict[str, dict[str, int]] = {}
    malformed = 0
    for line in output.splitlines():
        parts = line.rstrip("\r").split("|", 2)
        if len(parts) != 3:
            if line.strip():
                malformed += 1
            continue
        size_text, mtime_text, remote_path = parts
        if not remote_path.startswith(prefix):
            malformed += 1
            continue
        relative = PurePosixPath(remote_path[len(prefix) :])
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            malformed += 1
            continue
        try:
            size = int(size_text)
            mtime = int(mtime_text)
        except ValueError:
            malformed += 1
            continue
        result[relative.as_posix()] = {"size": size, "mtime": mtime}
    if malformed:
        raise RuntimeError(f"远端文件清单有 {malformed} 行无法解析。")
    if not result:
        raise RuntimeError("远端目录中没有读取到任何文件。")
    return result


def load_sync_manifest(path: Path) -> dict[str, dict[str, int]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, dict[str, int]] = {}
    for relative, metadata in payload.items():
        if not isinstance(relative, str) or not isinstance(metadata, dict):
            continue
        try:
            result[relative] = {
                "size": int(metadata["size"]),
                "mtime": int(metadata["mtime"]),
            }
        except (KeyError, TypeError, ValueError):
            continue
    return result


def save_sync_manifest(path: Path, manifest: dict[str, dict[str, int]]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def local_path_for_remote(package_output: Path, relative: str) -> Path:
    posix_path = PurePosixPath(relative)
    if posix_path.is_absolute() or not posix_path.parts or ".." in posix_path.parts:
        raise ValueError(f"不安全的远端相对路径：{relative}")
    return package_output.joinpath(*posix_path.parts)


def changed_remote_files(
    package_output: Path,
    remote_manifest: dict[str, dict[str, int]],
    previous_manifest: dict[str, dict[str, int]],
) -> list[str]:
    changed: list[str] = []
    for relative, metadata in remote_manifest.items():
        local_path = local_path_for_remote(package_output, relative)
        try:
            local_size = local_path.stat().st_size if local_path.is_file() else -1
        except OSError:
            local_size = -1
        if previous_manifest.get(relative) != metadata or local_size != metadata["size"]:
            changed.append(relative)
    return sorted(changed)


def pull_batches(
    relative_paths: list[str], *, remote_prefix: str = ""
) -> list[list[str]]:
    """同目录分批，保证多源 adb pull 不会丢失远端目录层级。"""
    by_parent: dict[str, list[str]] = {}
    for relative in relative_paths:
        parent = PurePosixPath(relative).parent.as_posix()
        by_parent.setdefault(parent, []).append(relative)
    batches: list[list[str]] = []
    for parent in sorted(by_parent):
        current: list[str] = []
        current_chars = 0
        for relative in sorted(by_parent[parent]):
            path_chars = len(remote_prefix) + len(relative) + 3
            if current and (
                len(current) >= PULL_BATCH_FILES
                or current_chars + path_chars > PULL_BATCH_CHARS
            ):
                batches.append(current)
                current = []
                current_chars = 0
            current.append(relative)
            current_chars += path_chars
        if current:
            batches.append(current)
    return batches


class ResourcePullApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("860x620")
        self.root.minsize(720, 520)

        settings = load_settings()
        self.adb_var = tk.StringVar(value=settings["adb"])
        self.mumu_dir_var = tk.StringVar(value=settings.get("mumu_dir") or infer_mumu_dir(settings["adb"]))
        self.device_var = tk.StringVar(value=settings["device"])
        self.device_display_var = tk.StringVar(value=settings["device"])
        self.remote_var = tk.StringVar(value=settings["remote"])
        self.output_var = tk.StringVar(value=settings["output"])
        self.status_var = tk.StringVar(value="请确认设备和目录，然后开始拉取。")

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.current_process: subprocess.Popen[str] | None = None
        self.cancel_requested = False
        self.busy = False
        self.discovered_devices: dict[str, dict[str, str]] = {}
        self.device_labels: dict[str, str] = {}

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.poll_events)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(7, weight=1)

        ttk.Label(outer, text="MuMu 模拟器目录").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(outer, textvariable=self.mumu_dir_var).grid(
            row=0, column=1, sticky="ew", padx=(10, 6), pady=5
        )
        ttk.Button(outer, text="选择…", command=self.choose_mumu_dir).grid(
            row=0, column=2, sticky="ew", pady=5
        )

        ttk.Label(outer, text="ADB 程序").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(outer, textvariable=self.adb_var).grid(
            row=1, column=1, sticky="ew", padx=(10, 6), pady=5
        )
        ttk.Button(outer, text="浏览…", command=self.choose_adb).grid(
            row=1, column=2, sticky="ew", pady=5
        )

        ttk.Label(outer, text="正在运行的模拟器").grid(row=2, column=0, sticky="w", pady=5)
        self.device_combo = ttk.Combobox(outer, textvariable=self.device_display_var)
        self.device_combo.grid(row=2, column=1, sticky="ew", padx=(10, 6), pady=5)
        self.device_combo.bind("<<ComboboxSelected>>", self.on_device_selected)
        ttk.Button(outer, text="检测设备", command=self.refresh_devices).grid(
            row=2, column=2, sticky="ew", pady=5
        )

        source_label = "游戏包名" if GAME_PROFILE.key == "moba" else "Android 源目录"
        ttk.Label(outer, text=source_label).grid(row=3, column=0, sticky="w", pady=5)
        ttk.Entry(outer, textvariable=self.remote_var).grid(
            row=3, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=5
        )

        ttk.Label(outer, text="本地保存目录").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Entry(outer, textvariable=self.output_var).grid(
            row=4, column=1, sticky="ew", padx=(10, 6), pady=5
        )
        ttk.Button(outer, text="选择…", command=self.choose_output).grid(
            row=4, column=2, sticky="ew", pady=5
        )

        if GAME_PROFILE.key == "moba":
            note = (
                "决战平安京完整模式会通过 MuMu root 视角镜像 APK、Android/data、"
                "应用私有目录与 OBB；其中 Documents\\extend\\hero 的按式神 NPK 会完整保留。"
                "不会再使用 /storage/emulated/0 的受限视图，避免漏掉大资源。"
                f"默认保存到 {GAME_PROFILE.local_resource_dir}\\com.netease.moba；"
                "增量模式只更新新增、变化或本地缺失文件。"
            )
        else:
            note = (
                f"先选择 MuMu 目录并检测设备，再从下拉框选择实例；工具会自动查找{GAME_PROFILE.display_name}目录。"
                f"默认保存到本工具所在目录下的 {GAME_PROFILE.local_resource_dir}；所有字段均可修改并会保存在本机。"
                "增量模式只下载新增、变化或本地缺失的文件；完整模式会重新下载全部文件。"
                "拉取前请先在模拟器内完成游戏更新。"
            )
        ttk.Label(outer, text=note, foreground="#555555", wraplength=790).grid(
            row=5, column=0, columnspan=3, sticky="w", pady=(6, 10)
        )

        actions = ttk.Frame(outer)
        actions.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        self.connect_button = ttk.Button(actions, text="连接并检测", command=self.connect_device)
        self.connect_button.pack(side="left")
        self.pull_button = ttk.Button(
            actions,
            text=("完整拉取整个游戏" if GAME_PROFILE.key == "moba" else "完整拉取全部资源"),
            command=self.start_pull,
        )
        self.pull_button.pack(side="left", padx=8)
        self.incremental_button = ttk.Button(
            actions, text="增量拉取更新", command=self.start_incremental_pull
        )
        self.incremental_button.pack(side="left")
        self.update_button = ttk.Button(
            actions, text="更新项目", command=self.update_project
        )
        self.update_button.pack(side="left", padx=(8, 0))
        self.cancel_button = ttk.Button(
            actions, text="停止", command=self.cancel, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=(8, 0))
        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=180)
        self.progress.pack(side="right", padx=(10, 0))

        log_frame = ttk.LabelFrame(outer, text="运行日志", padding=6)
        log_frame.grid(row=7, column=0, columnspan=3, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(log_frame, wrap="word", height=16, state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

        ttk.Label(outer, textvariable=self.status_var, anchor="w").grid(
            row=8, column=0, columnspan=3, sticky="ew", pady=(8, 0)
        )

    def choose_mumu_dir(self) -> None:
        current = Path(os.path.expandvars(self.mumu_dir_var.get())).expanduser()
        initial = current if current.is_dir() else APP_DIR
        selected = filedialog.askdirectory(title="选择 MuMu 模拟器目录", initialdir=str(initial))
        if selected:
            self.mumu_dir_var.set(selected)
            self.refresh_devices()

    def choose_adb(self) -> None:
        selected = filedialog.askopenfilename(
            title="选择 adb 程序",
            initialdir=str(APP_DIR),
            filetypes=[("ADB", "adb.exe"), ("可执行文件", "*.exe"), ("所有文件", "*.*")],
        )
        if selected:
            self.adb_var.set(selected)

    def choose_output(self) -> None:
        current = Path(os.path.expandvars(self.output_var.get())).expanduser()
        initial = current if current.is_dir() else APP_DIR
        selected = filedialog.askdirectory(title="选择本地资源保存目录", initialdir=str(initial))
        if selected:
            self.output_var.set(selected)

    def append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip("\r\n") + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def resolve_adb(self) -> str:
        value = os.path.expandvars(self.adb_var.get().strip())
        if not value:
            raise RuntimeError("请选择 adb.exe，或将 adb 加入系统 PATH。")
        candidate = Path(value).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        located = shutil.which(value)
        if located:
            return located
        raise RuntimeError(f"找不到 ADB 程序：{value}")

    def save_settings(self) -> None:
        payload = {
            "adb": self.adb_var.get().strip(),
            "mumu_dir": self.mumu_dir_var.get().strip(),
            "device": self.device_var.get().strip(),
            "remote": self.remote_var.get().strip(),
            "output": self.output_var.get().strip(),
        }
        try:
            SETTINGS_PATH.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    def set_busy(self, value: bool, status: str | None = None) -> None:
        self.busy = value
        button_state = "disabled" if value else "normal"
        self.connect_button.configure(state=button_state)
        self.pull_button.configure(state=button_state)
        self.incremental_button.configure(state=button_state)
        self.update_button.configure(state=button_state)
        self.cancel_button.configure(state="normal" if value else "disabled")
        if value:
            self.progress.start(12)
        else:
            self.progress.stop()
        if status:
            self.status_var.set(status)

    def start_worker(self, target, status: str) -> None:
        if self.busy:
            return
        self.save_settings()
        self.cancel_requested = False
        self.set_busy(True, status)
        threading.Thread(target=target, daemon=True).start()

    def run_command(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        log_command: bool = True,
        log_output: bool = True,
    ) -> tuple[int, str]:
        if log_command:
            self.events.put(("log", "> " + subprocess.list2cmdline(command)))
        process = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess_flags(),
        )
        self.current_process = process
        output: list[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            output.append(line)
            if log_output:
                self.events.put(("log", line))
        code = process.wait()
        self.current_process = None
        return code, "".join(output)

    def ensure_device(self, adb: str, device: str) -> None:
        if ":" in device:
            code, output = self.run_command([adb, "connect", device])
            if code != 0:
                raise RuntimeError(output.strip() or "ADB 连接失败。")
        code, output = self.run_command([adb, "-s", device, "get-state"])
        if code != 0 or "device" not in output.lower():
            raise RuntimeError(output.strip() or f"设备 {device} 当前不可用。")

    def read_remote_manifest(
        self,
        adb: str,
        device: str,
        remote: str,
        *,
        prune_paths: tuple[str, ...] = (),
        allow_empty: bool = False,
    ) -> dict[str, dict[str, int]]:
        remote_root = remote.rstrip("/")
        if allow_empty:
            code, probe = self.run_command(
                [
                    adb,
                    "-s",
                    device,
                    "shell",
                    f"find {shlex.quote(remote_root)} -type f -print -quit",
                ],
                log_output=False,
            )
            if code != 0 or not probe.strip():
                self.events.put(("log", f"远端目录为空或不存在：{remote_root}"))
                return {}
        find_parts = [f"find {shlex.quote(remote_root)}"]
        for prune_path in prune_paths:
            find_parts.append(
                f"-path {shlex.quote(prune_path.rstrip('/'))} -prune -o"
            )
        find_parts.append("-type f -print0")
        shell_command = (
            " ".join(find_parts)
            + " | xargs -0 -n 100 stat -c '%s|%Y|%n'"
        )
        self.events.put(("log", "正在读取远端文件大小和修改时间…"))
        code, output = self.run_command(
            [adb, "-s", device, "shell", shell_command],
            log_output=False,
        )
        if self.cancel_requested:
            raise InterruptedError("用户停止了拉取。")
        if code != 0:
            raise RuntimeError(output.strip() or "读取远端文件清单失败。")
        if allow_empty and not output.strip():
            self.events.put(("log", f"远端目录为空：{remote_root}"))
            return {}
        manifest = parse_remote_manifest(output, remote_root)
        self.events.put(("log", f"远端文件清单：{len(manifest)} 个文件。"))
        return manifest

    def refresh_devices(self) -> None:
        def worker() -> None:
            try:
                try:
                    configured = self.resolve_adb()
                except RuntimeError:
                    configured = ""
                candidates = find_mumu_adb_candidates(self.mumu_dir_var.get())
                if configured and configured not in candidates:
                    candidates.insert(0, configured)
                if not candidates:
                    raise RuntimeError("找不到 MuMu 目录中的 adb.exe。请选择正确的模拟器目录，或手动指定 ADB 程序。")
                discovered: dict[str, dict[str, str]] = {}
                for adb in candidates:
                    try:
                        code, output = self.run_command([adb, "devices", "-l"], log_output=False)
                    except OSError:
                        continue
                    if code != 0:
                        continue
                    for device in parse_adb_devices(output):
                        device["adb"] = adb
                        discovered.setdefault(device["serial"], device)
                if not discovered:
                    raise RuntimeError("未发现正在运行的模拟器。请确认 MuMu 已启动，并检查目录是否正确。")
                self.events.put(("devices", list(discovered.values())))
                self.events.put(("done", f"检测完成，共发现 {len(discovered)} 台可用模拟器。"))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        self.start_worker(worker, "正在检测 ADB 设备…")

    def connect_device(self) -> None:
        def worker() -> None:
            try:
                adb = self.selected_adb()
                device = self.device_var.get().strip()
                if not device:
                    raise RuntimeError("请输入设备地址或序列号。")
                self.ensure_device(adb, device)
                self.events.put(("done", f"设备连接正常：{device}"))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        self.start_worker(worker, "正在连接并检测设备…")

    def selected_adb(self) -> str:
        device = self.device_var.get().strip()
        record = self.discovered_devices.get(device)
        if record and record.get("adb"):
            self.adb_var.set(record["adb"])
            return record["adb"]
        return self.resolve_adb()

    def on_device_selected(self, _event=None) -> None:
        selected = self.device_display_var.get().strip()
        serial = self.device_labels.get(selected, selected)
        self.device_var.set(serial)
        record = self.discovered_devices.get(serial)
        if record and record.get("adb"):
            self.adb_var.set(record["adb"])
            self.auto_detect_remote(record["adb"], record["serial"])

    def auto_detect_remote(self, adb: str, device: str) -> None:
        def worker() -> None:
            try:
                packages_output = self.run_command(
                    [adb, "-s", device, "shell", "pm", "list", "packages"],
                    log_output=False,
                )[1]
                if GAME_PROFILE.key == "moba":
                    package = GAME_PROFILE.default_package
                    code, path_output = self.run_command(
                        [adb, "-s", device, "shell", "pm", "path", package],
                        log_output=False,
                    )
                    if code == 0 and "package:" in path_output:
                        self.events.put(("remote", f"package:{package}"))
                        self.events.put(("log", f"已自动找到{GAME_PROFILE.display_name} APK：{package}"))
                    else:
                        self.events.put(("log", f"未找到已安装的{GAME_PROFILE.display_name}：{package}"))
                    return
                packages = game_package_candidates(packages_output)
                bases = ["/sdcard/Android/data", "/storage/emulated/0/Android/data"]
                paths = [f"{base}/{package}" for base in bases for package in packages]
                for remote in paths:
                    code, listing = self.run_command(
                        [adb, "-s", device, "shell", "test", "-d", remote],
                        log_output=False,
                    )
                    if code == 0:
                        self.events.put(("remote", remote))
                        self.events.put(("log", f"已自动找到{GAME_PROFILE.display_name}目录：{remote}"))
                        return
                # 某些渠道包名可能与预设不同，最后从 Android/data 目录名兜底筛选。
                code, listing = self.run_command(
                    [
                        adb,
                        "-s",
                        device,
                        "shell",
                        "find",
                        "/sdcard/Android/data",
                        "/storage/emulated/0/Android/data",
                        "-mindepth",
                        "1",
                        "-maxdepth",
                        "1",
                        "-type",
                        "d",
                    ],
                    log_output=False,
                )
                if code == 0:
                    matches = [
                        line.strip()
                        for line in listing.splitlines()
                        if line.strip()
                        and any(
                            keyword in PurePosixPath(line.strip()).name.lower()
                            for keyword in GAME_PROFILE.package_keywords
                        )
                    ]
                    if matches:
                        self.events.put(("remote", matches[0]))
                        self.events.put(("log", f"已自动找到{GAME_PROFILE.display_name}目录：{matches[0]}"))
                        return
                self.events.put(("log", f"未自动找到{GAME_PROFILE.display_name}目录，请手动填写 Android 源目录。"))
            except Exception as exc:
                self.events.put(("log", f"自动查找{GAME_PROFILE.display_name}目录失败：{exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _moba_package_name(self) -> str:
        value = self.remote_var.get().strip()
        if value.startswith("package:"):
            value = value.split(":", 1)[1].strip()
        if value and "/" not in value:
            return value
        return GAME_PROFILE.default_package

    def _moba_apk_catalog(
        self, adb: str, device: str, package: str
    ) -> tuple[str, dict[str, int], list[tuple[str, int]]]:
        code, output = self.run_command(
            [adb, "-s", device, "shell", "pm", "path", package],
            log_output=False,
        )
        paths = [
            line.split("package:", 1)[1].strip()
            for line in output.splitlines()
            if line.strip().startswith("package:")
        ]
        if code != 0 or not paths:
            raise RuntimeError(f"找不到已安装的 {package} APK。")
        apk_path = paths[0]
        code, size_output = self.run_command(
            [adb, "-s", device, "shell", "stat", "-c", "%s", apk_path],
            log_output=False,
        )
        if code != 0:
            raise RuntimeError("无法读取决战平安京 APK 文件大小。")
        code, mtime_output = self.run_command(
            [adb, "-s", device, "shell", "stat", "-c", "%Y", apk_path],
            log_output=False,
        )
        if code != 0:
            raise RuntimeError("无法读取决战平安京 APK 修改时间。")
        try:
            apk_stamp = {
                "size": int(size_output.strip()),
                "mtime": int(mtime_output.strip()),
            }
        except (ValueError, TypeError):
            raise RuntimeError(
                "无法解析 APK 文件信息："
                f"size={size_output.strip()!r}, mtime={mtime_output.strip()!r}"
            )

        code, listing = self.run_command(
            [adb, "-s", device, "shell", "unzip", "-l", apk_path],
            log_output=False,
        )
        if code != 0:
            raise RuntimeError("无法读取 APK 内的 NPK 资源列表。")
        assets: list[tuple[str, int]] = []
        for line in listing.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            name = parts[-1]
            if not MOBA_MODEL_ASSET_RE.match(name):
                continue
            try:
                size = int(parts[0])
            except ValueError:
                continue
            assets.append((name, size))
        assets.sort(
            key=lambda item: (
                0 if Path(item[0]).stem.lower().startswith("hero") else 1,
                int(re.search(r"(\d+)", Path(item[0]).stem).group(1))
                if re.search(r"(\d+)", Path(item[0]).stem) else 999,
                item[0].lower(),
            )
        )
        if not assets:
            raise RuntimeError("APK 内没有找到 hero*.npk / res.npk。")
        return apk_path, apk_stamp, assets

    def _stream_moba_apk_asset(
        self,
        adb: str,
        device: str,
        apk_path: str,
        asset_name: str,
        target: Path,
        expected_size: int,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".part")
        try:
            with temporary.open("wb") as stream:
                process = subprocess.Popen(
                    [
                        adb, "-s", device, "exec-out", "unzip", "-p",
                        apk_path, asset_name,
                    ],
                    stdout=stream,
                    stderr=subprocess.PIPE,
                    creationflags=subprocess_flags(),
                )
                self.current_process = process
                _stdout, stderr = process.communicate()
                self.current_process = None
                if self.cancel_requested:
                    raise InterruptedError("用户停止了拉取。")
                if process.returncode != 0:
                    detail = (stderr or b"").decode("utf-8", "replace").strip()
                    raise RuntimeError(detail or f"APK 资源抽取失败：{asset_name}")
            actual_size = temporary.stat().st_size
            if actual_size != expected_size:
                raise RuntimeError(
                    f"{asset_name} 大小校验失败：{actual_size} != {expected_size}"
                )
            temporary.replace(target)
        finally:
            self.current_process = None
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def _read_moba_root_manifest(
        self,
        adb: str,
        device: str,
        remote_root: str,
        *,
        allow_empty: bool = False,
    ) -> dict[str, dict[str, int]]:
        """读取 MuMu root 视角的 Android/data 清单。

        MuMu 对 /storage/emulated/0 的普通 shell 视图会隐藏决战平安京的大资源，
        但真实文件仍位于 /data/media/0/Android/data/...。因此平安京完整同步必须
        通过 su 读取该物理目录，不能再依赖普通 sdcard FUSE 视图。
        """
        remote_root = remote_root.rstrip("/")
        shell_command = (
            f"find {shlex.quote(remote_root)} -type f "
            "-exec stat -c '%s|%Y|%n' {} +"
        )
        self.events.put(("log", "正在以 root 视角读取完整游戏文件清单…"))
        code, output = self.run_command(
            [
                adb,
                "-s",
                device,
                "shell",
                f"su -c {shlex.quote(shell_command)}",
            ],
            log_output=False,
        )
        if self.cancel_requested:
            raise InterruptedError("用户停止了拉取。")
        if code != 0:
            raise RuntimeError(output.strip() or "root 文件清单读取失败。")
        if allow_empty and not output.strip():
            self.events.put(("log", f"root 远端目录为空：{remote_root}"))
            return {}
        manifest = parse_remote_manifest(output, remote_root)
        self.events.put(("log", f"root 完整清单：{len(manifest):,} 个文件。"))
        return manifest

    def _stream_moba_root_file(
        self,
        adb: str,
        device: str,
        remote_path: str,
        target: Path,
        expected_size: int,
    ) -> None:
        """通过 su + cat 流式读取普通 shell 无权限访问的单个文件。"""
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".part")
        try:
            command = f"cat {shlex.quote(remote_path)}"
            with temporary.open("wb") as stream:
                process = subprocess.Popen(
                    [
                        adb,
                        "-s",
                        device,
                        "exec-out",
                        "su",
                        "-c",
                        command,
                    ],
                    stdout=stream,
                    stderr=subprocess.PIPE,
                    creationflags=subprocess_flags(),
                )
                self.current_process = process
                _stdout, stderr = process.communicate()
                self.current_process = None
                if self.cancel_requested:
                    raise InterruptedError("用户停止了拉取。")
                if process.returncode != 0:
                    detail = (stderr or b"").decode("utf-8", "replace").strip()
                    raise RuntimeError(detail or f"root 文件拉取失败：{remote_path}")
            actual_size = temporary.stat().st_size
            if actual_size != expected_size:
                raise RuntimeError(
                    f"root 文件大小校验失败：{remote_path}；"
                    f"{actual_size} != {expected_size}"
                )
            temporary.replace(target)
        finally:
            self.current_process = None
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def _stream_moba_root_tree(
        self,
        adb: str,
        device: str,
        remote_root: str,
        local_root: Path,
        manifest: dict[str, dict[str, int]],
    ) -> None:
        """首次全量同步时用 root tar 单流传输整个 Android/data 树。"""
        local_root.mkdir(parents=True, exist_ok=True)
        total_files = len(manifest)
        total_bytes = sum(row["size"] for row in manifest.values())
        done_files = 0
        done_bytes = 0
        command = f"tar -C {shlex.quote(remote_root)} -cf - ."
        process = subprocess.Popen(
            [adb, "-s", device, "exec-out", "su", "-c", command],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess_flags(),
        )
        self.current_process = process
        try:
            assert process.stdout is not None
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                for member in archive:
                    if self.cancel_requested:
                        process.terminate()
                        raise InterruptedError("用户停止了拉取。")
                    name = member.name.replace("\\", "/")
                    while name.startswith("./"):
                        name = name[2:]
                    if not name or name == ".":
                        continue
                    relative = PurePosixPath(name)
                    if relative.is_absolute() or ".." in relative.parts:
                        raise RuntimeError(f"tar 中出现不安全路径：{member.name}")
                    target = local_root.joinpath(*relative.parts)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    if not member.isfile():
                        # 游戏资源树不依赖符号链接；Windows 也不应创建 Android 链接。
                        continue
                    source = archive.extractfile(member)
                    if source is None:
                        raise RuntimeError(f"无法读取 tar 成员：{member.name}")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temporary = target.with_name(target.name + ".part")
                    try:
                        with temporary.open("wb") as output:
                            shutil.copyfileobj(source, output, length=1024 * 1024)
                        expected = manifest.get(relative.as_posix())
                        if expected is not None and temporary.stat().st_size != expected["size"]:
                            raise RuntimeError(
                                f"tar 文件大小校验失败：{relative.as_posix()}"
                            )
                        temporary.replace(target)
                        if member.mtime:
                            try:
                                os.utime(target, (member.mtime, member.mtime))
                            except OSError:
                                pass
                    finally:
                        if temporary.exists():
                            try:
                                temporary.unlink()
                            except OSError:
                                pass
                    done_files += 1
                    expected = manifest.get(relative.as_posix())
                    done_bytes += (
                        expected["size"] if expected is not None else int(member.size)
                    )
                    if done_files % 10 == 0 or done_files == total_files:
                        percent = done_bytes * 100 / total_bytes if total_bytes else 100.0
                        self.events.put(
                            (
                                "status",
                                f"root 全量拉取 {done_files:,}/{total_files:,}；"
                                f"约 {percent:.1f}%",
                            )
                        )
                    if done_files % 100 == 0 or done_files == total_files:
                        self.events.put(
                            (
                                "log",
                                f"root 全量拉取：{done_files:,}/{total_files:,} 个文件；"
                                f"约 {done_bytes / 1024 / 1024 / 1024:.2f} / "
                                f"{total_bytes / 1024 / 1024 / 1024:.2f} GB。",
                            )
                        )
            process.stdout.close()
            return_code = process.wait()
            stderr = b""
            if process.stderr is not None:
                stderr = process.stderr.read()
            if return_code != 0:
                detail = stderr.decode("utf-8", "replace").strip()
                raise RuntimeError(detail or f"root tar 拉取失败，退出码 {return_code}。")
            missing = [
                relative
                for relative, metadata in manifest.items()
                if not (
                    (target := local_path_for_remote(local_root, relative)).is_file()
                    and target.stat().st_size == metadata["size"]
                )
            ]
            if missing:
                raise RuntimeError(
                    f"root tar 拉取后仍缺少或大小不符 {len(missing)} 个文件；"
                    f"首个：{missing[0]}"
                )
        finally:
            self.current_process = None
            if process.poll() is None:
                process.terminate()

    def _pull_moba_root_incremental(
        self,
        adb: str,
        device: str,
        remote_root: str,
        local_root: Path,
        manifest: dict[str, dict[str, int]],
        changed: list[str],
    ) -> None:
        if not changed:
            return
        total_bytes = sum(manifest[path]["size"] for path in changed)
        done_bytes = 0
        for number, relative in enumerate(changed, 1):
            if self.cancel_requested:
                raise InterruptedError("用户停止了拉取。")
            remote_path = f"{remote_root.rstrip('/')}/{relative}"
            target = local_path_for_remote(local_root, relative)
            self.events.put(
                (
                    "status",
                    f"root 增量拉取 {number:,}/{len(changed):,}：{relative}",
                )
            )
            self._stream_moba_root_file(
                adb,
                device,
                remote_path,
                target,
                manifest[relative]["size"],
            )
            done_bytes += manifest[relative]["size"]
            if number % 25 == 0 or number == len(changed):
                self.events.put(
                    (
                        "log",
                        f"root 增量拉取：{number:,}/{len(changed):,}；"
                        f"约 {done_bytes / 1024 / 1024 / 1024:.2f} / "
                        f"{total_bytes / 1024 / 1024 / 1024:.2f} GB。",
                    )
                )

    def _moba_apk_files(
        self, adb: str, device: str, package: str
    ) -> list[dict[str, object]]:
        code, output = self.run_command(
            [adb, "-s", device, "shell", "pm", "path", package],
            log_output=False,
        )
        paths = [
            line.split("package:", 1)[1].strip()
            for line in output.splitlines()
            if line.strip().startswith("package:")
        ]
        if code != 0 or not paths:
            raise RuntimeError(f"找不到已安装的 {package} APK。")
        rows: list[dict[str, object]] = []
        used_names: set[str] = set()
        for number, remote_path in enumerate(paths, 1):
            code, size_output = self.run_command(
                [adb, "-s", device, "shell", "stat", "-c", "%s", remote_path],
                log_output=False,
            )
            if code != 0:
                raise RuntimeError(f"无法读取 APK 大小：{remote_path}")
            code, mtime_output = self.run_command(
                [adb, "-s", device, "shell", "stat", "-c", "%Y", remote_path],
                log_output=False,
            )
            if code != 0:
                raise RuntimeError(f"无法读取 APK 修改时间：{remote_path}")
            try:
                size = int(size_output.strip())
                mtime = int(mtime_output.strip())
            except ValueError as exc:
                raise RuntimeError(f"无法解析 APK 文件信息：{remote_path}") from exc
            name = PurePosixPath(remote_path).name or f"package_{number}.apk"
            if name.lower() in used_names:
                stem = Path(name).stem
                suffix = Path(name).suffix or ".apk"
                name = f"{stem}_{number}{suffix}"
            used_names.add(name.lower())
            rows.append(
                {
                    "remote": remote_path,
                    "name": name,
                    "size": size,
                    "mtime": mtime,
                }
            )
        return rows

    def _pull_remote_manifest_files(
        self,
        adb: str,
        device: str,
        remote_root: str,
        local_root: Path,
        remote_manifest: dict[str, dict[str, int]],
        relative_paths: list[str],
        *,
        label: str,
    ) -> None:
        if not relative_paths:
            return
        batches = pull_batches(
            relative_paths, remote_prefix=remote_root.rstrip("/") + "/"
        )
        total_bytes = sum(remote_manifest[path]["size"] for path in relative_paths)
        done_bytes = 0
        for number, batch in enumerate(batches, 1):
            if self.cancel_requested:
                raise InterruptedError("用户停止了拉取。")
            parent = PurePosixPath(batch[0]).parent.as_posix()
            local_parent = (
                local_root
                if parent in {"", "."}
                else local_path_for_remote(local_root, parent)
            )
            local_parent.mkdir(parents=True, exist_ok=True)
            remote_paths = [
                f"{remote_root.rstrip('/')}/{relative}" for relative in batch
            ]
            batch_bytes = sum(remote_manifest[path]["size"] for path in batch)
            self.events.put(
                (
                    "status",
                    f"{label} {number}/{len(batches)}；"
                    f"本批 {len(batch)} 个文件；"
                    f"约 {done_bytes / total_bytes * 100:.1f}%",
                )
            )
            code, text = self.run_command(
                [adb, "-s", device, "pull", "-a", *remote_paths, "."],
                cwd=local_parent,
                log_command=False,
                log_output=False,
            )
            if self.cancel_requested:
                raise InterruptedError("用户停止了拉取。")
            if code != 0:
                raise RuntimeError(
                    text.strip() or f"ADB {label}失败，退出码 {code}。"
                )
            done_bytes += batch_bytes
            if number % 10 == 0 or number == len(batches):
                self.events.put(
                    (
                        "log",
                        f"{label}：{number}/{len(batches)} 批，"
                        f"约 {done_bytes / 1024 / 1024 / 1024:.2f} / "
                        f"{total_bytes / 1024 / 1024 / 1024:.2f} GB。",
                    )
                )

    def _start_moba_full_game_pull(self, *, incremental: bool) -> None:
        try:
            output = Path(
                os.path.expandvars(self.output_var.get().strip())
            ).expanduser().resolve()
        except (OSError, ValueError) as exc:
            messagebox.showerror("目录无效", str(exc))
            return
        device = self.device_var.get().strip()
        package = self._moba_package_name()
        if not device:
            messagebox.showerror("信息不完整", "请先选择正在运行的模拟器。")
            return
        package_output = output / package
        external_root = f"{MOBA_ROOT_ANDROID_DATA}/{package}"
        private_root = f"/data/user/0/{package}"
        obb_root = f"/data/media/0/Android/obb/{package}"
        action = "增量拉取整个游戏" if incremental else "完整拉取整个游戏"
        if not messagebox.askokcancel(
            action,
            f"将镜像设备 {device} 上 {package} 的完整游戏资源：\n\n"
            f"1. {external_root}\n"
            f"2. {private_root}\n"
            "3. 安装 APK（含全部 assets）\n"
            f"4. {obb_root}\n\n"
            f"保存到：\n{package_output}\n\n"
            + (
                "只下载新增、变化、本地缺失或大小不符的文件。"
                if incremental
                else "本次会重新下载全部游戏资源，体积可能超过 10 GB。"
            ),
        ):
            return

        def worker() -> None:
            try:
                adb = self.selected_adb()
                output.mkdir(parents=True, exist_ok=True)
                package_output.mkdir(parents=True, exist_ok=True)
                self.ensure_device(adb, device)

                self.events.put(("log", "正在读取完整 Android/data 文件清单……"))
                external_manifest = self._read_moba_root_manifest(
                    adb,
                    device,
                    external_root,
                )
                external_bytes = sum(row["size"] for row in external_manifest.values())
                self.events.put(
                    (
                        "log",
                        f"Android/data：{len(external_manifest):,} 个文件，"
                        f"去重后约 {external_bytes / 1024 / 1024 / 1024:.2f} GB。",
                    )
                )

                self.events.put(("log", "正在读取应用私有目录文件清单……"))
                private_manifest = self._read_moba_root_manifest(
                    adb, device, private_root, allow_empty=True
                )
                private_bytes = sum(row["size"] for row in private_manifest.values())
                self.events.put((
                    "log",
                    f"应用私有目录：{len(private_manifest):,} 个文件，约 "
                    f"{private_bytes / 1024 / 1024:.1f} MB。",
                ))

                self.events.put(("log", "正在读取 OBB 文件清单……"))
                obb_manifest = self._read_moba_root_manifest(
                    adb, device, obb_root, allow_empty=True
                )
                apk_rows = self._moba_apk_files(adb, device, package)
                apk_bytes = sum(int(row["size"]) for row in apk_rows)
                self.events.put(
                    (
                        "log",
                        f"APK：{len(apk_rows)} 个文件，约 "
                        f"{apk_bytes / 1024 / 1024 / 1024:.2f} GB。",
                    )
                )

                manifest_path = package_output / MOBA_FULL_SYNC_MANIFEST
                try:
                    old_payload = json.loads(
                        manifest_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError, TypeError):
                    old_payload = {}
                if not isinstance(old_payload, dict):
                    old_payload = {}
                previous_external = old_payload.get("android_data", {})
                previous_private = old_payload.get("private_data", {})
                previous_obb = old_payload.get("obb", {})
                previous_apk_rows = old_payload.get("apk", [])
                if not isinstance(previous_external, dict):
                    previous_external = {}
                if not isinstance(previous_private, dict):
                    previous_private = {}
                if not isinstance(previous_obb, dict):
                    previous_obb = {}
                previous_apk = {
                    str(row.get("name")): row
                    for row in previous_apk_rows
                    if isinstance(row, dict) and row.get("name")
                } if isinstance(previous_apk_rows, list) else {}

                external_changed = (
                    changed_remote_files(
                        package_output, external_manifest, previous_external
                    )
                    if incremental
                    else sorted(external_manifest)
                )
                private_output = package_output / "_private"
                private_changed = (
                    changed_remote_files(
                        private_output, private_manifest, previous_private
                    )
                    if incremental
                    else sorted(private_manifest)
                )
                obb_output = package_output / "_obb"
                obb_changed = (
                    changed_remote_files(
                        obb_output, obb_manifest, previous_obb
                    )
                    if incremental
                    else sorted(obb_manifest)
                )
                self.events.put(
                    (
                        "log",
                        f"资源比较：Android/data 需下载 {len(external_changed):,} / "
                        f"{len(external_manifest):,}；私有目录需下载 "
                        f"{len(private_changed):,} / {len(private_manifest):,}；"
                        f"OBB 需下载 {len(obb_changed):,} / {len(obb_manifest):,}。",
                    )
                )

                if incremental:
                    self._pull_moba_root_incremental(
                        adb,
                        device,
                        external_root,
                        package_output,
                        external_manifest,
                        external_changed,
                    )
                else:
                    self.events.put((
                        "log",
                        "首次完整同步使用 root tar 单流传输整个 Android/data，"
                        "避免大资源被 /storage/emulated/0 视图隐藏。",
                    ))
                    self._stream_moba_root_tree(
                        adb,
                        device,
                        external_root,
                        package_output,
                        external_manifest,
                    )
                if private_changed:
                    if incremental:
                        self._pull_moba_root_incremental(
                            adb,
                            device,
                            private_root,
                            private_output,
                            private_manifest,
                            private_changed,
                        )
                    else:
                        self._stream_moba_root_tree(
                            adb,
                            device,
                            private_root,
                            private_output,
                            private_manifest,
                        )
                if obb_changed:
                    self._pull_moba_root_incremental(
                        adb,
                        device,
                        obb_root,
                        obb_output,
                        obb_manifest,
                        obb_changed,
                    )

                apk_output = package_output / "_apk"
                apk_output.mkdir(parents=True, exist_ok=True)
                apk_changed = 0
                for number, row in enumerate(apk_rows, 1):
                    if self.cancel_requested:
                        raise InterruptedError("用户停止了拉取。")
                    name = str(row["name"])
                    target = apk_output / name
                    old = previous_apk.get(name, {})
                    needs_pull = (
                        not incremental
                        or int(old.get("size", -1)) != int(row["size"])
                        or int(old.get("mtime", -1)) != int(row["mtime"])
                        or not target.is_file()
                        or target.stat().st_size != int(row["size"])
                    )
                    if not needs_pull:
                        continue
                    apk_changed += 1
                    self.events.put(
                        ("status", f"拉取 APK {number}/{len(apk_rows)}：{name}")
                    )
                    code, text = self.run_command(
                        [adb, "-s", device, "pull", "-a", str(row["remote"]), "."],
                        cwd=apk_output,
                        log_command=False,
                        log_output=False,
                    )
                    if code != 0:
                        raise RuntimeError(
                            text.strip() or f"APK 拉取失败：{row['remote']}"
                        )
                    if not target.is_file() or target.stat().st_size != int(row["size"]):
                        raise RuntimeError(f"APK 大小校验失败：{target}")

                # 保留旧的 APK NPK 缓存，方便现有解包入口继续读取基础 hero/res。
                npk_root = package_output / "npk"
                npk_root.mkdir(parents=True, exist_ok=True)
                apk_path, apk_stamp, assets = self._moba_apk_catalog(
                    adb, device, package
                )
                asset_manifest_path = package_output / MOBA_APK_ASSET_MANIFEST
                try:
                    old_asset_payload = json.loads(
                        asset_manifest_path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError, TypeError):
                    old_asset_payload = {}
                old_apk_stamp = (
                    old_asset_payload.get("apk", {})
                    if isinstance(old_asset_payload, dict)
                    else {}
                )
                same_apk = (
                    isinstance(old_apk_stamp, dict)
                    and int(old_apk_stamp.get("size", -1)) == apk_stamp["size"]
                    and int(old_apk_stamp.get("mtime", -1)) == apk_stamp["mtime"]
                )
                extracted_assets = 0
                for asset_name, asset_size in assets:
                    target = npk_root / Path(asset_name).name
                    if (
                        incremental
                        and same_apk
                        and target.is_file()
                        and target.stat().st_size == asset_size
                    ):
                        continue
                    self.events.put(("status", f"更新 APK NPK 缓存：{target.name}"))
                    self._stream_moba_apk_asset(
                        adb, device, apk_path, asset_name, target, asset_size
                    )
                    extracted_assets += 1
                asset_manifest_path.write_text(
                    json.dumps(
                        {
                            "package": package,
                            "apk": {**apk_stamp, "path": apk_path},
                            "assets": {
                                name: {"size": size} for name, size in assets
                            },
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )

                payload = {
                    "package": package,
                    "android_data_root": external_root,
                    "android_data": external_manifest,
                    "private_root": private_root,
                    "private_data": private_manifest,
                    "obb_root": obb_root,
                    "obb": obb_manifest,
                    "apk": apk_rows,
                }
                temporary = manifest_path.with_name(manifest_path.name + ".tmp")
                temporary.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                temporary.replace(manifest_path)
                self.events.put(
                    (
                        "done",
                        f"完整游戏资源已同步到 {package_output}；"
                        f"Android/data 下载 {len(external_changed):,} 个文件，"
                        f"私有目录下载 {len(private_changed):,} 个，"
                        f"APK 更新 {apk_changed} 个，OBB 更新 {len(obb_changed):,} 个，"
                        f"APK NPK 缓存更新 {extracted_assets} 个。",
                    )
                )
            except InterruptedError:
                self.events.put(("done", "拉取已停止。"))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        self.start_worker(
            worker,
            "正在检查完整游戏资源……" if incremental else "正在拉取整个游戏……",
        )

    def _start_moba_apk_pull(self, *, incremental: bool) -> None:
        try:
            output = Path(
                os.path.expandvars(self.output_var.get().strip())
            ).expanduser().resolve()
        except (OSError, ValueError) as exc:
            messagebox.showerror("目录无效", str(exc))
            return
        device = self.device_var.get().strip()
        package = self._moba_package_name()
        if not device:
            messagebox.showerror("信息不完整", "请先选择正在运行的模拟器。")
            return
        package_output = output / package
        npk_root = package_output / "npk"
        action = "增量拉取更新" if incremental else "完整拉取建模资源"
        if not messagebox.askokcancel(
            action,
            f"将从设备 {device} 的 {package} APK 中抽取建模资源：\n"
            "hero1~9.npk + res.npk\n\n"
            f"保存到：\n{npk_root}\n\n"
            + (
                "APK 未变化时会直接复用本地 NPK。"
                if incremental
                else "本次会重新抽取这些建模 NPK。"
            ),
        ):
            return

        def worker() -> None:
            try:
                adb = self.selected_adb()
                output.mkdir(parents=True, exist_ok=True)
                package_output.mkdir(parents=True, exist_ok=True)
                npk_root.mkdir(parents=True, exist_ok=True)
                self.ensure_device(adb, device)
                self.events.put(("log", "正在读取决战平安京 APK 内的 NPK 清单……"))
                apk_path, apk_stamp, assets = self._moba_apk_catalog(
                    adb, device, package
                )
                manifest_path = package_output / MOBA_APK_ASSET_MANIFEST
                try:
                    old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    old_manifest = {}
                old_apk = old_manifest.get("apk", {}) if isinstance(old_manifest, dict) else {}
                same_apk = (
                    isinstance(old_apk, dict)
                    and int(old_apk.get("size", -1)) == apk_stamp["size"]
                    and int(old_apk.get("mtime", -1)) == apk_stamp["mtime"]
                )
                changed: list[tuple[str, int]] = []
                for asset_name, asset_size in assets:
                    target = npk_root / Path(asset_name).name
                    if (
                        not incremental
                        or not same_apk
                        or not target.is_file()
                        or target.stat().st_size != asset_size
                    ):
                        changed.append((asset_name, asset_size))
                if incremental and not changed:
                    self.events.put(("done", f"检查完成：{len(assets)} 个建模 NPK 已是最新。"))
                    return

                total_bytes = sum(size for _, size in changed)
                done_bytes = 0
                for number, (asset_name, asset_size) in enumerate(changed, 1):
                    if self.cancel_requested:
                        raise InterruptedError("用户停止了拉取。")
                    target = npk_root / Path(asset_name).name
                    self.events.put((
                        "status",
                        f"抽取 APK 建模资源 {number}/{len(changed)}：{target.name}",
                    ))
                    self.events.put((
                        "log",
                        f"抽取 {asset_name}（{asset_size / 1024 / 1024:.1f} MB）……",
                    ))
                    self._stream_moba_apk_asset(
                        adb, device, apk_path, asset_name, target, asset_size
                    )
                    done_bytes += asset_size
                    self.events.put((
                        "log",
                        f"完成 {target.name}；总进度约 "
                        f"{done_bytes / total_bytes * 100:.1f}%",
                    ))

                payload = {
                    "package": package,
                    "apk": {**apk_stamp, "path": apk_path},
                    "assets": {
                        name: {"size": size}
                        for name, size in assets
                    },
                }
                manifest_path.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                self.events.put((
                    "done",
                    f"建模资源拉取完成：{len(changed)} 个 NPK；保存到 {npk_root}",
                ))
            except InterruptedError:
                self.events.put(("done", "拉取已停止。"))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        self.start_worker(
            worker,
            "正在检查 APK 建模资源……" if incremental else "正在抽取 APK 建模资源……",
        )

    def start_pull(self) -> None:
        if GAME_PROFILE.key == "moba":
            self._start_moba_full_game_pull(incremental=False)
            return
        try:
            output = Path(os.path.expandvars(self.output_var.get().strip())).expanduser().resolve()
        except (OSError, ValueError) as exc:
            messagebox.showerror("目录无效", str(exc))
            return
        remote = self.remote_var.get().strip()
        device = self.device_var.get().strip()
        if not device or not remote:
            messagebox.showerror("信息不完整", "请填写设备地址和 Android 源目录。")
            return
        if not remote.startswith("/"):
            messagebox.showerror("源目录无效", "Android 源目录应以 / 开头。")
            return
        try:
            package_output, remote_contents = pull_destination(output, remote)
        except ValueError as exc:
            messagebox.showerror("源目录无效", str(exc))
            return
        if not messagebox.askokcancel(
            "完整拉取全部资源",
            f"将从设备 {device} 拉取：\n{remote}\n\n保存到：\n{package_output}\n\n"
            "此模式会重新下载全部远端文件，过程可能耗时较长。",
        ):
            return

        def worker() -> None:
            try:
                adb = self.selected_adb()
                output.mkdir(parents=True, exist_ok=True)
                package_output.mkdir(parents=True, exist_ok=True)
                self.ensure_device(adb, device)
                remote_manifest: dict[str, dict[str, int]] | None = None
                try:
                    remote_manifest = self.read_remote_manifest(adb, device, remote)
                except Exception as exc:
                    self.events.put(
                        ("log", f"警告：无法建立增量清单，本次仍继续完整拉取：{exc}")
                    )
                if self.cancel_requested:
                    self.events.put(("done", "拉取已停止。"))
                    return
                # MuMu 附带的 ADB 在包含中文的绝对 Windows 目标路径下，可能无法
                # 递归创建子目录。让系统先进入已创建的包目录，再将纯 ASCII 的
                # "." 交给 ADB，既避开路径编码问题，也保证父目录一定存在。
                code, text = self.run_command(
                    [adb, "-s", device, "pull", "-a", remote_contents, "."],
                    cwd=package_output,
                )
                if self.cancel_requested:
                    self.events.put(("done", "拉取已停止。"))
                elif code != 0:
                    raise RuntimeError(text.strip() or f"ADB 拉取失败，退出码 {code}。")
                else:
                    if remote_manifest is not None:
                        save_sync_manifest(
                            package_output / SYNC_MANIFEST_NAME, remote_manifest
                        )
                    self.events.put(("done", f"资源拉取完成：{package_output}"))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        self.start_worker(worker, "正在拉取完整游戏资源…")

    def start_incremental_pull(self) -> None:
        if GAME_PROFILE.key == "moba":
            self._start_moba_full_game_pull(incremental=True)
            return
        try:
            output = Path(
                os.path.expandvars(self.output_var.get().strip())
            ).expanduser().resolve()
        except (OSError, ValueError) as exc:
            messagebox.showerror("目录无效", str(exc))
            return
        remote = self.remote_var.get().strip()
        device = self.device_var.get().strip()
        if not device or not remote:
            messagebox.showerror("信息不完整", "请填写设备地址和 Android 源目录。")
            return
        try:
            package_output, _ = pull_destination(output, remote)
        except ValueError as exc:
            messagebox.showerror("源目录无效", str(exc))
            return
        if not messagebox.askokcancel(
            "增量拉取更新",
            f"将检查设备 {device}：\n{remote}\n\n更新到：\n{package_output}\n\n"
            "只下载新增、变化、本地缺失或大小不符的文件。"
            "远端已删除的文件会在本地保留。",
        ):
            return

        def worker() -> None:
            try:
                adb = self.selected_adb()
                output.mkdir(parents=True, exist_ok=True)
                package_output.mkdir(parents=True, exist_ok=True)
                self.ensure_device(adb, device)
                remote_manifest = self.read_remote_manifest(adb, device, remote)
                manifest_path = package_output / SYNC_MANIFEST_NAME
                previous_manifest = load_sync_manifest(manifest_path)
                changed = changed_remote_files(
                    package_output, remote_manifest, previous_manifest
                )
                removed_remote = len(set(previous_manifest) - set(remote_manifest))
                unchanged = len(remote_manifest) - len(changed)
                self.events.put(
                    (
                        "log",
                        f"增量比较完成：需下载 {len(changed)}，"
                        f"未变化 {unchanged}，远端已删除 {removed_remote}（本地保留）。",
                    )
                )
                if self.cancel_requested:
                    self.events.put(("done", "增量拉取已停止。"))
                    return
                if not changed:
                    save_sync_manifest(manifest_path, remote_manifest)
                    self.events.put(("done", "检查完成：本地资源已是最新。"))
                    return

                remote_root = remote.rstrip("/")
                batches = pull_batches(changed, remote_prefix=remote_root + "/")
                for number, batch in enumerate(batches, 1):
                    if self.cancel_requested:
                        self.events.put(("done", "增量拉取已停止。"))
                        return
                    parent = PurePosixPath(batch[0]).parent.as_posix()
                    local_parent = (
                        package_output
                        if parent in {"", "."}
                        else local_path_for_remote(package_output, parent)
                    )
                    local_parent.mkdir(parents=True, exist_ok=True)
                    remote_paths = [f"{remote_root}/{relative}" for relative in batch]
                    self.events.put(
                        (
                            "status",
                            f"增量下载 {number}/{len(batches)}；"
                            f"本批 {len(batch)} 个文件",
                        )
                    )
                    self.events.put(
                        (
                            "log",
                            f"下载批次 {number}/{len(batches)}："
                            f"{parent}（{len(batch)} 个文件）",
                        )
                    )
                    code, text = self.run_command(
                        [adb, "-s", device, "pull", "-a", *remote_paths, "."],
                        cwd=local_parent,
                        log_command=False,
                        log_output=False,
                    )
                    if self.cancel_requested:
                        self.events.put(("done", "增量拉取已停止。"))
                        return
                    if code != 0:
                        raise RuntimeError(
                            text.strip() or f"ADB 增量拉取失败，退出码 {code}。"
                        )
                    summary = next(
                        (line.strip() for line in reversed(text.splitlines()) if line.strip()),
                        "本批下载完成。",
                    )
                    self.events.put(("log", summary))

                save_sync_manifest(manifest_path, remote_manifest)
                self.events.put(
                    (
                        "done",
                        f"增量更新完成：下载 {len(changed)} 个文件；"
                        f"未变化 {unchanged} 个。",
                    )
                )
            except InterruptedError:
                self.events.put(("done", "增量拉取已停止。"))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        self.start_worker(worker, "正在检查增量更新…")

    def update_project(self) -> None:
        if not messagebox.askokcancel(
            "更新项目",
            "将从 GitHub 拉取项目最新内容。更新成功后程序会自动重启。\n\n"
            "请先停止其他正在编辑或运行的项目文件。",
        ):
            return

        def worker() -> None:
            try:
                code, output = self.run_command(
                    ["git", "pull", "--ff-only", "origin", "main"],
                    cwd=APP_DIR,
                )
                if code != 0:
                    raise RuntimeError(output.strip() or "GitHub 更新失败。")
                self.events.put(("restart", "项目更新完成，正在重启…"))
            except OSError as exc:
                self.events.put(("error", f"无法执行 Git 更新，请确认已安装 Git：{exc}"))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        self.start_worker(worker, "正在从 GitHub 更新项目…")

    def restart_application(self) -> None:
        self.save_settings()
        script = Path(sys.argv[0]).resolve()
        command = [sys.executable, str(script), *sys.argv[1:]]
        try:
            subprocess.Popen(
                command,
                cwd=str(APP_DIR),
                creationflags=subprocess_flags(),
            )
        except OSError as exc:
            self.set_busy(False, "项目已更新，但自动重启失败。")
            self.append_log(f"错误：{exc}")
            messagebox.showerror("重启失败", f"项目已更新，请手动重新启动工具。\n\n{exc}")
            return
        self.root.after(150, self.root.destroy)

    def cancel(self) -> None:
        self.cancel_requested = True
        process = self.current_process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                self.append_log("已请求停止当前 ADB 进程。")
            except OSError as exc:
                self.append_log(f"停止失败：{exc}")

    def poll_events(self) -> None:
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.append_log(str(value))
            elif kind == "devices":
                records = list(value) if isinstance(value, list) else []
                self.discovered_devices = {
                    record["serial"]: record
                    for record in records
                    if isinstance(record, dict) and record.get("serial")
                }
                self.device_labels = {
                    format_device_label(record): record["serial"]
                    for record in self.discovered_devices.values()
                }
                devices = list(self.device_labels)
                self.device_combo.configure(values=devices)
                current_serial = self.device_var.get().strip()
                selected_label = next(
                    (label for label, serial in self.device_labels.items() if serial == current_serial),
                    devices[0] if devices else "",
                )
                if selected_label:
                    self.device_display_var.set(selected_label)
                    self.device_var.set(self.device_labels[selected_label])
                if devices:
                    self.on_device_selected()
            elif kind == "remote":
                self.remote_var.set(str(value))
            elif kind == "restart":
                self.set_busy(False, str(value))
                self.append_log(str(value))
                self.restart_application()
            elif kind == "done":
                self.set_busy(False, str(value))
                self.append_log(str(value))
            elif kind == "error":
                self.set_busy(False, "操作失败。")
                self.append_log("错误：" + str(value))
                messagebox.showerror("操作失败", str(value))
        self.root.after(100, self.poll_events)

    def on_close(self) -> None:
        if self.busy and not messagebox.askyesno("确认退出", "当前任务仍在运行，确定要停止并退出吗？"):
            return
        self.save_settings()
        self.cancel()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    ResourcePullApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
