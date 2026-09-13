from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from ai_disk_assistant.ai_advisor import HybridAdvisor
from ai_disk_assistant.analyzer import analyze_root
from ai_disk_assistant.config import Settings
from ai_disk_assistant.inventory import Inventory
from tests.test_inventory import seed_snapshot


def no_ai_advisor() -> HybridAdvisor:
    return HybridAdvisor(Settings(None, "https://example.invalid/v1", "demo", 1), enable_ai=False)


class AnalyzerSmokeTests(unittest.TestCase):
    """analyze 全链路（注入式快照）：圈区域 → 递归下钻 → 评分 → 同体积组。"""

    def setUp(self) -> None:
        self._stack = ExitStack()
        self.temp_dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        inv_dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self.inventory_path = inv_dir / "inv.sqlite3"
        # 布局：big(8 个子目录 + 1 个散文件) 触发下钻；other 小区域直接评分；
        # other 下两个 1MB+ 同体积文件触发重复组。
        self.files = [
            (f"big/sub{index}/junk.log", 1024 * (index + 1)) for index in range(8)
        ]
        self.files += [
            ("big/loose_old.tmp", 512),
            ("other/note.txt", 4096),
            ("other/setup1.msi", 1024 * 1024 + 11),
            ("other/setup2.msi", 1024 * 1024 + 11),
        ]
        self.dirs = ["big"] + [f"big/sub{index}" for index in range(8)] + ["other"]
        self.addCleanup(self._stack.close)

    def _seed(self, inventory: Inventory) -> int:
        return seed_snapshot(inventory, self.temp_dir, self.files, self.dirs)

    def test_drill_scores_leaves_and_loose_files(self) -> None:
        def fake_snapshot(drive, inventory, progress=None, **_kwargs):
            snapshot_id = self._seed(inventory)
            return snapshot_id, len(self.files)

        with patch("ai_disk_assistant.analyzer.snapshot_volume", side_effect=fake_snapshot):
            result = analyze_root(
                self.temp_dir,
                no_ai_advisor(),
                area_limit=2,
                drill_depth=2,
                inventory_path=self.inventory_path,
            )
        # 启发式贪心圈选：other 体积最大先圈，big 次之；big 子目录多 → 下钻，
        # 叶子（体积前 2 的 sub7/sub6）才是扫描目标，散文件单独评审不漏扫。
        scanned = {area.path.rsplit("\\", 1)[-1] for area in result.areas}
        self.assertEqual(scanned, {"sub6", "sub7", "other"})
        # big 被下钻：叶子是体积前 2 的子目录（sub7/sub6），散文件单独评审不漏扫。
        candidate_paths = [item.metadata.path for item in result.candidates]
        self.assertTrue(any("loose_old.tmp" in path for path in candidate_paths))
        self.assertTrue(any("sub7" in path for path in candidate_paths))
        self.assertTrue(any("sub6" in path for path in candidate_paths))
        self.assertFalse(any("sub0" in path for path in candidate_paths))
        # 无 AI 时：junk 后缀 + %TEMP% 缓存上下文 → 本地直判"建议删除"（local-rule）；
        # .msi 是可执行/安装包类 → 人工确认。
        for item in result.candidates:
            if item.metadata.suffix in {".log", ".tmp"}:
                self.assertEqual(item.advice.advice_level, "建议删除")
            else:
                self.assertEqual(item.advice.advice_level, "人工确认")
        self.assertEqual(result.snapshot_file_count, len(self.files))
        # 同体积组（other 下两个 1MB+ 文件）。
        self.assertEqual(len(result.duplicates), 1)
        self.assertEqual(result.duplicates[0].count, 2)
        self.assertIsNone(result.duplicates[0].verdict)

    def test_duplicates_get_ai_verdict(self) -> None:
        advisor = HybridAdvisor(
            Settings("key", "https://example.invalid/v1", "demo", 1, ai_cache_path=str(
                Path(self.inventory_path).parent / "c.sqlite3"
            ))
        )
        areas_body = {"areas": [{"id": 0, "reason": "体积最大"}]}
        reviews_body = {"reviews": [{"id": 0, "verdict": "likely", "reason": "同名副本散落"}]}

        def fake_response(body):
            content = json.dumps(body, ensure_ascii=False)

            class _Resp:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self):
                    return json.dumps(
                        {"choices": [{"message": {"content": content}}]}, ensure_ascii=False
                    ).encode("utf-8")

            return _Resp()

        def fake_snapshot(drive, inventory, progress=None, **_kwargs):
            snapshot_id = self._seed(inventory)
            return snapshot_id, len(self.files)

        with patch(
            "ai_disk_assistant.ai_advisor.urllib.request.urlopen",
            side_effect=[fake_response(areas_body), fake_response(reviews_body)],
        ), patch("ai_disk_assistant.analyzer.snapshot_volume", side_effect=fake_snapshot):
            result = analyze_root(
                self.temp_dir,
                advisor,
                area_limit=1,
                inventory_path=self.inventory_path,
            )
        # 阶段一圈区域 1 次 + 重复组判读 1 次（big 下钻用启发式？不——AI 可用时下钻也调
        # suggest_areas，但 area_limit=1 且 big 的子目录圈选命中 1 次；合计 3 次）。
        # AI 调用共 2 次：阶段一圈区域 + 重复组判读（.msi 由本地守卫裁决，不耗 AI）。
        self.assertEqual(advisor.stats.api_calls, 2)
        self.assertEqual(result.duplicates[0].verdict, "likely")
        self.assertEqual(result.duplicates[0].comment, "同名副本散落")

    def test_invalid_root_raises(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "nope"
            with self.assertRaises(ValueError):
                analyze_root(missing, no_ai_advisor(), inventory_path=Path(temp_dir) / "inv.sqlite3")


if __name__ == "__main__":
    unittest.main()
