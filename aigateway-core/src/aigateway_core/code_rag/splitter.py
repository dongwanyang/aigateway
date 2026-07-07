"""代码库切分 + 路径白名单 + 行号跨度回退.

切分链路(AST.txt 指定):
  GenericLoader.from_filesystem
    → LanguageParser  (底层 tree-sitter,按类/函数边界切)
    → RecursiveCharacterTextSplitter.from_language  (超长块按 AST 边界二次切)

路径白名单用 realpath 展开对比,拒绝符号链接逃逸。
"""
from __future__ import annotations

import os
import re
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

# 只对这些后缀跑 AST 切分,避免二进制/图片/压缩包冲进来把 utf-8 解码打炸。
# 与 AST.txt 里例子的 suffixes=[".py",".js",".java"] 一脉相承,但覆盖更完整
# (仍保守;非源码文件不进代码 RAG,由文本 RAG 覆盖)。
_CODE_SUFFIXES: tuple[str, ...] = (
    ".py",
    ".pyi",
    ".js",
    ".mjs",
    ".cjs",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".kt",
    ".kts",
    ".go",
    ".rs",
    ".c",
    ".h",
    ".hpp",
    ".hh",
    ".cc",
    ".cpp",
    ".cxx",
    ".cs",
    ".php",
    ".rb",
    ".scala",
    ".swift",
    ".html",
    ".htm",
    ".md",
    ".sol",
    ".lua",
    ".pl",
    ".pm",
    ".hs",
    ".ex",
    ".exs",
    ".ps1",
    ".cbl",
    ".cob",
    ".tex",
    ".proto",
)


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


# 顶层符号提取: 覆盖主流语言的 def/class/func/impl 头。返回 (kind, name) 或 None。
# 只匹配 chunk 前若干行里第一个 top-level 声明,足以支撑图谱按 symbol_name 定位。
_SYMBOL_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    # Python
    ("function", "python", re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)")),
    ("class", "python", re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)")),
    # JS / TS
    ("function", "js", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)")),
    ("class", "js", re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)")),
    # Go
    ("function", "go", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)")),
    # Rust
    ("function", "rust", re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)")),
    ("class", "rust", re.compile(r"^\s*(?:pub\s+)?struct\s+([A-Za-z_][A-Za-z0-9_]*)")),
    # Java / Kotlin / C# / Scala — 类
    ("class", "java", re.compile(r"^\s*(?:public|private|protected|internal|abstract|final|sealed|open)?\s*(?:static\s+)?(?:class|interface|object|trait|enum)\s+([A-Za-z_][A-Za-z0-9_]*)")),
    # C / C++ — 只截函数名(粗略但稳定)
    ("function", "c", re.compile(r"^[A-Za-z_][A-Za-z0-9_\s\*<>,]*\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^;]*\)\s*\{")),
)


def extract_top_symbol(chunk_text: str) -> tuple[str, str] | None:
    """从 chunk 前若干行里提取第一个函数/类声明.

    返回 ('function'|'class', name) 或 None。用于给 CodeGraph 查询提供 symbol_name。
    """
    if not chunk_text:
        return None
    head_lines = chunk_text.splitlines()[:40]
    head = "\n".join(head_lines)
    for kind, _hint, pattern in _SYMBOL_PATTERNS:
        match = pattern.search(head)
        if match:
            return (kind, match.group(1))
    return None


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

    实现要点:
    - 先按后缀白名单(_CODE_SUFFIXES)枚举文件,天然跳过二进制/媒体/压缩包等,
      避免 GenericLoader 在整目录加载时因单个二进制文件解码失败而整体抛异常。
    - 逐个文件调 GenericLoader.from_filesystem(path=file),单文件解析失败只 log
      warning 并跳过,不影响其他文件(与 spec 的"file parse failure: skip"一致)。
    - 忽略规则: 只要文件路径中包含任一 ignore_patterns 中的子串就跳过。
    """
    import logging

    from langchain_community.document_loaders.generic import GenericLoader
    from langchain_community.document_loaders.parsers import LanguageParser
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    logger = logging.getLogger(__name__)

    root_path = Path(root_dir).resolve()
    results: list[dict[str, Any]] = []

    for file_path in root_path.rglob("*"):
        if not file_path.is_file():
            continue
        source = str(file_path)
        if any(pattern in source for pattern in ignore_patterns):
            continue
        if file_path.suffix.lower() not in _CODE_SUFFIXES:
            continue

        try:
            loader = GenericLoader.from_filesystem(
                str(file_path),
                parser=LanguageParser(parser_threshold=parser_threshold),
            )
            documents = loader.load()
        except UnicodeDecodeError as exc:
            logger.warning("code_rag: 跳过无法解码的文件 %s: %s", file_path, exc)
            continue
        except Exception as exc:
            logger.warning("code_rag: 单文件解析异常,跳过 %s: %s", file_path, exc)
            continue

        for doc in documents:
            language_hint = doc.metadata.get("language")
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
                rel_path = str(file_path.relative_to(root_path))
            except ValueError:
                rel_path = str(file_path)

            for index, chunk in enumerate(splitter.split_text(source_text)):
                start_line, end_line = compute_line_span(source_text, chunk)
                symbol = extract_top_symbol(chunk)
                function_name = symbol[1] if symbol and symbol[0] == "function" else None
                class_name = symbol[1] if symbol and symbol[0] == "class" else None
                results.append(
                    {
                        "file_path": rel_path,
                        "filename": file_path.name,
                        "language": language_name or "text",
                        "chunk_index": index,
                        "chunk_text": chunk,
                        "start_line": start_line,
                        "end_line": end_line,
                        "function_name": function_name,
                        "class_name": class_name,
                    }
                )
    return results
