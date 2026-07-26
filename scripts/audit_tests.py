#!/usr/bin/env python3
"""Fail CI on test constructs that can turn broken behavior into a green run."""

from __future__ import annotations

import ast
import sys
from pathlib import Path


FORBIDDEN_MARKERS = {"skip", "skipif", "xfail"}


def _dotted_name(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def audit_python(path: Path) -> list[str]:
    violations: list[str] = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [f"{path}:{exc.lineno}: invalid Python: {exc.msg}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = _dotted_name(node.func)
            if name in {
                "pytest.skip",
                "pytest.xfail",
                "pytest.importorskip",
                "unittest.skip",
                "unittest.skipIf",
                "unittest.skipUnless",
            }:
                violations.append(f"{path}:{node.lineno}: forbidden {name}()")
            if any(
                name.endswith(f".mark.{marker}")
                for marker in FORBIDDEN_MARKERS
            ):
                violations.append(f"{path}:{node.lineno}: forbidden {name}")
        elif isinstance(node, ast.Assert):
            try:
                constant = ast.literal_eval(node.test)
            except (ValueError, TypeError):
                continue
            if constant:
                violations.append(
                    f"{path}:{node.lineno}: constant truthy assertion"
                )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("test"):
                continue
            meaningful = [
                stmt
                for stmt in node.body
                if not (
                    isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Constant)
                    and isinstance(stmt.value.value, str)
                )
            ]
            if not meaningful or all(isinstance(stmt, ast.Pass) for stmt in meaningful):
                violations.append(f"{path}:{node.lineno}: empty test body")
    return violations


def audit_typescript(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    forbidden = (
        ".skip(",
        ".only(",
        ".todo(",
        "expect(true).toBe(true)",
        "expect(1).toBe(1)",
    )
    return [
        f"{path}: forbidden test construct {token!r}"
        for token in forbidden
        if token in text
    ]


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    violations: list[str] = []
    for path in sorted((root / "tests").rglob("*.py")):
        if "__pycache__" not in path.parts:
            violations.extend(audit_python(path.relative_to(root)))
    for pattern in ("*.test.ts", "*.test.tsx"):
        for path in sorted((root / "control-panel" / "src").rglob(pattern)):
            violations.extend(audit_typescript(path.relative_to(root)))

    if violations:
        print("Test authenticity audit failed:", file=sys.stderr)
        print("\n".join(f"- {item}" for item in violations), file=sys.stderr)
        return 1
    print("Test authenticity audit passed: no skip/xfail/trivial-pass constructs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
