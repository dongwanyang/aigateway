"""Comment-aware config template schema extraction."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

_DESCRIPTION_FALLBACKS: dict[str, str] = {
    "plugin_runtime.default_timeout_seconds": "插件默认超时（秒），单个插件可用 timeout_seconds 覆盖",
    "auth.api_keys[].key": "API Key 值（强烈建议使用环境变量；空值表示不创建该 Key）",
    "plugins[].enabled": "是否启用该插件",
    "plugins[].depends_on": "依赖的插件名列表（按定义顺序先于本插件执行）",
    "plugins[].config": "插件私有配置（结构因插件而异）",
    "plugins[].config.model_name": "压缩模型名称",
    "plugins[].config.rerank_api_base": "远程 rerank 服务地址（rerank_backend=remote 时使用）",
    "plugins[].config.rerank_api_key": "远程 rerank 服务 API Key（建议环境变量）",
    "plugins[].config.embedding_api_base": "OpenAI 嵌入后端地址（embedding_backend=openai 时使用）",
    "plugins[].config.embedding_api_key": "OpenAI 嵌入后端 API Key（建议环境变量）",
    "providers.*": "提供商标识符（自定义命名）",
    "providers.*.api_key": "API Key（强烈建议使用环境变量）",
    "providers.*.base_url": "API 基地址（OpenAI 兼容格式）",
    "providers.*.timeout": "单次请求超时（秒），超时后触发重试或 fallback",
    "providers.*.num_retries": "最大重试次数（0=不重试）",
    "providers.*.retry_after": "重试间隔（毫秒），实际使用递增退避",
    "providers.*.model_grouper": "模型分组（一个提供商可有多组模型）",
    "providers.*.model_grouper[].models": "模型列表（字典格式）",
    "providers.*.model_grouper[].models[].name": "模型标识符（与提供商模型名一致）",
    "providers.*.model_grouper[].models[].tasks": "文本任务能力，可选 coding | reasoning | summary | vision | general",
    "providers.*.model_grouper[].models[].features": "运行时能力，可选 vision | tool_calling | structured_output | long_context",
    "providers.*.model_grouper[].fallback_models": "降级模型列表（主模型全部失败后尝试）",
    "providers.*.model_grouper[].pricing": "每个模型的定价（$/token）",
    "providers.*.model_grouper[].pricing.*.prompt": "输入 token 单价",
    "providers.*.model_grouper[].pricing.*.completion": "输出 token 单价",
    "debug.plugins.per_plugin.pii_detector": "PII 检测器调试开关",
    "debug.plugins.per_plugin.prompt_cache": "提示词精确缓存调试开关",
    "debug.plugins.per_plugin.semantic_cache": "语义缓存调试开关",
    "debug.plugins.per_plugin.rag_retriever": "RAG 检索增强调试开关",
    "debug.plugins.per_plugin.conv_compressor": "对话历史压缩调试开关",
    "debug.plugins.per_plugin.media_optimizer": "多模态优化调试开关",
    "debug.plugins.per_plugin.ai_director": "Prompt 结构化改写调试开关",
    "debug.plugins.per_plugin.intent_evaluator": "意图评估调试开关",
    "debug.plugins.per_plugin.token_compressor": "视觉 Token 压缩调试开关",
    "debug.plugins.per_plugin.draft_generator": "草图生成调试开关",
    "debug.plugins.per_plugin.gen_model_router": "生成模型路由调试开关",
    "debug.plugins.per_plugin.cost_tracker": "成本追踪调试开关",
    "media_optimization.download_timeouts.image": "图片下载超时（秒）",
    "media_optimization.download_timeouts.video": "视频下载超时（秒）",
    "media_optimization.download_timeouts.audio": "音频下载超时（秒）",
    "media_optimization.download_timeouts.document": "文档下载超时（秒）",
    "intent_classifier.fast_path_confidence": "快速路径置信度",
    "intent_classifier.cache_max_entries": "分类结果缓存最大条目数",
    "task_routing.enabled": "是否启用任务路由策略",
    "task_routing.model_preferences.coding": "编码任务偏好模型",
    "generation_optimization.draft_workflow.comfyui.manager_enabled": "是否启用 ComfyUI Manager",
    "generation_optimization.draft_workflow.comfyui.progress_stall_timeout": "ComfyUI 连续无进度反馈超时（秒）",
    "generation_optimization.draft_workflow.comfyui.checkpoint_name": "SD checkpoint 名称",
    "generation_optimization.draft_workflow.comfyui.video_enabled": "是否启用视频生成",
    "generation_optimization.draft_workflow.comfyui.video_cfg": "视频 CFG 引导强度",
}

_DESCRIPTION_OVERRIDES: dict[str, str] = {
    "providers.*.model_grouper[].models[].capabilities": (
        "模型能力列表，可选 text | image | video"
    ),
}

_TYPE_FALLBACKS: dict[str, str] = {
    "auth.api_keys[].scopes": "string[]",
    "plugins[].depends_on": "string[]",
    "providers.*.model_grouper[].models[].capabilities": "string[]",
    "providers.*.model_grouper[].models[].tasks": "string[]",
    "providers.*.model_grouper[].models[].features": "string[]",
    "providers.*.model_grouper[].fallback_models": "string[]",
    "server.cors_origins": "string[]",
    "media_optimization.image.ocr_languages": "string[]",
    "code_rag.allowed_server_paths": "string[]",
    "code_rag.ignore_patterns": "string[]",
}

_EDITOR_OVERRIDES: dict[str, str] = {
    "auth.api_keys[].scopes": "token_list",
    "plugins[].depends_on": "token_list",
    "providers.*.model_grouper[].models[].capabilities": "token_list",
    "providers.*.model_grouper[].models[].tasks": "token_list",
    "providers.*.model_grouper[].models[].features": "token_list",
    "providers.*.model_grouper[].fallback_models": "token_list",
    "media_optimization.image.ocr_languages": "token_list",
}


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


def _normalized_schema_path(path: tuple[str, ...]) -> str:
    parts = list(path)
    if len(parts) >= 2 and parts[0] == "providers":
        parts[1] = "*"
    if (
        len(parts) >= 3
        and parts[0] == "plugin_runtime"
        and parts[1] == "plugins"
    ):
        parts[2] = "*"
    for index, part in enumerate(parts[:-1]):
        if part == "pricing" and parts[0] == "providers":
            parts[index + 1] = "*"
        if (
            part == "model_capabilities"
            and parts[:3] == [
                "generation_optimization",
                "model_router",
                "model_capabilities",
            ]
        ):
            parts[index + 1] = "*"
    return ".".join(parts)


def _node_value_type(node: Node) -> str:
    if isinstance(node, MappingNode):
        return "object"
    if isinstance(node, SequenceNode):
        if not node.value:
            return "array"
        child_types = {_node_value_type(child) for child in node.value}
        if len(child_types) == 1:
            child_type = next(iter(child_types))
            if child_type in {"string", "integer", "number", "boolean", "null"}:
                return f"{child_type}[]"
        return "array"
    if isinstance(node, ScalarNode):
        tag = node.tag.rsplit(":", 1)[-1]
        return {
            "str": "string",
            "int": "integer",
            "float": "number",
            "bool": "boolean",
            "null": "null",
        }.get(tag, "string")
    return "unknown"


def parse_template_schema(config_path: str) -> list[dict[str, Any]]:
    """Extract concrete and wildcard descriptions from the YAML template."""
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
    comments: dict[str, str] = {}
    value_types: dict[str, str] = {}
    metadata_paths: dict[str, str] = {}
    order: list[str] = []
    visiting: set[int] = set()

    def remember(
        path: str,
        node: Node,
        comment: str,
        metadata_path: str,
    ) -> None:
        if path not in value_types:
            value_types[path] = _TYPE_FALLBACKS.get(
                metadata_path,
                _node_value_type(node),
            )
            metadata_paths[path] = metadata_path
            order.append(path)
        if comment and path not in comments:
            comments[path] = comment

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
                    child_path = (*path, str(key_node.value))
                    exact = ".".join(child_path)
                    normalized = _normalized_schema_path(child_path)
                    line_number = key_node.start_mark.line
                    comment = (
                        _inline_comment(lines[line_number])
                        if 0 <= line_number < len(lines)
                        else ""
                    )
                    remember(exact, value_node, comment, normalized)
                    if normalized != exact:
                        remember(normalized, value_node, comment, normalized)
                    walk(value_node, child_path)
            elif isinstance(node, SequenceNode):
                item_path = _sequence_path(path)
                for item in node.value:
                    walk(item, item_path)
        finally:
            visiting.remove(marker)

    walk(root, ())

    for path, description in _DESCRIPTION_FALLBACKS.items():
        if path in value_types and path not in comments:
            comments[path] = description
    for path, description in _DESCRIPTION_OVERRIDES.items():
        if path in value_types:
            comments[path] = description

    items: list[dict[str, Any]] = []
    for path in order:
        metadata_path = metadata_paths[path]
        description = comments.get(path) or comments.get(metadata_path)
        if not description:
            continue
        item: dict[str, Any] = {
            "path": path,
            "module": path.split(".", 1)[0].replace("[]", ""),
            "description": description,
            "value_type": _TYPE_FALLBACKS.get(
                metadata_path,
                value_types[path],
            ),
        }
        editor = _EDITOR_OVERRIDES.get(metadata_path)
        if editor:
            item["editor"] = editor
        items.append(item)
    return items


__all__ = ["parse_template_schema"]
