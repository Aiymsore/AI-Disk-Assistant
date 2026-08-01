from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, VERTICAL, BooleanVar, StringVar, Tk, filedialog, messagebox, ttk

from .ai_advisor import HybridAdvisor
from .config import Settings
from .report import build_summary, default_report_path, write_csv, write_html_report, write_summary_json
from .scanner import ScanPolicy, scan_with_stats


class DiskAssistantGUI:
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("AI Disk Assistant v1.2")
        self.root.geometry("1120x700")
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.last_html: Path | None = None

        self.path_var = StringVar(value=str(Path("demo/sample_disk").resolve()))
        self.status_var = StringVar(value="请选择目录并开始扫描。")
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

        options = ttk.Frame(self.root, padding=(12, 0, 12, 10))
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

        table_frame = ttk.Frame(self.root)
        table_frame.pack(fill=BOTH, expand=True, padx=12, pady=(0, 8))
        columns = ("size", "level", "purpose", "source", "reason", "path")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        headings = {
            "size": "大小",
            "level": "建议等级",
            "purpose": "用途",
            "source": "判断来源",
            "reason": "理由",
            "path": "路径",
        }
        widths = {"size": 90, "level": 90, "purpose": 125, "source": 110, "reason": 280, "path": 420}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], minwidth=70)
        scrollbar = ttk.Scrollbar(table_frame, orient=VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.pack(side=RIGHT, fill="y")

        bottom = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        bottom.pack(fill="x")
        self.progress = ttk.Progressbar(bottom, mode="indeterminate")
        self.progress.pack(fill="x")
        ttk.Label(bottom, textvariable=self.status_var).pack(anchor="w", pady=(6, 0))

    def _choose_directory(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.path_var.get() or str(Path.home()))
        if selected:
            self.path_var.set(selected)

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
        self.scan_button.configure(state="disabled")
        self.ai_test_button.configure(state="disabled")
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
        self.ai_test_button.configure(state="disabled")
        self.scan_button.configure(state="disabled")
        self.status_var.set("正在测试 AI 接口和结构化返回……")
        privacy = self.privacy_var.get()
        threading.Thread(target=self._ai_test_worker, args=(privacy,), daemon=True).start()

    def _ai_test_worker(self, privacy: str) -> None:
        try:
            settings = replace(Settings.from_env(), ai_privacy_mode=privacy)
            advisor = HybridAdvisor(settings, enable_ai=True)
            advice = advisor.probe()
            self.events.put(
                (
                    "ai_test_done",
                    (settings.ai_api_style, settings.ai_model, advice, advisor.stats),
                )
            )
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

    def _poll_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "progress":
                    self.status_var.set(f"已检查 {payload} 个文件……")
                elif event == "done":
                    result, stats, csv_path, _summary_path, html_path = payload  # type: ignore[misc]
                    self.last_html = Path(html_path)
                    for candidate in result.candidates:
                        self.tree.insert(
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
                    self.progress.stop()
                    self.ai_test_button.configure(state="normal")
                    self.scan_button.configure(state="normal")
                    self.status_var.set(
                        f"完成：检查 {result.stats.visited_files} 个文件，保留 {len(result.candidates)} 个候选；"
                        f"AI[{stats.api_style}] 请求 {stats.api_calls} 次，缓存命中 {stats.cache_hits} 项。报告：{csv_path}"
                    )
                elif event == "ai_test_done":
                    api_style, model, advice, stats = payload  # type: ignore[misc]
                    self.ai_test_button.configure(state="normal")
                    self.scan_button.configure(state="normal")
                    self.status_var.set(
                        f"AI 连接成功：{api_style} / {model}；请求 {stats.api_calls} 次。"
                    )
                    messagebox.showinfo(
                        "AI 连接成功",
                        f"协议：{api_style}\n模型：{model}\n"
                        f"结构化结果：{advice.purpose} / {advice.advice_level}\n"
                        f"理由：{advice.reason}",
                    )
                elif event == "ai_test_error":
                    self.ai_test_button.configure(state="normal")
                    self.scan_button.configure(state="normal")
                    self.status_var.set("AI 连接测试失败。")
                    messagebox.showerror("AI 连接失败", str(payload))
                elif event == "error":
                    self.progress.stop()
                    self.ai_test_button.configure(state="normal")
                    self.scan_button.configure(state="normal")
                    self.status_var.set("扫描失败。")
                    messagebox.showerror("扫描失败", str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _open_report(self) -> None:
        if self.last_html is None or not self.last_html.exists():
            messagebox.showinfo("暂无报告", "请先完成一次扫描。")
            return
        if sys.platform.startswith("win"):
            os.startfile(self.last_html)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(self.last_html)], check=False)
        else:
            subprocess.run(["xdg-open", str(self.last_html)], check=False)


def main() -> None:
    root = Tk()
    DiskAssistantGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
