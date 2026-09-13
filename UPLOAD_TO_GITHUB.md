# 上传到 GitHub

## 1. 上传前检查

在项目目录执行：

```powershell
python -m unittest discover -s tests -v
python demo\create_demo_files.py
python gui.py
```

确认以下内容没有被提交：

- `.env` 和 API Key；
- `.venv`、`build`、`dist`、`__pycache__`；
- `.cache/ai_advice.sqlite3`；
- `demo/sample_disk`；
- 包含真实路径的 CSV、JSON 和 HTML 报告。

## 2. 初始化仓库

```powershell
git init
git add .
git commit -m "feat: release AI Disk Assistant v1.2.1"
git branch -M main
git remote add origin https://github.com/你的用户名/AI-Disk-Assistant.git
git push -u origin main
```

仓库建议：

- Repository name：`AI-Disk-Assistant`
- Description：`Safety-first Windows disk analyzer with batched and explainable AI recommendations`
- Topics：`python`、`llm`、`windows`、`automation`、`tkinter`、`ai-safety`、`file-management`

## 3. 加入 GIF

按照 `docs/GIF_GUIDE.md` 录制后，把文件保存为：

```text
docs/assets/demo.gif
```

然后取消 README 顶部 GIF 行的注释，提交：

```powershell
git add README.md docs/assets/demo.gif
git commit -m "docs: add product demo gif"
git push
```

## 4. 生成 Windows EXE

GitHub 页面进入 `Actions` → `Windows Release` → `Run workflow`。完成后可在该次运行的 Artifacts 下载 CLI 和 GUI 两个 EXE。

正式发布时创建标签：

```powershell
git tag v1.2.1
git push origin v1.2.1
```

工作流会创建 Release 并上传 EXE。

## 5. 简历前的真实数据

配置自己的 AI 接口后运行：

```powershell
python evaluation\run_benchmark.py
```

只把真实生成的准确率、覆盖率和危险误判数写进简历，不要使用占位数字。

## 6. 哪些文件应该上传

应该上传：

- 全部 Python 源码、测试、文档和工作流；
- `.env.example`（只包含占位符）；
- `demo/create_demo_files.py` 与 `evaluation/benchmark.jsonl`；
- `docs/assets/demo.gif`；
- 有意制作并完成脱敏的示例截图或示例报告。

不要上传：

- `.env`、任何 API Key；
- `.venv/`、`build/`、`dist/`、`*.spec`；
- `.cache/`、SQLite 缓存；
- `demo/sample_disk/` 本地生成文件；
- `reports/` 中的真实扫描结果；
- 含真实用户名、项目名、公司名或私人目录的截图；
- EXE 到源码分支。EXE 应放在 GitHub Releases。

执行以下命令可以预览 Git 即将提交的文件：

```powershell
git status --short
git check-ignore -v .env .venv demo/sample_disk reports .cache dist
```
