"""L2 BM25 近似匹配 — 基于 Redis Stack RediSearch 文本索引.

背景:
    L2 缓存从精确 SHA-256 哈希匹配改为 BM25 近似匹配。用户反馈很少发
    完全相同的画，精确匹配命中率低。本模块在 Redis Stack 上建 RediSearch
    文本索引，对 normalized_prompt 做 BM25 全文检索，命中后直接从同一条
    Hash 记录中取 response_json。

索引设计:
    索引名: aigateway:l2:idx
    Hash 前缀: aigateway:cache:v2search:
    LANGUAGE_FIELD: doc_lang (每条文档读 doc_lang 字段值决定分词语言)
    字段:
        - normalized_prompt: TEXT (BM25 打分目标)
        - doc_lang:          隐式语言字段 (写 chinese 触发 Friso 中文分词)
        - pipeline_kind:     TAG  (隔离 understanding/generation)
        - model_family:      TAG
        - pipeline_version:  TAG  (路由/管道语义版本)
        - cache_scope:       TAG  (private/group/public)
        - scope_id:          TAG  (user_id 或 group_id, 按 scope 过滤)
        - response_json:     随 Hash 存储、可 return_fields 取回，但不进 schema 索引
        - created_at:        NUMERIC

注:
    normalized_prompt 是 dispatcher 传来的 JSON 序列化 messages 数组
    (含大量 JSON 语法字符)。BM25 分词前需抽回纯文本，否则括号引号
    污染词项。见 ``_extract_plain_text``。

    **中文分词**: RediSearch 默认分词器只按空白/标点切词，对中文整段
    当一个 token，导致原样重发都不命中。索引声明 ``LANGUAGE_FIELD
    doc_lang`` + 文档写 ``doc_lang=chinese`` + 查询 ``.language("chinese")``
    三处配合，启用 RediSearch 内置 Friso 词典分词（无需 jieba 等 Python
    依赖）。完全相同/高度重叠的中文 prompt 高分命中，不相关的不命中。

    所有方法在 RediSearch 不可用 / 查询失败时返回 None / 空结果，
    调用方据此降级，不影响可用性。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 索引常量
# ------------------------------------------------------------------

# Compatibility defaults for direct callers. Config-aware callers pass the
# resolved namespace explicitly and never mutate these module globals.
L2_INDEX_NAME = "aigateway:l2:idx:v3"
L2_HASH_PREFIX = "aigateway:cache:v3search:"

# BM25 默认阈值。实测分数分布（response_json 不进 schema 后）：
#   完全相同 prompt ~5、近重复 ~4.75、核心子串 ~3、单核心词 ~2.25、完全不相关 0。
#   1.5 落在"命中区下沿"，过滤噪声词误命中，又保留近重复命中。需按实际语料调参。
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
        return normalized_messages_json
    if not isinstance(msgs, list):
        return normalized_messages_json
    parts: list[str] = []
    for message in msgs:
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                parts.append(content)
            elif isinstance(content, list):
                for segment in content:
                    if isinstance(segment, dict):
                        text = segment.get("text") or segment.get("content")
                        if isinstance(text, str) and text.strip():
                            parts.append(text)
    return " ".join(parts)


def _escape_tag(value: str) -> str:
    """转义 RediSearch TAG 存储值中的特殊字符。"""
    if not value:
        return "_"
    out = []
    for char in str(value):
        if char.isalnum() or char in "_-./":
            out.append(char)
        else:
            out.append("_")
    return "".join(out) or "_"


def _escape_tag_query(value: str) -> str:
    """转义 TAG 查询子句中的特殊字符。"""
    escaped = _escape_tag(value)
    out = []
    for char in escaped:
        if char in "-.":
            out.append("\\" + char)
        else:
            out.append(char)
    return "".join(out) or "_"


async def ensure_index(
    client: Any,
    *,
    index_name: str = L2_INDEX_NAME,
    hash_prefix: str = L2_HASH_PREFIX,
) -> bool:
    """幂等创建 L2 BM25 索引。

    ``index_name`` 和 ``hash_prefix`` 是显式调用参数，配置热更新不会通过
    修改模块全局变量影响并发中的其他请求。
    """
    if client is None:
        return False
    try:
        from redis.commands.search.field import NumericField, TagField, TextField
        from redis.commands.search.index_definition import IndexDefinition, IndexType
        from redis.exceptions import ResponseError
    except ImportError as exc:
        logger.warning("L2 BM25: redis-py search 模块不可用: %s", exc)
        return False

    try:
        try:
            await client.ft(index_name).info()
            logger.info("L2 BM25 索引已存在: %s", index_name)
            return True
        except ResponseError:
            pass

        definition = IndexDefinition(
            prefix=[hash_prefix],
            index_type=IndexType.HASH,
            language_field="doc_lang",
            language="chinese",
        )
        schema = (
            TextField("normalized_prompt", weight=1.0),
            TagField("pipeline_kind", separator="|"),
            TagField("pipeline_version", separator="|"),
            TagField("model_family", separator="|"),
            TagField("cache_scope", separator="|"),
            TagField("scope_id", separator="|"),
            NumericField("created_at"),
        )
        await client.ft(index_name).create_index(schema, definition=definition)
        logger.info("L2 BM25 索引创建成功: %s", index_name)
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
    pipeline_version: str,
    model_family: str,
    cache_scope: str,
    scope_id: str,
    ttl_seconds: int = 3600,
    *,
    hash_prefix: str = L2_HASH_PREFIX,
) -> None:
    """写入 L2 BM25 缓存索引项。"""
    if client is None:
        return
    try:
        plain = _extract_plain_text(normalized_prompt)
        if not plain.strip():
            return

        redis_key = f"{hash_prefix}{key}"
        now = int(time.time())
        await client.hset(
            redis_key,
            mapping={
                "normalized_prompt": plain,
                "doc_lang": "chinese",
                "pipeline_kind": _escape_tag(pipeline_kind),
                "pipeline_version": _escape_tag(pipeline_version),
                "model_family": _escape_tag(model_family),
                "cache_scope": _escape_tag(cache_scope),
                "scope_id": _escape_tag(scope_id),
                "response_json": value[:10000],
                "created_at": now,
            },
        )
        await client.expire(redis_key, ttl_seconds)
        logger.debug("L2 BM25 写入: key=%s ttl=%ds", key[:16], ttl_seconds)
    except Exception as exc:
        logger.debug("L2 BM25 写入失败 (可忽略，不影响主流程): %s", exc)


async def search(
    client: Any,
    normalized_prompt: str,
    pipeline_kind: str,
    pipeline_version: str,
    model_family: str,
    cache_scope: str,
    scope_id: str,
    top_k: int = L2_DEFAULT_TOP_K,
    min_score: float = L2_DEFAULT_MIN_SCORE,
    *,
    index_name: str = L2_INDEX_NAME,
) -> dict[str, Any] | None:
    """BM25 全文检索相似 prompt，返回命中结果 dict。"""
    if client is None:
        return None
    plain = _extract_plain_text(normalized_prompt)
    if not plain.strip():
        return None

    filter_clauses = [
        f"@pipeline_kind:{{{_escape_tag_query(pipeline_kind)}}}",
        f"@pipeline_version:{{{_escape_tag_query(pipeline_version)}}}",
        f"@model_family:{{{_escape_tag_query(model_family)}}}",
        f"@cache_scope:{{{_escape_tag_query(cache_scope)}}}",
        f"@scope_id:{{{_escape_tag_query(scope_id)}}}",
    ]
    filter_part = " ".join(filter_clauses)

    query_text = _escape_query_text(plain)
    if not query_text:
        return None
    query_str = f"{filter_part} @normalized_prompt:{query_text}"

    try:
        from redis.commands.search.query import Query

        query = (
            Query(query_str)
            .language("chinese")
            .return_fields("response_json", "model_family", "pipeline_kind")
            .with_scores()
            .paging(0, top_k)
        )
        result = await client.ft(index_name).search(query)
    except Exception as exc:
        logger.debug("L2 BM25 查询失败 (降级无缓存): %s", exc)
        return None

    docs = getattr(result, "docs", None) or []
    if not docs:
        return None

    best_doc = docs[0]
    score = getattr(best_doc, "score", None)
    try:
        score_value = float(score) if score is not None else 0.0
    except (TypeError, ValueError):
        score_value = 0.0

    if score_value < min_score:
        logger.debug(
            "L2 BM25 最高分 %.4f < 阈值 %.4f，视为 miss",
            score_value,
            min_score,
        )
        return None

    response_json = getattr(best_doc, "response_json", None)
    if isinstance(response_json, bytes):
        response_json = response_json.decode("utf-8", errors="replace")

    return {
        "id": best_doc.id,
        "score": score_value,
        "response_json": response_json or "",
    }


def _escape_query_text(text: str) -> str:
    """转义 BM25 查询串中的 RediSearch 特殊字符，保留中文交给 Friso 分词。"""
    if not text:
        return ""
    out = []
    for char in text:
        if char in ':()"|-*$.\\':
            out.append("\\" + char)
        else:
            out.append(char)
    return "".join(out).strip()


__all__ = [
    "L2_DEFAULT_MIN_SCORE",
    "L2_DEFAULT_TOP_K",
    "L2_HASH_PREFIX",
    "L2_INDEX_NAME",
    "_escape_query_text",
    "_escape_tag",
    "_escape_tag_query",
    "_extract_plain_text",
    "ensure_index",
    "search",
    "store",
]
