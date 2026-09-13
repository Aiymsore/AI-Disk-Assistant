from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ai_disk_assistant.ai_advisor import HybridAdvisor
from ai_disk_assistant.config import Settings
from ai_disk_assistant.metadata import format_size
from ai_disk_assistant.models import Advice, FileMetadata


@dataclass(slots=True)
class BenchmarkRecord:
    id: int
    metadata: FileMetadata
    safe_to_delete: bool
    label_note: str


@dataclass(slots=True)
class Metrics:
    method: str
    total: int
    accuracy: float
    precision: float
    recall: float
    coverage: float
    dangerous_false_positives: int
    false_negatives: int
    elapsed_seconds: float
    api_calls: int = 0
    cache_hits: int = 0


def load_dataset(path: Path) -> list[BenchmarkRecord]:
    records: list[BenchmarkRecord] = []
    now = datetime.now()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        timestamp = now - timedelta(days=int(row["age_days"]))
        metadata = FileMetadata(
            path=row["path"],
            name=row["name"],
            suffix=row["suffix"],
            parent_folder=row["parent_folder"],
            size_bytes=int(row["size_bytes"]),
            size_text=format_size(int(row["size_bytes"])),
            modified_time=timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            accessed_time=timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        )
        records.append(BenchmarkRecord(row["id"], metadata, row["safe_to_delete"], row["label_note"]))
    return records


def calculate(method: str, labels: list[bool], advices: list[Advice], elapsed: float, advisor: HybridAdvisor) -> Metrics:
    predictions = [advice.recommend_delete for advice in advices]
    tp = sum(pred and label for pred, label in zip(predictions, labels, strict=True))
    tn = sum((not pred) and (not label) for pred, label in zip(predictions, labels, strict=True))
    fp = sum(pred and (not label) for pred, label in zip(predictions, labels, strict=True))
    fn = sum((not pred) and label for pred, label in zip(predictions, labels, strict=True))
    covered = sum(advice.advice_level != "人工确认" for advice in advices)
    return Metrics(
        method=method,
        total=len(labels),
        accuracy=(tp + tn) / len(labels) if labels else 0,
        precision=tp / (tp + fp) if tp + fp else 0,
        recall=tp / (tp + fn) if tp + fn else 0,
        coverage=covered / len(labels) if labels else 0,
        dangerous_false_positives=fp,
        false_negatives=fn,
        elapsed_seconds=elapsed,
        api_calls=advisor.stats.api_calls,
        cache_hits=advisor.stats.cache_hits,
    )


def evaluate(
    method: str,
    records: list[BenchmarkRecord],
    advisor: HybridAdvisor,
    function: Callable[[list[FileMetadata]], list[Advice]],
) -> tuple[Metrics, list[dict[str, object]]]:
    started = time.perf_counter()
    advices = function([record.metadata for record in records])
    elapsed = time.perf_counter() - started
    labels = [record.safe_to_delete for record in records]
    metrics = calculate(method, labels, advices, elapsed, advisor)
    details = []
    for record, advice in zip(records, advices, strict=True):
        details.append(
            {
                "id": record.id,
                "path": record.metadata.path,
                "label_safe_to_delete": record.safe_to_delete,
                "label_note": record.label_note,
                **advice.to_dict(),
                "correct": advice.recommend_delete == record.safe_to_delete,
            }
        )
    return metrics, details


def main() -> int:
    parser = argparse.ArgumentParser(description="比较本地规则、纯 AI 建议和安全混合方案。")
    parser.add_argument("--dataset", default="evaluation/benchmark.jsonl")
    parser.add_argument("--output", default="reports/benchmark_results.json")
    args = parser.parse_args()

    records = load_dataset(Path(args.dataset))
    settings = Settings.from_env()
    results: list[Metrics] = []
    details: dict[str, list[dict[str, object]]] = {}

    local = HybridAdvisor(settings, enable_ai=False)
    metric, rows = evaluate("local_rules", records, local, local.advise_many)
    results.append(metric)
    details[metric.method] = rows

    if settings.ai_api_key and settings.ai_model:
        pure_ai = HybridAdvisor(settings, enable_ai=True)
        metric, rows = evaluate("pure_ai_advisory", records, pure_ai, pure_ai.advise_ai_only_many)
        results.append(metric)
        details[metric.method] = rows

        hybrid = HybridAdvisor(settings, enable_ai=True)
        metric, rows = evaluate("hybrid_safe", records, hybrid, hybrid.advise_many)
        results.append(metric)
        details[metric.method] = rows
    else:
        print("未配置 AI_API_KEY / AI_MODEL，本次只运行本地规则基线。")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {"metrics": [asdict(metric) for metric in results], "details": details}
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n方法                 准确率   精确率   召回率   覆盖率   危险误判   耗时")
    for metric in results:
        print(
            f"{metric.method:<20} {metric.accuracy:>7.1%} {metric.precision:>8.1%} "
            f"{metric.recall:>8.1%} {metric.coverage:>8.1%} {metric.dangerous_false_positives:>10} "
            f"{metric.elapsed_seconds:>7.2f}s"
        )
    print(f"\n详细结果：{output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
