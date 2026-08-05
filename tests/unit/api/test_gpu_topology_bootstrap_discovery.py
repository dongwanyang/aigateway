from __future__ import annotations

from types import SimpleNamespace


def _module():
    from aigateway_api import gpu_topology_bootstrap

    return gpu_topology_bootstrap


def test_bootstrap_discovery_rejects_partial_output(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                "0, GPU-a, GPU A, 16384, 16000\n"
                "malformed\n"
            ),
            stderr="",
        ),
    )

    assert module._discover_devices() == []


def test_bootstrap_discovery_rejects_duplicate_uuid(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                "0, GPU-same, GPU A, 16384, 16000\n"
                "1, GPU-same, GPU B, 24576, 24000\n"
            ),
            stderr="",
        ),
    )

    assert module._discover_devices() == []


def test_bootstrap_discovery_sorts_complete_inventory(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=(
                "1, GPU-b, GPU B, 24576, 24000\n"
                "0, GPU-a, GPU A, 16384, 16000\n"
            ),
            stderr="",
        ),
    )

    devices = module._discover_devices()

    assert [item["index"] for item in devices] == [0, 1]
    assert [item["uuid"] for item in devices] == ["GPU-a", "GPU-b"]
