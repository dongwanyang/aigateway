"""
单次请求模块 (run)

发送一次性聊天请求到 AI Gateway，并输出结果。

用法:
    aigateway run --prompt "..." [--model gpt-4o] [--format json|text]

根据 API_CONTRACT.md 中 POST /v1/chat/completions 的定义构造请求。
"""

import json
import sys
from typing import Any

import httpx
from rich.console import Console

console = Console()


def send_request(
    *,
    prompt: str,
    api_key: str,
    base_url: str,
    model: str | None,
    temperature: float | None,
) -> dict[str, Any]:
    """发送单次聊天请求到 Gateway。

    参数:
        prompt: 用户消息文本
        api_key: 认证用 API Key
        base_url: Gateway 基础地址，如 http://localhost:8000
        model: 模型名称，None 时由服务端使用默认模型
        temperature: 温度参数，None 时使用默认值

    返回:
        响应体 JSON 中的 data.choices 数组第一条记录的完整数据

    异常:
        SystemExit: 当 HTTP 请求出错时打印错误信息并退出
    """
    # 按契约构造请求体
    request_body: dict[str, Any] = {
        "model": model or "gpt-4o",
        "messages": [
            {"role": "user", "content": prompt},
        ],
    }
    if temperature is not None:
        request_body["temperature"] = temperature

    url = f"{base_url}/v1/chat/completions"
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(url, json=request_body, headers=headers)
    except httpx.ConnectError as e:
        console.print(f"\n[bold red]连接失败: 无法到达 {url}[/]")
        console.print(f"[dim]详细信息: {e}[/]")
        sys.exit(1)
    except httpx.TimeoutException as e:
        console.print(f"\n[bold red]请求超时: {e}[/]")
        sys.exit(1)

    # 按契约处理各种 HTTP 错误状态码
    if response.status_code == 401:
        data = response.json()
        msg = data.get("error", {}).get("message", "API Key 无效或已过期")
        console.print(f"\n[bold red]认证失败: {msg}[/]")
        sys.exit(1)

    if response.status_code == 403:
        data = response.json()
        msg = data.get("error", {}).get("message", "API Key 已被撤销")
        console.print(f"\n[bold red]禁止访问: {msg}[/]")
        sys.exit(1)

    if response.status_code == 400:
        data = response.json()
        msg = data.get("error", {}).get("message", "请求参数错误")
        console.print(f"\n[bold red]请求失败: {msg}[/]")
        sys.exit(1)

    if response.status_code == 429:
        data = response.json()
        msg = data.get("error", {}).get("message", "速率限制已触发")
        retry_after = response.headers.get("Retry-After", "")
        if retry_after:
            msg += f" (请在 {retry_after} 秒后重试)"
        console.print(f"\n[bold yellow]速率限制: {msg}[/]")
        sys.exit(1)

    if response.status_code == 503:
        data = response.json()
        msg = data.get("error", {}).get("message", "服务暂时不可用")
        console.print(f"\n[bold red]服务不可用: {msg}[/]")
        sys.exit(1)

    if response.status_code == 504:
        data = response.json()
        msg = data.get("error", {}).get("message", "上游提供商超时")
        console.print(f"\n[bold red]上游超时: {msg}[/]")
        sys.exit(1)

    if response.status_code != 200:
        console.print(f"\n[bold red]HTTP {response.status_code}: {response.text}[/]")
        sys.exit(1)

    # 解析成功响应 — 契约规定成功响应格式:
    # { "data": { ... }, "message": "success" }
    try:
        full_response = response.json()
    except json.JSONDecodeError:
        console.print("\n[bold red]响应体不是有效的 JSON[/]")
        sys.exit(1)

    data = full_response.get("data", {})
    choices = data.get("choices", [])
    if not choices:
        console.print("\n[bold yellow]警告: 响应中没有 choices 字段[/]")
        return full_response

    return choices[0]


def format_output(result: dict[str, Any], output_format: str, model: str | None = None) -> None:
    """格式化并打印请求结果。

    参数:
        result: send_request 返回的 choice 数据
        output_format: 输出格式 ("text" 或 "json")
        model: 模型名称
    """
    # 提取消息内容
    message = result.get("message", {})
    content = message.get("content", "")
    tool_calls = message.get("tool_calls")
    finish_reason = result.get("finish_reason")

    # 提取 usage 信息
    usage = result.get("usage")

    if output_format == "json":
        # JSON 格式: 输出完整响应
        output_obj: dict[str, Any] = {"result": content}
        if usage:
            output_obj["usage"] = usage
        if tool_calls:
            output_obj["tool_calls"] = tool_calls
        if finish_reason:
            output_obj["finish_reason"] = finish_reason
        console.print(json.dumps(output_obj, ensure_ascii=False, indent=2))
    else:
        # 纯文本格式: 直接输出内容
        if content:
            console.print(content)
        if tool_calls:
            console.print(f"\n[yellow]工具调用 ({len(tool_calls)} 个):[/]")
            for tc in tool_calls:
                func = tc.get("function", {})
                console.print(f"  - 函数: {func.get('name', '?')}")
                console.print(f"    参数: {func.get('arguments', '{}')}")
        if finish_reason:
            console.print(f"\n[dim]完成原因: {finish_reason}[/]")

    # 打印 token 使用量
    if usage:
        console.print(f"\n[dim]Tokens — 输入: {usage.get('prompt_tokens', '?')}, "
                      f"输出: {usage.get('completion_tokens', '?')}, "
                      f"总计: {usage.get('total_tokens', '?')}[/]")

    # 打印管线元数据
    meta = message.get("_meta") or result.get("_meta")
    if meta:
        route_info = meta.get("routed_to")
        if route_info:
            console.print(f"[dim]路由到: {route_info.get('provider', '?')} / {route_info.get('model', '?')}[/]")
        if meta.get("cache_hit"):
            console.print("[green dim]缓存命中 ✓[/]")


def main(
    *,
    prompt: str,
    api_key: str,
    base_url: str,
    model: str | None,
    output_format: str,
    temperature: float | None,
) -> None:
    """run 子命令的入口函数。

    参数:
        prompt: 用户提示文本
        api_key: 认证用 API Key
        base_url: Gateway 基础地址
        model: 模型名称
        output_format: 输出格式 ("text" 或 "json")
        temperature: 温度参数
    """
    console.print(f"\n[dim]发送请求到: {base_url}/v1/chat/completions[/]")
    if model:
        console.print(f"[dim]模型: {model}[/]")

    result = send_request(
        prompt=prompt,
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=temperature,
    )

    console.print()  # 空行分隔
    format_output(result, output_format, model)
    console.print()  # 尾部空行
