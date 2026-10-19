import json
from pathlib import Path


class FixtureSource:
    """Read-only source over a fixed, packaged benchmark dataset."""

    def __init__(self, fixture_path, allowed_records):
        self._allowed = frozenset(allowed_records)
        self._records = json.loads(Path(fixture_path).read_text(encoding="utf-8"))["records"]

    async def read(self, record_id):
        if record_id not in self._allowed:
            raise LookupError("RECORD_NOT_ALLOWED")
        try:
            text = self._records[record_id]
        except KeyError:
            raise LookupError("RECORD_NOT_FOUND") from None
        if not isinstance(text, str) or not text.strip():
            raise LookupError("INVALID_RECORD")
        return text
