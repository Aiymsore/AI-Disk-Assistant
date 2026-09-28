# P4Disk4P

Windows 磁盘分析助手：**直读 NTFS 卷的 $MFT 秒级建立全盘快照**，本地安全规则打底、AI 分层判读，产出可解释的清理建议与报告。**自动流程只产出建议，永不移动或删除任何文件**；唯一的删除入口是用户在列表行首勾选 + 二次确认的手动删除，且一律移入回收站（可还原）。

执行分析的硬性前置条件：**管理员权限（MFT 直读）+ 已配置 AI**，缺一在入口拒绝并给出引导（GUI 提供"以管理员重启"一键提权）。

---

## 一、两条核心机制

### 1. MFT 直读扫描（`mft_scanner.py` → `inventory.py`）——全盘快照的唯一来源

**为什么不用 `os.walk` 之类的文件系统 API？** 逐目录枚举 + 逐文件 `stat` 在百万文件的卷上是分钟级耗时，且受目录树形状影响。NTFS 把每个文件/目录的元数据集中连续地存放在主文件表（$MFT）里，顺序读完这一张表就等于拿到了全卷清单——这正是 WizTree / Everything 的路线。代价是需要管理员权限打开卷句柄（`\\.\D:`），且仅适用于 NTFS（exFAT/FAT 没有 $MFT）。

**五步管线**（`snapshot_volume`，全程恒定内存，路径重建放在 SQL 里完成）：

1. **解析引导扇区**（`parse_boot_sector`）：从 $Boot 取"每 MFT 记录占用的簇数"（偏移 **0x40**，有符号字节；负值表示记录大小 = 2^-n 字节——曾误读 0x44 导致粒度错 4 倍）、$MFT 起始 LCN（0x30）；
2. **顺序读取 $MFT 数据流**（`_read_mft_image`）：按 $MFT 自身 $DATA 的 runlist 逐段读，切成定长记录；`parse_record` 修复扇区尾（update sequence fixup）、**跳过已释放记录**（删除只清"使用中"位，残留属性会产生幽灵文件与重复路径）与元文件（frn < 16）；
3. **解析记录属性**：$FILE_NAME 取名字/父目录引用/大小，$STANDARD_INFORMATION 取时间，非常驻 $DATA 取真实大小；**所有入库名值都钳制到 int64**——损坏记录的垃圾时间戳/体积曾导致 SQLite 绑定溢出；
4. **暂存 + 路径重建**：批量写入 `mft_staging`（按 `parent` 建索引，递归 CTE 每出队一条都要按父引用查子节点，无索引就是 85 万次全表扫描）→ 递归 CTE 从根（frn=5）出发沿父引用拼出完整路径，`parent_path` 随递归直接携带，避免二次回填；
5. **落库与回收**：写入 `files`/`dirs`（`OR REPLACE` 兜底同路径冲突）→ 目录聚合 → **只保留最近 3 次快照并 VACUUM**，库体积不随扫描次数增长。

**mtime 的态度**：Windows 下 mtime 不可靠（解压保留原时间、安装器回写旧时间），因此它**只入库备查、随报告导出，不参与任何打分与判定**；垃圾时间值归零或钳制，显示层遇到无法表示的值统一显示"—"。

### 2. AI 运行规则（`ai_advisor.py` + `safety.py` + `privacy.py` + `cache.py` + `units.py`）

AI 承担**四类独立任务**，各有专属提示词与失败哲学：

| 任务 | 触发位置 | 输入 | 失败时的行为 |
|---|---|---|---|
| **文件介绍**（GUI 评审核心） | GUI「AI 介绍所选」`describe_items` | 选中文件/目录的元数据（名称、路径模式、大小、聚合统计） | 抛错由界面弹窗呈现，不影响其他功能 |
| **判定单元判读** | CLI analyze 评分管线 `advise_units` | 同目录+同后缀+同大小档的文件组聚合统计 | 降级到本地规则/安全兜底，不中断 |
| **区域圈选**（建议性） | 全盘分析阶段一 `suggest_areas` | 体积 top 目录摘要 | 返回 None，回退体积启发式 |
| **重复组判读**（提示性） | 全盘分析 `review_duplicates` | 同体积文件组 | 返回 None，仅放弃提示 |
| **深度分析综述**（展示性） | `analyze_root` 收尾 / GUI「AI 深度分析」 | 整目录聚合事实（排名、后缀构成、最大文件、重复组、评审分布） | 返回 None，不写综述、不影响任何判定 |

前二/三类任务输出**结构化 JSON**（每单元 5 字段 + 证据 + 枚举），走严格校验与决策表；
第四类**深度分析综述**（`overview.py` + `summarize_overview`）输出**自由 Markdown 长文**，
定位是"把磁盘占用截图发给 AI"那种整体解读：空间去了哪、重点区域点评、风险与不确定项、
建议处理顺序。它与前三类的边界是硬约束——**综述只进报告与界面，永远不参与 `recommend_delete`**，
因此它既不需要枚举校验，也不可能让某个文件变成"建议删除"。事实载荷来自快照库，
按隐私三档裁剪路径，并显式声明输出预算（`AI_OVERVIEW_MAX_TOKENS`）；
缓存键含事实内容哈希，盘面没变时重复分析零成本。

**单元判定的六道关**（`advise_units` + `decide_unit_advice`，任何一道都只能让建议更保守，即 never-upgrade）：

1. **本地硬守卫**（`local_safety_guard`）：受保护目录 / 可执行配置后缀 / 用户内容后缀能定论的**就地裁决，AI 无权推翻**；位于临时/缓存/下载目录的安装包与压缩包、以及其余未知类型放行给 AI；
2. **缓存查询**：缓存键 = 供应商标识 + 隐私档 + 单元指纹 + `PROMPT_VERSION` + `RULES_VERSION`。指纹只含"目录+后缀+大小档"这类稳定模式（不含数量/体积/时间），同模式文件跨扫描直接命中，"同样的盘状态必得同样的建议"由缓存数学保证；
3. **批量 HTTP**：支持 `chat_completions` 与 `responses` 两种协议，429/5xx 指数退避重试，坏批次二分拆分隔离，单条坏数据不拖累整批；
4. **严格校验**：字段完整性、purpose/level 枚举、reason/evidence 长度、id 不重不漏不越界，违反即报错；
5. **证据闸门**：AI 必须从输入载荷中原样引用至少一条"字段名=值"事实，编造或对不上 → 整条判定降级"人工确认"。载荷字段即校验白名单（`unit_payload_for_ai`）；
6. **决策表合成**：最终建议 = min(AI 等级, 守卫上限)。守卫"人工确认"的单元，AI 最高只能到"谨慎删除"；守卫"不建议删除"则彻底否决。

**隐私三档**（`privacy.py`）：`strict` 只给目录上下文标记（不发路径）/ `balanced` 匿名化用户名与主目录（默认，GUI 固定此档）/ `full` 原样。载荷中不含任何时间字段。

**兜底原则**：AI 是建议者不是执行者——未配置、请求失败、返回异常、证据不足，任何失败路径最终都落到 `safe_fallback_advice`（"未知用途 + 人工确认 + 不自动删除"），扫描永不中断。自动流程**永不执行删除**；唯一的删除入口在 GUI：行首方框勾选 → 二次确认 → `cleaner.py` 移入回收站（受保护目录无条件拒绝）。

---

## 二、逐模块解析

### 入口层

| 文件 | 职责 |
|---|---|
| `main.py` | CLI 启动脚本，转发到 `ai_disk_assistant.cli.main` |
| `gui.py`（根目录） | GUI 启动脚本，转发到 `ai_disk_assistant.gui.main` |
| `ai_disk_assistant/cli.py` | 子命令 `inspect`（判断单个文件，走逐文件管线）与 `analyze`（整卷区域分析）；前置条件校验、参数解析、控制台输出 |
| `ai_disk_assistant/gui.py` | Tkinter 界面 + 纯 ttk 视觉系统（`_setup_style`，DPI 感知适配高分屏）：卷选择、扫描控制（暂停/继续/取消）、目录下钻浏览（双击进入）、扩展名分类面板、选中让 AI 介绍（是什么/删除影响/处理建议，可只看"可清理"）、行首方框勾选 + 「删除勾选」（移入回收站）、`AIConfigDialog` 图形编辑 .env。扫描/介绍/删除跑后台线程，经事件队列回主线程刷新 |

### 扫描层

| 文件 | 职责 |
|---|---|
| `ai_disk_assistant/mft_scanner.py` | 上述五步管线全部实现：`parse_boot_sector` / `parse_runlist` / `parse_record` 是可独立测试的纯函数；`ScanControl` 提供暂停/继续/取消与确定性进度；`snapshot_volume` 编排整卷落库 |
| `ai_disk_assistant/inventory.py` | SQLite 快照库（`snapshots`/`files`/`dirs` 三表 + 两个索引），`analyze` 的唯一事实源。提供目录聚合（`refresh_dir_aggregates`）、下钻查询（`top_dirs`/`child_dirs`/`direct_files`/`files_under`）、同体积组（`duplicate_groups`）、快照保留（`prune_snapshots`，默认留 3 次 + VACUUM） |

### 分析与评分层

| 文件 | 职责 |
|---|---|
| `ai_disk_assistant/analyzer.py` | 全盘区域分析：阶段一目录摘要 → AI 圈区域（无 AI 回退体积启发式）→ **递归下钻**（子目录 ≥6 的大区域再圈一层，叶子目录才对子树文件整体评分，每层散文件单独评审不漏扫）→ 同体积组提示。`analyze_root` 是 CLI/GUI 共用入口 |
| `ai_disk_assistant/scanner.py` | 纯计算评分层：`_signals_from_facts` 按后缀/目录上下文/大小多信号打分（**无年龄信号**，不做文件系统遍历）；`judge_metadata_records` 归并判定单元 → `advise_units` → 展开回候选。`ScanPolicy` 是扫描参数默认值的唯一来源 |

### 安全层

`safety.py` —— 不依赖网络与 AI 的底线，全项目后缀/目录常量的唯一定义处：

- `PROTECTED_DIR_NAMES` / `EXECUTABLE_OR_CONFIG_SUFFIXES` / `USER_CONTENT_SUFFIXES`：守卫名单；
- `INSTALLER_CONTEXT_NAMES` 等：上下文常量——安装包信号只在下载/临时/缓存目录生效，程序目录里的 exe 不再涌入候选；
- `local_safety_guard`：硬守卫，`source="local-guard"` 的结论 AI 无权推翻；
- `decide_unit_advice`：决策表纯函数，`_GUARD_CAPS` 定义各守卫等级的激进上限。

### AI 决策层

| 文件 | 职责 |
|---|---|
| `ai_disk_assistant/ai_advisor.py` | `HybridAdvisor` 主入口：`describe_items`（文件介绍，GUI 评审核心）+ `advise_units`（单元管线，CLI analyze 用）+ `advise`/`advise_many`（逐文件管线，CLI inspect 用）+ `suggest_areas`/`review_duplicates`（建议性任务）+ `probe`（连接测试）。`_validate_advice` 严格校验，`_request_batch_resilient` 二分降级，`build_advisor` 为 CLI/GUI 共用工厂 |
| `ai_disk_assistant/cache.py` | SQLite 建议缓存（WAL），键由 `make_key` 统一构造 |
| `ai_disk_assistant/privacy.py` | 三档隐私裁剪 + 路径匿名化（`anonymize_path`），载荷键名即证据校验白名单 |
| `ai_disk_assistant/units.py` | 判定单元归并：`size_bucket` 大小分桶（桶边界一经发布只能追加）、`unit_fingerprint` 稳定模式指纹、`build_units` 按最高候选分降序输出 |
| `ai_disk_assistant/overview.py` | 深度分析事实层：`build_overview_payload` 把快照库聚合成"整盘/整目录视图"（范围总量、子目录排名、深层热点、后缀构成、最大文件、重复组、本次评审分布），按隐私三档裁剪路径；与 units.py 的**逐条视角**互补，是唯一产出自由长文的载荷 |

### 支撑层

| 文件 | 职责 |
|---|---|
| `ai_disk_assistant/cleaner.py` | 手动删除执行层（GUI 勾选 + 二次确认后的唯一删除入口）：`plan_deletion` 按快照库展开目录子树/去重并拒绝受保护路径，`split_permanent` 读回收站配额预测"会被永久删除"的目标供确认框提前告知，`recycle_paths` 分块执行 + 存在性复核（Windows 走 `SHFileOperationW + FOF_ALLOWUNDO` 移入回收站，弹窗父化到应用窗口并显示进度），`prune_empty_dirs` 清掉删空后的目录壳；快照回删由调用方经 `inventory.remove_files` / `remove_dir_subtree` 完成 |
| `ai_disk_assistant/metadata.py` | `format_size` / `format_mtime`（垃圾时间显示"—"，防脏数据中断渲染）/ `format_pct`（一位小数百分比）；`get_file_metadata` 采集单文件 stat 快照（CLI inspect 用） |
| `ai_disk_assistant/config.py` | 手写 .env 解析器（保留注释与未知行）、`Settings.from_env`、默认路径/UA 的唯一定义处；PyInstaller 冻结环境下配置跟随可执行文件 |
| `ai_disk_assistant/admin.py` | 管理员检测（`is_user_admin`）与 UAC 提权重启（`relaunch_as_admin`）、分析前置条件清单 |
| `ai_disk_assistant/models.py` | 数据结构基座：`FileMetadata`/`Unit`/`Advice`/`Candidate`/`ScanStats`、合法值域（`PURPOSES`/`ADVICE_LEVELS`）、统一兜底约束（`Advice.__post_init__`）与安全默认工厂（`safe_fallback_advice`） |
| `ai_disk_assistant/report.py` | CSV / 明细 JSON / 统计摘要 JSON / HTML 四种报告 + AI 综述 Markdown，`write_all_reports` 为唯一流水线；`markdown_to_html` 是零依赖的 Markdown 子集渲染（先转义再套标签，模型输出无法注入 HTML） |
| `ai_disk_assistant/analyzer.py` | 全盘区域分析：圈区域 → 递归下钻 → 叶子评分 → 重复组提示 → **深度分析综述**（写入 `AnalyzeResult.narrative`，只作展示，不回流判定） |

### 外围脚本

- `tests/`：125 个用例——MFT 解析纯函数与合成整卷端到端、快照库聚合与回收、守卫真值表、决策表、证据校验、单元缓存、区域分析（启发式 + AI mock）、协议解析、.env 读写、报告生成、手动删除计划/执行/回收站容量预测/快照同步、DPI 无关的格式化函数
- `tools/test_ai_connection.py`：独立验证 AI 密钥、协议与结构化返回
- `evaluation/run_benchmark.py`：标注数据上对比 `local_rules` / `pure_ai` / `hybrid` 三种方案
- `demo/create_demo_files.py`：生成演示文件树

---

## 三、一次评审的执行顺序

1. GUI 选卷 → 管理员权限校验 → `snapshot_volume` 直读 $MFT 建快照（唯一事实来源），完成后自动回收旧快照
2. 目录树双击下钻，数据全部来自快照库（毫秒级）；扩展名面板同步聚合
3. 选中目录/文件 → `describe_items`：AI 介绍"是什么 / 删除影响 / 处理建议（可清理/需核对/别动）"，
   结果按处理建议着色、可只看"可清理"；纯展示任务，不经过守卫与判定管线，不参与任何自动行为
4. 自动流程不执行任何删除；介绍结果缓存复用（提示词版本进缓存键）
5. **手动删除（可选）**：列表行首方框勾选文件/目录（点表头全选）→ 顶栏「删除勾选」→
   二次确认（显示文件数/体积/被拒目标，以及**超回收站容量将被永久删除**的目标）→
   `plan_deletion` 展开子树并拒绝受保护路径 → `recycle_paths` 移入回收站
   （系统弹窗父化到应用窗口、显示进度与"已删 i/n"状态）→ 按磁盘事实回删快照行并刷新目录聚合与当前视图
6. **深度分析（可选）**：GUI 点「AI 深度分析（当前目录）」或 CLI `analyze` 收尾时，把当前范围的聚合事实
   （排名、后缀构成、最大文件、重复组、评审分布）一次性交给 AI，写出一份自由 Markdown 综述并落盘；
   综述同时进入 HTML 报告顶部与独立的 `reports/…_analysis.md`，界面提供复制全文与打开文件

## 四、相关文档

- [docs/code-map.md](docs/code-map.md) —— 符号级代码地图、并发模型与维护守则
- [docs/redundancy-report.md](docs/redundancy-report.md) —— 冗余代码清单与两轮处置记录
- [docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md) —— 安装与 AI 配置
- [docs/BUILD_EXE.md](docs/BUILD_EXE.md) —— Windows 打包
