"""CodeGraph 图谱库构建 wrapper.

一个仓库一份 SQLite 图谱: graph_db_dir/<document_id>.db
构建失败会向上抛 —— import 是 strict 的,retrieval 才是 tolerant 的。
codegraph 走 lazy import,便于开发机在不装该包时仍能加载本模块。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any


def build_code_graph(source_dir: str, graph_db_path: str) -> str:
    """在 source_dir 上跑 CodeGraph 预索引,把 SQLite 图谱写到 graph_db_path.

    返回真实落盘的 graph_db_path。父目录不存在会先建。
    构建/落盘失败时向上抛异常,由调用方(_run_code_import_task)标记任务失败。
    """
    Path(graph_db_path).parent.mkdir(parents=True, exist_ok=True)

    # codegraph 的 Python API 在 pypi 上有多个实现;此处按最常见的公开约定调用,
    # 若实际安装版本 API 略有差异,由集成阶段做最小对齐(仍禁止手写 tree-sitter)。
    from codegraph import CodeGraph  # type: ignore[import-not-found]

    graph: Any = CodeGraph(source_dir)
    graph.build()
    graph.save(graph_db_path)
    return graph_db_path
