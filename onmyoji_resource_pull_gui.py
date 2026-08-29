from __future__ import annotations

import json
import os
import queue
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


def subprocess_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def load_settings() -> dict[str, str]:
    defaults = {
        "adb": shutil.which("adb") or "adb",
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


class ResourcePullApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("阴阳师资源拉取工具")
        self.root.geometry("860x620")
        self.root.minsize(720, 520)

        settings = load_settings()
        self.adb_var = tk.StringVar(value=settings["adb"])
        self.device_var = tk.StringVar(value=settings["device"])
        self.remote_var = tk.StringVar(value=settings["remote"])
        self.output_var = tk.StringVar(value=settings["output"])
        self.status_var = tk.StringVar(value="请确认设备和目录，然后开始拉取。")

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.current_process: subprocess.Popen[str] | None = None
        self.cancel_requested = False
        self.busy = False

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.poll_events)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(6, weight=1)

        ttk.Label(outer, text="ADB 程序").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(outer, textvariable=self.adb_var).grid(
            row=0, column=1, sticky="ew", padx=(10, 6), pady=5
        )
        ttk.Button(outer, text="浏览…", command=self.choose_adb).grid(
            row=0, column=2, sticky="ew", pady=5
        )

        ttk.Label(outer, text="设备地址/序列号").grid(row=1, column=0, sticky="w", pady=5)
        self.device_combo = ttk.Combobox(outer, textvariable=self.device_var)
        self.device_combo.grid(row=1, column=1, sticky="ew", padx=(10, 6), pady=5)
        ttk.Button(outer, text="检测设备", command=self.refresh_devices).grid(
            row=1, column=2, sticky="ew", pady=5
        )

        ttk.Label(outer, text="Android 源目录").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(outer, textvariable=self.remote_var).grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=5
        )

        ttk.Label(outer, text="本地保存目录").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Entry(outer, textvariable=self.output_var).grid(
            row=3, column=1, sticky="ew", padx=(10, 6), pady=5
        )
        ttk.Button(outer, text="选择…", command=self.choose_output).grid(
            row=3, column=2, sticky="ew", pady=5
        )

        note = (
            "默认保存到本工具所在目录下的 yys；所有字段均可修改并会保存在本机。"
            "拉取前请先在模拟器内完成游戏更新。"
        )
        ttk.Label(outer, text=note, foreground="#555555", wraplength=790).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(6, 10)
        )

        actions = ttk.Frame(outer)
        actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        self.connect_button = ttk.Button(actions, text="连接并检测", command=self.connect_device)
        self.connect_button.pack(side="left")
        self.pull_button = ttk.Button(actions, text="开始拉取完整资源", command=self.start_pull)
        self.pull_button.pack(side="left", padx=8)
        self.cancel_button = ttk.Button(
            actions, text="停止", command=self.cancel, state="disabled"
        )
        self.cancel_button.pack(side="left")
        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=180)
        self.progress.pack(side="right", padx=(10, 0))

        log_frame = ttk.LabelFrame(outer, text="运行日志", padding=6)
        log_frame.grid(row=6, column=0, columnspan=3, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(log_frame, wrap="word", height=16, state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scrollbar.set)

        ttk.Label(outer, textvariable=self.status_var, anchor="w").grid(
            row=7, column=0, columnspan=3, sticky="ew", pady=(8, 0)
        )

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
        self, command: list[str], *, cwd: Path | None = None
    ) -> tuple[int, str]:
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

    def refresh_devices(self) -> None:
        def worker() -> None:
            try:
                adb = self.resolve_adb()
                code, output = self.run_command([adb, "devices"])
                if code != 0:
                    raise RuntimeError(output.strip() or "无法读取 ADB 设备列表。")
                devices = []
                for line in output.splitlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == "device":
                        devices.append(parts[0])
                self.events.put(("devices", devices))
                self.events.put(("done", f"检测完成，共发现 {len(devices)} 台可用设备。"))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        self.start_worker(worker, "正在检测 ADB 设备…")

    def connect_device(self) -> None:
        def worker() -> None:
            try:
                adb = self.resolve_adb()
                device = self.device_var.get().strip()
                if not device:
                    raise RuntimeError("请输入设备地址或序列号。")
                self.ensure_device(adb, device)
                self.events.put(("done", f"设备连接正常：{device}"))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        self.start_worker(worker, "正在连接并检测设备…")

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
            "开始拉取",
            f"将从设备 {device} 拉取：\n{remote}\n\n保存到：\n{package_output}\n\n"
            "目录已有文件会由 ADB 增量更新，过程可能耗时较长。",
        ):
            return

        def worker() -> None:
            try:
                adb = self.resolve_adb()
                output.mkdir(parents=True, exist_ok=True)
                package_output.mkdir(parents=True, exist_ok=True)
                self.ensure_device(adb, device)
                # MuMu 附带的 ADB 在包含中文的绝对 Windows 目标路径下，可能无法
                # 递归创建子目录。让系统先进入已创建的包目录，再将纯 ASCII 的
                # "." 交给 ADB，既避开路径编码问题，也保证父目录一定存在。
                code, text = self.run_command(
                    [adb, "-s", device, "pull", remote_contents, "."],
                    cwd=package_output,
                )
                if self.cancel_requested:
                    self.events.put(("done", "拉取已停止。"))
                elif code != 0:
                    raise RuntimeError(text.strip() or f"ADB 拉取失败，退出码 {code}。")
                else:
                    self.events.put(("done", f"资源拉取完成：{package_output}"))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        self.start_worker(worker, "正在拉取完整游戏资源…")

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
                devices = list(value) if isinstance(value, list) else []
                self.device_combo.configure(values=devices)
                if devices and self.device_var.get().strip() not in devices:
                    self.device_var.set(devices[0])
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
