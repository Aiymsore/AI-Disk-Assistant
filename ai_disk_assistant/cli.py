"""命令行入口：inspect（单文件判断）与 analyze（整卷 MFT 全盘分析）。

由根目录 main.py 调起，pyproject.toml 注册为 `p4disk4p` 命令。
本工具只产出建议与报告，不执行任何删除动作。
分析的硬性前置条件：管理员权限（MFT 直读）+ 已配置 AI；缺一拒绝执行并给出引导。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .admin import analysis_blockers
from .ai_advisor import HybridAdvisor, build_advisor
from .analyzer import analyze_root
from .config import DEFAULT_INVENTORY_PATH
from .metadata import format_size, get_file_metadata
from .mft_scanner import MftError
from .report import write_all_reports
from .scanner import ScanPolicy

# CLI 参数默认值以 ScanPolicy 为唯一来源，避免两处硬编码漂移。
_DEFAULT_POLICY = ScanPolicy()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="P4Disk4P",
        description="以本地安全规则为底线、由 AI 提供可解释建议的 Windows 磁盘分析工具（只建议，不删除）。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="分析单个文件")
    inspect_parser.add_argument("file", help="目标文件路径")
    inspect_parser.add_argument("--no-ai", action="store_true", help="仅使用本地规则")
    inspect_parser.add_argument(
        "--privacy", choices=["strict", "balanced", "full"], help="发送给 AI 的路径隐私级别"
    )

    analyze_parser = subparsers.add_parser(
        "analyze", help="整卷 MFT 全盘分析：圈区域 → 递归下钻 → 评分与重复组提示"
    )
    analyze_parser.add_argument("path", help="需要分析的目录（自动定位所在 NTFS 卷）")
    analyze_parser.add_argument(
        "--area-limit", type=int, default=6, help="每层最多圈出的区域数量"
    )
    analyze_parser.add_argument(
        "--drill-depth",
        type=int,
        default=2,
        help="被圈区域内的递归下钻层数（每层对大区域再做一次摘要圈选，小区域直接评分）",
    )
    analyze_parser.add_argument(
        "--privacy", choices=["strict", "balanced", "full"], help="发送给 AI 的路径隐私级别"
    )
    analyze_parser.add_argument(
        "--inventory", default=DEFAULT_INVENTORY_PATH, help="快照数据库路径"
    )
    analyze_parser.add_argument("--output", help="CSV 输出路径")
    analyze_parser.add_argument("--summary-json", help="可选统计摘要 JSON 输出路径")
    analyze_parser.add_argument("--html", dest="html_output", help="可视化 HTML 报告路径")
    analyze_parser.add_argument("--areas-json", help="可选区域与重复组 JSON 输出路径")
    return parser


def _advisor(privacy: str | None = None) -> HybridAdvisor:
    advisor = build_advisor(enable_ai=True, privacy_mode=privacy)
    mode = "AI + 本地安全规则" if advisor.ai_available else "本地安全规则（未配置 AI）"
    print(f"判断模式：{mode}；隐私模式：{advisor.settings.ai_privacy_mode}")
    return advisor


def _print_candidate(index: int, item) -> None:
    print(f"\n[{index}] {item.metadata.size_text} | {item.advice.advice_level} | {item.advice.purpose}")
    print(f"路径：{item.metadata.path}")
    print(f"候选依据：{item.local_reason}")
    print(f"候选分数：{item.candidate_score:.2f}")
    print(f"建议理由：{item.advice.reason}")
    if item.advice.evidence:
        print(f"判断依据：{'；'.join(item.advice.evidence)}")
    print(f"判断来源：{item.advice.source}")


# ── 子命令实现 ───────────────────────────────────────────────────────────
def command_inspect(args: argparse.Namespace) -> int:
    metadata = get_file_metadata(args.file)
    advisor = build_advisor(enable_ai=not args.no_ai, privacy_mode=args.privacy)
    advice = advisor.advise(metadata)
    print(json.dumps({"file": metadata.to_dict(), "advice": advice.to_dict()}, ensure_ascii=False, indent=2))
    return 0


def command_analyze(args: argparse.Namespace) -> int:
    advisor = _advisor(args.privacy)
    blockers = analysis_blockers(advisor.ai_available)
    if blockers:
        print("无法开始分析，前置条件不满足：")
        for blocker in blockers:
            print(f"- {blocker}")
        print("请以管理员身份运行终端，并在 .env / \"AI 配置\"中完成 AI 设置。")
        return 2

    policy = ScanPolicy()
    root_path = Path(args.path).expanduser().resolve()
    print(f"正在分析：{root_path}（卷 {root_path.anchor}，MFT 直读）")

    result = analyze_root(
        root_path,
        advisor,
        policy=policy,
        area_limit=max(args.area_limit, 1),
        inventory_path=args.inventory,
        progress=lambda message: print(message, flush=True),
        drill_depth=max(args.drill_depth, 0),
    )

    print(f"\n快照文件数：{result.snapshot_file_count}")
    print(f"扫描目标（含下钻叶子）：{len(result.areas)} 个")
    for index, area in enumerate(result.areas, start=1):
        print(
            f"\n[区域 {index}] {area.path}（{format_size(area.size_bytes)}，{area.file_count} 个文件）"
            f"—— 候选 {area.candidate_count} 个，{format_size(area.candidate_size)}"
        )
        print(f"圈定理由：{area.reason}")
    for index, item in enumerate(result.candidates[:30], start=1):
        _print_candidate(index, item)
    if len(result.candidates) > 30:
        print("\n终端仅展示前 30 个，完整结果请查看报告。")
    if result.duplicates:
        total_wasted = sum(group.wasted_bytes for group in result.duplicates)
        print(
            f"\n同体积疑似重复组：{len(result.duplicates)} 组，"
            f"若组内确为重复、去重后约可释放 {format_size(total_wasted)}（同体积未必同内容，请核对后处理）"
        )
        for index, group in enumerate(result.duplicates, start=1):
            verdict_text = f"[{group.verdict}] " if group.verdict else ""
            comment_text = f"——{group.comment}" if group.comment else ""
            print(
                f"[{index}] {format_size(group.size_bytes)} × {group.count} 个"
                f"（约可释放 {format_size(group.wasted_bytes)}）{verdict_text}{comment_text}"
            )
            for sample in group.sample_paths[:2]:
                print(f"    - {sample}")
            if len(group.sample_paths) > 2:
                print(f"    - ……共 {group.count} 个，其余见 areas JSON")

    reports = write_all_reports(
        result.candidates,
        result.stats,
        advisor.stats.to_dict(),
        csv_path=args.output,
        summary_json_path=args.summary_json,
        html_path=args.html_output,
    )
    print(f"\nCSV 报告：{reports.csv}")
    print(f"统计摘要：{reports.summary}")
    print(f"HTML 可视化：{reports.html}")
    if args.areas_json:
        areas_path = Path(args.areas_json)
        areas_path.parent.mkdir(parents=True, exist_ok=True)
        areas_path.write_text(
            json.dumps(
                {
                    "areas": [area.to_dict() for area in result.areas],
                    "duplicates": [group.to_dict() for group in result.duplicates],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"区域判定 JSON：{areas_path.resolve()}")
    print(
        f"AI 调用 {advisor.stats.api_calls} 次，分析 {advisor.stats.api_items} 项，"
        f"缓存命中 {advisor.stats.cache_hits} 项，重试 {advisor.stats.retries} 次。"
    )
    print("本工具只生成建议与报告，不会移动或删除任何文件。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            return command_inspect(args)
        if args.command == "analyze":
            return command_analyze(args)
    except MftError as exc:
        print(f"错误：{exc}")
        return 2
    except (FileNotFoundError, PermissionError, ValueError, OSError) as exc:
        print(f"错误：{exc}")
        return 1
    return 0
