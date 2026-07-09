#!/usr/bin/env python3
"""Batch-import source code into the RAG knowledge base.

Sends each file as one POST — the server handles chunking + embedding.
Race condition: if a file is large, many chunks = long server-side encode().
The Qwen3-Embedding model processes ~6-7s/chunk on CPU, so a file that
splits into 40 chunks takes ~4-5 minutes. Use chunk_size=4096 to keep
chunk count minimal.

Usage:
    python scripts/import_to_rag.py --dry-run
    python scripts/import_to_rag.py
    python scripts/import_to_rag.py --include core --include api
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent.parent
API_BASE = "http://localhost:8000"
ADMIN_KEY = "gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o"
HEADERS = {"Authorization": f"Bearer {ADMIN_KEY}", "Content-Type": "application/json"}

# (rel_dir, chunk_strategy, chunk_size, chunk_overlap)
# chunk_size=4096 keeps ~1-6 chunks per file (~6-40s per POST), safe for 600s timeout
FILE_GROUPS = [
    ("aigateway-core/src/aigateway_core", "paragraph", 4096, 256),
    ("aigateway-api/src/aigateway_api", "paragraph", 4096, 256),
    ("control-panel/src", "paragraph", 4096, 256),
    ("aigateway-cli/src/aigateway_cli", "paragraph", 2048, 128),
]

DOC_PATHS = [
    "docs/ARCHITECTURE_DIAGRAM.md",
    "docs/DB_SCHEMA.md",
    "docs/TEST_PLAN.md",
    "config.yaml",
    "config.yaml.template",
    "design.md",
]

SKIP_NAMES = {"__init__.py"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("import_to_rag")


def _should_skip(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    for p in parts:
        if p in {"__pycache__", ".pytest_cache", ".eggs", "build", "dist", "node_modules", ".git", "venv", ".venv"}:
            return True
    if Path(rel_path).suffix in {".pyc", ".pyo", ".so", ".egg-info", ".css"}:
        return True
    if rel_path.endswith(".d.ts") or Path(rel_path).name in SKIP_NAMES:
        return True
    return False


def _estimate_chunks(content: str, strategy: str, chunk_size: int, overlap: int) -> int:
    """Estimate number of chunks without actually building them."""
    if not content or len(content) < 10:
        return 0
    if strategy == "paragraph":
        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
        if not paragraphs:
            return 0
        chunks, current = 0, ""
        for para in paragraphs:
            if len(current) + len(para) + 2 > chunk_size and current:
                chunks += 1
                current = current[-overlap:] + "\n\n" + para if overlap > 0 else para
            else:
                current = current + "\n\n" + para if current else para
        if current:
            chunks += 1
        return chunks or 1
    else:
        return max(len(content) // (chunk_size - overlap), 1)


def collect_files(groups, max_size=0):
    """Return [(rel_path, abs_path, strategy, chunk_size, overlap)]."""
    results = []
    for rel_dir, strategy, chunk_size, overlap in groups:
        d = BASE_DIR / rel_dir
        if not d.exists():
            log.warning("Not found: %s", d)
            continue
        for root, dirs, files in os.walk(d):
            dirs[:] = [x for x in dirs if x not in {"__pycache__", ".git", "node_modules"}]
            for fn in sorted(files):
                ap = os.path.join(root, fn)
                rp = os.path.relpath(ap, BASE_DIR)
                if _should_skip(rp):
                    continue
                if max_size and os.path.getsize(ap) > max_size:
                    continue
                results.append((rp, ap, strategy, chunk_size, overlap))
    return results


def collect_docs(paths):
    """Return [(rel_path, abs_path, 'whole', 0, 0)]."""
    return [(p, str((BASE_DIR / p).resolve()), "whole", 0, 0) for p in paths if (BASE_DIR / p).exists()]


def main():
    parser = argparse.ArgumentParser(description="Import code into RAG.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include", nargs="+", choices=["core", "api", "frontend", "cli", "docs"])
    parser.add_argument("--max-file-size", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=600, help="HTTP timeout per file (s)")
    args = parser.parse_args()

    groups = []
    if not args.include or "core" in args.include or "api" in args.include:
        groups.extend(FILE_GROUPS[:2])
    if not args.include or "frontend" in args.include:
        groups.append(FILE_GROUPS[2])
    if not args.include or "cli" in args.include:
        groups.append(FILE_GROUPS[3])
    include_docs = not args.include or "docs" in args.include

    code_files = collect_files(groups, args.max_file_size)
    doc_files = collect_docs(DOC_PATHS) if include_docs else []
    all_files = code_files + doc_files

    if not all_files:
        log.error("No files!")
        sys.exit(1)

    total_bytes = sum(os.path.getsize(f[1]) for f in all_files)
    total_lines = sum(sum(1 for _ in open(f[1], encoding="utf-8", errors="replace")) for f in all_files)
    total_chunks_est = sum(
        _estimate_chunks(open(f[1], encoding="utf-8", errors="replace").read(), f[2], f[3], f[4])
        if f[2] != "whole" else 1
        for f in all_files
    )

    log.info("=" * 70)
    log.info("RAG Import Summary")
    log.info("=" * 70)
    log.info("Code files : %d  Doc files: %d", len(code_files), len(doc_files))
    log.info("Total size : %.1f KB  Lines: %d", total_bytes / 1024, total_lines)
    log.info("Est chunks : %d  chunk_size=4096/2048", total_chunks_est)
    if args.dry_run:
        log.info("*** DRY RUN ***")
    log.info("=" * 70)

    if args.dry_run:
        for rp, ap, strat, csize, overlap in all_files:
            lc = sum(1 for _ in open(ap, encoding="utf-8", errors="replace"))
            log.info("  %s  (%d lines, %s)", rp, lc, strat)
        return

    ok = err = 0
    n = len(all_files)
    with httpx.Client(timeout=args.timeout) as client:
        for i, (rel_path, abs_path, strategy, chunk_size, overlap) in enumerate(all_files, 1):
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
                if not content.strip():
                    log.info("  [%d/%d] SKIP  %s  (empty)", i, n, rel_path)
                    continue
                payload = {
                    "content": content,
                    "filename": rel_path,
                    "chunk_strategy": strategy if strategy != "whole" else "paragraph",
                    "chunk_size": chunk_size if strategy != "whole" else (len(content) + 1),
                    "chunk_overlap": overlap if strategy != "whole" else 0,
                }
                resp = client.post(f"{API_BASE}/admin/rag/documents", headers=HEADERS, json=payload)
                if resp.status_code == 200:
                    data = resp.json()["data"]
                    ok += 1
                    log.info("  [%d/%d] OK    %s  (%d chunks, %d tokens, %dms)",
                             i, n, rel_path, data["chunk_count"], data["total_tokens"], data["elapsed_ms"])
                else:
                    err += 1
                    log.error("  [%d/%d] ERR   %s  (HTTP %d: %s)", i, n, rel_path, resp.status_code, _trunc(resp.text, 100))
            except Exception as e:
                err += 1
                log.error("  [%d/%d] EXC   %s  (%s)", i, n, rel_path, _trunc(str(e), 100))

    log.info("")
    log.info("=" * 70)
    log.info("OK: %d  ERR: %d", ok, err)
    log.info("=" * 70)
    if err:
        sys.exit(1)


def _trunc(s, n):
    return s[:n] + "..." if len(s) > n else s


if __name__ == "__main__":
    main()
