"""解析 tests-output.txt，把每个含 FAIL/ERROR 的测试文件的末尾 traceback 转成 ::error:: 注解。

CI 诊断工具：job 日志只有仓库管理员能读，而 ::error:: 注解走公开 API 可匿名读取，
用于在无日志权限时定位 CI 独有的测试失败（如 Linux/3.10 环境差异）。
"""

from pathlib import Path

LINES_PER_FILE = 10


def main() -> None:
    output = Path("tests-output.txt")
    if not output.exists():
        print("::error::tests-output.txt 不存在")
        return

    text = output.read_text(encoding="utf-8", errors="replace")
    sections: list[tuple[str, list[str]]] = []
    current_file = "unknown"
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("=== tests/"):
            if current:
                sections.append((current_file, current))
            current_file = line.strip("= ")
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append((current_file, current))

    emitted = 0
    for file_name, lines in sections:
        if not any(line.startswith(("FAIL:", "ERROR:")) for line in lines):
            continue
        for line in lines[-LINES_PER_FILE:]:
            emitted += 1
            print(f"::error title=diag-{emitted} [{file_name}]:: {line}")
    print(f"::error::emitted {emitted} annotations for failing files")


if __name__ == "__main__":
    main()
