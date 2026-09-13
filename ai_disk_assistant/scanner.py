"""评分与评审层：多信号打分 → 判定单元归并 → AI 判断（无任何文件系统遍历）。

扫描事实一律来自 MFT 快照库（inventory），本层只做纯计算：
- _signals_from_facts：凭后缀/所在目录/大小打分（无年龄——Windows 下 mtime 不可靠）；
- judge_metadata_records：候选归并为判定单元后交 AI，证据校验 + 决策表出最终建议。
analyze 全链路与诊断脚本共用同一套评分与评审语义。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from .ai_advisor import HybridAdvisor
from .models import Advice, Candidate, FileMetadata, safe_fallback_advice
from .safety import (
    INSTALLER_CONTEXT_NAMES,
    INSTALLER_SUFFIXES,
    SAFE_CONTEXT_NAMES,
    SIGNAL_ARCHIVE_SUFFIXES,
    SIGNAL_JUNK_SUFFIXES,
)
from .units import build_units


# ── 扫描策略：CLI/GUI 的参数默认值统一以这里的字段为唯一来源 ──────────────
# 有意不把修改时间用作判据：Windows 下 mtime 不可靠（解压保留原时间、安装器回写旧时间等），
# 修改时间仅随报告导出供人工参考，不参与打分、单元指纹或 AI 载荷。
@dataclass(frozen=True, slots=True)
class ScanPolicy:
    small_file_size: int = 10 * 1024
    big_file_size: int = 100 * 1024 * 1024
    # 有意不设 AI 判断数量上限：评审范围由用户选择的目标目录决定，
    # 且判定单元缓存保证同类文件跨扫描只判一次。


# ── 候选打分：多信号累加，无任何信号则不成为候选 ─────────────────────────
def _signals_from_facts(
    path_text: str,
    suffix: str,
    size_bytes: int,
    root: Path,
    policy: ScanPolicy,
) -> tuple[str, float, int] | None:
    """凭文件事实（路径/后缀/大小）打分。root 用于计算相对路径里的目录上下文。"""
    is_small = size_bytes <= policy.small_file_size
    is_big = size_bytes >= policy.big_file_size
    try:
        relative_text = str(Path(path_text).relative_to(root))
    except ValueError:
        relative_text = path_text
    # 分隔符归一：Linux 上 Path 不把反斜杠当分隔符，纯字符串切分保证上下文匹配跨平台一致。
    path_parts = {
        part.casefold() for part in relative_text.replace("\\", "/").split("/") if part
    }
    in_safe_context = bool(path_parts & SAFE_CONTEXT_NAMES)

    reasons: list[str] = []
    score = 0.0
    if suffix in SIGNAL_JUNK_SUFFIXES:
        reasons.append("临时/日志类后缀")
        score += 35
    if in_safe_context:
        reasons.append("位于缓存、日志或临时目录")
        score += 30
    if is_big:
        reasons.append("大文件，请人工确认用途")
        score += 20
    if is_small and (suffix in SIGNAL_JUNK_SUFFIXES or in_safe_context):
        reasons.append("小型缓存类文件")
        score += 3
    if suffix in INSTALLER_SUFFIXES and path_parts & INSTALLER_CONTEXT_NAMES:
        # 只在下载/临时/缓存上下文里才把安装包后缀当"残留"信号：
        # 否则程序目录里的每个 exe/dll 都会被拉进候选，评审全是对程序本体的误报。
        reasons.append("安装包残留")
        score += 12
    if suffix in SIGNAL_ARCHIVE_SUFFIXES:
        reasons.append("压缩包或镜像")
        score += 10

    has_context_signal = (
        in_safe_context
        or suffix in SIGNAL_JUNK_SUFFIXES | INSTALLER_SUFFIXES | SIGNAL_ARCHIVE_SUFFIXES
        or is_big
    )
    if not reasons or not has_context_signal:
        return None

    # Size only breaks ties moderately; it cannot make a random user file safe.
    score += min(math.log2(max(size_bytes, 1) + 1), 32) * 0.25
    return "；".join(dict.fromkeys(reasons)), score, size_bytes


# ── 评审主体：归并判定单元 → AI 判断 → 展开回候选 ─────────────────────────
def judge_metadata_records(
    metadata_records: list[tuple[FileMetadata, str, float]],
    advisor: HybridAdvisor,
    policy: ScanPolicy,
) -> tuple[list[Candidate], int]:
    """把 (元数据, 候选理由, 打分) 记录经单元管道变成带建议的候选。

    返回 (候选列表, 单元总数)。全部单元都会实判。
    analyze 的叶子扫描与散文件评审共用这一个出口，保证判定语义完全一致。
    """
    unit_count = 0
    unit_advice_by_path: dict[str, Advice] = {}
    if metadata_records:
        units, path_to_unit = build_units(metadata_records)
        unit_count = len(units)
        unit_advices = advisor.advise_units(units)
        advice_by_unit = {
            unit.unit_id: advice for unit, advice in zip(units, unit_advices, strict=True)
        }
        for path_text, unit_id in path_to_unit.items():
            advice = advice_by_unit.get(unit_id)
            if advice is not None:
                unit_advice_by_path[path_text] = advice

    results: list[Candidate] = []
    for metadata, reason, score in metadata_records:
        advice = unit_advice_by_path.get(metadata.path)
        if advice is None:
            advice = safe_fallback_advice("内部状态异常，已安全跳过。", source="not-evaluated")
        results.append(
            Candidate(
                metadata=metadata,
                local_reason=reason,
                advice=advice,
                candidate_score=score,
            )
        )
    return results, unit_count
