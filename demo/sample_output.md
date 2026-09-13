# Demo output

Run locally:

```powershell
python demo/create_demo_files.py
python main.py scan demo/sample_disk --no-ai --output reports/demo.csv --html reports/demo.html
```

Expected behavior:

- `.tmp`、`.log` 和 `.dmp` 文件位于明确的缓存/日志目录时，可由本地规则给出“建议删除”；
- 无后缀缓存、`preview.cache` 等模糊对象会进入“谨慎删除”，配置 AI 后可以展示语义建议；
- `coursework.docx`、`project_backup.7z` 和安装程序不会自动进入清理列表；
- 扫描会生成 CSV 明细、JSON 统计摘要和 HTML 可视化报告；
- 第二次 AI 扫描可展示 `hybrid-cache`，用于证明缓存减少了重复接口请求。
