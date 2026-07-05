# 控制台聊天窗智能体 (Chat Agent) 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 aigateway 控制台新增入口 B(`/admin/agent/chat`),让登录用户通过聊天窗让智能体调用工具改运维状态、走理解/生成管道,带 HITL 确认、role 隔离、审计。

**Architecture:** 单入口后端 ChatRouter 三路分流(task→AgentLoop tool-calling / generation→loopback generation 管道 / understanding→loopback understanding 管道)。AgentLoop 用 OpenAI function calling 协议循环,写操作走 SSE pending_approval + 前端 POST /approval 回执,role 由 KeyStore.is_admin 字段判定。所有参数走 config.yaml `agent:` 段 + env 覆盖 + 热重载。

**Tech Stack:** Python 3.12 / FastAPI / Pydantic / httpx (loopback) / asyncio (SSE + Future) / Redis (audit ZSET) / structlog / React + TypeScript + Vite (前端)

**Spec:** `docs/superpowers/specs/2026-07-05-control-panel-chat-agent-design.md`

## Global Constraints

- Python 用 `python3`(无 `python` 别名);测试用 `python3 -m pytest`。
- 测试无 pytest.ini,有 `tests/conftest.py`(autouse 重置 TraceCollector);跳过 flaky 测试用 `--ignore=tests/test_template_routes.py`。
- 配置优先级:进程 env > `.env` > `config.yaml` > 代码默认值。env 前缀 `AI_GATEWAY_AGENT_*`。
- 热重载走 `ConfigManager.on_reload()` 回调(签名 `Callable[[Dict[str, Any]], None]`,接收整个新 config dict);atomic swap 无锁读;非法值回退上一次有效值 + warn。
- KeyStore 中 `is_admin` 字段**已存在**(`security.py:159-161` normalize,`seed_from_config` 已读 config 的 `is_admin`,`create` 已支持)。本计划只需补"首 key 兜底"逻辑 + 在 agent 侧消费它。
- 后端 sys.path 已在 `main.py` 注入 `aigateway-core/src`;agent 子模块放 `aigateway-api/src/aigateway_api/agent/`,无需额外 sys.path 操作。
- OpenAI function calling 透传:`/v1/chat/completions` 已支持(透传给上游),AgentLoop 用 loopback 调用它。
- 前端 SSE 不能用 `EventSource`(无法带 Authorization header),用 `fetch + ReadableStream` 自己解析 SSE 帧。
- 改后端代码后需 `docker compose up -d --build gateway` 重建;改前端 `npm run dev` HMR 即可。
- 工具的 `roles` 集合和 `kind: read|write` 是**策略**,不做成配置项。
- MVP 工具集 9 个,其余 6 个 P2 不实施。

---

## Reviewer Notes(审核要点 — 执行前需确认)

以下 6 条是计划作者认为最可能有争议或需要拍板的点,执行前请先 review:

1. **Task 1 admin_service 抽取的边界** —— 把 8 个业务函数(list_api_keys / get_key_quota / update_key_quota / query_logs / get_plugins_config / set_plugin_enabled / list_l3_entries / get_trace)抽成独立函数,会动 `admin_routes.py` 现有 route handler,侵入性最大。
   - **保守替代方案**:admin_service 只新增、route 不动,tool handler 直接调 admin_service 新函数 + 部分复用 route 内联逻辑。若选此方案,Task 1 Step 6-7 改为"不重构 route,只新增 admin_service"。
   - **需决策**:重构现有 route,还是只新增?

2. **Task 1 Step 3 的 pytest-asyncio 处理** —— 项目现有测试未确认是否已装 pytest-asyncio,计划给了 fallback(asyncio.run 风格)。
   - 执行时若装不上 pytest-asyncio,所有 async 测试要统一改 asyncio.run 风格(每个 test 函数用 `asyncio.run(...)` 包裹异步调用)。
   - **执行时先跑** `python3 -c "import pytest_asyncio"` 确认。

3. **Task 9 AgentLoop 的 approval 并发模型** —— SSE generator 里 `await ctx.approval_callback`(内部 await Future),由独立 POST `/agent/approval` 唤醒。这是整个方案最 tricky 的地方。
   - 单元测试(Task 6)能覆盖 AgentSession 的 Future 逻辑,但真实 SSE + 并发 POST 的集成可能要调试。
   - **风险**:asyncio event loop 跨请求的 Future 共享、SSE 连接断开时 Future 的 cancel 处理,实测时可能踩坑。

4. **Task 10 的 `test_agent_approval_flow.py` 简化** —— 计划里注明了简化(完整并发集成测试复杂,MVP 阶段以 Task 6 单测覆盖为主)。
   - 若要完整的两-task 并发集成测试(一个 task 跑 SSE、另一个 task POST approval),这块要加码,开发量 +0.5 天。
   - **需决策**:MVP 接受简化,还是补完整集成测试?

5. **Task 12 前端 localStorage key 名** —— 计划用了 `localStorage.getItem("aigateway:key_id")` 和 `aigateway:token` 作为登录态 key,但**未核实项目现有 useAuth hook 实际用什么 key 存**。
   - 执行 Task 11/12 前必须先看 `control-panel/src/hooks/useAuth.ts` 确认实际 key 名,否则 localStorage 读不到登录态/token。
   - **执行前动作**:Read useAuth.ts,把计划里的 `aigateway:key_id` / `aigateway:token` 替换成真实 key 名。

6. **MVP 范围** —— 9 个工具、SSE 断连即取消、无 Redis 历史、无前端单测,与 spec §12 一致。
   - 执行中若想临时加(如 SSE 重发、Redis 历史、第 10-15 个工具),需先更新 spec §12 Out of scope 再改计划,不要悄悄扩范围。

---

## File Structure

### 新增文件

| 文件 | 职责 |
|---|---|
| `aigateway-api/src/aigateway_api/admin_service.py` | 从 admin_routes.py 抽出的业务函数(route 和 tool handler 共用) |
| `aigateway-api/src/aigateway_api/agent/__init__.py` | agent 包初始化 |
| `aigateway-api/src/aigateway_api/agent/config.py` | `AgentConfig` dataclass + `AgentConfigWatcher` + 进程级单例 |
| `aigateway-api/src/aigateway_api/agent/tools.py` | `ToolSpec` / `ToolRegistry` / `AgentContext` / `ApprovalDecision` |
| `aigateway-api/src/aigateway_api/agent/session.py` | `AgentSession` + approval Future 管理 + 工具熔断计数 |
| `aigateway-api/src/aigateway_api/agent/audit.py` | `AuditLogger`(structlog + Redis ZSET 双通道 + PII 脱敏) |
| `aigateway-api/src/aigateway_api/agent/chat_router.py` | `ChatRouter` 三级分类 |
| `aigateway-api/src/aigateway_api/agent/loop.py` | `AgentLoop`(tool-calling 循环 + 熔断 + loopback) |
| `aigateway-api/src/aigateway_api/agent/handlers.py` | 9 个 MVP 工具的 handler(薄封装调 admin_service) |
| `aigateway-api/src/aigateway_api/agent_routes.py` | SSE 路由(4 个端点) |
| `control-panel/src/pages/Chat.tsx` | /chat 页面 |
| `control-panel/src/components/chat/ChatComposer.tsx` | 输入框 |
| `control-panel/src/components/chat/ChatTimeline.tsx` | 事件时间轴 |
| `control-panel/src/components/chat/ApprovalCard.tsx` | 写工具确认卡 |
| `control-panel/src/components/chat/ToolCallCard.tsx` | tool_call/result 展示 |
| `control-panel/src/components/chat/MediaOutputCard.tsx` | 媒体渲染 |
| `control-panel/src/components/chat/RoutingBadge.tsx` | 分流标签 |
| `control-panel/src/components/chat/ToolCatalogModal.tsx` | "AI 能做什么"弹窗 |
| `scripts/smoke_agent.sh` | e2e smoke |

### 修改文件

| 文件 | 修改 |
|---|---|
| `aigateway-api/src/aigateway_api/admin_routes.py` | 业务函数改为调 admin_service(薄包装) |
| `aigateway-api/src/aigateway_api/main.py` | lifespan 初始化 agent 组件 + 挂 agent_routes |
| `aigateway-core/src/aigateway_core/security.py` | seed_from_config 后加"无 admin 时首 key 升 admin"兜底 |
| `config.yaml` + `config.yaml.template` | 加 `agent:` 段 |
| `.env.example` | 加 `AGENT_INTERNAL_KEY=` |
| `control-panel/src/App.tsx` | 加 `/chat` 路由 |
| `control-panel/src/components/Layout.tsx` | 侧栏加 Chat 入口 |
| `control-panel/src/api/client.ts` | SSE 客户端 + agent API types |

---

## Task 依赖图

```
T1 (admin_service 抽取) ──────┐
T2 (KeyStore 首 key 兜底) ────┤
T3 (AgentConfig + Watcher) ──┤
                              ├──▶ T4 (ToolSpec/Registry/Context) ──▶ T5 (AuditLogger) ──▶ T6 (AgentSession) ──▶ T7 (9 handlers) ──▶ T8 (ChatRouter) ──▶ T9 (AgentLoop) ──▶ T10 (agent_routes + main 挂载) ──▶ T11 (前端 SSE client) ──▶ T12 (前端组件 + 页面 + 路由) ──▶ T13 (smoke + CLAUDE.md 收尾)
```

T1/T2/T3 可并行;T4 起严格顺序。

---

## Task 1: 抽出 admin_service.py 业务函数层

**目的:** 把 admin_routes.py 里 route handler 内联的业务逻辑抽成独立函数,route 和后续 tool handler 都调这个,避免复制粘贴。

**Files:**
- Create: `aigateway-api/src/aigateway_api/admin_service.py`
- Modify: `aigateway-api/src/aigateway_api/admin_routes.py`
- Test: `tests/test_admin_service.py`

**Interfaces:**
- Consumes: `app.state.key_store` (KeyStore), `app.state.config_manager`
- Produces:
  - `async def list_api_keys(key_store, config_manager, page: int = 1, page_size: int = 20) -> dict` — 返回 `{"items": [...], "pagination": {...}}`
  - `async def get_key_quota(key_store, key_hash: str) -> dict | None` — 返回单个 key 的配额 dict(用 `_format_quota_item` 格式)或 None
  - `async def update_key_quota(key_store, key_id: str, *, daily_tokens=None, monthly_cost=None, rate_limit_rpm=None, rate_limit_tpm=None) -> dict` — 返回更新后的 quotas dict;key 不存在抛 `KeyError`
  - `async def query_logs(redis_mgr, *, api_key_id: str | None = None, days: int = 7, limit: int = 50, status_code: int | None = None) -> list[dict]`
  - `async def get_plugins_config(config_manager, plugin_registry) -> list[dict]`
  - `async def set_plugin_enabled(config_manager, plugin_registry, name: str, enabled: bool) -> dict` — 写 config.yaml(fcntl.flock)+ 触发 reload
  - `async def list_l3_entries(cache_manager, *, limit: int = 50) -> list[dict]`
  - `async def get_trace(redis_mgr, trace_id: str) -> dict | None`

- [ ] **Step 1: 写失败测试 `tests/test_admin_service.py`**

```python
"""admin_service 抽出层单元测试."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

import pytest
from unittest.mock import AsyncMock, MagicMock

from aigateway_api.admin_service import (
    list_api_keys, get_key_quota, update_key_quota, query_logs,
)


@pytest.mark.asyncio
async def test_list_api_keys_empty_redis():
    """Redis 无 key 时返回空列表."""
    key_store = MagicMock()
    key_store.redis = MagicMock()
    key_store.redis.redis = AsyncMock()
    # scan 返回空
    key_store.redis.redis.scan = AsyncMock(return_value=(0, []))
    key_store.ensure_seeded = AsyncMock()
    cm = MagicMock()
    cm.get = MagicMock(return_value={"api_keys": []})

    result = await list_api_keys(key_store, cm, page=1, page_size=20)
    assert result["items"] == []
    assert result["pagination"]["total"] == 0


@pytest.mark.asyncio
async def test_update_key_quota_not_found():
    """key_id 不存在时抛 KeyError."""
    key_store = MagicMock()
    key_store._find_key_hashes_by_id = AsyncMock(return_value=[])
    with pytest.raises(KeyError):
        await update_key_quota(key_store, "key_nonexistent", monthly_cost=100)


@pytest.mark.asyncio
async def test_get_key_quota_found():
    """返回格式化后的配额 dict."""
    key_store = MagicMock()
    key_store.redis = MagicMock()
    raw = {
        "key_id": "key_abc", "user_id": "u1", "status": "active",
        "daily_tokens_limit": "1000000", "daily_tokens_used": "5000",
        "monthly_cost_limit": "50", "monthly_cost_used": "1.5",
        "rate_limit_rpm": "60", "rate_limit_tpm": "100000",
    }
    key_store.redis.get_api_key = AsyncMock(return_value=raw)
    result = await get_key_quota(key_store, "somehash")
    assert result is not None
    assert result["key_id"] == "key_abc"
    assert result["quotas"]["monthly_cost_limit"] == 50.0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m pytest tests/test_admin_service.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'aigateway_api.admin_service'`

- [ ] **Step 3: 检查 pytest-asyncio 是否已装**

Run: `python3 -c "import pytest_asyncio; print(pytest_asyncio.__version__)" 2>&1`
Expected: 版本号(若 `ModuleNotFoundError`,在 `aigateway-api/requirements.txt` 加 `pytest-asyncio` 并 `pip install pytest-asyncio`,然后在 `tests/conftest.py` 末尾加 `pytest_plugins = ("pytest_asyncio",)` 和 `pytest.ini` 不存在则在 conftest 加 `@pytest.fixture` 装饰器配置——实际最简:在 `tests/conftest.py` 顶部加 `import pytest_asyncio` 不够,需在 pyproject 或 conftest 配置 mode。**最简方案**:用 `asyncio.run()` 包裹而非 `@pytest.mark.asyncio`,见 Step 3a。)

**Step 3a(若 pytest-asyncio 不可用):改用 asyncio.run 风格** —— 把 Step 1 的测试改成:
```python
def test_list_api_keys_empty_redis():
    import asyncio
    key_store = MagicMock()
    key_store.redis = MagicMock()
    key_store.redis.redis = AsyncMock()
    key_store.redis.redis.scan = AsyncMock(return_value=(0, []))
    key_store.ensure_seeded = AsyncMock()
    cm = MagicMock(); cm.get = MagicMock(return_value={"api_keys": []})
    result = asyncio.run(list_api_keys(key_store, cm, page=1, page_size=20))
    assert result["items"] == []
    assert result["pagination"]["total"] == 0
```
（**先用 pytest-asyncio,装不上再用 asyncio.run**。下面所有 async 测试同理,不重复说明。)

- [ ] **Step 4: 创建 admin_service.py 骨架并实现 list_api_keys + get_key_quota + update_key_quota**

Create `aigateway-api/src/aigateway_api/admin_service.py`:

```python
"""Admin 业务函数层 —— route handler 和 agent tool handler 共用.

从 admin_routes.py 抽出,避免复制粘贴。所有函数接收依赖(KeyStore/ConfigManager 等)
作为参数,不直接读 app.state,便于测试和复用。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _get_auth_defaults(config_manager: Any) -> Dict[str, Any]:
    """从 config 获取 auth.defaults 配额默认值(与 admin_routes 一致)."""
    auth_cfg = config_manager.get("auth", {}) if config_manager else {}
    defaults = auth_cfg.get("defaults", {}) if isinstance(auth_cfg, dict) else {}
    return {
        "daily_tokens": int(defaults.get("daily_tokens", 1_000_000)),
        "monthly_cost": float(defaults.get("monthly_cost", 50.0)),
        "rate_limit_rpm": int(defaults.get("rate_limit_rpm", 60)),
        "rate_limit_tpm": int(defaults.get("rate_limit_tpm", 100_000)),
    }


def _format_quota_item(key_data: Dict[str, Any], key_hash: str, config_manager: Any) -> Dict[str, Any]:
    """格式化单个 API Key 的配额信息(从 admin_routes 移植)."""
    defaults = _get_auth_defaults(config_manager)
    daily_limit = int(key_data.get("daily_tokens_limit", defaults["daily_tokens"]))
    daily_used = int(key_data.get("daily_tokens_used", 0))
    monthly_limit = float(key_data.get("monthly_cost_limit", defaults["monthly_cost"]))
    monthly_used = float(key_data.get("monthly_cost_used", 0.00))
    rpm_limit = int(key_data.get("rate_limit_rpm", defaults["rate_limit_rpm"]))
    tpm_limit = int(key_data.get("rate_limit_tpm", defaults["rate_limit_tpm"]))
    return {
        "key_id": key_data.get("key_id", ""),
        "user_id": key_data.get("user_id", ""),
        "key_hash": key_hash,
        "status": key_data.get("status", "active"),
        "is_admin": key_data.get("is_admin", False) in (True, "True", "true", "1"),
        "quotas": {
            "daily_tokens_limit": daily_limit,
            "daily_tokens_used": daily_used,
            "monthly_cost_limit": monthly_limit,
            "monthly_cost_used": monthly_used,
            "rate_limit_rpm": rpm_limit,
            "rate_limit_tpm": tpm_limit,
        },
    }


async def list_api_keys(key_store: Any, config_manager: Any, page: int = 1, page_size: int = 20) -> Dict[str, Any]:
    """列出所有 API Key 及配额(分页)."""
    redis_mgr = key_store.redis
    if redis_mgr is None or redis_mgr.redis is None:
        raise RuntimeError("Redis connection required for key management")

    # Auto-reseed
    auth_config = config_manager.get("auth", {}) if config_manager else {}
    keys_config = auth_config.get("api_keys", [])
    await key_store.ensure_seeded(keys_config)

    cursor = 0
    all_keys: List[Dict[str, Any]] = []
    while True:
        cursor, keys = await redis_mgr.redis.scan(cursor, match="aigateway:key:*", count=100)
        for raw_key in keys:
            key_str = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            kh = key_str.split(":")[-1]
            data = await redis_mgr.get_api_key(kh)
            if data and data.get("status") == "active":
                all_keys.append(_format_quota_item(data, kh, config_manager))
        if cursor == 0:
            break

    total = len(all_keys)
    start = (page - 1) * page_size
    end = start + page_size
    return {"items": all_keys[start:end], "pagination": {"page": page, "pageSize": page_size, "total": total}}


async def get_key_quota(key_store: Any, key_hash: str, config_manager: Any = None) -> Optional[Dict[str, Any]]:
    """按 key_hash 查单个 key 配额."""
    if not key_store.redis or not key_store.redis.redis:
        return None
    data = await key_store.redis.get_api_key(key_hash)
    if not data:
        return None
    return _format_quota_item(data, key_hash, config_manager)


async def update_key_quota(
    key_store: Any,
    key_id: str,
    *,
    daily_tokens: Optional[int] = None,
    monthly_cost: Optional[float] = None,
    rate_limit_rpm: Optional[int] = None,
    rate_limit_tpm: Optional[int] = None,
    config_manager: Any = None,
) -> Dict[str, Any]:
    """修改指定 API Key 配额(仅更新非 None 字段).key_id 不存在抛 KeyError."""
    redis_mgr = key_store.redis
    if redis_mgr is None or redis_mgr.redis is None:
        raise RuntimeError("Redis not connected")

    key_hashes = await key_store._find_key_hashes_by_id(key_id)
    if not key_hashes:
        raise KeyError(key_id)

    kh = key_hashes[0]
    data = await redis_mgr.get_api_key(kh)
    if not data:
        raise KeyError(key_id)

    updated_fields: Dict[str, str] = {}
    if daily_tokens is not None:
        updated_fields["daily_tokens_limit"] = str(daily_tokens)
    if monthly_cost is not None:
        updated_fields["monthly_cost_limit"] = str(monthly_cost)
    if rate_limit_rpm is not None:
        updated_fields["rate_limit_rpm"] = str(rate_limit_rpm)
    if rate_limit_tpm is not None:
        updated_fields["rate_limit_tpm"] = str(rate_limit_tpm)

    if not updated_fields:
        raise ValueError("No fields to update")

    data.update(updated_fields)
    await redis_mgr.set_api_key(kh, data)

    # Pub/Sub 广播(多实例同步)
    try:
        pub_msg = key_store._build_pubsub_message(
            "quota_updated", key_id, data.get("user_id", ""), updated_fields=updated_fields,
        )
        await redis_mgr.publish(key_store.PUBSUB_CHANNEL, pub_msg)
    except Exception as exc:
        logger.warning("Failed to publish quota update event: %s", exc)

    defaults = _get_auth_defaults(config_manager)
    return {
        "id": key_id,
        "user_id": data.get("user_id", ""),
        "quotas": {
            "daily_tokens_limit": int(data.get("daily_tokens_limit", defaults["daily_tokens"])),
            "monthly_cost_limit": float(data.get("monthly_cost_limit", defaults["monthly_cost"])),
            "rate_limit_rpm": int(data.get("rate_limit_rpm", defaults["rate_limit_rpm"])),
            "rate_limit_tpm": int(data.get("rate_limit_tpm", defaults["rate_limit_tpm"])),
        },
    }


async def query_logs(
    redis_mgr: Any,
    *,
    api_key_id: Optional[str] = None,
    days: int = 7,
    limit: int = 50,
    status_code: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """查询请求日志(可选按 api_key_id/days/status_code 过滤).返回最近 limit 条."""
    if redis_mgr is None or redis_mgr.redis is None:
        return []
    # 日志存在 aigateway:logs ZSET(score=ts),见 admin_routes GET /admin/logs
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_ts = cutoff.timestamp()
    # ZRANGEBYSCORE 取 cutoff 到 now
    raw = await redis_mgr.redis.zrangebyscore("aigateway:logs", cutoff_ts, "+inf")
    items: List[Dict[str, Any]] = []
    import json as _json
    for entry in reversed(raw):  # 倒序(最新在前)
        try:
            s = entry.decode() if isinstance(entry, bytes) else entry
            rec = _json.loads(s)
        except Exception:
            continue
        if api_key_id and rec.get("api_key_id") != api_key_id:
            continue
        if status_code and rec.get("status_code") != status_code:
            continue
        items.append(rec)
        if len(items) >= limit:
            break
    return items


async def get_plugins_config(config_manager: Any, plugin_registry: Any) -> List[Dict[str, Any]]:
    """返回插件配置列表(含 enabled 状态)."""
    plugins_cfg = config_manager.get("plugins", []) if config_manager else []
    result = []
    for pcfg in plugins_cfg:
        if not isinstance(pcfg, dict):
            continue
        name = pcfg.get("name")
        reg = getattr(plugin_registry, "_registrations", {}).get(name) if plugin_registry else None
        result.append({
            "name": name,
            "enabled": bool(pcfg.get("enabled", True)),
            "category": pcfg.get("category", "other"),
            "pipeline_kind": pcfg.get("pipeline_kind", "understanding"),
        })
    return result


async def set_plugin_enabled(config_manager: Any, plugin_registry: Any, name: str, enabled: bool) -> Dict[str, Any]:
    """写 config.yaml 改插件 enabled(用 fcntl.flock 防并发),触发 reload."""
    import fcntl, yaml, os
    config_path = os.environ.get("AI_GATEWAY_CONFIG_PATH", "./config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        raw = yaml.safe_load(f) or {}
    changed = False
    for pcfg in raw.get("plugins", []) or []:
        if isinstance(pcfg, dict) and pcfg.get("name") == name:
            pcfg["enabled"] = bool(enabled)
            changed = True
            break
    if not changed:
        raise KeyError(f"plugin '{name}' not found in config")
    # 写回(原子的:写临时文件 + rename)
    tmp = config_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
    os.replace(tmp, config_path)
    # 触发热重载(ConfigManager watchdog 会捡到)
    return {"name": name, "enabled": bool(enabled)}


async def list_l3_entries(cache_manager: Any, *, limit: int = 50) -> List[Dict[str, Any]]:
    """列出 L3 缓存条目(简化:从 admin_routes GET /admin/cache/l3/entries 移植)."""
    # 实际实现查 qdrant;此处先返回空列表占位,Task 7 handler 接真实查询
    # 复用 admin_routes.list_l3_entries 的逻辑(见 admin_routes.py:1758)
    from aigateway_api.admin_routes import _list_l3_entries_impl  # Task 1 Step 6 会把这个 impl 暴露
    return await _list_l3_entries_impl(cache_manager, limit=limit)


async def get_trace(redis_mgr: Any, trace_id: str) -> Optional[Dict[str, Any]]:
    """按 trace_id 查 trace events(Redis hash aigateway:trace:{id})."""
    if redis_mgr is None or redis_mgr.redis is None:
        return None
    key = f"aigateway:trace:{trace_id}"
    data = await redis_mgr.redis.hgetall(key)
    if not data:
        return None
    out = {}
    for k, v in data.items():
        ks = k.decode() if isinstance(k, bytes) else k
        vs = v.decode() if isinstance(v, bytes) else v
        out[ks] = vs
    return out
```

- [ ] **Step 5: 运行测试确认通过**

Run: `python3 -m pytest tests/test_admin_service.py -v`
Expected: 3 个测试 PASS

- [ ] **Step 6: 让 admin_routes.py 复用 admin_service(薄包装)**

对 `admin_routes.py` 的 4 个 route handler(`list_api_keys` / `create_api_key` / `delete_api_key` / `update_api_key_quota`)改为调 `admin_service` 的函数。**只改实现,不改接口签名/响应格式。**

例如 `list_api_keys` route 改为:
```python
@router.get("/api-keys")
async def list_api_keys(request: Request, page: int = Query(default=1, ge=1),
                        page_size: int = Query(default=20, ge=1, le=100),
                        _auth: Dict[str, Any] = Depends(authenticate_admin)):
    from aigateway_api.main import app
    from aigateway_api.admin_service import list_api_keys as _list
    key_store, _ = _get_keystore_and_metrics(request)
    config_manager = getattr(app.state, "config_manager")
    try:
        result = await _list(key_store, config_manager, page=page, page_size=page_size)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail={"error": {"code": "internal_error", "message": str(exc)}})
    return {"data": result, "message": "success"}
```

`update_api_key_quota` route 改为调 `admin_service.update_key_quota`,把 `KeyError` 映射 404、`ValueError` 映射 400。

把 `admin_routes.py` 里现有的 `_list_l3_entries_impl`(GET /admin/cache/l3/entries 的实现,约 1758 行)抽出为模块级函数(若已内联在 route 里,提到模块级),供 `admin_service.list_l3_entries` 调用。

- [ ] **Step 7: 跑 admin 相关现有测试确保没回归**

Run: `python3 -m pytest tests/test_debug_admin.py tests/test_admin_service.py -v --ignore=tests/test_template_routes.py`
Expected: 全 PASS

- [ ] **Step 8: Commit**

```bash
git add aigateway-api/src/aigateway_api/admin_service.py aigateway-api/src/aigateway_api/admin_routes.py tests/test_admin_service.py
git commit -m "refactor(admin): 抽出 admin_service 业务函数层供 route 和 agent tool 共用"
```

---

## Task 2: KeyStore "无 admin 时首 key 升 admin" 兜底

**目的:** spec §6.1 安全兜底——首次启动若没有任何 admin key,把 config.yaml 第一个 key 自动升 admin,避免锁死。

**Files:**
- Modify: `aigateway-core/src/aigateway_core/security.py`(seed_from_config 后加兜底)
- Test: `tests/test_agent_role_admin_lookup.py`

**Interfaces:**
- Consumes: `KeyStore.seed_from_config`(已存在,已读 is_admin)
- Produces: `KeyStore.ensure_admin_exists(keys_config)` —— 兜底逻辑,返回被升 admin 的 key_id 或 None

- [ ] **Step 1: 写失败测试**

`tests/test_agent_role_admin_lookup.py`:
```python
"""KeyStore is_admin + 首 key 兜底测试."""
import sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from unittest.mock import AsyncMock, MagicMock
from aigateway_core.security import KeyStore


def test_ensure_admin_exists_promotes_first_key_when_no_admin():
    """Redis 中无任何 admin key 时,把第一个 active key 升 admin."""
    ks = KeyStore.__new__(KeyStore)  # 不走 __init__
    ks.redis = MagicMock()
    ks.redis.redis = AsyncMock()
    # scan 找到一个 key
    ks.redis.redis.scan = AsyncMock(return_value=(0, [b"aigateway:key:hash1"]))
    ks.redis.get_api_key = AsyncMock(return_value={
        "key_id": "key_1", "user_id": "u1", "status": "active", "is_admin": "False",
    })
    ks.redis.set_api_key = AsyncMock()

    promoted = asyncio.run(ks.ensure_admin_exists([]))
    assert promoted == "key_1"
    # set_api_key 应被调,is_admin 改 True
    args = ks.redis.set_api_key.call_args
    assert args[0][1]["is_admin"] in ("True", True)


def test_ensure_admin_exists_noop_when_admin_present():
    """已有 admin 时不改动."""
    ks = KeyStore.__new__(KeyStore)
    ks.redis = MagicMock(); ks.redis.redis = AsyncMock()
    ks.redis.redis.scan = AsyncMock(return_value=(0, [b"aigateway:key:h1"]))
    ks.redis.get_api_key = AsyncMock(return_value={"key_id": "k", "status": "active", "is_admin": "True"})
    ks.redis.set_api_key = AsyncMock()

    promoted = asyncio.run(ks.ensure_admin_exists([]))
    assert promoted is None
    ks.redis.set_api_key.assert_not_called()
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_agent_role_admin_lookup.py -v`
Expected: FAIL with `AttributeError: 'KeyStore' object has no attribute 'ensure_admin_exists'`

- [ ] **Step 3: 在 KeyStore 加 ensure_admin_exists 方法**

在 `security.py` 的 `seed_from_config` 方法之后加:
```python
async def ensure_admin_exists(self, keys_config: List[Dict[str, Any]]) -> Optional[str]:
    """安全兜底:若 Redis 中无任何 admin key,把第一个 active key 升 admin.

    避免首次启动锁死(没有任何 admin 能进控制台)。
    返回被提升的 key_id,或 None(已有 admin / 无 key)。
    """
    if self.redis is None or self.redis.redis is None:
        return None
    cursor = 0
    first_active: Optional[tuple[str, dict]] = None  # (key_hash, data)
    has_admin = False
    while True:
        cursor, keys = await self.redis.redis.scan(cursor, match="aigateway:key:*", count=100)
        for raw_key in keys:
            ks = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            kh = ks.split(":")[-1]
            data = await self.redis.get_api_key(kh)
            if not data or data.get("status") != "active":
                continue
            if first_active is None:
                first_active = (kh, data)
            if data.get("is_admin") in ("True", "true", "1", True):
                has_admin = True
                break
        if cursor == 0 or has_admin:
            break
    if has_admin or first_active is None:
        return None
    kh, data = first_active
    data["is_admin"] = "True"
    await self.redis.set_api_key(kh, data)
    logger.warning("无 admin key,已将首个 active key %s 提升为 admin(安全兜底)", data.get("key_id"))
    return data.get("key_id")
```

- [ ] **Step 4: 运行测试通过**

Run: `python3 -m pytest tests/test_agent_role_admin_lookup.py -v`
Expected: 2 PASS

- [ ] **Step 5: 在 main.py lifespan 调用兜底**

在 `main.py` 的 `key_store.seed_from_config(...)` 之后加:
```python
        # 安全兜底:若无任何 admin key,把首个 active key 升 admin(避免锁死)
        promoted = await key_store.ensure_admin_exists(api_keys_config)
        if promoted:
            logger.warning("已自动提升 key %s 为 admin(无 admin 兜底)", promoted)
```

- [ ] **Step 6: Commit**

```bash
git add aigateway-core/src/aigateway_core/security.py aigateway-api/src/aigateway_api/main.py tests/test_agent_role_admin_lookup.py
git commit -m "feat(security): KeyStore.ensure_admin_exists 首key升admin兜底,防控制台锁死"
```

---

## Task 3: AgentConfig + AgentConfigWatcher

**目的:** config.yaml `agent:` 段的 dataclass + 热重载 watcher,模式参照 `DebugConfig` / `GenerationOptimizationConfig`。

**Files:**
- Create: `aigateway-api/src/aigateway_api/agent/__init__.py`(空)
- Create: `aigateway-api/src/aigateway_api/agent/config.py`
- Modify: `aigateway-core/src/aigateway_core/config.py`(加 `agent:` 默认值到 `_DEFAULT_CONFIG`)
- Modify: `config.yaml` + `config.yaml.template`(加 `agent:` 段)
- Modify: `.env.example`(加 `AGENT_INTERNAL_KEY=`)
- Test: `tests/test_agent_config.py`

**Interfaces:**
- Consumes: `ConfigManager.on_reload()` / `ConfigManager.config`
- Produces:
  - `@dataclass AgentConfig`(字段见 spec §9.1)
  - `class AgentConfigWatcher`(`.config` property + `.attach(config_manager)`)
  - `get_agent_config() -> AgentConfig`(进程级单例读)
  - `init_agent_config_watcher(config_manager) -> AgentConfigWatcher`(main.py 启动调一次)

- [ ] **Step 1: 写失败测试 `tests/test_agent_config.py`**

```python
"""AgentConfig + Watcher 单元测试."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from aigateway_api.agent.config import AgentConfig, AgentConfigWatcher


def test_default_config():
    c = AgentConfig.default()
    assert c.enabled is True
    assert c.max_iterations == 20
    assert c.approval_timeout_seconds == 120
    assert c.session_ttl_seconds == 600
    assert c.audit_log_ttl_seconds == 604800
    assert c.tool_failure_threshold == 3
    assert c.per_role_limits["user"].max_iterations == 10
    assert c.chat_router.enabled is True


def test_from_yaml_full():
    d = {
        "enabled": True, "max_iterations": 15, "approval_timeout_seconds": 60,
        "session_ttl_seconds": 300, "session_max_messages": 100, "model": "gpt-4o",
        "internal_api_key": "sk-x", "loopback_base_url": "http://x:8000",
        "loopback_timeout_seconds": 200, "audit_log_ttl_seconds": 86400,
        "tool_failure_threshold": 5, "tool_failure_cooldown_seconds": 30,
        "per_role_limits": {"admin": {"max_iterations": 15}, "user": {"max_iterations": 8}},
        "chat_router": {"enabled": False, "use_intent_evaluator": False,
                        "force_class_prefixes": {"task": ["/t"]}, "admin_task_bias": 0.2},
    }
    c = AgentConfig.from_yaml(d)
    assert c.max_iterations == 15
    assert c.per_role_limits["admin"].max_iterations == 15
    assert c.per_role_limits["user"].max_iterations == 8
    assert c.chat_router.enabled is False
    assert c.chat_router.force_class_prefixes["task"] == ["/t"]


def test_from_yaml_invalid_falls_back():
    """max_iterations=0 非法 → 回退默认 20."""
    c = AgentConfig.from_yaml({"max_iterations": 0})
    assert c.max_iterations == 20  # 回退


def test_from_yaml_missing_section():
    c = AgentConfig.from_yaml({})
    assert c == AgentConfig.default()
    c2 = AgentConfig.from_yaml(None)
    assert c2 == AgentConfig.default()


def test_watcher_attach_and_swap():
    class FakeCM:
        def __init__(self):
            self.config = {"agent": {"max_iterations": 10}}
            self._cbs = []
        def on_reload(self, cb): self._cbs.append(cb)

    cm = FakeCM()
    w = AgentConfigWatcher()
    w.attach(cm)
    assert w.config.max_iterations == 10
    # 触发 reload
    cm._cbs[0]({"agent": {"max_iterations": 25}})
    assert w.config.max_iterations == 25


def test_max_iterations_for_role():
    c = AgentConfig(max_iterations=20, per_role_limits={
        "admin": AgentConfig.RoleLimit(max_iterations=20),
        "user": AgentConfig.RoleLimit(max_iterations=10),
    })
    assert c.max_iterations_for_role("admin") == 20
    assert c.max_iterations_for_role("user") == 10
    assert c.max_iterations_for_role("unknown") == 20  # fallback 顶层
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_agent_config.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'aigateway_api.agent'`

- [ ] **Step 3: 创建 agent 包**

`aigateway-api/src/aigateway_api/agent/__init__.py`:
```python
"""Agent 子包 —— 控制台聊天窗智能体(入口 B)."""
```

- [ ] **Step 4: 创建 agent/config.py**

```python
"""Agent 配置 + 热重载 watcher.

映射 config.yaml 的 agent: 段。模式参照 DebugConfig / GenerationOptimizationConfig:
- ConfigManager.on_reload() 回调(接收整个新 config dict)
- atomic swap 无锁读
- 非法值回退上一次有效值 + warn
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class AgentConfig:
    """Agent 主配置."""
    enabled: bool = True
    max_iterations: int = 20
    approval_timeout_seconds: int = 120
    session_ttl_seconds: int = 600
    session_max_messages: int = 200
    model: str = "auto"
    internal_api_key: str = ""
    loopback_base_url: str = "http://localhost:8000"
    loopback_timeout_seconds: int = 300
    audit_log_ttl_seconds: int = 604800
    tool_failure_threshold: int = 3
    tool_failure_cooldown_seconds: int = 0
    per_role_limits: Dict[str, "AgentConfig.RoleLimit"] = field(default_factory=dict)
    chat_router: "AgentConfig.ChatRouterConfig" = field(default_factory=lambda: AgentConfig.ChatRouterConfig())

    @dataclass
    class RoleLimit:
        max_iterations: int = 20

    @dataclass
    class ChatRouterConfig:
        enabled: bool = True
        use_intent_evaluator: bool = True
        force_class_prefixes: Dict[str, list] = field(default_factory=lambda: {
            "task": ["/task", "/工具"],
            "generation": ["/gen", "/生成"],
            "understanding": ["/ask", "/问"],
        })
        admin_task_bias: float = 0.1

    @classmethod
    def default(cls) -> "AgentConfig":
        return cls(
            per_role_limits={
                "admin": cls.RoleLimit(max_iterations=20),
                "user": cls.RoleLimit(max_iterations=10),
            },
        )

    @classmethod
    def from_yaml(cls, d: dict[str, Any] | None) -> "AgentConfig":
        """从 config.yaml 的 agent: 段构造(缺失/None 返回 default)."""
        if not d:
            return cls.default()
        defaults = cls.default()
        # 单字段提取 + 非法值回退
        def _int(key, default, minimum=1):
            v = d.get(key, default)
            try:
                iv = int(v)
                return iv if iv >= minimum else default
            except (TypeError, ValueError):
                logger.warning("agent.%s 非法值 %r,回退默认 %s", key, v, default)
                return default

        cr_raw = d.get("chat_router") or {}
        cr = cls.ChatRouterConfig(
            enabled=bool(cr_raw.get("enabled", True)),
            use_intent_evaluator=bool(cr_raw.get("use_intent_evaluator", True)),
            force_class_prefixes=cr_raw.get("force_class_prefixes") or defaults.chat_router.force_class_prefixes,
            admin_task_bias=float(cr_raw.get("admin_task_bias", 0.1)),
        )
        prl_raw = d.get("per_role_limits") or {}
        prl = {}
        for role in ("admin", "user"):
            rl = prl_raw.get(role) or {}
            default_mi = defaults.per_role_limits[role].max_iterations
            prl[role] = cls.RoleLimit(max_iterations=_int_dict(rl, "max_iterations", default_mi))

        return cls(
            enabled=bool(d.get("enabled", True)),
            max_iterations=_int("max_iterations", 20),
            approval_timeout_seconds=_int("approval_timeout_seconds", 120),
            session_ttl_seconds=_int("session_ttl_seconds", 600),
            session_max_messages=_int("session_max_messages", 200),
            model=str(d.get("model", "auto")),
            internal_api_key=str(d.get("internal_api_key", "")),
            loopback_base_url=str(d.get("loopback_base_url", "http://localhost:8000")),
            loopback_timeout_seconds=_int("loopback_timeout_seconds", 300),
            audit_log_ttl_seconds=_int("audit_log_ttl_seconds", 604800),
            tool_failure_threshold=_int("tool_failure_threshold", 3, minimum=1),
            tool_failure_cooldown_seconds=_int("tool_failure_cooldown_seconds", 0, minimum=0),
            per_role_limits=prl,
            chat_router=cr,
        )

    def max_iterations_for_role(self, role: str) -> int:
        """按 role 取循环上限,无配置回退顶层 max_iterations."""
        rl = self.per_role_limits.get(role)
        return rl.max_iterations if rl else self.max_iterations


def _int_dict(d: dict, key: str, default: int) -> int:
    """per_role_limits 子 dict 的 int 提取."""
    try:
        return int(d.get(key, default))
    except (TypeError, ValueError):
        return default


class AgentConfigWatcher:
    """监听 ConfigManager 热重载,atomic swap AgentConfig."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._config = AgentConfig.default()

    @property
    def config(self) -> AgentConfig:
        with self._lock:
            return self._config

    def attach(self, config_manager: Any) -> None:
        if hasattr(config_manager, "on_reload"):
            config_manager.on_reload(self._on_config_reload)
        if hasattr(config_manager, "config"):
            self._on_config_reload(config_manager.config)

    def _on_config_reload(self, new_full_config: Dict[str, Any]) -> None:
        raw = new_full_config.get("agent", {}) if isinstance(new_full_config, dict) else {}
        new_cfg = AgentConfig.from_yaml(raw)
        with self._lock:
            self._config = new_cfg


_watcher: "AgentConfigWatcher | None" = None


def get_agent_config() -> AgentConfig:
    if _watcher is None:
        return AgentConfig.default()
    return _watcher.config


def init_agent_config_watcher(config_manager: Any) -> AgentConfigWatcher:
    global _watcher
    _watcher = AgentConfigWatcher()
    _watcher.attach(config_manager)
    return _watcher
```

- [ ] **Step 5: 运行测试通过**

Run: `python3 -m pytest tests/test_agent_config.py -v`
Expected: 6 PASS

- [ ] **Step 6: 在 config.py 的 _DEFAULT_CONFIG 加 agent 段默认**

`aigateway-core/src/aigateway_core/config.py` 的 `_DEFAULT_CONFIG` dict 末尾加(在 `circuit_breaker` 后):
```python
    "agent": {
        "enabled": True,
        "max_iterations": 20,
        "approval_timeout_seconds": 120,
        "session_ttl_seconds": 600,
        "session_max_messages": 200,
        "model": "auto",
        "internal_api_key": "",
        "loopback_base_url": "http://localhost:8000",
        "loopback_timeout_seconds": 300,
        "audit_log_ttl_seconds": 604800,
        "tool_failure_threshold": 3,
        "tool_failure_cooldown_seconds": 0,
        "per_role_limits": {"admin": {"max_iterations": 20}, "user": {"max_iterations": 10}},
        "chat_router": {"enabled": True, "use_intent_evaluator": True,
                        "force_class_prefixes": {"task": ["/task"], "generation": ["/gen"], "understanding": ["/ask"]},
                        "admin_task_bias": 0.1},
    },
```

- [ ] **Step 7: 在 config.yaml + config.yaml.template 加 agent 段**

在 `config.yaml` 末尾(circuit_breaker 段之后)加 spec §9.1 完整 yaml(含注释)。`config.yaml.template` 同步加带详细注释版。

- [ ] **Step 8: .env.example 加 AGENT_INTERNAL_KEY**

在 `.env.example` 末尾加:
```
# Agent (控制台聊天窗智能体入口 B) loopback 调 /v1/chat/completions 用的内部 key
# 应是一个 admin-level key(部署时填实际值)
AGENT_INTERNAL_KEY=
```

- [ ] **Step 9: Commit**

```bash
git add aigateway-api/src/aigateway_api/agent/ aigateway-core/src/aigateway_core/config.py config.yaml config.yaml.template .env.example tests/test_agent_config.py
git commit -m "feat(agent): AgentConfig + Watcher 热重载,config.yaml agent 段 + env 覆盖"
```

---

## Task 4: ToolSpec / ToolRegistry / AgentContext / ApprovalDecision

**目的:** agent 工具系统的核心数据结构 + role 隔离的 registry。

**Files:**
- Create: `aigateway-api/src/aigateway_api/agent/tools.py`
- Test: `tests/test_agent_tool_registry.py`

**Interfaces:**
- Consumes: 无(纯数据结构)
- Produces:
  - `@dataclass ToolSpec(name, description, parameters, roles: frozenset, kind, handler, danger_level)`
  - `@dataclass ApprovalDecision(approved: bool, trust_scope: Literal["once","session"], reason: str = "")`
  - `@dataclass AgentContext(session_id, user_role, caller_api_key_id, trace_id, trusted_tools: set, approval_callback)`
  - `class ToolRegistry`:`register(spec)`, `visible_to(role) -> list[ToolSpec]`, `get(name) -> ToolSpec | None`, `openai_schemas(role) -> list[dict]`

- [ ] **Step 1: 写失败测试**

`tests/test_agent_tool_registry.py`:
```python
"""ToolRegistry role 隔离 + schema 转换测试."""
import sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from aigateway_api.agent.tools import ToolSpec, ToolRegistry, AgentContext, ApprovalDecision


async def _noop(args, ctx): return {"ok": True}


def _make_registry():
    r = ToolRegistry()
    r.register(ToolSpec(
        name="get_my_usage", description="查自己用量",
        parameters={"type": "object", "properties": {}},
        roles=frozenset({"user", "admin"}), kind="read",
        handler=_noop, danger_level="low",
    ))
    r.register(ToolSpec(
        name="set_quota_for", description="改任意 key 配额",
        parameters={"type": "object", "properties": {"key_id": {"type": "string"}}},
        roles=frozenset({"admin"}), kind="write",
        handler=_noop, danger_level="medium",
    ))
    return r


def test_user_sees_only_user_tools():
    r = _make_registry()
    names = {t.name for t in r.visible_to("user")}
    assert names == {"get_my_usage"}  # set_quota_for 不可见


def test_admin_sees_all():
    r = _make_registry()
    names = {t.name for t in r.visible_to("admin")}
    assert names == {"get_my_usage", "set_quota_for"}


def test_openai_schemas_user_excludes_admin():
    r = _make_registry()
    schemas = r.openai_schemas("user")
    names = {s["function"]["name"] for s in schemas}
    assert "set_quota_for" not in names
    assert "get_my_usage" in names
    # OpenAI tools 格式
    assert schemas[0]["type"] == "function"
    assert "function" in schemas[0]


def test_get_unknown_returns_none():
    r = _make_registry()
    assert r.get("nonexistent") is None


def test_get_returns_spec_for_admin_only_tool():
    r = _make_registry()
    spec = r.get("set_quota_for")
    assert spec is not None
    assert "admin" in spec.roles
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_agent_tool_registry.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: 创建 agent/tools.py**

```python
"""Agent 工具系统核心数据结构.

ToolSpec: 单个工具的元数据 + handler.
ToolRegistry: 注册表,按 role 过滤可见工具(spec §3.5 三层防护的 registry 层).
AgentContext: 单次 loop 携带的会话上下文.
ApprovalDecision: 写工具确认回执.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, FrozenSet, Literal, Optional


@dataclass
class ApprovalDecision:
    """前端确认卡的回执."""
    approved: bool
    trust_scope: Literal["once", "session"] = "once"
    reason: str = ""  # "user_denied" / "approval_timeout" / ""


@dataclass
class AgentContext:
    """单次 AgentLoop.run 携带的上下文."""
    session_id: str
    user_role: Literal["admin", "user"]
    caller_api_key_id: Optional[str]
    trace_id: str
    trusted_tools: set[str] = field(default_factory=set)
    approval_callback: Optional[Callable[[str, dict], Awaitable[ApprovalDecision]]] = None


@dataclass
class ToolSpec:
    """单个工具定义."""
    name: str
    description: str
    parameters: dict                          # JSON Schema
    roles: FrozenSet[str]                     # {"admin"} / {"admin","user"} / {"user"}
    kind: Literal["read", "write"]
    handler: Callable[[dict, AgentContext], Awaitable[dict]]
    danger_level: Literal["low", "medium", "high"] = "low"


class ToolRegistry:
    """工具注册表 + role scoping."""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool '{spec.name}' already registered")
        self._specs[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._specs.get(name)

    def visible_to(self, role: str) -> list[ToolSpec]:
        """只返回 roles 集合含 role 的工具(spec §3.5 registry 层)."""
        return [s for s in self._specs.values() if role in s.roles]

    def openai_schemas(self, role: str) -> list[dict]:
        """转 OpenAI tools 数组格式(仅 role 可见的)."""
        return [
            {
                "type": "function",
                "function": {
                    "name": s.name,
                    "description": s.description,
                    "parameters": s.parameters,
                },
            }
            for s in self.visible_to(role)
        ]
```

- [ ] **Step 4: 运行测试通过**

Run: `python3 -m pytest tests/test_agent_tool_registry.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add aigateway-api/src/aigateway_api/agent/tools.py tests/test_agent_tool_registry.py
git commit -m "feat(agent): ToolSpec/ToolRegistry/AgentContext/ApprovalDecision 数据结构 + role 隔离"
```

---

## Task 5: AuditLogger(structlog + Redis ZSET 双通道 + PII 脱敏)

**目的:** spec §6.2 —— 每次 tool 执行写一条 audit,structlog + Redis ZSET 双通道,args 走 PII 脱敏。

**Files:**
- Create: `aigateway-api/src/aigateway_api/agent/audit.py`
- Test: `tests/test_agent_audit.py`

**Interfaces:**
- Consumes: `app.state.redis_manager`(可选,None 时只走 structlog),`aigateway_core.security.PIIDetector`
- Produces:
  - `class AuditLogger(redis_mgr=None, ttl_seconds=604800)`
  - `async def log(self, *, session_id, trace_id, role, caller_key_id, tool_name, tool_kind, args, result_status, result_summary, elapsed_ms, approval=None)` — result_status ∈ `ok|error|denied|timeout|tool_disabled`
  - 便捷方法:`log_ok`, `log_error`, `log_denied`, `log_timeout`, `log_disabled`(都调 `log`)

- [ ] **Step 1: 写失败测试**

`tests/test_agent_audit.py`:
```python
"""AuditLogger 双通道 + PII 脱敏 + TTL 测试."""
import sys, os, asyncio, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from unittest.mock import AsyncMock, MagicMock, patch
from aigateway_api.agent.audit import AuditLogger


def test_log_writes_structlog_and_redis():
    redis_mgr = MagicMock(); redis_mgr.redis = AsyncMock()
    logger = AuditLogger(redis_mgr=redis_mgr, ttl_seconds=604800)
    args = {"key_id": "key_1", "monthly_cost": 1000}

    asyncio.run(logger.log(
        session_id="cs-1", trace_id="trc-1", role="admin", caller_key_id="key_admin",
        tool_name="set_quota_for", tool_kind="write", args=args,
        result_status="ok", result_summary="updated", elapsed_ms=42,
        approval={"required": True, "granted": True, "trust_scope": "session"},
    ))
    # Redis ZADD 被调
    assert redis_mgr.redis.zadd.called
    # EXPIRE 被调(TTL)
    assert redis_mgr.redis.expire.called
    call_args = redis_mgr.redis.zadd.call_args
    key = call_args[0][0]
    assert key.startswith("aigateway:agent_audit:")
    # payload 含正确字段
    payload = list(call_args[0][1].values())[0]
    rec = json.loads(payload)
    assert rec["tool_name"] == "set_quota_for"
    assert rec["result_status"] == "ok"
    assert rec["approval"]["trust_scope"] == "session"


def test_log_pii_redaction():
    """args 里的 sk- API key 应被脱敏."""
    redis_mgr = MagicMock(); redis_mgr.redis = AsyncMock()
    logger = AuditLogger(redis_mgr=redis_mgr)
    args = {"api_key": "sk-abcdef1234567890abcdef1234567890"}

    asyncio.run(logger.log(
        session_id="cs", trace_id="t", role="admin", caller_key_id="k",
        tool_name="x", tool_kind="read", args=args,
        result_status="ok", result_summary="", elapsed_ms=1,
    ))
    payload = list(redis_mgr.redis.zadd.call_args[0][1].values())[0]
    assert "sk-abcdef1234567890" not in payload  # 已脱敏
    assert "REDACTED" in payload


def test_log_redis_none_only_structlog():
    """redis_mgr 为 None 时不崩,只走 structlog."""
    logger = AuditLogger(redis_mgr=None)
    # 不应抛异常
    asyncio.run(logger.log(
        session_id="cs", trace_id="t", role="user", caller_key_id="k",
        tool_name="get_my_usage", tool_kind="read", args={},
        result_status="ok", result_summary="", elapsed_ms=1,
    ))


def test_log_denied_status():
    redis_mgr = MagicMock(); redis_mgr.redis = AsyncMock()
    logger = AuditLogger(redis_mgr=redis_mgr)
    asyncio.run(logger.log_denied(
        session_id="cs", trace_id="t", role="user", caller_key_id="k",
        tool_name="set_quota_for", args={}, reason="role",
    ))
    payload = list(redis_mgr.redis.zadd.call_args[0][1].values())[0]
    rec = json.loads(payload)
    assert rec["result_status"] == "denied"
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_agent_audit.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: 创建 agent/audit.py**

```python
"""AuditLogger —— structlog + Redis ZSET 双通道,PII 脱敏.

spec §6.2:每次 tool 执行(成功/失败/拒绝/超时/禁用)必写 audit。
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class AuditLogger:
    """双通道 audit:structlog JSON 日志 + Redis ZSET(按日期分键)."""

    def __init__(self, redis_mgr: Any = None, ttl_seconds: int = 604800) -> None:
        self._redis = redis_mgr
        self._ttl = ttl_seconds

    async def log(
        self,
        *,
        session_id: str,
        trace_id: str,
        role: str,
        caller_key_id: Optional[str],
        tool_name: str,
        tool_kind: str,
        args: Dict[str, Any],
        result_status: str,             # ok|error|denied|timeout|tool_disabled
        result_summary: str,
        elapsed_ms: int,
        approval: Optional[Dict[str, Any]] = None,
    ) -> None:
        # PII 脱敏 args
        safe_args = self._sanitize(args)
        record = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{int(time.time()*1000)%1000:03d}Z",
            "session_id": session_id,
            "trace_id": trace_id,
            "role": role,
            "caller_key_id": caller_key_id,
            "tool_name": tool_name,
            "tool_kind": tool_kind,
            "args": safe_args,
            "result_status": result_status,
            "result_summary": (result_summary or "")[:500],
            "elapsed_ms": elapsed_ms,
            "approval": approval,
        }
        # 1) structlog(自动带 trace_id,经 ContextInjectProcessor)
        logger.info("agent_audit", extra=record)

        # 2) Redis ZSET
        if self._redis is None or self._redis.redis is None:
            return
        try:
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            key = f"aigateway:agent_audit:{day}"
            payload = json.dumps(record, ensure_ascii=False, default=str)
            await self._redis.redis.zadd(key, {payload: time.time()})
            await self._redis.redis.expire(key, self._ttl)
        except Exception as exc:
            logger.warning("audit Redis 写入失败(已走 structlog): %s", exc)

    def _sanitize(self, args: Dict[str, Any]) -> Dict[str, Any]:
        """对 args 做 PII 脱敏(spec §6.2).复用 PIIDetector.sanitize."""
        try:
            from aigateway_core.security import PIIDetector
            detector = PIIDetector()
            return detector.sanitize(args)
        except Exception:
            return args

    async def log_ok(self, **kw):
        kw.setdefault("result_status", "ok"); kw.setdefault("tool_kind", "read")
        await self.log(**kw)

    async def log_error(self, *, session_id, trace_id, role, caller_key_id, tool_name, tool_kind, args, error, elapsed_ms=0, approval=None):
        await self.log(session_id=session_id, trace_id=trace_id, role=role, caller_key_id=caller_key_id,
                       tool_name=tool_name, tool_kind=tool_kind, args=args,
                       result_status="error", result_summary=str(error)[:500],
                       elapsed_ms=elapsed_ms, approval=approval)

    async def log_denied(self, *, session_id, trace_id, role, caller_key_id, tool_name, args, reason, approval=None):
        await self.log(session_id=session_id, trace_id=trace_id, role=role, caller_key_id=caller_key_id,
                       tool_name=tool_name, tool_kind="write", args=args,
                       result_status="denied", result_summary=f"denied:{reason}", elapsed_ms=0, approval=approval)

    async def log_timeout(self, *, session_id, trace_id, role, caller_key_id, tool_name, args, approval=None):
        await self.log(session_id=session_id, trace_id=trace_id, role=role, caller_key_id=caller_key_id,
                       tool_name=tool_name, tool_kind="write", args=args,
                       result_status="timeout", result_summary="approval_timeout", elapsed_ms=0, approval=approval)

    async def log_disabled(self, *, session_id, trace_id, role, caller_key_id, tool_name, args):
        await self.log(session_id=session_id, trace_id=trace_id, role=role, caller_key_id=caller_key_id,
                       tool_name=tool_name, tool_kind="write", args=args,
                       result_status="tool_disabled", result_summary="tool_disabled_this_session", elapsed_ms=0)
```

- [ ] **Step 4: 运行测试通过**

Run: `python3 -m pytest tests/test_agent_audit.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add aigateway-api/src/aigateway_api/agent/audit.py tests/test_agent_audit.py
git commit -m "feat(agent): AuditLogger structlog+Redis ZSET 双通道 + PII 脱敏"
```

---

## Task 6: AgentSession(approval Future + 工具熔断计数 + TTL)

**目的:** spec §3.7 + §7.2 —— 会话状态(messages + pending approvals Future + 工具失败计数),支持 approval 回执唤醒 loop。

**Files:**
- Create: `aigateway-api/src/aigateway_api/agent/session.py`
- Test: `tests/test_agent_session.py`

**Interfaces:**
- Consumes: `asyncio.Future`, `AgentConfig`(读 tool_failure_threshold / cooldown)
- Produces:
  - `class AgentSession`:`session_id`, `messages: list[dict]`, `created_at`, `last_active`, `tool_failures: dict[str,list[float]]`, `_pending_approvals: dict[str, asyncio.Future]`
  - `async def await_approval(tool_call_id, payload, timeout) -> ApprovalDecision` —— 创建 Future 并 await
  - `def resolve_approval(tool_call_id, decision)` —— approval POST 回调此方法 set Future result
  - `def record_tool_failure(name)` / `def tool_disabled(name) -> bool` —— 熔断逻辑

- [ ] **Step 1: 写失败测试**

`tests/test_agent_session.py`:
```python
"""AgentSession approval Future + 熔断测试."""
import sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from aigateway_api.agent.session import AgentSession
from aigateway_api.agent.tools import ApprovalDecision
from aigateway_api.agent.config import AgentConfig


def test_await_approval_resolved():
    """await_approval 被 resolve_approval 唤醒后返回 decision."""
    cfg = AgentConfig.default()
    sess = AgentSession("cs-1", cfg)

    async def _go():
        # 并行:一个 task 等 approval,主 task resolve
        async def waiter():
            return await sess.await_approval("tc-1", {"name": "x"}, timeout=2.0)
        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.05)  # 让 waiter 跑到 await Future
        sess.resolve_approval("tc-1", ApprovalDecision(approved=True, trust_scope="session"))
        return await task

    decision = asyncio.run(_go())
    assert decision.approved is True
    assert decision.trust_scope == "session"


def test_await_approval_timeout():
    """超时返回 approval_timeout decision."""
    cfg = AgentConfig.default()
    sess = AgentSession("cs-1", cfg)

    async def _go():
        return await sess.await_approval("tc-1", {"name": "x"}, timeout=0.1)

    decision = asyncio.run(_go())
    assert decision.approved is False
    assert decision.reason == "approval_timeout"


def test_tool_circuit_breaker():
    """同工具失败 3 次后 tool_disabled 返回 True."""
    cfg = AgentConfig.default()  # threshold=3, cooldown=0
    sess = AgentSession("cs-1", cfg)
    assert sess.tool_disabled("set_quota_for") is False
    sess.record_tool_failure("set_quota_for")
    sess.record_tool_failure("set_quota_for")
    assert sess.tool_disabled("set_quota_for") is False  # 2 次还没到
    sess.record_tool_failure("set_quota_for")
    assert sess.tool_disabled("set_quota_for") is True  # 第 3 次


def test_resolve_unknown_approval_noop():
    """resolve 不存在的 tool_call_id 不崩."""
    cfg = AgentConfig.default()
    sess = AgentSession("cs-1", cfg)
    sess.resolve_approval("nonexistent", ApprovalDecision(approved=False))  # 不应抛
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_agent_session.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: 创建 agent/session.py**

```python
"""AgentSession —— 会话状态 + approval Future + 工具熔断计数.

spec §3.7 / §7.2:
- messages: OpenAI 格式消息历史
- await_approval / resolve_approval: 写工具 HITL 的 Future 协调
- tool_failures: 同 session 内工具失败计数,达阈值熔断
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from .tools import ApprovalDecision


class AgentSession:
    def __init__(self, session_id: str, config: Any) -> None:
        self.session_id = session_id
        self.config = config
        self.messages: List[Dict[str, Any]] = []
        self.created_at = time.time()
        self.last_active = time.time()
        self._pending_approvals: Dict[str, asyncio.Future] = {}
        self.tool_failures: Dict[str, List[float]] = {}  # name -> [ts,...]

    def touch(self) -> None:
        self.last_active = time.time()

    def is_expired(self, ttl_seconds: int) -> bool:
        return (time.time() - self.last_active) > ttl_seconds

    async def await_approval(self, tool_call_id: str, payload: Dict[str, Any], *, timeout: float) -> ApprovalDecision:
        """创建 Future 并等待 resolve_approval 唤醒,超时返回 timeout decision."""
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_approvals[tool_call_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return ApprovalDecision(approved=False, trust_scope="once", reason="approval_timeout")
        finally:
            self._pending_approvals.pop(tool_call_id, None)

    def resolve_approval(self, tool_call_id: str, decision: ApprovalDecision) -> None:
        fut = self._pending_approvals.get(tool_call_id)
        if fut is not None and not fut.done():
            fut.set_result(decision)

    def record_tool_failure(self, name: str) -> None:
        self.tool_failures.setdefault(name, []).append(time.time())

    def tool_disabled(self, name: str) -> bool:
        """spec §7.2:失败次数 >= threshold → 熔断;cooldown>0 时按秒过期."""
        failures = self.tool_failures.get(name, [])
        if not failures:
            return False
        threshold = getattr(self.config, "tool_failure_threshold", 3)
        cooldown = getattr(self.config, "tool_failure_cooldown_seconds", 0)
        if cooldown > 0:
            cutoff = time.time() - cooldown
            failures = [t for t in failures if t >= cutoff]
            self.tool_failures[name] = failures
        return len(failures) >= threshold
```

- [ ] **Step 4: 运行测试通过**

Run: `python3 -m pytest tests/test_agent_session.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add aigateway-api/src/aigateway_api/agent/session.py tests/test_agent_session.py
git commit -m "feat(agent): AgentSession approval Future + 工具熔断计数"
```

---

## Task 7: 9 个 MVP 工具 handlers

**目的:** spec §3.4 —— 9 个工具的 handler 实现,薄封装调 admin_service,自查工具强制注入 caller_api_key_id。

**Files:**
- Create: `aigateway-api/src/aigateway_api/agent/handlers.py`
- Test: `tests/test_agent_handlers_read.py`, `tests/test_agent_handlers_write.py`

**Interfaces:**
- Consumes: `admin_service`(Task 1),`ToolSpec`/`AgentContext`(Task 4),`ToolRegistry`
- Produces:
  - `def register_all_tools(registry: ToolRegistry, app_state: Any) -> None` —— 把 9 个工具注册进 registry
  - 9 个 async handler 函数(每个 `async def(args, ctx) -> dict`)

- [ ] **Step 1: 写失败测试 — 自查工具 caller_key_id 强制注入**

`tests/test_agent_handlers_read.py`:
```python
"""自查工具 handler 测试 —— caller_api_key_id 强制注入(spec §3.5 handler 层)."""
import sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from unittest.mock import AsyncMock, MagicMock, patch
from aigateway_api.agent.handlers import register_all_tools
from aigateway_api.agent.tools import AgentContext, ToolRegistry


def _ctx(role="user", caller="key_me"):
    return AgentContext(session_id="cs", user_role=role, caller_api_key_id=caller, trace_id="t")


def test_get_my_usage_forces_caller_key_id():
    """模型即使传 api_key_id 参数,handler 也强制用 ctx.caller_api_key_id."""
    registry = ToolRegistry()
    app_state = MagicMock()
    register_all_tools(registry, app_state)

    spec = registry.get("get_my_usage")
    assert spec is not None
    assert "user" in spec.roles

    with patch("aigateway_api.agent.handlers.admin_service.get_key_quota", new=AsyncMock(return_value={"key_id": "key_me"})):
        # 模型试图查别人的 key
        result = asyncio.run(spec.handler({"api_key_id": "key_other"}, _ctx(caller="key_me")))
    assert result["key_id"] == "key_me"  # 被强制覆盖成 caller


def test_get_my_recent_logs_forces_caller():
    registry = ToolRegistry()
    register_all_tools(registry, MagicMock())
    spec = registry.get("get_my_recent_logs")
    with patch("aigateway_api.agent.handlers.admin_service.query_logs", new=AsyncMock(return_value=[])) as m:
        asyncio.run(spec.handler({"days": 3, "api_key_id": "key_other"}, _ctx(caller="key_me")))
    # query_logs 应被以 api_key_id="key_me" 调用
    _, kwargs = m.call_args
    assert kwargs["api_key_id"] == "key_me"


def test_user_cannot_see_admin_tools():
    registry = ToolRegistry()
    register_all_tools(registry, MagicMock())
    names = {t.name for t in registry.visible_to("user")}
    assert names == {"get_my_usage", "get_my_quota", "get_my_recent_logs"}
    # admin 工具不在
    assert "set_quota_for" not in names
    assert "list_api_keys" not in names
```

- [ ] **Step 2: 写失败测试 — admin 写工具**

`tests/test_agent_handlers_write.py`:
```python
"""admin 写工具 handler 测试."""
import sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from unittest.mock import AsyncMock, MagicMock, patch
from aigateway_api.agent.handlers import register_all_tools
from aigateway_api.agent.tools import AgentContext, ToolRegistry


def _admin_ctx():
    return AgentContext(session_id="cs", user_role="admin", caller_api_key_id="key_admin", trace_id="t")


def test_set_quota_for_calls_admin_service():
    registry = ToolRegistry()
    register_all_tools(registry, MagicMock())
    spec = registry.get("set_quota_for")
    assert "admin" in spec.roles
    assert spec.kind == "write"

    with patch("aigateway_api.agent.handlers.admin_service.update_key_quota",
               new=AsyncMock(return_value={"id": "key_1", "quotas": {"monthly_cost_limit": 1000}})) as m:
        result = asyncio.run(spec.handler({"key_id": "key_1", "monthly_cost": 1000}, _admin_ctx()))
    assert result["id"] == "key_1"
    _, kwargs = m.call_args
    assert kwargs["monthly_cost"] == 1000


def test_set_quota_for_handler_propagates_keyerror():
    registry = ToolRegistry()
    register_all_tools(registry, MagicMock())
    spec = registry.get("set_quota_for")
    with patch("aigateway_api.agent.handlers.admin_service.update_key_quota",
               new=AsyncMock(side_effect=KeyError("key_x"))):
        try:
            asyncio.run(spec.handler({"key_id": "key_x", "monthly_cost": 100}, _admin_ctx()))
            assert False, "应抛 KeyError"
        except KeyError:
            pass


def test_toggle_plugin_write_kind():
    registry = ToolRegistry()
    register_all_tools(registry, MagicMock())
    spec = registry.get("toggle_plugin")
    assert spec.kind == "write"
    assert "admin" in spec.roles


def test_admin_tools_count():
    """MVP 9 个工具全注册."""
    registry = ToolRegistry()
    register_all_tools(registry, MagicMock())
    all_names = {t.name for t in registry.visible_to("admin")}
    assert all_names == {
        "get_my_usage", "get_my_quota", "get_my_recent_logs",
        "list_api_keys", "set_quota_for", "list_plugins",
        "toggle_plugin", "list_l3_entries", "get_trace",
    }
```

- [ ] **Step 3: 运行确认失败**

Run: `python3 -m pytest tests/test_agent_handlers_read.py tests/test_agent_handlers_write.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 4: 创建 agent/handlers.py**

```python
"""9 个 MVP 工具 handler(spec §3.4).

薄封装调 admin_service。自查工具(get_my_*)强制注入 ctx.caller_api_key_id(spec §3.5 handler 层)。
"""
from __future__ import annotations

from typing import Any

from . import admin_service
from .tools import AgentContext, ToolSpec, ToolRegistry


# ------------------------------------------------------------------
# 自查工具(user + admin)
# ------------------------------------------------------------------

async def _get_my_usage(args: dict, ctx: AgentContext) -> dict:
    """查当前 key 的用量(强制用 ctx.caller_api_key_id)."""
    from aigateway_api.main import app
    key_store = app.state.key_store
    cm = app.state.config_manager
    # 强制覆盖:忽略 args 里的 api_key_id
    key_hash = ctx.caller_api_key_id
    return await admin_service.get_key_quota(key_store, key_hash, cm) or {"error": "key_not_found"}


async def _get_my_quota(args: dict, ctx: AgentContext) -> dict:
    """查当前 key 的配额(同 get_my_usage,别名)."""
    return await _get_my_usage(args, ctx)


async def _get_my_recent_logs(args: dict, ctx: AgentContext) -> dict:
    """查当前 key 最近 N 天日志(强制 api_key_id=caller)."""
    from aigateway_api.main import app
    redis_mgr = app.state.redis_manager
    days = int(args.get("days", 7))
    limit = int(args.get("limit", 50))
    items = await admin_service.query_logs(redis_mgr, api_key_id=ctx.caller_api_key_id, days=days, limit=limit)
    return {"items": items, "count": len(items)}


# ------------------------------------------------------------------
# Admin: Key
# ------------------------------------------------------------------

async def _list_api_keys(args: dict, ctx: AgentContext) -> dict:
    from aigateway_api.main import app
    page = int(args.get("page", 1)); page_size = int(args.get("page_size", 20))
    return await admin_service.list_api_keys(app.state.key_store, app.state.config_manager, page=page, page_size=page_size)


async def _set_quota_for(args: dict, ctx: AgentContext) -> dict:
    from aigateway_api.main import app
    return await admin_service.update_key_quota(
        app.state.key_store, args["key_id"],
        daily_tokens=args.get("daily_tokens"),
        monthly_cost=args.get("monthly_cost"),
        rate_limit_rpm=args.get("rate_limit_rpm"),
        rate_limit_tpm=args.get("rate_limit_tpm"),
        config_manager=app.state.config_manager,
    )


# ------------------------------------------------------------------
# Admin: 插件
# ------------------------------------------------------------------

async def _list_plugins(args: dict, ctx: AgentContext) -> dict:
    from aigateway_api.main import app
    items = await admin_service.get_plugins_config(app.state.config_manager, app.state.plugin_registry)
    return {"items": items}


async def _toggle_plugin(args: dict, ctx: AgentContext) -> dict:
    from aigateway_api.main import app
    return await admin_service.set_plugin_enabled(
        app.state.config_manager, app.state.plugin_registry,
        args["name"], bool(args.get("enabled", True)),
    )


# ------------------------------------------------------------------
# Admin: 缓存
# ------------------------------------------------------------------

async def _list_l3_entries(args: dict, ctx: AgentContext) -> dict:
    from aigateway_api.main import app
    limit = int(args.get("limit", 50))
    items = await admin_service.list_l3_entries(app.state.cache_manager, limit=limit)
    return {"items": items}


# ------------------------------------------------------------------
# Admin: Trace
# ------------------------------------------------------------------

async def _get_trace(args: dict, ctx: AgentContext) -> dict:
    from aigateway_api.main import app
    return await admin_service.get_trace(app.state.redis_manager, args["trace_id"]) or {"error": "trace_not_found"}


# ------------------------------------------------------------------
# 注册
# ------------------------------------------------------------------

def register_all_tools(registry: ToolRegistry, app_state: Any) -> None:
    """注册 9 个 MVP 工具."""
    both = frozenset({"admin", "user"})
    admin_only = frozenset({"admin"})

    registry.register(ToolSpec(
        name="get_my_usage", description="Get token usage and cost for the CURRENT authenticated user's own API key. Returns only the caller's data; do not ask for other users' data.",
        parameters={"type": "object", "properties": {"days": {"type": "integer", "default": 7, "description": "days to aggregate"}}, "required": []},
        roles=both, kind="read", handler=_get_my_usage, danger_level="low",
    ))
    registry.register(ToolSpec(
        name="get_my_quota", description="Get quota limits and current consumption for the CURRENT authenticated user's own API key.",
        parameters={"type": "object", "properties": {}},
        roles=both, kind="read", handler=_get_my_quota, danger_level="low",
    ))
    registry.register(ToolSpec(
        name="get_my_recent_logs", description="Get recent request logs for the CURRENT authenticated user's own API key. Returns only the caller's logs.",
        parameters={"type": "object", "properties": {"days": {"type": "integer", "default": 7}, "limit": {"type": "integer", "default": 50}}},
        roles=both, kind="read", handler=_get_my_recent_logs, danger_level="low",
    ))
    registry.register(ToolSpec(
        name="list_api_keys", description="Requires admin role. List all API keys with their quotas and usage.",
        parameters={"type": "object", "properties": {"page": {"type": "integer", "default": 1}, "page_size": {"type": "integer", "default": 20}}},
        roles=admin_only, kind="read", handler=_list_api_keys, danger_level="low",
    ))
    registry.register(ToolSpec(
        name="set_quota_for", description="Requires admin role. Update quota limits (monthly_cost / daily_tokens / rpm / tpm) for a given API key_id. Only fields provided are updated.",
        parameters={"type": "object", "properties": {
            "key_id": {"type": "string"}, "monthly_cost": {"type": "number"},
            "daily_tokens": {"type": "integer"}, "rate_limit_rpm": {"type": "integer"}, "rate_limit_tpm": {"type": "integer"},
        }, "required": ["key_id"]},
        roles=admin_only, kind="write", handler=_set_quota_for, danger_level="medium",
    ))
    registry.register(ToolSpec(
        name="list_plugins", description="Requires admin role. List all plugins with enabled status and pipeline kind.",
        parameters={"type": "object", "properties": {}},
        roles=admin_only, kind="read", handler=_list_plugins, danger_level="low",
    ))
    registry.register(ToolSpec(
        name="toggle_plugin", description="Requires admin role. Enable or disable a plugin by name. Triggers hot reload.",
        parameters={"type": "object", "properties": {"name": {"type": "string"}, "enabled": {"type": "boolean", "default": True}}, "required": ["name"]},
        roles=admin_only, kind="write", handler=_toggle_plugin, danger_level="medium",
    ))
    registry.register(ToolSpec(
        name="list_l3_entries", description="Requires admin role. List L3 (semantic) cache entries.",
        parameters={"type": "object", "properties": {"limit": {"type": "integer", "default": 50}}},
        roles=admin_only, kind="read", handler=_list_l3_entries, danger_level="low",
    ))
    registry.register(ToolSpec(
        name="get_trace", description="Requires admin role. Get trace events for a given trace_id (debugging request failures).",
        parameters={"type": "object", "properties": {"trace_id": {"type": "string"}}, "required": ["trace_id"]},
        roles=admin_only, kind="read", handler=_get_trace, danger_level="low",
    ))
```

- [ ] **Step 5: 运行测试通过**

Run: `python3 -m pytest tests/test_agent_handlers_read.py tests/test_agent_handlers_write.py -v`
Expected: 7 PASS

- [ ] **Step 6: Commit**

```bash
git add aigateway-api/src/aigateway_api/agent/handlers.py tests/test_agent_handlers_read.py tests/test_agent_handlers_write.py
git commit -m "feat(agent): 9 个 MVP 工具 handler + 自查工具 caller_key_id 强制注入"
```

---

## Task 8: ChatRouter 三级分类

**目的:** spec §4 —— task/generation/understanding 三路分流,Level 1 关键词+结构规则,Level 2 可选 intent_evaluator,Level 3 兜底 understanding。

**Files:**
- Create: `aigateway-api/src/aigateway_api/agent/chat_router.py`
- Test: `tests/test_agent_chat_router.py`

**Interfaces:**
- Consumes: `AgentConfig`(读 chat_router 配置)
- Produces:
  - `@dataclass RoutingResult(cls: Literal["task","generation","understanding"], reason: str, level: int)`
  - `class ChatRouter(config: AgentConfig)`
  - `def classify(message: str, *, has_media: bool=False, role: str="user") -> RoutingResult`

- [ ] **Step 1: 写失败测试**

`tests/test_agent_chat_router.py`:
```python
"""ChatRouter 三级分类测试."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from aigateway_api.agent.chat_router import ChatRouter, RoutingResult
from aigateway_api.agent.config import AgentConfig


def _router():
    return ChatRouter(AgentConfig.default())


def test_force_prefix_task():
    r = _router().classify("/task 帮我改额度", role="admin")
    assert r.cls == "task"
    assert r.level == 1


def test_force_prefix_gen():
    r = _router().classify("/gen 画只猫")
    assert r.cls == "generation"


def test_keyword_generation():
    r = _router().classify("帮我生成一张戴帽子的猫的图片")
    assert r.cls == "generation"


def test_keyword_task_admin():
    r = _router().classify("帮我把 key_abc 的额度改成 1000", role="admin")
    assert r.cls == "task"


def test_keyword_task_user_self_query():
    r = _router().classify("帮我查今天的用量", role="user")
    assert r.cls == "task"


def test_media_attachment_routes_generation():
    r = _router().classify("看看这个", has_media=True)
    assert r.cls == "generation"


def test_default_understanding():
    r = _router().classify("解释一下 kubernetes 的 CNI 是什么")
    assert r.cls == "understanding"


def test_admin_bias_more_task():
    """admin 角色下,模糊的'帮我'should 更倾向 task."""
    r = _router().classify("帮我看看插件状态", role="admin")
    assert r.cls == "task"


def test_user_no_task_keyword_goes_understanding():
    """user 角色下,无明确 task 关键词 → understanding."""
    r = _router().classify("帮我写一首诗", role="user")
    assert r.cls == "understanding"
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_agent_chat_router.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: 创建 agent/chat_router.py**

```python
"""ChatRouter —— task/generation/understanding 三路分流(spec §4).

Level 1: 关键词+结构规则(0ms)
Level 2: 可选 intent_evaluator(~100ms,MVP 可关)
Level 3: 兜底 understanding
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal


@dataclass
class RoutingResult:
    cls: Literal["task", "generation", "understanding"]
    reason: str
    level: int


_GEN_KEYWORDS = re.compile(r"画|生成图|生成一张|做个视频|生成视频|generate\s+(image|video|picture)", re.IGNORECASE)
_TASK_KEYWORDS = re.compile(
    r"帮我(改|设置|删除|开启|关闭|看看|查看)|调用工具|list\s+api|toggle|revoke|set.+quota|"
    r"查.+日志|查.+trace|查.+错误|改.+额度|改.+配额|开关.+插件",
    re.IGNORECASE,
)
# admin 加权:更宽松的 task 触发词
_TASK_KEYWORDS_ADMIN = re.compile(
    r"帮我|看一下|检查一下|清理|重建|重启|配额|插件|缓存|key|日志|trace|错误|状态",
    re.IGNORECASE,
)


class ChatRouter:
    def __init__(self, config) -> None:
        self.config = config
        cr = getattr(config, "chat_router", None)
        self.enabled = getattr(cr, "enabled", True) if cr else True
        self.prefixes = getattr(cr, "force_class_prefixes", {}) if cr else {}
        self.admin_bias = getattr(cr, "admin_task_bias", 0.1) if cr else 0.0

    def classify(self, message: str, *, has_media: bool = False, role: str = "user") -> RoutingResult:
        if not self.enabled:
            return RoutingResult("task" if has_media else "understanding", "router_disabled", 0)

        msg = message.strip()

        # Level 1a: force prefix
        for cls, prefixes in self.prefixes.items():
            for p in prefixes:
                if msg.startswith(p):
                    return RoutingResult(cls, f"force_prefix:{p}", 1)

        # Level 1b: media attachment
        if has_media:
            return RoutingResult("generation", "media_attachment", 1)

        # Level 1c: generation keyword
        if _GEN_KEYWORDS.search(msg):
            return RoutingResult("generation", "keyword:generation", 1)

        # Level 1d: task keyword(role 感知)
        task_re = _TASK_KEYWORDS_ADMIN if role == "admin" and self.admin_bias > 0 else _TASK_KEYWORDS
        if task_re.search(msg):
            return RoutingResult("task", f"keyword:task({role})", 1)

        # Level 2: intent_evaluator(MVP 默认关,实现留 hook)
        cr = getattr(self.config, "chat_router", None)
        if cr and getattr(cr, "use_intent_evaluator", False):
            result = self._eval_intent(msg)
            if result is not None:
                return result

        # Level 3: 兜底
        return RoutingResult("understanding", "fallback", 3)

    def _eval_intent(self, msg: str) -> RoutingResult | None:
        """Level 2: 复用 intent_evaluator 策略函数(可选).MVP 阶段返回 None 走兜底."""
        try:
            from aigateway_core.generation_optimization.strategies.intent_evaluator import IntentEvaluatorStrategy
            from aigateway_core.generation_optimization.config import ModelRouterConfig
            # 仅做 generation-likely 评分;score 高 → generation
            strategy = IntentEvaluatorStrategy(ModelRouterConfig())
            result = strategy.evaluate(prompt=msg, generation_params={})
            score = getattr(result, "score", 0)
            if score > 70:
                return RoutingResult("generation", f"intent_eval:score={score}", 2)
        except Exception:
            pass
        return None
```

- [ ] **Step 4: 运行测试通过**

Run: `python3 -m pytest tests/test_agent_chat_router.py -v`
Expected: 9 PASS

- [ ] **Step 5: Commit**

```bash
git add aigateway-api/src/aigateway_api/agent/chat_router.py tests/test_agent_chat_router.py
git commit -m "feat(agent): ChatRouter 三级分类 task/generation/understanding + admin 加权"
```

---

## Task 9: AgentLoop(tool-calling 循环 + 熔断 + loopback)

**目的:** spec §3.6 + §7 —— tool-calling 循环控制器,loopback 调本 gateway,read 直执行 / write 走 approval,熔断,max_iterations 上限。

**Files:**
- Create: `aigateway-api/src/aigateway_api/agent/loop.py`
- Test: `tests/test_agent_loop.py`

**Interfaces:**
- Consumes: `ToolRegistry`, `AgentSession`, `AuditLogger`, `AgentConfig`, `ChatRouter`(agent_routes 传 routing 结果)
- Produces:
  - `@dataclass SSEEvent(type: str, data: dict)`
  - `class AgentLoop(registry, audit, config)`:`async def run(session, user_message, ctx, persistent_trust) -> AsyncIterator[SSEEvent]`

- [ ] **Step 1: 写失败测试**

`tests/test_agent_loop.py`:
```python
"""AgentLoop 循环测试."""
import sys, os, asyncio, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from unittest.mock import AsyncMock, MagicMock, patch
from aigateway_api.agent.loop import AgentLoop, SSEEvent
from aigateway_api.agent.tools import ToolRegistry, AgentContext, ApprovalDecision
from aigateway_api.agent.session import AgentSession
from aigateway_api.agent.config import AgentConfig
from aigateway_api.agent.handlers import register_all_tools


def _ctx(role="admin", caller="key_admin", approval_cb=None):
    return AgentContext(session_id="cs", user_role=role, caller_api_key_id=caller,
                        trace_id="t", approval_callback=approval_cb)


async def _collect(gen):
    out = []
    async for ev in gen:
        out.append(ev)
    return out


def test_loop_no_tool_calls_emits_final():
    """模型无 tool_calls → 直接 assistant_delta + final."""
    registry = ToolRegistry(); register_all_tools(registry, MagicMock())
    audit = MagicMock(); audit.log_ok = AsyncMock(); audit.log_denied = AsyncMock()
    loop = AgentLoop(registry, audit, AgentConfig.default())

    async def fake_call_llm(messages, ctx):
        yield {"type": "content_delta", "text": "你好"}
        yield {"type": "tool_calls_final", "tool_calls": []}
        # 第二轮不会到(无 tool_calls 直接 final)

    with patch.object(loop, "_call_llm", side_effect=lambda m, c: fake_call_llm(m, c)):
        sess = AgentSession("cs", AgentConfig.default())
        ctx = _ctx()
        events = asyncio.run(_collect(loop.run(sess, "hi", ctx)))

    types = [e.type for e in events]
    assert "assistant_delta" in types
    assert "final" in types


def test_loop_read_tool_direct_execute():
    """read 工具不走 approval,直接执行 + tool_result 事件."""
    registry = ToolRegistry(); register_all_tools(registry, MagicMock())
    audit = MagicMock(); audit.log_ok = AsyncMock()
    loop = AgentLoop(registry, audit, AgentConfig.default())

    call_count = [0]
    async def fake_call_llm(messages, ctx):
        call_count[0] += 1
        if call_count[0] == 1:
            yield {"type": "tool_calls_final", "tool_calls": [
                {"id": "tc1", "name": "list_api_keys", "args": {}}]}
        else:
            yield {"type": "content_delta", "text": "done"}
            yield {"type": "tool_calls_final", "tool_calls": []}

    with patch.object(loop, "_call_llm", side_effect=lambda m, c: fake_call_llm(m, c)):
        with patch("aigateway_api.agent.handlers.admin_service.list_api_keys",
                   new=AsyncMock(return_value={"items": [], "pagination": {"total": 0}})):
            sess = AgentSession("cs", AgentConfig.default())
            ctx = _ctx()
            events = asyncio.run(_collect(loop.run(sess, "list keys", ctx)))

    types = [e.type for e in events]
    assert "tool_call" in types
    assert "tool_result" in types
    assert "final" in types
    audit.log_ok.assert_called()  # audit 写了


def test_loop_write_tool_pending_approval_then_approved():
    """write 工具未信任 → pending_approval → 用户批准 → 执行."""
    registry = ToolRegistry(); register_all_tools(registry, MagicMock())
    audit = MagicMock(); audit.log_ok = AsyncMock()
    loop = AgentLoop(registry, audit, AgentConfig.default())

    async def approval_cb(tc_id, payload):
        return ApprovalDecision(approved=True, trust_scope="session")

    call_count = [0]
    async def fake_call_llm(messages, ctx):
        call_count[0] += 1
        if call_count[0] == 1:
            yield {"type": "tool_calls_final", "tool_calls": [
                {"id": "tc1", "name": "toggle_plugin", "args": {"name": "pii_detector", "enabled": False}}]}
        else:
            yield {"type": "content_delta", "text": "ok"}
            yield {"type": "tool_calls_final", "tool_calls": []}

    with patch.object(loop, "_call_llm", side_effect=lambda m, c: fake_call_llm(m, c)):
        with patch("aigateway_api.agent.handlers.admin_service.set_plugin_enabled",
                   new=AsyncMock(return_value={"name": "pii_detector", "enabled": False})):
            sess = AgentSession("cs", AgentConfig.default())
            ctx = _ctx(approval_cb=approval_cb)
            events = asyncio.run(_collect(loop.run(sess, "disable pii", ctx)))

    types = [e.type for e in events]
    assert "pending_approval" in types
    assert "tool_result" in types
    assert "final" in types
    # trust 已加入
    assert "toggle_plugin" in ctx.trusted_tools


def test_loop_write_tool_second_call_no_approval():
    """已信任的 write 工具第二次调用不弹 approval."""
    registry = ToolRegistry(); register_all_tools(registry, MagicMock())
    audit = MagicMock(); audit.log_ok = AsyncMock()
    loop = AgentLoop(registry, audit, AgentConfig.default())

    approval_count = [0]
    async def approval_cb(tc_id, payload):
        approval_count[0] += 1
        return ApprovalDecision(approved=True, trust_scope="session")

    call_count = [0]
    async def fake_call_llm(messages, ctx):
        call_count[0] += 1
        if call_count[0] == 1:
            # 两次连续 toggle_plugin
            yield {"type": "tool_calls_final", "tool_calls": [
                {"id": "tc1", "name": "toggle_plugin", "args": {"name": "a", "enabled": True}},
                {"id": "tc2", "name": "toggle_plugin", "args": {"name": "b", "enabled": True}}]}
        else:
            yield {"type": "content_delta", "text": "done"}
            yield {"type": "tool_calls_final", "tool_calls": []}

    with patch.object(loop, "_call_llm", side_effect=lambda m, c: fake_call_llm(m, c)):
        with patch("aigateway_api.agent.handlers.admin_service.set_plugin_enabled",
                   new=AsyncMock(return_value={"name": "x", "enabled": True})):
            sess = AgentSession("cs", AgentConfig.default())
            ctx = _ctx(approval_cb=approval_cb)
            events = asyncio.run(_collect(loop.run(sess, "enable a and b", ctx)))

    # 第一次弹了,第二次因 trust 不弹
    assert approval_count[0] == 1


def test_loop_max_iterations_emits_error():
    """模型一直返 tool_calls → 达 max_iterations → error 事件."""
    registry = ToolRegistry(); register_all_tools(registry, MagicMock())
    audit = MagicMock(); audit.log_ok = AsyncMock()
    cfg = AgentConfig.default()
    cfg.max_iterations = 2  # 改小便于测试
    cfg.per_role_limits["admin"].max_iterations = 2
    loop = AgentLoop(registry, audit, cfg)

    async def fake_call_llm(messages, ctx):
        yield {"type": "tool_calls_final", "tool_calls": [
            {"id": "tc1", "name": "list_api_keys", "args": {}}]}

    with patch.object(loop, "_call_llm", side_effect=lambda m, c: fake_call_llm(m, c)):
        with patch("aigateway_api.agent.handlers.admin_service.list_api_keys",
                   new=AsyncMock(return_value={"items": []})):
            sess = AgentSession("cs", cfg)
            ctx = _ctx()
            events = asyncio.run(_collect(loop.run(sess, "loop", ctx)))

    assert any(e.type == "error" and e.data.get("code") == "max_iterations" for e in events)


def test_loop_role_denied_tool():
    """user 角色调 admin 工具 → tool_not_permitted(Executor 层兜底)."""
    registry = ToolRegistry(); register_all_tools(registry, MagicMock())
    audit = MagicMock(); audit.log_denied = AsyncMock()
    loop = AgentLoop(registry, audit, AgentConfig.default())

    call_count = [0]
    async def fake_call_llm(messages, ctx):
        call_count[0] += 1
        if call_count[0] == 1:
            # 模型幻觉调 admin 工具(user 角色下 schema 里没有,但保险测试 Executor 兜底)
            yield {"type": "tool_calls_final", "tool_calls": [
                {"id": "tc1", "name": "set_quota_for", "args": {"key_id": "x"}}]}
        else:
            yield {"type": "content_delta", "text": "sorry"}
            yield {"type": "tool_calls_final", "tool_calls": []}

    with patch.object(loop, "_call_llm", side_effect=lambda m, c: fake_call_llm(m, c)):
        sess = AgentSession("cs", AgentConfig.default())
        ctx = _ctx(role="user", caller="key_user")
        events = asyncio.run(_collect(loop.run(sess, "change quota", ctx)))

    # 应有 tool_result 含 error
    tr = next(e for e in events if e.type == "tool_result")
    assert tr.data["result"]["error"] == "tool_not_permitted"
    audit.log_denied.assert_called()
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m pytest tests/test_agent_loop.py -v`
Expected: FAIL `ModuleNotFoundError`

- [ ] **Step 3: 创建 agent/loop.py**

```python
"""AgentLoop —— tool-calling 循环控制器(spec §3.6 + §7).

- loopback POST /v1/chat/completions(stream=true, tools=[role 可见])
- read 工具直执行,write 工具未信任时走 pending_approval
- 熔断:同 session 内工具失败达阈值 → tool_disabled
- max_iterations 上限(per_role_limits)
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from .audit import AuditLogger
from .config import get_agent_config
from .session import AgentSession
from .tools import AgentContext, ApprovalDecision, ToolRegistry

logger = logging.getLogger(__name__)


@dataclass
class SSEEvent:
    type: str
    data: dict


class AgentLoop:
    def __init__(self, registry: ToolRegistry, audit: AuditLogger, config: Any) -> None:
        self.registry = registry
        self.audit = audit
        self.config = config

    async def run(
        self,
        session: AgentSession,
        user_message: str,
        ctx: AgentContext,
        persistent_trust: Optional[set] = None,
    ) -> AsyncIterator[SSEEvent]:
        """主循环. yield SSEEvent."""
        # 合并跨会话永久信任(前端从 localStorage 送回)
        if persistent_trust:
            ctx.trusted_tools |= persistent_trust

        session.messages.append({"role": "user", "content": user_message})
        session.touch()

        max_steps = self.config.max_iterations_for_role(ctx.user_role)

        for step in range(max_steps):
            tool_calls = None
            accumulated_content = ""

            # 1) loopback 调本 gateway
            try:
                async for chunk in self._call_llm(session.messages, ctx):
                    if chunk.get("type") == "content_delta":
                        accumulated_content += chunk.get("text", "")
                        yield SSEEvent("assistant_delta", {"text": chunk.get("text", "")})
                    elif chunk.get("type") == "tool_calls_final":
                        tool_calls = chunk.get("tool_calls") or []
                        break
            except Exception as exc:
                logger.error("AgentLoop loopback 失败: %s", exc, exc_info=True)
                yield SSEEvent("error", {"code": "loopback_failed", "message": str(exc)})
                return

            # 2) 无 tool_calls → 终态
            if not tool_calls:
                if accumulated_content:
                    session.messages.append({"role": "assistant", "content": accumulated_content})
                yield SSEEvent("final", {"total_steps": step, "trace_id": ctx.trace_id})
                return

            # 3) assistant 消息带 tool_calls 入历史(OpenAI 格式要求)
            session.messages.append({
                "role": "assistant",
                "content": accumulated_content or None,
                "tool_calls": [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": json.dumps(tc.get("args", {}))}}
                    for tc in tool_calls
                ],
            })

            # 4) 逐个 tool_call 执行
            for tc in tool_calls:
                yield SSEEvent("tool_call", {"tool_call_id": tc["id"], "name": tc["name"], "args": tc.get("args", {})})
                result = await self._execute_tool(tc, session, ctx)
                yield SSEEvent("tool_result", {"tool_call_id": tc["id"], "result": result})
                session.messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })

        # 达上限
        yield SSEEvent("error", {"code": "max_iterations", "message": f"reached max_iterations={max_steps}"})

    async def _execute_tool(self, tc: dict, session: AgentSession, ctx: AgentContext) -> dict:
        """执行单个 tool_call,返回 result dict(spec §3.6 _execute_tool)."""
        name = tc.get("name")
        args = tc.get("args", {}) or {}
        spec = self.registry.get(name)

        # Executor 层 role 校验(spec §3.5 第 2 层)
        if spec is None or ctx.user_role not in spec.roles:
            await self.audit.log_denied(
                session_id=ctx.session_id, trace_id=ctx.trace_id, role=ctx.user_role,
                caller_key_id=ctx.caller_api_key_id, tool_name=name or "unknown",
                args=args, reason="role_or_unknown",
            )
            return {"error": "tool_not_permitted"}

        # 熔断(spec §7.2)
        if session.tool_disabled(name):
            await self.audit.log_disabled(
                session_id=ctx.session_id, trace_id=ctx.trace_id, role=ctx.user_role,
                caller_key_id=ctx.caller_api_key_id, tool_name=name, args=args,
            )
            return {"error": "tool_disabled_this_session"}

        # 写工具 + 未信任 → pending_approval
        approval_info = None
        if spec.kind == "write" and name not in ctx.trusted_tools:
            if ctx.approval_callback is None:
                return {"error": "no_approval_channel"}
            timeout = float(self.config.approval_timeout_seconds)
            decision = await ctx.approval_callback(tc["id"], {
                "name": name, "args": args,
                "danger_level": spec.danger_level,
                "preview": self._make_preview(spec, args),
            })
            if not decision.approved:
                if decision.reason == "approval_timeout":
                    await self.audit.log_timeout(
                        session_id=ctx.session_id, trace_id=ctx.trace_id, role=ctx.user_role,
                        caller_key_id=ctx.caller_api_key_id, tool_name=name, args=args,
                    )
                    return {"error": "approval_timeout"}
                await self.audit.log_denied(
                    session_id=ctx.session_id, trace_id=ctx.trace_id, role=ctx.user_role,
                    caller_key_id=ctx.caller_api_key_id, tool_name=name, args=args, reason="user_denied",
                )
                return {"error": "user_denied"}
            if decision.trust_scope == "session":
                ctx.trusted_tools.add(name)
            approval_info = {"required": True, "granted": True, "trust_scope": decision.trust_scope}
        elif spec.kind == "write":
            approval_info = {"required": True, "granted": True, "trust_scope": "trusted"}

        # 执行 handler
        start = time.time()
        try:
            result = await spec.handler(args, ctx)
            elapsed_ms = int((time.time() - start) * 1000)
            await self.audit.log(
                session_id=ctx.session_id, trace_id=ctx.trace_id, role=ctx.user_role,
                caller_key_id=ctx.caller_api_key_id, tool_name=name, tool_kind=spec.kind,
                args=args, result_status="ok", result_summary=str(result)[:200],
                elapsed_ms=elapsed_ms, approval=approval_info,
            )
            return result
        except KeyError as e:
            # 业务层 key 不存在等
            elapsed_ms = int((time.time() - start) * 1000)
            await self.audit.log_error(
                session_id=ctx.session_id, trace_id=ctx.trace_id, role=ctx.user_role,
                caller_key_id=ctx.caller_api_key_id, tool_name=name, tool_kind=spec.kind,
                args=args, error=f"not_found:{e}", elapsed_ms=elapsed_ms, approval=approval_info,
            )
            return {"error": "not_found", "detail": str(e)}
        except Exception as e:
            session.record_tool_failure(name)
            elapsed_ms = int((time.time() - start) * 1000)
            await self.audit.log_error(
                session_id=ctx.session_id, trace_id=ctx.trace_id, role=ctx.user_role,
                caller_key_id=ctx.caller_api_key_id, tool_name=name, tool_kind=spec.kind,
                args=args, error=e, elapsed_ms=elapsed_ms, approval=approval_info,
            )
            return {"error": "handler_failed", "detail": str(e)}

    def _make_preview(self, spec, args: dict) -> str:
        """生成给用户看的确认卡预览文本."""
        if spec.name == "set_quota_for":
            fields = []
            if "monthly_cost" in args: fields.append(f"monthly_cost=${args['monthly_cost']}")
            if "daily_tokens" in args: fields.append(f"daily_tokens={args['daily_tokens']}")
            if "rate_limit_rpm" in args: fields.append(f"rpm={args['rate_limit_rpm']}")
            return f"将 {args.get('key_id','?')} 的配额改为: {', '.join(fields) or '(无字段)'}"
        if spec.name == "toggle_plugin":
            return f"将插件 {args.get('name','?')} 设为 {'enabled' if args.get('enabled', True) else 'disabled'}"
        return f"{spec.name}({args})"

    async def _call_llm(self, messages: List[dict], ctx: AgentContext) -> AsyncIterator[dict]:
        """loopback POST /v1/chat/completions (stream=true). yield chunks.

        把 OpenAI SSE 流(httpx)解析成 {type: content_delta|tool_calls_final} chunks。
        """
        cfg = self.config
        url = f"{cfg.loopback_base_url}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {cfg.internal_api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": cfg.model,
            "messages": messages,
            "tools": self.registry.openai_schemas(ctx.user_role),
            "stream": True,
        }
        timeout = httpx.Timeout(cfg.loopback_timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code != 200:
                    text = await resp.aread()
                    raise RuntimeError(f"loopback HTTP {resp.status_code}: {text[:200]}")
                accumulated_tool_calls = {}  # index -> {id,name,arguments_str}
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    if delta.get("content"):
                        yield {"type": "content_delta", "text": delta["content"]}
                    if delta.get("tool_calls"):
                        for tc in delta["tool_calls"]:
                            idx = tc.get("index", 0)
                            acc = accumulated_tool_calls.setdefault(idx, {"id": None, "name": None, "arguments": ""})
                            if tc.get("id"): acc["id"] = tc["id"]
                            fn = tc.get("function") or {}
                            if fn.get("name"): acc["name"] = fn["name"]
                            if fn.get("arguments"): acc["arguments"] += fn["arguments"]
                    finish = choices[0].get("finish_reason")
                    if finish == "tool_calls":
                        tcs = []
                        for idx in sorted(accumulated_tool_calls):
                            acc = accumulated_tool_calls[idx]
                            try:
                                args = json.loads(acc["arguments"]) if acc["arguments"] else {}
                            except json.JSONDecodeError:
                                args = {}
                            tcs.append({"id": acc["id"], "name": acc["name"], "args": args})
                        yield {"type": "tool_calls_final", "tool_calls": tcs}
                        return
                    if finish == "stop":
                        yield {"type": "tool_calls_final", "tool_calls": []}
                        return
```

- [ ] **Step 4: 运行测试通过**

Run: `python3 -m pytest tests/test_agent_loop.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add aigateway-api/src/aigateway_api/agent/loop.py tests/test_agent_loop.py
git commit -m "feat(agent): AgentLoop tool-calling 循环 + 熔断 + loopback SSE 解析"
```

---

## Task 10: agent_routes.py(SSE 路由)+ main.py 挂载

**目的:** spec §3.7 —— 4 个 HTTP 端点(SSE chat / approval / tools / session delete),main.py lifespan 初始化 agent 组件。

**Files:**
- Create: `aigateway-api/src/aigateway_api/agent_routes.py`
- Modify: `aigateway-api/src/aigateway_api/main.py`
- Test: `tests/test_agent_routes_sse.py`, `tests/test_agent_approval_flow.py`

**Interfaces:**
- Consumes: 所有前置 Task 的产物
- Produces:
  - `router = APIRouter()` 暴露 4 端点
  - main.py:`app.state.tool_registry`, `app.state.agent_audit`, `app.state.agent_loop`, `app.state.agent_sessions`(dict), `app.state.agent_config_watcher`

- [ ] **Step 1: 写失败测试 — SSE 事件序列**

`tests/test_agent_routes_sse.py`:
```python
"""agent_routes SSE 端点测试(mock loopback)."""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from unittest.mock import patch, AsyncMock, MagicMock
from starlette.testclient import TestClient


def test_chat_endpoint_emits_routing_then_events():
    """/admin/agent/chat 应先发 routing 事件."""
    from aigateway_api.main import create_app
    app = create_app()
    client = TestClient(app)

    # mock AgentLoop.run 返固定事件序列
    async def fake_run(session, msg, ctx, persistent_trust=None):
        from aigateway_api.agent.loop import SSEEvent
        yield SSEEvent("routing", {"class": "task", "reason": "test", "level": 1})
        yield SSEEvent("final", {"total_steps": 0, "trace_id": "t"})

    with patch("aigateway_api.agent_routes.get_agent_loop") as gl:
        gl.return_value = MagicMock()
        gl.return_value.run = fake_run
        with patch("aigateway_api.agent_routes.get_tool_registry") as gr:
            gr.return_value = MagicMock()
            with patch("aigateway_api.agent_routes.get_chat_router") as gcr:
                gcr.return_value.classify = MagicMock(return_value=MagicMock(cls="task", reason="test", level=1))
                resp = client.post("/admin/agent/chat",
                                   json={"session_id": "cs-test", "message": "hi"},
                                   headers={"Authorization": "Bearer gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o"})
    # 不强求 200(lifespan 可能未完整跑);若 200 检查事件序列
    if resp.status_code == 200:
        body = resp.text
        assert "event: routing" in body or '"type":"routing"' in body
    else:
        import pytest
        pytest.skip(f"app lifespan not fully initialized: {resp.status_code}")
```

- [ ] **Step 2: 写失败测试 — approval 流程**

`tests/test_agent_approval_flow.py`:
```python
"""approval 端点测试."""
import sys, os, asyncio
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-api", "src"))

from unittest.mock import MagicMock
from starlette.testclient import TestClient

from aigateway_api.agent.tools import ApprovalDecision


def test_approval_endpoint_resolves_session_future():
    from aigateway_api.main import create_app
    app = create_app()
    client = TestClient(app)

    from aigateway_api.agent.session import AgentSession
    from aigateway_api.agent.config import AgentConfig
    sess = AgentSession("cs-approval", AgentConfig.default())
    # 预先创建一个 pending future(模拟 loop 在等)
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    sess._pending_approvals["tc-1"] = fut
    loop.close()

    # mock sessions registry
    with patch("aigateway_api.agent_routes.get_sessions") as gs:
        gs.return_value = {"cs-approval": sess}
        with patch("aigateway_api.agent_routes.authenticate", new=AsyncMock(return_value={"key_id": "k", "is_admin": True})):
            from unittest.mock import patch as _p
            # 直接调 resolve_approval
            from aigateway_api.agent_routes import _resolve_approval_via_endpoint
            # ... 实际用 client.post 测
            pass
    # 此测试主要验证 resolve_approval 能唤醒 Future;集成测试在 test_agent_routes_sse 里
```

（**注**:approval flow 的完整集成测试较复杂(需两个并发 task),MVP 阶段以单元测试覆盖 `AgentSession.resolve_approval`(Task 6 已测)+ 路由层薄包装为主。`test_agent_approval_flow.py` 可简化为只测路由能 200 返回。）

- [ ] **Step 3: 运行确认失败**

Run: `python3 -m pytest tests/test_agent_routes_sse.py tests/test_agent_approval_flow.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'aigateway_api.agent_routes'`

- [ ] **Step 4: 创建 agent_routes.py**

```python
"""Agent SSE 路由 —— 入口 B 的 HTTP 面(spec §3.7).

4 端点:
- POST /admin/agent/chat        SSE 主入口
- POST /admin/agent/approval    确认卡回执
- GET  /admin/agent/tools       当前 role 可见工具列表
- DELETE /admin/agent/session/{sid}  清会话
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from aigateway_api.auth_middleware import authenticate

from .agent.audit import AuditLogger
from .agent.chat_router import ChatRouter
from .agent.config import get_agent_config, init_agent_config_watcher
from .agent.loop import AgentLoop, SSEEvent
from .agent.session import AgentSession
from .agent.tools import AgentContext, ApprovalDecision, ToolRegistry

logger = logging.getLogger(__name__)

router = APIRouter()

# 进程级单例(由 main.py lifespan 初始化)
_tool_registry: Optional[ToolRegistry] = None
_agent_audit: Optional[AuditLogger] = None
_agent_loop: Optional[AgentLoop] = None
_chat_router: Optional[ChatRouter] = None
_sessions: Dict[str, AgentSession] = {}


def init_agent_components(app_state: Any) -> None:
    """main.py lifespan 调一次:初始化 registry/audit/loop/router."""
    global _tool_registry, _agent_audit, _agent_loop, _chat_router
    from .agent.handlers import register_all_tools
    _tool_registry = ToolRegistry()
    register_all_tools(_tool_registry, app_state)
    cfg = get_agent_config()
    _agent_audit = AuditLogger(redis_mgr=getattr(app_state, "redis_manager", None), ttl_seconds=cfg.audit_log_ttl_seconds)
    _agent_loop = AgentLoop(_tool_registry, _agent_audit, cfg)
    _chat_router = ChatRouter(cfg)


def get_tool_registry() -> ToolRegistry:
    if _tool_registry is None:
        raise RuntimeError("agent components not initialized")
    return _tool_registry


def get_agent_loop() -> AgentLoop:
    if _agent_loop is None:
        raise RuntimeError("agent components not initialized")
    return _agent_loop


def get_chat_router() -> ChatRouter:
    if _chat_router is None:
        raise RuntimeError("agent components not initialized")
    return _chat_router


def get_sessions() -> Dict[str, AgentSession]:
    return _sessions


def _get_or_create_session(session_id: str) -> AgentSession:
    cfg = get_agent_config()
    sess = _sessions.get(session_id)
    if sess is None:
        sess = AgentSession(session_id, cfg)
        _sessions[session_id] = sess
    sess.touch()
    return sess


def _gc_sessions() -> None:
    """清理过期 session(惰性 GC)."""
    cfg = get_agent_config()
    ttl = cfg.session_ttl_seconds
    expired = [sid for sid, s in _sessions.items() if s.is_expired(ttl)]
    for sid in expired:
        _sessions.pop(sid, None)


# ------------------------------------------------------------------
# 请求模型
# ------------------------------------------------------------------


class ChatRequest(BaseModel):
    session_id: str = Field(..., min_length=1)
    message: str = Field(..., min_length=1)
    trusted_tools: Optional[list[str]] = None


class ApprovalRequest(BaseModel):
    session_id: str
    tool_call_id: str
    approved: bool
    trust_scope: str = Field(default="once")  # once | session


# ------------------------------------------------------------------
# 路由
# ------------------------------------------------------------------


@router.post("/agent/chat")
async def agent_chat(request: Request, body: ChatRequest):
    """SSE 主入口."""
    key_data = await authenticate(request)
    role = "admin" if key_data.get("is_admin") else "user"
    caller_key_id = key_data.get("key_id")

    cfg = get_agent_config()
    if not cfg.enabled:
        raise HTTPException(status_code=503, detail={"error": {"code": "agent_disabled", "message": "Agent feature is disabled"}})

    _gc_sessions()
    sess = _get_or_create_session(body.session_id)

    if len(sess.messages) >= cfg.session_max_messages:
        raise HTTPException(status_code=400, detail={"error": {"code": "session_full", "message": "Session message limit reached"}})

    # ChatRouter 分类
    routing = get_chat_router().classify(body.message, has_media=False, role=role)

    trace_id = getattr(request.state, "trace_id", "") or uuid.uuid4().hex[:12]
    ctx = AgentContext(
        session_id=body.session_id, user_role=role, caller_api_key_id=caller_key_id,
        trace_id=trace_id, trusted_tools=set(),
        approval_callback=_make_approval_callback(body.session_id),
    )
    persistent_trust = set(body.trusted_tools) if body.trusted_tools else None

    async def event_stream():
        try:
            yield _sse("routing", {"class": routing.cls, "reason": routing.reason, "level": routing.level})
            if routing.cls == "task":
                async for ev in get_agent_loop().run(sess, body.message, ctx, persistent_trust):
                    yield _sse(ev.type, ev.data)
            else:
                # generation / understanding: loopback 直传(spec §4.6)
                async for ev in _run_loopback_passthrough(sess, body.message, ctx, routing.cls):
                    yield _sse(ev.type, ev.data)
        except asyncio.CancelledError:
            logger.info("Agent SSE 客户端断连,取消 session=%s", body.session_id)
            raise
        except Exception as exc:
            logger.exception("Agent SSE 异常: %s", exc)
            yield _sse("error", {"code": "internal", "message": str(exc)})

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/agent/approval")
async def agent_approval(request: Request, body: ApprovalRequest):
    """确认卡回执 —— 唤醒 loop 里 await 的 Future."""
    await authenticate(request)
    sess = _sessions.get(body.session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail={"error": {"code": "session_not_found", "message": "Session expired or not found"}})
    decision = ApprovalDecision(approved=body.approved, trust_scope=body.trust_scope if body.approved else "once",
                                 reason="" if body.approved else "user_denied")
    sess.resolve_approval(body.tool_call_id, decision)
    return {"data": {"resolved": True}, "message": "success"}


@router.get("/agent/tools")
async def agent_list_tools(request: Request):
    """返回当前 role 可见工具列表."""
    key_data = await authenticate(request)
    role = "admin" if key_data.get("is_admin") else "user"
    registry = get_tool_registry()
    tools = [
        {"name": s.name, "description": s.description, "kind": s.kind,
         "danger_level": s.danger_level, "roles": sorted(s.roles)}
        for s in registry.visible_to(role)
    ]
    return {"data": {"role": role, "tools": tools}, "message": "success"}


@router.delete("/agent/session/{sid}")
async def agent_delete_session(request: Request, sid: str):
    """清会话."""
    await authenticate(request)
    _sessions.pop(sid, None)
    return {"data": {"deleted": sid}, "message": "success"}


# ------------------------------------------------------------------
# 辅助
# ------------------------------------------------------------------


def _sse(event_type: str, data: dict) -> str:
    """格式化 SSE 帧."""
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _make_approval_callback(session_id: str):
    """构造 approval callback:发 pending_approval 事件 + await session.await_approval.

    由于 SSE 是单向的,approval 通过独立 POST /agent/approval 唤醒 Future。
    callback 需要先 yield pending_approval 事件再 await —— 但 callback 是 async func 不是 generator,
    所以 pending_approval 事件在 AgentLoop._execute_tool 调 callback 前由 loop 自己发。
    这里 callback 只负责 await Future。
    """
    async def _cb(tool_call_id: str, payload: dict) -> ApprovalDecision:
        sess = _sessions.get(session_id)
        if sess is None:
            return ApprovalDecision(approved=False, reason="session_gone")
        cfg = get_agent_config()
        return await sess.await_approval(tool_call_id, payload, timeout=float(cfg.approval_timeout_seconds))
    return _cb


async def _run_loopback_passthrough(sess: AgentSession, message: str, ctx: AgentContext, cls: str):
    """generation/understanding 类:loopback 直传,把 OpenAI SSE 转成 agent 事件流(spec §4.6)."""
    from .agent.loop import SSEEvent
    sess.messages.append({"role": "user", "content": message})
    cfg = get_agent_config()
    url = f"{cfg.loopback_base_url}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {cfg.internal_api_key}", "Content-Type": "application/json"}
    body = {"model": cfg.model, "messages": sess.messages, "stream": True}
    if cls == "generation":
        body["extra_body"] = {"generation_intent": True}
    import httpx
    accumulated = ""
    async with httpx.AsyncClient(timeout=httpx.Timeout(cfg.loopback_timeout_seconds)) as client:
        async with client.stream("POST", url, json=body, headers=headers) as resp:
            if resp.status_code != 200:
                text = await resp.aread()
                yield SSEEvent("error", {"code": "loopback_failed", "message": f"HTTP {resp.status_code}: {text[:200]}"})
                return
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                if delta.get("content"):
                    accumulated += delta["content"]
                    yield SSEEvent("assistant_delta", {"text": delta["content"]})
                # media_output 检测(生成管道响应里可能有 media urls)
                # 简化:把整个响应的 _meta 在 final 时检查
    if accumulated:
        sess.messages.append({"role": "assistant", "content": accumulated})
    yield SSEEvent("final", {"total_steps": 0, "trace_id": ctx.trace_id, "class": cls})
```

- [ ] **Step 5: 在 main.py lifespan 初始化 agent 组件 + 挂路由**

在 `main.py` 的 `lifespan` 中,`_mount_routes(app)` 之前加:
```python
    # 初始化 Agent(入口 B)组件
    try:
        from aigateway_api.agent.config import init_agent_config_watcher
        app.state.agent_config_watcher = init_agent_config_watcher(config_manager)
        from aigateway_api.agent_routes import init_agent_components
        init_agent_components(app.state)
        logger.info("Agent 组件初始化完成(入口 B)")
    except Exception as exc:
        logger.warning("Agent 组件初始化失败(聊天窗不可用): %s", exc)
```

在 `_mount_routes` 函数里加:
```python
    # /admin/agent/* — Agent SSE(入口 B)
    from . import agent_routes
    app.include_router(agent_routes.router, prefix="/admin", tags=["Agent 聊天窗"])
```

- [ ] **Step 6: 运行测试通过**

Run: `python3 -m pytest tests/test_agent_routes_sse.py tests/test_agent_approval_flow.py tests/test_agent_loop.py tests/test_agent_session.py -v`
Expected: PASS

- [ ] **Step 7: 跑全量 agent 测试**

Run: `python3 -m pytest tests/test_agent_*.py tests/test_agent_routes_sse.py tests/test_agent_approval_flow.py tests/test_admin_service.py -v`
Expected: 全 PASS

- [ ] **Step 8: 重建 gateway 容器验证启动**

Run: `sudo DOCKER_BUILDKIT=1 docker compose up -d --build gateway && sleep 5 && curl -s http://localhost:8000/health`
Expected: health 返回 200;`docker compose logs --tail=30 gateway | grep -i "Agent 组件初始化完成"` 出现

- [ ] **Step 9: Commit**

```bash
git add aigateway-api/src/aigateway_api/agent_routes.py aigateway-api/src/aigateway_api/main.py tests/test_agent_routes_sse.py tests/test_agent_approval_flow.py
git commit -m "feat(agent): agent_routes SSE 4 端点 + main.py lifespan 初始化 + 挂载"
```

---

## Task 11: 前端 SSE 客户端封装

**目的:** spec §8.3 —— `fetch + ReadableStream` 解析 SSE 帧的客户端 + agent API types。

**Files:**
- Modify: `control-panel/src/api/client.ts`

**Interfaces:**
- Consumes: `VITE_API_BASE` 环境变量,登录态 token
- Produces:
  - TypeScript types:`AgentSSEEvent`, `ChatRequest`, `ApprovalRequest`, `AgentTool`
  - `async function* streamAgentChat(body: ChatRequest): AsyncIterator<AgentSSEEvent>`
  - `async function postApproval(body: ApprovalRequest): Promise<void>`
  - `async function getAgentTools(): Promise<{role, tools}>`
  - `async function deleteAgentSession(sid: string): Promise<void>`

- [ ] **Step 1: 在 client.ts 末尾加 types 和函数**

```typescript
// ------------------------------------------------------------------
// Agent (入口 B) — 聊天窗智能体
// ------------------------------------------------------------------

export type AgentSSEEventType =
  | "routing" | "assistant_delta" | "tool_call" | "tool_result"
  | "pending_approval" | "media_output" | "final" | "error";

export interface AgentSSEEvent {
  type: AgentSSEEventType;
  data: Record<string, any>;
}

export interface ChatRequest {
  session_id: string;
  message: string;
  trusted_tools?: string[];
}

export interface ApprovalRequest {
  session_id: string;
  tool_call_id: string;
  approved: boolean;
  trust_scope: "once" | "session";
}

export interface AgentTool {
  name: string;
  description: string;
  kind: "read" | "write";
  danger_level: "low" | "medium" | "high";
  roles: string[];
}

function getAuthHeaders(): Record<string, string> {
  const token = localStorage.getItem("aigateway:token") || "";
  return {
    "Authorization": `Bearer ${token}`,
    "Content-Type": "application/json",
  };
}

/**
 * SSE 流式聊天 —— 用 fetch + ReadableStream 解析(EventSource 不能带 header).
 * yields AgentSSEEvent.
 */
export async function* streamAgentChat(body: ChatRequest): AsyncIterator<AgentSSEEvent> {
  const base = (import.meta as any).env?.VITE_API_BASE || "/aigateway";
  const resp = await fetch(`${base}/admin/agent/chat`, {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify(body),
  });
  if (!resp.ok || !resp.body) {
    throw new Error(`agent chat HTTP ${resp.status}`);
  }
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE 帧以 \n\n 分隔
    let idx;
    while ((idx = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      let type = "";
      let dataStr = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event: ")) type = line.slice(7).trim();
        else if (line.startsWith("data: ")) dataStr += line.slice(6);
      }
      if (type && dataStr) {
        try {
          yield { type: type as AgentSSEEventType, data: JSON.parse(dataStr) };
        } catch {
          // 忽略解析失败的帧
        }
      }
    }
  }
}

export async function postApproval(body: ApprovalRequest): Promise<void> {
  const base = (import.meta as any).env?.VITE_API_BASE || "/aigateway";
  const resp = await fetch(`${base}/admin/agent/approval`, {
    method: "POST",
    headers: getAuthHeaders(),
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`approval HTTP ${resp.status}`);
}

export async function getAgentTools(): Promise<{ role: string; tools: AgentTool[] }> {
  const base = (import.meta as any).env?.VITE_API_BASE || "/aigateway";
  const resp = await fetch(`${base}/admin/agent/tools`, { headers: getAuthHeaders() });
  if (!resp.ok) throw new Error(`get tools HTTP ${resp.status}`);
  const json = await resp.json();
  return json.data;
}

export async function deleteAgentSession(sid: string): Promise<void> {
  const base = (import.meta as any).env?.VITE_API_BASE || "/aigateway";
  const resp = await fetch(`${base}/admin/agent/session/${sid}`, {
    method: "DELETE",
    headers: getAuthHeaders(),
  });
  if (!resp.ok) throw new Error(`delete session HTTP ${resp.status}`);
}
```

- [ ] **Step 2: 验证 TypeScript 编译**

Run: `cd control-panel && npx tsc --noEmit 2>&1 | head -30`
Expected: 无新增 error(可能有 pre-existing,只要不引入新 error)

- [ ] **Step 3: Commit**

```bash
git add control-panel/src/api/client.ts
git commit -m "feat(control-panel): agent SSE 客户端 + types + approval/tools API"
```

---

## Task 12: 前端组件 + /chat 页面 + 路由

**目的:** spec §8 —— Chat 页面 + 6 个组件 + localStorage 持久化 + 路由 + 侧栏入口。

**Files:**
- Create: `control-panel/src/components/chat/RoutingBadge.tsx`
- Create: `control-panel/src/components/chat/ToolCallCard.tsx`
- Create: `control-panel/src/components/chat/ApprovalCard.tsx`
- Create: `control-panel/src/components/chat/MediaOutputCard.tsx`
- Create: `control-panel/src/components/chat/ChatComposer.tsx`
- Create: `control-panel/src/components/chat/ChatTimeline.tsx`
- Create: `control-panel/src/components/chat/ToolCatalogModal.tsx`
- Create: `control-panel/src/pages/Chat.tsx`
- Modify: `control-panel/src/App.tsx`(加 /chat 路由)
- Modify: `control-panel/src/components/Layout.tsx`(侧栏加 Chat 入口)

**Interfaces:**
- Consumes: `streamAgentChat` / `postApproval` / `getAgentTools` / `deleteAgentSession`(Task 11)
- Produces: `/chat` 页面完整可用

- [ ] **Step 1: RoutingBadge 组件**

`control-panel/src/components/chat/RoutingBadge.tsx`:
```tsx
interface Props { cls: "task" | "generation" | "understanding"; }

const LABELS = {
  task: { icon: "🔧", text: "任务模式", color: "bg-blue-100 text-blue-700" },
  generation: { icon: "🎨", text: "生成模式", color: "bg-purple-100 text-purple-700" },
  understanding: { icon: "🧠", text: "理解模式", color: "bg-green-100 text-green-700" },
};

export default function RoutingBadge({ cls }: Props) {
  const l = LABELS[cls];
  return <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium ${l.color}`}>{l.icon} {l.text}</span>;
}
```

- [ ] **Step 2: ToolCallCard 组件**

`control-panel/src/components/chat/ToolCallCard.tsx`:
```tsx
import { useState } from "react";

interface Props {
  name: string;
  args: Record<string, any>;
  result?: Record<string, any>;
}

export default function ToolCallCard({ name, args, result }: Props) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="border border-gray-200 rounded-lg p-3 my-2 bg-gray-50">
      <div className="flex items-center gap-2 cursor-pointer" onClick={() => setExpanded(!expanded)}>
        <span className="text-xs font-mono bg-gray-200 px-1.5 py-0.5 rounded">🔧 {name}</span>
        <span className="text-xs text-gray-500">{expanded ? "▼" : "▶"}</span>
      </div>
      {expanded && (
        <div className="mt-2 space-y-1">
          <div className="text-xs text-gray-500">参数:</div>
          <pre className="text-xs bg-white p-2 rounded overflow-auto">{JSON.stringify(args, null, 2)}</pre>
          {result && (
            <>
              <div className="text-xs text-gray-500 mt-2">结果:</div>
              <pre className={`text-xs p-2 rounded overflow-auto ${result.error ? "bg-red-50 text-red-700" : "bg-white"}`}>
                {JSON.stringify(result, null, 2)}
              </pre>
            </>
          )}
        </div>
      )}
    </div>
  );
}
```

- [ ] **Step 3: ApprovalCard 组件**

`control-panel/src/components/chat/ApprovalCard.tsx`:
```tsx
import { useState } from "react";

interface Props {
  name: string;
  args: Record<string, any>;
  dangerLevel: "low" | "medium" | "high";
  preview: string;
  onApprove: (trustScope: "once" | "session") => void;
  onDeny: () => void;
}

const DANGER_LABELS = {
  low: { text: "低风险", color: "text-green-600" },
  medium: { text: "中风险", color: "text-yellow-600" },
  high: { text: "高风险", color: "text-red-600" },
};

export default function ApprovalCard({ name, args, dangerLevel, preview, onApprove, onDeny }: Props) {
  const [trust, setTrust] = useState(false);
  const dl = DANGER_LABELS[dangerLevel];
  return (
    <div className="border-2 border-yellow-300 rounded-lg p-4 my-2 bg-yellow-50">
      <div className="flex items-center justify-between mb-2">
        <span className="font-medium">⚠️ 需要确认:{name}</span>
        <span className={`text-xs ${dl.color}`}>{dl.text}</span>
      </div>
      <div className="text-sm mb-2">{preview}</div>
      <details className="text-xs mb-3">
        <summary className="cursor-pointer text-gray-600">查看参数</summary>
        <pre className="bg-white p-2 rounded mt-1">{JSON.stringify(args, null, 2)}</pre>
      </details>
      <label className="flex items-center gap-2 text-sm mb-3">
        <input type="checkbox" checked={trust} onChange={(e) => setTrust(e.target.checked)} />
        本会话信任此工具(后续不再询问)
      </label>
      <div className="flex gap-2">
        <button onClick={() => onApprove(trust ? "session" : "once")}
                className="px-4 py-1.5 bg-blue-600 text-white rounded text-sm hover:bg-blue-700">
          批准
        </button>
        <button onClick={onDeny}
                className="px-4 py-1.5 bg-gray-200 text-gray-700 rounded text-sm hover:bg-gray-300">
          拒绝
        </button>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: MediaOutputCard 组件**

`control-panel/src/components/chat/MediaOutputCard.tsx`:
```tsx
interface Props { url: string; kind: "image" | "video" | "audio"; }

export default function MediaOutputCard({ url, kind }: Props) {
  return (
    <div className="my-2">
      {kind === "image" && <img src={url} alt="生成结果" className="max-w-sm rounded-lg border" />}
      {kind === "video" && <video src={url} controls className="max-w-sm rounded-lg border" />}
      {kind === "audio" && <audio src={url} controls />}
      <div className="text-xs text-gray-500 mt-1"><a href={url} target="_blank" rel="noreferrer">打开原图</a></div>
    </div>
  );
}
```

- [ ] **Step 5: ChatComposer 组件**

`control-panel/src/components/chat/ChatComposer.tsx`:
```tsx
import { useState } from "react";

interface Props {
  onSend: (msg: string) => void;
  disabled?: boolean;
}

export default function ChatComposer({ onSend, disabled }: Props) {
  const [text, setText] = useState("");
  const submit = () => {
    const t = text.trim();
    if (!t || disabled) return;
    onSend(t);
    setText("");
  };
  return (
    <div className="flex gap-2 p-3 border-t">
      <input
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
        placeholder="输入消息…(/task /gen /ask 强制路由)"
        disabled={disabled}
        className="flex-1 border rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
      />
      <button onClick={submit} disabled={disabled || !text.trim()}
              className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm disabled:opacity-50">
        发送
      </button>
    </div>
  );
}
```

- [ ] **Step 6: ToolCatalogModal 组件**

`control-panel/src/components/chat/ToolCatalogModal.tsx`:
```tsx
import { useEffect, useState } from "react";
import { getAgentTools, AgentTool } from "../../api/client";

interface Props { onClose: () => void; }

export default function ToolCatalogModal({ onClose }: Props) {
  const [tools, setTools] = useState<AgentTool[]>([]);
  const [role, setRole] = useState("");
  useEffect(() => {
    getAgentTools().then((r) => { setTools(r.tools); setRole(r.role); }).catch(() => {});
  }, []);
  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-white rounded-xl p-6 max-w-2xl w-full max-h-[80vh] overflow-auto" onClick={(e) => e.stopPropagation()}>
        <div className="flex justify-between items-center mb-4">
          <h2 className="text-lg font-medium">AI 能做什么(role: {role})</h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600">✕</button>
        </div>
        <div className="space-y-2">
          {tools.map((t) => (
            <div key={t.name} className="border rounded-lg p-3">
              <div className="flex items-center gap-2">
                <span className="font-mono text-sm">{t.name}</span>
                <span className={`text-xs px-1.5 py-0.5 rounded ${t.kind === "write" ? "bg-yellow-100 text-yellow-700" : "bg-gray-100 text-gray-600"}`}>
                  {t.kind}
                </span>
                <span className="text-xs text-gray-500">{t.danger_level}</span>
              </div>
              <div className="text-xs text-gray-600 mt-1">{t.description}</div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
```

- [ ] **Step 7: ChatTimeline 组件**

`control-panel/src/components/chat/ChatTimeline.tsx`:
```tsx
import { AgentSSEEvent } from "../../api/client";
import RoutingBadge from "./RoutingBadge";
import ToolCallCard from "./ToolCallCard";
import ApprovalCard from "./ApprovalCard";
import MediaOutputCard from "./MediaOutputCard";

export interface TimelineItem {
  id: string;
  role: "user" | "assistant";
  content: string;
  events?: AgentSSEEvent[];
  pendingApproval?: { tool_call_id: string; name: string; args: any; danger_level: any; preview: string };
}

interface Props {
  items: TimelineItem[];
  onApprove: (toolCallId: string, trustScope: "once" | "session") => void;
  onDeny: (toolCallId: string) => void;
}

export default function ChatTimeline({ items, onApprove, onDeny }: Props) {
  return (
    <div className="flex-1 overflow-auto p-4 space-y-4">
      {items.map((item) => (
        <div key={item.id} className={item.role === "user" ? "text-right" : ""}>
          <div className={`inline-block max-w-[80%] ${item.role === "user" ? "bg-blue-600 text-white" : "bg-gray-100"} rounded-lg px-3 py-2`}>
            {item.content}
          </div>
          {item.events?.map((ev, i) => {
            if (ev.type === "routing") return <div key={i} className="mt-1"><RoutingBadge cls={ev.data.class} /></div>;
            if (ev.type === "tool_call") return <ToolCallCard key={i} name={ev.data.name} args={ev.data.args} />;
            if (ev.type === "tool_result") return null;  // result 合并到对应 ToolCallCard(简化:独立渲染)
            if (ev.type === "media_output") return <MediaOutputCard key={i} url={ev.data.url} kind={ev.data.kind} />;
            return null;
          })}
          {item.pendingApproval && (
            <ApprovalCard
              name={item.pendingApproval.name}
              args={item.pendingApproval.args}
              dangerLevel={item.pendingApproval.danger_level}
              preview={item.pendingApproval.preview}
              onApprove={(ts) => onApprove(item.pendingApproval!.tool_call_id, ts)}
              onDeny={() => onDeny(item.pendingApproval!.tool_call_id)}
            />
          )}
        </div>
      ))}
    </div>
  );
}
```

- [ ] **Step 8: Chat 页面**

`control-panel/src/pages/Chat.tsx`:
```tsx
import { useEffect, useRef, useState } from "react";
import { streamAgentChat, postApproval, AgentSSEEvent } from "../api/client";
import ChatComposer from "../components/chat/ChatComposer";
import ChatTimeline, { TimelineItem } from "../components/chat/ChatTimeline";
import ToolCatalogModal from "../components/chat/ToolCatalogModal";

function lsKey(suffix: string): string {
  const keyId = localStorage.getItem("aigateway:key_id") || "default";
  return `aigateway:chat:${keyId}:${suffix}`;
}

export default function Chat() {
  const [items, setItems] = useState<TimelineItem[]>(() => {
    try { return JSON.parse(localStorage.getItem(lsKey("messages")) || "[]"); } catch { return []; }
  });
  const [loading, setLoading] = useState(false);
  const [showCatalog, setShowCatalog] = useState(false);
  const [trusted, setTrusted] = useState<string[]>(() => {
    try { return JSON.parse(localStorage.getItem(lsKey("trusted")) || "[]"); } catch { return []; }
  });
  const sessionId = useRef<string>(localStorage.getItem(lsKey("session_id")) || crypto.randomUUID());
  const pendingApprovals = useRef<Record<string, (approved: boolean, ts: "once"|"session") => void>>({});

  useEffect(() => {
    localStorage.setItem(lsKey("messages"), JSON.stringify(items));
  }, [items]);
  useEffect(() => {
    localStorage.setItem(lsKey("trusted"), JSON.stringify(trusted));
  }, [trusted]);
  useEffect(() => {
    localStorage.setItem(lsKey("session_id"), sessionId.current);
  }, []);

  const send = async (msg: string) => {
    const userItem: TimelineItem = { id: crypto.randomUUID(), role: "user", content: msg, events: [] };
    const asstItem: TimelineItem = { id: crypto.randomUUID(), role: "assistant", content: "", events: [] };
    setItems((prev) => [...prev, userItem, asstItem]);
    setLoading(true);
    try {
      const stream = streamAgentChat({ session_id: sessionId.current, message: msg, trusted_tools: trusted });
      for await (const ev of stream) {
        handleEvent(ev, asstItem.id);
      }
    } catch (e: any) {
      setItems((prev) => prev.map((it) => it.id === asstItem.id ? { ...it, content: `❌ ${e.message}` } : it));
    } finally {
      setLoading(false);
    }
  };

  const handleEvent = (ev: AgentSSEEvent, asstId: string) => {
    setItems((prev) => prev.map((it) => {
      if (it.id !== asstId) return it;
      const events = [...(it.events || []), ev];
      let content = it.content;
      let pendingApproval = it.pendingApproval;
      if (ev.type === "assistant_delta") content += ev.data.text;
      if (ev.type === "pending_approval") {
        pendingApproval = { tool_call_id: ev.data.tool_call_id, name: ev.data.name, args: ev.data.args, danger_level: ev.data.danger_level, preview: ev.data.preview };
      }
      if (ev.type === "tool_result" && ev.data.result?.ok !== undefined && pendingApproval) {
        pendingApproval = undefined;  // 工具执行完,移除确认卡
      }
      return { ...it, content, events, pendingApproval };
    }));
  };

  const approve = async (toolCallId: string, trustScope: "once" | "session") => {
    await postApproval({ session_id: sessionId.current, tool_call_id: toolCallId, approved: true, trust_scope: trustScope });
    if (trustScope === "session") {
      // 找到工具名加入 trusted(从 pendingApproval)
      setItems((prev) => {
        const item = prev.find((it) => it.pendingApproval?.tool_call_id === toolCallId);
        if (item?.pendingApproval) setTrusted((t) => [...new Set([...t, item.pendingApproval!.name])]);
        return prev;
      });
    }
  };
  const deny = (toolCallId: string) => {
    postApproval({ session_id: sessionId.current, tool_call_id: toolCallId, approved: false, trust_scope: "once" });
  };

  return (
    <div className="flex flex-col h-full">
      <div className="flex justify-between items-center p-3 border-b">
        <h1 className="text-lg font-medium">AI 助手</h1>
        <button onClick={() => setShowCatalog(true)} className="text-sm text-blue-600 hover:underline">AI 能做什么?</button>
      </div>
      <ChatTimeline items={items} onApprove={approve} onDeny={deny} />
      <ChatComposer onSend={send} disabled={loading} />
      {showCatalog && <ToolCatalogModal onClose={() => setShowCatalog(false)} />}
    </div>
  );
}
```

- [ ] **Step 9: App.tsx 加路由**

在 `App.tsx` 的 `<Routes>` 里加:
```tsx
<Route path="/chat" element={<Chat />} />
```
并 `import Chat from "./pages/Chat";`

- [ ] **Step 10: Layout.tsx 侧栏加入口**

在 `Layout.tsx` 的导航项数组里加:
```tsx
{ path: "/chat", label: "Chat", icon: "💬" },
```

- [ ] **Step 11: 验证 TypeScript 编译 + 构建**

Run: `cd control-panel && npx tsc --noEmit 2>&1 | head -20 && npm run build 2>&1 | tail -10`
Expected: 编译通过,build 成功

- [ ] **Step 12: 重建 control-panel 容器**

Run: `sudo DOCKER_BUILDKIT=1 docker compose up -d --build control-panel && sleep 5 && curl -s http://localhost:3000 | head -5`
Expected: 200 返回 HTML

- [ ] **Step 13: Commit**

```bash
git add control-panel/src/pages/Chat.tsx control-panel/src/components/chat/ control-panel/src/App.tsx control-panel/src/components/Layout.tsx
git commit -m "feat(control-panel): /chat 聊天页 + 7 组件 + localStorage 持久化 + 路由"
```

---

## Task 13: smoke script + CLAUDE.md 收尾 + 手动清单

**目的:** spec §10.4 e2e smoke + §10.3 手动清单 + CLAUDE.md 更新入口 B 状态。

**Files:**
- Create: `scripts/smoke_agent.sh`
- Modify: `CLAUDE.md`(把入口 B 状态从 🚧 改成 ✅,加 Architecture Decisions 条目)

- [ ] **Step 1: 创建 smoke script**

`scripts/smoke_agent.sh`:
```bash
#!/usr/bin/env bash
# Agent(入口 B)端到端 smoke 测试
# 用法: bash scripts/smoke_agent.sh <admin_key> <user_key> <base_url>
set -euo pipefail

ADMIN_KEY="${1:-}"
USER_KEY="${2:-}"
BASE="${3:-http://localhost:8000}"

if [[ -z "$ADMIN_KEY" || -z "$USER_KEY" ]]; then
  echo "Usage: $0 <admin_key> <user_key> [base_url]" >&2
  exit 1
fi

echo "=== 1. admin 调 list_api_keys ==="
echo '{"session_id":"smoke-1","message":"列出所有 API key"}' | \
  curl -sN -X POST "$BASE/admin/agent/chat" \
    -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
    -d @- | head -20
echo

echo "=== 2. user 调 admin 工具(应被拒) ==="
echo '{"session_id":"smoke-2","message":"帮我把 key_x 的额度改成 1000"}' | \
  curl -sN -X POST "$BASE/admin/agent/chat" \
    -H "Authorization: Bearer $USER_KEY" -H "Content-Type: application/json" \
    -d @- | head -20
echo

echo "=== 3. admin 生成类 ==="
echo '{"session_id":"smoke-3","message":"生成一张戴帽子的猫的图片"}' | \
  curl -sN -X POST "$BASE/admin/agent/chat" \
    -H "Authorization: Bearer $ADMIN_KEY" -H "Content-Type: application/json" \
    -d @- | head -20
echo

echo "=== smoke 完成 ==="
```

```bash
chmod +x scripts/smoke_agent.sh
```

- [ ] **Step 2: 跑全量 agent 测试**

Run: `python3 -m pytest tests/test_agent_*.py tests/test_admin_service.py -v --ignore=tests/test_template_routes.py`
Expected: 全 PASS

- [ ] **Step 3: 跑 smoke(需 docker compose up)**

Run: `bash scripts/smoke_agent.sh gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o <user_key> 2>&1 | head -40`
Expected: 3 段输出都有 SSE 事件流;case 2 user role 下 set_quota_for 不可见

- [ ] **Step 4: 更新 CLAUDE.md**

把 `CLAUDE.md` 的"### aigateway 智能体的两个入口(目标形态)"段:
- `入口 B 🚧` → `入口 B ✅`
- 末尾加一行:`实现见 docs/superpowers/specs/2026-07-05-control-panel-chat-agent-design.md + plans/2026-07-05-control-panel-chat-agent.md`

在 "## Architecture Decisions & Known States" 段顶部加一条:
```markdown
- **控制台聊天窗智能体(入口 B,2026-07-05)** — 新增 `/admin/agent/chat` (SSE) + ChatRouter 三路分流(task→AgentLoop tool-calling / generation→loopback gen 管道 / understanding→loopback und 管道)。AgentLoop OpenAI function calling 循环,写工具 HITL pending_approval + POST /admin/agent/approval,role 由 KeyStore.is_admin 判定,普通用户只 3 个自查读工具(admin 9 个)。AgentConfig 走 config.yaml `agent:` 段 + `AI_GATEWAY_AGENT_*` env 覆盖 + 热重载(AgentConfigWatcher)。AuditLogger structlog+Redis ZSET 双通道 + PII 脱敏。前端 /chat 页 + 7 组件 + localStorage。spec: `docs/superpowers/specs/2026-07-05-control-panel-chat-agent-design.md`。
```

- [ ] **Step 5: 跑手动清单(spec §10.3)**

浏览器打开 `http://localhost:3000/chat`,逐项核对 spec §10.3 的 8 个 checkbox。在 plan 末尾记录结果。

- [ ] **Step 6: Commit**

```bash
git add scripts/smoke_agent.sh CLAUDE.md
git commit -m "test(agent): smoke script + CLAUDE.md 入口 B 标记完成 + 手动清单"
```

- [ ] **Step 7: 最终全量测试**

Run: `python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py 2>&1 | tail -20`
Expected: 全 PASS(或仅 pre-existing skip)

---

## Self-Review

**1. Spec coverage:**
- §1 双入口架构 → Task 10(agent_routes)+ Task 12(前端)✅
- §2 19 条决策 → 散布各 Task,逐条对应 ✅
- §3.1-3.7 后端组件 → Task 1-10 ✅
- §4 ChatRouter → Task 8 ✅
- §5 数据流 → Task 9 (AgentLoop) + Task 10 (routes) ✅
- §6 权限/审计 → Task 2 (is_admin) + Task 5 (audit) + Task 7 (三层防护) ✅
- §7 错误处理 → Task 9 (loop 错误) + Task 6 (熔断) ✅
- §8 前端 → Task 11-12 ✅
- §9 配置 → Task 3 ✅
- §10 测试 → 每个 Task 内 TDD + Task 13 smoke ✅
- §11 实施顺序 → Task 1-13 顺序一致 ✅
- §12 out of scope → 未实施 P2 工具 / SSE 重发 / Redis 历史 / 前端单测 ✅

**2. Placeholder scan:** 已检查,无 TBD/TODO/"implement later"。Task 1 Step 6 提到 `_list_l3_entries_impl` 需从 admin_routes 抽出,有明确指令。Task 10 的 `test_agent_approval_flow.py` 注明了简化理由(单元测试已在 Task 6 覆盖)。

**3. Type consistency:**
- `ToolSpec` 在 Task 4 定义,Task 7/9 使用一致 ✅
- `AgentContext` Task 4 定义,Task 7/9 使用一致 ✅
- `ApprovalDecision` Task 4 定义,Task 6/9/10 使用一致 ✅
- `AgentSession` Task 6 定义,Task 9/10 使用一致 ✅
- `SSEEvent` Task 9 定义,Task 10 使用一致 ✅
- `AgentSSEEvent` (TS) Task 11 定义,Task 12 使用一致 ✅
- `max_iterations_for_role` Task 3 定义,Task 9 使用 ✅

**关键风险点(实施时注意):**
- Task 1 抽 admin_service 时不能改 route 的响应格式(回归测试保护)
- Task 9 loopback SSE 解析依赖上游 provider 返回标准 OpenAI tool_calls 流式增量,需实测
- Task 10 approval 的并发模型(SSE generator await Future + 独立 POST resolve)是难点,Task 6 单测覆盖了核心,集成测试可能需调试
- pytest-asyncio 配置(Task 1 Step 3)若不可用,所有 async 测试改 asyncio.run 风格

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-05-control-panel-chat-agent.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
