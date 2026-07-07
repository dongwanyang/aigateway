"""读取官方 CodeGraph SQLite 图谱库并查询符号级 callers/callees/imports.

基于 upstream SQLite schema（通过 `codegraph init` 生成的 `.codegraph/codegraph.db`）：
- nodes(id, kind, name, qualified_name, file_path, language, start_line, end_line, ...)
- edges(source, target, kind, metadata, line, col, ...)
- files(path, language, ...)

我们只依赖稳定的几个字段：
- `edges.kind='calls'` 表示调用边
- `edges.kind='contains'` 连接 file node / symbol node / import node
- `nodes.kind` 可能是 file / function / class / import / ...

查询策略：
1. 优先按 `(file_path, symbol_name)` 在 nodes 表定位 symbol node
2. callers: `calls` 边的 source 侧名字
3. callees: `calls` 边的 target 侧名字
4. imports: 该文件 `contains` 的所有 import 节点名
5. 若 symbol_name 为空或找不到，退化为 module 级 chunk + 空关系

与系统总策略一致：
- import 阶段：构图失败是 hard failure（由 graph_builder 抛）
- retrieval 阶段：图谱查询失败时只返回空关系（由上层 tolerant 处理）
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

_EMPTY_METADATA: dict[str, Any] = {
    "callers": [],
    "callees": [],
    "imports": [],
    "chunk_type": "module",
    "function_name": None,
    "class_name": None,
}


def _open_db(graph_db_path: str) -> sqlite3.Connection:
    if not Path(graph_db_path).exists():
        raise FileNotFoundError(graph_db_path)
    conn = sqlite3.connect(graph_db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _find_symbol_node_id(
    conn: sqlite3.Connection,
    file_path: str,
    symbol_name: str,
) -> Optional[sqlite3.Row]:
    """按文件 + 名字定位符号节点(优先 function/class，其次任意同名节点)."""
    row = conn.execute(
        """
        SELECT id, kind, name, qualified_name, file_path, language, start_line, end_line
        FROM nodes
        WHERE file_path = ?
          AND name = ?
          AND kind IN ('function', 'class', 'method')
        ORDER BY CASE kind WHEN 'function' THEN 0 WHEN 'method' THEN 1 WHEN 'class' THEN 2 ELSE 9 END,
                 start_line ASC
        LIMIT 1
        """,
        (file_path, symbol_name),
    ).fetchone()
    if row is not None:
        return row

    return conn.execute(
        """
        SELECT id, kind, name, qualified_name, file_path, language, start_line, end_line
        FROM nodes
        WHERE file_path = ?
          AND name = ?
        ORDER BY start_line ASC
        LIMIT 1
        """,
        (file_path, symbol_name),
    ).fetchone()


def _get_calls_from(
    conn: sqlite3.Connection,
    *,
    source_id: Optional[str] = None,
    target_id: Optional[str] = None,
) -> list[str]:
    if source_id is None and target_id is None:
        return []
    if source_id is not None:
        rows = conn.execute(
            """
            SELECT DISTINCT t.name
            FROM edges e
            JOIN nodes t ON t.id = e.target
            WHERE e.kind = 'calls' AND e.source = ? AND t.name IS NOT NULL AND t.name != ''
            ORDER BY t.name
            """,
            (source_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT DISTINCT s.name
            FROM edges e
            JOIN nodes s ON s.id = e.source
            WHERE e.kind = 'calls' AND e.target = ? AND s.name IS NOT NULL AND s.name != ''
            ORDER BY s.name
            """,
            (target_id,),
        ).fetchall()
    return [str(r[0]) for r in rows]


def _get_imports_for_file(conn: sqlite3.Connection, file_path: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT n.name
        FROM edges e
        JOIN nodes f ON f.id = e.source
        JOIN nodes n ON n.id = e.target
        WHERE e.kind = 'contains'
          AND f.kind = 'file'
          AND f.file_path = ?
          AND n.kind = 'import'
          AND n.name IS NOT NULL
          AND n.name != ''
        ORDER BY n.name
        """,
        (file_path,),
    ).fetchall()
    return [str(r[0]) for r in rows]


def lookup_symbol_metadata(
    graph_db_path: str,
    file_path: str,
    symbol_name: str | None,
    chunk_text: str,
) -> dict[str, Any]:
    """返回 chunk 关联的调用图元数据.

    返回字段对齐 spec / Qdrant payload:
    - callers / callees / imports
    - chunk_type
    - function_name / class_name
    """
    if not symbol_name:
        result = dict(_EMPTY_METADATA)
        return result

    try:
        with _open_db(graph_db_path) as conn:
            node = _find_symbol_node_id(conn, file_path, symbol_name)
            imports = _get_imports_for_file(conn, file_path)
            if node is None:
                result = dict(_EMPTY_METADATA)
                result['imports'] = imports
                return result

            node_id = str(node['id'])
            kind = str(node['kind'] or 'module')
            callers = _get_calls_from(conn, target_id=node_id)
            callees = _get_calls_from(conn, source_id=node_id)

            chunk_type = 'module'
            if kind in ('function', 'method'):
                chunk_type = 'function'
            elif kind == 'class':
                chunk_type = 'class'
            elif chunk_text.lstrip().startswith('class '):
                chunk_type = 'class'
            elif symbol_name:
                chunk_type = 'function'

            return {
                'callers': callers,
                'callees': callees,
                'imports': imports,
                'chunk_type': chunk_type,
                'function_name': symbol_name if chunk_type == 'function' else None,
                'class_name': symbol_name if chunk_type == 'class' else None,
            }
    except Exception:
        # retrieval 层容忍,import 层若需要严格则会在 build 阶段就失败。
        return dict(_EMPTY_METADATA)
