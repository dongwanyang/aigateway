"""Prometheus /metrics text-format parser + assertion helpers.

Parses lines like:
    aigateway_tokens_total{type="prompt"} 1234
    aigateway_request_duration_seconds_bucket{le="0.5"} 42
into {metric_name: [({label_dict}, value), ...]}.
"""
import re
import pytest
import httpx
from typing import Optional

from tests.conftest import BASE

_LINE_RE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{([^}]*)\})?\s+([0-9eE.+\-nNaAiIfF]+)\s*$')
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def _parse_labels(raw: str) -> dict:
    return dict(_LABEL_RE.findall(raw)) if raw else {}


def _parse_metrics_text(text: str) -> dict:
    out: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        name, labels_raw, value_raw = m.group(1), m.group(2) or "", m.group(3)
        try:
            value = float(value_raw)
        except ValueError:
            continue
        out.setdefault(name, []).append((_parse_labels(labels_raw), value))
    return out


class PromScraper:
    def __init__(self, url: str):
        self.url = url

    def snapshot(self) -> dict:
        r = httpx.get(self.url, timeout=5)
        r.raise_for_status()
        return _parse_metrics_text(r.text)

    def value(self, snap: dict, metric: str, **labels) -> float:
        """Return numeric value of `metric` with exactly-matching labels; 0.0 if absent."""
        for lbl, val in snap.get(metric, []):
            if all(lbl.get(k) == v for k, v in labels.items()):
                return val
        return 0.0

    def diff(self, before: dict, after: dict, metric: str, **labels) -> float:
        return self.value(after, metric, **labels) - self.value(before, metric, **labels)


@pytest.fixture
def prom_scrape():
    """Yield a PromScraper against gateway `/metrics`."""
    return PromScraper(f"{BASE}/metrics")


@pytest.fixture
def prom_scrape_prom_server():
    """PromScraper against Prometheus server's `/api/v1/query` (window C metrics reconciliation §8 #8)."""
    from tests.conftest import PROM_URL

    class PromServerScraper:
        def query(self, promql: str) -> list:
            r = httpx.get(f"{PROM_URL}/api/v1/query", params={"query": promql}, timeout=5)
            r.raise_for_status()
            return r.json().get("data", {}).get("result", [])

    return PromServerScraper()
