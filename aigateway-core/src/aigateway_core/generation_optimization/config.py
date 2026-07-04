"""
配置管理 — 生成优化层配置数据结构
=================================

定义 generation_optimization 配置节的所有 dataclass 配置类，
支持从 YAML 加载和环境变量覆盖。

功能:
- 所有配置 dataclass（AIDirectorConfig, ModelRouterConfig 等）
- GenerationOptimizationConfig 主配置类聚合所有子配置
- load_from_dict() 从字典创建配置实例
- load_from_config_manager() 从 ConfigManager 加载
- 配置校验: 类型检查 + 范围检查，无效值保留旧值并记录错误日志
- 环境变量覆盖: AI_GATEWAY_GENERATION_OPTIMIZATION_* 前缀

需求: 6.1, 6.2, 6.3, 6.4, 6.7
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, fields
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ==================================================================
# 子配置 dataclass 定义
# ==================================================================


@dataclass
class AIDirectorConfig:
    """AI 导演配置.

    Attributes:
        enabled: 是否启用 AI Director 策略 (默认: True)
        rewrite_model: 改写使用的模型名称 (默认: "gpt-4o-mini")
        timeout_seconds: 模型调用超时时间/秒 (默认: 10.0, 范围: 1.0-120.0)
        max_prompt_length: 优化后 prompt 最大长度/字符 (默认: 2000, 范围: 100-50000)
        min_prompt_length: 触发自动扩展的最短 prompt 长度 (默认: 10, 范围: 1-1000)
        prompt_confirmation_enabled: 是否启用 prompt 确认流程 (默认: True)
    """

    enabled: bool = True
    rewrite_model: str = "gpt-4o-mini"
    timeout_seconds: float = 10.0
    max_prompt_length: int = 2000
    min_prompt_length: int = 10
    prompt_confirmation_enabled: bool = True


@dataclass
class ModelRouterConfig:
    """模型路由配置.

    Attributes:
        enabled: 是否启用模型路由 (默认: True)
        default_model: 评估失败时的默认模型 (默认: "agnes-2.0-flash")
        evaluation_timeout_seconds: 意图评估超时/秒 (默认: 2.0, 范围: 0.5-30.0)
        default_capability_score: 未注册模型的默认能力评分 (默认: 50, 范围: 0-100)
        model_capabilities: 模型能力评分映射 model_name -> score(0-100)
        model_modalities: 模型模态分类映射 model_name -> ["llm"|"mllm"|"generative", ...]
            一个模型可属于多个模态（列表元素）
    """

    enabled: bool = True
    default_model: str = "agnes-2.0-flash"
    evaluation_timeout_seconds: float = 2.0
    default_capability_score: int = 50
    model_capabilities: Dict[str, int] = field(default_factory=dict)
    model_modalities: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class DraftWorkflowConfig:
    """Draft-to-HiRes 工作流配置.

    Attributes:
        enabled: 是否启用 Draft 工作流 (默认: True)
        draft_resolution: 草图分辨率 (默认: (512, 512))
        default_target_resolution: 默认目标分辨率 (默认: (1920, 1080))
        max_target_resolution: 最大允许目标分辨率 (默认: (4096, 4096))
        max_regeneration_attempts: 最大重试次数 (默认: 5, 范围: 1-50)
        retention_period_hours: 草图保留时间/小时 (默认: 24, 范围: 1-168)
        preview_video_duration_seconds: 预览视频时长/秒 (默认: 30, 范围: 1-300)
        preview_keyframe_interval_seconds: 关键帧间隔/秒 (默认: 5, 范围: 1-60)
        preview_video_fps: 预览视频帧率 (默认: 8, 范围: 1-30)
        target_fps: 目标帧率 (默认: 60, 范围: 24-120)
        target_fps_range: 允许的目标帧率范围 (默认: (24, 120))
        upscale_algorithm: 放大算法名称 (默认: "real-esrgan")
    """

    enabled: bool = True
    draft_resolution: Tuple[int, int] = (512, 512)
    default_target_resolution: Tuple[int, int] = (1920, 1080)
    max_target_resolution: Tuple[int, int] = (4096, 4096)
    max_regeneration_attempts: int = 5
    retention_period_hours: int = 24
    preview_video_duration_seconds: int = 30
    preview_keyframe_interval_seconds: int = 5
    preview_video_fps: int = 8
    target_fps: int = 60
    target_fps_range: Tuple[int, int] = (24, 120)
    upscale_algorithm: str = "real-esrgan"


@dataclass
class TokenCompressorConfig:
    """Token 压缩配置.

    Attributes:
        enabled: 是否启用 Token 压缩 (默认: True)
        target_compression_ratio: 目标压缩率 (默认: 0.5, 范围: 0.2-0.9)
        min_compression_ratio: 最小压缩率 (默认: 0.2)
        max_compression_ratio: 最大压缩率 (默认: 0.9)
        max_vector_dimensions: Feature Vector 最大维度 (默认: 512, 范围: 1-4096)
        timeout_seconds: 单图压缩超时/秒 (默认: 30.0, 范围: 1.0-300.0)
        supported_formats: 支持的图片格式列表
        max_images_per_request: 每请求最大图片数 (默认: 10, 范围: 1-100)
        max_image_size_bytes: 单图最大字节数 (默认: 20MB)
    """

    enabled: bool = True
    target_compression_ratio: float = 0.5
    min_compression_ratio: float = 0.2
    max_compression_ratio: float = 0.9
    max_vector_dimensions: int = 512
    timeout_seconds: float = 30.0
    supported_formats: List[str] = field(
        default_factory=lambda: ["image/png", "image/jpeg", "image/webp", "image/bmp"]
    )
    max_images_per_request: int = 10
    max_image_size_bytes: int = 20 * 1024 * 1024  # 20 MB


@dataclass
class FeatureCacheConfig:
    """特征缓存配置.

    Attributes:
        enabled: 是否启用特征缓存 (默认: True)
        ttl_days: 缓存 TTL/天 (默认: 30, 范围: 1-365)
        lookup_timeout_ms: 缓存查找超时/毫秒 (默认: 500, 范围: 50-5000)
        extraction_model_version: 特征提取模型版本 (默认: "clip-vit-large-patch14")
    """

    enabled: bool = True
    ttl_days: int = 30
    lookup_timeout_ms: int = 500
    extraction_model_version: str = "clip-vit-large-patch14"


@dataclass
class CostTrackingConfig:
    """成本追踪配置.

    Attributes:
        enabled: 是否启用成本追踪 (默认: True)
        assumed_retry_rate: 假定重试率 (默认: 0.3, 范围: 0.0-1.0)
        precision_decimal_places: 成本精度/小数位 (默认: 6, 范围: 1-10)
    """

    enabled: bool = True
    assumed_retry_rate: float = 0.3
    precision_decimal_places: int = 6


@dataclass
class PromptTemplateConfig:
    """提示词模板配置.

    Attributes:
        enabled: 是否启用模板功能 (默认: True)
        default_page_size: 默认分页大小 (默认: 20, 范围: 1-100)
        max_page_size: 最大分页大小 (默认: 100, 范围: 1-1000)
        max_name_length: 模板名称最大长度 (默认: 64, 范围: 1-256)
        max_content_length: 模板内容最大长度 (默认: 10000, 范围: 1-100000)
        max_description_length: 描述最大长度 (默认: 500, 范围: 0-5000)
    """

    enabled: bool = True
    default_page_size: int = 20
    max_page_size: int = 100
    max_name_length: int = 64
    max_content_length: int = 10000
    max_description_length: int = 500


# ==================================================================
# 配置校验规则定义
# ==================================================================

# 每个字段的合法范围定义: {field_name: (min, max)}
# 适用于数值类型字段
_VALIDATION_RULES: Dict[str, Dict[str, Tuple[float, float]]] = {
    "ai_director": {
        "timeout_seconds": (1.0, 120.0),
        "max_prompt_length": (100, 50000),
        "min_prompt_length": (1, 1000),
    },
    "model_router": {
        "evaluation_timeout_seconds": (0.5, 30.0),
    },
    "draft_workflow": {
        "max_regeneration_attempts": (1, 50),
        "retention_period_hours": (1, 168),
        "preview_video_duration_seconds": (1, 300),
        "preview_keyframe_interval_seconds": (1, 60),
        "preview_video_fps": (1, 30),
        "target_fps": (24, 120),
    },
    "token_compressor": {
        "target_compression_ratio": (0.2, 0.9),
        "min_compression_ratio": (0.01, 0.99),
        "max_compression_ratio": (0.01, 0.99),
        "max_vector_dimensions": (1, 4096),
        "timeout_seconds": (1.0, 300.0),
        "max_images_per_request": (1, 100),
        "max_image_size_bytes": (1024, 1024 * 1024 * 1024),  # 1KB - 1GB
    },
    "feature_cache": {
        "ttl_days": (1, 365),
        "lookup_timeout_ms": (50, 5000),
    },
    "cost_tracking": {
        "assumed_retry_rate": (0.0, 1.0),
        "precision_decimal_places": (1, 10),
    },
    "prompt_templates": {
        "default_page_size": (1, 100),
        "max_page_size": (1, 1000),
        "max_name_length": (1, 256),
        "max_content_length": (1, 100000),
        "max_description_length": (0, 5000),
    },
}

# 子配置名称到 dataclass 类型的映射
_SUB_CONFIG_CLASSES: Dict[str, Type[Any]] = {
    "ai_director": AIDirectorConfig,
    "model_router": ModelRouterConfig,
    "draft_workflow": DraftWorkflowConfig,
    "token_compressor": TokenCompressorConfig,
    "feature_cache": FeatureCacheConfig,
    "cost_tracking": CostTrackingConfig,
    "prompt_templates": PromptTemplateConfig,
}

# 环境变量前缀
_ENV_PREFIX = "AI_GATEWAY_GENERATION_OPTIMIZATION"


# ==================================================================
# 工具函数
# ==================================================================


def _parse_env_value(value: str) -> Any:
    """解析环境变量字符串为合适的 Python 类型.

    解析优先级: JSON → bool → int → float → str
    """
    import json as _json

    # JSON (数组、对象)
    try:
        parsed = _json.loads(value)
        return parsed
    except (ValueError, _json.JSONDecodeError):
        pass

    # bool
    if value.lower() in ("true", "yes", "1"):
        return True
    if value.lower() in ("false", "no", "0"):
        return False

    # int
    try:
        return int(value)
    except ValueError:
        pass

    # float
    try:
        return float(value)
    except ValueError:
        pass

    return value


def _resolve_type(type_hint: Any) -> Any:
    """将字符串类型注解或泛型类型解析为可用的类型信息.

    由于使用了 `from __future__ import annotations`，dataclass field.type
    可能是字符串形式，需要在运行时解析。

    Returns:
        解析后的类型信息 (origin, args, raw_type)
    """
    import typing

    # 如果是字符串形式的类型注解，直接判断常见模式
    if isinstance(type_hint, str):
        hint = type_hint.strip()
        if hint.startswith("Tuple[") or hint.startswith("tuple["):
            return (tuple, "int", hint)
        if hint.startswith("List[") or hint.startswith("list["):
            return (list, None, hint)
        if hint.startswith("Dict[") or hint.startswith("dict["):
            return (dict, None, hint)
        if hint == "bool":
            return (None, None, bool)
        if hint == "int":
            return (None, None, int)
        if hint == "float":
            return (None, None, float)
        if hint == "str":
            return (None, None, str)
        return (None, None, None)

    # 泛型类型
    origin = getattr(type_hint, "__origin__", None)
    args = getattr(type_hint, "__args__", None)
    return (origin, args, type_hint)


def _coerce_value(value: Any, target_type: Any, field_name: str) -> Any:
    """将值转换为目标类型，支持 Tuple 和 List 特殊处理.

    Args:
        value: 原始值
        target_type: 目标类型（可能是字符串注解或实际类型）
        field_name: 字段名称（用于日志）

    Returns:
        转换后的值

    Raises:
        TypeError: 无法转换
        ValueError: 值不合法
    """
    # 处理 None
    if value is None:
        raise TypeError(f"值不能为 None: {field_name}")

    origin, args, raw_type = _resolve_type(target_type)

    # Tuple[int, int] — 从列表转换
    if origin is tuple:
        if isinstance(value, (list, tuple)):
            # 假设所有 Tuple 元素都是 int（我们的配置中所有 Tuple 都是 Tuple[int, int]）
            return tuple(int(v) for v in value)
        raise TypeError(f"无法将 {type(value).__name__} 转换为 Tuple: {field_name}")

    # List[str] — 确保是列表
    if origin is list:
        if isinstance(value, list):
            return value
        raise TypeError(f"无法将 {type(value).__name__} 转换为 List: {field_name}")

    # Dict[str, X]
    if origin is dict:
        if isinstance(value, dict):
            return value
        raise TypeError(f"无法将 {type(value).__name__} 转换为 Dict: {field_name}")

    # 基本类型
    if raw_type is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            if value.lower() in ("true", "yes", "1"):
                return True
            if value.lower() in ("false", "no", "0"):
                return False
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise TypeError(f"无法将 {value!r} 转换为 bool: {field_name}")

    if raw_type is int:
        if isinstance(value, bool):
            raise TypeError(f"bool 不能作为 int 使用: {field_name}")
        return int(value)

    if raw_type is float:
        if isinstance(value, bool):
            raise TypeError(f"bool 不能作为 float 使用: {field_name}")
        return float(value)

    if raw_type is str:
        return str(value)

    # 直接返回
    return value


def _validate_range(
    value: Any,
    field_name: str,
    section_name: str,
) -> bool:
    """校验字段值是否在允许范围内.

    Returns:
        True 表示合法，False 表示不合法
    """
    rules = _VALIDATION_RULES.get(section_name, {})
    if field_name not in rules:
        return True

    min_val, max_val = rules[field_name]
    try:
        numeric_val = float(value)
        if numeric_val < min_val or numeric_val > max_val:
            logger.error(
                "配置值超出范围: generation_optimization.%s.%s = %s "
                "(允许范围: [%s, %s])，保留旧值",
                section_name,
                field_name,
                value,
                min_val,
                max_val,
            )
            return False
    except (TypeError, ValueError):
        return False

    return True


def _load_sub_config(
    cls: Type[T],
    data: Dict[str, Any],
    section_name: str,
    previous: Optional[T] = None,
) -> T:
    """从字典创建子配置 dataclass 实例，带校验.

    无效字段保留 previous 的对应值（如果提供），否则使用 dataclass 默认值。

    Args:
        cls: dataclass 类
        data: 配置字典
        section_name: 配置节名称（用于日志和校验）
        previous: 之前的有效配置实例

    Returns:
        新的配置实例
    """
    # 如果没有传入 data 或者 data 为空，则返回 previous 或默认实例
    if not data:
        return previous if previous is not None else cls()

    # 创建默认实例获取默认值
    default_instance = cls()
    kwargs: Dict[str, Any] = {}

    for f in fields(cls):
        if f.name not in data:
            # 未提供的字段使用 previous 或默认值
            if previous is not None:
                kwargs[f.name] = getattr(previous, f.name)
            else:
                kwargs[f.name] = getattr(default_instance, f.name)
            continue

        raw_value = data[f.name]

        # 尝试类型转换
        try:
            coerced = _coerce_value(raw_value, f.type, f.name)
        except (TypeError, ValueError) as exc:
            logger.error(
                "配置类型错误: generation_optimization.%s.%s = %r (%s)，保留旧值",
                section_name,
                f.name,
                raw_value,
                exc,
            )
            # 保留旧值
            if previous is not None:
                kwargs[f.name] = getattr(previous, f.name)
            else:
                kwargs[f.name] = getattr(default_instance, f.name)
            continue

        # 范围校验
        if not _validate_range(coerced, f.name, section_name):
            # 保留旧值
            if previous is not None:
                kwargs[f.name] = getattr(previous, f.name)
            else:
                kwargs[f.name] = getattr(default_instance, f.name)
            continue

        kwargs[f.name] = coerced

    return cls(**kwargs)


def _get_env_overrides() -> Dict[str, Dict[str, Any]]:
    """扫描环境变量，提取 AI_GATEWAY_GENERATION_OPTIMIZATION_* 覆盖.

    环境变量命名规则:
    AI_GATEWAY_GENERATION_OPTIMIZATION_<SECTION>_<FIELD> → section.field

    例如:
    AI_GATEWAY_GENERATION_OPTIMIZATION_AI_DIRECTOR_TIMEOUT_SECONDS=15
      → ai_director.timeout_seconds = 15.0
    AI_GATEWAY_GENERATION_OPTIMIZATION_ENABLED=false
      → enabled = False

    Returns:
        嵌套字典 {section_name: {field_name: value}}
        顶层键直接放在 "_top" 下
    """
    prefix = _ENV_PREFIX + "_"
    overrides: Dict[str, Dict[str, Any]] = {"_top": {}}

    # 已知的 section 名前缀（大写形式，用于匹配）
    known_sections = {
        "AI_DIRECTOR": "ai_director",
        "MODEL_ROUTER": "model_router",
        "DRAFT_WORKFLOW": "draft_workflow",
        "TOKEN_COMPRESSOR": "token_compressor",
        "FEATURE_CACHE": "feature_cache",
        "COST_TRACKING": "cost_tracking",
        "PROMPT_TEMPLATES": "prompt_templates",
    }

    for env_key, env_val in sorted(os.environ.items()):
        if not env_key.startswith(prefix):
            continue

        # 去掉前缀后的部分
        remainder = env_key[len(prefix):]

        # 尝试匹配已知 section
        matched_section = None
        field_part = None

        for section_upper, section_lower in known_sections.items():
            section_prefix = section_upper + "_"
            if remainder.startswith(section_prefix):
                matched_section = section_lower
                field_part = remainder[len(section_prefix):].lower()
                break

        if matched_section and field_part:
            if matched_section not in overrides:
                overrides[matched_section] = {}
            overrides[matched_section][field_part] = _parse_env_value(env_val)
        else:
            # 顶层字段 (如 ENABLED)
            overrides["_top"][remainder.lower()] = _parse_env_value(env_val)

    return overrides


# ==================================================================
# 主配置类
# ==================================================================


@dataclass
class GenerationOptimizationConfig:
    """生成优化层主配置 — 映射 config.yaml 中的 generation_optimization 节.

    聚合所有子配置模块，提供统一的配置访问入口。
    支持从 YAML 字典加载、从 ConfigManager 加载、环境变量覆盖和配置校验。

    Attributes:
        enabled: 全局开关，是否启用生成优化层 (默认: True)
        ai_director: AI 导演配置
        model_router: 模型路由配置
        draft_workflow: Draft-to-HiRes 工作流配置
        token_compressor: Token 压缩配置
        feature_cache: 特征缓存配置
        cost_tracking: 成本追踪配置
        prompt_templates: 提示词模板配置
    """

    enabled: bool = True
    ai_director: AIDirectorConfig = field(default_factory=AIDirectorConfig)
    model_router: ModelRouterConfig = field(default_factory=ModelRouterConfig)
    draft_workflow: DraftWorkflowConfig = field(default_factory=DraftWorkflowConfig)
    token_compressor: TokenCompressorConfig = field(default_factory=TokenCompressorConfig)
    feature_cache: FeatureCacheConfig = field(default_factory=FeatureCacheConfig)
    cost_tracking: CostTrackingConfig = field(default_factory=CostTrackingConfig)
    prompt_templates: PromptTemplateConfig = field(default_factory=PromptTemplateConfig)

    @classmethod
    def load_from_dict(
        cls,
        data: Dict[str, Any],
        previous: Optional["GenerationOptimizationConfig"] = None,
    ) -> "GenerationOptimizationConfig":
        """从字典创建配置实例（通常从 YAML 解析结果）.

        环境变量优先于字典中的值。无效值保留 previous 中的旧值并记录错误日志。
        缺失键使用文档化的默认值并记录 warning。

        Args:
            data: 配置字典（generation_optimization 节的内容）
            previous: 之前的有效配置实例，用于无效值回退

        Returns:
            新的 GenerationOptimizationConfig 实例
        """
        if data is None:
            data = {}

        # 获取环境变量覆盖
        env_overrides = _get_env_overrides()

        # 处理顶层 enabled 字段
        enabled_value = data.get("enabled", True)
        if "enabled" in env_overrides.get("_top", {}):
            enabled_value = env_overrides["_top"]["enabled"]

        # 类型校验 enabled
        if not isinstance(enabled_value, bool):
            if isinstance(enabled_value, str):
                enabled_value = enabled_value.lower() in ("true", "yes", "1")
            else:
                logger.error(
                    "配置类型错误: generation_optimization.enabled = %r，保留旧值",
                    enabled_value,
                )
                enabled_value = previous.enabled if previous else True

        # 构建子配置
        sub_configs: Dict[str, Any] = {}
        for section_name, config_cls in _SUB_CONFIG_CLASSES.items():
            section_data = dict(data.get(section_name, {}) or {})

            # 应用环境变量覆盖
            if section_name in env_overrides:
                section_data.update(env_overrides[section_name])

            # 记录缺失配置节的 warning
            if section_name not in data and section_name not in env_overrides:
                logger.warning(
                    "配置缺失: generation_optimization.%s 未提供，使用默认值",
                    section_name,
                )

            prev_sub = getattr(previous, section_name, None) if previous else None
            sub_configs[section_name] = _load_sub_config(
                config_cls, section_data, section_name, prev_sub
            )

        return cls(
            enabled=enabled_value,
            ai_director=sub_configs["ai_director"],
            model_router=sub_configs["model_router"],
            draft_workflow=sub_configs["draft_workflow"],
            token_compressor=sub_configs["token_compressor"],
            feature_cache=sub_configs["feature_cache"],
            cost_tracking=sub_configs["cost_tracking"],
            prompt_templates=sub_configs["prompt_templates"],
        )

    @classmethod
    def load_from_config_manager(
        cls,
        config_manager: Any,
        previous: Optional["GenerationOptimizationConfig"] = None,
    ) -> "GenerationOptimizationConfig":
        """从 ConfigManager 实例加载配置.

        读取 ConfigManager 中 "generation_optimization" 路径的配置字典，
        然后委托给 load_from_dict 进行解析和校验。

        Args:
            config_manager: ConfigManager 实例（需有 get() 方法）
            previous: 之前的有效配置实例，用于无效值回退

        Returns:
            新的 GenerationOptimizationConfig 实例
        """
        data = config_manager.get("generation_optimization", {})
        if data is None:
            data = {}
        if not isinstance(data, dict):
            logger.error(
                "ConfigManager 返回的 generation_optimization 配置不是字典: %r，使用默认配置",
                type(data).__name__,
            )
            data = {}
        return cls.load_from_dict(data, previous=previous)

    def validate(self) -> List[str]:
        """校验当前配置实例的所有字段值.

        对所有子配置进行类型检查和范围检查。

        Returns:
            错误消息列表。空列表表示配置完全有效。
        """
        errors: List[str] = []

        for section_name in _SUB_CONFIG_CLASSES:
            sub_config = getattr(self, section_name)
            rules = _VALIDATION_RULES.get(section_name, {})

            for field_name, (min_val, max_val) in rules.items():
                value = getattr(sub_config, field_name, None)
                if value is None:
                    continue

                try:
                    numeric_val = float(value)
                    if numeric_val < min_val or numeric_val > max_val:
                        msg = (
                            f"generation_optimization.{section_name}.{field_name} = {value} "
                            f"超出范围 [{min_val}, {max_val}]"
                        )
                        errors.append(msg)
                except (TypeError, ValueError):
                    msg = (
                        f"generation_optimization.{section_name}.{field_name} = {value!r} "
                        f"类型错误，期望数值类型"
                    )
                    errors.append(msg)

        return errors



# ==================================================================
# 便捷函数和 Watcher 类（供外部模块使用）
# ==================================================================


def parse_generation_optimization_config(
    data: Dict[str, Any],
    previous: Optional[GenerationOptimizationConfig] = None,
) -> GenerationOptimizationConfig:
    """解析 generation_optimization 配置字典.

    便捷函数，等价于 GenerationOptimizationConfig.load_from_dict()。

    Args:
        data: 配置字典（generation_optimization 节的内容）
        previous: 之前的有效配置实例

    Returns:
        新的 GenerationOptimizationConfig 实例
    """
    return GenerationOptimizationConfig.load_from_dict(data, previous=previous)


def validate_generation_optimization_config(
    config: GenerationOptimizationConfig,
) -> List[str]:
    """校验配置实例的所有字段.

    便捷函数，等价于 config.validate()。

    Args:
        config: 要校验的配置实例

    Returns:
        错误消息列表。空列表表示配置完全有效。
    """
    return config.validate()


class GenerationOptimizationConfigWatcher:
    """配置热重载监视器.

    监听 ConfigManager 的 Watchdog 变更事件，当 YAML 配置文件中的
    generation_optimization 节发生变化时，自动重新加载和校验配置。

    热重载行为:
    - ConfigManager 的 Watchdog 在文件修改后 5 秒内触发回调
    - 本 Watcher 注册 ConfigManager.on_reload() 回调，自动响应变更
    - 验证通过: 原子交换到新配置
    - 验证失败（类型错误/超出范围）: 保留旧配置，记录 ERROR 日志

    需求: 6.4, 6.5
    """

    def __init__(
        self,
        config_manager: Any,
        initial_config: Optional[GenerationOptimizationConfig] = None,
    ) -> None:
        """
        Args:
            config_manager: ConfigManager 实例
            initial_config: 初始配置，如果未提供则从 config_manager 加载
        """
        import threading

        self._config_manager = config_manager
        self._lock = threading.RLock()
        self._callbacks: List[Any] = []

        # 初始化当前配置
        self._current_config: GenerationOptimizationConfig = (
            initial_config
            if initial_config is not None
            else GenerationOptimizationConfig.load_from_config_manager(config_manager)
        )

        # 注册 ConfigManager 的热重载回调，实现文件变更自动触发
        # ConfigManager.start_watching() 使用 Watchdog 监听文件变更，
        # 变更后调用 load() 重新解析 YAML，然后通知所有 on_reload 回调。
        if hasattr(config_manager, "on_reload"):
            config_manager.on_reload(self._on_config_reload)
            logger.info(
                "GenerationOptimizationConfigWatcher: 已注册 ConfigManager 热重载回调"
            )

    @property
    def config(self) -> GenerationOptimizationConfig:
        """获取当前生效的配置（线程安全）."""
        with self._lock:
            return self._current_config

    def reload(self) -> GenerationOptimizationConfig:
        """手动从 ConfigManager 重新加载配置.

        无效值保留旧配置，记录错误日志。

        Returns:
            更新后的配置实例
        """
        with self._lock:
            previous = self._current_config

        new_config = GenerationOptimizationConfig.load_from_config_manager(
            self._config_manager, previous=previous
        )
        errors = new_config.validate()
        if errors:
            for err in errors:
                logger.error("配置校验错误（重载后）: %s", err)
            # load_from_dict 已对无效值做了回退处理

        with self._lock:
            self._current_config = new_config

        # 通知回调
        self._notify_callbacks(new_config)

        return new_config

    def on_change(self, callback: Any) -> None:
        """注册配置变更回调.

        Args:
            callback: 接收新 GenerationOptimizationConfig 的回调函数
        """
        self._callbacks.append(callback)

    def _on_config_reload(self, new_full_config: Dict[str, Any]) -> None:
        """ConfigManager 热重载回调 — 文件变更时自动调用.

        由 ConfigManager 的 Watchdog 在检测到 YAML 文件修改后触发
        （5 秒内）。执行以下流程:
        1. 提取 generation_optimization 配置节
        2. 解析为 GenerationOptimizationConfig（无效字段保留旧值）
        3. 验证解析结果
        4. 原子交换配置（线程安全）
        5. 通知变更回调

        如果整个 generation_optimization 节不是字典类型，直接拒绝并保留旧配置。

        Args:
            new_full_config: 完整的新配置字典（所有配置节）。
        """
        raw_section = new_full_config.get("generation_optimization")

        # 如果配置中没有 generation_optimization 节，保留当前配置
        if raw_section is None:
            logger.info(
                "配置热重载: generation_optimization 节不存在，保留当前配置"
            )
            return

        if not isinstance(raw_section, dict):
            logger.error(
                "配置热重载失败: generation_optimization 节类型无效 (%s)，保留旧配置",
                type(raw_section).__name__,
            )
            return

        # 使用当前配置作为 previous，确保无效值回退到旧值
        with self._lock:
            previous = self._current_config

        # 解析新配置（无效字段会自动回退到 previous 的值）
        new_config = GenerationOptimizationConfig.load_from_dict(
            raw_section, previous=previous
        )

        # 验证最终配置
        errors = new_config.validate()
        if errors:
            for err in errors:
                logger.error("配置热重载验证失败: %s", err)
            logger.error(
                "generation_optimization 热重载: %d 个字段超出范围，这些字段已保留旧值",
                len(errors),
            )

        # 原子交换配置
        with self._lock:
            self._current_config = new_config

        logger.info(
            "generation_optimization 配置热重载完成: enabled=%s",
            new_config.enabled,
        )

        # 通知变更回调
        self._notify_callbacks(new_config)

    def _notify_callbacks(self, new_config: GenerationOptimizationConfig) -> None:
        """通知所有注册的变更回调."""
        for cb in self._callbacks:
            try:
                cb(new_config)
            except Exception as exc:
                logger.error("配置变更回调执行失败: %s", exc)
