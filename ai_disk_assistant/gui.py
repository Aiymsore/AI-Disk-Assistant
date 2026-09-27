"""图形界面层（Tkinter）：WizTree 式磁盘浏览器 + AI 文件介绍 + 勾选手动删除。

布局：顶栏（选卷/扫描/暂停取消/删除勾选 + AI 工具 + 权限）→ 空间头（总/已用/可用/耗时）→
主区（左侧当前目录列表：行首方框标记删除、目录在前、大小降序、双击下钻；右侧扩展名分类面板）→
底部（选中让 AI 介绍"是什么/删除影响/处理建议" + 变色结果列表）。不做全量树与 treemap：
浏览式下钻一次只查一层（SQLite 毫秒级）。
扫描支持暂停/继续/取消与百分比进度（ScanControl）。
评审不经过本地守卫与删除建议管线：AI 介绍是纯展示任务（describe_items），
失败直接报错呈现；唯一的执行级保护在 cleaner 层（受保护目录拒绝移入回收站）。
自动流程（扫描/介绍/报告/综述）不做任何删除动作；唯一删除入口是行首方框勾选 +
「删除勾选」按钮二次确认，由 cleaner.py 执行（移入回收站）。
"""

from __future__ import annotations

import os
import queue
import string
import subprocess
import sys
import threading
import time
from pathlib import Path
from tkinter import (
    BOTH,
    END,
    HORIZONTAL,
    LEFT,
    RIGHT,
    VERTICAL,
    BooleanVar,
    PhotoImage,
    StringVar,
    Text,
    TclError,
    Tk,
    Toplevel,
    messagebox,
    ttk,
)

from . import __version__
from .admin import analysis_blockers, is_user_admin, relaunch_as_admin
from .ai_advisor import build_advisor
from .cleaner import DeletionPlan, plan_deletion, prune_empty_dirs, recycle_paths
from .config import (
    DEFAULT_CACHE_PATH,
    DEFAULT_INVENTORY_PATH,
    DEFAULT_USER_AGENT,
    Settings,
    default_env_path,
    normalize_api_style,
    read_dotenv,
    update_dotenv,
)
from .inventory import DirAgg, FileRow, Inventory
from .metadata import format_mtime, format_pct, format_size
from .mft_scanner import MftError, ScanCancelled, ScanControl, snapshot_volume, volume_capacity
from .models import FileDescription
from .overview import build_overview_payload
from .privacy import anonymize_path
from .report import default_report_path, write_overview_markdown


# 处理建议 → 行前景色：可清理是"放心动手"用绿，别动是警告用红，需核对居中用橙。
HANDLE_COLORS = {
    "可清理": "#1e8449",
    "需核对": "#d68910",
    "别动": "#c0392b",
}

# 单次 AI 介绍的目标数上限：一次请求批量可控，超出按体积取前 N 项。
_DESCRIBE_MAX_ITEMS = 100

_DIR_TAG = "dir"
_FILE_TAG = "file"
_STRIPE_TAG = "stripe"

# 行首删除勾选方框（Treeview 无原生复选框，用字符形绘制，点击行首列切换）。
_CHECK_ON = "☑"
_CHECK_OFF = "□"

# ── 视觉设计系统（纯 ttk 实现，零第三方依赖）──────────────────────────────
# 白底工作区 + 浅灰面板 + 蓝色主操作色；等级色沿用 LEVEL_COLORS。
COLOR_BG = "#f4f5f7"  # 窗口底色/工具面板
COLOR_SURFACE = "#ffffff"  # 表格工作区
COLOR_BORDER = "#e3e6ea"  # 描边
COLOR_INK = "#1f2430"  # 主文字
COLOR_MUTED = "#6b7280"  # 次要文字
COLOR_ACCENT = "#2563eb"  # 主操作（扫描/评审）
COLOR_ACCENT_HOVER = "#1d4ed8"
COLOR_ACCENT_SOFT = "#dbeafe"  # 选中行底色
COLOR_STRIPE = "#f7f8fa"  # 表格斑马纹
COLOR_DIR_ROW = "#eef3fb"  # 目录行底色
COLOR_DANGER = "#c0392b"  # 删除操作（与"建议删除"等级色一致）
COLOR_DANGER_HOVER = "#a93226"
APP_NAME = "P4Disk4P"
FONT_FAMILY = "Microsoft YaHei UI"


def _enable_windows_dpi_awareness() -> None:
    """让进程按真实 DPI 原生渲染。

    Tk 进程默认不是 DPI 感知的：高缩放屏（如 200%）上 Windows 会把 96 DPI 的
    窗口整窗位图拉伸——这就是文字发糊的根源。必须在创建 Tk 窗口之前调用。
    """
    if not sys.platform.startswith("win"):
        return
    import ctypes

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # SYSTEM_AWARE
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def _ui_scale(root: Tk) -> float:
    """真实 DPI 相对 96 的倍率：所有像素尺寸（行高/内边距/图标）按它缩放。"""
    if not sys.platform.startswith("win"):
        return 1.0
    import ctypes

    try:
        # GetDpiForSystem 在 user32（不是 shcore）；进程 DPI 感知后返回真实系统 DPI。
        dpi = ctypes.windll.user32.GetDpiForSystem()
    except (AttributeError, OSError):
        return 1.0
    return max(dpi / 96.0, 1.0)


def _setup_style(root: Tk) -> None:
    """全局 ttk 视觉：clam 底座 + 自定义色板/字体/控件样式，统一窗口观感。"""
    style = ttk.Style(root)
    style.theme_use("clam")

    scale = _ui_scale(root)
    # 字体用磅值，由 tk scaling 按 DPI 自动换算；显式设置确保高缩放屏下不出错。
    root.tk.call("tk", "scaling", 96.0 / 72.0 * scale)
    style.configure(".", background=COLOR_BG, foreground=COLOR_INK, font=(FONT_FAMILY, 9))
    style.configure("TFrame", background=COLOR_BG)
    style.configure("TLabel", background=COLOR_BG, foreground=COLOR_INK)
    style.configure("Muted.TLabel", foreground=COLOR_MUTED)
    style.configure("Header.TLabel", font=(FONT_FAMILY, 11, "bold"))

    # 按钮：普通操作白底描边，主操作（Primary）实心蓝
    style.configure(
        "TButton",
        background=COLOR_SURFACE,
        foreground=COLOR_INK,
        bordercolor=COLOR_BORDER,
        lightcolor=COLOR_SURFACE,
        darkcolor=COLOR_SURFACE,
        padding=(round(12 * scale), round(4 * scale)),
        relief="flat",
    )
    style.map(
        "TButton",
        background=[("disabled", "#eceef1"), ("pressed", "#e8ebf0"), ("active", "#f0f3f8")],
        foreground=[("disabled", "#9aa1ab")],
    )
    style.configure(
        "Primary.TButton",
        background=COLOR_ACCENT,
        foreground="#ffffff",
        bordercolor=COLOR_ACCENT,
        lightcolor=COLOR_ACCENT,
        darkcolor=COLOR_ACCENT,
    )
    style.map(
        "Primary.TButton",
        background=[("disabled", "#93b4f5"), ("pressed", COLOR_ACCENT_HOVER), ("active", COLOR_ACCENT_HOVER)],
        foreground=[("disabled", "#eef2ff")],
    )
    style.configure(
        "Danger.TButton",
        background=COLOR_DANGER,
        foreground="#ffffff",
        bordercolor=COLOR_DANGER,
        lightcolor=COLOR_DANGER,
        darkcolor=COLOR_DANGER,
    )
    style.map(
        "Danger.TButton",
        background=[("disabled", "#e5a79d"), ("pressed", COLOR_DANGER_HOVER), ("active", COLOR_DANGER_HOVER)],
        foreground=[("disabled", "#fdf3f1")],
    )

    style.configure(
        "TCombobox",
        fieldbackground=COLOR_SURFACE,
        background=COLOR_SURFACE,
        bordercolor=COLOR_BORDER,
        arrowcolor=COLOR_INK,
        padding=3,
    )
    style.map("TCombobox", fieldbackground=[("readonly", COLOR_SURFACE)])
    style.configure("TEntry", fieldbackground=COLOR_SURFACE, bordercolor=COLOR_BORDER, padding=3)

    style.configure(
        "Horizontal.TProgressbar",
        background=COLOR_ACCENT,
        troughcolor="#e5e8ee",
        bordercolor=COLOR_BG,
        lightcolor=COLOR_ACCENT,
        darkcolor=COLOR_ACCENT,
        thickness=round(8 * scale),
    )

    style.configure(
        "Treeview",
        background=COLOR_SURFACE,
        fieldbackground=COLOR_SURFACE,
        foreground=COLOR_INK,
        bordercolor=COLOR_BORDER,
        rowheight=round(28 * scale),
        font=(FONT_FAMILY, 9),
    )
    style.configure(
        "Treeview.Heading",
        background="#eef0f4",
        foreground=COLOR_INK,
        relief="flat",
        font=(FONT_FAMILY, 9, "bold"),
        padding=(round(6 * scale), round(5 * scale)),
    )
    style.map("Treeview.Heading", background=[("active", "#e3e6ea")])
    style.map(
        "Treeview",
        background=[("selected", COLOR_ACCENT_SOFT)],
        foreground=[("selected", COLOR_INK)],
    )

    style.configure(
        "Vertical.TScrollbar",
        background=COLOR_BG,
        troughcolor=COLOR_BG,
        bordercolor=COLOR_BG,
        arrowcolor=COLOR_MUTED,
        relief="flat",
    )
    style.map("Vertical.TScrollbar", background=[("active", "#d5d9e0")])

    style.configure("TPanedwindow", background=COLOR_BG, bordercolor=COLOR_BG)
    # 卡片：白色面板 + 1px 描边，包裹各表格区域
    style.configure(
        "Card.TFrame",
        background=COLOR_SURFACE,
        bordercolor=COLOR_BORDER,
        relief="solid",
        borderwidth=1,
    )


def list_volumes() -> list[str]:
    """枚举本机盘符（C:\、D:\……）。"""
    volumes = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if Path(root).exists():
            volumes.append(root)
    return volumes


# ── 通用小工具 ───────────────────────────────────────────────────────────
def _open_path(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def _open_in_explorer(path: Path) -> None:
    if sys.platform.startswith("win"):
        subprocess.run(["explorer", "/select,", str(path)], check=False)
    else:
        _open_path(path.parent if path.is_file() else path)


# ── AI 配置对话框：.env 文件的图形编辑器 ─────────────────────────────────
class AIConfigDialog:
    """Small GUI editor for the runtime ``.env`` AI configuration."""

    def __init__(self, parent: Tk, on_saved) -> None:
        self.parent = parent
        self.on_saved = on_saved
        self.window = Toplevel(parent)
        self.window.title("AI 配置")
        self.window.resizable(False, False)
        self.window.transient(parent)
        self.window.grab_set()

        file_values = read_dotenv()
        try:
            settings = Settings.from_env()
        except ValueError:
            settings = Settings(None, "https://api.openai.com/v1", "", 30.0)

        self.key_var = StringVar(value=file_values.get("AI_API_KEY", settings.ai_api_key or ""))
        self.base_url_var = StringVar(value=file_values.get("AI_BASE_URL", settings.ai_base_url))
        self.model_var = StringVar(value=file_values.get("AI_MODEL", settings.ai_model))
        self.api_style_var = StringVar(value=file_values.get("AI_API_STYLE", settings.ai_api_style))
        self.timeout_var = StringVar(value=file_values.get("AI_TIMEOUT", str(settings.ai_timeout)))
        self.batch_size_var = StringVar(value=file_values.get("AI_BATCH_SIZE", str(settings.ai_batch_size)))
        self.max_retries_var = StringVar(value=file_values.get("AI_MAX_RETRIES", str(settings.ai_max_retries)))
        self.retry_backoff_var = StringVar(
            value=file_values.get("AI_RETRY_BACKOFF", str(settings.ai_retry_backoff))
        )
        self.cache_path_var = StringVar(value=file_values.get("AI_CACHE_PATH", settings.ai_cache_path))
        self.show_key_var = BooleanVar(value=False)

        self._build()
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)
        self.window.focus_set()

    def _build(self) -> None:
        frame = ttk.Frame(self.window, padding=16)
        frame.pack(fill="both", expand=True)

        rows = [
            ("API Key", self.key_var),
            ("Base URL", self.base_url_var),
            ("模型 ID", self.model_var),
            ("超时（秒）", self.timeout_var),
            ("批量大小", self.batch_size_var),
            ("最大重试", self.max_retries_var),
            ("退避秒数", self.retry_backoff_var),
            ("缓存路径", self.cache_path_var),
        ]
        self.entries: dict[str, ttk.Entry] = {}
        for row, (label, variable) in enumerate(rows):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", pady=5)
            entry = ttk.Entry(frame, textvariable=variable, width=52)
            entry.grid(row=row, column=1, columnspan=3, sticky="ew", padx=(10, 0), pady=5)
            self.entries[label] = entry

        self.entries["API Key"].configure(show="*")
        ttk.Checkbutton(
            frame,
            text="显示密钥",
            variable=self.show_key_var,
            command=self._toggle_key,
        ).grid(row=0, column=4, padx=(8, 0), sticky="w")

        protocol_row = len(rows)
        ttk.Label(frame, text="接口协议").grid(row=protocol_row, column=0, sticky="w", pady=5)
        ttk.Combobox(
            frame,
            textvariable=self.api_style_var,
            values=("responses", "chat_completions"),
            state="readonly",
            width=20,
        ).grid(row=protocol_row, column=1, sticky="w", padx=(10, 0), pady=5)

        env_path = default_env_path()
        ttk.Label(
            frame,
            text=f"配置文件：{env_path}",
            foreground="#555555",
        ).grid(row=protocol_row + 1, column=0, columnspan=5, sticky="w", pady=(10, 4))

        buttons = ttk.Frame(frame)
        buttons.grid(row=protocol_row + 2, column=0, columnspan=5, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="打开 .env", command=self._open_env).pack(side=LEFT, padx=4)
        ttk.Button(buttons, text="取消", command=self.window.destroy).pack(side=LEFT, padx=4)
        ttk.Button(buttons, text="保存", command=lambda: self._save(False)).pack(side=LEFT, padx=4)
        ttk.Button(buttons, text="保存并测试", command=lambda: self._save(True)).pack(side=LEFT, padx=4)

        frame.columnconfigure(1, weight=1)
        frame.columnconfigure(3, weight=1)

    def _toggle_key(self) -> None:
        self.entries["API Key"].configure(show="" if self.show_key_var.get() else "*")

    def _open_env(self) -> None:
        path = default_env_path()
        if not path.exists():
            path.write_text("# P4Disk4P runtime configuration\n", encoding="utf-8")
        _open_path(path)

    def _save(self, test_after_save: bool) -> None:
        key = self.key_var.get().strip()
        base_url = self.base_url_var.get().strip().rstrip("/")
        model = self.model_var.get().strip()
        cache_path = self.cache_path_var.get().strip() or DEFAULT_CACHE_PATH

        if not key:
            messagebox.showerror("配置错误", "API Key 不能为空。", parent=self.window)
            return
        if not base_url.startswith(("https://", "http://")):
            messagebox.showerror("配置错误", "Base URL 必须以 http:// 或 https:// 开头。", parent=self.window)
            return
        if not model:
            messagebox.showerror("配置错误", "模型 ID 不能为空。", parent=self.window)
            return

        try:
            api_style = normalize_api_style(self.api_style_var.get())
            timeout = max(float(self.timeout_var.get()), 1.0)
            batch_size = max(int(self.batch_size_var.get()), 1)
            max_retries = max(int(self.max_retries_var.get()), 0)
            retry_backoff = max(float(self.retry_backoff_var.get()), 0.0)
        except ValueError as exc:
            messagebox.showerror("配置错误", f"数值参数无效：{exc}", parent=self.window)
            return

        path = update_dotenv(
            {
                "AI_API_KEY": key,
                "AI_BASE_URL": base_url,
                "AI_MODEL": model,
                "AI_API_STYLE": api_style,
                "AI_TIMEOUT": str(timeout),
                "AI_BATCH_SIZE": str(batch_size),
                "AI_MAX_RETRIES": str(max_retries),
                "AI_RETRY_BACKOFF": str(retry_backoff),
                "AI_CACHE_PATH": cache_path,
                # 隐私模式统一为 balanced（匿名化用户名），不再暴露选择。
                "AI_PRIVACY_MODE": "balanced",
                "AI_USER_AGENT": DEFAULT_USER_AGENT,
            }
        )
        self.window.destroy()
        messagebox.showinfo("配置已保存", f"AI 配置已保存到：\n{path}", parent=self.parent)
        self.on_saved(test_after_save)


# ── 主窗口 ───────────────────────────────────────────────────────────────
class DiskAssistantGUI:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title(f"{APP_NAME} v{__version__}")
        self._dpi_scale = _ui_scale(root)
        self.root.geometry(f"{round(1280 * self._dpi_scale)}x{round(800 * self._dpi_scale)}")
        self._logo_images: list[PhotoImage] = []  # 防 GC：PhotoImage 必须持有引用
        icon = self._load_logo(32)
        if icon is not None:
            self.root.iconphoto(True, icon)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.last_html: Path | None = None
        self.reviewed_descriptions: list[FileDescription] = []
        self._busy = False
        # 行首方框的删除标记（按路径），跨目录导航保留；重新扫描后清空。
        self._delete_marks: set[str] = set()

        self.inventory = Inventory(DEFAULT_INVENTORY_PATH)
        self.snapshot_id: int | None = None
        self.scan_control: ScanControl | None = None
        self.scan_started_at = 0.0
        self.current_dir = ""
        self.parent_total = 0  # 当前目录的递归总大小（百分比分母）
        self.loose_rows: dict[str, FileRow] = {}  # 当前目录散文件的行事实（评审用）

        volumes = list_volumes()
        self.volume_var = StringVar(value=volumes[-1] if volumes else "C:\\")
        self.status_var = StringVar(value="选择卷并开始扫描（前置条件：管理员）。")
        self.space_var = StringVar(value="总空间 — │ 已用 — │ 可用 —")
        self.used_bytes = 0

        self._build()
        self._update_capacity()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_events)

    # ── 布局 ─────────────────────────────────────────────────────────────
    def _build(self) -> None:
        def px(value: float) -> int:
            """逻辑像素 → 物理像素：DPI 感知后所有像素尺寸都要按倍率放大。"""
            return round(value * self._dpi_scale)

        # 顶栏：品牌 + 卷选择 + 扫描控制（左），AI 工具（右）
        top = ttk.Frame(self.root, padding=(px(14), px(10), px(14), px(6)))
        top.pack(fill="x")
        logo = self._load_logo(28)
        if logo is not None:
            ttk.Label(top, image=logo).pack(side=LEFT, padx=(0, px(8)))
        ttk.Label(top, text=APP_NAME, style="Header.TLabel").pack(side=LEFT)
        ttk.Label(top, text=f"v{__version__}", style="Muted.TLabel").pack(side=LEFT, padx=(px(6), px(16)))
        ttk.Label(top, text="卷").pack(side=LEFT)
        self.volume_box = ttk.Combobox(
            top, textvariable=self.volume_var, values=list_volumes(), width=8, state="readonly"
        )
        self.volume_box.pack(side=LEFT, padx=(px(6), 0))
        self.volume_box.bind("<<ComboboxSelected>>", lambda _e: self._update_capacity())
        self.scan_button = ttk.Button(
            top, text="开始扫描", style="Primary.TButton", command=self._start_scan
        )
        self.scan_button.pack(side=LEFT, padx=(px(12), 0))
        self.pause_button = ttk.Button(
            top, text="暂停", command=self._toggle_pause, state="disabled", width=8
        )
        self.pause_button.pack(side=LEFT, padx=(px(6), 0))
        self.cancel_button = ttk.Button(
            top, text="取消", command=self._cancel_scan, state="disabled", width=6
        )
        self.cancel_button.pack(side=LEFT, padx=(px(6), 0))
        # 手动删除：与扫描/暂停同一行；只有行首方框勾选后可用，点击后二次确认。
        self.delete_button = ttk.Button(
            top, text="删除勾选", style="Danger.TButton", command=self._delete_checked, state="disabled"
        )
        self.delete_button.pack(side=LEFT, padx=(px(12), 0))
        self.admin_button = ttk.Button(top, text="以管理员重启", command=self._restart_as_admin)
        self.admin_button.pack(side=RIGHT)
        self.ai_config_button = ttk.Button(top, text="AI 配置", command=self._open_ai_config)
        self.ai_config_button.pack(side=RIGHT, padx=(0, px(8)))
        self.ai_test_button = ttk.Button(top, text="测试 AI 连接", command=self._start_ai_test)
        self.ai_test_button.pack(side=RIGHT, padx=(0, px(8)))

        # 空间头：容量统计 + 权限 + 扫描进度
        space = ttk.Frame(self.root, padding=(px(14), px(2), px(14), px(4)))
        space.pack(fill="x")
        ttk.Label(space, textvariable=self.space_var, style="Header.TLabel").pack(side=LEFT)
        admin_state = "已提权" if is_user_admin() else "未提权"
        ttk.Label(space, text=f"权限：{admin_state}", style="Muted.TLabel").pack(side=RIGHT)
        self.progress = ttk.Progressbar(space, mode="determinate", length=px(260))
        self.progress.pack(side=RIGHT, padx=(0, px(12)))

        # 地址行：返回上级 + 当前状态
        address = ttk.Frame(self.root, padding=(px(14), 0, px(14), px(4)))
        address.pack(fill="x")
        ttk.Button(address, text="返回上级", command=self._go_up).pack(side=LEFT)
        ttk.Label(address, textvariable=self.status_var, style="Muted.TLabel").pack(side=LEFT, padx=(px(12), 0))

        main = ttk.PanedWindow(self.root, orient=HORIZONTAL)
        main.pack(fill=BOTH, expand=True, padx=px(14), pady=(px(4), 0))

        # 左：当前目录列表（行首方框标记删除；目录在前、大小降序，双击进入；多选后送评审）
        list_frame = ttk.Frame(main, style="Card.TFrame", padding=1)
        list_columns = ("check", "name", "pct", "size", "items", "mtime")
        self.list_tree = ttk.Treeview(
            list_frame, columns=list_columns, show="headings", selectmode="extended"
        )
        list_headings = {
            "check": "全选",
            "name": "名称",
            "pct": "父级百分比",
            "size": "大小",
            "items": "项数",
            "mtime": "修改时间",
        }
        list_widths = {"check": 48, "name": 320, "pct": 90, "size": 100, "items": 80, "mtime": 150}
        for column in list_columns:
            self.list_tree.heading(column, text=list_headings[column])
            self.list_tree.column(column, width=px(list_widths[column]), minwidth=px(60))
        # 勾选列：居中、不随窗口拉伸；点击表头对当前列表全选/反选。
        self.list_tree.column("check", minwidth=px(40), anchor="center", stretch=False)
        self.list_tree.heading("check", command=self._toggle_all_marks)
        self.list_tree.tag_configure(_DIR_TAG, background=COLOR_DIR_ROW)
        self.list_tree.tag_configure(_STRIPE_TAG, background=COLOR_STRIPE)
        for handle, color in HANDLE_COLORS.items():
            self.list_tree.tag_configure(f"desc:{handle}", foreground=color)
        list_scroll = ttk.Scrollbar(list_frame, orient=VERTICAL, command=self.list_tree.yview)
        self.list_tree.configure(yscrollcommand=list_scroll.set)
        self.list_tree.pack(side=LEFT, fill=BOTH, expand=True)
        list_scroll.pack(side=RIGHT, fill="y")
        self.list_tree.bind("<Button-1>", self._on_list_click)
        self.list_tree.bind("<Double-1>", self._on_double_click)
        main.add(list_frame, weight=3)

        # 右：扩展名分类面板（当前目录递归聚合）
        ext_frame = ttk.Frame(main, style="Card.TFrame", padding=1)
        ext_columns = ("suffix", "size", "files")
        self.ext_tree = ttk.Treeview(ext_frame, columns=ext_columns, show="headings")
        for column, text_value, width in (
            ("suffix", "扩展名", 110),
            ("size", "大小", 100),
            ("files", "文件数", 80),
        ):
            self.ext_tree.heading(column, text=text_value)
            self.ext_tree.column(column, width=px(width), minwidth=px(50))
        self.ext_tree.tag_configure(_STRIPE_TAG, background=COLOR_STRIPE)
        ext_scroll = ttk.Scrollbar(ext_frame, orient=VERTICAL, command=self.ext_tree.yview)
        self.ext_tree.configure(yscrollcommand=ext_scroll.set)
        self.ext_tree.pack(side=LEFT, fill=BOTH, expand=True)
        ext_scroll.pack(side=RIGHT, fill="y")
        main.add(ext_frame, weight=1)

        # 底部：选中送 AI 介绍 + 变色结果列表（与行首删除方框互不相干）
        review_bar = ttk.Frame(self.root, padding=(px(14), px(6), px(14), 0))
        review_bar.pack(fill="x")
        ttk.Label(
            review_bar,
            text="选中（高亮）目录/文件后让 AI 介绍它是什么、删除有何影响（行首方框是手动删除标记）：",
        ).pack(side=LEFT)
        # 折叠开关：默认全部显示（介绍本身就是有效信息），勾选后只看"可清理"项。
        self.only_cleanup_var = BooleanVar(value=False)
        ttk.Checkbutton(
            review_bar,
            text="只看可清理",
            variable=self.only_cleanup_var,
            command=self._apply_review_filter,
        ).pack(side=LEFT, padx=(px(12), 0))
        self.review_count_label = ttk.Label(review_bar, text="", style="Muted.TLabel")
        self.review_count_label.pack(side=LEFT, padx=(px(10), 0))
        self.overview_button = ttk.Button(
            review_bar,
            text="AI 深度分析（当前目录）",
            command=self._start_overview,
        )
        self.overview_button.pack(side=RIGHT, padx=(0, px(8)))
        self.review_button = ttk.Button(
            review_bar, text="AI 介绍所选", style="Primary.TButton", command=self._review_selected
        )
        self.review_button.pack(side=RIGHT)

        review_frame = ttk.Frame(self.root, style="Card.TFrame", padding=1)
        review_frame.pack(fill=BOTH, expand=True, padx=px(14), pady=(px(4), 0))
        review_columns = ("size", "handle", "what", "impact", "path")
        self.review_tree = ttk.Treeview(
            review_frame, columns=review_columns, show="headings", height=8
        )
        review_headings = {
            "size": "大小",
            "handle": "处理建议",
            "what": "是什么",
            "impact": "删除影响",
            "path": "路径",
        }
        review_widths = {
            "size": 90,
            "handle": 90,
            "what": 320,
            "impact": 300,
            "path": 330,
        }
        for column in review_columns:
            self.review_tree.heading(column, text=review_headings[column])
            self.review_tree.column(column, width=px(review_widths[column]), minwidth=px(60))
        for handle, color in HANDLE_COLORS.items():
            self.review_tree.tag_configure(handle, foreground=color)
        self.review_tree.tag_configure(_STRIPE_TAG, background=COLOR_STRIPE)
        review_scroll = ttk.Scrollbar(review_frame, orient=VERTICAL, command=self.review_tree.yview)
        self.review_tree.configure(yscrollcommand=review_scroll.set)
        self.review_tree.pack(side=LEFT, fill=BOTH, expand=True)
        review_scroll.pack(side=RIGHT, fill="y")

        status_bar = ttk.Frame(self.root, padding=(px(14), px(4), px(14), px(8)))
        status_bar.pack(fill="x")
        ttk.Label(status_bar, textvariable=self.status_var, style="Muted.TLabel").pack(anchor="w")

    # ── 顶栏动作 ─────────────────────────────────────────────────────────
    def _update_capacity(self) -> None:
        total, free, used = volume_capacity(self.volume_var.get())
        if total:
            pct = used * 100 // total
            self.space_var.set(
                f"总空间 {format_size(total)} │ 已用 {format_size(used)} ({pct}%) │ 可用 {format_size(free)}"
            )
        else:
            self.space_var.set("总空间 — │ 已用 — │ 可用 —")

    def _restart_as_admin(self) -> None:
        if is_user_admin():
            messagebox.showinfo("已是管理员", "当前程序已经以管理员身份运行。")
            return
        gui_entry = Path(__file__).resolve().parents[1] / "gui.py"
        if not relaunch_as_admin(gui_entry):
            messagebox.showerror("重启失败", "UAC 提权被取消或失败，未启动新进程。")
            return
        self.root.after(300, self._on_close)

    def _on_close(self) -> None:
        """关窗/提权重启的统一出口：先取消扫描，再立即终止进程。

        PyInstaller 单文件版退出时由引导器删除解包目录（%TEMP%\\_MEI…）；若解释器
        收尾时后台线程仍卡在扫描循环/AI 请求重试里，句柄未释放，引导器删不掉目录，
        会弹 "Failed to remove temporary directory" 警告框。这里先取消扫描再用
        os._exit 终止：全部句柄随进程终止一起释放，临时目录必能清掉。
        """
        if self.scan_control is not None:
            self.scan_control.cancel()
        self.root.destroy()
        os._exit(0)

    def _open_ai_config(self) -> None:
        AIConfigDialog(self.root, self._after_ai_config_saved)

    def _after_ai_config_saved(self, test_after_save: bool) -> None:
        if test_after_save:
            self._start_ai_test()

    def _start_ai_test(self) -> None:
        self._set_busy(True)
        self.status_var.set("正在测试 AI 接口和结构化返回……")
        threading.Thread(target=self._ai_test_worker, daemon=True).start()

    def _ai_test_worker(self) -> None:
        try:
            advisor = build_advisor(enable_ai=True)
            advice = advisor.probe()
            settings = advisor.settings
            self.events.put(("ai_test_done", (settings.ai_api_style, settings.ai_model, advice, advisor.stats)))
        except Exception as exc:
            self.events.put(("ai_test_error", exc))

    # ── 扫描（MFT，支持暂停/继续/取消）───────────────────────────────────
    def _start_scan(self) -> None:
        if not is_user_admin():
            detail = "MFT 直读需要管理员权限。\n\n点击\"以管理员重启\"可弹出 UAC 提权重启程序。"
            messagebox.showerror("无法开始扫描", detail)
            return
        drive = self.volume_var.get()
        if not Path(drive).exists():
            messagebox.showerror("卷无效", f"卷 {drive} 不存在。")
            return

        self.scan_control = ScanControl()
        self.scan_started_at = time.perf_counter()
        self._set_busy(True)
        self.pause_button.configure(state="normal", text="暂停")
        self.cancel_button.configure(state="normal")
        self.progress.configure(value=0)
        self.status_var.set(f"正在直读 {drive} 的 $MFT……")

        def progress(message: str) -> None:
            self.events.put(("progress_text", message))
            if "（" in message and "%）" in message:
                try:
                    pct = int(message.split("（")[1].split("%）")[0])
                    self.events.put(("progress_pct", pct))
                except (IndexError, ValueError):
                    pass

        def worker() -> None:
            try:
                snapshot_id, file_count = snapshot_volume(
                    drive, self.inventory, progress=progress, control=self.scan_control
                )
                elapsed = time.perf_counter() - self.scan_started_at
                self.events.put(("scan_done", (snapshot_id, file_count, drive, elapsed)))
            except ScanCancelled:
                self.events.put(("scan_cancelled", None))
            except MftError as exc:
                self.events.put(("scan_error", str(exc)))
            except Exception as exc:
                import traceback

                traceback.print_exc()
                detail = ""
                frames = traceback.extract_tb(exc.__traceback__)
                if frames:
                    last = frames[-1]
                    detail = f"\n\n位置：{Path(last.filename).name}:{last.lineno}（{last.name}）"
                self.events.put(("scan_error", f"{type(exc).__name__}: {exc}{detail}"))

        threading.Thread(target=worker, daemon=True).start()

    def _toggle_pause(self) -> None:
        if not self.scan_control:
            return
        if self.pause_button["text"] == "暂停":
            self.scan_control.pause()
            self.pause_button.configure(text="继续")
            self.status_var.set("扫描已暂停。")
        else:
            self.scan_control.resume()
            self.pause_button.configure(text="暂停")
            self.status_var.set("扫描继续……")

    def _cancel_scan(self) -> None:
        if self.scan_control:
            self.scan_control.cancel()
            self.status_var.set("正在取消扫描……")

    # ── 浏览 ─────────────────────────────────────────────────────────────
    def _enter_dir(self, directory: str, parent_total: int) -> None:
        if self.snapshot_id is None:
            return
        self.current_dir = directory
        self.parent_total = parent_total
        self.loose_rows = {row.path: row for row in self.inventory.direct_files(self.snapshot_id, directory)}
        for item in self.list_tree.get_children():
            self.list_tree.delete(item)

        status_extra = f"当前：{directory}"
        if directory.endswith(":\\"):
            # 根目录的子项百分比以全卷快照已用为分母。
            self.parent_total = max(self.used_bytes, 1)

        children = self.inventory.child_dirs(self.snapshot_id, directory)
        rows = []
        for child in children:
            name = Path(child.path).name
            mtime = format_mtime(child.mtime_ns)
            rows.append((name, format_pct(child.total_size, self.parent_total), format_size(child.total_size), str(child.file_count), mtime, child.path, _DIR_TAG, child.total_size))
        loose = sorted(
            self.loose_rows.values(),
            key=lambda row: row.size_bytes,
            reverse=True,
        )
        for row in loose:
            mtime = format_mtime(row.mtime_ns)
            rows.append((row.name, format_pct(row.size_bytes, self.parent_total), format_size(row.size_bytes), "—", mtime, row.path, _FILE_TAG, row.size_bytes))

        for index, (name, pct, size, items, mtime, path, tag, _sort_key) in enumerate(rows):
            tags = (tag, _STRIPE_TAG) if index % 2 else (tag,)
            check = _CHECK_ON if path in self._delete_marks else _CHECK_OFF
            self.list_tree.insert(
                "", END, iid=path, tags=tags, values=(check, name, pct, size, items, mtime)
            )

        self.ext_tree.delete(*self.ext_tree.get_children())
        for index, stats in enumerate(self.inventory.extension_stats(self.snapshot_id, directory)):
            tags = (_STRIPE_TAG,) if index % 2 else ()
            self.ext_tree.insert(
                "",
                END,
                tags=tags,
                values=(stats["suffix"], format_size(int(stats["size_bytes"])), stats["file_count"]),
            )
        self.status_var.set(status_extra + f"（共 {len(rows)} 项，选中后可让 AI 介绍）")

    def _on_double_click(self, event) -> None:
        if self.list_tree.identify_column(event.x) == "#1":
            return  # 行首方框列：双击只切换勾选，不触发下钻/打开
        item = self.list_tree.identify_row(event.y)
        if not item:
            return
        tags = self.list_tree.item(item, "tags")
        if tags and tags[0] == _DIR_TAG:
            # 子级百分比分母 = 该目录自身的递归聚合大小。
            children_total = self._child_total_from_inventory(item)
            self._enter_dir(item, children_total)
        else:
            _open_in_explorer(Path(item))

    # ── 手动删除（行首方框勾选 → 二次确认 → 回收站）─────────────────────
    def _on_list_click(self, event) -> None:
        """点击行首方框列切换删除标记；其余列保持 Treeview 原生选择行为。"""
        if self.list_tree.identify_region(event.x, event.y) != "cell":
            return
        if self.list_tree.identify_column(event.x) != "#1":
            return
        item = self.list_tree.identify_row(event.y)
        if item:
            self._toggle_mark(item)

    def _toggle_mark(self, item: str) -> None:
        if item in self._delete_marks:
            self._delete_marks.discard(item)
            self.list_tree.set(item, "check", _CHECK_OFF)
        else:
            self._delete_marks.add(item)
            self.list_tree.set(item, "check", _CHECK_ON)
        self._refresh_delete_state()

    def _toggle_all_marks(self) -> None:
        """点击"全选"表头：当前列表已全勾选则清空，否则全部勾上。"""
        children = self.list_tree.get_children()
        if not children:
            return
        if all(item in self._delete_marks for item in children):
            for item in children:
                self._delete_marks.discard(item)
                self.list_tree.set(item, "check", _CHECK_OFF)
        else:
            for item in children:
                self._delete_marks.add(item)
                self.list_tree.set(item, "check", _CHECK_ON)
        self._refresh_delete_state()

    def _refresh_delete_state(self) -> None:
        """删除按钮仅在"非忙且有勾选"时可用，并实时显示勾选数量。"""
        count = len(self._delete_marks)
        state = "normal" if count and not self._busy else "disabled"
        self.delete_button.configure(state=state, text=f"删除勾选（{count}）" if count else "删除勾选")

    def _delete_checked(self) -> None:
        if self.snapshot_id is None:
            messagebox.showinfo("尚未扫描", "请先完成一次扫描，再勾选删除目标。")
            return
        if not self._delete_marks:
            messagebox.showinfo("未勾选", "请先在列表行首的方框中勾选要删除的文件或目录。")
            return

        plan = plan_deletion(self.inventory, self.snapshot_id, self._delete_marks)
        if not plan.targets:
            detail = "\n".join(f"· {path}：{reason}" for path, reason in plan.refused[:10])
            messagebox.showwarning("没有可删除的目标", f"勾选的目标均不可删除：\n{detail}")
            return

        lines = [
            f"即将删除 {len(plan.targets)} 个文件，共 {format_size(plan.total_bytes)}"
            f"（来自 {len(plan.accepted_dir_marks)} 个勾选目录、{len(plan.accepted_file_marks)} 个勾选文件）。",
            "文件将移入系统回收站，可在回收站中还原。",
        ]
        if plan.refused:
            lines.append(f"另有 {len(plan.refused)} 个勾选目标被拒绝（受保护目录或快照中不存在）。")
        if len(plan.targets) > 5000:
            lines.append("勾选目标较多，删除可能耗时较长。")
        lines += ["", "示例："]
        lines += [f"· {row.path}" for row in plan.targets[:8]]
        if len(plan.targets) > 8:
            lines.append(f"…… 以及另外 {len(plan.targets) - 8} 项")
        if not messagebox.askyesno("确认删除", "\n".join(lines)):
            return

        self._set_busy(True)
        self.status_var.set(f"正在删除 {len(plan.targets)} 个文件（移入回收站）……")
        threading.Thread(
            target=self._delete_worker, args=(plan, self.snapshot_id), daemon=True
        ).start()

    def _delete_worker(self, plan: DeletionPlan, snapshot_id: int) -> None:
        """后台线程：回收站执行 → 按磁盘事实回删快照行 → 重算目录聚合。"""
        started = time.perf_counter()
        try:
            deleted, failed = recycle_paths([row.path for row in plan.targets])
            deleted_set = set(deleted)
            freed = sum(row.size_bytes for row in plan.targets if row.path in deleted_set)
            if deleted_set:
                self.inventory.remove_files(snapshot_id, deleted_set)
            # 目录勾选：整体已消失 → 连同目录行回删；仍存在（部分删除）→ 清掉空目录壳。
            for directory in plan.accepted_dir_marks:
                if not os.path.exists(directory):
                    self.inventory.remove_dir_subtree(snapshot_id, directory)
                else:
                    for removed_dir in prune_empty_dirs(directory):
                        self.inventory.remove_dir_subtree(snapshot_id, removed_dir)
            self.inventory.refresh_dir_aggregates(snapshot_id)
            elapsed = time.perf_counter() - started
            self.events.put(("delete_done", (deleted, failed, plan.refused, freed, elapsed)))
        except Exception as exc:
            self.events.put(("delete_error", f"{type(exc).__name__}: {exc}"))

    def _refresh_current_view(self) -> None:
        """删除后刷新当前目录（聚合已重算）；目录自身消失时回退到最近的现存祖先。"""
        if self.snapshot_id is None or not self.current_dir:
            return
        target = self.current_dir
        while len(target) > 3 and not Path(target).exists():
            target = str(Path(target.rstrip("\\")).parent)
        self.current_dir = target
        if target.endswith(":\\"):
            self._enter_dir(target, max(self.used_bytes, 1))
        else:
            self._enter_dir(target, self._child_total_from_inventory(target))

    def _child_total_from_inventory(self, directory: str) -> int:
        rows = self.inventory.child_dirs(self.snapshot_id or 0, str(Path(directory).parent))
        for row in rows:
            if row.path == directory:
                return max(row.total_size, 1)
        return 1

    def _go_up(self) -> None:
        if not self.current_dir:
            return
        parent = str(Path(self.current_dir.rstrip("\\")).parent)
        if len(parent) <= 3:
            parent = parent[:3]
        if Path(parent).exists():
            # 分母 = 父目录自身的递归总量；用全卷已用会让非根目录的子项百分比全部失真。
            self._enter_dir(parent, self._child_total_from_inventory(parent))

    def _go_root(self) -> None:
        drive = self.volume_var.get()
        if self.snapshot_id is not None:
            self.used_bytes, _count = self.inventory.volume_usage(self.snapshot_id)
            self._enter_dir(drive, max(self.used_bytes, 1))

    # ── AI 介绍（选中 → 这是什么/删除影响/处理建议）─────────────────────
    def _review_selected(self) -> None:
        if self.snapshot_id is None:
            messagebox.showinfo("尚未扫描", "请先完成一次扫描。")
            return
        advisor = build_advisor()
        blockers = [b for b in analysis_blockers(advisor.ai_available) if "管理员" not in b]
        if blockers:
            messagebox.showerror(
                "无法介绍",
                "AI 未配置：请点击\"AI 配置\"填写密钥与模型。\n" + "\n".join(blockers),
            )
            return

        selection = self.list_tree.selection()
        if not selection:
            messagebox.showinfo(
                "未选择", "请先在列表中选中（高亮）要介绍的目录或文件（Ctrl/Shift 可多选）。"
            )
            return

        # 介绍按"选中项自身"逐条生成：目录整体一条（含聚合统计），文件一条。
        entries: list[tuple[int, FileDescription, dict]] = []
        for item in selection:
            tags = self.list_tree.item(item, "tags")
            if tags and tags[0] == _DIR_TAG:
                entries.append(self._dir_describe_entry(item))
            elif item in self.loose_rows:
                entries.append(self._file_describe_entry(self.loose_rows[item]))
        if not entries:
            messagebox.showinfo("无可介绍内容", "所选目标没有可用的快照信息。")
            return
        entries.sort(key=lambda entry: entry[0], reverse=True)
        note = ""
        if len(entries) > _DESCRIBE_MAX_ITEMS:
            entries = entries[:_DESCRIBE_MAX_ITEMS]
            note = f"（选中较多，已按体积取前 {_DESCRIBE_MAX_ITEMS} 项）"
        metas = [entry[1] for entry in entries]
        payloads = [entry[2] for entry in entries]

        self._set_busy(True)
        self.status_var.set(f"正在让 AI 介绍 {len(metas)} 个目标{note}……")
        threading.Thread(
            target=self._describe_worker, args=(metas, payloads, advisor), daemon=True
        ).start()

    def _dir_describe_entry(self, item: str) -> tuple[int, FileDescription, dict]:
        """目录介绍条目：聚合统计取自快照库；路径按隐私档裁剪后发给 AI。"""
        agg = self._dir_agg(item)
        name = Path(item).name or item
        total = agg.total_size if agg else 0
        meta = FileDescription(
            path=item, name=name, kind="目录", size_text=format_size(total),
            what="", impact="", handle="需核对",
        )
        payload: dict[str, object] = {
            "kind": "目录",
            "name": name,
            "path": item,
            "total_size_bytes": total,
            "file_count": agg.file_count if agg else 0,
            "top_suffixes": agg.top_suffixes if agg else [],
        }
        return total, meta, self._anonymize_payload(payload)

    def _file_describe_entry(self, row: FileRow) -> tuple[int, FileDescription, dict]:
        meta = FileDescription(
            path=row.path, name=row.name, kind="文件", size_text=format_size(row.size_bytes),
            what="", impact="", handle="需核对",
        )
        payload: dict[str, object] = {
            "kind": "文件",
            "name": row.name,
            "path": row.path,
            "suffix": row.suffix,
            "size_bytes": row.size_bytes,
        }
        return row.size_bytes, meta, self._anonymize_payload(payload)

    def _anonymize_payload(self, payload: dict[str, object]) -> dict[str, object]:
        """介绍载荷只做路径匿名化（balanced）；strict 档用户走 CLI，不在 GUI 出现。"""
        payload["path"] = anonymize_path(str(payload["path"]))
        return payload

    def _dir_agg(self, directory: str) -> DirAgg | None:
        rows = self.inventory.child_dirs(self.snapshot_id or 0, str(Path(directory).parent))
        for row in rows:
            if row.path == directory:
                return row
        return None

    def _describe_worker(
        self, metas: list[FileDescription], payloads: list[dict], advisor
    ) -> None:
        """后台线程：批量请求 AI 介绍（纯展示任务，不写报告、不触判定管线）。"""
        started = time.perf_counter()
        try:
            results = advisor.describe_items(payloads)
            descriptions = [
                FileDescription(
                    path=meta.path,
                    name=meta.name,
                    kind=meta.kind,
                    size_text=meta.size_text,
                    what=item.what,
                    impact=item.impact,
                    handle=item.handle,
                )
                for meta, item in zip(metas, results, strict=True)
            ]
            elapsed = time.perf_counter() - started
            self.events.put(("review_done", (descriptions, advisor.stats, elapsed)))
        except Exception as exc:
            self.events.put(("review_error", f"{type(exc).__name__}: {exc}"))

    # ── AI 深度分析：自由综述（等价于"把磁盘占用截图发给 AI"）──────────────
    def _start_overview(self) -> None:
        if self.snapshot_id is None:
            messagebox.showinfo("尚未扫描", "请先完成一次扫描，再对当前目录做深度分析。")
            return
        advisor = build_advisor()
        blockers = [b for b in analysis_blockers(advisor.ai_available) if "管理员" not in b]
        if blockers:
            messagebox.showerror(
                "无法深度分析",
                "AI 未配置：请点击\"AI 配置\"填写密钥与模型。\n" + "\n".join(blockers),
            )
            return
        scope = self.current_dir or self.volume_var.get()
        self._set_busy(True)
        self.status_var.set(f"正在做 AI 深度分析：{scope}（聚合并发送快照事实）……")
        threading.Thread(
            target=self._overview_worker,
            args=(scope, advisor),
            daemon=True,
        ).start()

    def _overview_worker(self, scope: str, advisor) -> None:
        """后台线程：快照事实聚合 → 综述请求。全程只读快照库，不触碰文件系统。"""
        started = time.perf_counter()
        try:
            payload = build_overview_payload(
                self.inventory,
                self.snapshot_id or 0,
                scope,
                privacy_mode=advisor.settings.ai_privacy_mode,
            )
            narrative = advisor.summarize_overview(payload)
            if not narrative:
                self.events.put(("overview_error", "AI 未返回分析正文（接口错误、超时或输出为空）。"))
                return
            path = write_overview_markdown(narrative, default_report_path("analysis.md"))
            self.events.put(
                (
                    "overview_done",
                    (scope, narrative, path, time.perf_counter() - started, advisor.stats),
                )
            )
        except Exception as exc:
            self.events.put(("overview_error", f"{type(exc).__name__}: {exc}"))

    def _show_overview(self, scope: str, narrative: str, path: Path, elapsed: float, stats) -> None:
        """综述阅读窗口：长文本按标题分层显示，支持复制与打开落盘的 Markdown。"""
        window = Toplevel(self.root)
        window.title(f"{APP_NAME} · AI 深度分析")
        window.geometry(f"{round(920 * self._dpi_scale)}x{round(720 * self._dpi_scale)}")

        header = ttk.Frame(window, padding=(12, 10, 12, 6))
        header.pack(fill="x")
        ttk.Label(header, text=f"分析范围：{scope}", style="Header.TLabel").pack(side=LEFT)
        ttk.Label(
            header,
            text=f"耗时 {elapsed:.1f}s │ AI 请求 {stats.api_calls} 次 │ 缓存命中 {stats.cache_hits} 项",
            style="Muted.TLabel",
        ).pack(side=RIGHT)

        body = ttk.Frame(window, padding=(12, 0, 12, 0))
        body.pack(fill=BOTH, expand=True)
        text_widget = Text(body, wrap="word", relief="flat", padx=10, pady=8)
        scroll = ttk.Scrollbar(body, orient=VERTICAL, command=text_widget.yview)
        text_widget.configure(yscrollcommand=scroll.set)
        text_widget.pack(side=LEFT, fill=BOTH, expand=True)
        scroll.pack(side=RIGHT, fill="y")
        body_font = (FONT_FAMILY, 10)
        text_widget.configure(font=body_font, background=COLOR_SURFACE, foreground=COLOR_INK)
        text_widget.tag_configure("heading", font=(FONT_FAMILY, 13, "bold"), spacing1=14, spacing3=6)
        text_widget.tag_configure("body", font=body_font, spacing1=2, spacing3=4)
        for line in narrative.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                text_widget.insert(END, stripped.lstrip("#").strip() + "\n", "heading")
            else:
                text_widget.insert(END, line + "\n", "body")
        text_widget.configure(state="disabled")

        footer = ttk.Frame(window, padding=(12, 8, 12, 12))
        footer.pack(fill="x")

        def copy_all() -> None:
            self.root.clipboard_clear()
            self.root.clipboard_append(narrative)
            self.status_var.set("AI 深度分析正文已复制到剪贴板。")

        ttk.Button(footer, text="复制全文", command=copy_all).pack(side=LEFT)
        ttk.Button(footer, text="打开 Markdown 文件", command=lambda: _open_path(path)).pack(
            side=LEFT, padx=(6, 0)
        )
        ttk.Label(
            footer,
            text=f"已保存：{path}　|　分析仅供参考，自动流程不会移动或删除任何文件。",
            style="Muted.TLabel",
        ).pack(side=RIGHT)

    # ── 通用 ─────────────────────────────────────────────────────────────
    def _load_logo(self, size: int) -> PhotoImage | None:
        """加载 assets 下与 DPI 最匹配的 logo（size 为逻辑像素）；资源缺失时静默降级。"""
        assets_dir = Path(__file__).resolve().parents[1] / "assets"
        physical = round(size * self._dpi_scale)
        candidates = sorted(
            assets_dir.glob("logo_*.png"),
            key=lambda path: abs(int(path.stem.rsplit("_", 1)[1]) - physical),
        )
        for asset in candidates:
            try:
                image = PhotoImage(file=str(asset))
            except TclError:
                continue
            self._logo_images.append(image)  # 防 GC：PhotoImage 必须持有引用
            return image
        return None

    def _fill_review_rows(self, descriptions: list[FileDescription]) -> None:
        self.reviewed_descriptions = list(descriptions)
        self._apply_review_filter()
        # 列表内已介绍的行按处理建议着色（可见即所得）。
        by_path = {desc.path: desc for desc in descriptions}
        for item in self.list_tree.get_children():
            desc = by_path.get(item)
            if desc is not None:
                self.list_tree.item(item, tags=(f"desc:{desc.handle}",))

    def _apply_review_filter(self) -> None:
        """按"只看可清理"开关重渲染介绍列表：数据完整保留，只控制显示范围。"""
        self.review_tree.delete(*self.review_tree.get_children())
        only_cleanup = self.only_cleanup_var.get()
        shown = 0
        hidden = 0
        for desc in self.reviewed_descriptions:
            if only_cleanup and desc.handle != "可清理":
                hidden += 1
                continue
            tags = (desc.handle, _STRIPE_TAG) if shown % 2 else (desc.handle,)
            self.review_tree.insert(
                "",
                END,
                tags=tags,
                values=(desc.size_text, desc.handle, desc.what, desc.impact, desc.path),
            )
            shown += 1

        total = len(self.reviewed_descriptions)
        if not total:
            self.review_count_label.configure(text="")
        elif shown < total:
            self.review_count_label.configure(text=f"显示 {shown} / 共 {total} 条（已折叠 {hidden} 条）")
        else:
            self.review_count_label.configure(text=f"共 {total} 条")

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.scan_button.configure(state=state)
        self.admin_button.configure(state=state)
        self.ai_test_button.configure(state=state)
        self.ai_config_button.configure(state=state)
        self.review_button.configure(state=state)
        self.overview_button.configure(state=state)
        self._refresh_delete_state()

    def _poll_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "progress_text":
                    self.status_var.set(str(payload))
                elif event == "progress_pct":
                    self.progress.configure(value=int(payload) if isinstance(payload, int) else 0)
                elif event == "scan_done":
                    snapshot_id, file_count, drive, elapsed = payload  # type: ignore[misc]
                    self.snapshot_id = snapshot_id
                    self._delete_marks.clear()  # 新快照的路径集合与旧勾选不再对应
                    self._refresh_delete_state()
                    self.used_bytes, _count = self.inventory.volume_usage(snapshot_id)
                    self.progress.configure(value=100)
                    self._set_busy(False)
                    self.pause_button.configure(state="disabled", text="暂停")
                    self.cancel_button.configure(state="disabled")
                    total, free, _used = volume_capacity(drive)
                    if total:
                        pct = self.used_bytes * 100 // total
                        self.space_var.set(
                            f"总空间 {format_size(total)} │ 文件占用 {format_size(self.used_bytes)} ({pct}%) │ 可用 {format_size(free)}"
                        )
                    self.status_var.set(
                        f"扫描完成：{file_count} 个文件，耗时 {elapsed:.1f} 秒。"
                        "双击目录进入；选中后让 AI 介绍，行首方框勾选后可删除（移入回收站）。"
                    )
                    self._go_root()
                elif event == "scan_cancelled":
                    self.progress.configure(value=0)
                    self._set_busy(False)
                    self.pause_button.configure(state="disabled", text="暂停")
                    self.cancel_button.configure(state="disabled")
                    self.status_var.set("扫描已取消（半成品已丢弃），可重新开始。")
                elif event == "scan_error":
                    self.progress.configure(value=0)
                    self._set_busy(False)
                    self.pause_button.configure(state="disabled", text="暂停")
                    self.cancel_button.configure(state="disabled")
                    self.status_var.set("扫描失败。")
                    messagebox.showerror("扫描失败", str(payload))
                elif event == "review_done":
                    descriptions, stats, elapsed = payload  # type: ignore[misc]
                    self._fill_review_rows(descriptions)
                    self._set_busy(False)
                    self.status_var.set(
                        f"AI 介绍完成：{len(descriptions)} 项，耗时 {elapsed:.1f} 秒；"
                        f"AI 请求 {stats.api_calls} 次，缓存命中 {stats.cache_hits} 项。"
                    )
                elif event == "review_error":
                    self._set_busy(False)
                    self.status_var.set("AI 介绍失败。")
                    messagebox.showerror("AI 介绍失败", str(payload))
                elif event == "delete_done":
                    deleted, failed, refused, freed, elapsed = payload  # type: ignore[misc]
                    self._delete_marks.difference_update(deleted)
                    self.used_bytes, _count = self.inventory.volume_usage(self.snapshot_id or 0)
                    total, free, _used = volume_capacity(self.volume_var.get())
                    if total:
                        pct = self.used_bytes * 100 // total
                        self.space_var.set(
                            f"总空间 {format_size(total)} │ 文件占用 {format_size(self.used_bytes)} ({pct}%) │ 可用 {format_size(free)}"
                        )
                    self._refresh_current_view()
                    self._set_busy(False)
                    message = (
                        f"已删除 {len(deleted)} 个文件（释放 {format_size(freed)}），移入回收站，"
                        f"耗时 {elapsed:.1f} 秒。"
                    )
                    if failed:
                        message += f" {len(failed)} 个未能删除（被占用、权限不足或已被移动）。"
                    if refused:
                        message += f" {len(refused)} 个勾选目标被拒绝（见确认框说明）。"
                    self.status_var.set(message)
                    if failed:
                        preview = "\n".join(f"· {path}" for path in failed[:8])
                        more = f"\n…… 以及另外 {len(failed) - 8} 项" if len(failed) > 8 else ""
                        messagebox.showwarning(
                            "部分目标未删除", f"以下目标未能删除：\n{preview}{more}"
                        )
                elif event == "delete_error":
                    self._set_busy(False)
                    self.status_var.set("删除失败。")
                    messagebox.showerror("删除失败", str(payload))
                elif event == "overview_done":
                    scope, narrative, path, elapsed, stats = payload  # type: ignore[misc]
                    self._set_busy(False)
                    self.status_var.set(
                        f"AI 深度分析完成：{scope}，耗时 {elapsed:.1f} 秒；已保存 {path}"
                    )
                    self._show_overview(scope, narrative, path, elapsed, stats)
                elif event == "overview_error":
                    self._set_busy(False)
                    self.status_var.set("AI 深度分析未完成。")
                    messagebox.showwarning("深度分析未完成", str(payload))
                elif event == "ai_test_done":
                    api_style, model, advice, stats = payload  # type: ignore[misc]
                    self._set_busy(False)
                    self.status_var.set(f"AI 连接成功：{api_style} / {model}；请求 {stats.api_calls} 次。")
                    messagebox.showinfo(
                        "AI 连接成功",
                        f"协议：{api_style}\n模型：{model}\n\n"
                        "连接测试说明：程序向 AI 发送了一个示例文件"
                        "（Temp 目录的 connection_test.tmp，128 B），\n"
                        "AI 返回了合法的结构化判定——说明密钥、接口协议与"
                        "结构化输出均正常，可以开始评审。\n\n"
                        f"示例文件的判定：{advice.purpose} / {advice.advice_level}\n"
                        f"理由：{advice.reason}",
                    )
                elif event == "ai_test_error":
                    self._set_busy(False)
                    self.status_var.set("AI 连接测试失败。")
                    messagebox.showerror("AI 连接失败", str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _open_report(self) -> None:
        if self.last_html is None or not self.last_html.exists():
            messagebox.showinfo("暂无报告", "请先完成一次评审（评审结果同时生成 HTML 报告）。")
            return
        _open_path(self.last_html)


def main() -> None:
    _enable_windows_dpi_awareness()
    root = Tk()
    _setup_style(root)
    DiskAssistantGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
