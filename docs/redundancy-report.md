# 冗余代码清单与处置记录

> 2026-09 重构时逐行核验产出。处置标记：✅ 已修复 / 📌 有意保留（附原因）/ 💡 建议未执行。
> 重构前基线见 git 首个提交（7cb9dfa）。

## A. 死代码（定义了但从未调用）

| # | 原位置 | 类型 | 处置 |
|---|---|---|---|
| 1 | `ai_advisor.py` 原 110-112 行 `_coerce_advice` | 死函数（自称"为外部导入保留"，实际全项目无调用） | ✅ 已删除 |
| 2 | `cache.py` 原 78-80 行 `AdviceCache.clear()` | 死方法（CLI/GUI/测试均未使用） | ✅ 已删除 |
| 3 | `scanner.py` 原 205-212 行 `scan_candidates` | 兼容包装器，仅旧测试使用 | ✅ 已删除，`tests/test_scanner.py` 改用 `scan_with_stats` |
| 4 | `models.py` `FileMetadata.accessed_time_ns` | 只写字段：被采集并导出 CSV，但无判定逻辑读取 | 📌 保留：随报告导出供人工核对；Windows atime 不可靠，快照校验只认 modified_time_ns（已加注释） |
| 5 | `ai_advisor.py` `AdvisorStats.prompt_tokens` 等 3 字段 | 只累计不展示 | 📌 保留：随 summary JSON 落盘供事后核对（已加注释） |
| 6 | `ai_advisor.py` `advise_ai_only_many` | 生产路径不调用 | 📌 保留：`evaluation/run_benchmark.py` 纯 AI 对照方案专用（已加注释） |

## B. 同名异值常量（重构前风险最高的一组）

| # | 原位置 | 问题 | 处置 |
|---|---|---|---|
| 7 | `scanner.py` 原 18 行 vs `safety.py` 原 77 行，两处都叫 `JUNK_SUFFIXES` | scanner 含 `.bak`，safety 不含——同名集合内容分叉，误删风险高 | ✅ 全部后缀集合收拢到 `safety.py` 唯一定义处，scanner 改为导入。按语义显式命名：`AUTO_DELETE_JUNK_SUFFIXES`（自动删除白名单，最保守，不含 .bak）与 `SIGNAL_JUNK_SUFFIXES`（打分信号 = 白名单 + .bak）。差异是有意设计，已用命名 + 注释固化 |
| 8 | `scanner.py` 原 20 行（6 个后缀） vs `safety.py` 原 74 行（3 个后缀），都叫 `ARCHIVE_SUFFIXES` | 同上 | ✅ 拆为 `SIGNAL_ARCHIVE_SUFFIXES`（打分）与 `USER_ARCHIVE_SUFFIXES`（内容分类），同处定义 |
| 9 | scanner 原 `INSTALLER_SUFFIXES` 与 safety `EXECUTABLE_OR_CONFIG_SUFFIXES` 在 .exe/.msi/.msix 上重叠 | 两处维护 | ✅ `INSTALLER_SUFFIXES` 移入 safety.py，与守卫集合同处定义并注明各自用途 |

## C. 复制粘贴的相似代码块

| # | 原位置 | 问题 | 处置 |
|---|---|---|---|
| 10 | 兜底 `Advice(False, "未知用途", "人工确认", ...)` 手写 6 处：ai_advisor 原 232/257/297/360 行、advise_ai_only_many 原 266 行、scanner 原 184 行 | 安全默认值散落多处，易漂移 | ✅ 提取 `models.safe_fallback_advice(reason, source)` 工厂，6 处全部替换 |
| 11 | 报告四连调用 3 处：`cli.py` 原 119-132、`gui.py` 原 397-400 与 481-484 | write_csv → build_summary → write_summary_json → write_html_report 流水线复制三遍 | ✅ 提取 `report.write_all_reports()`，三处统一接入；`ReportPaths` 返回各文件路径 |
| 12 | advisor 构造 3 处：`cli.py` 原 61-68、`gui.py` 原 377-379 与 389-390 | `Settings.from_env + replace(隐私) + HybridAdvisor` 组合重复 | ✅ 提取 `ai_advisor.build_advisor(enable_ai, privacy_mode)`，三处统一接入 |
| 13 | 候选大小求和 4 处：`cli.py:104/106`、`report.py` build_summary 内、`gui.py:429/446` | `sum(item.metadata.size_bytes ...)` 一行表达式 | 📌 保留：单行表达式的重复成本低，抽函数反而增加跳转；汇总统计仍以 build_summary 为唯一权威 |
| 14 | `metadata.py:36-39` 与 `cleaner.py:32,38-39` 的 getattr 纳秒/设备号回退模式 | 同一兼容写法两处各写一遍 | 📌 保留：仅 1-2 行的 platform 兼容模式，各自上下文语义不同（采集 vs 复核），已加注释说明用途 |
| 15 | `ai_advisor.py` 两个响应解析函数中重复的 `{"value": ...}` 解包（原 133-134 行 vs 162-164 行） | 相同分支写两遍 | ✅ 提取 `_text_part_to_str()`，两个协议解析共用 |
| 16 | `report.py` 4 个写入函数重复 `mkdir + write` 样板 | 同一模式四遍 | 📌 部分保留：CSV 需 utf-8-sig + newline、HTML 无缩进，参数差异大不宜强抽；主样板已随 write_all_reports 收敛 |
| 17 | TRASH 口令二次确认 CLI（终端 input）与 GUI（对话框）各一份 | 约定重复实现 | 📌 保留：UI 范式不同，强行统一收益低；已在两处加注释互相说明，口令约定保持一致 |

## D. 默认值 / 常量多处硬编码

| # | 原位置 | 问题 | 处置 |
|---|---|---|---|
| 18 | scanner `ScanPolicy` 默认值 vs `cli.py` argparse 默认又写一遍 vs `gui.py` 界面默认再写一遍 | 180/80/5000/0 三处硬编码 | ✅ CLI argparse 与 GUI 界面默认值全部改为引用 `ScanPolicy()` 字段 |
| 19 | `gui.py` 原 394 行 `max_candidates=1000` vs CLI 默认 5000 | 复制后私自改值且无说明 | 📌 保留差异：提为具名常量 `GUI_MAX_CANDIDATES = 1000` 并注释原因（Tkinter 树刷新性能），由"无声分叉"变为"有声明的设计决策" |
| 20 | User-Agent `"AI-Disk-Assistant/1.3"` 硬编码 3 处（config.py ×2、gui.py ×1），`.env.example` 还是 1.2 | 版本漂移 | ✅ 改为 `config.DEFAULT_USER_AGENT`，由 `__version__` 派生；.env.example 同步至 1.3 |
| 21 | 缓存路径 `".cache/ai_advice.sqlite3"` 硬编码 3 处（config.py ×2、gui.py ×1） | 路径漂移 | ✅ 收拢为 `config.DEFAULT_CACHE_PATH`，gui 导入使用 |
| 22 | SYSTEM_PROMPT 手抄 purpose/advice_level 枚举 vs `models.py` 常量 | 改 models 不会同步到提示词 | 📌 保留：提示词需自然语言措辞，无法直接插值常量集合；两侧均已加"改动须同步"的醒目注释，并写入 code-map 维护守则 |
| 23 | 版本号 4 处不同步：`__init__.py` 1.2.1、`pyproject.toml` 1.3.0、GUI 标题 v1.3、`.env.example` 1.2 | 实际冲突 | ✅ 统一为 1.3.0：`__version__` 为唯一来源，GUI 标题与 UA 自动派生，pyproject 加同步注释 |
| 24 | reason 120 字符限制两处用不同策略（ai_advisor 抛错 vs models 静默截断） | 语义分叉未说明 | ✅ 提取 `models.REASON_MAX_LENGTH` 共用常量；两种策略均为有意设计（严格校验 AI / 容错落地本地），已加注释区分 |

## E. 可合并的重复分支

| # | 原位置 | 问题 | 处置 |
|---|---|---|---|
| 25 | `_apply_hybrid_guard` 两个近似 if 分支（原 305-324 行） | 都返回"谨慎删除 + 不自动删除"，仅 reason 不同 | ✅ 合并为单一降级构造 + 两个 reason 判定 |
| 26 | `safety.py` 原 104-128 行两个 Advice 构造分支结构一致 | 可查表驱动 | 💡 未执行：两个分支判定逻辑实质不同（固定用途 vs 按子类细分），查表化会掩盖逻辑；已加分节注释提高可读性 |
| 27 | `scanner.py` 原 85-90 行 installer/archive 两个打分分支结构相同 | 可循环化 | 💡 未执行：仅 2 个分支且分值/文案各异，循环化收益不明显 |

## 成果统计

- 删除死代码 3 处；合并重复逻辑 11 处（兜底工厂 6 + 报告流水线 3 + advisor 构造 3 + 解析 2 + guard 2，部分重叠计）
- 常量收拢：后缀集合 2 组同名异值消除、默认值 4 类（策略/UA/缓存路径/版本号）归一
- 全部 18 个文件变更：+313 / -147 行（净增主要为中文注释与两份文档）
- 27 个测试全绿；CLI/GUI 行为不变（仅版本号显示由 1.3 → 1.3.0）

## F. remaster 后复审（2026-09-13）

> MFT 管线、快照回收、GUI 重设计、品牌化（P4Disk4P）落地后的第二轮全量审视。

### 死代码（✅ 本轮已删）

| # | 原位置 | 问题 | 处置 |
|---|---|---|---|
| F1 | `gui.py` `_start_ai_test` 双重定义（旧 623 行与新 919 行） | 后定义静默覆盖前者，功能虽同但属隐患 | ✅ 删除重复副本，保留一处 |
| F2 | `models.py` `ScanResult` 数据类 | 全仓零引用（分析层改用 `AnalyzeResult` 后遗留） | ✅ 删除 |
| F3 | `scanner.py` `_candidate_signals` | 零调用方（docstring 声称"仅诊断脚本使用"，诊断脚本实际也已改走快照事实评分） | ✅ 删除 |
| F4 | `safety.py` 用户内容分支的"存档或备份文件"标注 | 压缩包改为守卫放行后该分支不可达 | ✅ 删除 |

### 新增模块的审视（📌 有意保留 / 💡 可合并未执行）

| # | 位置 | 观察 | 处置 |
|---|---|---|---|
| F5 | `ai_advisor.suggest_areas` / `review_duplicates` | 各自重复约 20 行"双协议请求构造 + 提取 + 条目校验"，仅提示词/列表键/校验字段不同 | 💡 可合并为共享的 `_structured_list_request()`（需参数化条目解析器）；两处合计 110 行，合并收益中等，暂留 |
| F6 | `inventory.add_files` / `add_dirs` | 生产管线（snapshot_volume 的 SQL INSERT）已不使用，仅测试 seed 助手调用 | 💡 可移交测试侧；保留的代价是公共 API 面扩大。暂留，若后续无第三方使用再移 |
| F7 | 每文件 AI 管线 `advise` / `advise_many` / `_get_ai_advices` / `_apply_hybrid_guard` / `metadata_for_ai` | 生产扫描已全部走单元管线，每文件管线仅剩 CLI inspect（单文件产品功能）与 evaluation 基准对照使用 | 📌 有意保留：两者均为产品/评测功能，非冗余 |
| F8 | `FileMetadata.accessed_time(_ns)` / `device_id` / `file_id`、`AdvisorStats.prompt_tokens` / `completion_tokens` | 不参与判定/展示 | 📌 有意保留（注释已声明）：随报告导出供人工核对、进快照校验键、随 summary JSON 落盘 |
| F9 | `ADVICE_DELETE_SUFFIXES` ⊂ `SIGNAL_JUNK_SUFFIXES` | 后缀集合重叠 | 📌 有意保留（评分求宽、直判求窄，注释已声明） |
