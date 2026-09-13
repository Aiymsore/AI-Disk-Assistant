"""安全规则层：全项目的安全底线，不依赖任何网络与 AI。

- 常量区：受保护目录 + 全部后缀集合（唯一定义处，scanner 打分也从这里导入）。
- local_safety_guard：本地规则判定，凡返回 source="local-guard" 的结论 AI 无权推翻。
- decide_unit_advice：判定单元决策表（never-upgrade），AI 只能让建议更保守。
本工具只产出建议与报告，不做任何删除动作——因此这里没有任何"删除执行"类接口。
"""

from __future__ import annotations

from pathlib import Path

from .models import Advice, FileMetadata


# 本地规则与决策表的版本号：任何守卫/决策逻辑变更都必须递增，
# 它进入单元判定的缓存键，保证历史判定不因规则升级而被静默复用。
RULES_VERSION = "unit-decision-3"


PROTECTED_DIR_NAMES = {
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "system32",
    "syswow64",
    "drivers",
    "boot",
    "windowsapps",
    "system volume information",
    "$recycle.bin",
}

EXECUTABLE_OR_CONFIG_SUFFIXES = {
    ".sys",
    ".dll",
    ".exe",
    ".msi",
    ".msix",
    ".bat",
    ".cmd",
    ".ps1",
    ".reg",
    ".ini",
    ".db",
    ".sqlite",
    ".dat",
}

USER_CONTENT_SUFFIXES = {
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".pdf",
    ".txt",
    ".md",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".mp3",
    ".wav",
    ".mp4",
    ".mov",
    ".mkv",
    ".zip",
    ".rar",
    ".7z",
    ".py",
    ".js",
    ".ts",
    ".java",
    ".cpp",
    ".c",
    ".ipynb",
}


# ── 后缀常量（全项目唯一定义处，scanner.py 等模块从这里导入）─────────────
# 打分信号集合（SIGNAL_*）只决定一个文件是否值得作为候选展示；守卫集合与决策表
# 决定 AI 的建议最高能到哪一档。打分求宽、守卫求窄，两者取值差异是有意设计。
CODE_SUFFIXES = {".py", ".js", ".ts", ".java", ".cpp", ".c", ".ipynb"}
MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp3", ".wav", ".mp4", ".mov", ".mkv"}
# 用户内容里的压缩包子类，仅用于把用途细分为"存档或备份文件"。
USER_ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z"}

# ── 本地"建议删除"直判后缀（仅在 AI 不可用时使用）────────────────────────
# AI 可用时所有单元都会交给 AI 判断；只有未配置 AI 时，缓存/临时目录中的这几种
# 后缀才由本地直判"建议删除"。.bak 有意不在列——可能是用户手动备份。
ADVICE_DELETE_SUFFIXES = {".tmp", ".temp", ".log", ".dmp", ".old"}
# 扫描打分信号：只影响哪些文件值得作为候选展示，与直判后缀是两回事。
# 取值目前比直判集宽一档（含 .bak），两者允许独立演化——扫描求宽，直判求窄。
SIGNAL_JUNK_SUFFIXES = {".tmp", ".temp", ".log", ".dmp", ".old", ".bak"}
# 扫描打分用的安装包/压缩包信号（比用户内容分类覆盖更广，仅影响候选评分）。
INSTALLER_SUFFIXES = {".exe", ".msi", ".msix", ".apk"}
SIGNAL_ARCHIVE_SUFFIXES = {".zip", ".rar", ".7z", ".tar", ".gz", ".iso"}

SAFE_CONTEXT_NAMES = {"temp", "tmp", "cache", "caches", "logs", "log", "crashdumps", "crash"}

# 下载/暂存类目录名：安装包与压缩包只有位于这些上下文（或临时/缓存目录）时才被
# 视为"待清理对象"放行给 AI——程序目录里的同名文件维持保守上限（评分同样用它降噪）。
DOWNLOAD_CONTEXT_NAMES = {"downloads", "download", "下载"}
INSTALLER_CONTEXT_NAMES = SAFE_CONTEXT_NAMES | DOWNLOAD_CONTEXT_NAMES


def normalized_parts(path: str | Path) -> list[str]:
    text = str(path).replace("\\", "/")
    return [part.casefold() for part in text.split("/") if part]


# ── 路径判定 ──────────────────────────────────────────────────────────────
def is_protected_path(path: str | Path) -> bool:
    parts = set(normalized_parts(path))
    return bool(parts & PROTECTED_DIR_NAMES)


# ── 本地规则判定：从最严到最宽依次匹配，返回 None 表示交给 AI ────────────
def local_safety_guard(metadata: FileMetadata) -> Advice | None:
    path = Path(metadata.path)
    suffix = metadata.suffix.casefold()
    parts = set(normalized_parts(path))

    if is_protected_path(path):
        return Advice(
            recommend_delete=False,
            purpose="系统文件",
            advice_level="不建议删除",
            reason="文件位于系统或程序关键目录，误删可能导致系统或软件异常。",
            source="local-guard",
        )

    if suffix in INSTALLER_SUFFIXES and parts & INSTALLER_CONTEXT_NAMES:
        # 临时/缓存/下载目录里的安装包形态文件是典型可清理对象：守卫放行，AI 全权判断。
        # 程序目录里的同名文件（资源、内置工具）不走这里，继续落入下一条硬上限。
        return None

    if suffix in EXECUTABLE_OR_CONFIG_SUFFIXES:
        return Advice(
            recommend_delete=False,
            purpose="程序配置文件",
            advice_level="人工确认",
            reason="该类型可能影响程序安装、配置或运行，不能自动建议删除。",
            source="local-guard",
        )

    if suffix in USER_ARCHIVE_SUFFIXES:
        # 压缩包（旧安装包/旧游戏包）是最常见的可回收空间，且误删代价远低于文档媒体：
        # 守卫放行，由 AI 在证据闸门下全权判断，不再一刀切"人工确认"。
        return None

    if suffix in USER_CONTENT_SUFFIXES:
        if suffix in CODE_SUFFIXES:
            purpose = "代码或项目文件"
        elif suffix in MEDIA_SUFFIXES:
            purpose = "媒体文件"
        else:
            purpose = "用户文档"
        return Advice(
            recommend_delete=False,
            purpose=purpose,
            advice_level="人工确认",
            reason="该文件可能属于个人资料、媒体、压缩包或项目内容，必须人工确认。",
            source="local-guard",
        )

    if suffix in ADVICE_DELETE_SUFFIXES and parts & SAFE_CONTEXT_NAMES:
        return Advice(
            recommend_delete=True,
            purpose="临时文件" if suffix in {".tmp", ".temp"} else "日志文件",
            advice_level="建议删除",
            reason="文件位于缓存、日志或临时目录，且后缀符合常见清理对象。",
            source="local-rule",
        )

    if parts & SAFE_CONTEXT_NAMES:
        return Advice(
            recommend_delete=False,
            purpose="缓存文件",
            advice_level="谨慎删除",
            reason="文件位于缓存或临时目录，但仅凭元数据不足以直接建议删除。",
            source="local-rule",
        )

    return None


# ── 判定单元决策表：最终建议 = min(AI 等级, 守卫上限, 证据闸门) ──────────────
# 等级刻度：0=不建议删除（最保守）… 3=建议删除（最激进）。
# never-upgrade 原则：任何一道闸门都只能把建议往保守方向压，不能往激进方向抬。
# 设计取向：AI 是主判断者——除受保护目录/可执行配置/用户内容签名外，AI 有权直接
# 给"建议删除"（工具不执行删除，激进建议的代价只是用户多看一眼）。
_ADVICE_RANK = {
    "不建议删除": 0,
    "人工确认": 1,
    "谨慎删除": 2,
    "建议删除": 3,
}
_RANK_TO_LEVEL = {rank: level for level, rank in _ADVICE_RANK.items()}
# 守卫等级对应的激进上限：不建议删除→彻底否决；人工确认→最多到"谨慎删除"。
_GUARD_CAPS = {"不建议删除": 0, "人工确认": 2, "谨慎删除": 2, "建议删除": 3}


def advice_rank(level: str) -> int:
    return _ADVICE_RANK.get(level, 1)


def decide_unit_advice(
    guard: Advice | None,
    ai_advice: Advice,
    *,
    evidence_ok: bool,
) -> Advice:
    """把 AI 对判定单元的意见与本地守卫合成为最终建议（纯函数，可真值表测试）。

    - 证据闸门：AI 未原样引用输入事实（evidence 核对失败）→ 一律压到"人工确认"。
    - 守卫上限：本地规则已给出的结论构成 AI 不可逾越的上限；守卫为 None 的单元
      （即守卫放行的未知类型）AI 有完整判断权，包括"建议删除"。
    """
    ai_rank = advice_rank(ai_advice.advice_level)
    cap_rank = 3
    cap_reasons: list[str] = []

    if not evidence_ok:
        cap_rank = min(cap_rank, 1)
        cap_reasons.append("AI 未引用可核实的输入依据")
    if guard is not None:
        guard_cap = _GUARD_CAPS.get(guard.advice_level, 2)
        if guard_cap < ai_rank:
            cap_reasons.append("本地安全规则限制")
        cap_rank = min(cap_rank, guard_cap)

    final_rank = min(ai_rank, cap_rank)
    final_level = _RANK_TO_LEVEL[final_rank]

    if final_rank < ai_rank:
        purpose = guard.purpose if guard is not None else "未知用途"
        suffix_reasons = "；".join(cap_reasons)
        return Advice(
            recommend_delete=final_rank == 3,
            purpose=purpose,
            advice_level=final_level,
            reason=f"{ai_advice.reason}（已降级：{suffix_reasons}）" if suffix_reasons else ai_advice.reason,
            source="unit-fallback" if not evidence_ok else "unit-hybrid-guarded",
            evidence=ai_advice.evidence,
        )
    return Advice(
        recommend_delete=final_rank == 3,
        purpose=ai_advice.purpose,
        advice_level=final_level,
        reason=ai_advice.reason,
        source="unit-hybrid-cache" if ai_advice.source == "unit-cache" else "unit-hybrid-ai",
        evidence=ai_advice.evidence,
    )
