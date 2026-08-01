# GitHub 演示 GIF 录制指南

## 最推荐的录制内容

控制在 15～25 秒，画面只展示四步：

1. 双击 `demo/prepare_demo.bat` 打开 GUI；
2. 点击“开始扫描”；
3. 展示用途、建议等级、判断来源和理由；
4. 点击“打开最新 HTML 报告”，展示统计卡片与分布图。

不要在 GIF 中展示 API Key、`.env`、真实用户名、私人目录或实际删除操作。

## 方法一：ScreenToGif

这是 Windows 下最省事的方式。

1. 打开 ScreenToGif，选择“录像机”；
2. 把录制框缩到程序窗口大小，建议约 `1100 × 700`；
3. 帧率设为 `10～15 FPS`；
4. 开始录制后执行上面的四步；
5. 停止录制，删除开头和结尾的无操作帧；
6. 导出为 GIF，宽度建议 `900～1100 px`；
7. 使用有损压缩或减少颜色，把文件控制在约 `5～10 MB`。

保存位置：

```text
docs/assets/demo.gif
```

然后在 `README.md` 顶部取消这一行的注释：

```markdown
![AI Disk Assistant Demo](docs/assets/demo.gif)
```

## 方法二：ShareX

1. 选择“捕获”→“屏幕录制 GIF”；
2. 框选 GUI 窗口；
3. 扫描 Demo 并打开 HTML 报告；
4. 停止后把 GIF 保存到 `docs/assets/demo.gif`。

ShareX 操作快，但后期剪辑能力不如 ScreenToGif。

## 方法三：先录 MP4 再转 GIF

画质要求高时，可用 Xbox Game Bar 或 OBS 录制 MP4，然后用 ffmpeg 转换：

```powershell
ffmpeg -i demo.mp4 -vf "fps=12,scale=1000:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer" -loop 0 docs\assets\demo.gif
```

## 录制前准备

```powershell
python -m venv .venv
.\.venv\Scripts\activate
python -m pip install -r requirements.txt
python demo\create_demo_files.py
python gui.py
```

为了突出 AI：

- 已配置 API 时，保持“启用 AI 建议”勾选，判断来源会出现 `hybrid-ai` 或 `hybrid-cache`；
- 未配置 API 时，可先录制本地安全流程，但 README 中不要宣称画面展示了真实 AI 调用；
- 第一次扫描会调用 AI，第二次扫描通常会展示 `hybrid-cache`，这正好可以演示缓存机制。

## GIF 画面建议

- Windows 显示缩放调到 100% 或 125%；
- 隐藏桌面通知和任务栏敏感信息；
- 使用 `demo/sample_disk`，不要扫描真实 C 盘；
- 鼠标移动慢一点，避免画面难以理解；
- 最后停留 2 秒展示 HTML 报告；
- GitHub 首页 GIF 只展示流程，详细参数放在正文。
