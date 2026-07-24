"""CodeGraph 官方 CLI wrapper.

重要：这里集成的是 GitHub 文档里的官方 CodeGraph CLI / npm 包
`@colbymchenry/codegraph`，而不是我们之前误用的 Python 包 API 假设。

集成路线（重构后）：
1. 在持久化的目标目录 {graph_repo_path} 下创建可写 work_dir
2. 执行 `codegraph init <work_dir>`（位置参数，**不接 -p**；在 work_dir 创建 .codegraph/）
3. 把生成的整个 .codegraph/ 目录移到 {graph_repo_path}/.codegraph/（保留目录结构，
   而非只复制单个 db 文件——codegraph CLI 查询类命令用 `-p {graph_repo_path}` 时
   需要整个 .codegraph/ 在场）
4. 返回 graph_repo_path（目录）

持久化策略：.codegraph/ 目录落在持久化卷（如 /data/code_graphs/{document_id}/）
内，即使容器重启也不会丢失。删除仓库时整目录清理（见 code_rag_routes 删除端点）。

环境兼容：
- 此逻辑不依赖 Docker 容器；它依赖的目标图谱目录（由
  config.yaml 的 code_graph_db_dir 或 graph_builder 调用方传入的
  graph_repo_path 决定）必须可写。
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

    仅用于 build 阶段的 `codegraph init`（位置参数命令）。查询类命令（query/callers/
    callees/impact/files）走 graph_query.py 里的 _run_codegraph_json / _run_codegraph_raw。

    用 Popen + start_new_session=True 启动,超时后 killpg 收割整个进程组
    (codegraph init 会 spawn 解析器孙进程,只杀主子进程会留僵尸)。
    """
    import os
    import signal

    try:
        proc = subprocess.Popen(
            args,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            "codegraph CLI 未安装；请在 gateway 镜像中安装 @colbymchenry/codegraph"
        ) from exc

    try:
        stdout, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        raise RuntimeError(
            f"codegraph command timed out after {timeout}s: {' '.join(args)}"
        )

    if proc.returncode != 0:
        output = (stdout or "").strip()
        raise RuntimeError(
            f"codegraph command failed ({' '.join(args)}): {output[:4000]}"
        )


def build_code_graph(
    source_dir: str,
    graph_repo_path: str,
    *,
    timeout: float = 1800.0,
) -> str:
    """为 source_dir 构建官方 CodeGraph 图谱，保留整个 .codegraph/ 目录。

    返回值：graph_repo_path（最终目录绝对路径，codegraph CLI 用 `-p {graph_repo_path}` 查询）

    失败策略：任一步失败直接抛异常，由导入任务整体标记 failed。

    工作目录：在 graph_repo_path 下创建可写的 work_dir 子目录（如
    {graph_repo_path}/.tmp/cg_xxx），codegraph init 在此执行，
    完成后把 .codegraph/ 移到 {graph_repo_path}/.codegraph/ 并清理 work_dir。
    这样即使容器重启，图谱数据仍保留在持久化卷中，不会丢失。
    """
    source_root = Path(source_dir).resolve()
    repo_dir = Path(graph_repo_path).resolve()
    repo_dir.mkdir(parents=True, exist_ok=True)

    # 在目标目录内创建可写的工作子目录，避免 codegraph init 在只读
    # 挂载的源码目录写入 .codegraph/ 目录。
    # 使用 UUID 命名避免并发导入时同一毫秒内的目录碰撞。
    work_dir = repo_dir / ".tmp" / f"cg_{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True, exist_ok=False)

    try:
        # 将源码 symlink 到 work_dir，codegraph init 会跟随 symlink
        # 在 work_dir 下创建 .codegraph/（持久化卷内可写）
        link_name = work_dir / "src"
        if not link_name.exists():
            link_name.symlink_to(source_root)

        # codegraph init <work_dir>（位置参数，不接 -p）
        # 在 work_dir 运行，自动扫描 symlink 指向的源码并创建 .codegraph/
        _run_codegraph(["codegraph", "init", str(work_dir)], cwd=str(work_dir), timeout=timeout)

        graph_dir_in_work = work_dir / ".codegraph"
        if not graph_dir_in_work.exists():
            raise RuntimeError(
                f"codegraph 索引后未生成 .codegraph 目录: expected {graph_dir_in_work}"
            )

        # 保留整个 .codegraph/ 目录到持久化目标路径（不只 db 文件）
        target_graph_dir = repo_dir / ".codegraph"
        if target_graph_dir.exists():
            shutil.rmtree(target_graph_dir, ignore_errors=True)
        shutil.move(str(graph_dir_in_work), str(target_graph_dir))
        return str(repo_dir)
    finally:
        # 清理工作目录（.codegraph/ 已移到持久化目标路径）
        shutil.rmtree(work_dir, ignore_errors=True)
