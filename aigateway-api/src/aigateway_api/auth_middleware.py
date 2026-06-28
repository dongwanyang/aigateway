"""
API Key 鉴权中间件
==================

FastAPI 依赖注入风格的鉴权函数，用于校验请求中的 API Key。

支持两种鉴权方式：
- Authorization: Bearer {API_KEY}
- x-api-key: {API_KEY}

根据 API_CONTRACT.md 认证说明:
- /v1/* 业务接口需要普通 API Key
- /admin/* 管理接口需要管理员权限的 API Key
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Optional

from fastapi import Header, HTTPException, Request, status
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


def _extract_api_key(
    authorization: Optional[str] = None,
    x_api_key: Optional[str] = None,
) -> Optional[str]:
    """从请求头中提取 API Key 值。

    Args:
        authorization: Authorization 请求头。
        x_api_key: x-api-key 请求头。

    Returns:
        API Key 字符串，均未提供则返回 None。
    """
    if x_api_key:
        return x_api_key
    if authorization:
        parts = authorization.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1]
    return None


def _hash_key(key_value: str) -> str:
    """计算 API Key 的 SHA-256 哈希（取前 16 位 hex）。

    与 KeyStore._hash_key 保持一致。

    Args:
        key_value: 完整 API Key 字符串。

    Returns:
        16 位 hex 哈希字符串。
    """
    return hashlib.sha256(key_value.encode("utf-8")).hexdigest()[:16]


async def authenticate(
    request: Request,
    api_key: Optional[str] = Header(None, alias="x-api-key"),
    authorization: Optional[str] = Header(None),
) -> Optional[Dict[str, Any]]:
    """鉴权中间件，校验 API Key 有效性。

    验证流程：
    1. 从请求头提取 API Key
    2. 从 Redis 查找 key_hash 对应的 Key 记录
    3. 检查状态（active/revoked/suspended）
    4. 将 Key 元数据挂载到 request.state.api_key_data

    Args:
        request: FastAPI 请求对象。
        api_key: x-api-key 请求头的值。
        authorization: Authorization 请求头的值。

    Returns:
        Key 元数据字典（包含 key_id, user_id, status 等），
        鉴权失败时抛出 HTTPException。

    Raises:
        HTTPException 401: Key 缺失或无效。
        HTTPException 403: Key 已被撤销。
    """
    key_value = _extract_api_key(authorization, api_key)

    if not key_value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid or missing API key",
                }
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 从 app.state.key_store 获取 KeyStore 实例
    key_store = request.app.state.key_store

    if key_store is None:
        logger.error("KeyStore 未初始化，无法鉴权")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": {
                    "code": "internal_error",
                    "message": "Authentication service unavailable",
                }
            },
        )

    try:
        key_data = await key_store.validate(key_value)
    except Exception as e:
        # 捕获 KeyStore.AuthError 等异常
        error_msg = str(e)
        if "revoked" in error_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": {
                        "code": "forbidden",
                        "message": f"API key '{key_value[:8]}...' has been revoked",
                    }
                },
            )
        if "suspended" in error_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": {
                        "code": "forbidden",
                        "message": f"API key '{key_value[:8]}...' is suspended",
                    }
                },
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid or missing API key",
                }
            },
        )

    if key_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid or missing API key",
                }
            },
        )

    # 将 Key 元数据挂载到 request.state，供后续路由使用
    request.state.api_key_data = key_data
    request.state.api_key_value = key_value

    return key_data


async def authenticate_admin(request: Request) -> Optional[Dict[str, Any]]:
    """管理员鉴权中间件。

    校验 API Key 是否具有管理权限。
    管理接口要求 Key 附带 admin=true 标记（通过 KeyStore 扩展字段）。

    Args:
        request: FastAPI 请求对象。

    Returns:
        Key 元数据字典，管理权限校验失败时抛出 HTTPException。

    Raises:
        HTTPException 401: Key 缺失或无效。
        HTTPException 403: 当前 Key 无管理权限。
    """
    # 先通过普通鉴权
    key_data = await authenticate(request)

    if key_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid or missing API key",
                }
            },
        )

    # 检查管理员权限标记（KeyStore.create 时可通过 config 设置）
    is_admin = key_data.get("is_admin", False)
    if not is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": {
                    "code": "forbidden",
                    "message": "Insufficient permissions",
                }
            },
        )

    return key_data


async def require_api_key(request: Request) -> None:
    """直接挂载鉴权结果的中间件函数（可用于 FastAPI middleware）。

    简化调用方式：中间件中只需 await require_api_key(request)，
    即可在 request.state 上获得 api_key_data。

    Args:
        request: FastAPI 请求对象。

    Raises:
        HTTPException: 鉴权失败时抛出。
    """
    api_key = request.headers.get("x-api-key")
    authorization = request.headers.get("authorization")

    key_value = _extract_api_key(authorization, api_key)

    if not key_value:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid or missing API key",
                }
            },
        )

    key_store = request.app.state.key_store

    if key_store is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": {
                    "code": "internal_error",
                    "message": "Authentication service unavailable",
                }
            },
        )

    try:
        key_data = await key_store.validate(key_value)
    except Exception as e:
        error_msg = str(e)
        if "revoked" in error_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": {
                        "code": "forbidden",
                        "message": f"API key '{key_value[:8]}...' has been revoked",
                    }
                },
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid or missing API key",
                }
            },
        )

    if key_data is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid or missing API key",
                }
            },
        )

    request.state.api_key_data = key_data
    request.state.api_key_value = key_value
