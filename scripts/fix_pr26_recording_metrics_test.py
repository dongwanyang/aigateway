"""Add the missing local metrics recorder used by PR #26 regressions."""
from pathlib import Path

path = Path("tests/unit/test_merge_readiness_followup.py")
text = path.read_text(encoding="utf-8")
anchor = '''class _RecordingKeyStore:
'''
insert = '''class RecordingMetrics:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, str]] = []
        self.durations: list[tuple[str, float]] = []

    def record_request(self, method: str, path: str, status: str) -> None:
        self.requests.append((method, path, status))

    def record_duration(self, path: str, duration: float) -> None:
        self.durations.append((path, duration))


class _RecordingKeyStore:
'''
if anchor not in text:
    raise SystemExit("RecordingKeyStore anchor not found")
path.write_text(text.replace(anchor, insert, 1), encoding="utf-8")
