from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from ai_disk_assistant.ai_advisor import HybridAdvisor, _extract_json
from ai_disk_assistant.config import Settings
from ai_disk_assistant.metadata import get_file_metadata
from ai_disk_assistant.models import FileMetadata
from ai_disk_assistant.safety import RULES_VERSION, is_protected_path, local_safety_guard


def _metadata(path: str, suffix: str) -> FileMetadata:
    return FileMetadata(
        path=path,
        name="file" + suffix,
        suffix=suffix,
        parent_folder="",
        size_bytes=100,
        size_text="100 B",
        modified_time="",
        accessed_time="",
        modified_time_ns=0,
        accessed_time_ns=0,
        device_id=0,
        file_id=0,
    )


class LocalGuardRuleTests(unittest.TestCase):
    """守卫真值表：放行只针对上下文与压缩包，受保护目录/个人内容硬上限不动。"""

    def test_installer_in_download_context_is_released_to_ai(self) -> None:
        self.assertIsNone(local_safety_guard(_metadata(r"C:\Users\me\Downloads\setup.exe", ".exe")))
        self.assertIsNone(local_safety_guard(_metadata(r"D:\temp\package.msi", ".msi")))

    def test_executable_outside_context_stays_capped(self) -> None:
        advice = local_safety_guard(_metadata(r"D:\校园跑计划\Microsoft VS Code\rg.exe", ".exe"))
        self.assertEqual(advice.advice_level, "人工确认")
        self.assertEqual(advice.source, "local-guard")

    def test_archives_are_released_to_ai(self) -> None:
        self.assertIsNone(local_safety_guard(_metadata(r"D:\backup.zip", ".zip")))
        self.assertIsNone(local_safety_guard(_metadata(r"D:\games\client.7z", ".7z")))

    def test_personal_content_stays_capped(self) -> None:
        cases = ((r"D:\docs\note.docx", ".docx"), (r"D:\app\foo.dll", ".dll"), (r"D:\proj\main.py", ".py"))
        for path, suffix in cases:
            advice = local_safety_guard(_metadata(path, suffix))
            self.assertEqual(advice.advice_level, "人工确认", path)

    def test_protected_dir_still_wins_over_released_rules(self) -> None:
        advice = local_safety_guard(_metadata(r"C:\Windows\System32\foo.dll", ".dll"))
        self.assertEqual(advice.advice_level, "不建议删除")

    def test_rules_version_bumped_with_guard_change(self) -> None:
        # 守卫逻辑变更必须递增版本号，否则判定缓存会复用旧结论。
        self.assertEqual(RULES_VERSION, "unit-decision-3")


class SafetyTests(unittest.TestCase):
    def test_windows_path_is_protected(self) -> None:
        self.assertTrue(is_protected_path(r"C:\Windows\System32\kernel.dll"))
        self.assertTrue(is_protected_path(r"C:\Program Files\Example\app.exe"))

    def test_user_document_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            file = Path(temp_dir) / "report.docx"
            file.write_bytes(b"demo")
            advisor = HybridAdvisor(
                Settings(None, "https://example.invalid/v1", "demo", 1),
                enable_ai=False,
            )
            advice = advisor.advise(get_file_metadata(file))
            self.assertFalse(advice.recommend_delete)
            self.assertEqual(advice.advice_level, "人工确认")

    def test_temp_log_can_be_suggested(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = Path(temp_dir) / "cache"
            cache_dir.mkdir()
            file = cache_dir / "old.log"
            file.write_text("demo", encoding="utf-8")
            advisor = HybridAdvisor(
                Settings(None, "https://example.invalid/v1", "demo", 1),
                enable_ai=False,
            )
            advice = advisor.advise(get_file_metadata(file))
            self.assertTrue(advice.recommend_delete)
            self.assertEqual(advice.advice_level, "建议删除")

    def test_extract_json_from_code_fence(self) -> None:
        result = _extract_json('```json\n{"recommend_delete": false}\n```')
        self.assertFalse(result["recommend_delete"])

    def test_model_name_is_required_for_ai_mode(self) -> None:
        advisor = HybridAdvisor(
            Settings("secret", "https://example.invalid/v1", "", 1),
            enable_ai=True,
        )
        self.assertFalse(advisor.ai_available)


class InstallerSignalScoringTests(unittest.TestCase):
    """评分层：安装包信号只在下载/临时上下文生效，程序目录的 exe 不再涌入候选。"""

    ROOT = Path("D:\\")

    def _signal(self, path: str):
        from ai_disk_assistant.scanner import ScanPolicy, _signals_from_facts

        return _signals_from_facts(path, Path(path).suffix.casefold(), 5_000_000, self.ROOT, ScanPolicy())

    def test_installer_signal_fires_in_download_context(self) -> None:
        result = self._signal(r"D:\Downloads\setup.exe")
        self.assertIsNotNone(result)
        self.assertIn("安装包残留", result[0])

    def test_installer_signal_silent_in_program_dir(self) -> None:
        # 无其他信号的小体积程序文件：不再仅因 .exe 后缀成为候选
        self.assertIsNone(self._signal(r"D:\校园跑计划\Microsoft VS Code\rg.exe"))


if __name__ == "__main__":
    unittest.main()
