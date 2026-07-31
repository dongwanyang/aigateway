from __future__ import annotations

import fcntl
import threading
import time

import yaml

from aigateway_core.shared import runtime_values


def test_runtime_reader_waits_for_inplace_writer(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "server": {"port": 8000},
                "observability": {"otel_service_name": "gateway"},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_GATEWAY_CONFIG_PATH", str(path))
    monkeypatch.setenv("AI_GATEWAY_ENV", "production")
    monkeypatch.setattr(runtime_values, "_CACHE_PATH", None)
    monkeypatch.setattr(runtime_values, "_CACHE_MTIME_NS", None)
    monkeypatch.setattr(runtime_values, "_CACHE_ENV_FINGERPRINT", None)
    monkeypatch.setattr(runtime_values, "_CACHE_DATA", {})

    writer_ready = threading.Event()
    release_writer = threading.Event()
    reader_done = threading.Event()
    result: dict = {}

    def writer() -> None:
        payload = yaml.safe_dump(
            {
                "server": {"port": 9000},
                "observability": {"otel_service_name": "gateway"},
            }
        ).encode("utf-8")
        with path.open("r+b") as file:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
            file.seek(0)
            file.truncate()
            file.write(b"server: [")
            file.flush()
            writer_ready.set()
            assert release_writer.wait(2)
            file.seek(0)
            file.truncate()
            file.write(payload)
            file.flush()
            fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    def reader() -> None:
        result.update(runtime_values.load_runtime_config())
        reader_done.set()

    writer_thread = threading.Thread(target=writer)
    reader_thread = threading.Thread(target=reader)
    writer_thread.start()
    assert writer_ready.wait(2)
    reader_thread.start()

    time.sleep(0.1)
    assert not reader_done.is_set()
    release_writer.set()
    writer_thread.join(2)
    reader_thread.join(2)

    assert not writer_thread.is_alive()
    assert not reader_thread.is_alive()
    assert result["server"]["port"] == 9000
