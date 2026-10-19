from pathlib import Path

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "benchmark.json"

SAMPLE_RECORD_IDS = ("TX-001", "TX-002", "TX-003", "TX-004")

BENCHMARK_RECORD_IDS = frozenset(
    {
        "TX-001",
        "TX-002",
        "TX-003",
        "TX-004",
        "TX-INJECT",
        "TX-LONG",
    }
)
