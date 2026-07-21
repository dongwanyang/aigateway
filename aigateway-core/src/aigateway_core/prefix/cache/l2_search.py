"""L2 BM25 近似匹配 — 基于 Redis Stack RediSearch 文本索引.

背景:
    L2 缓存从精确 SHA-256 哈希匹配改为 BM25 近似匹配。用户反馈很少发
    完全相同的画，精确匹配命中率低。本模块在 Redis Stack 上建 RediSearch
    文本索引，对 normalized_prompt 做 BM25 全文检索，命中后直接从同一条
    Hash 记录中取 response_json。

索引设计:
    索引名: aigateway:l2:idx
    Hash 前缀: aigateway:cache:v2search:
    字段:
        - normalized_prompt: TEXT (BM25 打分目标)
        - pipeline_kind:    TAG  (隔离 understanding/generation)
        - model_family:     TAG
        - cache_scope:      TAG  (private/group/public)
        - scope_id:         TAG  (user_id 或 group_id, 按 scope 过滤)
        - response_json:    TEXT (OpenAI 格式响应 JSON)
        - created_at:       NUMERIC

注:
    normalized_prompt 是 dispatcher 传来的 JSON 序列化 messages 数组
    (含大量 JSON 语法字符)。BM25 分词前需抽回纯文本，否则括号引号
    污染词项。见 ``_extract_plain_text``。

    所有方法在 RediSearch 不可用 / 查询失败时返回 None / 空结果，
    调用方据此降级，不影响可用性。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 索引常量
# ------------------------------------------------------------------

L2_INDEX_NAME = "aigateway:l2:idx"
L2_HASH_PREFIX = "aigateway:cache:v2search:"

# BM25 默认阈值。BM25 分数无上界，此值是保守起点，需按实际语料调参。
L2_DEFAULT_MIN_SCORE = 1.5
L2_DEFAULT_TOP_K = 5


def _extract_plain_text(normalized_messages_json: str) -> str:
    """从 JSON 序列化的 messages 数组抽回纯文本。

    dispatcher 把 ``[{"role":"system","content":"..."}, ...]`` JSON 序列化
    后作为 normalized_prompt。直接拿这个字符串做 BM25 会把 ``[{`` ``:`` ``,``
    等 JSON 语法符当词项，噪声极大。这里解析回结构后只拼接 content 字段。

    Args:
        normalized_messages_json: JSON 字符串，形如 ``[{"role":..,"content":..}]``。

    Returns:
        所有 content 拼接的纯文本；解析失败时返回原字符串。
    """
    if not normalized_messages_json:
        return ""
    try:
        msgs = json.loads(normalized_messages_json)
    except (json.JSONDecodeError, TypeError):
        # 非 JSON (例如已是纯文本 prompt)，原样返回
        return normalized_messages_json
    if not isinstance(msgs, list):
        return normalized_messages_json
    parts: list[str] = []
    for m in msgs:
        if isinstance(m, dict):
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                parts.append(content)
            elif isinstance(content, list):
                # 多模态消息 content 可能是 [{"type":"text","text":...}, ...]
                for seg in content:
                    if isinstance(seg, dict):
                        text = seg.get("text") or seg.get("content")
                        if isinstance(text, str) and text.strip():
                            parts.append(text)
    return " ".join(parts)


def _escape_tag(value: str) -> str:
    """转义 RediSearch TAG 存储值中的特殊字符。

    TAG 字段值含空格/逗号/``|``/``{``/``}`` 等会被解释为选择分隔符。
    这里只保留 ASCII 字母数字/下划线/连字符/点/斜杠，其余替换为下划线。
    空值映射为 ``_`` (TAG 不能为空)。

    注: 此函数用于 **存储** 时的值规范化。查询时需额外用
    ``_escape_tag_query`` 对 ``-`` ``.`` 反斜杠转义，否则 RediSearch
    查询解析器会把 ``agnes-2.0-flash`` 当成 ``agnes`` + 取反 ``2.0`` 报
    "Syntax error near -2.0"。
    """
    if not value:
        return "_"
    out = []
    for ch in str(value):
        if ch.isalnum() or ch in "_-./":
            out.append(ch)
        else:
            out.append("_")
    return "".join(out) or "_"


def _escape_tag_query(value: str) -> str:
    """转义 TAG **查询** 子句 ``{@field:{value}}`` 中的特殊字符。

    RediSearch 查询语法在 ``{...}`` 选择列表里，``-`` 是取反前缀、``.``
    是字段修饰符分隔、``,``/``|`` 是 OR 分隔。``_escape_tag`` 保留的
    ``-`` ``.`` 在查询时必须反斜杠转义，否则报 Syntax error。

    对 ``_escape_tag`` 的输出再做一遍 ``-``/``.`` → ``\\-``/``\\.`` 转义。
    """
    escaped = _escape_tag(value)
    # 反斜杠转义查询语法里的特殊符；其余字符 _escape_tag 已过滤为安全集
    out = []
    for ch in escaped:
        if ch in "-.":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out) or "_"


async def ensure_index(client: Any) -> bool:
    """幂等创建 L2 BM25 索引。

    Args:
        client: ``redis.asyncio.Redis`` 实例 (需 redis-stack 提供 RediSearch 模块)。

    Returns:
        True 表示索引就绪；False 表示不可用 (模块缺失/创建失败)，调用方应降级。
    """
    if client is None:
        return False
    try:
        # redis-py 8.x: index_definition (旧版是 indexDefinition)
        from redis.commands.search.field import NumericField, TagField, TextField
        from redis.commands.search.index_definition import IndexDefinition, IndexType
        from redis.exceptions import ResponseError
    except ImportError as exc:
        logger.warning("L2 BM25: redis-py search 模块不可用: %s", exc)
        return False

    try:
        # 探测索引是否已存在
        try:
            await client.ft(L2_INDEX_NAME).info()
            logger.info("L2 BM25 索引已存在: %s", L2_INDEX_NAME)
            return True
        except ResponseError:
            pass  # 索引不存在，继续创建

        definition = IndexDefinition(
            prefix=[L2_HASH_PREFIX],
            index_type=IndexType.HASH,
        )
        schema = (
            TextField("normalized_prompt", weight=1.0),
            TagField("pipeline_kind", separator="|"),
            TagField("model_family", separator="|"),
            TagField("cache_scope", separator="|"),
            TagField("scope_id", separator="|"),
            TextField("response_json"),
            NumericField("created_at"),
        )
        await client.ft(L2_INDEX_NAME).create_index(
            schema, definition=definition
        )
        logger.info("L2 BM25 索引创建成功: %s", L2_INDEX_NAME)
        return True
    except Exception as exc:
        logger.warning("L2 BM25 索引创建失败，L2 将不可用: %s", exc)
        return False


async def store(
    client: Any,
    key: str,
    value: str,
    normalized_prompt: str,
    pipeline_kind: str,
    model_family: str,
    cache_scope: str,
    scope_id: str,
    ttl_seconds: int = 3600,
) -> None:
    """写入 L2 BM25 缓存索引项。

    Args:
        client: ``redis.asyncio.Redis``。
        key: cache key hash (用于生成最终 key: aigateway:cache:v2search:{key})。
        value: OpenAI 格式响应 JSON 字符串。
        normalized_prompt: JSON 序列化的 messages 数组。
        pipeline_kind / model_family / cache_scope / scope_id: 过滤维度。
        ttl_seconds: TTL 秒数。
    """
    if client is None:
        return
    try:
        plain = _extract_plain_text(normalized_prompt)
        if not plain.strip():
            return  # 空文本无检索价值

        redis_key = f"{L2_HASH_PREFIX}{key}"
        now = int(time.time())
        await client.hset(
            redis_key,
            mapping={
                "normalized_prompt": plain,
                "pipeline_kind": _escape_tag(pipeline_kind),
                "model_family": _escape_tag(model_family),
                "cache_scope": _escape_tag(cache_scope),
                "scope_id": _escape_tag(scope_id),
                "response_json": value[:10000],  # 截断避免 payload 过大
                "created_at": now,
            },
        )
        # 设置 TTL，让索引项和响应一起过期
        await client.expire(redis_key, ttl_seconds)
        logger.debug("L2 BM25 写入: key=%s ttl=%ds", key[:16], ttl_seconds)
    except Exception as exc:
        logger.debug("L2 BM25 写入失败 (可忽略，不影响主流程): %s", exc)


async def search(
    client: Any,
    normalized_prompt: str,
    pipeline_kind: str,
    model_family: str,
    cache_scope: str,
    scope_id: str,
    top_k: int = L2_DEFAULT_TOP_K,
    min_score: float = L2_DEFAULT_MIN_SCORE,
) -> Optional[Dict[str, Any]]:
    """BM25 全文检索相似 prompt，返回命中结果 dict。

    流程:
        1. 过滤维度拼成 TAG 查询 ``@pipeline_kind:{...} @model_family:{...} ...``
        2. normalized_prompt 抽纯文本后作为 BM25 查询串
        3. ``FT.SEARCH ... WITHSCORES``，取 BM25 最高分命中
        4. score < min_score 视 miss (返回 None)

    Args:
        client: ``redis.asyncio.Redis``。
        其余: 同 store 的过滤维度 + 查询文本。
        top_k: RediSearch 返回上限。
        min_score: BM25 分数阈值。

    Returns:
        命中结果 dict {id, score, response_json}；未命中或失败返回 None。
    """
    if client is None:
        return None
    plain = _extract_plain_text(normalized_prompt)
    if not plain.strip():
        return None

    # 过滤子句 (查询侧用 _escape_tag_query 转义 - 和 .，否则 Syntax error)
    filter_clauses = [
        f"@pipeline_kind:{{{ _escape_tag_query(pipeline_kind) }}}",
        f"@model_family:{{{ _escape_tag_query(model_family) }}}",
        f"@cache_scope:{{{ _escape_tag_query(cache_scope) }}}",
        f"@scope_id:{{{ _escape_tag_query(scope_id) }}}",
    ]
    filter_part = " ".join(filter_clauses)

    # BM25 查询串: 过滤子句 + 全文匹配。纯文本需转义 RediSearch 特殊符。
    query_text = _escape_query_text(plain)
    if not query_text:
        return None
    query_str = f"{filter_part} @normalized_prompt:{query_text}"

    try:
        from redis.commands.search.query import Query

        q = (
            Query(query_str)
            .return_fields("response_json", "model_family", "pipeline_kind")
            .with_scores()
            .paging(0, top_k)
        )
        result = await client.ft(L2_INDEX_NAME).search(q)
    except Exception as exc:
        logger.debug("L2 BM25 查询失败 (降级无缓存): %s", exc)
        return None

    docs = getattr(result, "docs", None) or []
    if not docs:
        return None

    # 取最高分命中
    best_doc = docs[0]
    score = getattr(best_doc, "score", None)
    try:
        score_f = float(score) if score is not None else 0.0
    except (TypeError, ValueError):
        score_f = 0.0

    if score_f < min_score:
        logger.debug(
            "L2 BM25 最高分 %.4f < 阈值 %.4f，视为 miss", score_f, min_score
        )
        return None

    response_json = getattr(best_doc, "response_json", None)
    if isinstance(response_json, bytes):
        response_json = response_json.decode("utf-8", errors="replace")

    return {
        "id": best_doc.id,
        "score": score_f,
        "response_json": response_json or "",
    }


def _escape_query_text(text: str) -> str:
    """转义 BM25 查询串中的 RediSearch 特殊字符。

    RediSearch 全文查询里 ``:`` ``(" ")`` ``|`` ``-`` ``$`` 等有特殊含义。
    这里把文本拆词后，每个词用双引号包起来 (精确短语匹配太严，单词 OR 更
    贴近 BM25 语义)。多个词之间空格即隐含 AND/OR 由 RediSearch 默认 (AND)。
    """
    if not text:
        return ""
    # 按空白切词，过滤空词
    words = [w for w in text.split() if w]
    if not words:
        return ""
    # 用双引号包每个词，避免特殊符被解释
    quoted = [f'"{w}"' for w in words]
    return " ".join(quoted)


__all__ = [
    "L2_INDEX_NAME",
    "L2_HASH_PREFIX",
    "L2_DEFAULT_MIN_SCORE",
    "L2_DEFAULT_TOP_K",
    "ensure_index",
    "store",
    "search",
    "_extract_plain_text",
    "_escape_tag",
    "_escape_tag_query",
    "_escape_query_text",
]
