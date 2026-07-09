"""
Qdrant 初始化脚本
=================

在首次部署时创建 Qdrant 集合：
- semantic_cache: 语义缓存集合（DB_SCHEMA Qdrant §1）
- rag_documents: RAG 文档预留集合（DB_SCHEMA Qdrant §2）

用法:
    python scripts/init_qdrant.py [--url http://localhost:6333]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

# 将项目根目录加入 PYTHONPATH
sys.path.insert(0, "/home/ubuntu/gateway2/aigateway-core/src")

from aigateway_core.shared.qdrant_client import QdrantClientManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("init_qdrant")


def _print_step(label: str) -> None:
    """打印步骤标题。"""
    print(f"\n{'=' * 60}")
    print(f"  [{label}]")
    print(f"{'=' * 60}")


async def create_semantic_cache(qmgr: QdrantClientManager) -> None:
    """创建 semantic_cache 集合（DB_SCHEMA Qdrant §1）。

    向量配置:
    - size: 1024（Qwen3-Embedding-0.6B 输出维度）
    - distance: COSINE（余弦相似度）
    - hnsw_config.m: 16
    - hnsw_config.ef_construct: 128
    """
    _print_step("创建 semantic_cache 集合")

    success = await qmgr.upsert_collection(
        name="semantic_cache",
        size=1024,
        distance="COSINE",
    )

    if success:
        print("  集合 'semantic_cache' 创建成功")
        print("  向量配置:")
        print("    size:       1024 (Qwen3-Embedding-0.6B)")
        print("    distance:   COSINE")
        print("    hnsw.m:     16")
        print("    hnsw.ef_construct: 128")
        print("\n  Payload Schema:")
        print("    prompt_hash        string  Keyword  输入 prompt 的 SHA-256")
        print("    prompt_normalized  string  Keyword  归一化 prompt 文本")
        print("    model              string  Keyword  模型名称")
        print("    response_json      string  —       完整 OpenAI 响应 JSON")
        print("    user_id            string  Keyword  用户 ID（多租户隔离）")
        print("    created_at         integer Integer  Unix 时间戳（秒）")
        print("    ttl                integer Integer  过期时间戳（Unix 秒）")
        print("    hit_count          integer Integer  命中次数")
        print("    token_count        integer Integer  响应 token 数")
        print("    cache_tier         string  Keyword  固定值 L3")
        print("    embedding_model    string  Keyword  嵌入模型名")
    else:
        print("  [警告] 集合创建可能失败，请检查日志")


async def create_rag_collection(qmgr: QdrantClientManager) -> None:
    """创建 rag_documents 预留集合（DB_SCHEMA Qdrant §2）。

    注意：MVP 阶段此集合预留位置，实际检索引擎延后启用。
    """
    _print_step("创建 rag_documents 预留集合")

    success = await qmgr.upsert_collection(
        name="rag_documents",
        size=1024,
        distance="COSINE",
    )

    if success:
        print("  集合 'rag_documents' 创建成功（预留，MVP 不启用）")
        print("\n  Payload Schema（预留）:")
        print("    document_id  string  Keyword  文档唯一 ID（UUID）")
        print("    user_id      string  Keyword  所属用户 ID")
        print("    filename     string  Keyword  原始文件名")
        print("    file_type    string  Keyword  pdf | txt | csv | json | markdown")
        print("    chunk_index  integer Integer  文档内分块索引")
        print("    chunk_text   string  Keyword  分块文本内容")
        print("    metadata     object  —       JSON 附加元数据")
        print("    created_at   integer Integer  Unix 时间戳（秒）")
        print("    deleted      boolean Keyword  软删除标记（默认 false）")
    else:
        print("  [警告] 集合创建可能失败，请检查日志")


async def main() -> None:
    """主入口。"""
    parser = argparse.ArgumentParser(description="初始化 Qdrant 集合")
    parser.add_argument(
        "--url",
        default="http://localhost:6333",
        help="Qdrant REST API 地址 (默认: http://localhost:6333)",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("  AI Gateway Qdrant 初始化")
    print(f"  连接地址: {args.url}")
    print("=" * 60)

    qmgr = QdrantClientManager()

    try:
        # 1. 连接 Qdrant
        _print_step("连接 Qdrant")
        await qmgr.connect(args.url)
        print("  Qdrant 连接成功")

        # 2. 创建语义缓存集合
        await create_semantic_cache(qmgr)

        # 3. 创建 RAG 文档预留集合
        await create_rag_collection(qmgr)

        print(f"\n{'=' * 60}")
        print("  初始化完成！")
        print(f"{'=' * 60}\n")

    except ConnectionError as exc:
        print(f"\n  [错误] Qdrant 连接失败: {exc}")
        print("  请确保 Qdrant 服务正在运行")
        sys.exit(1)
    except Exception as exc:
        print(f"\n  [错误] 初始化失败: {exc}")
        logger.exception("初始化异常")
        sys.exit(1)
    finally:
        await qmgr.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
