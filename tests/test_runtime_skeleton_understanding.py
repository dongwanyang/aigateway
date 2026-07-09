"""Verify aigateway_core.pipelines.understanding re-exports."""
import importlib


def _assert_identical(new_mod, src_path):
    src_mod = importlib.import_module(src_path)
    for name in dir(src_mod):
        if name.startswith("_"):
            continue
        assert hasattr(new_mod, name), (
            f"{new_mod.__name__} missing {name!r} from {src_path}"
        )
        assert getattr(new_mod, name) is getattr(src_mod, name)


def test_understanding_rag_reexports():
    from aigateway_core.pipelines.understanding import rag
    _assert_identical(rag, "aigateway_core.plugins.rag_retriever_plugin")


def test_understanding_conversation_reexports():
    from aigateway_core.pipelines.understanding import conversation
    _assert_identical(conversation, "aigateway_core.plugins.conv_compressor_plugin")


def test_understanding_compression_reexports():
    from aigateway_core.pipelines.understanding import compression
    from aigateway_core.prefix.plugins.classic_plugins import PromptCompressPlugin

    assert compression.PromptCompressPlugin is PromptCompressPlugin
