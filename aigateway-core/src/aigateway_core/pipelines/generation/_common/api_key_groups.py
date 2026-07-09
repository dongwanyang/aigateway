"""
API Key 分组管理工具
====================

从 auth 配置中构建 API Key user_id 到 group 标签的映射。

该映射用于将 group 标签注入 Prometheus 成本追踪指标（api_key_group label）。
group 字段不影响资源隔离逻辑——模板和特征缓存仍按单独 API Key 隔离。

需求: 9.1, 9.2, 9.4, 9.5
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from aigateway_core.pipelines.generation._common.metrics import DEFAULT_API_KEY_GROUP

logger = logging.getLogger(__name__)


def build_api_key_groups(auth_config: Dict[str, Any]) -> Dict[str, str]:
    """Build api_key_id to group mapping from auth config.

    Scans auth.api_keys and extracts the 'group' field for each key.
    Keys without a group get 'default'.

    Args:
        auth_config: The 'auth' section of config.yaml

    Returns:
        Dict mapping api_key user_id to group label
    """
    groups: Dict[str, str] = {}

    if not auth_config or not isinstance(auth_config, dict):
        logger.debug("auth_config 为空或非字典，返回空映射")
        return groups

    api_keys = auth_config.get("api_keys", [])

    if not isinstance(api_keys, list):
        logger.warning("auth.api_keys 应为列表，实际类型: %s", type(api_keys).__name__)
        return groups

    for entry in api_keys:
        if not isinstance(entry, dict):
            logger.debug("跳过非字典 API Key 条目: %s", type(entry).__name__)
            continue

        user_id = entry.get("user_id")
        if not user_id:
            logger.debug("跳过无 user_id 的 API Key 条目")
            continue

        group = entry.get("group", DEFAULT_API_KEY_GROUP)
        if not isinstance(group, str) or not group.strip():
            group = DEFAULT_API_KEY_GROUP

        groups[str(user_id)] = group.strip()

    logger.info(
        "构建 API Key 分组映射完成: %d 个 Key, %d 个分组",
        len(groups),
        len(set(groups.values())),
    )

    return groups
