from __future__ import annotations

import unittest

from ai_disk_assistant.models import FileMetadata, Unit
from ai_disk_assistant.privacy import unit_payload_for_ai
from ai_disk_assistant.units import build_units, size_bucket, unit_fingerprint


def metadata(path: str, size: int = 100) -> FileMetadata:
    normalized = path.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1]
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return FileMetadata(
        path=path,
        name=name,
        suffix=suffix,
        parent_folder=normalized.rsplit("/", 1)[0],
        size_bytes=size,
        size_text=f"{size} B",
        modified_time="2020-01-01 00:00:00",
        accessed_time="2020-01-01 00:00:00",
        modified_time_ns=1_577_836_800_000_000_000,
    )


class BucketTests(unittest.TestCase):
    def test_size_bucket_boundaries(self) -> None:
        self.assertEqual(size_bucket(0), "<10KB")
        self.assertEqual(size_bucket(9 * 1024), "<10KB")
        self.assertEqual(size_bucket(10 * 1024), "10KB-1MB")
        self.assertEqual(size_bucket(1024**3), "1-10GB")
        self.assertEqual(size_bucket(11 * 1024**3), ">10GB")


class FingerprintTests(unittest.TestCase):
    def test_same_pattern_same_fingerprint(self) -> None:
        first = unit_fingerprint(r"C:\Users\A\AppData\Local\npm-cache", ".log", "<10KB")
        second = unit_fingerprint(r"C:\Users\A\AppData\Local\npm-cache", ".log", "<10KB")
        self.assertEqual(first, second)

    def test_case_and_separator_insensitive(self) -> None:
        first = unit_fingerprint(r"C:\Data\Cache", ".tmp", "<10KB")
        second = unit_fingerprint("c:/data/cache", ".TMP", "<10KB")
        self.assertEqual(first, second)

    def test_different_pattern_different_fingerprint(self) -> None:
        base = unit_fingerprint(r"C:\cache", ".log", "<10KB")
        self.assertNotEqual(base, unit_fingerprint(r"C:\cache", ".tmp", "<10KB"))
        self.assertNotEqual(base, unit_fingerprint(r"C:\cache", ".log", "10KB-1MB"))


class BuildUnitsTests(unittest.TestCase):
    def test_merges_same_pattern_and_keeps_path_map(self) -> None:
        records = [
            (metadata(r"C:\app\trace\a.log", 100), "理由", 5.0),
            (metadata(r"C:\app\trace\b.log", 200), "理由", 3.0),
            (metadata(r"C:\app\trace\c.tmp", 300), "理由", 1.0),
        ]
        units, path_map = build_units(records)
        log_units = [unit for unit in units if unit.suffix == ".log"]
        self.assertEqual(len(log_units), 1)
        log_unit = log_units[0]
        self.assertEqual(log_unit.file_count, 2)
        self.assertEqual(log_unit.total_size, 300)
        self.assertEqual(log_unit.members[0].suffix, ".log")
        self.assertEqual(len(path_map), 3)
        self.assertNotEqual(path_map[r"C:\app\trace\a.log"], path_map[r"C:\app\trace\c.tmp"])
        # 单元按最高候选分降序：.log 单元（5.0）排在 .tmp 单元（1.0）之前。
        self.assertEqual(units[0].suffix, ".log")

    def test_different_size_bucket_splits_units(self) -> None:
        records = [
            (metadata(r"C:\app\trace\small.log", 100), "理由", 1.0),
            (metadata(r"C:\app\trace\huge.log", 50 * 1024 * 1024), "理由", 1.0),
        ]
        units, _path_map = build_units(records)
        self.assertEqual(len(units), 2)


class UnitPayloadPrivacyTests(unittest.TestCase):
    def make_unit(self) -> Unit:
        return Unit(
            unit_id="fp",
            parent_folder=r"C:\Users\Test\AppData\Local\Temp",
            suffix=".tmp",
            size_bucket="<10KB",
            file_count=2,
            total_size=300,
            members=[metadata(r"C:\Users\Test\AppData\Local\Temp\a.tmp")],
        )

    def test_payload_has_no_age_fields(self) -> None:
        payload = unit_payload_for_ai(self.make_unit(), "balanced")
        self.assertNotIn("age_min_days", payload)
        self.assertNotIn("age_max_days", payload)
        self.assertNotIn("modified_time", payload)

    def test_balanced_anonymizes_pattern(self) -> None:
        payload = unit_payload_for_ai(self.make_unit(), "balanced")
        self.assertNotIn("Test", payload["path_pattern"])
        self.assertEqual(payload["file_count"], 2)
        self.assertEqual(payload["suffix"], ".tmp")

    def test_strict_omits_pattern(self) -> None:
        payload = unit_payload_for_ai(self.make_unit(), "strict")
        self.assertNotIn("path_pattern", payload)
        self.assertIn("temp", payload["directory_context"])

    def test_full_keeps_raw_pattern(self) -> None:
        payload = unit_payload_for_ai(self.make_unit(), "full")
        self.assertEqual(payload["path_pattern"], r"C:\Users\Test\AppData\Local\Temp")


if __name__ == "__main__":
    unittest.main()
