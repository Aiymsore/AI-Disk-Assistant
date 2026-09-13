# Windows EXE 构建与发布

项目建议同时提供两种使用方式：

- 普通用户：从 GitHub Releases 下载 GUI EXE；
- 开发者和招聘方：克隆源码、查看测试并自行运行。

EXE 不建议直接提交到 Git 仓库，因为二进制文件会增大仓库和历史记录。

## 本地构建

在 Windows 项目根目录双击：

```text
build_windows_exe.bat
```

脚本会创建或复用 `.venv`，安装 PyInstaller，然后生成：

```text
dist/AI-Disk-Assistant.exe
dist/AI-Disk-Assistant-GUI.exe
```

其中：

- `AI-Disk-Assistant-GUI.exe`：简历展示和普通用户使用的主要版本；
- `AI-Disk-Assistant.exe`：命令行版本，适合批处理和技术演示。

发布前应在一台没有 Python 环境的 Windows 电脑或 Windows Sandbox 中测试：

1. GUI 是否启动；
2. 未配置 `.env` 时本地规则是否正常；
3. `.env` 放在 EXE 同目录后 AI 连接是否正常；
4. Demo、扫描、报告导出是否正常；
5. 杀毒软件是否误报。

## 使用 GitHub Actions 构建

仓库已包含：

```text
.github/workflows/windows-release.yml
```

上传源码后：

1. 打开 GitHub 仓库的 **Actions**；
2. 选择 **Windows Release**；
3. 点击 **Run workflow**；
4. 构建完成后下载 Artifact。

正式发布：

```powershell
git tag v1.2.1
git push origin v1.2.1
```

工作流会创建 Release 并上传 EXE 与发布压缩包。

## Release 建议内容

Release 页面建议包含：

- GUI EXE；
- CLI EXE；
- 包含 `.env.example`、README 和 LICENSE 的发布 ZIP；
- 版本更新说明；
- SHA-256 校验值（可选）；
- “不会上传文件正文，只发送脱敏元数据”的隐私说明。

不要把自己的 `.env`、API Key、缓存数据库或真实扫描报告放入发布包。
