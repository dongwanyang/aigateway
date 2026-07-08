"""Cache key v2 单元测试.

验证 generate_cache_key v2 分层设计的行为:
1. model_family 抽取:snapshot 后缀被去掉,同 family 共享 key
2. 参数分桶:temperature/max_tokens 细微偏差落同一桶
3. temperature<=0.05 保留精确桶(不与 0.2 共享)
4. top_p 被完全忽略
5. cache_scope:shared 不带 user_id,private 才带
6. pipeline_kind:understanding vs generation 严格隔离
7. normalize:全半角/空白差异等价
8. tenant_id 隔离
9. auto 模型不做 family 抽取
"""
from aigateway_core.prefix.cache.cache_keys import (
    _bucket_max_tokens,
    _bucket_temperature,
    _model_family,
    _normalize_prompt,
)
from aigateway_core.prefix.cache.cache_manager import CacheManager


class TestModelFamily:
    def test_gpt4o_snapshot_stripped(self):
        assert _model_family("gpt-4o-2024-08-06") == "gpt-4o"
        assert _model_family("gpt-4o-2024-11-20") == "gpt-4o"
        assert _model_family("gpt-4o") == "gpt-4o"

    def test_gpt4o_mini_snapshot_stripped(self):
        assert _model_family("gpt-4o-mini-2024-07-18") == "gpt-4o-mini"
        assert _model_family("gpt-4o-mini") == "gpt-4o-mini"

    def test_claude_snapshot_stripped(self):
        assert _model_family("claude-3-5-sonnet-20241022") == "claude-3-5-sonnet"
        assert _model_family("claude-3-5-sonnet-20240620") == "claude-3-5-sonnet"
        assert _model_family("claude-sonnet-4-5-20250929") == "claude-sonnet-4-5"

    def test_latest_suffix_stripped(self):
        assert _model_family("gpt-4-latest") == "gpt-4"

    def test_provider_prefix_preserved(self):
        assert _model_family("openai/gpt-4o-2024-08-06") == "openai/gpt-4o"
        assert _model_family("anthropic/claude-3-5-sonnet-20241022") == "anthropic/claude-3-5-sonnet"

    def test_empty(self):
        assert _model_family("") == ""


class TestTemperatureBucket:
    def test_zero_and_near_zero_are_exact(self):
        assert _bucket_temperature(0.0) == "exact_zero"
        assert _bucket_temperature(0.03) == "exact_zero"
        assert _bucket_temperature(0.05) == "exact_zero"

    def test_low_range_is_det(self):
        assert _bucket_temperature(0.1) == "det"
        assert _bucket_temperature(0.2) == "det"
        assert _bucket_temperature(0.3) == "det"

    def test_mid_range_is_bal(self):
        # 用户常见 0.7 vs 0.75 都应落 bal
        assert _bucket_temperature(0.5) == "bal"
        assert _bucket_temperature(0.7) == "bal"
        assert _bucket_temperature(0.75) == "bal"
        assert _bucket_temperature(0.9) == "bal"

    def test_high_range_is_cre(self):
        assert _bucket_temperature(1.0) == "cre"
        assert _bucket_temperature(1.5) == "cre"
        assert _bucket_temperature(2.0) == "cre"

    def test_none_defaults_to_openai_default(self):
        # None → 1.0(OpenAI 默认) → cre
        assert _bucket_temperature(None) == "cre"


class TestMaxTokensBucket:
    def test_none_or_zero(self):
        assert _bucket_max_tokens(None) == "any"
        assert _bucket_max_tokens(0) == "any"

    def test_round_up_to_nearest(self):
        assert _bucket_max_tokens(100) == "le_256"
        assert _bucket_max_tokens(256) == "le_256"
        assert _bucket_max_tokens(300) == "le_512"
        assert _bucket_max_tokens(1024) == "le_1024"
        # 常见 SDK 默认差异:1000 和 1024 都落 le_1024
        assert _bucket_max_tokens(1000) == _bucket_max_tokens(1024) == "le_1024"

    def test_beyond_max(self):
        assert _bucket_max_tokens(20000) == "gt_16384"


class TestNormalize:
    def test_whitespace_collapse(self):
        assert _normalize_prompt("你好   世界") == "你好 世界"
        assert _normalize_prompt("hello\n\nworld") == "hello world"
        assert _normalize_prompt("  hello  ") == "hello"

    def test_nfkc_fullwidth(self):
        # 全角字符归一化为半角
        assert _normalize_prompt("ａｂｃ") == "abc"

    def test_empty(self):
        assert _normalize_prompt("") == ""


class TestGenerateCacheKey:
    """核心行为:验证不同参数组合的 key 关系(相等 or 不等)。"""

    def _key(self, **kw):
        defaults = {
            "normalized_prompt": "你好",
            "model": "gpt-4o",
        }
        defaults.update(kw)
        return CacheManager.generate_cache_key(**defaults)

    def test_snapshot_and_family_same_key(self):
        """gpt-4o 和 gpt-4o-2024-08-06 生成同一个 key。"""
        k1 = self._key(model="gpt-4o")
        k2 = self._key(model="gpt-4o-2024-08-06")
        k3 = self._key(model="gpt-4o-2024-11-20")
        assert k1 == k2 == k3

    def test_different_family_different_key(self):
        """gpt-4o 和 gpt-4o-mini 是不同 family → 不同 key。"""
        assert self._key(model="gpt-4o") != self._key(model="gpt-4o-mini")

    def test_cross_vendor_different_key(self):
        """gpt-4o 和 claude 必须严格隔离。"""
        assert self._key(model="gpt-4o") != self._key(model="claude-3-5-sonnet-20241022")

    def test_temperature_bucket_merges_neighbors(self):
        """0.7 / 0.75 都在 bal 桶,应生成同一 key。"""
        k1 = self._key(temperature=0.7)
        k2 = self._key(temperature=0.75)
        assert k1 == k2

    def test_temperature_zero_stays_exact(self):
        """temperature=0 和 0.2 不该共享(前者 exact_zero,后者 det)。"""
        assert self._key(temperature=0.0) != self._key(temperature=0.2)

    def test_top_p_ignored(self):
        """top_p 完全不影响 key。"""
        k1 = self._key(top_p=1.0)
        k2 = self._key(top_p=0.9)
        k3 = self._key(top_p=0.5)
        assert k1 == k2 == k3

    def test_max_tokens_bucket_merges(self):
        """1000 和 1024 都在 le_1024 桶 → 同 key。"""
        assert self._key(max_tokens=1000) == self._key(max_tokens=1024)

    def test_max_tokens_none_and_zero_equal(self):
        """None / 0 都归 any → 同 key。"""
        assert self._key(max_tokens=None) == self._key(max_tokens=0)

    def test_scope_shared_ignores_user_id(self):
        """默认 shared:同 prompt 不同 user_id 共享 key。"""
        k_alice = self._key(cache_scope="shared", user_id="alice")
        k_bob = self._key(cache_scope="shared", user_id="bob")
        k_none = self._key(cache_scope="shared", user_id="")
        assert k_alice == k_bob == k_none

    def test_scope_private_includes_user_id(self):
        """private:不同 user_id → 不同 key。"""
        k_alice = self._key(cache_scope="private", user_id="alice")
        k_bob = self._key(cache_scope="private", user_id="bob")
        assert k_alice != k_bob

    def test_scope_shared_vs_private_different(self):
        """同 user 下,shared 和 private 生成不同 key(避免历史 shared 缓存
        误命中新升 private 的请求)。"""
        k_shared = self._key(cache_scope="shared", user_id="alice")
        k_private = self._key(cache_scope="private", user_id="alice")
        assert k_shared != k_private

    def test_pipeline_kind_isolation(self):
        """understanding vs generation 严格隔离,防止跨管道结果污染。"""
        k_u = self._key(pipeline_kind="understanding")
        k_g = self._key(pipeline_kind="generation")
        assert k_u != k_g

    def test_tenant_isolation(self):
        """不同 tenant_id 生成不同 key。"""
        k_a = self._key(tenant_id="org_a")
        k_b = self._key(tenant_id="org_b")
        assert k_a != k_b

    def test_auto_model_kept_as_is(self):
        """model='auto' 不做 family 抽取,保留原样。"""
        k_auto = self._key(model="auto")
        k_gpt = self._key(model="gpt-4o")
        assert k_auto != k_gpt

    def test_key_is_stable(self):
        """同参数多次调用生成相同 key(确定性)。"""
        k1 = self._key(temperature=0.7, max_tokens=1024, user_id="alice")
        k2 = self._key(temperature=0.7, max_tokens=1024, user_id="alice")
        assert k1 == k2

    def test_prompt_normalize_effect(self):
        """caller 用 _normalize_prompt 归一化后,全半角/空白差异应等价。
        (generate_cache_key 本身不重复归一化,依赖 caller。)"""
        # 说明:generate_cache_key 内部不再做归一化,依赖 dispatcher 传入
        # 已归一化的 prompt。这里验证 helper 单独工作正确即可。
        n1 = _normalize_prompt("你好   世界")
        n2 = _normalize_prompt("你好 世界")
        assert n1 == n2

        k1 = self._key(normalized_prompt=n1)
        k2 = self._key(normalized_prompt=n2)
        assert k1 == k2

    def test_key_length(self):
        """输出必须是 64 位 hex SHA-256。"""
        k = self._key()
        assert len(k) == 64
        assert all(c in "0123456789abcdef" for c in k)
