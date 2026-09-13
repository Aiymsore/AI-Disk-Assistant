# 代码地图（Code Map）

> 本文档描述重构后的代码结构。行号以当前版本为准，大改动后请同步更新。

## 一、程序入口

| 入口 | 文件 | 说明 |
|---|---|---|
| CLI | `main.py` → `ai_disk_assistant/cli.py:165 main()` | pyproject 注册为 `ai-disk-assistant` |
| GUI | `gui.py` → `ai_disk_assistant/gui.py:600 main()` | pyproject 注册为 `ai-disk-assistant-gui` |
| 版本号唯一来源 | `ai_disk_assistant/__init__.py:5` | pyproject.toml / GUI 标题 / User-Agent 均由此派生 |

辅助脚本：`tools/test_ai_connection.py`（AI 连通性）、`evaluation/run_benchmark.py`（三方案评测）、`demo/create_demo_files.py`（演示数据）。

## 二、分层架构（自底向上，无循环依赖）

```
                 main.py            gui.py（根目录启动脚本）
                    │                  │
                 cli.py ◄────────── gui.py          ← 入口层：参数解析 / 界面 / 后台线程
                    │                  │
      ┌─────────────┼──────────┬───────┴────┐
   scanner.py   cleaner.py   report.py   ai_advisor.py   ← 业务层
      │             │            │       ├─ cache.py
      │             │            │       ├─ config.py ←── 配置层（.env / Settings）
      │             │            │       └─ privacy.py
   metadata.py      │            │             │
      └─────────────┴────┬───────┴─────────────┘
                    safety.py                 ← 安全底线层
                         │
                     models.py                 ← 数据模型层（零依赖基座）
```

功能分类：

| 分类 | 模块 | 一句话职责 |
|---|---|---|
| 数据模型层 | `models.py` | 数据结构与安全默认值（Advice 兜底约束、safe_fallback_advice 工厂） |
| 安全底线层 | `safety.py` | 受保护目录、后缀集合（唯一定义处）、本地规则守卫、删除前置检查 |
| 元数据/隐私/配置 | `metadata.py` / `privacy.py` / `config.py` | stat 快照采集；AI 请求字段裁剪；.env 读写与 Settings |
| AI 决策层 | `ai_advisor.py` + `cache.py` | 混合决策：本地守卫优先，AI 批量判断 + 缓存 + 失败降级 |
| 业务层 | `scanner.py` / `cleaner.py` / `report.py` | 扫描打分；回收站清理；四种报告产物 |
| 入口层 | `cli.py` / `gui.py` | 两条交互入口，共用同一套业务层与公共函数 |

## 三、一次扫描的完整数据流

```
cli.command_scan / gui._scan_worker
  → scanner.scan_with_stats (scanner.py:133)
      1. _iter_files 遍历目录（跳过受保护目录、符号链接）
      2. _candidate_signals 多信号打分（后缀/安全上下文/年龄/大小）
      3. _retain_top_candidate 小顶堆保留 top max_candidates
      4. get_file_metadata 采集最终候选的 stat 快照
      5. advisor.advise_many 前 ai_limit 个候选
          → safety.local_safety_guard   本地守卫（local-guard 结论 AI 无权推翻）
          → privacy.metadata_for_ai     按隐私档裁剪字段
          → AdviceCache.get/set         建议缓存
          → _request_ai_batch_resilient HTTP 批量请求（重试 + 二分降级）
          → _validate_advice            严格校验 AI 返回
          → _apply_hybrid_guard         混合守卫降级
      6. 超出 AI 限额的候选 → safe_fallback_advice（人工确认）
  → write_all_reports (report.py:145)：CSV + summary JSON + HTML（明细 JSON 可选）
  → 可选：cleaner.move_to_trash（先 can_move_to_trash + verify_file_unchanged 复核）
```

## 四、模块职责与关键位置

### models.py（146 行）— 数据模型层
| 符号 | 行号 | 说明 |
|---|---|---|
| `PURPOSES` / `ADVICE_LEVELS` | 16 / 30 | AI 输出校验值域；SYSTEM_PROMPT 中的枚举须与其同步 |
| `REASON_MAX_LENGTH` | 35 | 理由长度上限（AI 校验抛错 / 本地兜底截断，两种策略均有意） |
| `FileMetadata` | 39 | stat 快照；`snapshot()` 供 TOCTOU 复核 |
| `Advice.__post_init__` | 76 | 全体 Advice 的兜底约束（非法值回落、非"建议删除"不自动删） |
| `safe_fallback_advice` | 89 | 拿不准时的唯一安全默认值工厂 |
| `Candidate` / `ScanStats` / `ScanResult` | 104 / 131 / 144 | 结果载体 |

### safety.py（192 行）— 安全底线层
| 符号 | 行号 | 说明 |
|---|---|---|
| `PROTECTED_DIR_NAMES` | 16 | 禁止扫描/删除的系统目录名 |
| 全部后缀集合 | 30-97 | **全项目唯一定义处**；`AUTO_DELETE_*`（白名单，最保守）与 `SIGNAL_*`（打分信号，可放宽）的有意差异见行内注释 |
| `local_safety_guard` | 112 | 本地规则判定；返回 `source="local-guard"` 的结论 AI 无权推翻 |
| `is_auto_delete_eligible` | 174 | 自动删除白名单判定 |
| `can_move_to_trash` | 181 | 移入回收站的前置检查 |

### metadata.py（43 行）/ privacy.py（82 行）/ config.py（192 行）
| 符号 | 位置 | 说明 |
|---|---|---|
| `format_size` / `get_file_metadata` | metadata.py:11 / 21 | 大小格式化、stat 快照采集 |
| `metadata_for_ai` | privacy.py:59 | strict/balanced/full 三档裁剪发给 AI 的字段 |
| `DEFAULT_CACHE_PATH` / `DEFAULT_USER_AGENT` | config.py:20-21 | 默认值唯一来源，GUI 配置对话框亦从此导入 |
| `read_dotenv` / `update_dotenv` | config.py:75 / 105 | 手写 .env 解析（保留注释与未知行） |
| `Settings.from_env` | config.py:178 | 配置读取唯一入口（经 `build_advisor`） |

### cache.py（81 行）— SQLite 建议缓存
`AdviceCache`（cache.py:19）：`make_key`（47）= 模型+隐私模式+快照 payload 的 sha256；`get`（56）/ `set`（69）。仅 ai_advisor.py 使用。

### ai_advisor.py（507 行）— AI 决策层
| 分节 | 行号 | 说明 |
|---|---|---|
| `SYSTEM_PROMPT` | 34 | 枚举清单须与 models.PURPOSES/ADVICE_LEVELS 同步 |
| `AdvisorStats` | 63 | token/耗时仅落盘 summary JSON，不在界面展示（有意保留） |
| `_extract_json` / `_validate_advice` | 80 / 104 | 容错提取 JSON；严格校验字段与值域 |
| `_chat_content_to_text` / `_responses_content_to_text` | 143 / 160 | 两种 API 协议的文本抽取（共用 `_text_part_to_str`） |
| `HybridAdvisor.advise_many` | 236 | 生产入口：本地守卫优先 → AI 批量 → 失败降级 |
| `advise_ai_only_many` | 275 | **仅评测用**（run_benchmark.py），生产路径不走 |
| `probe` | 285 | 用一个无害样本验证连通性与结构化输出 |
| `_fallback_advice` / `_apply_hybrid_guard` | 301 / 314 | AI 失败降级；AI 建议删除时的双重本地闸门 |
| `_request_ai_batch_resilient` | 373 | 坏样本二分拆批，避免整批报废 |
| `_request_ai_batch` | 411 | HTTP + 重试（429/5xx 指数退避）+ id 完整性校验 |
| `build_advisor` | 502 | CLI/GUI 共用的构造工厂（读 .env → Settings → HybridAdvisor） |

### scanner.py（211 行）— 扫描层
`ScanPolicy`（32，**CLI/GUI 默认值唯一来源**）、`_iter_files`（42）、`_candidate_signals`（57，多信号打分）、`_retain_top_candidate`（116，小顶堆）、`scan_with_stats`（133，主流程）。

### cleaner.py（77 行）— 清理层
`select_auto_candidates`（20）、`verify_file_unchanged`（28，TOCTOU 复核：大小/mtime/设备号/inode）、`move_to_trash`（54）。

### report.py（246 行）— 报告层
`FIELDNAMES`（22）、`write_csv`（50）/ `write_json`（61）、`build_summary`（70）、`write_all_reports`（145，**CLI/GUI 共用流水线**）、`write_html_report`（183）。

### cli.py（176 行）— 命令行入口
`_build_parser`（22，默认值引用 `_DEFAULT_POLICY`）、`_advisor`（67）、`command_inspect`（84）、`command_scan`（91，扫描→打印→报告→可选 `--trash-auto`）。

### gui.py（607 行）— 图形界面
| 分节 | 位置 | 说明 |
|---|---|---|
| `GUI_MAX_CANDIDATES` | 51 | 与 CLI 5000 不同是**有意设计**（Tkinter 树刷新性能），见行内注释 |
| `AIConfigDialog` | 65-220 | .env 图形编辑器 |
| 主窗口构建 | 246-343 | 控件与布局 |
| 后台任务 | 393-417 | `_ai_test_worker` / `_scan_worker`（事件队列回传） |
| 选择与回收站 | 419-501 | 多选筛选、TRASH 口令二次确认、移动后刷新报告 |
| `_poll_events` | 502 | 主线程每 100ms 消费后台事件，驱动全部 UI 更新 |

## 五、并发模型（gui.py）

所有耗时操作（扫描 / AI 测试 / 移回收站）在 daemon 线程执行，通过 `queue.Queue` 投递
`("事件名", 载荷)` 元组；主线程 `_poll_events` 以 100ms 间隔轮询并分发。UI 线程绝不直接执行扫描。

## 六、维护守则（改代码前先读）

1. **后缀集合只在 safety.py 加/改**：先分清该后缀属于"自动删除白名单"还是仅"打分信号"。
2. **兜底 Advice 只用 `safe_fallback_advice`**，不要手写 `Advice(False, "未知用途", ...)`。
3. **改 `PURPOSES`/`ADVICE_LEVELS` 必须同步 SYSTEM_PROMPT**（ai_advisor.py:34）。
4. **报告产出一律走 `write_all_reports`**，不要再手写四个 write 调用。
5. **改默认扫描参数只改 `ScanPolicy`**，CLI/GUI 自动跟随（GUI 候选上限除外，见 gui.py:51）。
6. **版本号只改 `__init__.py` 的 `__version__`**，并同步 pyproject.toml（已加注释提醒）。
7. 动过 cleaner/scanner 后运行 `py -m pytest tests/ -q`（27 个测试，约 1 秒）。
