"""CodeGraph 官方 CLI wrapper.

重要：这里集成的是 GitHub 文档里的官方 CodeGraph CLI / npm 包
`@colbymchenry/codegraph`，而不是我们之前误用的 Python 包 API 假设。

集成路线：
1. 在持久化卷下的可写临时目录创建源码 symlink
2. 执行 `codegraph init`（在可写目录创建 .codegraph/）
3. 将生成的 .codegraph/codegraph.db 复制到 graph_db_path

持久化策略：.codegraph/ 目录始终落在持久化卷（如 /data/code_graphs）
内，即使容器重启也不会丢失。

环境兼容：
- 此逻辑不依赖 Docker 容器；它依赖的目标图谱目录（由
  config.yaml 的 code_graph_db_dir 或 graph_builder 调用方传入的
  graph_db_path 决定）必须可写。
- 在 Docker 中通常通过持久卷（如 code_graphs_data）保证可写；
  本地非容器部署时需确保配置的图谱目录可写。
"""
from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path


def _run_codegraph(args: list[str], *, cwd: str, timeout: float = 1800.0) -> None:
    """调用官方 codegraph CLI；失败或超时时抛 RuntimeError（导入链路 strict）。

    默认 30 分钟超时(config: code_rag.codegraph_timeout_seconds 可覆盖)。
    """
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "codegraph CLI 未安装；请在 gateway 镜像中安装 @colbymchenry/codegraph"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"codegraph command timed out after {timeout}s: {' '.join(args)}"
        ) from exc

    if proc.returncode != 0:
        output = proc.stdout.strip()
        raise RuntimeError(
            f"codegraph command failed ({' '.join(args)}): {output[:4000]}"
        )


def build_code_graph(
    source_dir: str,
    graph_db_path: str,
    *,
    timeout: float = 1800.0,
) -> str:
    """为 source_dir 构建官方 CodeGraph SQLite 图谱，并复制到 graph_db_path。

    返回值：graph_db_path（最终 SQLite 文件绝对路径）

    失败策略：任一步失败直接抛异常，由导入任务整体标记 failed。

    工作目录：在 graph_db_path 同级目录下创建临时子目录（如
    /data/code_graphs/.tmp/xxx），codegraph init 必须在此可写目录执行，
    完成后将 .codegraph/codegraph.db 复制到目标位置并清理临时目录。
    这样即使容器重启，图谱数据仍保留在持久化卷中，不会丢失。
    """
    source_root = Path(source_dir).resolve()
    target = Path(graph_db_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    # 在目标目录同级创建可写的工作子目录，避免 codegraph init 在只读
    # 挂载的源码目录写入 .codegraph/ 目录。
    # 使用 UUID 命名避免并发导入时同一毫秒内的目录碰撞。
    work_dir = target.parent / ".tmp" / f"cg_{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True, exist_ok=False)

    try:
        # 将源码 symlink 到 work_dir，codegraph init 会跟随 symlink
        # 在 work_dir 下创建 .codegraph/（持久化卷内可写）
        link_name = work_dir / "src"
        if not link_name.exists():
            link_name.symlink_to(source_root)

        # codegraph init 在 work_dir 运行，自动扫描 symlink 指向的源码
        # 并在此处创建 .codegraph/（持久化卷内）
        _run_codegraph(["codegraph", "init"], cwd=str(work_dir), timeout=timeout)

        db_in_work = work_dir / ".codegraph" / "codegraph.db"
        if not db_in_work.exists():
            raise RuntimeError(
                f"codegraph 索引后未生成 SQLite: expected {db_in_work}"
            )

        shutil.copy2(db_in_work, target)
        return str(target)
    finally:
        # 清理工作目录（db 已复制到持久化目标路径）
        shutil.rmtree(work_dir, ignore_errors=True)
