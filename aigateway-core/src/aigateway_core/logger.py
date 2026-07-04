"""
Logger — 结构化 JSON 日志
=========================

使用 structlog 实现 JSON 格式输出，自动注入 trace_id / request_id / user_id / level / event。

根据 TECH_SPEC.md 日志规范:
- JSON（structlog）
- 必须包含 trace_id, request_id, timestamp, level, event 字段
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any, Callable, Dict, Optional

import structlog

# ------------------------------------------------------------------
# 全局配置
# ------------------------------------------------------------------

_logger_config: Dict[str, Any] = {
    "log_level": os.environ.get("AI_GATEWAY_LOG_LEVEL", "info").upper(),
    "log_format": os.environ.get("AI_GATEWAY_LOG_FORMAT", "json"),
    "service_name": "ai-gateway",
    "version": "1.0.0",
}


def get_logger_config() -> Dict[str, Any]:
    """获取当前日志配置。

    Returns:
        配置字典副本。
    """
    return dict(_logger_config)


def update_logger_config(updates: Dict[str, Any]) -> None:
    """更新日志配置。

    Args:
        updates: 需要更新的配置项。
    """
    _logger_config.update(updates)


# ------------------------------------------------------------------
# 上下文处理器 — 自动注入上下文字段
# ------------------------------------------------------------------


class ContextInjectProcessor:
    """structlog 处理器：自动注入 trace_id / request_id / user_id。

    从 thread-local 或 structlog 的上下文字典中提取这些字段，
    确保每条日志都包含它们。
    """

    _context: Dict[str, Any] = {}

    @classmethod
    def set(cls, trace_id: str, request_id: str, user_id: Optional[str] = None) -> None:
        """设置当前上下文的追踪字段。

        Args:
            trace_id: 追踪 ID。
            request_id: 请求 ID。
            user_id: 用户 ID（可选）。
        """
        cls._context["trace_id"] = trace_id
        cls._context["request_id"] = request_id
        if user_id is not None:
            cls._context["user_id"] = user_id

    @classmethod
    def clear(cls) -> None:
        """清除当前上下文。"""
        cls._context.clear()

    @classmethod
    def process(
        cls,
        logger_obj: Any,
        method_name: str,
        event_dict: Dict[str, Any],
    ) -> Dict[str, Any]:
        """structlog processor 实现：注入上下文字段。

        Args:
            logger_obj: structlog Logger 实例（未使用）。
            method_name: 日志级别名，如 "info"。
            event_dict: 当前事件字典。

        Returns:
            注入后的事件字典。
        """
        # 合并当前上下文
        event_dict.update(cls._context)

        # 确保必要字段存在
        if "trace_id" not in event_dict:
            event_dict["trace_id"] = ""
        if "request_id" not in event_dict:
            event_dict["request_id"] = ""
        if "user_id" not in event_dict:
            event_dict["user_id"] = ""
        if "timestamp" not in event_dict:
            event_dict["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if "level" not in event_dict:
            event_dict["level"] = method_name
        if "event" not in event_dict:
            event_dict["event"] = str(event_dict.get("", ""))

        # 标准化 level 为小写
        event_dict["level"] = event_dict["level"].lower()

        # 重命名空字符串字段
        if "" in event_dict:
            del event_dict[""]

        return event_dict


# ------------------------------------------------------------------
# 初始化
# ------------------------------------------------------------------


def setup_logging(
    log_level: Optional[str] = None,
    log_format: Optional[str] = None,
) -> None:
    """别名：调用 setup_structlog。"""
    setup_structlog(log_level=log_level, log_format=log_format)


def setup_structlog(
    log_level: Optional[str] = None,
    log_format: Optional[str] = None,
) -> None:
    """初始化 structlog 配置。

    Args:
        log_level: 日志级别，默认从 AI_GATEWAY_LOG_LEVEL 读取。
        log_format: 日志格式 "json" | "text"，默认 "json"。
    """
    level_str = (log_level or _logger_config["log_level"]).upper()
    fmt = log_format or _logger_config["log_format"]

    # 映射日志级别字符串到 structlog 级别
    level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    stdlib_level = level_map.get(level_str, logging.INFO)

    processors: list[Callable[..., Any]] = [
        # 添加时间戳
        structlog.stdlib.filter_by_level,
        # 添加日志级别和时间
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.ExceptionPrettyPrinter(),
        # 自定义上下文注入
        ContextInjectProcessor.process,
        # 格式化时间戳
        structlog.processors.TimeStamper(fmt="iso"),
        # 格式化时间戳为 UTC ISO 8601
        structlog.dev.set_exc_info,
    ]

    if fmt == "json":
        # JSON 输出
        processors.append(structlog.processors.JSONRenderer())
    else:
        # 可读文本输出（开发环境）
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # 把根 stdlib logger 的级别也拉到相同高度，否则 aigateway_core.pipeline 等
    # 使用 logging.getLogger(__name__) 的模块拿不到 DEBUG 日志。
    # 如果根 logger 还没有 handler，就给它挂一个 StreamHandler（uvicorn 已挂过则复用）。
    root_logger = logging.getLogger()
    root_logger.setLevel(stdlib_level)
    if not root_logger.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(stdlib_level)
        root_logger.addHandler(handler)
    else:
        for h in root_logger.handlers:
            h.setLevel(stdlib_level)


def get_logger() -> Any:
    """获取 structlog BoundLogger 实例。

    Returns:
        structlog BoundLogger 实例。
    """
    return structlog.get_logger()


# ------------------------------------------------------------------
# 便捷函数
# ------------------------------------------------------------------


def info(event: str, **kwargs: Any) -> None:
    """记录 info 级别日志。

    Args:
        event: 事件描述。
        **kwargs: 额外字段。
    """
    setup_structlog_if_needed()
    logger = get_logger()
    logger.info(event, **kwargs)


def debug(event: str, **kwargs: Any) -> None:
    """记录 debug 级别日志。

    Args:
        event: 事件描述。
        **kwargs: 额外字段。
    """
    setup_structlog_if_needed()
    logger = get_logger()
    logger.debug(event, **kwargs)


def warning(event: str, **kwargs: Any) -> None:
    """记录 warning 级别日志。

    Args:
        event: 事件描述。
        **kwargs: 额外字段。
    """
    setup_structlog_if_needed()
    logger = get_logger()
    logger.warning(event, **kwargs)


def error(event: str, **kwargs: Any) -> None:
    """记录 error 级别日志。

    Args:
        event: 事件描述。
        **kwargs: 额外字段。
    """
    setup_structlog_if_needed()
    logger = get_logger()
    logger.error(event, **kwargs)


def critical(event: str, **kwargs: Any) -> None:
    """记录 critical 级别日志。

    Args:
        event: 事件描述。
        **kwargs: 额外字段。
    """
    setup_structlog_if_needed()
    logger = get_logger()
    logger.critical(event, **kwargs)


def log_with_context(
    level: str,
    event: str,
    trace_id: Optional[str] = None,
    request_id: Optional[str] = None,
    user_id: Optional[str] = None,
    **kwargs: Any,
) -> None:
    """带上下文注入的日志记录。

    自动将 trace_id / request_id / user_id 注入到当前 structlog 上下文中，
    后续所有日志都会自动携带这些字段。

    Args:
        level: 日志级别 "info" | "debug" | "warning" | "error"。
        event: 事件描述。
        trace_id: 追踪 ID。
        request_id: 请求 ID。
        user_id: 用户 ID。
        **kwargs: 额外字段。
    """
    setup_structlog_if_needed()

    # 注入上下文
    ContextInjectProcessor.set(
        trace_id=trace_id or "",
        request_id=request_id or "",
        user_id=user_id,
    )

    logger = get_logger()
    log_fn = getattr(logger, level, logger.info)
    log_fn(event, **kwargs)


# ------------------------------------------------------------------
# 一次性初始化守卫
# ------------------------------------------------------------------

_structlog_setup_done = False


def setup_structlog_if_needed() -> None:
    """一次性初始化 structlog（全局只有一个入口）。"""
    global _structlog_setup_done
    if _structlog_setup_done:
        return
    _structlog_setup_done = True
    setup_structlog()
