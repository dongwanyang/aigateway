"""代码库切分 + 路径白名单 + 行号跨度回退.

切分链路(AST.txt 指定):
  GenericLoader.from_filesystem
    → LanguageParser  (底层 tree-sitter,按类/函数边界切)
    → RecursiveCharacterTextSplitter.from_language  (超长块按 AST 边界二次切)

路径白名单用 realpath 展开对比,拒绝符号链接逃逸。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# 语言名 → LangChain Language 枚举成员名(全大写)
# LangChain 的 Language 成员包含: CPP/GO/JAVA/KOTLIN/JS/TS/PHP/PROTO/PYTHON/
#   RST/RUBY/RUST/SCALA/SWIFT/MARKDOWN/LATEX/HTML/SOL/CSHARP/COBOL/C/LUA/
#   PERL/HASKELL/ELIXIR/POWERSHELL/VISUALBASIC6
_LANGUAGE_ALIASES = {
    "python": "PYTHON",
    "py": "PYTHON",
    "javascript": "JS",
    "js": "JS",
    "typescript": "TS",
    "ts": "TS",
    "java": "JAVA",
    "kotlin": "KOTLIN",
    "go": "GO",
    "rust": "RUST",
    "rs": "RUST",
    "c": "C",
    "cpp": "CPP",
    "c++": "CPP",
    "csharp": "CSHARP",
    "cs": "CSHARP",
    "php": "PHP",
    "ruby": "RUBY",
    "rb": "RUBY",
    "scala": "SCALA",
    "swift": "SWIFT",
    "html": "HTML",
    "markdown": "MARKDOWN",
    "md": "MARKDOWN",
    "sol": "SOL",
    "solidity": "SOL",
    "lua": "LUA",
    "perl": "PERL",
    "haskell": "HASKELL",
    "elixir": "ELIXIR",
    "powershell": "POWERSHELL",
    "cobol": "COBOL",
    "latex": "LATEX",
    "proto": "PROTO",
}


def is_path_allowed(candidate: str, allowed_roots: list[str]) -> bool:
    """判断 candidate 是否落在任一 allowed_roots 下(realpath 展开后)。

    - 拒绝符号链接逃逸(realpath 后不再位于 allowed_root 下即返回 False)
    - 拒绝空/不存在的父目录(通过 resolve() 得到的绝对路径处理)
    """
    try:
        candidate_real = Path(os.path.realpath(candidate))
    except OSError:
        return False
    for root in allowed_roots:
        try:
            root_real = Path(os.path.realpath(root))
        except OSError:
            continue
        if candidate_real == root_real or root_real in candidate_real.parents:
            return True
    return False


def compute_line_span(source_text: str, chunk_text: str) -> tuple[int, int]:
    """返回 chunk 在 source 中的 (start_line, end_line),1 起。

    找不到时退化为 (1, 源码总行数),保证 payload 始终有合法整数。
    """
    start_index = source_text.find(chunk_text)
    if start_index < 0:
        return (1, max(1, source_text.count("\n") + 1))
    start_line = source_text[:start_index].count("\n") + 1
    end_line = start_line + chunk_text.count("\n")
    return (start_line, end_line)


def _match_language(language_hint: str | None) -> Any:
    """把 LangChain 元数据里的 language 字符串映射到 Language 枚举.

    未知语言退化为 PYTHON(RecursiveCharacterTextSplitter 的通用 fallback)。
    """
    # lazy import: 生产镜像装了 langchain-text-splitters,开发机也已装
    from langchain_text_splitters import Language

    hint = (language_hint or "").strip().lower()
    key = _LANGUAGE_ALIASES.get(hint, hint.upper())
    return getattr(Language, key, Language.PYTHON)


def split_code_directory(
    root_dir: str,
    ignore_patterns: list[str],
    *,
    chunk_size: int = 2000,
    chunk_overlap: int = 200,
    parser_threshold: int = 500,
) -> list[dict[str, Any]]:
    """遍历 root_dir 下所有源码,按 AST 边界切成规范化的 chunk 字典.

    每个 chunk 字典包含:
      file_path/filename/language/chunk_index/chunk_text/start_line/end_line

    忽略规则: 只要文件路径中包含任一 ignore_patterns 中的子串就跳过。
    该实现是 API 路由层调用的主入口,是"strict on import"的一部分,
    因此任何解析异常都会向上抛,由路由层记为任务失败。
    """
    from langchain_community.document_loaders.generic import GenericLoader
    from langchain_community.document_loaders.parsers import LanguageParser
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    loader = GenericLoader.from_filesystem(
        root_dir,
        glob="**/*",
        parser=LanguageParser(parser_threshold=parser_threshold),
    )
    documents = loader.load()

    root_path = Path(root_dir).resolve()
    results: list[dict[str, Any]] = []
    for doc in documents:
        source = str(doc.metadata.get("source", "") or "")
        if any(pattern in source for pattern in ignore_patterns):
            continue

        language_hint = doc.metadata.get("language")
        # LanguageParser 会给出 Language 枚举/字符串两种
        language_name = (
            language_hint.name.lower()
            if hasattr(language_hint, "name")
            else str(language_hint or "").lower()
        )

        splitter = RecursiveCharacterTextSplitter.from_language(
            language=_match_language(language_name),
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
        source_text = doc.page_content
        try:
            rel_path = str(Path(source).relative_to(root_path)) if source else ""
        except ValueError:
            rel_path = source

        for index, chunk in enumerate(splitter.split_text(source_text)):
            start_line, end_line = compute_line_span(source_text, chunk)
            results.append(
                {
                    "file_path": rel_path,
                    "filename": Path(source).name if source else "",
                    "language": language_name or "text",
                    "chunk_index": index,
                    "chunk_text": chunk,
                    "start_line": start_line,
                    "end_line": end_line,
                }
            )
    return results
