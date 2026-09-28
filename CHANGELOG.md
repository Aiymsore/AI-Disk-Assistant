# Changelog

## 1.4.1 - 2026-09-28

- 修复手动删除"卡死半小时"且无反馈的问题：`SHFileOperationW` 此前 `hwnd=None` 且 `FOF_SILENT`，文件超回收站容量时系统弹出的"是否永久删除"确认框可能落在 GUI 主窗口背后无人点击，后台线程随之无限阻塞，界面上看不到任何进度。现在：① 弹窗父化到应用顶层窗口（`recycle_paths(hwnd=…)`，句柄在主线程取得）；② `FOF_SILENT` 改为 `FOF_SIMPLEPROGRESS`，长任务显示系统进度框；③ 新增 `split_permanent`，读注册表 `BitBucket\Volume\{卷}` 的 `MaxCapacity`（单位 MB，缺项按卷容量 5% 兜底）与 `NukeOnDelete`，把"超容量将被永久删除"的目标提前写进二次确认框，用户确认后才设 `FOF_NOCONFIRMATION` 压掉系统重复确认（未预告时仍保留系统确认作最后防线）；④ 删除按分块回调状态栏"正在删除 i/n 个文件"。

## 1.4.0 - 2026-09-28

- **GUI 评审全面改为 AI 文件介绍**：按用户方向移除评审流程中的本地守卫（local-guard）与删除建议管线（评分→判定单元→证据闸门→决策表），新增 `describe_items` 展示性任务——对选中的文件/目录输出"是什么 / 删除影响 / 处理建议（可清理/需核对/别动）"，界面列表列与着色相应更新，"只看建议清理"开关改为"只看可清理"（默认全部显示）。介绍按单条载荷+提示词版本缓存（文本缓存通道），坏批次仍二分降级；失败弹窗呈现。CLI 的 `analyze`/`inspect` 暂保留旧管线；cleaner 的执行级保护（受保护目录拒绝移入回收站）不受影响。
- 修复 AI 判读大面积失败兜底"人工确认"的严重问题：证据长度上限 80 装不下深层目录的 `path_pattern` 真实取值（90+ 字符），AI 如实引用长路径证据被当作编造整批拒绝，二分重试全败后所有候选兜底成"人工确认"（实测一次评审 8 次请求全废）。上限放宽到 200，提示词引导优先引用单值字段、不引用 `sample_names` 列表字段（`unit-prompt-5`），并加回归测试。
- 修复 GUI 每次关闭后弹出 `Failed to remove temporary directory`（`%TEMP%\_MEI…`）警告的问题：单文件 EXE 退出时 PyInstaller 引导器要删除解包临时目录，若后台线程（扫描循环/AI 请求重试）仍在运行，句柄未释放导致清理失败并弹窗。现在关窗与提权重启统一走 `_on_close`：先取消扫描、再以 `os._exit(0)` 立即终止进程，句柄随进程释放，临时目录必能清掉。
- 新增评审结果**折叠开关**：底部候选列表默认只显示"建议删除/谨慎删除"两档（"只看建议清理"开关，可随时切换），大量"人工确认"不再刷屏；折叠时显示"显示 X / 共 N 条（已折叠 M 条，含 K 条人工确认）"，数据完整保留、报告不裁剪。
- **AI 提示词全面扩写**：五个提示词（判定单元/逐文件/区域圈选/重复组/深度分析）补齐"裁判手册"——目录上下文×后缀×数量档的判据参考、四级建议的分级标尺（建议删除须多信号互相印证；仅凭体积大/数量多不得建议删除；.db/.dat 等个人数据倾向人工确认）、reason 语录规范（定性+关键依据+核对点，附分等级范例，禁止套话）、匿名化路径与 mtime 不可靠的显式说明；深度分析综述新增第 8 条语气要求并可建议使用本工具的回收站删除。缓存版本递增（`unit-prompt-4` / `overview-prompt-2`），旧判定自动失效重判。
- 修复逐文件建议缓存键缺失提示词版本的问题：`_prompt_version` 现已进入缓存键，与单元管线同获"提示词升级即重判"的保证。
- 清理第一版配置残留：`.env` 中遗留的 `AI_USER_AGENT=AI-Disk-Assistant/1.3.0` 已移除（默认值随程序版本自动更新），`.env.example` 同步更名为 P4Disk4P。配置读取自程序所在目录的 `.env` 文件，程序内无旧版硬编码。
- 新增提示词枚举同步测试：PURPOSES/ADVICE_LEVELS 与两个结构化提示词的枚举清单、以及证据示例字段名的真实性均有测试守护。
- 新增 GUI **手动删除**：文件列表行首新增勾选方框（点击切换，点"全选"表头对当前列表全选/反选），顶栏扫描行新增「删除勾选」按钮——二次确认（文件数/体积/被拒目标/示例路径）后由新增 `cleaner.py` 执行：`plan_deletion` 按快照库展开目录子树、去重并拒绝受保护目录与快照外路径；`recycle_paths` 在 Windows 上经 `SHFileOperationW(FO_DELETE + FOF_ALLOWUNDO)` 移入回收站（可还原，不设 FOF_NOCONFIRMATION 以防超大文件被静默永久删除），删除后按磁盘事实回删快照行、清空目录壳并刷新聚合与当前视图。自动流程（扫描/评审/报告/综述）依旧不触碰文件系统。
- 新增 **AI 深度分析综述**：把整盘/整目录的聚合事实（子目录排名、深层热点、后缀构成、最大文件、重复组、本次评审分布）一次性交给 AI，输出自由 Markdown 长文——空间去向、重点区域点评、风险与不确定项、建议处理顺序。
- 新增 `overview.py`（事实聚合层，按 strict/balanced/full 裁剪路径）与 `HybridAdvisor.summarize_overview`（唯一不返回 JSON 的 AI 任务；显式 `AI_OVERVIEW_MAX_TOKENS` 输出预算；缓存键含事实内容哈希，盘面未变则零成本复用）。
- 新增 GUI「AI 深度分析」按钮与综述阅读窗口（标题分层显示、复制全文、打开 Markdown），CLI `analyze` 打印综述并支持 `--overview-md`。
- 报告体系增加第五种产物：综述 Markdown，并入 HTML 报告顶部（零依赖 Markdown 子集渲染，先转义后套标签）。
- 新增 `cache.py` 文本缓存表与 `inventory.py` 的范围查询（`subtree_usage` / `largest_files` / `top_dirs(under=…)` / `child_dirs(limit=…)`）。
- CLI 打印自由文本时做编码兜底，避免 GBK 控制台因模型输出的非 GBK 字符中断流程。

## v1.2.1

- 修正 Windows PowerShell 虚拟环境激活命令。
- 增加 `setup_windows.bat` 一键创建环境与安装依赖。
- 增加 `build_windows_exe.bat` 本地构建 GUI/CLI EXE。
- 增加完整源码安装、AI 配置和 EXE 发布文档。
- 明确运行时文件、Demo 文件、报告、密钥和 EXE 的 GitHub 发布边界。

## 1.2.0 - 2026-08-02

- Added `AI_API_STYLE=responses` support for providers using `POST /v1/responses`.
- Retained `chat_completions` compatibility for existing OpenAI-style gateways.
- Added parsing for Responses API `output[].content[].text` and top-level `output_text`.
- Added Responses API input/output token accounting and provider-aware cache keys.
- Added configurable `User-Agent` and clearer protocol-specific HTTP errors.
- Added a GUI **Test AI Connection** button and a CLI connection diagnostic script.
- Included the active API protocol in GUI status and HTML reports.
- Added tests for both API protocols and configuration aliases.

## 1.1.0 - 2026-08-02

- Added a Tkinter desktop interface and HTML analytics report.
- Replaced early candidate cutoff with full traversal and bounded Top-N selection.
- Added file snapshot fields and pre-trash TOCTOU verification.
- Added strict, balanced and full AI privacy modes.
- Added batched AI requests, SQLite caching, retries, backoff, token statistics and failed-batch splitting.
- Added strict AI response validation.
- Added a 40-record benchmark for local, pure-AI advisory and hybrid-safe comparison.
- Expanded the suite from 7 to 19 tests with more than 80% measured coverage.
- Added multi-platform CI and automatic Windows CLI/GUI executable builds.

## 1.0.0 - 2026-08-02

- Reorganized the original two-script prototype into a reusable package.
- Replaced the hard-coded third-party API endpoint with environment-based configuration.
- Added a local safety guard that AI cannot bypass.
- Disabled whole-folder deletion in the public version.
- Added CSV / JSON reports, demo data, tests, documentation, and packaging metadata.
