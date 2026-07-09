"""读取官方 CodeGraph SQLite 图谱库并查询符号级 callers/callees/imports.

基于 upstream SQLite schema（通过 `codegraph init` 生成的 `.codegraph/codegraph.db`）：
- nodes(id, kind, name, qualified_name, file_path, language, start_line, end_line, ...)
- edges(source, target, kind, metadata, line, col, ...)
- files(path, language, ...)

我们只依赖稳定的几个字段：
- `edges.kind='calls'` 表示调用边
- `edges.kind='contains'` 连接 file node / symbol node / import node
- `nodes.kind` 可能是 file / function / class / import / method / ...

对外暴露两类接口：
- strict import-time API：任何 graph query 问题都抛异常，导入整体 failed
- tolerant retrieval-time API：任何 graph query 问题都返回空结果，不打断主链路
"""
from __future__ import annotations

import sqlite3
from collections import deque
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


def _find_symbol_node(
    conn: sqlite3.Connection,
    file_path: str,
    symbol_name: str,
) -> Optional[sqlite3.Row]:
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
        WHERE file_path = ? AND name = ?
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


def _classify_chunk_type(kind: str, symbol_name: str | None, chunk_text: str) -> str:
    if kind in ('function', 'method'):
        return 'function'
    if kind == 'class':
        return 'class'
    if chunk_text.lstrip().startswith('class '):
        return 'class'
    if symbol_name:
        return 'function'
    return 'module'


def lookup_symbol_metadata_strict(
    graph_db_path: str,
    file_path: str,
    symbol_name: str | None,
    chunk_text: str,
) -> dict[str, Any]:
    """严格版：用于 import 阶段。任何 query/schema/IO 问题都抛异常。"""
    if not symbol_name:
        return dict(_EMPTY_METADATA)

    with _open_db(graph_db_path) as conn:
        node = _find_symbol_node(conn, file_path, symbol_name)
        imports = _get_imports_for_file(conn, file_path)
        if node is None:
            result = dict(_EMPTY_METADATA)
            result['imports'] = imports
            return result

        node_id = str(node['id'])
        kind = str(node['kind'] or 'module')
        callers = _get_calls_from(conn, target_id=node_id)
        callees = _get_calls_from(conn, source_id=node_id)
        chunk_type = _classify_chunk_type(kind, symbol_name, chunk_text)
        return {
            'callers': callers,
            'callees': callees,
            'imports': imports,
            'chunk_type': chunk_type,
            'function_name': symbol_name if chunk_type == 'function' else None,
            'class_name': symbol_name if chunk_type == 'class' else None,
        }


def lookup_symbol_metadata(
    graph_db_path: str,
    file_path: str,
    symbol_name: str | None,
    chunk_text: str,
) -> dict[str, Any]:
    """宽松版：用于 retrieval 阶段。任何问题都降级为空 metadata。"""
    try:
        return lookup_symbol_metadata_strict(graph_db_path, file_path, symbol_name, chunk_text)
    except Exception:
        return dict(_EMPTY_METADATA)


def lookup_related_symbols_strict(
    graph_db_path: str,
    file_path: str,
    symbol_name: str,
    *,
    hops: int = 1,
) -> list[dict[str, Any]]:
    """严格版：按 calls 图做有限跳 BFS，返回邻接符号。

    返回项：{file_path, symbol_name, kind, start_line, end_line}
    - hops=1：直接 callers/callees
    - hops=2：邻居的邻居
    """
    if hops <= 0:
        return []

    with _open_db(graph_db_path) as conn:
        root = _find_symbol_node(conn, file_path, symbol_name)
        if root is None:
            return []

        visited = {str(root['id'])}
        queue = deque([(str(root['id']), 0)])
        related: list[dict[str, Any]] = []

        while queue:
            node_id, depth = queue.popleft()
            if depth >= hops:
                continue
            rows = conn.execute(
                """
                SELECT DISTINCT n.id, n.kind, n.name, n.file_path, n.start_line, n.end_line
                FROM (
                  SELECT target AS neighbor_id FROM edges WHERE kind = 'calls' AND source = ?
                  UNION
                  SELECT source AS neighbor_id FROM edges WHERE kind = 'calls' AND target = ?
                ) q
                JOIN nodes n ON n.id = q.neighbor_id
                WHERE n.name IS NOT NULL AND n.name != ''
                """,
                (node_id, node_id),
            ).fetchall()
            for row in rows:
                rid = str(row['id'])
                if rid in visited:
                    continue
                visited.add(rid)
                queue.append((rid, depth + 1))
                related.append(
                    {
                        'file_path': str(row['file_path']),
                        'symbol_name': str(row['name']),
                        'kind': str(row['kind']),
                        'start_line': int(row['start_line'] or 1),
                        'end_line': int(row['end_line'] or 1),
                    }
                )
        return related


def lookup_related_symbols(
    graph_db_path: str,
    file_path: str,
    symbol_name: str,
    *,
    hops: int = 1,
) -> list[dict[str, Any]]:
    try:
        return lookup_related_symbols_strict(
            graph_db_path, file_path, symbol_name, hops=hops
        )
    except Exception:
        return []
