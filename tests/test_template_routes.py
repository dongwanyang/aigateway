"""
Tests for Template Routes — 提示词模板 API 端点测试
=====================================================

测试 POST/GET/PUT/DELETE /api/v1/generation/templates 端点的:
- CRUD 操作
- 同 API Key 内名称唯一性
- 跨 API Key 访问控制
- 引用不存在模板时 404
- 缺失占位符变量时 400 验证错误
- 渲染模板端点

需求: 8.5, 8.6, 8.7, 8.8
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.pipelines.generation._common.config import PromptTemplateConfig
from aigateway_core.pipelines.generation._common.exceptions import TemplateValidationError
from aigateway_core.pipelines.generation._common.models import PromptTemplate
from aigateway_core.pipelines.generation.token.prompt_template_manager import (
    PromptTemplateManager,
)


# ==================================================================
# Fixtures
# ==================================================================


@pytest.fixture
def template_manager():
    """Create an in-memory PromptTemplateManager for testing."""
    config = PromptTemplateConfig()
    manager = PromptTemplateManager(redis_client=None, config=config)
    return manager


@pytest.fixture
def app(template_manager):
    """Create a test FastAPI app with template routes and a mock key_store."""
    from aigateway_api.template_routes import router

    test_app = FastAPI()
    test_app.include_router(router)

    # Register custom exception handler to match main app behavior
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    @test_app.exception_handler(HTTPException)
    async def http_exception_handler(request, exc):
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            body = detail
        else:
            body = {"error": {"code": "internal_error", "message": str(detail) if detail else "Internal error"}}
        return JSONResponse(status_code=exc.status_code, content=body)

    # Setup app.state
    test_app.state.prompt_template_manager = template_manager

    # Mock key_store that accepts any key
    mock_key_store = MagicMock()
    mock_key_store.validate = AsyncMock(
        return_value={"key_id": "test-key-1", "user_id": "test-user", "status": "active"}
    )
    test_app.state.key_store = mock_key_store

    return test_app


@pytest.fixture
def app_key_b(template_manager):
    """Create a test FastAPI app with a different API key (key B)."""
    from aigateway_api.template_routes import router

    test_app = FastAPI()
    test_app.include_router(router)

    # Register custom exception handler to match main app behavior
    from fastapi import HTTPException
    from fastapi.responses import JSONResponse

    @test_app.exception_handler(HTTPException)
    async def http_exception_handler(request, exc):
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            body = detail
        else:
            body = {"error": {"code": "internal_error", "message": str(detail) if detail else "Internal error"}}
        return JSONResponse(status_code=exc.status_code, content=body)

    test_app.state.prompt_template_manager = template_manager

    mock_key_store = MagicMock()
    mock_key_store.validate = AsyncMock(
        return_value={"key_id": "test-key-2", "user_id": "test-user-2", "status": "active"}
    )
    test_app.state.key_store = mock_key_store

    return test_app


@pytest_asyncio.fixture
async def client(app):
    """Create async test client for key A."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"x-api-key": "sk-test-key-1"},
    ) as c:
        yield c


@pytest_asyncio.fixture
async def client_b(app_key_b):
    """Create async test client for key B."""
    async with AsyncClient(
        transport=ASGITransport(app=app_key_b),
        base_url="http://test",
        headers={"x-api-key": "sk-test-key-2"},
    ) as c:
        yield c


# ==================================================================
# Tests: Create Template (POST)
# ==================================================================


class TestCreateTemplate:
    """POST /api/v1/generation/templates"""

    @pytest.mark.asyncio
    async def test_create_template_success(self, client):
        """Successfully create a new template returns 201."""
        resp = await client.post(
            "/api/v1/generation/templates",
            json={
                "name": "greeting-template",
                "content": "Hello {{name}}, welcome to {{place}}!",
                "description": "A simple greeting template",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["message"] == "success"
        assert data["data"]["name"] == "greeting-template"
        assert data["data"]["content"] == "Hello {{name}}, welcome to {{place}}!"
        assert data["data"]["description"] == "A simple greeting template"
        assert "name" in data["data"]["variables"]
        assert "place" in data["data"]["variables"]

    @pytest.mark.asyncio
    async def test_create_template_duplicate_name_409(self, client):
        """Creating a template with duplicate name returns 409."""
        # Create first
        resp1 = await client.post(
            "/api/v1/generation/templates",
            json={"name": "my-template", "content": "Hello {{name}}!"},
        )
        assert resp1.status_code == 201

        # Attempt duplicate
        resp2 = await client.post(
            "/api/v1/generation/templates",
            json={"name": "my-template", "content": "Different content"},
        )
        assert resp2.status_code == 409
        assert resp2.json()["error"]["code"] == "conflict"

    @pytest.mark.asyncio
    async def test_create_template_invalid_name_422(self, client):
        """Creating a template with invalid name format returns 422 (pydantic validation)."""
        resp = await client.post(
            "/api/v1/generation/templates",
            json={"name": "invalid name with spaces!", "content": "Hello"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_template_missing_content_422(self, client):
        """Creating a template without content returns 422 (validation)."""
        resp = await client.post(
            "/api/v1/generation/templates",
            json={"name": "test-template"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_create_template_empty_name_422(self, client):
        """Creating a template with empty name returns 422."""
        resp = await client.post(
            "/api/v1/generation/templates",
            json={"name": "", "content": "Hello"},
        )
        assert resp.status_code == 422


# ==================================================================
# Tests: Get Template (GET /{name})
# ==================================================================


class TestGetTemplate:
    """GET /api/v1/generation/templates/{name}"""

    @pytest.mark.asyncio
    async def test_get_template_success(self, client):
        """Getting an existing template returns 200."""
        # Create first
        await client.post(
            "/api/v1/generation/templates",
            json={"name": "test-get", "content": "Content {{var}}"},
        )

        resp = await client.get("/api/v1/generation/templates/test-get")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["name"] == "test-get"
        assert data["data"]["content"] == "Content {{var}}"

    @pytest.mark.asyncio
    async def test_get_template_not_found_404(self, client):
        """Getting a non-existent template returns 404."""
        resp = await client.get("/api/v1/generation/templates/nonexistent")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"


# ==================================================================
# Tests: List Templates (GET)
# ==================================================================


class TestListTemplates:
    """GET /api/v1/generation/templates"""

    @pytest.mark.asyncio
    async def test_list_templates_empty(self, client):
        """Listing with no templates returns empty list."""
        resp = await client.get("/api/v1/generation/templates")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["items"] == []
        assert data["data"]["pagination"]["total"] == 0

    @pytest.mark.asyncio
    async def test_list_templates_with_data(self, client):
        """Listing returns created templates with pagination."""
        # Create two templates
        await client.post(
            "/api/v1/generation/templates",
            json={"name": "template-a", "content": "A"},
        )
        await client.post(
            "/api/v1/generation/templates",
            json={"name": "template-b", "content": "B"},
        )

        resp = await client.get("/api/v1/generation/templates")
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["pagination"]["total"] == 2
        assert len(data["data"]["items"]) == 2

    @pytest.mark.asyncio
    async def test_list_templates_pagination(self, client):
        """Pagination works correctly."""
        # Create three templates
        for i in range(3):
            await client.post(
                "/api/v1/generation/templates",
                json={"name": f"tmpl-{i}", "content": f"Content {i}"},
            )

        resp = await client.get("/api/v1/generation/templates?page=1&page_size=2")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]["items"]) == 2
        assert data["data"]["pagination"]["total"] == 3
        assert data["data"]["pagination"]["page"] == 1
        assert data["data"]["pagination"]["page_size"] == 2


# ==================================================================
# Tests: Update Template (PUT /{name})
# ==================================================================


class TestUpdateTemplate:
    """PUT /api/v1/generation/templates/{name}"""

    @pytest.mark.asyncio
    async def test_update_template_success(self, client):
        """Updating an existing template returns 200."""
        # Create
        await client.post(
            "/api/v1/generation/templates",
            json={"name": "update-me", "content": "Old content"},
        )

        # Update
        resp = await client.put(
            "/api/v1/generation/templates/update-me",
            json={"content": "New content {{var}}", "description": "Updated desc"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["content"] == "New content {{var}}"
        assert data["data"]["description"] == "Updated desc"

    @pytest.mark.asyncio
    async def test_update_template_not_found_404(self, client):
        """Updating a non-existent template returns 404."""
        resp = await client.put(
            "/api/v1/generation/templates/nonexistent",
            json={"content": "Something"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"


# ==================================================================
# Tests: Delete Template (DELETE /{name})
# ==================================================================


class TestDeleteTemplate:
    """DELETE /api/v1/generation/templates/{name}"""

    @pytest.mark.asyncio
    async def test_delete_template_success(self, client):
        """Deleting an existing template returns 204."""
        # Create
        await client.post(
            "/api/v1/generation/templates",
            json={"name": "delete-me", "content": "To be deleted"},
        )

        resp = await client.delete("/api/v1/generation/templates/delete-me")
        assert resp.status_code == 204

        # Verify it's gone
        resp2 = await client.get("/api/v1/generation/templates/delete-me")
        assert resp2.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_template_not_found_404(self, client):
        """Deleting a non-existent template returns 404."""
        resp = await client.delete("/api/v1/generation/templates/nonexistent")
        assert resp.status_code == 404


# ==================================================================
# Tests: Cross-API-Key Access Control (Req 8.8)
# ==================================================================


class TestCrossApiKeyAccess:
    """Verify that API Key B cannot update/delete API Key A's templates."""

    @pytest.mark.asyncio
    async def test_different_keys_same_name_allowed(self, template_manager):
        """Two different API Keys can have templates with the same name (Req 8.7)."""
        # Key A creates template
        await template_manager.create(
            api_key_id="key-a", name="shared-name", content="Key A content"
        )
        # Key B creates template with same name — should succeed
        tmpl_b = await template_manager.create(
            api_key_id="key-b", name="shared-name", content="Key B content"
        )
        assert tmpl_b.api_key_id == "key-b"
        assert tmpl_b.content == "Key B content"

    @pytest.mark.asyncio
    async def test_cannot_get_other_keys_template(self, template_manager):
        """API Key B cannot see API Key A's template."""
        await template_manager.create(
            api_key_id="key-a", name="private-tmpl", content="Private"
        )
        # Key B tries to get it
        result = await template_manager.get(api_key_id="key-b", name="private-tmpl")
        assert result is None

    @pytest.mark.asyncio
    async def test_cannot_update_other_keys_template(self, template_manager):
        """Updating another key's template raises error."""
        await template_manager.create(
            api_key_id="key-a", name="owned-by-a", content="Original"
        )
        # Key B tries to update — template manager returns not found for key-b
        with pytest.raises(TemplateValidationError):
            await template_manager.update(
                api_key_id="key-b", name="owned-by-a", content="Hacked"
            )

    @pytest.mark.asyncio
    async def test_cannot_delete_other_keys_template(self, template_manager):
        """Deleting another key's template returns False (not found)."""
        await template_manager.create(
            api_key_id="key-a", name="owned-by-a", content="Original"
        )
        # Key B tries to delete — should return False
        result = await template_manager.delete(api_key_id="key-b", name="owned-by-a")
        assert result is False

        # Verify original template still exists
        original = await template_manager.get(api_key_id="key-a", name="owned-by-a")
        assert original is not None
        assert original.content == "Original"


# ==================================================================
# Tests: Cross-API-Key HTTP-Level Access Control (Req 8.8)
# ==================================================================


class TestCrossApiKeyHTTPAccess:
    """Verify cross-key access control through actual HTTP endpoints.

    Key A (test-key-1) creates a template; Key B (test-key-2)
    cannot read/update/delete it via API.
    """

    @pytest.mark.asyncio
    async def test_key_b_cannot_get_key_a_template_via_http(
        self, client, client_b, template_manager
    ):
        """API Key B gets 404 when trying to GET Key A's template via HTTP."""
        # Key A creates template through the API
        resp = await client.post(
            "/api/v1/generation/templates",
            json={"name": "key-a-only", "content": "Secret content {{var}}"},
        )
        assert resp.status_code == 201

        # Key B tries to GET it → 404 (cannot see other key's template)
        resp_b = await client_b.get("/api/v1/generation/templates/key-a-only")
        assert resp_b.status_code == 404
        assert resp_b.json()["error"]["code"] == "not_found"

    @pytest.mark.asyncio
    async def test_key_b_cannot_update_key_a_template_via_http(
        self, client, client_b, template_manager
    ):
        """API Key B gets 404 when trying to PUT Key A's template via HTTP."""
        # Key A creates template
        resp = await client.post(
            "/api/v1/generation/templates",
            json={"name": "owned-by-a-http", "content": "Original"},
        )
        assert resp.status_code == 201

        # Key B tries to update → 404
        resp_b = await client_b.put(
            "/api/v1/generation/templates/owned-by-a-http",
            json={"content": "Hacked content"},
        )
        assert resp_b.status_code == 404

        # Verify Key A's template is unchanged
        resp_verify = await client.get("/api/v1/generation/templates/owned-by-a-http")
        assert resp_verify.status_code == 200
        assert resp_verify.json()["data"]["content"] == "Original"

    @pytest.mark.asyncio
    async def test_key_b_cannot_delete_key_a_template_via_http(
        self, client, client_b, template_manager
    ):
        """API Key B gets 404 when trying to DELETE Key A's template via HTTP."""
        # Key A creates template
        resp = await client.post(
            "/api/v1/generation/templates",
            json={"name": "no-delete-by-b", "content": "Protected"},
        )
        assert resp.status_code == 201

        # Key B tries to delete → 404
        resp_b = await client_b.delete("/api/v1/generation/templates/no-delete-by-b")
        assert resp_b.status_code == 404

        # Verify it still exists for Key A
        resp_verify = await client.get("/api/v1/generation/templates/no-delete-by-b")
        assert resp_verify.status_code == 200

    @pytest.mark.asyncio
    async def test_same_name_different_keys_via_http(
        self, client, client_b, template_manager
    ):
        """Two keys can have templates with the same name (Req 8.7) via HTTP."""
        # Key A creates "shared-name"
        resp_a = await client.post(
            "/api/v1/generation/templates",
            json={"name": "shared-name", "content": "Content from A"},
        )
        assert resp_a.status_code == 201

        # Key B creates "shared-name" — should succeed
        resp_b = await client_b.post(
            "/api/v1/generation/templates",
            json={"name": "shared-name", "content": "Content from B"},
        )
        assert resp_b.status_code == 201

        # Each key sees only their own version
        resp_a_get = await client.get("/api/v1/generation/templates/shared-name")
        assert resp_a_get.status_code == 200
        assert resp_a_get.json()["data"]["content"] == "Content from A"

        resp_b_get = await client_b.get("/api/v1/generation/templates/shared-name")
        assert resp_b_get.status_code == 200
        assert resp_b_get.json()["data"]["content"] == "Content from B"

    @pytest.mark.asyncio
    async def test_key_b_cannot_render_key_a_template_via_http(
        self, client, client_b, template_manager
    ):
        """API Key B gets 404 when trying to render Key A's template via HTTP."""
        # Key A creates template
        await client.post(
            "/api/v1/generation/templates",
            json={"name": "render-only-a", "content": "Hello {{name}}!"},
        )

        # Key B tries to render → 404
        resp_b = await client_b.post(
            "/api/v1/generation/templates/render-only-a/render",
            json={"variables": {"name": "Intruder"}},
        )
        assert resp_b.status_code == 404


# ==================================================================
# Tests: Template Rendering and Missing Variables (Req 8.6)
# ==================================================================


class TestRenderTemplate:
    """POST /api/v1/generation/templates/{name}/render"""

    @pytest.mark.asyncio
    async def test_render_template_success(self, client):
        """Rendering with all variables provided returns 200."""
        # Create template
        await client.post(
            "/api/v1/generation/templates",
            json={
                "name": "render-test",
                "content": "Hello {{name}}, you are {{age}} years old.",
            },
        )

        # Render
        resp = await client.post(
            "/api/v1/generation/templates/render-test/render",
            json={"variables": {"name": "Alice", "age": "30"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["data"]["rendered"] == "Hello Alice, you are 30 years old."
        assert data["data"]["template_name"] == "render-test"

    @pytest.mark.asyncio
    async def test_render_template_missing_variables_400(self, client):
        """Rendering with missing variables returns 400 listing the missing vars."""
        # Create template with multiple variables
        await client.post(
            "/api/v1/generation/templates",
            json={
                "name": "multi-var",
                "content": "{{subject}} does {{action}} in {{location}}",
            },
        )

        # Render with only one variable provided
        resp = await client.post(
            "/api/v1/generation/templates/multi-var/render",
            json={"variables": {"subject": "Cat"}},
        )
        assert resp.status_code == 400
        error = resp.json()["error"]
        assert error["code"] == "validation_error"
        # Error message should mention missing variables
        assert "action" in error["message"] or "location" in error["message"]

    @pytest.mark.asyncio
    async def test_render_nonexistent_template_404(self, client):
        """Rendering a non-existent template returns 404."""
        resp = await client.post(
            "/api/v1/generation/templates/ghost/render",
            json={"variables": {}},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    @pytest.mark.asyncio
    async def test_render_no_variables_needed(self, client):
        """Rendering a template with no placeholders succeeds with empty vars."""
        await client.post(
            "/api/v1/generation/templates",
            json={"name": "static-tmpl", "content": "Just a static prompt."},
        )
        resp = await client.post(
            "/api/v1/generation/templates/static-tmpl/render",
            json={"variables": {}},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["rendered"] == "Just a static prompt."


# ==================================================================
# Tests: Service Unavailable (no manager)
# ==================================================================


class TestServiceUnavailable:
    """Endpoints return 503 when PromptTemplateManager is not initialized."""

    @pytest.mark.asyncio
    async def test_503_when_no_manager(self):
        """All endpoints return 503 if prompt_template_manager is None."""
        from aigateway_api.template_routes import router
        from fastapi.responses import JSONResponse
        from fastapi import HTTPException as _HTTPException

        test_app = FastAPI()
        test_app.include_router(router)

        @test_app.exception_handler(_HTTPException)
        async def http_exception_handler(request, exc):
            detail = exc.detail
            if isinstance(detail, dict) and "error" in detail:
                body = detail
            else:
                body = {"error": {"code": "internal_error", "message": str(detail) if detail else "Internal error"}}
            return JSONResponse(status_code=exc.status_code, content=body)

        test_app.state.prompt_template_manager = None

        mock_key_store = MagicMock()
        mock_key_store.validate = AsyncMock(
            return_value={"key_id": "test-key", "user_id": "user", "status": "active"}
        )
        test_app.state.key_store = mock_key_store

        async with AsyncClient(
            transport=ASGITransport(app=test_app),
            base_url="http://test",
            headers={"x-api-key": "sk-test"},
        ) as client:
            resp = await client.get("/api/v1/generation/templates")
            assert resp.status_code == 503

            resp = await client.post(
                "/api/v1/generation/templates",
                json={"name": "test", "content": "hi"},
            )
            assert resp.status_code == 503
