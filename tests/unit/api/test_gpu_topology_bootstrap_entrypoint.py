from __future__ import annotations

import sys
from types import SimpleNamespace

import aigateway_api


def test_reconcile_entrypoint_skips_explicit_cuda_disablement(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AI_GATEWAY_ACCELERATOR", "cuda")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "-1")
    monkeypatch.setitem(
        sys.modules,
        "aigateway_api.gpu_topology_bootstrap",
        SimpleNamespace(
            bootstrap_gpu_topology=lambda: (_ for _ in ()).throw(
                AssertionError("bootstrap must not run")
            )
        ),
    )

    aigateway_api._reconcile_gpu_topology()
