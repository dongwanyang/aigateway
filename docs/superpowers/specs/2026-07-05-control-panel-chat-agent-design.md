# 控制台聊天窗智能体 (Chat Agent) 设计

**Status:** Draft
**Date:** 2026-07-05
**Author:** Gateway2 team via brainstorming skill
**Related:** `docs/superpowers/specs/2026-07-04-trace-debug-modality-design.md`

---

## 1. 目标形态:aigateway 智能体的两个入口

> ⭐ **本节图为 aigateway 平台的未来最终形态**(future final architecture)。当前实现覆盖入口 A;本 spec 落地入口 B。未来若新增第三入口(MCP / IDE 插件 / bot),右侧继续扩,左侧内核(dispatcher + 两条管道 + LiteLLM 出口)不变。

**核心心智:** aigateway 不是"OpenAI 代理 + 附加聊天窗",而是**一个智能体本体,面向机器和面向人各开一个入口**。两个入口共享同一套管道核心(理解 / 生成),入口 B 额外挂一层 agent tool loop —— 因为控制台真人还想让智能体"帮我做事",而机器客户端只需要"帮我理解/生成"。

```
                        ┌─────────────────────────────────────┐
                        │      aigateway 智能体                 │
                        └─────────────────────────────────────┘

╔═══════════════════════════════╗          ╔═══════════════════════════════╗
║  入口 A(机器面,现有)          ║          ║  入口 B(真人面,本 spec 新加)  ║
║  /v1/chat/completions         ║          ║  /admin/agent/chat            ║
║  OpenAI-兼容 · JSON/SSE       ║          ║  SSE · 控制台 /chat 页面       ║
╚═══════════════════════════════╝          ╚═══════════════════════════════╝
             ▲                                            ▲
             │                                            │
   ┌─────────┼─────────┐                          ┌───────┴────────┐
   │         │         │                          │                │
OpenAI    aigateway  裸 HTTP                    Control          未来还能
 SDK       CLI       (curl)                    Panel /chat        接更多
(Python/  aigateway  Postman                   (React SPA)      (MCP/IDE
 Node/     chat/run  ...                        双角色登录        插件…)
 …)                                                              
─────────────────────────────────────────────────────────────────────────
             │                                            │
             │                                            ▼
             │                                   ┌────────────────────┐
             │                                   │ ChatRouter         │
             │                                   │ (task/gen/und 分类)  │
             │                                   └────────────────────┘
             │                                            │
             │                       ┌────── task ────────┤
             │                       │                    │
             │                       ▼                    │
             │                ┌────────────────┐          │
             │                │ AgentLoop      │          │
             │                │ (tool calling) │          │
             │                │ + HITL/Audit   │          │
             │                └────────┬───────┘          │
             │                         │                  │
             │                         │ loopback         │ 直连
             │                         │ (带 AGENT_KEY)    │
             │                         │                  │
             ▼                         ▼                  ▼
   ═══════════════════════════════════════════════════════════════
                     │ 统一进入 RequestDispatcher.dispatch()          │
                     └──────────────────────────────────────────────┘
                                        │
                       ① 共用前置:media_optimization → PII
                       ② classify_request:模态/意图判定
                       ③ 分流到理解 or 生成管道
                                        │
                     ┌──────────────────┴──────────────────┐
                     ▼                                     ▼
          ┌──────────────────────┐              ┌──────────────────────┐
          │ 理解管道 (Understanding) │              │ 生成管道 (Generation)  │
          │ PipelineEngine        │              │ PipelineEngine        │
          │ • rag_retriever       │              │ • ai_director         │
          │ • conv_compressor     │              │ • intent_evaluator    │
          │ • 缓存查找 L1/L2/L3     │              │ • token_compressor    │
          │ • 配额扣减              │              │ • draft_generator     │
          │ • prompt_compress     │              │ • gen_model_router    │
          └──────────┬───────────┘              │ • cost_tracker        │
                     │                          └──────────┬───────────┘
                     └────────────┬──────────────────────┘
                                  ▼
                     ┌────────────────────────────┐
                     │  LiteLLMBridge (统一出口)   │
                     │  • auto 模型末端解析          │
                     │  • CircuitBreaker            │
                     │  • fallback chain            │
                     └────────────┬───────────────┘
                                  │
                     ┌────────────┼─────────────────────────┐
                     ▼            ▼           ▼             ▼
                  OpenAI     Anthropic     Agnes       DeepSeek/…
                (gpt-4o…)   (claude…)   (image/video)   
```

### 1.1 两个入口的对照

| 维度 | 入口 A: `/v1/chat/completions` | 入口 B: `/admin/agent/chat` |
|---|---|---|
| **面向谁** | 机器(SDK/CLI/IDE/curl) | 控制台真人(admin + 终端用户) |
| **协议** | OpenAI-兼容 chat completions | SSE 事件流(自定义事件类型) |
| **认证** | Bearer / x-api-key(用户自己的 key) | 同左(登录态透传) |
| **能触发的能力** | 理解管道 · 生成管道 | 工具循环 · 理解管道 · 生成管道 |
| **有 tool loop 吗** | ✗(客户端传 `tools` 时仅透传给上游模型,不代执行) | ✓(ChatRouter 判 task 类走 AgentLoop) |
| **有 HITL 确认吗** | ✗ | ✓(写工具默认弹确认卡) |
| **能改运维状态吗** | ✗(纯 LLM 调用) | ✓(admin 角色可通过工具改配额/插件/缓存) |
| **响应形态** | JSON / SSE(标准 OpenAI 格式) | SSE(routing/tool_call/pending_approval/media_output/final) |
| **计费归属** | 该 key 的正常配额 | task 类走 `AGENT_INTERNAL_KEY`(不扣用户配额)· gen/und 类走登录 key 配额 |

### 1.2 关键性质

1. **共享内核**:两个入口最终都进 `RequestDispatcher.dispatch()`;PII / 缓存 / 路由 / 熔断 / 成本追踪一次实现两边受益。
2. **入口 B ⊇ 入口 A 的能力**:入口 B 处理"理解/生成"类请求时,给入口 A 套 SSE 门面 + 认证透传(内部 loopback)。入口 A 的任何优化(缓存命中、模型路由)入口 B 自动继承。
3. **入口 B 独占的东西**:tool loop / HITL / audit / trust 都只挂在入口 B,不引入到入口 A —— 保持 OpenAI 兼容纯净。
4. **未来接第三个入口很便宜**:MCP server / IDE 插件 / bot 等按同样心智新开一个入口,复用 dispatcher 即可。**这是"平台即智能体"这个心智模型的最大好处 —— 入口是拿来即用的,内核只需要维护一份。**

---

## 2. 决策清单(19 条)

已在 brainstorming 环节确认:

1. **双角色都要**,role-based tool allowlist(admin / user)。
2. **写操作 HITL 卡**,读操作直通。
3. **信任范围**默认会话级,允许升级"永久信任此工具"(前端 localStorage)。
4. **Agent 循环在后端**(`/admin/agent/chat`,SSE)。
5. **普通用户只有自查读工具**;写操作全在 admin 工具集。
6. **UI 单独页面 `/chat`**(不做 Dock 浮动面板)。
7. **Agent 调本 gateway loopback**(不扣终端用户配额)。
8. **聊天历史 localStorage**(MVP);未来可升级 Redis。
9. **SSE 流式响应**(assistant_delta / tool_call / tool_result / pending_approval / media_output / final / error)。
10. **Chat Router 单入口三路分流**(task / generation / understanding)。
11. **1.1 图入 spec + `CLAUDE.md`**(标记"目标形态")。
12. **关键参数走 `config.yaml agent:` 段** + env 覆盖 + 热重载。
13. **MVP 9 个工具**,其余 6 个进 P2。
14. **`max_iterations=20`,`approval_timeout=120s`,`session_ttl=600s`(全部可配)。**
15. **Admin 用 KeyStore `is_admin` 字段识别**(初始灌种 from config.yaml)。
16. **Audit 默认 7 天(可配)**。
17. **SSE 断连即取消**(MVP 不做事件重发)。
18. **单工具熔断阈值/冷却做配置项**(默认 3 次 / 会话内永久禁用)。
19. **后端有单/集成测试**,前端 MVP 手动清单 + smoke script。

---

## 3. 后端组件契约

### 3.1 新增文件

| 文件 | 职责 |
|---|---|
| `aigateway-api/src/aigateway_api/agent_routes.py` | SSE 路由 (`/admin/agent/chat` / `/approval` / `/tools` / `/session/{sid}`)、AgentSession 存储与 GC |
| `aigateway-api/src/aigateway_api/agent/loop.py` | AgentLoop —— tool-calling 循环控制器 |
| `aigateway-api/src/aigateway_api/agent/tools.py` | ToolRegistry / ToolSpec / role scoping / OpenAI schema 转换 |
| `aigateway-api/src/aigateway_api/agent/handlers.py` | 每个工具的实际 handler(薄封装,内部调 `admin_service`) |
| `aigateway-api/src/aigateway_api/agent/chat_router.py` | ChatRouter —— 分类 task/generation/understanding |
| `aigateway-api/src/aigateway_api/agent/session.py` | AgentSession 数据结构 + await_approval Future 管理 |
| `aigateway-api/src/aigateway_api/agent/audit.py` | AuditLogger —— structlog + Redis ZSET 双通道 |
| `aigateway-api/src/aigateway_api/agent/config.py` | `AgentConfig` dataclass + Watcher(热重载) |
| `aigateway-api/src/aigateway_api/admin_service.py` | **抽出**:把 `admin_routes.py` 里的业务函数抽成独立函数,route 和 tool handler 都调这个 |

### 3.2 修改文件

| 文件 | 修改 |
|---|---|
| `aigateway-api/src/aigateway_api/main.py` | lifespan 中初始化 ToolRegistry / AuditLogger / AgentConfigWatcher;挂载 agent_routes |
| `aigateway-core/src/aigateway_core/security.py` | KeyStore 增加 `is_admin: bool` 字段读写 |
| `aigateway-core/src/aigateway_core/config.py` | 增加 `agent:` 段 + 默认值 + env 覆盖 |
| `config.yaml` + `config.yaml.template` | 增加 `agent:` 段(含全部可配参数注释) |
| `.env.example` | 增加 `AGENT_INTERNAL_KEY`(loopback 用) |
| `control-panel/src/App.tsx` | 新增 `/chat` 路由 |
| `control-panel/src/components/Layout.tsx` | 侧栏新增 "Chat" 入口 |
| `control-panel/src/api/client.ts` | SSE 客户端封装 + agent-related API types |

### 3.3 ToolSpec 数据结构

```python
@dataclass
class ToolSpec:
    name: str                                       # e.g. "get_my_usage"
    description: str                                # 给模型看的
    parameters: dict                                # JSON Schema
    roles: FrozenSet[Literal["admin", "user"]]
    kind: Literal["read", "write"]
    handler: Callable[[dict, AgentContext], Awaitable[dict]]
    danger_level: Literal["low", "medium", "high"]  # 前端确认卡展示

class ToolRegistry:
    def register(self, spec: ToolSpec) -> None: ...
    def visible_to(self, role: str) -> list[ToolSpec]: ...
    def get(self, name: str) -> ToolSpec | None: ...
    def openai_schemas(self, role: str) -> list[dict]: ...

@dataclass
class AgentContext:
    session_id: str
    user_role: Literal["admin", "user"]
    caller_api_key_id: str | None
    trace_id: str
    trusted_tools: set[str]                # 本会话已信任的工具名
    approval_callback: Callable[[str, dict], Awaitable[ApprovalDecision]]
```

### 3.4 MVP 工具集(9 个,含 P2 展望)

**MVP(9):**

| 分组 | 工具 | roles | kind | 底层 |
|---|---|---|---|---|
| 自查 | `get_my_usage` | user, admin | read | `admin_service.get_key_usage(ctx.caller_api_key_id)` |
| 自查 | `get_my_quota` | user, admin | read | `admin_service.get_key_quota(ctx.caller_api_key_id)` |
| 自查 | `get_my_recent_logs` | user, admin | read | `admin_service.query_logs(api_key_id=ctx.caller_api_key_id, days=N)` |
| Admin: Key | `list_api_keys` | admin | read | `admin_service.list_api_keys()` |
| Admin: Key | `set_quota_for` | admin | write | `admin_service.update_api_key(key_id, monthly_cost/daily_token/rpm/tpm)` |
| Admin: 插件 | `list_plugins` | admin | read | `admin_service.get_plugins_config()` |
| Admin: 插件 | `toggle_plugin` | admin | write | `admin_service.set_plugin_enabled(name, enabled)` |
| Admin: 缓存 | `list_l3_entries` | admin | read | `admin_service.list_l3_entries()` |
| Admin: Trace | `get_trace` | admin | read | `admin_service.get_trace(trace_id)` |

**P2(6,不在本次实施范围):** `create_api_key`, `revoke_api_key`, `set_plugin_debug`, `delete_l3_entry`, `list_rag_documents`, `search_logs`。

### 3.5 普通用户强隔离(三层防护)

1. **Registry 层**:`registry.visible_to("user")` 只返回 `roles` 集合含 "user" 的工具。LLM 的 tools 数组里根本不出现 admin 工具的 schema —— 模型看不到。
2. **Executor 层**:即使模型幻觉出 admin 工具名,ToolExecutor 二次校验 `role in spec.roles`,不通过直接返回 `{"error":"tool_not_permitted"}`,并 audit 一条 denied 记录。
3. **Handler 层**:`get_my_*` 系列 handler 内部**强制** `api_key_id = ctx.caller_api_key_id`,忽略模型传入的同名参数(且 schema 里 user role 版本不暴露该参数)。

### 3.6 AgentLoop 骨架

```python
class AgentLoop:
    async def run(self, session, user_message, ctx, persistent_trust: set[str] | None = None) -> AsyncIterator[SSEEvent]:
        ctx.trusted_tools |= (persistent_trust or set())  # 合并跨会话永久信任
        session.messages.append({"role": "user", "content": user_message})
        max_steps = config.per_role_limits.get(ctx.user_role, {}).get("max_iterations", config.max_iterations)
        for step in range(max_steps):
            # 1) loopback POST /v1/chat/completions(stream=true, tools=[...])
            tool_calls = None
            async for chunk in self._call_llm(session.messages, ctx):
                if chunk.type == "content_delta":
                    yield SSEEvent("assistant_delta", chunk.text)
                elif chunk.type == "tool_calls_final":
                    tool_calls = chunk.tool_calls
                    break

            if not tool_calls:
                yield SSEEvent("final", {"total_steps": step, "trace_id": ctx.trace_id})
                return

            # 2) 逐个 tool_call:read 直接执行、write 走 approval
            for tc in tool_calls:
                yield SSEEvent("tool_call", {"tool_call_id": tc.id, "name": tc.name, "args": tc.args})
                result = await self._execute_tool(tc, ctx)
                yield SSEEvent("tool_result", {"tool_call_id": tc.id, "result": result})
                session.messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result),
                })

        yield SSEEvent("error", {"code": "max_iterations"})

    async def _execute_tool(self, tc, ctx):
        spec = registry.get(tc.name)
        if spec is None or ctx.user_role not in spec.roles:
            audit.log_denied(tc, ctx, reason="role")
            return {"error": "tool_not_permitted"}

        # 熔断检查(见 §5.3)
        if session.tool_disabled(tc.name):
            return {"error": "tool_disabled_this_session"}

        if spec.kind == "write" and tc.name not in ctx.trusted_tools:
            decision = await ctx.approval_callback(tc.id, {
                "name": tc.name, "args": tc.args,
                "danger_level": spec.danger_level,
                "preview": self._make_preview(spec, tc.args),
            })
            if not decision.approved:
                return {"error": decision.reason}  # user_denied / approval_timeout
            if decision.trust_scope == "session":
                ctx.trusted_tools.add(tc.name)
            # trust_scope == "permanent" 由前端落 localStorage,后端不管

        try:
            result = await spec.handler(tc.args, ctx)
            audit.log_ok(tc, ctx, result)
            return result
        except Exception as e:
            session.record_tool_failure(tc.name)
            audit.log_error(tc, ctx, e)
            return {"error": "handler_failed", "detail": str(e)}
```

### 3.7 HTTP 面

| Method + Path | Body / Params | 用途 |
|---|---|---|
| `POST /admin/agent/chat` | `{session_id, message, trusted_tools?: string[]}` | SSE 主入口;`trusted_tools` 由前端从 localStorage 提取跨会话永久信任的工具送回来 |
| `POST /admin/agent/approval` | `{session_id, tool_call_id, approved, trust_scope: "once"|"session"}` | 前端确认卡回执 |
| `GET /admin/agent/tools` | — | 返回当前 role 可见的工具列表(前端展示"AI 能做什么") |
| `DELETE /admin/agent/session/{sid}` | — | 清会话(切用户 / 显式清空) |

**Role 判定**:auth_middleware 从 Bearer token 查 KeyStore,读 `is_admin` 字段得出 `role`;`caller_api_key_id = key.id`。**不信任前端传入的 role_hint。**

---

## 4. ChatRouter 三路分流

单入口后端做分类,分层三级判定(便宜到贵,任何一级命中即返回):

### 4.1 Level 1 — 关键词+结构规则(0ms,免费)

- 附件里有图/音/视频块 → `generation`
- message 命中正则 `画|生成图|生成一张|做个视频|generate\s+(image|video|picture)` → `generation`
- message 命中正则 `帮我(改|设置|删除|开启|关闭)|调用工具|list\s+api|toggle|revoke|set.+quota|查.+日志|查.+trace|查.+错误` → `task`
- 用户显式前缀 `/task` `/gen` `/ask`(可配) → 强制路由
- 否则 → Level 2

### 4.2 Level 2 — 轻量意图评估(可选,~100ms)

复用现有 `intent_evaluator` 生成优化插件的策略函数(不走整个 pipeline),score > 0.7 归对应类别,否则回落 understanding。

### 4.3 Level 3 — 兜底

`understanding`(最安全,分错代价小 —— LLM 自己会说明"这个需要生成图片")。

### 4.4 admin 加权

admin 大概率是"来做事的",Level 1 的 task 关键词表在 admin 角色下更宽松;user 角色只有明确命中"查我用量/日志"才判 task。

### 4.5 分流结果曝光到前端

每条用户消息处理起始,SSE 首发 `routing` 事件:

```
event: routing
data: {"class":"generation","reason":"keyword:生成图","level":1}
```

前端渲染小 badge(🔧 任务模式 / 🧠 理解模式 / 🎨 生成模式)。用户觉得分错可以用前缀强制重发。

### 4.6 三类差异化 SSE 事件

| 事件类型 | task | generation | understanding |
|---|---|---|---|
| `routing` | ✓ | ✓ | ✓ |
| `assistant_delta` | ✓ | ✓ | ✓ |
| `tool_call` / `tool_result` | ✓ | ✗ | ✗ |
| `pending_approval` | ✓ | ✗ | ✗ |
| `media_output` | ✗ | ✓ | ✗ |
| `final` | ✓ | ✓ | ✓ |

`media_output` 事件让前端渲染 `<img>` / `<video>` / `<audio>`;生成管道原本响应里就有 media URLs,agent_routes 层拆解为事件流。

---

## 5. 数据流(以"改月度上限为 $1000"为例)

```
① 用户在 /chat 输入: "把 sk-abc 的月度上限改成 1000 美元"
    │
    ▼
② 前端 POST /admin/agent/chat (SSE)
     body = {session_id: "cs-uuid", message: "..."}
     headers: Authorization: Bearer <当前登录 admin key>
    │
    ▼
③ agent_routes.py:
     - auth_middleware 解出 role="admin", caller_key_id
     - 首次见到 session_id → new AgentSession(TTL 600s)
     - ChatRouter 判定 → task
     - SSE: routing {class:"task",...}
     - registry.visible_to("admin") → 9 个工具 schema
     - AgentLoop.run() 开始迭代
    │
    ▼
④ AgentLoop step 1: loopback POST /v1/chat/completions (AGENT_INTERNAL_KEY)
     模型输出 tool_calls: list_api_keys()
    │
    ├── SSE: tool_call {name:"list_api_keys",args:{}}   ─→ 时间轴节点
    │
    └── read 直执行 → {"keys":[{id:"sk-abc-id",...}]}
    │
    ├── SSE: tool_result {...}                          ─→ 折叠 JSON
    │
    ▼
⑤ AgentLoop step 2: 再调 loopback
     模型输出 tool_calls: set_quota_for(key_id="sk-abc-id", monthly_cost=1000)
    │
    ▼
⑥ write & 未信任 → 发 pending_approval
     SSE: pending_approval {
       tool_call_id: "tc-1",
       name: "set_quota_for",
       args: {key_id:"sk-abc-id", monthly_cost:1000},
       danger_level: "medium",
       preview: "将 sk-abc 的月度成本上限从 $500 改为 $1000"
     }
     AgentLoop await approval,最多 120s
    │
    ▼
⑦ 用户勾"本会话信任此工具" + 点"批准"
     前端 POST /admin/agent/approval
       {session_id, tool_call_id:"tc-1", approved:true, trust_scope:"session"}
    │
    ▼
⑧ AgentLoop 拿到 decision:
     ctx.trusted_tools.add("set_quota_for")
     调 handler → admin_service.update_api_key(...)
     SSE: tool_result {ok:true, monthly_cost_limit:1000}
     audit.log_ok(...)
    │
    ▼
⑨ AgentLoop step 3: 模型无 tool_calls,输出终态文本
     SSE: assistant_delta "已经把 sk-abc 的月度上限改为 $1000..."
     SSE: final {total_steps:3, tools_used:[...], trace_id:...}
    │
    ▼
⑩ 前端 localStorage 落盘 messages
   若 approval 卡还勾"永久信任" → localStorage.trustedTools.add("set_quota_for")
```

---

## 6. 权限模型与审计

### 6.1 Role 判定链

```
HTTP Request → auth_middleware → KeyStore.get(bearer_token)
                                        │
                             key.is_admin = ? → role = "admin" | "user"
                                                caller_key_id = key.id
```

**KeyStore 变更**:API-Key hash 加 `is_admin: bool`(默认 false)。`config.yaml auth.api_keys[]` 可声明 `is_admin: true` 初始灌种。

**安全兜底**:首次启动如果没有任何 admin key,把 `config.yaml` 里第一个 key 自动升 admin(打 warn 日志),避免锁死。

### 6.2 Audit 记录格式

```json
{
  "ts": "2026-07-05T10:32:11.234Z",
  "session_id": "cs-uuid",
  "trace_id": "trc-xxx",
  "role": "admin",
  "caller_key_id": "sk-abc-hash",
  "tool_name": "set_quota_for",
  "tool_kind": "write",
  "args": {"key_id": "sk-target-hash", "monthly_cost": 1000},
  "result_status": "ok",
  "result_summary": "...",
  "elapsed_ms": 42,
  "approval": {"required": true, "granted": true, "trust_scope": "session"}
}
```

**双通道输出:**
- **structlog** JSON 日志(自动带 trace_id,依赖现有 ContextInjectProcessor)
- **Redis ZSET** `aigateway:agent_audit:{yyyy-mm-dd}`,score=ts_epoch,TTL=`audit_log_ttl_seconds`

**PII 脱敏**:写入 audit 前对 args 走 `PIIDetector.sanitize`,防用户在自然语言里粘的 API key 被记进 audit。

### 6.3 Result status 集合

`ok | error | denied | timeout | tool_disabled`

无论哪种,必写 audit(除非 audit 系统本身挂了)。

---

## 7. 错误处理

### 7.1 分层策略

| 层 | 场景 | 处理 |
|---|---|---|
| **网关层** | 未登录 / rate limit | sauth_middleware / RateLimiter,SSE error + 断连 |
| **AgentLoop** | 达到 `max_iterations` | SSE `error {code:"max_iterations"}` |
| **AgentLoop** | Loopback 返回空 | SSE `error {code:"empty_response"}` |
| **ToolExecutor** | Role 拒绝 / 工具不存在 | tool_result `{error:"tool_not_permitted"}` → **不断流,让模型继续** |
| **ToolExecutor** | Handler 抛异常 | tool_result `{error:"handler_failed"}` → **不断流** |
| **Approval** | 用户拒绝 | tool_result `{error:"user_denied"}` → 不断流 |
| **Approval** | 超时 120s | tool_result `{error:"approval_timeout"}` → 不断流 |
| **Loopback** | HTTP 500 / 熔断 | SSE `error {code:"loopback_failed"}` + 断流 |
| **SSE 连接** | 客户端断连 | asyncio.CancelledError → 取消 pending future,session TTL 内保留(MVP 不重发) |

**关键取舍:Tool 错误 vs Loop 错误**
- **Tool 错误**(handler / role / denied / timeout)→ 塞给模型让它决定下一步。模型能优雅降级("好的我不改了")。
- **Loop 错误**(max_iter / loopback 挂)→ 中断流,发 SSE error,前端展示提示但保留会话。

### 7.2 熔断降级

- **Loopback 熔断**:连续失败 5 次 → session 标 "degraded" 60s,期间新消息直接返回 `gateway_degraded`。
- **单工具熔断**:同 session 内某工具连续失败 `tool_failure_threshold` 次(默认 3),后续调用直接 short-circuit;`tool_failure_cooldown_seconds` 秒后恢复(默认 0 = 本会话内永久禁用)。

### 7.3 用户可见错误消息约定

前端拿 error code 后走统一 toast 模板。admin role 额外附"查看 trace_id"链接跳 Logs 页。

| code | 展示文案 |
|---|---|
| `max_iterations` | AI 一次能做的动作太多了,请把任务拆小一点重发 |
| `approval_timeout` | 确认卡片超时未响应,已取消这次操作 |
| `loopback_failed` | 网关内部服务异常,请稍后重试 |
| `gateway_degraded` | 网关正在降级保护中,请稍等 1 分钟后重试 |
| `tool_disabled_this_session` | 这个工具本次会话连续失败,已暂时禁用 |
| `empty_response` | 模型没有返回内容,请换个说法重试 |

---

## 8. 前端组件

### 8.1 页面结构

```
control-panel/src/pages/Chat.tsx
control-panel/src/components/chat/
  ├── ChatComposer.tsx       # 输入框 + 发送 + 前缀提示
  ├── ChatTimeline.tsx       # 事件时间轴(user/assistant/tool_call/tool_result/media)
  ├── ApprovalCard.tsx       # 写工具确认卡(danger_level / preview / trust checkbox)
  ├── ToolCallCard.tsx       # tool_call + tool_result 展示(可折叠 JSON)
  ├── MediaOutputCard.tsx    # 图/视频/音频渲染
  ├── RoutingBadge.tsx       # 🔧/🧠/🎨 分流标签
  └── ToolCatalogModal.tsx   # "AI 能做什么" 弹窗(GET /admin/agent/tools)
```

### 8.2 localStorage 结构

```
aigateway:chat:{login_key_id}:messages   # OpenAI 格式 messages 数组
aigateway:chat:{login_key_id}:trusted    # Array<tool_name>(永久信任集合)
aigateway:chat:{login_key_id}:session_id # 当前 session_id(切登录/清空时重置)
```

### 8.3 SSE 客户端(`api/client.ts`)

用 `EventSource` 不能带自定义 header,改用 `fetch + ReadableStream` 自己解析 SSE 帧:
```typescript
async function* streamAgentChat(body: ChatRequest): AsyncIterator<AgentSSEEvent>
```

---

## 9. 配置

### 9.1 `config.yaml` 新增 `agent:` 段

```yaml
agent:
  enabled: true                          # 总开关
  max_iterations: 20                     # 单条消息内 tool-loop 步数上限
  approval_timeout_seconds: 120          # pending_approval 等前端回执超时
  session_ttl_seconds: 600               # AgentSession 内存 TTL
  session_max_messages: 200              # 单会话消息数硬顶
  model: "auto"                          # agent 用的模型
  internal_api_key: "${AGENT_INTERNAL_KEY}"  # loopback 用 key(从 .env)
  loopback_base_url: "http://localhost:8000"
  loopback_timeout_seconds: 300
  audit_log_ttl_seconds: 604800          # audit Redis TTL(7 天)
  tool_failure_threshold: 3              # 单工具熔断阈值
  tool_failure_cooldown_seconds: 0       # 熔断冷却(0=本会话永久)
  per_role_limits:
    admin:
      max_iterations: 20
    user:
      max_iterations: 10
  chat_router:
    enabled: true
    use_intent_evaluator: true
    force_class_prefixes:
      task: ["/task", "/工具"]
      generation: ["/gen", "/生成"]
      understanding: ["/ask", "/问"]
    admin_task_bias: 0.1
```

### 9.2 Env 覆盖

按 `AI_GATEWAY_AGENT_*` 命名(与 `AI_GATEWAY_GENERATION_OPTIMIZATION_*` 一致):
- `AI_GATEWAY_AGENT_MAX_ITERATIONS`
- `AI_GATEWAY_AGENT_APPROVAL_TIMEOUT_SECONDS`
- `AI_GATEWAY_AGENT_SESSION_TTL_SECONDS`
- `AI_GATEWAY_AGENT_AUDIT_LOG_TTL_SECONDS`
- ...

`.env.example` 新增 `AGENT_INTERNAL_KEY=`(部署时填一个 admin-level key)。

### 9.3 热重载

`AgentConfig` dataclass + `AgentConfigWatcher`,走 `ConfigManager.on_reload()` callback,atomic swap 无锁读。非法值(如 `max_iterations = 0`)回退到上一次有效值并 warn(和 `GenerationOptimizationConfigWatcher` 一致的容错策略)。

**硬边界(不做成配置项):**
- 工具的 `roles` 集合(策略,不能被 config 改)
- 工具 `kind: read|write`(策略,不能被 config 改)

---

## 10. 测试策略

### 10.1 单元测试(`tests/`)

| 文件 | 覆盖 |
|---|---|
| `test_agent_tool_registry.py` | ToolSpec 注册 / role 隔离 / openai_schemas 转换 |
| `test_agent_handlers_read.py` | 自查工具 `caller_key_id` 强制注入 |
| `test_agent_handlers_write.py` | admin 写工具成功/失败路径 |
| `test_agent_loop.py` | 步数上限 / 无 tool_calls 终止 / pending_approval / trust session |
| `test_agent_chat_router.py` | Level 1 关键词 / 附件推断 / admin 加权 / force prefix |
| `test_agent_config.py` | dataclass 校验 / env 覆盖 / 非法值降级 / 热重载 |
| `test_agent_audit.py` | structlog+Redis 双写 / TTL / PII 脱敏 / 各 status |
| `test_agent_role_admin_lookup.py` | KeyStore is_admin / config 灌种 / 首个 key 自动升 admin |

### 10.2 集成测试

| 文件 | 覆盖 |
|---|---|
| `test_agent_routes_sse.py` | SSE 事件序列(mock loopback) |
| `test_agent_approval_flow.py` | pending_approval → POST /approval → loop 恢复 完整链路 |
| `test_chat_router_gen_und.py` | 生成/理解走 loopback 而非 tool loop |

### 10.3 前端 MVP 手动清单

- [ ] Chat 页面能打开,输入框能发消息
- [ ] admin 登录能看到全部 9 个工具(`/admin/agent/tools`),user 登录只看 3 个自查
- [ ] "帮我查今天用量"(user)→ 走 tool loop,渲染 tool_call / tool_result 卡
- [ ] "帮我改额度到 500"(admin)→ 弹 ApprovalCard,批准后完成
- [ ] 勾"本会话信任",第二次同工具调用不弹卡
- [ ] "画一只戴帽子的猫"→ ChatRouter 判 generation,`media_output` 事件渲染图片
- [ ] SSE 断连(手动关标签页)→ 后端 loop 优雅取消,不 hang
- [ ] localStorage 里 messages 落盘,刷新后能重现

### 10.4 端到端 smoke(`scripts/smoke_agent.sh`)

`docker compose up -d` 后:
- 用 admin key 打 `/admin/agent/chat` "列出所有 API key",断言返回文本包含 seed key
- 用 user key 打相同请求,断言返回权限提示(role 隔离)
- 用 admin key 打生成类 message,断言 `media_output` 事件出现

### 10.5 运行命令

```bash
python3 -m pytest tests/ -v --ignore=tests/test_template_routes.py
python3 -m pytest tests/test_agent_*.py -v   # 只跑本次新增
bash scripts/smoke_agent.sh                  # e2e
```

---

## 11. 实施顺序建议(给 writing-plans skill)

1. **基础设施层**:抽 `admin_service.py`(把 admin_routes 里业务函数抽独立函数)
2. **认证层**:KeyStore `is_admin` 字段 + config 灌种 + 首 key 兜底
3. **配置层**:`AgentConfig` + `AgentConfigWatcher`
4. **Agent 内核**:ToolSpec / ToolRegistry / AgentSession / AgentLoop / AuditLogger
5. **9 个 MVP 工具 handlers**
6. **ChatRouter**
7. **HTTP 面**:`agent_routes.py`(SSE / approval / tools / session)+ main.py 挂载
8. **前端**:`/chat` 路由 + SSE 客户端 + 组件树 + localStorage
9. **测试**:单元 → 集成 → 手动清单 → smoke
10. **文档**:更新 `CLAUDE.md` 加"目标形态"章节

---

## 12. Out of scope(明确不做)

- P2 六个工具(create_api_key / revoke_api_key / set_plugin_debug / delete_l3_entry / list_rag_documents / search_logs)
- SSE 事件重发(断连即取消)
- 后端 Redis 聊天历史持久化(用 localStorage)
- 前端单测(Vitest 后续引入)
- 全站 audit 页面(Chat 内 audit 弹窗即可)
- MCP / IDE 插件 / bot 等第三入口(未来扩展点,不本次实施)
- ReAct / 判断式 tool call 之外的循环形式(方案 A 已定)
