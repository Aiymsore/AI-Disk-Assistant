"""图形界面层（Tkinter）：WizTree 式磁盘浏览器 + 勾选送 AI 评审。

布局：顶栏（选卷/扫描/管理员重启 + 权限）→ 空间头（总/已用/可用/耗时）→
主区（左侧当前目录列表：目录在前、大小降序、双击下钻；右侧扩展名分类面板）→
底部（勾选送 AI 评审 + 变色候选列表与判断依据）。不做全量树与 treemap：
浏览式下钻一次只查一层（SQLite 毫秒级），评审目标由用户勾选决定。
扫描支持暂停/继续/取消与百分比进度（ScanControl）。
本工具只产出建议与报告，不做任何删除动作。
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
    TclError,
    Tk,
    Toplevel,
    messagebox,
    ttk,
)

from . import __version__
from .admin import analysis_blockers, is_user_admin, relaunch_as_admin
from .ai_advisor import build_advisor
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
from .inventory import FileRow, Inventory
from .metadata import format_mtime, format_pct, format_size
from .mft_scanner import MftError, ScanCancelled, ScanControl, snapshot_volume, volume_capacity
from .models import Candidate, FileMetadata, ScanStats
from .report import write_all_reports
from .scanner import ScanPolicy, _signals_from_facts, judge_metadata_records

def _review_stats(scored: int, kept: int, unit_count: int) -> "ScanStats":
    stats = ScanStats(root="review")
    stats.visited_files = scored
    stats.matched_candidates = scored
    stats.retained_candidates = kept
    stats.unit_count = unit_count
    stats.units_judged = unit_count  # 无数量上限：全部单元实判。
    return stats


# 建议等级 → 行前景色（"变色"映射）：危险程度越高越红，保守结论越绿。
LEVEL_COLORS = {
    "建议删除": "#c0392b",
    "谨慎删除": "#d68910",
    "人工确认": "#2874a6",
    "不建议删除": "#1e8449",
}

_DIR_TAG = "dir"
_FILE_TAG = "file"
_STRIPE_TAG = "stripe"

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
        self.reviewed_candidates: list[Candidate] = []

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

        # 左：当前目录列表（目录在前、大小降序，双击进入；多选后送评审）
        list_frame = ttk.Frame(main, style="Card.TFrame", padding=1)
        list_columns = ("name", "pct", "size", "items", "mtime")
        self.list_tree = ttk.Treeview(
            list_frame, columns=list_columns, show="headings", selectmode="extended"
        )
        list_headings = {"name": "名称", "pct": "父级百分比", "size": "大小", "items": "项数", "mtime": "修改时间"}
        list_widths = {"name": 320, "pct": 90, "size": 100, "items": 80, "mtime": 150}
        for column in list_columns:
            self.list_tree.heading(column, text=list_headings[column])
            self.list_tree.column(column, width=px(list_widths[column]), minwidth=px(60))
        self.list_tree.tag_configure(_DIR_TAG, background=COLOR_DIR_ROW)
        self.list_tree.tag_configure(_STRIPE_TAG, background=COLOR_STRIPE)
        for level, color in LEVEL_COLORS.items():
            self.list_tree.tag_configure(f"reviewed:{level}", foreground=color)
        list_scroll = ttk.Scrollbar(list_frame, orient=VERTICAL, command=self.list_tree.yview)
        self.list_tree.configure(yscrollcommand=list_scroll.set)
        self.list_tree.pack(side=LEFT, fill=BOTH, expand=True)
        list_scroll.pack(side=RIGHT, fill="y")
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

        # 底部：勾选送 AI 评审 + 变色候选列表
        review_bar = ttk.Frame(self.root, padding=(px(14), px(6), px(14), 0))
        review_bar.pack(fill="x")
        ttk.Label(
            review_bar,
            text="在列表中勾选目录/文件后送 AI 评审（评审过的文件获得删除建议与依据）：",
        ).pack(side=LEFT)
        self.review_button = ttk.Button(
            review_bar, text="评审所选 → AI", style="Primary.TButton", command=self._review_selected
        )
        self.review_button.pack(side=RIGHT)

        review_frame = ttk.Frame(self.root, style="Card.TFrame", padding=1)
        review_frame.pack(fill=BOTH, expand=True, padx=px(14), pady=(px(4), 0))
        review_columns = ("size", "level", "purpose", "source", "reason", "evidence", "path")
        self.review_tree = ttk.Treeview(
            review_frame, columns=review_columns, show="headings", height=8
        )
        review_headings = {
            "size": "大小",
            "level": "建议等级",
            "purpose": "用途",
            "source": "判断来源",
            "reason": "理由",
            "evidence": "判断依据",
            "path": "路径",
        }
        review_widths = {
            "size": 90,
            "level": 90,
            "purpose": 120,
            "source": 105,
            "reason": 220,
            "evidence": 220,
            "path": 330,
        }
        for column in review_columns:
            self.review_tree.heading(column, text=review_headings[column])
            self.review_tree.column(column, width=px(review_widths[column]), minwidth=px(60))
        for level, color in LEVEL_COLORS.items():
            self.review_tree.tag_configure(level, foreground=color)
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
        self.root.after(300, self.root.destroy)

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
            self.list_tree.insert(
                "", END, iid=path, tags=tags, values=(name, pct, size, items, mtime, path)
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
        self.status_var.set(status_extra + f"（共 {len(rows)} 项，勾选后可送 AI 评审）")

    def _on_double_click(self, event) -> None:
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

    # ── AI 评审（勾选 → 送评审）─────────────────────────────────────────
    def _review_selected(self) -> None:
        if self.snapshot_id is None:
            messagebox.showinfo("尚未扫描", "请先完成一次扫描。")
            return
        advisor = build_advisor()
        blockers = [b for b in analysis_blockers(advisor.ai_available) if "管理员" not in b]
        if blockers:
            messagebox.showerror(
                "无法评审",
                "AI 未配置：请点击\"AI 配置\"填写密钥与模型。\n" + "\n".join(blockers),
            )
            return

        selection = self.list_tree.selection()
        if not selection:
            messagebox.showinfo("未选择", "请先在列表中勾选要评审的目录或文件（Ctrl/Shift 可多选）。")
            return

        rows: list[FileRow] = []
        for item in selection:
            tags = self.list_tree.item(item, "tags")
            if tags and tags[0] == _DIR_TAG:
                rows += self.inventory.files_under(self.snapshot_id, item)
            elif item in self.loose_rows:
                rows.append(self.loose_rows[item])
        if not rows:
            messagebox.showinfo("无可评审内容", "所选目标下没有可评分的文件。")
            return

        self._set_busy(True)
        self.review_button.configure(state="disabled")
        self.status_var.set(f"正在评审 {len(rows)} 个文件（勾选目标子树）……")
        area_path = Path(self.current_dir or self.volume_var.get())
        threading.Thread(
            target=self._review_worker,
            args=(rows, area_path, advisor),
            daemon=True,
        ).start()

    def _review_worker(self, rows: list[FileRow], area_path: Path, advisor) -> None:
        started = time.perf_counter()
        stats_holder: dict = {}
        try:
            policy = ScanPolicy()
            records: list[tuple[FileMetadata, str, float]] = []
            for row in rows:
                signal = _signals_from_facts(row.path, row.suffix, row.size_bytes, area_path, policy)
                if signal is None:
                    continue
                reason, score, _size = signal
                metadata = FileMetadata(
                    path=row.path,
                    name=row.name,
                    suffix=row.suffix,
                    parent_folder=str(Path(row.path).parent),
                    size_bytes=row.size_bytes,
                    size_text=format_size(row.size_bytes),
                    modified_time="—",
                    accessed_time="—",
                    modified_time_ns=row.mtime_ns,
                    accessed_time_ns=row.mtime_ns,
                )
                records.append((metadata, reason, score))
            if not records:
                self.events.put(("review_done", ([], advisor.stats)))
                return
            candidates, unit_count = judge_metadata_records(records, advisor, policy)
            stats = advisor.stats
            stats_holder["html"] = write_all_reports(
                candidates,
                _review_stats(len(records), len(candidates), unit_count),
                stats.to_dict(),
            ).html
            elapsed = time.perf_counter() - started
            self.events.put(("review_done", (candidates, stats, stats_holder.get("html"), elapsed)))
        except Exception as exc:
            self.events.put(("review_error", str(exc)))

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

    def _fill_review_rows(self, candidates: list[Candidate]) -> None:
        self.reviewed_candidates = list(candidates)
        for index, candidate in enumerate(candidates):
            advice = candidate.advice
            tags = (advice.advice_level, _STRIPE_TAG) if index % 2 else (advice.advice_level,)
            self.review_tree.insert(
                "",
                END,
                tags=tags,
                values=(
                    candidate.metadata.size_text,
                    advice.advice_level,
                    advice.purpose,
                    advice.source,
                    advice.reason,
                    "；".join(advice.evidence),
                    candidate.metadata.path,
                ),
            )
        # 列表内已评审的文件行同步着色（可见即所得）。
        by_path = {candidate.metadata.path: candidate for candidate in candidates}
        for item in self.list_tree.get_children():
            candidate = by_path.get(item)
            if candidate is not None:
                self.list_tree.item(item, tags=(f"reviewed:{candidate.advice.advice_level}",))

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.scan_button.configure(state=state)
        self.admin_button.configure(state=state)
        self.ai_test_button.configure(state=state)
        self.ai_config_button.configure(state=state)
        self.review_button.configure(state=state)

    def _start_ai_test(self) -> None:
        self._set_busy(True)
        self.status_var.set("正在测试 AI 接口和结构化返回……")
        threading.Thread(target=self._ai_test_worker, daemon=True).start()

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
                        f"扫描完成：{file_count} 个文件，耗时 {elapsed:.1f} 秒。双击目录进入，勾选后送 AI 评审。"
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
                    candidates, stats, html_path, elapsed = payload  # type: ignore[misc]
                    self.last_html = Path(html_path)
                    self.review_tree.delete(*self.review_tree.get_children())
                    self._fill_review_rows(candidates)
                    self._set_busy(False)
                    self.status_var.set(
                        f"评审完成：{len(candidates)} 个候选，耗时 {elapsed:.1f} 秒；"
                        f"AI 请求 {stats.api_calls} 次，缓存命中 {stats.cache_hits} 项。报告：{html_path}"
                    )
                elif event == "review_error":
                    self._set_busy(False)
                    self.status_var.set("评审失败。")
                    messagebox.showerror("评审失败", str(payload))
                elif event == "ai_test_done":
                    api_style, model, advice, stats = payload  # type: ignore[misc]
                    self._set_busy(False)
                    self.status_var.set(f"AI 连接成功：{api_style} / {model}；请求 {stats.api_calls} 次。")
                    messagebox.showinfo(
                        "AI 连接成功",
                        f"协议：{api_style}\n模型：{model}\n"
                        f"结构化结果：{advice.purpose} / {advice.advice_level}\n"
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
