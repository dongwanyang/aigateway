"""
Template Routes — 提示词模板 CRUD API 端点
==========================================

实现以下端点:
- POST   /api/v1/generation/templates       — 创建模板
- GET    /api/v1/generation/templates       — 列出模板（分页）
- GET    /api/v1/generation/templates/{name} — 获取指定模板
- PUT    /api/v1/generation/templates/{name} — 更新模板
- DELETE /api/v1/generation/templates/{name} — 删除模板

权限校验:
- 所有端点需要有效 API Key（通过 auth_middleware 验证）
- 模板以 API Key 隔离，跨 Key 访问被拒绝

需求: 8.5, 8.6, 8.7, 8.8
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .auth_middleware import authenticate

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/generation", tags=["generation-optimization"])


# ==================================================================
# 请求/响应模型
# ==================================================================


class CreateTemplateRequest(BaseModel):
    """POST /templates 请求体."""

    name: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[a-zA-Z0-9_-]+$",
        description="模板名称 (1-64 字符，字母数字/连字符/下划线)",
    )
    content: str = Field(..., min_length=1, max_length=10000, description="模板内容 (最大 10000 字符)")
    description: str = Field(default="", max_length=500, description="模板描述 (最大 500 字符)")


class UpdateTemplateRequest(BaseModel):
    """PUT /templates/{name} 请求体."""

    content: str = Field(..., min_length=1, max_length=10000, description="新的模板内容")
    description: str = Field(default="", max_length=500, description="新的模板描述")


class RenderTemplateRequest(BaseModel):
    """POST /templates/{name}/render 请求体."""

    variables: Dict[str, str] = Field(default_factory=dict, description="模板占位符变量值映射")


# ==================================================================
# 辅助函数
# ==================================================================


def _get_template_manager(request: Request):
    """从 app.state 获取 PromptTemplateManager 实例.

    Returns:
        PromptTemplateManager 实例

    Raises:
        HTTPException 503: 模板管理服务不可用
    """
    manager = getattr(request.app.state, "prompt_template_manager", None)
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": "service_unavailable",
                    "message": "Prompt template service is not available.",
                }
            },
        )
    return manager


def _get_api_key_id(request: Request) -> str:
    """从 request.state 获取当前请求的 api_key_id.

    auth_middleware 会将 key_data 挂载到 request.state.api_key_data，
    其中包含 key_id 字段。

    Returns:
        当前请求的 API Key ID

    Raises:
        HTTPException 401: 无法确定 API Key ID
    """
    key_data = getattr(request.state, "api_key_data", None)
    if key_data is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "code": "unauthorized",
                    "message": "Unable to identify API key.",
                }
            },
        )
    # key_data 中 key_id 字段标识该 API Key
    key_id = key_data.get("key_id", "")
    if not key_id:
        # 回退: 尝试从 request.state.api_key_value 获取（原始 key 值）
        key_id = getattr(request.state, "api_key_value", "") or ""
    return key_id


def _template_to_dict(template) -> Dict[str, Any]:
    """将 PromptTemplate 转换为 API 响应字典."""
    return {
        "name": template.name,
        "content": template.content,
        "description": template.description,
        "variables": template.variables,
        "created_at": template.created_at,
        "updated_at": template.updated_at,
    }


# ==================================================================
# API 端点
# ==================================================================


@router.post("/templates", status_code=201)
async def create_template(
    request: Request,
    body: CreateTemplateRequest,
    _auth: Dict[str, Any] = Depends(authenticate),
):
    """创建新的提示词模板.

    在当前 API Key 的作用域内创建模板。名称在同一 API Key 内必须唯一。

    Returns:
        201: 创建成功，返回模板数据
        409: 模板名称已存在
        400: 验证失败（名称格式无效等）
    """
    manager = _get_template_manager(request)
    api_key_id = _get_api_key_id(request)

    try:
        template = await manager.create(
            owner_id=api_key_id,
            name=body.name,
            content=body.content,
            description=body.description,
        )
    except Exception as exc:
        error_msg = str(exc)
        # 名称已存在 → 409
        if "已存在" in error_msg or "already exists" in error_msg.lower():
            raise HTTPException(
                status_code=409,
                detail={
                    "error": {
                        "code": "conflict",
                        "message": f"Template name '{body.name}' already exists for this API key.",
                    }
                },
            )
        # 其他验证错误 → 400
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "validation_error",
                    "message": error_msg,
                }
            },
        )

    return {
        "data": _template_to_dict(template),
        "message": "success",
    }


@router.get("/templates")
async def list_templates(
    request: Request,
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    _auth: Dict[str, Any] = Depends(authenticate),
):
    """列出当前 API Key 拥有的所有模板（分页）.

    Returns:
        200: 模板列表和分页元数据
    """
    manager = _get_template_manager(request)
    api_key_id = _get_api_key_id(request)

    result = await manager.list(
        owner_id=api_key_id,
        page=page,
        page_size=page_size,
    )

    items = [_template_to_dict(t) for t in result["items"]]

    return {
        "data": {
            "items": items,
            "pagination": {
                "page": result["page"],
                "page_size": result["page_size"],
                "total": result["total"],
                "total_pages": result["total_pages"],
            },
        },
        "message": "success",
    }


@router.get("/templates/{name}")
async def get_template(
    request: Request,
    name: str,
    _auth: Dict[str, Any] = Depends(authenticate),
):
    """获取指定名称的模板.

    仅返回当前 API Key 拥有的模板。

    Returns:
        200: 模板数据
        404: 模板不存在
    """
    manager = _get_template_manager(request)
    api_key_id = _get_api_key_id(request)

    template = await manager.get(owner_id=api_key_id, name=name)
    if template is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "not_found",
                    "message": f"Template '{name}' not found.",
                }
            },
        )

    return {
        "data": _template_to_dict(template),
        "message": "success",
    }


@router.put("/templates/{name}")
async def update_template(
    request: Request,
    name: str,
    body: UpdateTemplateRequest,
    _auth: Dict[str, Any] = Depends(authenticate),
):
    """更新指定名称的模板.

    仅允许更新当前 API Key 拥有的模板。
    尝试更新其他 API Key 的模板将返回 403。

    Returns:
        200: 更新后的模板数据
        403: 无权修改其他 API Key 的模板
        404: 模板不存在
    """
    manager = _get_template_manager(request)
    api_key_id = _get_api_key_id(request)

    # 先检查模板是否存在（在所有 API Key 中）
    template = await manager.get(owner_id=api_key_id, name=name)
    if template is None:
        # 模板在当前 Key 下不存在，但可能属于其他 Key
        # 这里统一返回 404，因为用户不应该知道其他 Key 的模板存在
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "not_found",
                    "message": f"Template '{name}' not found.",
                }
            },
        )

    # 验证所有权: 模板的 api_key_id 必须与请求者一致
    if template.api_key_id != api_key_id:
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "code": "forbidden",
                    "message": "You do not have permission to modify this template.",
                }
            },
        )

    try:
        updated = await manager.update(
            owner_id=api_key_id,
            name=name,
            content=body.content,
            description=body.description,
        )
    except Exception as exc:
        error_msg = str(exc)
        if "不存在" in error_msg or "not found" in error_msg.lower():
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "not_found",
                        "message": f"Template '{name}' not found.",
                    }
                },
            )
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "validation_error",
                    "message": error_msg,
                }
            },
        )

    return {
        "data": _template_to_dict(updated),
        "message": "success",
    }


@router.delete("/templates/{name}", status_code=204)
async def delete_template(
    request: Request,
    name: str,
    _auth: Dict[str, Any] = Depends(authenticate),
):
    """删除指定名称的模板.

    仅允许删除当前 API Key 拥有的模板。
    尝试删除其他 API Key 的模板将返回 403。

    Returns:
        204: 删除成功（无响应体）
        403: 无权删除其他 API Key 的模板
        404: 模板不存在
    """
    manager = _get_template_manager(request)
    api_key_id = _get_api_key_id(request)

    # 先检查模板是否存在
    template = await manager.get(owner_id=api_key_id, name=name)
    if template is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "not_found",
                    "message": f"Template '{name}' not found.",
                }
            },
        )

    # 验证所有权
    if template.api_key_id != api_key_id:
        raise HTTPException(
            status_code=403,
            detail={
                "error": {
                    "code": "forbidden",
                    "message": "You do not have permission to delete this template.",
                }
            },
        )

    success = await manager.delete(owner_id=api_key_id, name=name)
    if not success:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "not_found",
                    "message": f"Template '{name}' not found.",
                }
            },
        )

    # 204 No Content — FastAPI 会自动返回空响应体
    return None


@router.post("/templates/{name}/render")
async def render_template(
    request: Request,
    name: str,
    body: RenderTemplateRequest,
    _auth: Dict[str, Any] = Depends(authenticate),
):
    """渲染模板，替换占位符变量.

    查找当前 API Key 拥有的模板，使用提供的变量值替换
    所有 {{variable_name}} 占位符。

    Returns:
        200: 渲染后的文本
        404: 模板不存在
        400: 缺失必需的占位符变量
    """
    manager = _get_template_manager(request)
    api_key_id = _get_api_key_id(request)

    # 获取模板
    template = await manager.get(owner_id=api_key_id, name=name)
    if template is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "not_found",
                    "message": f"Template '{name}' not found.",
                }
            },
        )

    # 渲染模板
    try:
        rendered = manager.render(template, body.variables)
    except Exception as exc:
        error_msg = str(exc)
        # 缺失变量 → 400 验证错误
        if "缺少模板变量" in error_msg or "missing" in error_msg.lower():
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "code": "validation_error",
                        "message": error_msg,
                    }
                },
            )
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "validation_error",
                    "message": error_msg,
                }
            },
        )

    return {
        "data": {
            "rendered": rendered,
            "template_name": name,
            "variables_used": list(body.variables.keys()),
        },
        "message": "success",
    }
