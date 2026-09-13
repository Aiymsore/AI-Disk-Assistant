# Changelog

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
