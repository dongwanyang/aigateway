"""
aigateway-cli 入口点

提供命令行入口，注册子命令 chat 和 run。

        epilog=(
            "示例:\n"
            '  aigateway run --prompt "你好"\n'
            '  aigateway run --prompt "翻译: Hello world" --model gpt-4o\n'
            "  aigateway run --prompt " + repr('{"q":"天气"}') + " --format json\n"
        ),
"""

import argparse
import sys

# 加载 .env 文件到进程环境变量(在任何环境变量读取前执行)
# override=False → 不覆盖已存在的环境变量,保证 shell export > .env
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from aigateway_cli.__version__ import __version__  # type: ignore  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="aigateway",
        description="AI Gateway CLI 工具 — 交互式对话与单次请求",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "子命令:\n"
            "  chat    进入交互式对话模式\n"
            "  run     发送单次请求并输出结果\n"
            "\n"
            "环境变量:\n"
            "  AI_GATEWAY_API_KEY    默认 API Key（可选，也可通过 --api-key 传入）\n"
            "  AI_GATEWAY_URL        Gateway 基础地址（可选，也可通过 --url 传入）\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    # 全局可选参数
    parser.add_argument(
        "--api-key",
        default=None,
        help="API Key（默认读取环境变量 AI_GATEWAY_API_KEY）",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="AI Gateway 基础地址（默认 https://localhost:8000）",
    )

    subparsers = parser.add_subparsers(dest="command", help="可用的子命令")

    # ---------- chat 子命令 ----------
    chat_parser = subparsers.add_parser(
        "chat",
        help="进入交互式对话模式",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  aigateway chat\n"
            "  aigateway chat --session my-project\n"
            "  aigateway chat --model claude-3-sonnet\n"
        ),
    )
    chat_parser.add_argument(
        "--session",
        default=None,
        help="使用命名会话（持久化历史，保存在 ~/.aigateway/sessions/ 目录下）",
    )
    chat_parser.add_argument(
        "--model",
        default=None,
        help="指定对话使用的模型（默认使用系统默认模型）",
    )
    chat_parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="温度参数，范围 0.0-2.0（默认 1.0）",
    )

    # ---------- run 子命令 ----------
    run_parser = subparsers.add_parser(
        "run",
        help="发送单次请求并输出结果",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            '  aigateway run --prompt "你好"\n'
            '  aigateway run --prompt "翻译: Hello world" --model gpt-4o\n'
            "  aigateway run --prompt '{}' --format json".format('{"q":"天气"}')
        ),
    )
    run_parser.add_argument(
        "--prompt",
        required=True,
        help="用户消息内容",
    )
    run_parser.add_argument(
        "--model",
        default=None,
        help="指定使用的模型（默认 gpt-4o）",
    )
    run_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="输出格式：text（默认）或 json",
    )
    run_parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help="温度参数，范围 0.0-2.0（默认 1.0）",
    )

    # ---------- codegraph 子命令 ----------
    codegraph_parser = subparsers.add_parser(
        "codegraph",
        help="代码图谱查询与管理（走 admin API）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  aigateway codegraph status\n"
            "  aigateway codegraph query login -d code_abc123\n"
            "  aigateway codegraph callers login -d code_abc123 --json\n"
            "  aigateway codegraph impact login -d code_abc123 --depth 2\n"
            "  aigateway codegraph sync -d code_abc123\n"
        ),
    )
    codegraph_parser.add_argument(
        "action",
        choices=["status", "query", "callers", "callees", "impact", "node", "files", "sync"],
        help="子操作",
    )
    codegraph_parser.add_argument(
        "symbol",
        nargs="?",
        default=None,
        help="符号名（query/callers/callees/impact/node 需要）",
    )
    codegraph_parser.add_argument(
        "-d", "--document",
        default=None,
        help="document_id（除 status 外都需要）",
    )
    codegraph_parser.add_argument(
        "--kind",
        default=None,
        help="query 时按节点类型过滤（function/class/method）",
    )
    codegraph_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="query 最大结果数（默认 10）",
    )
    codegraph_parser.add_argument(
        "--depth",
        type=int,
        default=2,
        help="impact 遍历深度（默认 2）",
    )
    codegraph_parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="输出原始 JSON",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """主入口函数。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    # 未指定子命令
    if not args.command:
        parser.print_help()
        return 0

    # 确定 api_key
    api_key = args.api_key or __import__("os").environ.get("AI_GATEWAY_API_KEY", "")
    if not api_key:
        print("[bold red]错误: 未提供 API Key。请设置环境变量 AI_GATEWAY_API_KEY 或使用 --api-key。[/]", file=sys.stderr)
        return 1

    # 确定 base_url
    base_url = args.url or __import__("os").environ.get("AI_GATEWAY_URL", "http://localhost:8000")
    # 确保 url 没有尾部斜杠
    base_url = base_url.rstrip("/")

    if args.command == "chat":
        from aigateway_cli.chat import main as chat_main

        chat_main(
            api_key=api_key,
            base_url=base_url,
            session=args.session,
            model=args.model,
            temperature=args.temperature,
        )

    elif args.command == "run":
        from aigateway_cli.run import main as run_main

        run_main(
            prompt=args.prompt,
            api_key=api_key,
            base_url=base_url,
            model=args.model,
            output_format=args.format,
            temperature=args.temperature,
        )

    elif args.command == "codegraph":
        from aigateway_cli.codegraph import main as codegraph_main

        return codegraph_main(
            api_key=api_key,
            base_url=base_url,
            action=args.action,
            symbol=args.symbol,
            document=args.document,
            kind=args.kind,
            limit=args.limit,
            depth=args.depth,
            as_json=args.as_json,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
