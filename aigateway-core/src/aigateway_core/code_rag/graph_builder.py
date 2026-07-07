"""CodeGraph 官方 CLI wrapper.

重要：这里集成的是 GitHub 文档里的官方 CodeGraph CLI / npm 包
`@colbymchenry/codegraph`，而不是我们之前误用的 Python 包 API 假设。

集成路线：
1. 在源码目录执行 `codegraph init`（会创建 `.codegraph/` 并建立索引）
2. 再执行 `codegraph index [path]` 明确触发完整索引
3. 将生成的 `.codegraph/codegraph.db` 复制到 `graph_db_path`

文档要点（来自 upstream README）：
- 支持 20+ languages
- 本地 SQLite DB 默认位于 `.codegraph/codegraph.db`
- `codegraph init` / `codegraph index [path]` 是标准 CLI 工作流
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def _run_codegraph(args: list[str], *, cwd: str) -> None:
    """调用官方 codegraph CLI；失败时抛 RuntimeError（导入链路 strict）。"""
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "codegraph CLI 未安装；请在 gateway 镜像中安装 @colbymchenry/codegraph"
        ) from exc

    if proc.returncode != 0:
        output = proc.stdout.strip()
        raise RuntimeError(
            f"codegraph command failed ({' '.join(args)}): {output[:4000]}"
        )


def build_code_graph(source_dir: str, graph_db_path: str) -> str:
    """为 source_dir 构建官方 CodeGraph SQLite 图谱，并复制到 graph_db_path。

    返回值：graph_db_path（最终 SQLite 文件绝对路径）

    失败策略：任一步失败直接抛异常，由导入任务整体标记 failed。
    """
    source_root = Path(source_dir).resolve()
    target = Path(graph_db_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    # 官方 CLI 工作流: 在项目目录执行 init + index。
    _run_codegraph(["codegraph", "init"], cwd=str(source_root))
    _run_codegraph(["codegraph", "index", str(source_root)], cwd=str(source_root))

    default_db = source_root / ".codegraph" / "codegraph.db"
    if not default_db.exists():
        raise RuntimeError(
            f"codegraph 索引后未生成 SQLite: expected {default_db}"
        )

    shutil.copy2(default_db, target)
    return str(target)
