"""L2 BM25 缓存搜索单元测试

覆盖 L2 BM25 近似匹配的辅助函数和降级路径。
"""

import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.prefix.cache.l2_search import (
    L2_DEFAULT_MIN_SCORE,
    L2_DEFAULT_TOP_K,
    L2_HASH_PREFIX,
    L2_INDEX_NAME,
    _extract_plain_text,
    _escape_query_text,
    _escape_tag,
    _escape_tag_query,
    ensure_index,
    search,
    store,
)


# ==================================================================
# _extract_plain_text Tests
# ==================================================================


class TestExtractPlainText:
    """测试从 JSON 序列化 messages 中提取纯文本。"""

    def test_empty_input(self):
        assert _extract_plain_text("") == ""

    def test_invalid_json_returns_raw(self):
        """非 JSON 字符串原样返回。"""
        raw = "hello world"
        assert _extract_plain_text(raw) == raw

    def test_not_list_returns_raw(self):
        """JSON 对象（非列表）原样返回。"""
        raw = '{"role": "user"}'
        assert _extract_plain_text(raw) == raw

    def test_simple_messages(self):
        """简单消息列表提取纯文本。"""
        msgs = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hello"},
        ]
        result = _extract_plain_text(json.dumps(msgs))
        assert "You are helpful" in result
        assert "Hello" in result

    def test_multimodal_content(self):
        """多模态 content 提取 text block。"""
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "https://img/x.png"}},
                    {"type": "text", "text": "描述这张图"},
                ],
            }
        ]
        result = _extract_plain_text(json.dumps(msgs))
        assert "描述这张图" in result
        # image_url 不应出现在纯文本中
        assert "image_url" not in result.lower()

    def test_empty_content_skipped(self):
        """空 content 被跳过。"""
        msgs = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "Response"},
        ]
        result = _extract_plain_text(json.dumps(msgs))
        assert "Response" in result
        assert result.strip() == "Response"

    def test_mixed_content_blocks(self):
        """混合 content blocks 只提取 text 类型。"""
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "第一句"},
                    {"type": "input_image", "image_url": {"url": "x"}},
                    {"type": "text", "text": "第二句"},
                ],
            }
        ]
        result = _extract_plain_text(json.dumps(msgs))
        assert "第一句" in result
        assert "第二句" in result


# ==================================================================
# _escape_tag Tests
# ==================================================================


class TestEscapeTag:
    """测试 TAG 字段存储值转义。"""

    def test_empty_value(self):
        assert _escape_tag("") == "_"

    def test_normal_value(self):
        assert _escape_tag("understanding") == "understanding"

    def test_special_chars_replaced(self):
        """空格、逗号、管道符等被替换为下划线。"""
        result = _escape_tag("hello world|foo,bar{baz}")
        assert " " not in result
        assert "|" not in result
        assert "," not in result
        assert "{" not in result

    def test_alphanumeric_preserved(self):
        """字母数字保留。"""
        assert _escape_tag("agnes-2.0-flash") == "agnes-2.0-flash"

    def test_slash_preserved(self):
        """斜杠保留。"""
        assert _escape_tag("pipeline/kind") == "pipeline/kind"


# ==================================================================
# _escape_tag_query Tests
# ==================================================================


class TestEscapeTagQuery:
    """测试 TAG 查询子句转义。"""

    def test_hyphen_escaped(self):
        """连字符在查询中被反斜杠转义。"""
        result = _escape_tag_query("agnes-2.0-flash")
        assert "\\-" in result
        assert "\\." in result

    def test_normal_value_unmodified(self):
        """无特殊字符的值不变。"""
        assert _escape_tag_query("understanding") == "understanding"


# ==================================================================
# _escape_query_text Tests
# ==================================================================


class TestEscapeQueryText:
    """测试 BM25 查询串转义。"""

    def test_empty_text(self):
        assert _escape_query_text("") == ""

    def test_special_chars_escaped(self):
        """RediSearch 特殊字符被反斜杠转义（断言转义后的形式，防止回归到不转义）。"""
        result = _escape_query_text('hello:"world"|test')
        # 必须出现转义形式 \: \" \| —— 未转义时这些反斜杠不存在
        assert "\\:" in result
        assert '\\"' in result
        assert "\\|" in result

    def test_chinese_preserved(self):
        """中文文本保持原样。"""
        result = _escape_query_text("画一只猫")
        assert result == "画一只猫"

    def test_bare_query_no_quoting(self):
        """Friso 模式: 文本作为裸查询串返回，不按空格拆词、不加双引号。

        回归守护: 旧实现 `text.split()` + `f'"{w}"'` 会对每个词加引号，
        对中文无效（中文无空格，整段当一个精确短语，连原样重发都返回 0）。
        此测试确保不会回退到 split+quote。
        """
        # 混合 CJK + ASCII + 空格: 必须保持裸串，不出现任何双引号
        result = _escape_query_text("画一只 cat 猫")
        assert result == "画一只 cat 猫"
        assert '"' not in result
        # 纯 CJK 也不应有引号
        assert '"' not in _escape_query_text("画一只猫")

    def test_at_brace_not_escaped_but_scope_safe(self):
        """@ { } 不在转义集里，但安全: RediSearch 的 @field: 作用域会吞掉
        后续 token（含注入的 @scope_id:{...}），无法突破 TAG scope 过滤。
        此测试仅记录转义行为（这些字符原样保留），scope 隔离由集成测试保证。
        """
        result = _escape_query_text("x @scope_id:{grp-admin-team}")
        # @ { } 原样保留（未转义）；但 : 被转义
        assert "@" in result
        assert "{" in result
        assert "}" in result
        assert "\\:" in result  # 冒号仍被转义

    def test_strip_whitespace(self):
        """前后空白被去除。"""
        assert _escape_query_text("  hello  ") == "hello"


# ==================================================================
# ensure_index Tests
# ==================================================================


class TestEnsureIndex:
    """测试 L2 BM25 索引创建。"""

    @pytest.mark.asyncio
    async def test_none_client(self):
        """None client 返回 False。"""
        assert await ensure_index(None) is False

    @pytest.mark.asyncio
    async def test_existing_index(self):
        """已存在的索引直接返回 True。"""
        client = MagicMock()
        ft = MagicMock()
        ft.info = AsyncMock(return_value={"index_name": L2_INDEX_NAME})
        client.ft.return_value = ft

        result = await ensure_index(client)
        assert result is True
        ft.info.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_create_new_index(self):
        """新建索引时调用 create_index，且 Friso 配置 + response_json 不进 schema。"""
        client = MagicMock()
        ft = MagicMock()

        # Simulate ResponseError for non-existent index
        class FakeResponseError(Exception):
            pass

        ft.info = AsyncMock(side_effect=FakeResponseError("no such index"))
        ft.create_index = AsyncMock(return_value=True)
        client.ft.return_value = ft

        # 用真实 redis-py 类，捕获 IndexDefinition 的 language_field/language 参数，
        # 并让 schema 字段有真实 .name 属性可断言。
        from redis.commands.search.field import NumericField, TagField, TextField
        from redis.commands.search.index_definition import IndexDefinition, IndexType

        with patch.dict("sys.modules", {
            "redis.commands.search.field": MagicMock(
                NumericField=NumericField, TagField=TagField, TextField=TextField
            ),
            "redis.commands.search.index_definition": MagicMock(
                IndexDefinition=IndexDefinition, IndexType=IndexType
            ),
            "redis.exceptions": MagicMock(ResponseError=FakeResponseError),
        }):
            result = await ensure_index(client)

        assert result is True
        ft.create_index.assert_called_once()

        # IndexDefinition 必须带 Friso 中文分词配置（language_field + 默认 language）
        # redis-py IndexDefinition 把参数展平成 FT.CREATE 风格的 args 列表
        _, kwargs = ft.create_index.call_args
        definition = kwargs["definition"]
        args = definition.args
        assert "LANGUAGE_FIELD" in args
        assert args[args.index("LANGUAGE_FIELD") + 1] == "doc_lang"
        assert "LANGUAGE" in args
        assert args[args.index("LANGUAGE") + 1] == "chinese"

        # schema 里不能有 response_json（否则稀释 BM25 分数）
        schema = ft.create_index.call_args.args[0]
        field_names = [getattr(f, "name", None) for f in schema]
        assert "response_json" not in field_names
        assert "normalized_prompt" in field_names

    @pytest.mark.asyncio
    async def test_import_error(self):
        """缺少 redis-py 模块时返回 False。"""
        client = MagicMock()

        with patch.dict("sys.modules", {"redis.commands": None}):
            result = await ensure_index(client)

        assert result is False


# ==================================================================
# store Tests
# ==================================================================


class TestStore:
    """测试 L2 BM25 写入。"""

    @pytest.mark.asyncio
    async def test_none_client(self):
        """None client 直接返回。"""
        await store(None, "key", "value", "prompt", "kind", "model", "scope", "user")
        # 无异常即通过

    @pytest.mark.asyncio
    async def test_empty_prompt(self):
        """空 prompt 不写入。"""
        client = MagicMock()
        client.hset = AsyncMock()
        client.expire = AsyncMock()

        await store(client, "key", "value", "", "kind", "model", "scope", "user")

        client.hset.assert_not_called()

    @pytest.mark.asyncio
    async def test_successful_write(self):
        """正常写入流程。"""
        client = MagicMock()
        client.hset = AsyncMock()
        client.expire = AsyncMock()

        normalized_prompt = json.dumps([
            {"role": "user", "content": "画一只猫"}
        ])

        await store(
            client, "cache-key-123", '{"choices":[]}',
            normalized_prompt, "understanding", "gpt-4o", "group", "user1"
        )

        client.hset.assert_called_once()
        call_kwargs = client.hset.call_args.kwargs
        assert call_kwargs["mapping"]["normalized_prompt"] == "画一只猫"
        assert call_kwargs["mapping"]["doc_lang"] == "chinese"
        assert call_kwargs["mapping"]["pipeline_kind"] == "understanding"
        # response_json 虽不进 RediSearch schema，但仍随 Hash 存储（查询时 return_fields 取回）
        assert call_kwargs["mapping"]["response_json"] == '{"choices":[]}'
        client.expire.assert_called_once()

    @pytest.mark.asyncio
    async def test_value_truncated(self):
        """响应值超过 10000 字符时被截断。"""
        client = MagicMock()
        client.hset = AsyncMock()
        client.expire = AsyncMock()

        long_value = "x" * 20000
        await store(client, "key", long_value, "prompt", "k", "m", "s", "u")

        call_kwargs = client.hset.call_args.kwargs
        assert len(call_kwargs["mapping"]["response_json"]) == 10000

    @pytest.mark.asyncio
    async def test_exception_logged(self):
        """写入异常被捕获，不抛出。"""
        client = MagicMock()
        client.hset = AsyncMock(side_effect=Exception("Redis error"))

        await store(client, "key", "value", "prompt", "k", "m", "s", "u")
        # 无异常即通过


# ==================================================================
# search Tests
# ==================================================================


class TestSearch:
    """测试 L2 BM25 搜索。"""

    @pytest.mark.asyncio
    async def test_none_client(self):
        """None client 返回 None。"""
        result = await search(None, "prompt", "k", "m", "s", "u")
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_prompt(self):
        """空 prompt 返回 None。"""
        client = MagicMock()
        result = await search(client, "", "k", "m", "s", "u")
        assert result is None

    @pytest.mark.asyncio
    async def test_no_results(self):
        """无命中结果返回 None。"""
        client = MagicMock()
        ft = MagicMock()

        class MockResult:
            docs = []

        ft.search = AsyncMock(return_value=MockResult())
        client.ft.return_value = ft

        result = await search(client, "unknown prompt", "k", "m", "s", "u")
        assert result is None

    @pytest.mark.asyncio
    async def test_low_score_miss(self):
        """分数低于阈值视为 miss。"""
        client = MagicMock()
        ft = MagicMock()

        class MockDoc:
            id = "doc1"
            score = 0.5  # < L2_DEFAULT_MIN_SCORE

        class MockResult:
            docs = [MockDoc()]

        ft.search = AsyncMock(return_value=MockResult())
        client.ft.return_value = ft

        result = await search(client, "prompt", "k", "m", "s", "u")
        assert result is None

    @pytest.mark.asyncio
    async def test_boundary_score_is_hit(self):
        """分数恰好等于阈值视为命中（比较是严格 <，== 阈值不算 miss）。

        回归守护: 防止把 `< min_score` 误改成 `<= min_score`，把边界命中变 miss。
        """
        client = MagicMock()
        ft = MagicMock()

        class MockDoc:
            id = "doc1"
            score = L2_DEFAULT_MIN_SCORE  # 恰好等于阈值
            response_json = b'{"choices":[]}'

        class MockResult:
            docs = [MockDoc()]

        ft.search = AsyncMock(return_value=MockResult())
        client.ft.return_value = ft

        result = await search(client, "prompt", "k", "m", "s", "u")
        assert result is not None  # == 阈值是 hit，不是 miss

    @pytest.mark.asyncio
    async def test_successful_hit(self):
        """BM25 命中返回结果，且查询带 .language('chinese')（Friso 协调点）。"""
        client = MagicMock()
        ft = MagicMock()

        class MockDoc:
            id = "doc1"
            score = 3.5
            response_json = b'{"choices":[{"message":{"content":"hi"}}]}'

        class MockResult:
            docs = [MockDoc()]

        ft.search = AsyncMock(return_value=MockResult())
        client.ft.return_value = ft

        result = await search(client, "similar prompt", "k", "m", "s", "u")

        assert result is not None
        assert result["id"] == "doc1"
        assert result["score"] == 3.5
        assert "choices" in result["response_json"]

        # 查询必须带 .language("chinese")——Friso 三处协调点之一，回归守护
        q_arg = ft.search.call_args.args[0]
        assert getattr(q_arg, "_language", None) == "chinese"

    @pytest.mark.asyncio
    async def test_search_exception_fallback(self):
        """查询异常时返回 None（降级）。"""
        client = MagicMock()
        ft = MagicMock()
        ft.search = AsyncMock(side_effect=Exception("RediSearch error"))
        client.ft.return_value = ft

        result = await search(client, "prompt", "k", "m", "s", "u")
        assert result is None
