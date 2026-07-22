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
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# 索引常量
# ------------------------------------------------------------------

L2_INDEX_NAME = "aigateway:l2:idx"
L2_HASH_PREFIX = "aigateway:cache:v2search:"

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
            # LANGUAGE_FIELD: 每条 Hash 文档读 doc_lang 字段值作为分词语言。
            # RediSearch 内置 Friso 中文分词库，doc_lang=chinese 时对 CJK 文本
            # 做词典分词（而非默认的空白/标点切分，否则中文整段当一个 token 无法命中）。
            language_field="doc_lang",
            language="chinese",
        )
        schema = (
            TextField("normalized_prompt", weight=1.0),
            TagField("pipeline_kind", separator="|"),
            TagField("model_family", separator="|"),
            TagField("cache_scope", separator="|"),
            TagField("scope_id", separator="|"),
            NumericField("created_at"),
            # 注意: response_json **不**进 schema。它仍随 Hash 存储、查询时经
            # .return_fields("response_json") 取回，但不被 RediSearch 索引。
            # 若声明为 TEXT，其庞杂内容会稀释 BM25 词项 IDF，导致 normalized_prompt
            # 的命中分数从 ~5 掉到 ~0.4（实测），低于 min_score 阈值→永远 miss。
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
                # doc_lang 与索引 LANGUAGE_FIELD 对应，触发 Friso 中文分词
                "doc_lang": "chinese",
                "pipeline_kind": _escape_tag(pipeline_kind),
                "model_family": _escape_tag(model_family),
                "cache_scope": _escape_tag(cache_scope),
                "scope_id": _escape_tag(scope_id),
                # 截断避免 payload 过大。注: 截断可能切在 JSON 中间，
                # 极端长响应命中缓存时下游 json.loads 会失败 → 走 miss 重算。
                # 10000 字符覆盖绝大多数 chat completion 响应；超长响应本就少见。
                "response_json": value[:10000],
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

    # BM25 查询串: 过滤子句 + 全文匹配。
    # _escape_query_text 保留中文原样（交 Friso 分词），只转义 RediSearch 特殊符。
    # Query 加 .language("chinese") 让查询侧也走 Friso，与索引侧一致。
    query_text = _escape_query_text(plain)
    if not query_text:
        return None
    query_str = f"{filter_part} @normalized_prompt:{query_text}"

    try:
        from redis.commands.search.query import Query

        q = (
            Query(query_str)
            .language("chinese")
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
    """转义 BM25 查询串中的 RediSearch 特殊字符，保留中文交给 Friso 分词。

    RediSearch 全文查询里 ``:`` ``(" ")`` ``|`` ``-`` ``$`` ``*`` 等有特殊含义。
    本函数把整段文本作为**裸查询串**返回（不按空格拆、不加引号），让索引侧
    的 Friso 中文分词器（``LANGUAGE chinese``）对 CJK 文本做词典分词后按默认
    AND 匹配。这样完全相同/高度重叠的中文 prompt 能高分命中，不相关的不命中。

    之前按空格拆词+双引号包的做法对中文无效：中文无空格，整段被当成一个
    精确短语，与文档分词后的 token 无法匹配（连原样重发都返回 0）。

    特殊符统一用反斜杠转义；空文本返回空串（调用方据此判 miss）。
    """
    if not text:
        return ""
    # 转义 RediSearch 查询语法特殊符。Friso 分词在转义之后、查询解析之前进行，
    # 不会受影响——它只对 \w 与 CJK 字符切词，这些符号会被 Friso 当分隔符丢弃。
    out = []
    for ch in text:
        if ch in ':()"|-*$.\\':
            out.append("\\" + ch)
        else:
            out.append(ch)
    escaped = "".join(out).strip()
    return escaped


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
