"""Unit tests for admin_routes.py helper functions.

Covers:
- _split_text: fixed_size, paragraph, sentence chunking
- _compute_hash_embeddings: deterministic 1024-dim vectors
- _get_auth_defaults: config-based defaults
- _format_quota_item: quota formatting
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

import pytest
from unittest.mock import MagicMock, patch


class TestSplitText:
    """Test _split_text pure function for text chunking."""

    def _get_split_func(self):
        from aigateway_api.admin_routes import _split_text
        return _split_text

    def test_fixed_size_basic(self):
        split = self._get_split_func()
        text = "A" * 100
        chunks = split(text, strategy="fixed_size", chunk_size=30, overlap=5)
        assert len(chunks) > 1
        assert all(len(c) <= 30 for c in chunks)
        # First chunk should be 30 chars
        assert len(chunks[0]) == 30

    def test_fixed_size_exact_fit(self):
        split = self._get_split_func()
        text = "A" * 60
        chunks = split(text, strategy="fixed_size", chunk_size=30, overlap=0)
        assert len(chunks) == 2
        assert all(len(c) == 30 for c in chunks)

    def test_fixed_size_with_overlap(self):
        split = self._get_split_func()
        text = "A" * 100
        chunks = split(text, strategy="fixed_size", chunk_size=30, overlap=10)
        assert len(chunks) > 1
        # Each chunk should overlap by ~10 chars
        for i in range(1, len(chunks)):
            # The overlap region should match
            overlap_region = chunks[i][:10]
            prev_chunk_end = chunks[i-1][-10:]
            assert overlap_region == prev_chunk_end, f"Overlap mismatch at chunk {i}"

    def test_paragraph_strategy(self):
        split = self._get_split_func()
        text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
        chunks = split(text, strategy="paragraph", chunk_size=50, overlap=0)
        assert len(chunks) >= 1
        # Paragraphs should be preserved within chunks
        assert "\n\n" not in chunks[-1] or len(chunks) == 1

    def test_sentence_strategy(self):
        split = self._get_split_func()
        text = "First sentence. Second sentence. Third sentence."
        chunks = split(text, strategy="sentence", chunk_size=30, overlap=0)
        assert len(chunks) >= 1

    def test_empty_text(self):
        split = self._get_split_func()
        chunks = split("", strategy="fixed_size")
        assert chunks == []

    def test_short_text(self):
        split = self._get_split_func()
        chunks = split("Hi", strategy="fixed_size")
        assert chunks == []

    def test_single_chunk_fits(self):
        split = self._get_split_func()
        text = "A" * 20
        chunks = split(text, strategy="fixed_size", chunk_size=100, overlap=0)
        assert len(chunks) == 1
        assert chunks[0] == "A" * 20

    def test_default_parameters(self):
        split = self._get_split_func()
        text = "Word " * 100  # ~500 chars
        chunks = split(text)  # defaults: fixed_size, 512, 64
        assert isinstance(chunks, list)
        assert len(chunks) >= 1
        # Chunks should not exceed chunk_size
        assert all(len(c) <= 512 for c in chunks)

    def test_fixed_size_last_chunk_shorter(self):
        split = self._get_split_func()
        text = "A" * 35
        chunks = split(text, strategy="fixed_size", chunk_size=30, overlap=0)
        assert len(chunks) == 2
        assert len(chunks[0]) == 30
        assert len(chunks[1]) == 5  # remainder

    def test_overlap_zero(self):
        split = self._get_split_func()
        text = "A" * 60
        chunks = split(text, strategy="fixed_size", chunk_size=20, overlap=0)
        assert len(chunks) == 3
        assert all(len(c) == 20 for c in chunks)

    def test_overlap_equals_chunk_size(self):
        """Edge case: overlap equals chunk size — should still produce valid chunks."""
        split = self._get_split_func()
        text = "AB" * 30  # 60 chars
        chunks = split(text, strategy="fixed_size", chunk_size=30, overlap=30)
        assert len(chunks) >= 1

    def test_paragraph_empty_paragraphs_skipped(self):
        split = self._get_split_func()
        text = "Para one.\n\n\n\nPara two."
        chunks = split(text, strategy="paragraph", chunk_size=100, overlap=0)
        assert len(chunks) >= 1
        # Empty paragraphs should not create empty chunks
        assert all(len(c) > 0 for c in chunks)

    def test_sentence_no_periods(self):
        split = self._get_split_func()
        text = "No periods here just words"
        chunks = split(text, strategy="sentence", chunk_size=100, overlap=0)
        assert len(chunks) >= 1


class TestComputeHashEmbeddings:
    """Test _compute_hash_embeddings pure function."""

    def _get_func(self):
        from aigateway_api.admin_routes import _compute_hash_embeddings
        return _compute_hash_embeddings

    def test_single_text(self):
        func = self._get_func()
        vectors = func(["hello world"])
        assert len(vectors) == 1
        assert len(vectors[0]) == 1024

    def test_multiple_texts(self):
        func = self._get_func()
        vectors = func(["text one", "text two", "text three"])
        assert len(vectors) == 3
        assert all(len(v) == 1024 for v in vectors)

    def test_deterministic(self):
        func = self._get_func()
        v1 = func(["same text"])[0]
        v2 = func(["same text"])[0]
        assert v1 == v2

    def test_different_texts_different_vectors(self):
        func = self._get_func()
        v1 = func(["text a"])[0]
        v2 = func(["text b"])[0]
        assert v1 != v2

    def test_l2_normalized(self):
        func = self._get_func()
        vectors = func(["test"])
        import math
        norm = math.sqrt(sum(x * x for x in vectors[0]))
        # Should be approximately 1.0 (L2 normalized)
        assert abs(norm - 1.0) < 0.01

    def test_values_in_range(self):
        func = self._get_func()
        vectors = func(["test"])
        for v in vectors[0]:
            assert -1.0 <= v <= 1.0, f"Value {v} out of [-1, 1] range"

    def test_empty_list(self):
        func = self._get_func()
        vectors = func([])
        assert vectors == []


class TestGetAuthDefaults:
    """Test _get_auth_defaults with mocked config_manager."""

    def test_default_values_no_config(self):
        from aigateway_api.admin_routes import _get_auth_defaults

        with patch("aigateway_api.app_state.get_state") as mock_get_state:
            mock_get_state.return_value.config_manager = None
            defaults = _get_auth_defaults()
            assert defaults["daily_tokens"] == 1_000_000
            assert defaults["monthly_cost"] == 50.0
            assert defaults["rate_limit_rpm"] == 60
            assert defaults["rate_limit_tpm"] == 100_000

    def test_default_values_config_no_auth(self):
        from aigateway_api.admin_routes import _get_auth_defaults

        mock_cm = MagicMock()
        mock_cm.get.return_value = {}

        with patch("aigateway_api.app_state.get_state") as mock_get_state:
            mock_get_state.return_value.config_manager = mock_cm
            defaults = _get_auth_defaults()
            assert defaults["daily_tokens"] == 1_000_000
            assert defaults["monthly_cost"] == 50.0

    def test_custom_values_from_config(self):
        from aigateway_api.admin_routes import _get_auth_defaults

        mock_cm = MagicMock()
        mock_cm.get.return_value = {
            "defaults": {
                "daily_tokens": 500000,
                "monthly_cost": 25.0,
                "rate_limit_rpm": 30,
                "rate_limit_tpm": 50000,
            }
        }

        with patch("aigateway_api.app_state.get_state") as mock_get_state:
            mock_get_state.return_value.config_manager = mock_cm
            defaults = _get_auth_defaults()
            assert defaults["daily_tokens"] == 500000
            assert defaults["monthly_cost"] == 25.0
            assert defaults["rate_limit_rpm"] == 30
            assert defaults["rate_limit_tpm"] == 50000

    def test_partial_config_values(self):
        from aigateway_api.admin_routes import _get_auth_defaults

        mock_cm = MagicMock()
        mock_cm.get.return_value = {
            "defaults": {
                "daily_tokens": 1000000,
                # monthly_cost not specified — should use default
            }
        }

        with patch("aigateway_api.app_state.get_state") as mock_get_state:
            mock_get_state.return_value.config_manager = mock_cm
            defaults = _get_auth_defaults()
            assert defaults["daily_tokens"] == 1000000
            assert defaults["monthly_cost"] == 50.0  # default


class TestFormatQuotaItem:
    """Test _format_quota_item pure formatting function."""

    @pytest.fixture(autouse=True)
    def _isolate_get_state(self):
        """_format_quota_item 内部调 _get_auth_defaults() → get_state(),
        后者会拉起真实 app/config_manager,把测试和环境 config.yaml 耦合。
        这里把 get_state() mock 成 config_manager=None 的空 state,
        使 _get_auth_defaults 走硬编码默认值分支,测试确定且隔离。
        """
        empty_state = type("S", (), {"config_manager": None})()
        with patch("aigateway_api.app_state.get_state", return_value=empty_state):
            yield

    def test_basic_formatting(self):
        from aigateway_api.admin_routes import _format_quota_item

        key_data = {
            "key_id": "key_123",
            "key_prefix": "gw-abc",
            "user_id": "test-user",
            "group_id": "grp-default",
            "cache_scope": "group",
            "created_at": "2026-01-01T00:00:00Z",
            "last_used_at": "2026-01-02T12:00:00Z",
            "status": "active",
            "daily_tokens_limit": 1000000,
            "daily_tokens_used": 500000,
            "monthly_cost_limit": 50.0,
            "monthly_cost_used": 25.0,
            "rate_limit_rpm": 60,
            "rate_limit_tpm": 100000,
            "rpm_window_count": 10,
            "tpm_window_count": 50000,
        }

        item = _format_quota_item(key_data, "abc123", group_name="test-group")

        assert item["id"] == "key_123"
        assert item["key_prefix"] == "gw-abc"
        assert item["user_id"] == "test-user"
        assert item["group_name"] == "test-group"
        assert item["cache_scope"] == "group"
        assert item["status"] == "active"

    def test_quotas_section(self):
        from aigateway_api.admin_routes import _format_quota_item

        key_data = {
            "key_id": "key_456",
            "daily_tokens_limit": 1000000,
            "daily_tokens_used": 750000,
            "monthly_cost_limit": 100.0,
            "monthly_cost_used": 50.0,
            "rate_limit_rpm": 120,
            "rate_limit_tpm": 200000,
            "rpm_window_count": 50,
            "tpm_window_count": 150000,
        }

        item = _format_quota_item(key_data, "def456")

        assert item["quotas"]["daily_tokens_used"] == 750000
        assert item["quotas"]["daily_tokens_limit"] == 1000000
        assert item["quotas"]["monthly_cost_used"] == 50.0
        assert item["quotas"]["monthly_cost_limit"] == 100.0
        assert item["quotas"]["rpm_current"] == 50
        assert item["quotas"]["rpm_limit"] == 120
        assert item["quotas"]["tpm_current"] == 150000
        assert item["quotas"]["tpm_limit"] == 200000

    def test_usage_percentage(self):
        from aigateway_api.admin_routes import _format_quota_item

        key_data = {
            "key_id": "key_789",
            "daily_tokens_limit": 1000000,
            "daily_tokens_used": 250000,
            "monthly_cost_limit": 50.0,
            "monthly_cost_used": 12.5,
        }

        item = _format_quota_item(key_data, "ghi789")

        assert item["usage_percentage"]["daily_tokens"] == 0.25
        assert item["usage_percentage"]["monthly_cost"] == 0.25

    def test_zero_limits_no_division_error(self):
        from aigateway_api.admin_routes import _format_quota_item

        key_data = {
            "key_id": "key_000",
            "daily_tokens_limit": 0,
            "daily_tokens_used": 0,
            "monthly_cost_limit": 0.0,
            "monthly_cost_used": 0.0,
        }

        item = _format_quota_item(key_data, "jkl000")
        assert item["usage_percentage"]["daily_tokens"] == 0.0
        assert item["usage_percentage"]["monthly_cost"] == 0.0

    def test_null_group_id(self):
        from aigateway_api.admin_routes import _format_quota_item

        key_data = {
            "key_id": "key_111",
            "group_id": "",
            "daily_tokens_limit": 1000000,
            "daily_tokens_used": 0,
            "monthly_cost_limit": 50.0,
            "monthly_cost_used": 0.0,
            "rate_limit_rpm": 60,
            "rate_limit_tpm": 100000,
            "rpm_window_count": 0,
            "tpm_window_count": 0,
        }

        item = _format_quota_item(key_data, "mno111")
        assert item["group_id"] == ""
        assert item["cache_scope"] == "group"  # default

    def test_null_last_used_at(self):
        from aigateway_api.admin_routes import _format_quota_item

        key_data = {
            "key_id": "key_222",
            "last_used_at": None,
            "daily_tokens_limit": 1000000,
            "daily_tokens_used": 0,
            "monthly_cost_limit": 50.0,
            "monthly_cost_used": 0.0,
            "rate_limit_rpm": 60,
            "rate_limit_tpm": 100000,
            "rpm_window_count": 0,
            "tpm_window_count": 0,
        }

        item = _format_quota_item(key_data, "pqr222")
        assert item["last_used_at"] is None
