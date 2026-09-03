from __future__ import annotations

import json
import os
import queue
import shlex
import shutil
import subprocess
import threading
import tkinter as tk
from pathlib import Path, PurePosixPath
from tkinter import filedialog, messagebox, ttk


APP_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = APP_DIR / ".resource_pull_settings.json"
DEFAULT_DEVICE = "127.0.0.1:7555"
DEFAULT_REMOTE = "/sdcard/Android/data/com.netease.onmyoji.wyzymnqsd_cps"
SYNC_MANIFEST_NAME = ".yys_sync_manifest.json"
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


def onmyoji_package_candidates(package_output: str) -> list[str]:
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
        if "onmyoji" in lowered:
            packages.append(name)
    known = [
        "com.netease.onmyoji.wyzymnqsd_cps",
        "com.netease.onmyoji.wyzymnqsd",
        "com.netease.onmyoji",
    ]
    return list(dict.fromkeys([*packages, *known]))


def subprocess_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def load_settings() -> dict[str, str]:
    defaults = {
        "adb": shutil.which("adb") or "adb",
        "mumu_dir": "",
        "device": DEFAULT_DEVICE,
        "remote": DEFAULT_REMOTE,
        "output": str(APP_DIR / "yys"),
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
        self.root.title("阴阳师资源拉取工具")
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

        ttk.Label(outer, text="Android 源目录").grid(row=3, column=0, sticky="w", pady=5)
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

        note = (
            "先选择 MuMu 目录并检测设备，再从下拉框选择实例；工具会自动查找阴阳师目录。"
            "默认保存到本工具所在目录下的 yys；所有字段均可修改并会保存在本机。"
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
            actions, text="完整拉取全部资源", command=self.start_pull
        )
        self.pull_button.pack(side="left", padx=8)
        self.incremental_button = ttk.Button(
            actions, text="增量拉取更新", command=self.start_incremental_pull
        )
        self.incremental_button.pack(side="left")
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
        self, adb: str, device: str, remote: str
    ) -> dict[str, dict[str, int]]:
        remote_root = remote.rstrip("/")
        shell_command = (
            f"find {shlex.quote(remote_root)} -type f -print0 | "
            "xargs -0 -n 100 stat -c '%s|%Y|%n'"
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
                packages = onmyoji_package_candidates(packages_output)
                bases = ["/sdcard/Android/data", "/storage/emulated/0/Android/data"]
                paths = [f"{base}/{package}" for base in bases for package in packages]
                for remote in paths:
                    code, listing = self.run_command(
                        [adb, "-s", device, "shell", "test", "-d", remote],
                        log_output=False,
                    )
                    if code == 0:
                        self.events.put(("remote", remote))
                        self.events.put(("log", f"已自动找到阴阳师目录：{remote}"))
                        return
                # 某些渠道包的包名不含 onmyoji，最后从 Android/data 目录名兜底筛选。
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
                        if line.strip() and "onmyoji" in PurePosixPath(line.strip()).name.lower()
                    ]
                    if matches:
                        self.events.put(("remote", matches[0]))
                        self.events.put(("log", f"已自动找到阴阳师目录：{matches[0]}"))
                        return
                self.events.put(("log", "未自动找到阴阳师目录，请手动填写 Android 源目录。"))
            except Exception as exc:
                self.events.put(("log", f"自动查找阴阳师目录失败：{exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def start_pull(self) -> None:
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
