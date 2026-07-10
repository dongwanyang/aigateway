"""
PromptTemplateManager — 提示词模板 CRUD 管理器
===============================================

提供 Prompt_Template 资源的完整 CRUD 操作和模板渲染功能。

Redis Key 格式:
    模板数据: aigateway:prompt_template:{owner_id}:{template_name}
    名称索引: aigateway:prompt_template_index:{owner_id} (Redis SET)

其中 owner_id = '' (public) | group_id (group) | user_id (private)。

功能:
- 创建/获取/列表/更新/删除模板
- 模板名称验证: 1-64 字符，字母数字/连字符/下划线
- 内容最大 10000 字符，描述最大 500 字符
- 分页查询（默认 20 条/页，最大 100 条）
- 渲染模板: 替换 {{variable_name}} 占位符

需求: 8.1, 8.2, 8.3, 8.4
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional

from aigateway_core.pipelines.generation._common.config import PromptTemplateConfig
from aigateway_core.pipelines.generation._common.exceptions import TemplateValidationError
from aigateway_core.pipelines.generation._common.models import PromptTemplate

logger = logging.getLogger(__name__)

# 模板名称验证正则: 1-64 字符，字母数字、连字符、下划线
_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]+$")

# 模板占位符正则: {{variable_name}}
_PLACEHOLDER_PATTERN = re.compile(r"\{\{(\w+)\}\}")


class PromptTemplateManager:
    """提示词模板管理器 — 提供模板的 CRUD 操作和渲染功能.

    模板存储在 Redis 中，Key 格式:
        aigateway:prompt_template:{owner_id}:{template_name}

    owner_id 语义: "" (public) | group_id (group) | user_id (private).

    每个 owner 有一个对应的 Redis SET 作为模板名称索引:
        aigateway:prompt_template_index:{owner_id}

    用法:
        manager = PromptTemplateManager(redis_client, config)
        template = await manager.create("key123", "my-template", "Hello {{name}}!")
        rendered = manager.render(template, {"name": "World"})
    """

    KEY_PREFIX = "aigateway:prompt_template"
    INDEX_PREFIX = "aigateway:prompt_template_index"

    def __init__(self, redis_client: Any, config: PromptTemplateConfig) -> None:
        """初始化模板管理器.

        Args:
            redis_client: Redis 客户端实例（需有 .redis 属性），None 时使用内存存储。
            config: PromptTemplateConfig 配置实例。
        """
        self._redis_client = redis_client
        self._config = config
        # 内存存储回退（用于测试或 Redis 不可用时）
        self._memory_store: Dict[str, str] = {}
        self._memory_index: Dict[str, set] = {}

    def _build_key(self, owner_id: str, name: str) -> str:
        """构建模板数据的 Redis Key.

        格式: aigateway:prompt_template:{owner_id}:{name}
        """
        return f"{self.KEY_PREFIX}:{owner_id}:{name}"

    def _build_index_key(self, owner_id: str) -> str:
        """构建模板索引的 Redis Key.

        格式: aigateway:prompt_template_index:{owner_id}
        """
        return f"{self.INDEX_PREFIX}:{owner_id}"

    def _validate_name(self, name: str) -> None:
        """验证模板名称.

        规则: 1-{max_name_length} 字符，仅允许字母数字、连字符、下划线。

        Args:
            name: 模板名称

        Raises:
            TemplateValidationError: 名称不合法
        """
        max_len = self._config.max_name_length
        if not name:
            raise TemplateValidationError("模板名称不能为空")
        if len(name) > max_len:
            raise TemplateValidationError(
                f"模板名称长度超过限制: {len(name)} > {max_len}"
            )
        if not _NAME_PATTERN.match(name):
            raise TemplateValidationError(
                f"模板名称格式无效: '{name}'，仅允许字母数字、连字符和下划线"
            )

    def _validate_content(self, content: str) -> None:
        """验证模板内容.

        Args:
            content: 模板内容

        Raises:
            TemplateValidationError: 内容不合法
        """
        if not content:
            raise TemplateValidationError("模板内容不能为空")
        max_len = self._config.max_content_length
        if len(content) > max_len:
            raise TemplateValidationError(
                f"模板内容长度超过限制: {len(content)} > {max_len}"
            )

    def _validate_description(self, description: str) -> None:
        """验证模板描述.

        Args:
            description: 模板描述

        Raises:
            TemplateValidationError: 描述不合法
        """
        max_len = self._config.max_description_length
        if len(description) > max_len:
            raise TemplateValidationError(
                f"模板描述长度超过限制: {len(description)} > {max_len}"
            )

    def _serialize_template(self, template: PromptTemplate) -> str:
        """将模板序列化为 JSON 字符串."""
        return json.dumps(
            {
                "name": template.name,
                "content": template.content,
                "description": template.description,
                "api_key_id": template.api_key_id,
                "created_at": template.created_at,
                "updated_at": template.updated_at,
            },
            ensure_ascii=False,
        )

    def _deserialize_template(self, data: str) -> PromptTemplate:
        """将 JSON 字符串反序列化为 PromptTemplate."""
        obj = json.loads(data)
        return PromptTemplate(
            name=obj["name"],
            content=obj["content"],
            description=obj.get("description", ""),
            api_key_id=obj.get("api_key_id", ""),
            created_at=obj.get("created_at", 0.0),
            updated_at=obj.get("updated_at", 0.0),
        )

    @property
    def _use_memory(self) -> bool:
        """是否使用内存存储（redis_client 为 None 时）."""
        return self._redis_client is None

    async def create(
        self, owner_id: str, name: str, content: str, description: str = ""
    ) -> PromptTemplate:
        """创建模板.

        验证名称、内容、描述，检查重名，然后存储到 Redis。

        Args:
            owner_id: 所有者标识 (''=public | group_id=group | user_id=private)
            name: 模板名称 (1-64 字符, 字母数字/连字符/下划线)
            content: 模板内容 (最大 10000 字符)
            description: 模板描述 (最大 500 字符, 可选)

        Returns:
            创建的 PromptTemplate 实例

        Raises:
            TemplateValidationError: 参数验证失败或名称重复
        """
        # 验证输入
        self._validate_name(name)
        self._validate_content(content)
        self._validate_description(description)

        # 检查重名（需求 8.7）
        existing = await self.get(owner_id, name)
        if existing is not None:
            raise TemplateValidationError(
                f"模板名称已存在: '{name}' (owner_id={owner_id})"
            )

        now = time.time()
        template = PromptTemplate(
            name=name,
            content=content,
            description=description,
            api_key_id=owner_id,
            created_at=now,
            updated_at=now,
        )

        serialized = self._serialize_template(template)

        if self._use_memory:
            key = self._build_key(owner_id, name)
            self._memory_store[key] = serialized
            if owner_id not in self._memory_index:
                self._memory_index[owner_id] = set()
            self._memory_index[owner_id].add(name)
        else:
            redis = self._redis_client.redis
            key = self._build_key(owner_id, name)
            index_key = self._build_index_key(owner_id)
            await redis.set(key, serialized)
            await redis.sadd(index_key, name)

        logger.info(
            "prompt_template.created",
            extra={"owner_id": owner_id, "name": name},
        )
        return template

    async def get(self, owner_id: str, name: str) -> Optional[PromptTemplate]:
        """获取模板.

        Args:
            owner_id: 所有者标识 (''=public | group_id=group | user_id=private)
            name: 模板名称

        Returns:
            PromptTemplate 实例，不存在时返回 None
        """
        key = self._build_key(owner_id, name)

        if self._use_memory:
            raw = self._memory_store.get(key)
            if raw is None:
                return None
            return self._deserialize_template(raw)
        else:
            redis = self._redis_client.redis
            raw = await redis.get(key)
            if raw is None:
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            return self._deserialize_template(raw)

    async def list(
        self, owner_id: str, page: int = 1, page_size: int = 20
    ) -> Dict[str, Any]:
        """列出该 owner 的所有模板（分页）.

        Args:
            owner_id: 所有者标识
            page: 页码（从 1 开始, 默认: 1）
            page_size: 每页条数（默认: 20, 最大: 100）

        Returns:
            包含分页元数据和模板列表的字典:
            {
                "items": List[PromptTemplate],
                "total": int,
                "page": int,
                "page_size": int,
                "total_pages": int,
            }
        """
        # 限制 page_size 范围
        page_size = max(1, min(page_size, self._config.max_page_size))
        page = max(1, page)

        if self._use_memory:
            names = sorted(self._memory_index.get(owner_id, set()))
        else:
            redis = self._redis_client.redis
            index_key = self._build_index_key(owner_id)
            raw_names = await redis.smembers(index_key)
            names = sorted(
                n.decode("utf-8") if isinstance(n, bytes) else n for n in raw_names
            )

        total = len(names)
        total_pages = max(1, (total + page_size - 1) // page_size)

        # 计算分页偏移
        start = (page - 1) * page_size
        end = start + page_size
        page_names = names[start:end]

        # 批量获取模板数据
        items: List[PromptTemplate] = []
        for name in page_names:
            template = await self.get(owner_id, name)
            if template is not None:
                items.append(template)

        return {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
        }

    async def update(
        self, owner_id: str, name: str, content: str, description: str = ""
    ) -> PromptTemplate:
        """更新模板.

        Args:
            owner_id: 所有者标识
            name: 模板名称
            content: 新的模板内容
            description: 新的模板描述

        Returns:
            更新后的 PromptTemplate 实例

        Raises:
            TemplateValidationError: 模板不存在或参数验证失败
        """
        # 验证输入
        self._validate_content(content)
        self._validate_description(description)

        # 检查模板是否存在
        existing = await self.get(owner_id, name)
        if existing is None:
            raise TemplateValidationError(
                f"模板不存在: '{name}' (owner_id={owner_id})"
            )

        # 更新字段
        now = time.time()
        updated_template = PromptTemplate(
            name=existing.name,
            content=content,
            description=description,
            api_key_id=existing.api_key_id,
            created_at=existing.created_at,
            updated_at=now,
        )

        serialized = self._serialize_template(updated_template)

        if self._use_memory:
            key = self._build_key(owner_id, name)
            self._memory_store[key] = serialized
        else:
            redis = self._redis_client.redis
            key = self._build_key(owner_id, name)
            await redis.set(key, serialized)

        logger.info(
            "prompt_template.updated",
            extra={"owner_id": owner_id, "name": name},
        )
        return updated_template

    async def delete(self, owner_id: str, name: str) -> bool:
        """删除模板.

        Args:
            owner_id: 所有者标识
            name: 模板名称

        Returns:
            True 删除成功，False 模板不存在
        """
        # 检查模板是否存在
        existing = await self.get(owner_id, name)
        if existing is None:
            return False

        if self._use_memory:
            key = self._build_key(owner_id, name)
            self._memory_store.pop(key, None)
            if owner_id in self._memory_index:
                self._memory_index[owner_id].discard(name)
        else:
            redis = self._redis_client.redis
            key = self._build_key(owner_id, name)
            index_key = self._build_index_key(owner_id)
            await redis.delete(key)
            await redis.srem(index_key, name)

        logger.info(
            "prompt_template.deleted",
            extra={"owner_id": owner_id, "name": name},
        )
        return True

    def render(self, template: PromptTemplate, variables: Dict[str, str]) -> str:
        """渲染模板，替换 {{variable_name}} 占位符.

        扫描模板内容中的所有 {{variable_name}} 占位符，
        用 variables 中的对应值替换。如果有占位符在 variables 中
        未找到对应值，则抛出 TemplateValidationError。

        Args:
            template: PromptTemplate 实例
            variables: 占位符变量名到值的映射

        Returns:
            替换完成后的字符串

        Raises:
            TemplateValidationError: 缺少模板中需要的变量
        """
        # 提取模板中所有占位符变量名
        required_vars = set(_PLACEHOLDER_PATTERN.findall(template.content))

        # 检查是否有缺失的变量
        provided_vars = set(variables.keys())
        missing_vars = required_vars - provided_vars
        if missing_vars:
            raise TemplateValidationError(
                f"缺少模板变量: {sorted(missing_vars)}"
            )

        # 执行替换
        def replace_match(match: re.Match) -> str:
            var_name = match.group(1)
            return variables[var_name]

        rendered = _PLACEHOLDER_PATTERN.sub(replace_match, template.content)
        return rendered
