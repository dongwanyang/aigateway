"""Code RAG 代码图谱查询 CLI (codegraph 子命令)。

子命令全部走 AI Gateway 的 admin API(/admin/rag/code/*),不在本地直查
.codegraph 目录——仓库图谱库在服务端 /data/code_graphs/{document_id}/,
CLI 用户通常无本地访问权限,统一走 API 更一致。

子命令:
  aigateway codegraph status                      列仓库
  aigateway codegraph query <symbol> [-d <doc>] [--kind K] [--limit N] [--json]
  aigateway codegraph callers <symbol> -d <doc> [--json]
  aigateway codegraph callees <symbol> -d <doc> [--json]
  aigateway codegraph impact <symbol> -d <doc> [--depth N] [--json]
  aigateway codegraph node <symbol> -d <doc>
  aigateway codegraph files -d <doc> [--json]
  aigateway codegraph sync -d <doc>

-d/--document 指定 document_id(对应服务端 {graph_db_dir}/{document_id}/ 目录)。
status 不需要 -d。
"""

import json
import sys
from typing import Any, Optional

import httpx
from rich.console import Console
from rich.table import Table

console = Console()


def _admin_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _get_json(base_url: str, api_key: str, path: str, *, params: Optional[dict[str, Any]] = None) -> Any:
    url = f"{base_url}{path}"
    try:
        resp = httpx.get(url, headers=_admin_headers(api_key), params=params, timeout=30.0)
    except httpx.HTTPError as exc:
        console.print(f"[bold red]请求失败:[/] {exc}")
        raise SystemExit(1)
    if resp.status_code >= 400:
        console.print(f"[bold red]HTTP {resp.status_code}:[/] {resp.text[:500]}")
        raise SystemExit(1)
    return resp.json()


def _print_json(data: Any) -> None:
    console.print_json(json.dumps(data, ensure_ascii=False))


def _cmd_status(api_key: str, base_url: str, **_: Any) -> int:
    repos = _get_json(api_key=api_key, base_url=base_url, path="/admin/rag/code/repositories")
    if not isinstance(repos, list) or not repos:
        console.print("[yellow]暂无已导入代码仓库[/]")
        return 0
    table = Table(title="代码仓库")
    table.add_column("document_id", style="cyan")
    table.add_column("source_type", style="magenta")
    table.add_column("source_label")
    table.add_column("files", justify="right")
    table.add_column("funcs", justify="right")
    table.add_column("classes", justify="right")
    for r in repos:
        table.add_row(
            str(r.get("document_id", "")),
            str(r.get("source_type", "")),
            str(r.get("source_label", "")),
            str(r.get("file_count", 0)),
            str(r.get("function_count", 0)),
            str(r.get("class_count", 0)),
        )
    console.print(table)
    return 0


def _cmd_query(api_key: str, base_url: str, *, symbol: str, document: str, kind: Optional[str], limit: int, as_json: bool, **_: Any) -> int:
    params: dict[str, Any] = {"symbol": symbol, "limit": limit}
    if kind:
        params["kind"] = kind
    data = _get_json(api_key=api_key, base_url=base_url, path=f"/admin/rag/code/repositories/{document}/query", params=params)
    if as_json:
        _print_json(data)
        return 0
    if not data:
        console.print(f"[yellow]未找到符号: {symbol}[/]")
        return 0
    for node in data:
        console.print(
            f"[bold cyan]{node.get('name')}[/] ({node.get('kind')}) "
            f"[dim]{node.get('file_path')}:{node.get('start_line')}-{node.get('end_line')}[/]"
        )
        if node.get("signature"):
            console.print(f"  signature: {node['signature']}")
        if node.get("docstring"):
            console.print(f"  docstring: {node['docstring']}")
    return 0


def _cmd_relationship(
    api_key: str, base_url: str, *, kind: str, symbol: str, document: str, as_json: bool, **_: Any
) -> int:
    data = _get_json(api_key=api_key, base_url=base_url, path=f"/admin/rag/code/repositories/{document}/{kind}", params={"symbol": symbol})
    if as_json:
        _print_json(data)
        return 0
    items = data.get(kind) if isinstance(data, dict) else data
    if not items:
        console.print(f"[yellow]无 {kind}: {symbol}[/]")
        return 0
    for item in items:
        console.print(
            f"[bold cyan]{item.get('name')}[/] ({item.get('kind')}) "
            f"[dim]{item.get('file_path')}:{item.get('start_line')}[/]"
        )
    return 0


def _cmd_impact(api_key: str, base_url: str, *, symbol: str, document: str, depth: int, as_json: bool, **_: Any) -> int:
    data = _get_json(api_key=api_key, base_url=base_url, path=f"/admin/rag/code/repositories/{document}/impact", params={"symbol": symbol, "depth": depth})
    if as_json:
        _print_json(data)
        return 0
    affected = data.get("affected") if isinstance(data, dict) else []
    console.print(f"[bold]impact[/] {symbol} (depth={depth}, nodes={data.get('node_count')})")
    if not affected:
        console.print("[yellow]无影响范围[/]")
        return 0
    for item in affected:
        console.print(
            f"  [bold cyan]{item.get('name')}[/] ({item.get('kind')}) "
            f"[dim]{item.get('file_path')}:{item.get('start_line')}[/]"
        )
    return 0


def _cmd_node(api_key: str, base_url: str, *, symbol: str, document: str, **_: Any) -> int:
    data = _get_json(api_key=api_key, base_url=base_url, path=f"/admin/rag/code/repositories/{document}/node", params={"symbol": symbol})
    # node 返回 {symbol, raw};raw 是 codegraph node 的 markdown 输出
    if isinstance(data, dict) and data.get("raw"):
        console.print(data["raw"])
    else:
        _print_json(data)
    return 0


def _cmd_files(api_key: str, base_url: str, *, document: str, as_json: bool, **_: Any) -> int:
    data = _get_json(api_key=api_key, base_url=base_url, path=f"/admin/rag/code/repositories/{document}/files")
    if as_json:
        _print_json(data)
        return 0
    if not data:
        console.print("[yellow]无文件[/]")
        return 0
    table = Table(title=f"文件 ({document})")
    table.add_column("path", style="cyan")
    table.add_column("language", style="magenta")
    table.add_column("nodes", justify="right")
    table.add_column("size", justify="right")
    for f in data:
        table.add_row(
            str(f.get("path", "")),
            str(f.get("language", "")),
            str(f.get("node_count", 0)),
            str(f.get("size", 0)),
        )
    console.print(table)
    return 0


def _cmd_sync(api_key: str, base_url: str, *, document: str, **_: Any) -> int:
    url = f"{base_url}/admin/rag/code/repositories/{document}/sync"
    try:
        resp = httpx.post(url, headers=_admin_headers(api_key), timeout=120.0)
    except httpx.HTTPError as exc:
        console.print(f"[bold red]请求失败:[/] {exc}")
        return 1
    if resp.status_code >= 400:
        console.print(f"[bold red]HTTP {resp.status_code}:[/] {resp.text[:500]}")
        return 1
    data = resp.json()
    console.print(
        f"[green]同步完成:[/] files={data.get('synced_files', 0)}, "
        f"symbols={data.get('refreshed_symbols', 0)}, "
        f"deleted={data.get('deleted_files', 0)}"
    )
    return 0


def main(
    *,
    api_key: str,
    base_url: str,
    action: str,
    symbol: Optional[str],
    document: Optional[str],
    kind: Optional[str],
    limit: int,
    depth: int,
    as_json: bool,
) -> int:
    """codegraph 子命令入口。"""
    if action == "status":
        return _cmd_status(api_key=api_key, base_url=base_url)

    # 其余命令需要 document_id
    if not document:
        console.print("[bold red]错误: 该子命令需要 -d/--document <document_id>[/]")
        return 1
    if action == "query":
        if not symbol:
            console.print("[bold red]错误: query 需要 <symbol> 参数[/]")
            return 1
        return _cmd_query(api_key=api_key, base_url=base_url, symbol=symbol, document=document, kind=kind, limit=limit, as_json=as_json)
    if action == "callers":
        return _cmd_relationship(api_key=api_key, base_url=base_url, kind="callers", symbol=symbol or "", document=document, as_json=as_json)
    if action == "callees":
        return _cmd_relationship(api_key=api_key, base_url=base_url, kind="callees", symbol=symbol or "", document=document, as_json=as_json)
    if action == "impact":
        return _cmd_impact(api_key=api_key, base_url=base_url, symbol=symbol or "", document=document, depth=depth, as_json=as_json)
    if action == "node":
        return _cmd_node(api_key=api_key, base_url=base_url, symbol=symbol or "", document=document)
    if action == "files":
        return _cmd_files(api_key=api_key, base_url=base_url, document=document, as_json=as_json)
    if action == "sync":
        return _cmd_sync(api_key=api_key, base_url=base_url, document=document)

    console.print(f"[bold red]未知 action: {action}[/]")
    return 1
