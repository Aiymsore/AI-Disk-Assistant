from __future__ import annotations

import csv
import html
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .metadata import format_size
from .models import Candidate, ScanStats


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


def build_summary(
    candidates: Iterable[Candidate],
    scan_stats: ScanStats | Mapping[str, Any] | None = None,
    advisor_stats: Mapping[str, Any] | None = None,
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
    }


def write_summary_json(summary: Mapping[str, Any], output: str | Path) -> Path:
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(summary), ensure_ascii=False, indent=2), encoding="utf-8")
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
    document = f"""<!doctype html>
<html lang='zh-CN'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>AI Disk Assistant 扫描摘要</title>
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
</style>
</head>
<body><main>
<h1>AI Disk Assistant 扫描摘要</h1>
<p class='muted'>生成时间：{html.escape(str(summary.get('generated_at', '')))}</p>
<section class='cards'>
<div class='card'><div class='muted'>候选文件</div><div class='value'>{summary.get('candidate_count', 0)}</div></div>
<div class='card'><div class='muted'>候选总大小</div><div class='value'>{html.escape(str(summary.get('candidate_size_text', '0 B')))}</div></div>
<div class='card'><div class='muted'>明确建议删除</div><div class='value'>{summary.get('recommended_count', 0)}</div></div>
<div class='card'><div class='muted'>预计可释放</div><div class='value'>{html.escape(str(summary.get('recommended_size_text', '0 B')))}</div></div>
</section>
<section class='grid'>
<div class='panel'><h2>建议等级</h2>{_distribution_rows(summary.get('advice_level_distribution', {}))}</div>
<div class='panel'><h2>判断来源</h2>{_distribution_rows(summary.get('advice_source_distribution', {}))}</div>
<div class='panel'><h2>用途分布</h2>{_distribution_rows(summary.get('purpose_distribution', {}))}</div>
<div class='panel'><h2>运行信息</h2>
<p>检查文件：<strong>{scan.get('visited_files', 0)}</strong></p>
<p>命中候选：<strong>{scan.get('matched_candidates', 0)}</strong></p>
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
