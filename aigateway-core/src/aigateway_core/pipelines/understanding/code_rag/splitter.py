"""代码库切分 + 路径白名单 + 行号跨度回退.

两条切分链路:
1. **build_symbol_chunks(重构后主路径)**:从 codegraph db 读符号节点,用
   start_line/end_line 从源文件切出 chunk_text,并构造**结构描述嵌入文本**
   (符号名/签名/callers/callees/docstring)。一个符号一个 chunk,与 Qdrant
   point 一一对应。嵌入的是结构描述而非源码 → 意图语义强、向量稳定、增量友好。
2. **split_code_directory(兼容入口)**:LangChain GenericLoader + LanguageParser
   (tree-sitter) + RecursiveCharacterTextSplitter。保留给老调用方/单测,内部
   不再委托 build_symbol_chunks(两条链路独立,split_code_directory 仍嵌源码)。

路径白名单用 realpath 展开对比,拒绝符号链接逃逸。
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
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


# ----------------------------------------------------------------------
# build_symbol_chunks — 重构后的主切分路径(codegraph 行号切源码 + 结构描述嵌入)
# ----------------------------------------------------------------------


# 源码片段行数兜底:当 db 的 start_line/end_line 取不到源码(文件已删/越界)时,
# fallback 到读取源文件前 N 行,避免 chunk_text 为空把向量打废。
_SOURCE_FALLBACK_LINES = 40
# 无 docstring 时,embed_text 拼上源码前 N 行(注释 + 签名),避免向量区分度过低。
# 实测 ~88% 函数无 docstring,靠符号名 + callers/callees + 签名 + 首行补偿。
_EMBED_SOURCE_PREVIEW_LINES = 6


def _graph_db_path(graph_repo_path: str) -> str:
    return str(Path(graph_repo_path) / ".codegraph" / "codegraph.db")


def _read_symbol_nodes(
    graph_repo_path: str, only_files: list[str] | None = None
) -> list[dict[str, Any]]:
    """从 codegraph db 读 function/method/class 节点。

    only_files 给定时只读这些 db file_path(带 src/ 前缀)的节点——增量 sync 用,
    只重切变了文件的符号。
    返回 [{kind,name,file_path,start_line,end_line,signature,docstring,language}].
    file_path 带 src/ 前缀(graph_builder symlink 约定)。
    """
    db_path = _graph_db_path(graph_repo_path)
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if only_files:
            placeholders = ",".join("?" for _ in only_files)
            rows = conn.execute(
                f"""
                SELECT id, kind, name, file_path, start_line, end_line, signature, docstring, language
                FROM nodes
                WHERE kind IN ('function', 'method', 'class')
                  AND name IS NOT NULL AND name != ''
                  AND file_path IN ({placeholders})
                ORDER BY file_path, start_line
                """,
                only_files,
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, kind, name, file_path, start_line, end_line, signature, docstring, language
                FROM nodes
                WHERE kind IN ('function', 'method', 'class')
                  AND name IS NOT NULL AND name != ''
                ORDER BY file_path, start_line
                """
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _resolve_source_file(file_path_in_db: str, source_dir: str) -> tuple[str, str]:
    """把 db 里带 src/ 前缀的 file_path 解析成 (绝对源文件路径, 相对源根路径)。

    graph_builder 把源码 symlink 成 work_dir/src,db 存 src/<rel>。剥掉 src/ 前缀
    得到相对 source_dir 的路径,再 join source_dir 得绝对路径。
    若 db 路径不以 src/ 开头(理论上不会,但兜底),直接 join source_dir。
    """
    rel = file_path_in_db
    if rel.startswith("src/"):
        rel = rel[len("src/"):]
    elif rel.startswith("src\\"):
        rel = rel[len("src\\"):]
    abs_path = str(Path(source_dir) / rel) if rel else str(Path(source_dir))
    return abs_path, rel


def _slice_source_lines(
    abs_path: str, start_line: int, end_line: int
) -> str:
    """从源文件切出 [start_line, end_line] 行(1 起,含尾)。失败返回空串。"""
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return ""
    # 1-based → 0-based slice
    start = max(1, int(start_line or 1)) - 1
    end = max(start + 1, int(end_line or start_line or 1))
    return "".join(lines[start:end])


def _build_embed_text(
    kind: str,
    name: str,
    file_path: str,
    signature: str | None,
    docstring: str | None,
    callers: list[str],
    callees: list[str],
    source_preview: str,
) -> str:
    """构造结构描述嵌入文本(意图语义强、向量稳定、增量友好)。

    形如:
        function login in auth.py
        signature: (user, pw) -> str
        callers: register
        callees: hash_password, jwt_encode
        docstring: 用户登录认证

    无 docstring 时拼上源码前 N 行(注释+签名),避免嵌入文本过短。
    """
    rel = file_path
    if rel.startswith("src/"):
        rel = rel[len("src/"):]
    parts = [f"{kind} {name} in {rel}"]
    if signature:
        parts.append(f"signature: {signature}")
    if callers:
        parts.append("callers: " + ", ".join(callers))
    if callees:
        parts.append("callees: " + ", ".join(callees))
    if docstring and docstring.strip():
        parts.append(f"docstring: {docstring.strip()}")
    else:
        # 兜底:无 docstring 的符号(~88%),拼源码前 N 行(注释 + 首行签名)
        preview = "\n".join(
            ln for ln in source_preview.splitlines()[:_EMBED_SOURCE_PREVIEW_LINES]
            if ln.strip()
        )
        if preview:
            parts.append(f"source:\n{preview}")
    return "\n".join(parts)


def build_symbol_chunks(
    source_dir: str,
    graph_repo_path: str,
    ignore_patterns: list[str],
    *,
    only_files: list[str] | None = None,
    progress_cb: Any = None,
) -> list[dict[str, Any]]:
    """从 codegraph db 读符号节点,用行号切源码 + 构造结构描述嵌入文本。

    callers/callees 走 read_call_edges_strict(2 条 SQL,全图一次),不再逐符号 spawn CLI。
    用 strict 版:db 存在但损坏时抛 sqlite3.Error,让 import 整体失败暴露,而不是
    静默降级成空 callers/callees 误判导入"成功"。db 不存在仍返回 ({},{})(图谱未建,
    但 import 流程里 build_code_graph 在前,此处 db 必然存在,空 map 只发生在无 calls 边)。
    progress_cb(done, total, current_file) 每 200 符号回写一次(splitting 阶段进度)。
    """
    from aigateway_core.pipelines.understanding.code_rag.graph_query import (
        _get_imports_for_file,
        read_call_edges_strict,
    )

    logger = logging.getLogger(__name__)
    source_root = Path(source_dir).resolve()
    nodes = _read_symbol_nodes(graph_repo_path, only_files=only_files)

    # 一次性建全图 callers/callees map(替代 ~10k 次 CLI 子进程)
    callers_map, callees_map = read_call_edges_strict(graph_repo_path)
    total = len(nodes)

    chunks: list[dict[str, Any]] = []
    per_file_index: dict[str, int] = {}

    for i, node in enumerate(nodes):
        rel_in_db = str(node.get("file_path") or "")
        if any(pat and pat in rel_in_db for pat in (ignore_patterns or [])):
            continue
        abs_path, rel_path = _resolve_source_file(rel_in_db, str(source_root))
        name = str(node.get("name") or "")
        kind = str(node.get("kind") or "function")
        start_line = int(node.get("start_line") or 1)
        end_line = int(node.get("end_line") or start_line)

        chunk_text = _slice_source_lines(abs_path, start_line, end_line)
        if not chunk_text:
            logger.warning(
                "code_rag: 无法切出源码 %s L%d-%d,跳过该符号",
                rel_path, start_line, end_line,
            )
            continue

        # O(1) 查 map(不再 spawn CLI)。node id 精确匹配。
        sym_id = str(node.get("id") or "")
        callers = [str(r["name"]) for r in callers_map.get(sym_id, [])]
        callees = [str(r["name"]) for r in callees_map.get(sym_id, [])]
        try:
            imports = _get_imports_for_file(graph_repo_path, rel_path)
        except Exception:
            imports = []

        signature = node.get("signature")
        docstring = node.get("docstring")
        language = str(node.get("language") or "text")

        embed_text = _build_embed_text(
            kind, name, rel_path, signature, docstring, callers, callees, chunk_text,
        )

        idx = per_file_index.get(rel_path, 0)
        per_file_index[rel_path] = idx + 1

        function_name = name if kind in ("function", "method") else None
        class_name = name if kind == "class" else None

        chunks.append(
            {
                "embed_text": embed_text,
                "chunk_text": chunk_text,
                "file_path": rel_path,
                "filename": Path(rel_path).name,
                "language": language,
                "chunk_index": idx,
                "start_line": start_line,
                "end_line": end_line,
                "function_name": function_name,
                "class_name": class_name,
                "callers": callers,
                "callees": callees,
                "imports": imports,
                "signature": signature or "",
                "docstring": docstring or "",
            }
        )

        if progress_cb is not None and (i % 200 == 0 or i == total - 1):
            progress_cb(done=i + 1, total=total, current_file=rel_path)

    # 收尾(空 chunks 也回写一次 total,让进度条到 100%)
    if progress_cb is not None and total == 0:
        progress_cb(done=0, total=0, current_file="")

    return chunks

