"""命令行入口：inspect（单文件判断）与 scan（扫描目录 + 报告 + 可选回收站）。

由根目录 main.py 调起，pyproject.toml 注册为 `ai-disk-assistant` 命令。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .ai_advisor import HybridAdvisor, build_advisor
from .cleaner import CleanerUnavailable, move_to_trash, select_auto_candidates
from .metadata import format_size, get_file_metadata
from .report import write_all_reports
from .scanner import ScanPolicy, scan_with_stats

# CLI 参数默认值以 ScanPolicy 为唯一来源，避免两处硬编码漂移。
_DEFAULT_POLICY = ScanPolicy()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-disk-assistant",
        description="以本地安全规则为底线、由 AI 提供可解释建议的 Windows 磁盘分析工具。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="分析单个文件")
    inspect_parser.add_argument("file", help="目标文件路径")
    inspect_parser.add_argument("--no-ai", action="store_true", help="仅使用本地规则")
    inspect_parser.add_argument(
        "--privacy", choices=["strict", "balanced", "full"], help="发送给 AI 的路径隐私级别"
    )

    scan_parser = subparsers.add_parser("scan", help="扫描目录并生成报告")
    scan_parser.add_argument("path", help="需要扫描的目录")
    scan_parser.add_argument(
        "--old-days", type=int, default=_DEFAULT_POLICY.old_days, help="旧文件阈值（天）"
    )
    scan_parser.add_argument("--ai-limit", type=int, default=_DEFAULT_POLICY.ai_limit, help="AI 最多判断的候选数量")
    scan_parser.add_argument(
        "--max-candidates", type=int, default=_DEFAULT_POLICY.max_candidates, help="完整遍历后保留的候选数量"
    )
    scan_parser.add_argument(
        "--max-scan-files",
        type=int,
        default=_DEFAULT_POLICY.max_scan_files,
        help="最多检查的文件数，0 表示完整遍历",
    )
    scan_parser.add_argument("--no-ai", action="store_true", help="仅使用本地规则")
    scan_parser.add_argument(
        "--privacy", choices=["strict", "balanced", "full"], help="发送给 AI 的路径隐私级别"
    )
    scan_parser.add_argument("--output", help="CSV 输出路径")
    scan_parser.add_argument("--json", dest="json_output", help="可选明细 JSON 输出路径")
    scan_parser.add_argument("--summary-json", help="可选统计摘要 JSON 输出路径")
    scan_parser.add_argument("--html", dest="html_output", help="可视化 HTML 报告路径")
    scan_parser.add_argument(
        "--trash-auto",
        action="store_true",
        help="经再次确认和状态复核后，将“明确建议删除”的文件移入回收站",
    )
    return parser


def _advisor(no_ai: bool, privacy: str | None = None) -> HybridAdvisor:
    advisor = build_advisor(enable_ai=not no_ai, privacy_mode=privacy)
    mode = "AI + 本地安全规则" if advisor.ai_available else "本地安全规则（未调用 AI）"
    print(f"判断模式：{mode}；隐私模式：{advisor.settings.ai_privacy_mode}")
    return advisor


def _print_candidate(index: int, item) -> None:
    print(f"\n[{index}] {item.metadata.size_text} | {item.advice.advice_level} | {item.advice.purpose}")
    print(f"路径：{item.metadata.path}")
    print(f"候选依据：{item.local_reason}")
    print(f"候选分数：{item.candidate_score:.2f}")
    print(f"建议理由：{item.advice.reason}")
    print(f"判断来源：{item.advice.source}")


# ── 子命令实现 ───────────────────────────────────────────────────────────
def command_inspect(args: argparse.Namespace) -> int:
    metadata = get_file_metadata(args.file)
    advice = _advisor(args.no_ai, args.privacy).advise(metadata)
    print(json.dumps({"file": metadata.to_dict(), "advice": advice.to_dict()}, ensure_ascii=False, indent=2))
    return 0


def command_scan(args: argparse.Namespace) -> int:
    policy = ScanPolicy(
        old_days=max(args.old_days, 1),
        ai_limit=max(args.ai_limit, 0),
        max_candidates=max(args.max_candidates, 1),
        max_scan_files=max(args.max_scan_files, 0),
    )
    advisor = _advisor(args.no_ai, args.privacy)
    print(f"正在扫描：{Path(args.path).expanduser().resolve()}")

    scan_result = scan_with_stats(
        args.path,
        advisor=advisor,
        policy=policy,
        progress=lambda count: print(f"已检查 {count} 个文件……", flush=True),
    )
    candidates = scan_result.candidates
    total_size = sum(item.metadata.size_bytes for item in candidates)
    auto_candidates = select_auto_candidates(candidates)
    auto_size = sum(item.metadata.size_bytes for item in auto_candidates)

    print(
        f"\n共检查 {scan_result.stats.visited_files} 个文件，命中 {scan_result.stats.matched_candidates} 个候选，"
        f"最终保留 {len(candidates)} 个。"
    )
    print(f"候选总大小：{format_size(total_size)}")
    print(f"明确建议删除：{len(auto_candidates)} 个，总大小：{format_size(auto_size)}")
    for index, item in enumerate(candidates[:30], start=1):
        _print_candidate(index, item)
    if len(candidates) > 30:
        print("\n终端仅展示前 30 个，完整结果请查看报告。")

    reports = write_all_reports(
        candidates,
        scan_result.stats,
        advisor.stats.to_dict(),
        csv_path=args.output,
        detail_json_path=args.json_output,
        summary_json_path=args.summary_json,
        html_path=args.html_output,
    )
    print(f"\nCSV 报告：{reports.csv}")
    if reports.detail_json:
        print(f"明细 JSON：{reports.detail_json}")
    print(f"统计摘要：{reports.summary}")
    print(f"HTML 可视化：{reports.html}")
    print(
        f"AI 调用 {advisor.stats.api_calls} 次，分析 {advisor.stats.api_items} 项，"
        f"缓存命中 {advisor.stats.cache_hits} 项，重试 {advisor.stats.retries} 次。"
    )

    if args.trash_auto:
        # 回收站二次确认：CLI 用终端口令、GUI 用对话框，两处范式不同故各自实现，
        # 但口令约定（TRASH）与"先复核后移动"的流程保持一致。
        if not auto_candidates:
            print("没有可自动选择的文件，未执行回收站操作。")
            return 0
        print("\n以下操作不会永久删除，只会移动到系统回收站。")
        print("移动前会重新核对大小、修改时间和文件标识，发生变化的项目会自动跳过。")
        confirmation = input(f"确认移动 {len(auto_candidates)} 个文件请输入 TRASH：").strip()
        if confirmation != "TRASH":
            print("已取消。")
            return 0
        try:
            moved, failed = move_to_trash(auto_candidates)
        except CleanerUnavailable as exc:
            print(str(exc))
            return 2
        print(f"已移动到回收站：{len(moved)} 个；失败/跳过：{len(failed)} 个。")
        for path, reason in failed[:20]:
            print(f"- {path}: {reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            return command_inspect(args)
        if args.command == "scan":
            return command_scan(args)
    except (FileNotFoundError, PermissionError, ValueError, OSError) as exc:
        print(f"错误：{exc}")
        return 1
    return 0
