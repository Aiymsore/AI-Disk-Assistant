# AI Disk Assistant · 代码解析

本项目扫描磁盘目录，由本地安全规则与 AI 建议共同筛选可清理的候选文件，生成报告，并可在二次确认后移入系统回收站。

本文只说明各部分代码负责什么功能。符号级行号索引见 [docs/code-map.md](docs/code-map.md)，冗余清理记录见 [docs/redundancy-report.md](docs/redundancy-report.md)。

## 一、分层总览

```text
入口层      main.py / gui.py（根目录启动脚本）
            ai_disk_assistant/cli.py / ai_disk_assistant/gui.py
业务层      scanner.py    cleaner.py    report.py
AI 决策层   ai_advisor.py（+ cache.py）
支撑层      metadata.py   privacy.py    config.py
安全层      safety.py
数据层      models.py
```

依赖方向自上而下，无循环依赖。

## 二、各部分功能

### 入口层

| 文件 | 功能 |
|---|---|
| `main.py` | CLI 启动入口，转发到包内 `cli.main` |
| `gui.py`（根目录） | GUI 启动入口，转发到包内 `gui.main` |
| `ai_disk_assistant/cli.py` | 定义 `inspect`（判断单个文件）与 `scan`（扫描目录并出报告）两个子命令；负责参数解析、控制台输出与流程编排 |
| `ai_disk_assistant/gui.py` | Tkinter 界面，分三部分：`AIConfigDialog` 图形编辑 `.env` 配置；主窗口控件（目录选择、候选列表、回收站按钮）；扫描/AI 测试/回收站的后台线程与事件队列轮询刷新 |

### 数据模型层

`models.py` —— 全项目共享的数据结构基座：

- `FileMetadata`：单个文件的 stat 快照（路径、大小、修改/访问时间、设备号、inode）
- `Advice`：一条清理建议（是否可删、用途、建议等级、理由、来源）
- `Candidate` / `ScanStats` / `ScanResult`：候选、扫描统计与总结果
- `PURPOSES` / `ADVICE_LEVELS`：用途与建议等级的合法值域，AI 返回结果按此校验
- `Advice.__post_init__` + `safe_fallback_advice`：所有建议的统一兜底约束与安全默认值工厂

### 安全规则层

`safety.py` —— 不依赖网络与 AI 的安全底线：

- 常量区：受保护目录名 + 全部后缀集合（全项目唯一定义处；"自动删除白名单"与"扫描打分信号"两组取值不同是有意设计，见行内注释）
- `local_safety_guard`：按后缀与所在目录给出本地判断，`source="local-guard"` 的结论 AI 无权推翻
- `is_auto_delete_eligible` / `can_move_to_trash`：自动删除资格判定与移入回收站的前置检查

### 支撑层

- `metadata.py`：文件大小格式化；采集文件元数据快照
- `privacy.py`：把发送给 AI 的字段按 `strict`（不发路径）/ `balanced`（匿名化用户名）/ `full`（原路径）三档裁剪
- `config.py`：手写 `.env` 解析器（保留注释与未知行）、环境变量加载与增量保存、`Settings` 配置类；`DEFAULT_CACHE_PATH` / `DEFAULT_USER_AGENT` 等默认值的唯一定义处
- `cache.py`：SQLite 建议缓存，key 由模型、隐私模式与文件快照哈希构成，相同文件不重复请求 AI

### AI 决策层

`ai_advisor.py` —— `HybridAdvisor` 混合顾问，生产判断的主入口：

1. 本地守卫先行：`local_safety_guard` 能定论的直接采用
2. 需要 AI 时：按隐私档裁剪字段 → 查缓存 → 批量 HTTP 请求（支持 `responses` 与 `chat_completions` 两种协议、429/5xx 重试、坏批次二分拆分）
3. 返回后：严格校验字段与数量 → 混合守卫（AI 建议删除但本地条件不满足时降级为"谨慎删除"）
4. 任何失败：统一降级为"人工确认"，不阻塞扫描

`build_advisor` 是 CLI/GUI 共用的构造工厂；`advise_ai_only_many` 仅供评测脚本使用。

### 业务层

- `scanner.py`：`os.walk` 完整遍历（跳过受保护目录与符号链接）→ 按后缀/所在目录/文件年龄/大小多信号打分 → 小顶堆保留 top-N → 对前 `ai_limit` 个候选调用 AI 判断。`ScanPolicy` 是扫描参数默认值的唯一来源，CLI 与 GUI 都引用它
- `cleaner.py`：筛选"建议删除"项；移入回收站前重新核对大小、修改时间与文件标识（防止扫描后文件被替换）；实际移动使用 Send2Trash，不做永久删除
- `report.py`：CSV 明细、明细 JSON、统计摘要 JSON、HTML 可视化四种产物；`write_all_reports` 是 CLI/GUI 共用的报告流水线

### 测试与外围脚本

- `tests/`：27 个用例，覆盖 AI 协议解析与缓存、`.env` 读写、安全规则、路径脱敏、扫描与 top-N 截断、报告生成、回收站状态复核
- `tools/test_ai_connection.py`：独立脚本，验证 AI 密钥、协议与结构化返回
- `evaluation/run_benchmark.py` + `benchmark.jsonl`：40 条标注数据，对比 `local_rules` / `pure_ai` / `hybrid` 三种方案的判定指标
- `demo/create_demo_files.py`：生成带旧时间戳的演示文件树

## 三、一次扫描的执行顺序

1. `cli.command_scan`（或 `gui._scan_worker`）读取配置并经 `build_advisor` 构造顾问
2. `scanner.scan_with_stats` 遍历目录、打分、保留 top-N、采集元数据快照
3. 前 `ai_limit` 个候选进入 `HybridAdvisor.advise_many`：本地守卫 → 缓存 → AI → 校验 → 降级
4. 超出 AI 限额的候选标记"人工确认"
5. `write_all_reports` 生成四种报告
6. 可选：用户确认后 `cleaner.move_to_trash` 复核状态并移入回收站

## 四、相关文档

- [docs/code-map.md](docs/code-map.md) —— 符号级代码地图、并发模型与维护守则
- [docs/redundancy-report.md](docs/redundancy-report.md) —— 冗余代码清单与处置记录
- [docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md) —— 安装与 AI 配置
- [docs/BUILD_EXE.md](docs/BUILD_EXE.md) —— Windows 打包
