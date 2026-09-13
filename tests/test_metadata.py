from __future__ import annotations

import unittest

from ai_disk_assistant.metadata import format_mtime, format_pct


class FormatMtimeTests(unittest.TestCase):
    def test_normal_and_zero_values(self) -> None:
        self.assertEqual(format_mtime(0), "—")
        # 2020-01-01 UTC 前后允许时区偏差
        self.assertIn("2020-01-01", format_mtime(1_577_836_800_000_000_000))

    def test_extreme_garbage_values_do_not_crash(self) -> None:
        # Windows 的 time.localtime 对负时间戳抛 OSError：一条脏数据不能中断整个列表渲染。
        for garbage in (-(2**63), 2**63 - 1, 10**30, -10**30):
            self.assertIsInstance(format_mtime(garbage), str)


class FormatPctTests(unittest.TestCase):
    def test_keeps_small_but_meaningful_shares(self) -> None:
        # 2.5 GB / 262 GB ≈ 1%：地板除会显示 0%，一位小数保留有效信息。
        self.assertEqual(format_pct(2_500_000_000, 262_000_000_000), "1.0%")
        self.assertEqual(format_pct(240, 600), "40.0%")

    def test_edge_cases(self) -> None:
        self.assertEqual(format_pct(0, 600), "0.0%")
        self.assertEqual(format_pct(600, 0), "0.0%")


if __name__ == "__main__":
    unittest.main()
