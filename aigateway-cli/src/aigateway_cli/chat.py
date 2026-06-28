"""
交互式对话模块 (chat)

提供基于终端的交互式聊天体验，支持会话历史持久化。

用法:
    aigateway chat [--api-key KEY] [--url URL] [--session NAME] [--model MODEL]
"""

import json
import sys
from typing import Any

import httpx
from rich.console import Console
from rich.prompt import Prompt

from aigateway_cli.session import Session

console = Console()


def send_chat_request(
    *,
    messages: list[dict[str, Any]],
    api_key: str,
    base_url: str,
    model: str | None = None,
    temperature: float | None = None,
    stream: bool = False,
) -> dict[str, Any]:
    """发送聊天请求到 Gateway（非流式）。

    参数:
        messages: 完整消息历史（OpenAI 格式）。
        api_key: 认证用 API Key。
        base_url: Gateway 基础地址。
        model: 模型名称。
        temperature: 温度参数。
        stream: 是否流式（当前仅支持非流式）。

    返回:
        响应体中的 data.choices[0] 数据。

    异常:
        SystemExit: 请求失败时退出。
    """
    if stream:
        console.print("[yellow]流式模式暂不支持，已降级为非流式[/]")

    request_body: dict[str, Any] = {
        "model": model or "gpt-4o",
        "messages": messages,
        "stream": False,
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

    # 处理错误状态码
    if response.status_code == 401:
        data = response.json()
        msg = data.get("error", {}).get("message", "API Key 无效")
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
        console.print(f"\n[bold yellow]速率限制: {msg}[/]")
        sys.exit(1)

    if response.status_code != 200:
        console.print(f"\n[bold red]HTTP {response.status_code}: {response.text}[/]")
        sys.exit(1)

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


def print_assistant_content(choice: dict[str, Any]) -> None:
    """格式化并打印助手回复内容。

    参数:
        choice: send_chat_request 返回的 choice 数据。
    """
    message = choice.get("message", {})
    content = message.get("content", "")
    tool_calls = message.get("tool_calls")
    finish_reason = choice.get("finish_reason")

    if content:
        console.print(content)

    if tool_calls:
        console.print(f"\n[dim yellow]工具调用 ({len(tool_calls)} 个):[/]")
        for tc in tool_calls:
            func = tc.get("function", {})
            console.print(f"  - 函数: {func.get('name', '?')}")
            console.print(f"    参数: {func.get('arguments', '{}')}")

    if finish_reason:
        console.print(f"\n[dim]完成原因: {finish_reason}[/]")

    # 打印管线元数据
    meta = message.get("_meta") or choice.get("_meta")
    if meta:
        route_info = meta.get("routed_to")
        if route_info:
            console.print(f"[dim]路由到: {route_info.get('provider', '?')} / {route_info.get('model', '?')}[/]")
        if meta.get("cache_hit"):
            console.print("[green dim]缓存命中 ✓[/]")


def main(
    *,
    api_key: str,
    base_url: str,
    session: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
) -> None:
    """交互式对话主入口。

    参数:
        api_key: API Key。
        base_url: Gateway 基础地址。
        session: 会话名称（可选）。
        model: 模型名称（可选）。
        temperature: 温度参数（可选）。
    """
    # 初始化会话
    sess = Session(session) if session else None

    # 加载已有历史
    if sess:
        hist = sess.get_messages()
    else:
        hist = []

    # 系统提示
    system_prompt = "You are a helpful assistant."
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    if hist:
        messages.extend(hist)

    console.print(f"[dim]AI Gateway Chat — 输入 'quit' 或 'exit' 退出，'clear' 清空会话[/]")
    if model:
        console.print(f"[dim]模型: {model}[/]")
    console.print()

    while True:
        try:
            user_input = Prompt.ask("[bold cyan]你[/]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]退出对话[/]")
            break

        if not user_input:
            continue

        if user_input.lower() in ("quit", "exit"):
            console.print("[dim]再见！[/]")
            break

        if user_input.lower() == "clear":
            messages = [{"role": "system", "content": system_prompt}]
            if sess:
                sess.clear()
            console.print("[dim]会话已清空[/]")
            continue

        # 添加用户消息
        messages.append({"role": "user", "content": user_input})

        # 发送请求
        console.print()
        choice = send_chat_request(
            messages=messages,
            api_key=api_key,
            base_url=base_url,
            model=model,
            temperature=temperature,
        )

        # 打印助手回复
        print_assistant_content(choice)
        console.print()

        # 提取助手内容
        assistant_message = choice.get("message", {}).get("content", "")

        if assistant_message:
            # 添加到消息历史和会话
            messages.append({"role": "assistant", "content": assistant_message})
            if sess:
                sess.add_message("user", user_input)
                sess.add_message("assistant", assistant_message)
