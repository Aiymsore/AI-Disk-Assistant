"""解析 tests-output.txt，把每个失败测试文件的尾部输出合并成 ::error:: 注解。

CI 诊断工具：job 日志只有仓库管理员能读，而 ::error:: 注解走公开 API 可匿名读取。
每个文件合并为一条注解（换行用 %0A 转义），避开 GitHub 每步 10 条注解的上限。
"""

from pathlib import Path

LINES_PER_FILE = 12


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

    for file_name, lines in sections:
        picked: list[str] = []
        for index, line in enumerate(lines):
            if line.startswith(("FAIL:", "ERROR:")):
                picked.extend(lines[max(0, index - 2) : index + LINES_PER_FILE])
        if not picked:
            continue
        # 去重保序后合并为一条多行注解（%0A 转义换行，绕开每步 10 条注解的上限）
        body = "%0A".join(
            line.replace("%", "%25").replace("\r", "") for line in dict.fromkeys(picked)
        )
        print(f"::error title=[{file_name}]::{body}")


if __name__ == "__main__":
    main()
