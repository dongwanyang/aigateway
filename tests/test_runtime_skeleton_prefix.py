"""Verify prefix package exports match their authoritative implementations."""
import importlib


def _assert_identical(new_mod, sources):
    for src_path in sources:
        src_mod = importlib.import_module(src_path)
        names = getattr(src_mod, "__all__", None)
        if names is None:
            names = [n for n in dir(src_mod) if not n.startswith("_")]
        for name in names:
            assert hasattr(new_mod, name), (
                f"{new_mod.__name__} missing {name!r} from {src_path}"
            )
            assert getattr(new_mod, name) is getattr(src_mod, name), (
                f"{new_mod.__name__}.{name} diverges from {src_path}.{name}"
            )


def test_prefix_pii_exports():
    from aigateway_core.prefix import pii
    from aigateway_core.prefix.pii.detector import PIIDetector

    assert pii.PIIDetector is PIIDetector
    _assert_identical(pii, ["aigateway_core.prefix.pii.detector"])


def test_prefix_cache_exports():
    from aigateway_core.prefix import cache
    from aigateway_core.prefix.cache.cache_keys import (
        _normalize_prompt,
        _bucket_temperature,
        _bucket_max_tokens,
        _model_family,
    )

    assert cache._normalize_prompt is _normalize_prompt
    assert cache._bucket_temperature is _bucket_temperature
    assert cache._bucket_max_tokens is _bucket_max_tokens
    assert cache._model_family is _model_family
    _assert_identical(cache, ["aigateway_core.prefix.cache.cache_keys"])


def test_prefix_media_reexports():
    from aigateway_core import prefix
    from aigateway_core.prefix.media.plugin import MediaOptimizationPlugin

    assert prefix.media.plugin.MediaOptimizationPlugin is MediaOptimizationPlugin
    _assert_identical(prefix.media, ["aigateway_core.prefix.media"])
