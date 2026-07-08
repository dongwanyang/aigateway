"""Verify aigateway_core.prefix re-exports match legacy paths."""
import importlib


def _assert_identical(new_mod, sources):
    for src_path in sources:
        src_mod = importlib.import_module(src_path)
        # Respect __all__ when defined (real-split modules declare their public
        # surface explicitly); otherwise fall back to all non-underscore names.
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


def test_prefix_pii_reexports():
    from aigateway_core import prefix
    from aigateway_core.pipeline import PIIDetectorPlugin as LegacyPIIPlugin

    assert prefix.pii.PIIDetectorPlugin is LegacyPIIPlugin
    # After the Task 3 real split, PIIDetector lives in prefix.pii.detector and
    # KeyStore/exceptions moved to shared.auth/exceptions. prefix.pii owns only
    # the PII surface; verify it re-exports the detector module's public names.
    _assert_identical(prefix.pii, ["aigateway_core.prefix.pii.detector"])
    # Backward-compat: the security.py shim still exposes PIIDetector identically.
    from aigateway_core.security import PIIDetector as LegacyPII
    assert prefix.pii.PIIDetector is LegacyPII


def test_prefix_cache_reexports():
    from aigateway_core import prefix
    from aigateway_core.pipeline import PromptCachePlugin, SemanticCachePlugin

    assert prefix.cache.PromptCachePlugin is PromptCachePlugin
    assert prefix.cache.SemanticCachePlugin is SemanticCachePlugin
    _assert_identical(prefix.cache, ["aigateway_core.caching"])


def test_prefix_media_reexports():
    from aigateway_core import prefix
    from aigateway_core.media.plugin import MediaOptimizationPlugin

    assert prefix.media.plugin.MediaOptimizationPlugin is MediaOptimizationPlugin
    _assert_identical(prefix.media, ["aigateway_core.media"])
