# 源码安装与 AI 配置指南（Windows）

本指南适用于不使用 EXE、直接从源码运行项目的情况。建议使用 Python 3.10–3.12。

## 方法一：一键创建环境

在项目根目录双击：

```text
setup_windows.bat
```

脚本会：

1. 检查 Python；
2. 创建 `.venv` 虚拟环境；
3. 安装 `requirements.txt`；
4. 在不存在 `.env` 时复制 `.env.example`；
5. 保留已有 `.env`，不会覆盖密钥。

完成后双击：

```text
launch_gui.bat
```

## 方法二：手动创建虚拟环境

在项目根目录打开 PowerShell：

```powershell
py -3 -m venv .venv
```

如果电脑没有 `py` 命令：

```powershell
python -m venv .venv
```

### 激活虚拟环境

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
& .\.venv\Scripts\Activate.ps1
```

成功后提示符前面会显示：

```text
(.venv)
```

安装依赖：

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

也可以不激活虚拟环境，直接运行它自己的 Python：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe gui.py
```

## 配置 AI

复制配置文件：

```powershell
Copy-Item .env.example .env
notepad .env
```

Responses API 示例：

```env
AI_API_KEY=你的密钥
AI_BASE_URL=https://服务商地址/v1
AI_MODEL=服务商提供的准确模型ID
AI_API_STYLE=responses
AI_TIMEOUT=60
AI_BATCH_SIZE=12
AI_MAX_RETRIES=3
AI_CACHE_PATH=.cache/ai_advice.sqlite3
AI_PRIVACY_MODE=balanced
```

Chat Completions 示例：

```env
AI_API_STYLE=chat_completions
```

注意：

- `AI_BASE_URL` 只填写到 `/v1`；
- 不要把 `/responses` 或 `/chat/completions` 写进地址；
- `.env` 不能提交到 GitHub；
- 密钥一旦出现在截图、聊天或提交记录中，应立即作废并重建。

## 测试 AI 连接

启动 GUI 后点击“测试 AI 连接”，或运行：

```powershell
python tools\test_ai_connection.py
```

测试成功后再勾选“启用 AI 建议”进行扫描。

## 创建演示文件

```powershell
python demo\create_demo_files.py
```

生成的 `demo/sample_disk/` 只用于本地演示，已被 `.gitignore` 排除，不应提交。

## 启动

```powershell
python gui.py
```

或双击：

```text
launch_gui.bat
```

## 常见问题

### 找不到 `Activate.ps1`

先确认虚拟环境是否真的创建成功：

```powershell
Test-Path .\.venv\Scripts\python.exe
Get-ChildItem .\.venv\Scripts
```

若显示 `False`，删除后重建：

```powershell
Remove-Item -Recurse -Force .venv -ErrorAction SilentlyContinue
py -3 -m venv .venv
```

### AI 显示 `local-fallback`

依次检查：

1. `.env` 是否位于项目根目录；
2. `AI_API_STYLE` 是否符合服务商文档；
3. 模型 ID 是否准确；
4. 点击“测试 AI 连接”查看具体 HTTP 错误；
5. 中转站是否限制 IP、User-Agent 或 Cloudflare 规则。
