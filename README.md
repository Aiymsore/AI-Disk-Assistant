# AI Disk Assistant · 代码解析

本项目直读 NTFS 卷的 MFT 建立全盘快照，由本地安全规则与 AI 建议共同筛选可清理的候选文件，生成报告。**工具只产出建议，永不移动或删除任何文件**。执行分析的硬性前置条件：**管理员权限（MFT 直读）+ 已配置 AI**，缺一在入口拒绝并给出引导（GUI 提供"以管理员重启"）。

本文只说明各部分代码负责什么功能。符号级行号索引见 [docs/code-map.md](docs/code-map.md)，冗余清理记录见 [docs/redundancy-report.md](docs/redundancy-report.md)。

## 一、分层总览

```text
入口层      main.py / gui.py（根目录启动脚本）
            ai_disk_assistant/cli.py / ai_disk_assistant/gui.py
分析层      scanner.py    analyzer.py   inventory.py   report.py
AI 决策层   ai_advisor.py（+ cache.py）
支撑层      metadata.py   privacy.py    units.py    config.py
安全层      safety.py（含判定单元决策表）
数据层      models.py
```

依赖方向自上而下，无循环依赖。

## 二、各部分功能

### 入口层

| 文件 | 功能 |
|---|---|
| `main.py` | CLI 启动入口，转发到包内 `cli.main` |
| `gui.py`（根目录） | GUI 启动入口，转发到包内 `gui.main` |
| `ai_disk_assistant/cli.py` | 定义 `inspect`（判断单个文件）、`scan`（扫描目录并出报告）与 `analyze`（全盘区域分析）三个子命令；负责参数解析、控制台输出与流程编排 |
| `ai_disk_assistant/gui.py` | Tkinter 界面：`AIConfigDialog` 图形编辑 `.env` 配置（隐私统一 balanced）；主窗口控件（目录选择、等级着色的候选列表、判断依据列、以管理员重启）；分析/AI 测试的后台线程与事件队列轮询刷新 |

### 数据模型层

`models.py` —— 全项目共享的数据结构基座：

- `FileMetadata`：单个文件的 stat 快照（路径、大小、修改/访问时间、设备号、inode）
- `Advice`：一条清理建议（是否可删、用途、建议等级、理由、来源、证据）
- `Unit`：判定单元——同目录同后缀同大小档的候选文件聚合，AI 按"单元"给出建议
- `Candidate` / `ScanStats` / `ScanResult`：候选、扫描统计与总结果
- `PURPOSES` / `ADVICE_LEVELS`：用途与建议等级的合法值域，AI 返回结果按此校验
- `Advice.__post_init__` + `safe_fallback_advice`：所有建议的统一兜底约束与安全默认值工厂

### 安全规则层

`safety.py` —— 不依赖网络与 AI 的安全底线：

- 常量区：受保护目录名 + 全部后缀集合（全项目唯一定义处；扫描打分信号求宽、本地直判后缀求窄，两者有意独立，见行内注释）
- `local_safety_guard`：按后缀与所在目录给出本地判断，`source="local-guard"` 的结论 AI 无权推翻
- `decide_unit_advice`：判定单元决策表（纯函数）。never-upgrade 原则：最终建议 = min(AI 等级, 守卫上限, 证据闸门, 建议删除档白名单)，AI 只能让建议更保守

### 判定单元层

`units.py` —— 一致性与成本的核心：候选文件按"目录 + 后缀 + 大小档"归并为判定单元，`unit_fingerprint` 是稳定模式指纹（不含数量/体积/时间等易变值）。同模式文件跨扫描复用同一条 AI 判定与同一份缓存。**有意不使用修改时间作为判据**：Windows 下 mtime 不可靠（解压保留原时间、安装器回写旧时间），时间仅随报告导出供人工参考。

### 支撑层

- `metadata.py`：文件大小格式化；采集文件元数据快照
- `privacy.py`：把发送给 AI 的字段按 `strict`（不发路径）/ `balanced`（匿名化用户名）/ `full`（原路径）三档裁剪；覆盖逐文件与判定单元两种载荷
- `config.py`：手写 `.env` 解析器（保留注释与未知行）、环境变量加载与增量保存、`Settings` 配置类；`DEFAULT_CACHE_PATH` / `DEFAULT_INVENTORY_PATH` 等默认值的唯一定义处
- `cache.py`：SQLite 建议缓存。逐文件缓存按文件快照哈希；判定单元缓存按"单元指纹 + 提示词版本 + 规则版本"构成——同盘状态必得同建议，提示词或规则升级自动隔离旧缓存

### AI 决策层

`ai_advisor.py` —— `HybridAdvisor` 混合顾问，生产判断的主入口：

1. 本地硬守卫先行：受保护目录/可执行配置/用户内容签名能定论的直接采用，AI 无权推翻
2. 其余单元全部交给 AI（AI 是主判断者，守卫放行的未知类型可直达"建议删除"档）：按隐私档裁剪字段 → 指纹查缓存 → 批量 HTTP 请求（支持 `responses` 与 `chat_completions` 两种协议、429/5xx 重试、坏批次二分拆分）
3. 返回后：严格校验字段与数量 → **证据校验**（AI 必须原样引用输入事实，编造即降级）→ 决策表合成最终建议
4. 任何失败：统一降级为"人工确认"，不阻塞扫描

另有 `suggest_areas`：全盘分析的阶段一接口，让 AI 从目录聚合中挑出可疑区域，失败时调用方回退体积启发式。

`build_advisor` 是 CLI/GUI 共用的构造工厂；`advise_ai_only_many` 仅供评测脚本使用。

### 分析层

- `scanner.py`：纯计算层——按后缀/所在目录/大小多信号打分（**不含年龄**，事实全部来自快照库，不做文件系统遍历）→ 归并判定单元 → 单元经 `advise_units` 判断后展开回全部成员文件。`ScanPolicy` 是扫描参数默认值的唯一来源
- `inventory.py`：SQLite 快照库（`snapshots` / `files` / `dirs` 三表），`snapshot_tree` 记录扫描见到的每个文件与目录递归聚合（总大小、文件数、主要后缀），是 `analyze` 的唯一事实源
- `analyzer.py`：全盘区域分析（**MFT-only**，无目录遍历回退）。阶段一：体积 top 目录摘要 → AI 圈出可疑区域（无 AI 时回退体积启发式）；阶段二**递归下钻**：大区域（子目录 ≥6）在区域内再做一轮"摘要 → 圈子区域"，叶子目录的文件直接从快照库评分，每层散文件单独评审不漏扫；另从快照聚合出**同体积组**（疑似重复文件），AI 仅作提示性判读
- `report.py`：CSV 明细、明细 JSON、统计摘要 JSON、HTML 可视化四种产物；`write_all_reports` 是 CLI/GUI 共用的报告流水线

### 测试与外围脚本

- `tests/`：覆盖单元指纹稳定性、决策表真值表、证据校验、单元缓存、快照库聚合、区域分析（启发式 + AI mock）、AI 协议解析与缓存、`.env` 读写、安全规则、路径脱敏、扫描与 top-N 截断、报告生成
- `tools/test_ai_connection.py`：独立脚本，验证 AI 密钥、协议与结构化返回
- `evaluation/run_benchmark.py` + `benchmark.jsonl`：40 条标注数据，对比 `local_rules` / `pure_ai` / `hybrid` 三种方案的判定指标
- `demo/create_demo_files.py`：生成演示文件树

## 三、一次扫描的执行顺序

1. 入口校验前置条件（管理员 + AI 已配置），缺一拒绝
2. `mft_scanner.snapshot_volume` 直读 $MFT 写入 inventory（唯一快照来源）
3. 阶段一 `suggest_areas`（或体积启发式）圈区域 → `_drill_area` 递归下钻
4. 叶子与散文件的文件行从快照库取出 → `scanner._signals_from_facts` 评分 → 归并判定单元 → `advise_units`（本地守卫 → 缓存 → AI → 证据校验 → 决策表）
5. 汇总候选 + 同体积组提示 → `write_all_reports` 生成报告；工具不执行任何删除

## 四、相关文档

- [docs/code-map.md](docs/code-map.md) —— 符号级代码地图、并发模型与维护守则
- [docs/redundancy-report.md](docs/redundancy-report.md) —— 冗余代码清单与处置记录
- [docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md) —— 安装与 AI 配置
- [docs/BUILD_EXE.md](docs/BUILD_EXE.md) —— Windows 打包
