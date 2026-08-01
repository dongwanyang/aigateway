from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aigateway_api import main
from aigateway_api.main import _configure_cuda_memory_limit


@pytest.fixture(autouse=True)
def _reset_cuda_memory_limit_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "_cuda_memory_limit_applied", False)


def test_cuda_memory_limit_is_noop_when_unset(monkeypatch):
    monkeypatch.delenv("AI_GATEWAY_CUDA_MEMORY_FRACTION", raising=False)
    monkeypatch.setitem(sys.modules, "torch", None)
    _configure_cuda_memory_limit()


def test_cuda_memory_limit_fails_closed_without_cuda(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_CUDA_MEMORY_FRACTION", "0.4")
    torch = SimpleNamespace(
        cuda=SimpleNamespace(is_available=Mock(return_value=False))
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    with pytest.raises(RuntimeError, match="gateway_cuda_unavailable"):
        _configure_cuda_memory_limit()


def test_cuda_memory_limit_is_applied(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_CUDA_MEMORY_FRACTION", "0.4")
    setter = Mock()
    torch = SimpleNamespace(
        cuda=SimpleNamespace(
            is_available=Mock(return_value=True),
            device_count=Mock(return_value=1),
            set_per_process_memory_fraction=setter,
        )
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    _configure_cuda_memory_limit()
    setter.assert_called_once_with(0.4, device=0)
