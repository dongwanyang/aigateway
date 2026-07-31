"""Comment-aware config template schema extraction."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode


def _template_candidates(config_path: str) -> list[Path]:
    here = Path(__file__).resolve().parent
    configured = os.environ.get("AI_GATEWAY_CONFIG_TEMPLATE_PATH", "").strip()
    candidates = [
        Path(configured).expanduser() if configured else None,
        Path.cwd() / "config.yaml.template",
        Path(config_path).resolve().parent / "config.yaml.template",
        here.parents[2] / "config.yaml.template",
        here.parents[3] / "config.yaml.template",
    ]
    return [path for path in candidates if path is not None]


def _clean_inline_comment(comment: str) -> str:
    value = comment.strip()
    while value.startswith("="):
        value = value[1:].lstrip()
    while value.endswith("="):
        value = value[:-1].rstrip()
    return value


def _inline_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for index, char in enumerate(line):
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if quote == "'":
            if char == quote:
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "#" and (index == 0 or line[index - 1].isspace()):
            return _clean_inline_comment(line[index + 1 :])
    return ""


def _sequence_path(path: tuple[str, ...]) -> tuple[str, ...]:
    if not path:
        return ("[]",)
    return (*path[:-1], f"{path[-1]}[]")


def parse_template_schema(config_path: str) -> list[dict[str, Any]]:
    """Extract inline parameter comments with YAML-accurate list paths."""
    template_path = next(
        (path for path in _template_candidates(config_path) if path.is_file()),
        None,
    )
    if template_path is None:
        return []

    try:
        text = template_path.read_text(encoding="utf-8")
        root = yaml.compose(text)
    except (OSError, yaml.YAMLError):
        return []
    if root is None:
        return []

    lines = text.splitlines()
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    visiting: set[int] = set()

    def walk(node: Node, path: tuple[str, ...]) -> None:
        marker = id(node)
        if marker in visiting:
            return
        visiting.add(marker)
        try:
            if isinstance(node, MappingNode):
                for key_node, value_node in node.value:
                    if not isinstance(key_node, ScalarNode):
                        continue
                    key = str(key_node.value)
                    child_path = (*path, key)
                    line_number = key_node.start_mark.line
                    comment = (
                        _inline_comment(lines[line_number])
                        if 0 <= line_number < len(lines)
                        else ""
                    )
                    dotted = ".".join(child_path)
                    if comment and dotted not in seen:
                        items.append(
                            {
                                "path": dotted,
                                "module": child_path[0].replace("[]", ""),
                                "description": comment,
                            }
                        )
                        seen.add(dotted)
                    walk(value_node, child_path)
            elif isinstance(node, SequenceNode):
                item_path = _sequence_path(path)
                for item in node.value:
                    walk(item, item_path)
        finally:
            visiting.remove(marker)

    walk(root, ())
    return items


__all__ = ["parse_template_schema"]
