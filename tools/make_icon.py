"""从 assets/logo.png 生成多尺寸 assets/app.ico（Windows 可执行文件图标）。

构建期工具：需要 pillow（见 requirements-dev.txt），产物不提交（.gitignore 忽略）。
"""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> None:
    source = Image.open(ROOT / "assets" / "logo.png").convert("RGBA")
    target = ROOT / "assets" / "app.ico"
    source.save(target, format="ICO", sizes=SIZES)
    print(f"icon written: {target}")


if __name__ == "__main__":
    main()
