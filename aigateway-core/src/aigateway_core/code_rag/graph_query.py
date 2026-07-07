"""从 CodeGraph SQLite 图谱库读取符号级 callers/callees/imports.

对外只暴露 lookup_symbol_metadata,返回一个规范化 dict,方便:
1) import 时把结果并入 Qdrant payload
2) 检索时根据 hit.function_name 做图谱多跳展开

无法解析(缺 symbol_name / 图谱不存在 / 表结构变化)时返回空关系,
上层按"tolerant on retrieval"策略继续跑。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

_EMPTY_METADATA: dict[str, Any] = {
    "callers": [],
    "callees": [],
    "imports": [],
    "chunk_type": "module",
    "function_name": None,
    "class_name": None,
}


def _classify_chunk(symbol_name: str | None, chunk_text: str) -> str:
    """粗粒度判断 chunk 类型: class / function / module."""
    if not symbol_name:
        return "module"
    head = chunk_text.lstrip()
    if head.startswith("class "):
        return "class"
    return "function"


def _query_relationships(
    db_path: str, symbol_name: str
) -> tuple[list[str], list[str], list[str]]:
    """尝试从图谱库读 callers/callees/imports.

    真实 codegraph 的 schema 会随版本迭代;此函数只做尽力而为的解析,
    任何 sqlite 错误或表缺失都退化为空列表(retrieval 层容忍)。
    """
    if not Path(db_path).exists():
        return ([], [], [])

    callers: list[str] = []
    callees: list[str] = []
    imports: list[str] = []
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()

            # 相同符号出现多次时,只取字符串列存在的表;找不到就跳过。
            for query, sink in (
                ("SELECT caller FROM calls WHERE callee = ?", callers),
                ("SELECT callee FROM calls WHERE caller = ?", callees),
                ("SELECT module FROM imports WHERE symbol = ?", imports),
            ):
                try:
                    rows = cursor.execute(query, (symbol_name,)).fetchall()
                except sqlite3.Error:
                    continue
                sink.extend(str(row[0]) for row in rows if row and row[0])
    except sqlite3.Error:
        return ([], [], [])

    return (sorted(set(callers)), sorted(set(callees)), sorted(set(imports)))


def lookup_symbol_metadata(
    graph_db_path: str,
    file_path: str,
    symbol_name: str | None,
    chunk_text: str,
) -> dict[str, Any]:
    """返回 chunk 关联的调用图元数据.

    参数:
      graph_db_path: CodeGraph SQLite 图谱库路径
      file_path:     chunk 所在文件相对路径(便于未来做 file 维度过滤)
      symbol_name:   函数/类名;为 None 或空串时视为模块级 chunk
      chunk_text:    原始 chunk 文本(用于粗粒度分类)
    """
    if not symbol_name:
        result = dict(_EMPTY_METADATA)
        return result

    callers, callees, imports = _query_relationships(graph_db_path, symbol_name)
    chunk_type = _classify_chunk(symbol_name, chunk_text)
    return {
        "callers": callers,
        "callees": callees,
        "imports": imports,
        "chunk_type": chunk_type,
        "function_name": symbol_name if chunk_type != "class" else None,
        "class_name": symbol_name if chunk_type == "class" else None,
    }
