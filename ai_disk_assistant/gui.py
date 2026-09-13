from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from tkinter import (
    BOTH,
    END,
    LEFT,
    RIGHT,
    VERTICAL,
    BooleanVar,
    StringVar,
    Tk,
    Toplevel,
    filedialog,
    messagebox,
    simpledialog,
    ttk,
)

from .ai_advisor import HybridAdvisor
from .cleaner import CleanerUnavailable, move_to_trash, select_auto_candidates
from .config import (
    Settings,
    default_env_path,
    normalize_api_style,
    read_dotenv,
    update_dotenv,
)
from .metadata import format_size
from .models import Candidate
from .report import build_summary, default_report_path, write_csv, write_html_report, write_summary_json
from .scanner import ScanPolicy, scan_with_stats


def _open_path(path: Path) -> None:
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


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
        self.privacy_var = StringVar(value=file_values.get("AI_PRIVACY_MODE", settings.ai_privacy_mode))
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

        ttk.Label(frame, text="默认隐私模式").grid(row=protocol_row, column=2, sticky="e", pady=5)
        ttk.Combobox(
            frame,
            textvariable=self.privacy_var,
            values=("strict", "balanced", "full"),
            state="readonly",
            width=14,
        ).grid(row=protocol_row, column=3, sticky="w", padx=(10, 0), pady=5)

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
            path.write_text("# AI Disk Assistant runtime configuration\n", encoding="utf-8")
        _open_path(path)

    def _save(self, test_after_save: bool) -> None:
        key = self.key_var.get().strip()
        base_url = self.base_url_var.get().strip().rstrip("/")
        model = self.model_var.get().strip()
        cache_path = self.cache_path_var.get().strip() or ".cache/ai_advice.sqlite3"

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
                "AI_PRIVACY_MODE": self.privacy_var.get(),
                "AI_USER_AGENT": "AI-Disk-Assistant/1.3",
            }
        )
        privacy = self.privacy_var.get()
        self.window.destroy()
        messagebox.showinfo("配置已保存", f"AI 配置已保存到：\n{path}", parent=self.parent)
        self.on_saved(privacy, test_after_save)


class DiskAssistantGUI:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("AI Disk Assistant v1.3")
        self.root.geometry("1180x740")
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.last_html: Path | None = None
        self.current_candidates: list[Candidate] = []
        self.row_candidates: dict[str, Candidate] = {}
        self.last_scan_stats = None
        self.last_ai_stats: dict[str, object] = {}

        self.path_var = StringVar(value=str(Path("demo/sample_disk").resolve()))
        self.status_var = StringVar(value="请选择目录并开始扫描。")
        self.selection_var = StringVar(value="未选择文件。")
        self.use_ai_var = BooleanVar(value=True)
        self.privacy_var = StringVar(value="balanced")
        self.old_days_var = StringVar(value="180")
        self.ai_limit_var = StringVar(value="80")

        self._build()
        self.root.after(100, self._poll_events)

    def _build(self) -> None:
        controls = ttk.Frame(self.root, padding=12)
        controls.pack(fill="x")
        ttk.Label(controls, text="扫描目录").grid(row=0, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self.path_var, width=72).grid(row=0, column=1, padx=8, sticky="ew")
        ttk.Button(controls, text="选择目录", command=self._choose_directory).grid(row=0, column=2, padx=4)
        self.scan_button = ttk.Button(controls, text="开始扫描", command=self._start_scan)
        self.scan_button.grid(row=0, column=3, padx=4)
        controls.columnconfigure(1, weight=1)

        options = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        options.pack(fill="x")
        ttk.Checkbutton(options, text="启用 AI 建议", variable=self.use_ai_var).pack(side=LEFT)
        ttk.Label(options, text="隐私模式").pack(side=LEFT, padx=(18, 4))
        ttk.Combobox(
            options,
            textvariable=self.privacy_var,
            values=("strict", "balanced", "full"),
            state="readonly",
            width=10,
        ).pack(side=LEFT)
        ttk.Label(options, text="旧文件天数").pack(side=LEFT, padx=(18, 4))
        ttk.Entry(options, textvariable=self.old_days_var, width=7).pack(side=LEFT)
        ttk.Label(options, text="AI 分析上限").pack(side=LEFT, padx=(18, 4))
        ttk.Entry(options, textvariable=self.ai_limit_var, width=7).pack(side=LEFT)
        ttk.Button(options, text="打开最新 HTML 报告", command=self._open_report).pack(side=RIGHT)
        self.ai_test_button = ttk.Button(options, text="测试 AI 连接", command=self._start_ai_test)
        self.ai_test_button.pack(side=RIGHT, padx=(0, 8))
        self.ai_config_button = ttk.Button(options, text="AI 配置", command=self._open_ai_config)
        self.ai_config_button.pack(side=RIGHT, padx=(0, 8))

        actions = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        actions.pack(fill="x")
        ttk.Label(actions, textvariable=self.selection_var).pack(side=LEFT)
        self.trash_button = ttk.Button(
            actions,
            text="将选中项移入回收站",
            command=self._start_trash_selected,
            state="disabled",
        )
        self.trash_button.pack(side=RIGHT)
        self.clear_selection_button = ttk.Button(
            actions,
            text="清除选择",
            command=self._clear_selection,
            state="disabled",
        )
        self.clear_selection_button.pack(side=RIGHT, padx=(0, 8))
        self.select_recommended_button = ttk.Button(
            actions,
            text="选择全部‘建议删除’",
            command=self._select_recommended,
            state="disabled",
        )
        self.select_recommended_button.pack(side=RIGHT, padx=(0, 8))

        table_frame = ttk.Frame(self.root)
        table_frame.pack(fill=BOTH, expand=True, padx=12, pady=(0, 8))
        columns = ("size", "level", "purpose", "source", "reason", "path")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        headings = {
            "size": "大小",
            "level": "建议等级",
            "purpose": "用途",
            "source": "判断来源",
            "reason": "理由",
            "path": "路径",
        }
        widths = {"size": 90, "level": 90, "purpose": 125, "source": 110, "reason": 300, "path": 430}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=70)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._update_selection_state())
        scrollbar = ttk.Scrollbar(table_frame, orient=VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill="y")

        bottom = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        bottom.pack(fill="x")
        self.progress = ttk.Progressbar(bottom, mode="indeterminate")
        self.progress.pack(fill="x")
        ttk.Label(bottom, textvariable=self.status_var).pack(anchor="w", pady=(6, 0))

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.scan_button.configure(state=state)
        self.ai_test_button.configure(state=state)
        self.ai_config_button.configure(state=state)
        if busy:
            self.trash_button.configure(state="disabled")
            self.select_recommended_button.configure(state="disabled")
            self.clear_selection_button.configure(state="disabled")
        else:
            self._update_selection_state()
            self.select_recommended_button.configure(
                state="normal" if self.current_candidates else "disabled"
            )

    def _choose_directory(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.path_var.get() or str(Path.home()))
        if selected:
            self.path_var.set(selected)

    def _open_ai_config(self) -> None:
        AIConfigDialog(self.root, self._after_ai_config_saved)

    def _after_ai_config_saved(self, privacy: str, test_after_save: bool) -> None:
        self.privacy_var.set(privacy)
        if test_after_save:
            self._start_ai_test()

    def _start_scan(self) -> None:
        root_path = Path(self.path_var.get()).expanduser()
        if not root_path.is_dir():
            messagebox.showerror("目录无效", "请选择一个存在的目录。")
            return
        try:
            old_days = max(int(self.old_days_var.get()), 1)
            ai_limit = max(int(self.ai_limit_var.get()), 0)
        except ValueError:
            messagebox.showerror("参数错误", "旧文件天数和 AI 上限必须是整数。")
            return

        for item in self.tree.get_children():
            self.tree.delete(item)
        self.row_candidates.clear()
        self.current_candidates.clear()
        self.selection_var.set("未选择文件。")
        self._set_busy(True)
        self.progress.start(12)
        self.status_var.set("正在扫描并生成建议……")
        use_ai = bool(self.use_ai_var.get())
        privacy = self.privacy_var.get()
        threading.Thread(
            target=self._scan_worker,
            args=(root_path, old_days, ai_limit, use_ai, privacy),
            daemon=True,
        ).start()

    def _start_ai_test(self) -> None:
        self._set_busy(True)
        self.status_var.set("正在测试 AI 接口和结构化返回……")
        privacy = self.privacy_var.get()
        threading.Thread(target=self._ai_test_worker, args=(privacy,), daemon=True).start()

    def _ai_test_worker(self, privacy: str) -> None:
        try:
            settings = replace(Settings.from_env(), ai_privacy_mode=privacy)
            advisor = HybridAdvisor(settings, enable_ai=True)
            advice = advisor.probe()
            self.events.put(("ai_test_done", (settings.ai_api_style, settings.ai_model, advice, advisor.stats)))
        except Exception as exc:
            self.events.put(("ai_test_error", exc))

    def _scan_worker(
        self, root_path: Path, old_days: int, ai_limit: int, use_ai: bool, privacy: str
    ) -> None:
        try:
            settings = replace(Settings.from_env(), ai_privacy_mode=privacy)
            advisor = HybridAdvisor(settings, enable_ai=use_ai)
            result = scan_with_stats(
                root_path,
                advisor,
                ScanPolicy(old_days=old_days, ai_limit=ai_limit, max_candidates=1000),
                progress=lambda count: self.events.put(("progress", count)),
            )
            csv_path = write_csv(result.candidates, default_report_path("csv"))
            summary = build_summary(result.candidates, result.stats, advisor.stats.to_dict())
            summary_path = write_summary_json(summary, default_report_path("summary.json"))
            html_path = write_html_report(summary, default_report_path("html"))
            self.events.put(("done", (result, advisor.stats, csv_path, summary_path, html_path)))
        except Exception as exc:
            self.events.put(("error", exc))

    def _select_recommended(self) -> None:
        recommended = set(id(candidate) for candidate in select_auto_candidates(self.row_candidates.values()))
        item_ids = [
            item_id
            for item_id, candidate in self.row_candidates.items()
            if id(candidate) in recommended
        ]
        self.tree.selection_set(item_ids)
        if item_ids:
            self.tree.see(item_ids[0])
        self._update_selection_state()

    def _clear_selection(self) -> None:
        self.tree.selection_remove(self.tree.selection())
        self._update_selection_state()

    def _eligible_selection(self) -> tuple[list[Candidate], int]:
        selected = [self.row_candidates[item_id] for item_id in self.tree.selection() if item_id in self.row_candidates]
        eligible = select_auto_candidates(selected)
        return eligible, len(selected) - len(eligible)

    def _update_selection_state(self) -> None:
        selected_ids = self.tree.selection()
        eligible, ineligible_count = self._eligible_selection()
        total_size = sum(item.metadata.size_bytes for item in eligible)
        if selected_ids:
            extra = f"；{ineligible_count} 项因非‘建议删除’不会处理" if ineligible_count else ""
            self.selection_var.set(
                f"已选择 {len(selected_ids)} 项，可移入回收站 {len(eligible)} 项（{format_size(total_size)}）{extra}。"
            )
        else:
            self.selection_var.set("未选择文件。")
        self.trash_button.configure(state="normal" if eligible else "disabled")
        self.clear_selection_button.configure(state="normal" if selected_ids else "disabled")

    def _start_trash_selected(self) -> None:
        eligible, ineligible_count = self._eligible_selection()
        if not eligible:
            messagebox.showinfo("没有可处理项", "仅‘建议删除’且明确允许的候选可以移入回收站。")
            return

        total_size = sum(item.metadata.size_bytes for item in eligible)
        warning = (
            f"将把 {len(eligible)} 个文件（{format_size(total_size)}）移入系统回收站。\n\n"
            "程序会在操作前重新检查文件大小、修改时间和文件标识；发生变化或受保护的项目会自动跳过。"
        )
        if ineligible_count:
            warning += f"\n\n另外 {ineligible_count} 个非‘建议删除’项目不会处理。"
        if not messagebox.askyesno("确认移入回收站", warning):
            return

        confirmation = simpledialog.askstring(
            "二次确认",
            "此操作不是永久删除，但会修改文件位置。\n请输入 TRASH 继续：",
            parent=self.root,
        )
        if confirmation != "TRASH":
            messagebox.showinfo("已取消", "确认口令不正确，未移动任何文件。")
            return

        self._set_busy(True)
        self.progress.start(12)
        self.status_var.set(f"正在将 {len(eligible)} 个文件移入回收站……")
        threading.Thread(target=self._trash_worker, args=(eligible,), daemon=True).start()

    def _trash_worker(self, candidates: list[Candidate]) -> None:
        try:
            moved, failed = move_to_trash(candidates)
            self.events.put(("trash_done", (moved, failed)))
        except Exception as exc:
            self.events.put(("trash_error", exc))

    def _refresh_reports_after_trash(self) -> tuple[Path, Path]:
        if self.last_scan_stats is None:
            raise RuntimeError("缺少扫描统计信息，请重新扫描。")
        self.last_scan_stats.retained_candidates = len(self.current_candidates)
        csv_path = write_csv(self.current_candidates, default_report_path("csv"))
        summary = build_summary(self.current_candidates, self.last_scan_stats, self.last_ai_stats)
        write_summary_json(summary, default_report_path("summary.json"))
        html_path = write_html_report(summary, default_report_path("html"))
        self.last_html = Path(html_path)
        return Path(csv_path), Path(html_path)

    def _poll_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "progress":
                    self.status_var.set(f"已检查 {payload} 个文件……")
                elif event == "done":
                    result, stats, csv_path, _summary_path, html_path = payload  # type: ignore[misc]
                    self.last_html = Path(html_path)
                    self.current_candidates = list(result.candidates)
                    self.last_scan_stats = result.stats
                    self.last_ai_stats = stats.to_dict()
                    self.row_candidates.clear()
                    for candidate in result.candidates:
                        item_id = self.tree.insert(
                            "",
                            END,
                            values=(
                                candidate.metadata.size_text,
                                candidate.advice.advice_level,
                                candidate.advice.purpose,
                                candidate.advice.source,
                                candidate.advice.reason,
                                candidate.metadata.path,
                            ),
                        )
                        self.row_candidates[item_id] = candidate
                    self.progress.stop()
                    self._set_busy(False)
                    self.status_var.set(
                        f"完成：检查 {result.stats.visited_files} 个文件，保留 {len(result.candidates)} 个候选；"
                        f"AI[{stats.api_style}] 请求 {stats.api_calls} 次，缓存命中 {stats.cache_hits} 项。报告：{csv_path}"
                    )
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
                elif event == "trash_done":
                    moved, failed = payload  # type: ignore[misc]
                    moved_keys = {str(path) for path in moved}
                    for item_id, candidate in list(self.row_candidates.items()):
                        if str(candidate.path) in moved_keys:
                            self.tree.delete(item_id)
                            del self.row_candidates[item_id]
                    self.current_candidates = [
                        candidate for candidate in self.current_candidates if str(candidate.path) not in moved_keys
                    ]
                    try:
                        csv_path, _html_path = self._refresh_reports_after_trash()
                        report_text = f"\n报告已刷新：{csv_path}"
                    except Exception as exc:
                        report_text = f"\n报告刷新失败：{exc}"
                    self.progress.stop()
                    self._set_busy(False)
                    self.status_var.set(f"已移入回收站 {len(moved)} 项；失败或跳过 {len(failed)} 项。")
                    detail = ""
                    if failed:
                        preview = "\n".join(f"- {path}: {reason}" for path, reason in failed[:8])
                        detail = f"\n\n失败/跳过：\n{preview}"
                    messagebox.showinfo(
                        "回收站操作完成",
                        f"已移入回收站：{len(moved)} 项\n失败或跳过：{len(failed)} 项"
                        f"{detail}{report_text}",
                    )
                elif event == "trash_error":
                    self.progress.stop()
                    self._set_busy(False)
                    self.status_var.set("回收站操作失败。")
                    if isinstance(payload, CleanerUnavailable):
                        messagebox.showerror("缺少依赖", str(payload))
                    else:
                        messagebox.showerror("回收站操作失败", str(payload))
                elif event == "error":
                    self.progress.stop()
                    self._set_busy(False)
                    self.status_var.set("扫描失败。")
                    messagebox.showerror("扫描失败", str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _open_report(self) -> None:
        if self.last_html is None or not self.last_html.exists():
            messagebox.showinfo("暂无报告", "请先完成一次扫描。")
            return
        _open_path(self.last_html)


def main() -> None:
    root = Tk()
    DiskAssistantGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
