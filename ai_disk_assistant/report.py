"""报告层：CSV / JSON / 统计摘要 / HTML 可视化四种产物的生成。

cli.py 与 gui.py 统一经 write_all_reports 走同一条流水线，不各自拼装。
"""

from __future__ import annotations

import csv
import html
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .metadata import format_size
from .models import Candidate, ScanStats


# ── CSV 字段表：Candidate.to_row() 的输出顺序与此一一对应 ────────────────
FIELDNAMES = [
    "path",
    "name",
    "suffix",
    "parent_folder",
    "size_bytes",
    "size_text",
    "modified_time",
    "accessed_time",
    "modified_time_ns",
    "accessed_time_ns",
    "device_id",
    "file_id",
    "local_reason",
    "candidate_score",
    "recommend_delete",
    "purpose",
    "advice_level",
    "advice_reason",
    "advice_source",
    "advice_evidence",
]


def default_report_path(extension: str = "csv") -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path("reports") / f"scan_{stamp}.{extension}"


def write_csv(candidates: Iterable[Candidate], output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDNAMES)
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(candidate.to_row())
    return path.resolve()


def write_json(candidates: Iterable[Candidate], output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [candidate.to_row() for candidate in candidates]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path.resolve()


# ── 统计摘要：所有聚合数字只在这里算一次 ─────────────────────────────────
def build_summary(
    candidates: Iterable[Candidate],
    scan_stats: ScanStats | Mapping[str, Any] | None = None,
    advisor_stats: Mapping[str, Any] | None = None,
    narrative: str | None = None,
) -> dict[str, Any]:
    items = list(candidates)
    total_size = sum(item.metadata.size_bytes for item in items)
    auto_items = [item for item in items if item.advice.recommend_delete]
    auto_size = sum(item.metadata.size_bytes for item in auto_items)
    purpose_counts = Counter(item.advice.purpose for item in items)
    level_counts = Counter(item.advice.advice_level for item in items)
    source_counts = Counter(item.advice.source for item in items)
    suffix_sizes: dict[str, int] = defaultdict(int)
    parent_sizes: dict[str, int] = defaultdict(int)
    for item in items:
        suffix_sizes[item.metadata.suffix or "<无后缀>"] += item.metadata.size_bytes
        parent_sizes[item.metadata.parent_folder] += item.metadata.size_bytes

    if isinstance(scan_stats, ScanStats):
        scan_data: Mapping[str, Any] = scan_stats.to_dict()
    else:
        scan_data = scan_stats or {}

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "candidate_count": len(items),
        "candidate_size_bytes": total_size,
        "candidate_size_text": format_size(total_size),
        "recommended_count": len(auto_items),
        "recommended_size_bytes": auto_size,
        "recommended_size_text": format_size(auto_size),
        "purpose_distribution": dict(purpose_counts.most_common()),
        "advice_level_distribution": dict(level_counts.most_common()),
        "advice_source_distribution": dict(source_counts.most_common()),
        "largest_suffixes": [
            {"suffix": suffix, "size_bytes": size, "size_text": format_size(size)}
            for suffix, size in sorted(suffix_sizes.items(), key=lambda pair: pair[1], reverse=True)[:10]
        ],
        "largest_directories": [
            {"directory": directory, "size_bytes": size, "size_text": format_size(size)}
            for directory, size in sorted(parent_sizes.items(), key=lambda pair: pair[1], reverse=True)[:10]
        ],
        "top_files": [
            {
                "path": item.metadata.path,
                "size_bytes": item.metadata.size_bytes,
                "size_text": item.metadata.size_text,
                "purpose": item.advice.purpose,
                "advice_level": item.advice.advice_level,
                "source": item.advice.source,
            }
            for item in sorted(items, key=lambda candidate: candidate.metadata.size_bytes, reverse=True)[:20]
        ],
        "scan": dict(scan_data),
        "ai": dict(advisor_stats or {}),
        # AI 深度分析综述（Markdown 原文）；未启用 AI 或生成失败时为空串。
        "narrative": (narrative or "").strip(),
    }


def write_summary_json(summary: Mapping[str, Any], output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(summary), ensure_ascii=False, indent=2), encoding="utf-8")
    return path.resolve()


@dataclass(slots=True)
class ReportPaths:
    """一次扫描产出的各报告文件路径；detail_json / overview 仅在显式要求时生成。"""

    csv: Path
    summary: Path
    html: Path
    detail_json: Path | None = None
    overview: Path | None = None


def write_all_reports(
    candidates: Iterable[Candidate],
    scan_stats: ScanStats | Mapping[str, Any] | None,
    advisor_stats: Mapping[str, Any] | None,
    *,
    csv_path: str | Path | None = None,
    detail_json_path: str | Path | None = None,
    summary_json_path: str | Path | None = None,
    html_path: str | Path | None = None,
    narrative: str | None = None,
    overview_md_path: str | Path | None = None,
) -> ReportPaths:
    """CLI 与 GUI 共用的报告流水线：CSV + 统计摘要 JSON + HTML 必写，明细 JSON 与综述按需。

    综述（narrative）非空时同时写入 HTML 报告与一份独立 Markdown；为空则完全不产生该产物。
    未显式给路径的报告落到 reports/ 目录下的默认时间戳文件名。
    """
    items = list(candidates)
    summary = build_summary(items, scan_stats, advisor_stats, narrative)
    return ReportPaths(
        csv=write_csv(items, csv_path or default_report_path("csv")),
        detail_json=write_json(items, detail_json_path) if detail_json_path else None,
        summary=write_summary_json(summary, summary_json_path or default_report_path("summary.json")),
        html=write_html_report(summary, html_path or default_report_path("html")),
        overview=(
            write_overview_markdown(narrative, overview_md_path or default_report_path("analysis.md"))
            if (narrative or "").strip()
            else None
        ),
    )


# ── AI 综述渲染：极简 Markdown 子集 → HTML（零第三方依赖）─────────────────
# 只支持综述提示词约定的语法：标题、有序/无序列表、加粗、行内代码。
# 先 html.escape 再套标签，模型输出永远不能注入 HTML。
def _inline_markdown(text: str) -> str:
    escaped = html.escape(text)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return escaped


def markdown_to_html(text: str) -> str:
    """把 AI 综述转成 HTML 片段；未识别的行按段落处理，绝不原样透传标签。"""
    blocks: list[str] = []
    list_tag: str | None = None

    def close_list() -> None:
        nonlocal list_tag
        if list_tag is not None:
            blocks.append(f"</{list_tag}>")
            list_tag = None

    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            close_list()
            continue
        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            close_list()
            level = min(len(heading.group(1)) + 1, 5)  # "# " → h2（h1 留给报告标题）
            blocks.append(f"<h{level}>{_inline_markdown(heading.group(2))}</h{level}>")
            continue
        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        if bullet:
            if list_tag != "ul":
                close_list()
                blocks.append("<ul>")
                list_tag = "ul"
            blocks.append(f"<li>{_inline_markdown(bullet.group(1))}</li>")
            continue
        numbered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if numbered:
            if list_tag != "ol":
                close_list()
                blocks.append("<ol>")
                list_tag = "ol"
            blocks.append(f"<li>{_inline_markdown(numbered.group(1))}</li>")
            continue
        close_list()
        blocks.append(f"<p>{_inline_markdown(stripped)}</p>")
    close_list()
    return "\n".join(blocks)


def write_overview_markdown(narrative: str, output: str | Path) -> Path:
    """把 AI 综述单独落一份 Markdown（便于贴进聊天/文档，也是"截图式分析"的成品）。"""
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = (
        "# P4Disk4P 深度分析报告\n\n"
        f"> 生成时间：{stamp}　|　由 AI 依据本地扫描的聚合事实撰写，未读取任何文件内容。\n"
        "> 本报告仅为分析建议，自动流程不会移动或删除任何文件。\n\n"
        f"{narrative.strip()}\n"
    )
    path.write_text(body, encoding="utf-8")
    return path.resolve()


def _distribution_rows(distribution: Mapping[str, int]) -> str:
    maximum = max(distribution.values(), default=1)
    rows: list[str] = []
    for label, count in distribution.items():
        width = max(4, int(count / maximum * 100))
        rows.append(
            "<div class='bar-row'>"
            f"<span>{html.escape(str(label))}</span>"
            f"<div class='bar-track'><div class='bar' style='width:{width}%'></div></div>"
            f"<strong>{count}</strong></div>"
        )
    return "".join(rows) or "<p>暂无数据</p>"


def write_html_report(summary: Mapping[str, Any], output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    top_rows = "".join(
        "<tr>"
        f"<td title='{html.escape(str(item['path']))}'>{html.escape(Path(str(item['path'])).name)}</td>"
        f"<td>{html.escape(str(item['size_text']))}</td>"
        f"<td>{html.escape(str(item['purpose']))}</td>"
        f"<td>{html.escape(str(item['advice_level']))}</td>"
        f"<td>{html.escape(str(item['source']))}</td>"
        "</tr>"
        for item in summary.get("top_files", [])
    )
    scan = summary.get("scan", {})
    ai = summary.get("ai", {})
    narrative = str(summary.get("narrative", "") or "").strip()
    narrative_section = (
        "<section class='panel narrative' style='margin-top:16px'>"
        "<h2>AI 深度分析</h2>"
        "<p class='muted'>由 AI 依据本地扫描的聚合事实撰写，未读取任何文件内容；"
        "以下内容仅为分析建议，自动流程不会移动或删除任何文件。</p>"
        f"{markdown_to_html(narrative)}</section>"
        if narrative
        else ""
    )
    document = f"""<!doctype html>
<html lang='zh-CN'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>P4Disk4P 扫描摘要</title>
<style>
body{{font-family:Segoe UI,Microsoft YaHei,sans-serif;margin:0;background:#f5f7fb;color:#1f2937}}
main{{max-width:1120px;margin:32px auto;padding:0 20px}}
h1{{margin-bottom:6px}} .muted{{color:#6b7280}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin:24px 0}}
.card,.panel{{background:white;border:1px solid #e5e7eb;border-radius:14px;padding:18px;box-shadow:0 5px 18px rgba(15,23,42,.05)}}
.value{{font-size:28px;font-weight:700;margin-top:8px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}}
.bar-row{{display:grid;grid-template-columns:120px 1fr 36px;gap:10px;align-items:center;margin:12px 0}}
.bar-track{{height:11px;background:#e5e7eb;border-radius:9px;overflow:hidden}} .bar{{height:100%;background:#4f46e5;border-radius:9px}}
table{{width:100%;border-collapse:collapse}} th,td{{text-align:left;padding:10px;border-bottom:1px solid #eef0f4;font-size:14px}} th{{color:#4b5563}}
code{{background:#eef2ff;padding:2px 6px;border-radius:5px}}
.narrative h2{{font-size:19px;margin:18px 0 8px}} .narrative h3{{font-size:16px;margin:14px 0 6px}}
.narrative h4,.narrative h5{{font-size:15px;margin:12px 0 6px}}
.narrative p{{margin:8px 0;line-height:1.75}} .narrative li{{margin:4px 0;line-height:1.7}}
.narrative ul,.narrative ol{{margin:8px 0 8px 22px;padding:0}}
</style>
</head>
<body><main>
<h1>P4Disk4P 扫描摘要</h1>
<p class='muted'>生成时间：{html.escape(str(summary.get('generated_at', '')))}</p>
<section class='cards'>
<div class='card'><div class='muted'>候选文件</div><div class='value'>{summary.get('candidate_count', 0)}</div></div>
<div class='card'><div class='muted'>候选总大小</div><div class='value'>{html.escape(str(summary.get('candidate_size_text', '0 B')))}</div></div>
<div class='card'><div class='muted'>明确建议删除</div><div class='value'>{summary.get('recommended_count', 0)}</div></div>
<div class='card'><div class='muted'>预计可释放</div><div class='value'>{html.escape(str(summary.get('recommended_size_text', '0 B')))}</div></div>
</section>
{narrative_section}
<section class='grid'>
<div class='panel'><h2>建议等级</h2>{_distribution_rows(summary.get('advice_level_distribution', {}))}</div>
<div class='panel'><h2>判断来源</h2>{_distribution_rows(summary.get('advice_source_distribution', {}))}</div>
<div class='panel'><h2>用途分布</h2>{_distribution_rows(summary.get('purpose_distribution', {}))}</div>
<div class='panel'><h2>运行信息</h2>
<p>检查文件：<strong>{scan.get('visited_files', 0)}</strong></p>
<p>命中候选：<strong>{scan.get('matched_candidates', 0)}</strong></p>
<p>判定单元：<strong>{scan.get('unit_count', 0)}</strong>（AI 实判 {scan.get('units_judged', 0)} 个）</p>
<p>扫描耗时：<strong>{scan.get('elapsed_seconds', 0)} 秒</strong></p>
<p>AI 协议：<strong>{html.escape(str(ai.get('api_style', '未启用')))}</strong></p>
<p>AI 请求：<strong>{ai.get('api_calls', 0)}</strong>，缓存命中：<strong>{ai.get('cache_hits', 0)}</strong></p>
<p>AI 重试：<strong>{ai.get('retries', 0)}</strong>，失败项：<strong>{ai.get('failures', 0)}</strong></p>
</div>
</section>
<section class='panel' style='margin-top:16px'><h2>体积最大的候选文件</h2>
<table><thead><tr><th>文件</th><th>大小</th><th>用途</th><th>建议</th><th>来源</th></tr></thead><tbody>{top_rows}</tbody></table>
</section>
<p class='muted'>报告只展示分析结果；实际清理仍需在程序中二次确认，并会重新核对文件状态。</p>
</main></body></html>"""
    path.write_text(document, encoding="utf-8")
    return path.resolve()
