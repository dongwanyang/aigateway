# AI Gateway 端到端测试基线设计

**日期**:2026-07-05
**作者**:Gateway2 + Claude (brainstorming session)
**状态**:待用户 review

---

## 1. 目标与非目标

### 1.1 目标

针对近期落地的 PR1(全链路 trace_id)、PR2(五维度 debug 开关)、PR3(控制台 UI 改造)以及既有全部功能面,建立一套 **长期可复用的自动化 e2e 测试基线**,证明:

1. 控制台所有关键按钮真的调用了正确的后端接口且状态真的变
2. 参数配置(YAML 热重载、admin PUT、debug 段等)改动后真的生效
3. Prometheus 监控指标数值与业务事实一致
4. 总分总架构、两条管道、auto 末端解析、三级缓存、断路器、PII 等既有能力未回归

### 1.2 非目标

- **不做**:单元测试(项目已有 25 个 pytest 文件覆盖)、性能/压力测试、安全渗透测试、Grafana dashboard 可视化本身
- **不做**:GitHub Actions CI 配置(本地即测试环境,人工触发 `pytest` 即可)
- **不做**:LLM 输出内容质量评测(只关心 pipeline 是否走通)

### 1.3 覆盖范围

| 面 | 覆盖 |
|---|---|
| 后端 e2e | 11 个 test file,~121 条用例 |
| 前端 UI e2e(Playwright) | 4 个 test file,~22 条用例 |
| 总用例数 | ~143 条 |
| LLM 真调次数(一次全跑) | ~1037 次 Agnes 请求 |

---

## 2. 测试基础设施

### 2.1 环境:复用现有 docker compose

不新建 test compose,直接对着 **当前 docker compose 运行中的** `:8000` gateway + `:3000` control-panel + redis + qdrant + prometheus + grafana 跑。

- **好处**:0 基建成本,启动即测,监控指标断言用的就是真 Prometheus scrape 路径
- **代价**:测试会写入真实 redis / qdrant / Prometheus。用**命名前缀 + teardown 精准清理**隔离

### 2.2 LLM:全用 Agnes

`providers.agnes` 承担所有 LLM 调用:
- 文本(理解)用 Agnes 文本模型(具体模型名从 `config.yaml` 的 `providers.agnes.model_grouper` 读)
- 图片生成用 `agnes-image-2.1-flash`
- 视频生成用 `agnes-video-v2.0`

**Token 不设预算**:按真实业务规模发 prompt。`prompt_compress` 需要长文才有意义、RAG 需要多轮上下文、cache evict 需要 1001 条真请求 —— 一律照实来。

**fallback / circuit breaker 场景**:临时新增一个测试专用 provider `test-broken`(见 Precondition P3),base_url 指向不存在地址,承担"故意失败"角色,不污染 agnes 配置。

### 2.3 数据隔离策略

- **命名前缀**:所有测试造的 API key / cache key / rag document / qdrant point 都带 `test-e2e-<uuid>-` 前缀
- **teardown 清理**:测试 fixture 内部**直连 redis (`redis.asyncio`) 和 qdrant client** 扫描该前缀清理,不新增 gateway `/admin/test-cleanup` 接口
- **不影响开发数据**:开发日常创建的 key 从不带该前缀,天然隔离

### 2.4 前置健康检查

`tests/conftest.py::pytest_configure` 阶段先 `GET :8000/health`,不通 → `pytest.exit`,防止"环境没起"被误报为"测试失败"。

### 2.5 目录结构

```
tests/
├── conftest.py                            # 全局 fixtures + 前置健康检查
├── e2e/
│   ├── __init__.py
│   ├── test_pipelines_and_dispatch.py     # §5.1  9 条
│   ├── test_trace_id_end_to_end.py        # §5.2  7 条
│   ├── test_debug_dimensions.py           # §5.3  9 条
│   ├── test_cache_three_tier.py           # §5.4  9 条
│   ├── test_pii_detector.py               # §5.5  11 条
│   ├── test_circuit_breaker.py            # §5.6  5 条
│   ├── test_prometheus_metrics.py         # §5.7  12 条
│   ├── test_admin_api.py                  # §5.8  ~35 条
│   ├── test_config_effects.py             # §7    6 条
│   ├── test_metrics_reconciliation.py     # §8    9 条
│   └── test_trace_consistency.py          # §9    9 条
├── ui/
│   ├── __init__.py
│   ├── conftest.py                        # Playwright fixtures
│   ├── test_plugins_page.py               # §6.2  6 条
│   ├── test_config_page.py                # §6.3  5 条
│   ├── test_logs_page.py                  # §6.4  6 条
│   └── test_other_pages_smoke.py          # §6.5  6 条
└── fixtures/
    ├── __init__.py
    ├── clients.py                          # admin_client / user_client
    ├── prom.py                             # prom_scrape 助手
    ├── config.py                           # 读写宿主 config.yaml 助手
    ├── trace.py                            # events 顺序断言助手
    └── data.py                             # unique_prefix / redis+qdrant teardown 清理
```

### 2.6 pytest 命令

```bash
# 全套
python3 -m pytest tests/ -v

# 只测 UI
python3 -m pytest tests/ui -v

# 只测某文件
python3 -m pytest tests/e2e/test_debug_dimensions.py -v
```

**无 slow 标记** —— 所有用例(含 1001 条 cache evict)每次全跑。

---

## 3. 关键 fixtures 约定

### 3.1 `conftest.py`(全局)

```python
BASE = "http://localhost:8000"
UI_BASE = "http://localhost:3000"
ADMIN_KEY = os.environ["AI_GATEWAY_ADMIN_KEY"]         # 强制 env,不给默认
HOST_CONFIG_YAML = "/home/ubuntu/aigateway/config.yaml"  # 宿主可读

def pytest_configure(config):
    r = httpx.get(f"{BASE}/health", timeout=3)
    if r.status_code != 200:
        pytest.exit(f"Gateway :8000 not healthy: {r.status_code}", 2)

@pytest.fixture
def unique_prefix():
    return f"test-e2e-{uuid.uuid4().hex[:8]}-"

@pytest.fixture
def admin_client():
    return httpx.Client(base_url=BASE,
                        headers={"X-Admin-Key": ADMIN_KEY},
                        timeout=30)

@pytest.fixture
def user_client(admin_client, unique_prefix):
    resp = admin_client.post("/admin/api-keys",
                             json={"name": f"{unique_prefix}user"})
    key = resp.json()["key"]
    yield httpx.Client(base_url=BASE,
                       headers={"Authorization": f"Bearer {key}"},
                       timeout=60)
    admin_client.delete(f"/admin/api-keys/{key}")

@pytest.fixture(autouse=True)
def cleanup_test_data(unique_prefix):
    """Session teardown: 直连 redis + qdrant 扫 prefix 清理。"""
    yield
    _scan_and_delete_redis_keys(f"*{unique_prefix}*")
    _scan_and_delete_qdrant_points_by_payload_prefix(unique_prefix)
```

### 3.2 `tests/ui/conftest.py`(Playwright)

```python
@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        yield p.chromium.launch()

@pytest.fixture
def page(browser, admin_client):
    ctx = browser.new_context()
    # 无登录页,直接塞 localStorage
    ctx.add_init_script(
        f"localStorage.setItem('aigateway_api_key', '{ADMIN_KEY}');"
    )
    page = ctx.new_page()
    yield page
    ctx.close()
```

---

## 4. 分层组织(方案 A:金字塔)

- `tests/e2e/*` — httpx 后端断言,不启浏览器
- `tests/ui/*` — Playwright 真开 chromium,只测 UI 层
- `tests/fixtures/*` — 跨文件共享的 fixture 与工具函数

后端 e2e 失败 → 不阻塞 UI e2e 运行;两层独立报告。

---

## 5. 后端 e2e 用例大纲

### 5.1 `test_pipelines_and_dispatch.py`(9 条)

覆盖:`dispatcher.py::classify_request` + 两条 `PipelineEngine` + bridge `set_auto_resolver`。

| # | 用例 | 触发 | 断言 |
|---|---|---|---|
| 1 | understanding 分流 | 纯文本 chat,`model=<agnes 文本模型>` | 响应 `_meta.pipeline_kind == "understanding"`;events 含 rag/conv/cache 等 understanding 插件名 |
| 2 | generation 显式意图分流 | body 加 `generation_intent: true` | `_meta.pipeline_kind == "generation"`;events 含 6 gen-opt 插件 |
| 3 | generation 模态推断 | messages 含 `type: image_url` 内容块 | 同上;media_optimization 埋点在 dispatch 阶段(共用前置)出现 |
| 4 | generation 模型名 | `model=agnes-image-2.1-flash` | `_meta.pipeline_kind == "generation"` |
| 5 | auto 末端解析(understanding) | `model=auto`,纯文本 | `_meta.model_router` 存在;选中模型属 Agnes llm/mllm 候选池 |
| 6 | auto 末端解析(generation) | `model=auto` + `generation_intent: true` | `_meta.model_router` 选中属 Agnes generative 候选池 |
| 7 | PII 共用前置 | prompt 含 `email: foo@bar.com` | 两条管道都脱敏(understanding + generation 分别验) |
| 8 | generation 不查 prompt_cache | 连发两次相同 generation 请求 | 第二次 `_meta.cache_hit != "L1"`;events 无 prompt_cache 命中标记 |
| 9 | ModelRouterPlugin 空壳被 skip | 任意 understanding 请求 | events 里无 `plugin=model_router` 阶段(被 `_skip_names` 跳过) |

**Agnes 真调:~7 次**

### 5.2 `test_trace_id_end_to_end.py`(7 条)

覆盖:`trace_middleware.py` + `trace_event.py` + `logger.py::ContextInjectProcessor` + `/admin/trace/{id}`。

| # | 用例 | 断言 |
|---|---|---|
| 1 | 客户端不传 → 自动生成 | 响应 header 有 `X-Request-ID`;`GET /admin/trace/<id>` 返回非空 events |
| 2 | 客户端传 `X-Request-ID` → 透传 | 响应 header 回显同值;admin 查得到 |
| 3 | events 覆盖完整链路 | 数组含 `kind=stage`(dispatcher 埋点)+ `kind=plugin`(engine 插件埋点);顺序合理 |
| 4 | 5xx exception handler 兜底 | 打一个必失败的 admin 路径(非 GatewayError/HTTPException)→ 响应含 `X-Request-ID` + 统一结构 detail |
| 5 | Redis key `aigateway:trace:{id}` + TTL | 直连 redis:hash 存在;TTL 在 `(86400*6, 86400*7)` |
| 6 | stdlib logger.info 带 trace_id | 抓 `docker logs gateway` 找 trace_id → 有匹配日志行,含相同 trace_id 字段 |
| 7 | 早返回路径 emit skip | 配额耗尽的请求 → events 含 `status=skip` 埋点 |

**Agnes 真调:~3 次**

### 5.3 `test_debug_dimensions.py`(9 条)

覆盖:`debug_config.py` + `DebugConfigWatcher` + `TraceCollector.emit_debug` + admin `/admin/config/debug` + `/admin/plugins/{name}/debug`。

| # | 用例 | 断言 |
|---|---|---|
| 1 | 默认全关 → 无 debug 事件 | 5 维度 false;发一次请求 events 里 0 条 `kind=debug` |
| 2 | 只开 entry | events 出现 kind=debug + dimension=entry;其余维度 debug 事件 0 |
| 3 | 只开 cache | 命中/miss 各路径都有 dimension=cache 事件;payload 含 key_hash + tier_hit |
| 4 | 只开 bridge | bridge 正常返回路径 emit;payload 含 model + stream |
| 5 | plugins 总开关 AND per-plugin | 只开 plugins_enabled + per-plugin 关 → 无 plugin 事件;再开 per-plugin → 出现 |
| 6 | prompt_compress 隐藏 debug | GET `/admin/plugins-config` → prompt_compress 项 `debug === null` |
| 7 | hot-reload | 编辑 `config.yaml` debug 段 → 等 **3s** → GET `/admin/config/debug` 反映变化 |
| 8 | 无效值回退 | PUT 非法值(如 `entry: "maybe"`)→ 4xx 或值保持原样 |
| 9 | POST `/admin/plugins/{name}/debug` 单独 toggle | 只 toggle `rag_retriever` → 只 rag_retriever debug 起效 |

**Agnes 真调:~4 次**

### 5.4 `test_cache_three_tier.py`(9 条)

覆盖:`caching.py::CacheManager` + reranker + backfill 策略。

| # | 用例 | 断言 |
|---|---|---|
| 1 | L1 命中 | 相同 prompt 连发 2 次 → 第 2 次响应快;`cache_hit=L1`;metric `cache_hits_total{tier="L1"}` +1 |
| 2 | L2 命中(L1 evict) | 发 **1001** 条不同真 prompt(每条 5-10 token,`max_tokens=5`)→ 首条被 evict → 再发首条命中 L2;metric L2 +1 |
| 3 | L3 语义命中 | 首发 "帮我总结这段" → 再发 "请对上文做个概括" → `cache_hit=L3`;metric L3 +1 |
| 4 | L3 阈值 | 只发 5 token 短 prompt → 不触发 L3 async 回填(`token_count < 100`) |
| 5 | L2 hit 回填 L1 | L2 命中后再发相同请求 → L1 命中 |
| 6 | L3 hit 只回填 L1(不 L2) | 语义命中后 → L2 该 key 不存在;L1 存在 |
| 7 | generation 请求跳过 prompt_cache | 已在 5.1 覆盖,此处指向 |
| 8 | 手动清缓存 | POST `/admin/cache/clear` → L1/L2 count 归零 |
| 9 | 定时清理超期 auto 条目 | 塞一条 TTL=5s 的 L3 auto 条目 → POST `/admin/cache/l3/cleanup` 触发 → 数目减 1 |

**Agnes 真调:~1005 次(1001 条 evict + 4 条其他)**

### 5.5 `test_pii_detector.py`(11 条)

参数化 6 大类:

```python
@pytest.mark.parametrize("prompt,category", [
  ("我的邮箱是 foo@bar.com",              "email"),
  ("电话 138 0013 8000",                 "phone"),
  ("信用卡 4111 1111 1111 1111",          "credit_card"),
  ("身份证 110101199001011234",           "id_card"),
  ("password: SuperSecret123!",          "password"),
  ("api_key=sk-abcdefgh",                "api_key"),
])
def test_pii_masks(prompt, category, ...): ...
```

**再加**:
- 3 条策略切换:`sanitize` / `reject` / `hash`
- 2 条边界:命名字段 vs 独立模式

**Agnes 真调:0 次**(PII 在 LLM 前拦截)

### 5.6 `test_circuit_breaker.py`(5 条)

Precondition P3 已加了测试专用 provider `test-broken`(不存在 endpoint)+ fallback 链 `test-broken → agnes`。

| # | 用例 | 断言 |
|---|---|---|
| 1 | CLOSED → 3 次失败 → OPEN | metric `circuit_breaker_state{provider="test-broken"}` 从 0→1 |
| 2 | OPEN 状态直接拒绝 | 第 4 次请求返回错误 `CIRCUIT_OPEN`,不再打后端 |
| 3 | OPEN → 冷却窗后 → HALF-OPEN | 等 `cooldown_seconds` → 状态变 2 |
| 4 | HALF-OPEN 成功一次 → CLOSED | 恢复真 endpoint → 请求成功 → 状态回 0 |
| 5 | fallback 链跳到下一个 | 主 `test-broken` 失败 → fallback 到 agnes → 请求最终 200;`_meta.provider_used` 含 agnes |

**Agnes 真调:~3 次**

### 5.7 `test_prometheus_metrics.py`(12 条)

从 `metrics.py` 找出所有自定义 exporter,逐个"计数存在"验证(数值对账在 §8 独立测):

| # | Metric | 断言 |
|---|---|---|
| 1 | `aigateway_request_duration_seconds` histogram | `_count` +1;`_sum` > 0;buckets 至少一个非零 |
| 2 | `aigateway_cache_hits_total{tier=L1}` | 制造 L1 命中 → +1 |
| 3 | `aigateway_cache_hits_total{tier=L2}` | 同上,L2 |
| 4 | `aigateway_cache_hits_total{tier=L3}` | 同上,L3 |
| 5 | `aigateway_cache_misses_total` | miss 请求 → +1 |
| 6 | `aigateway_tokens_total{type=prompt/completion}` | 请求后两个值都 > 0 |
| 7 | `aigateway_active_requests` gauge | 并发时抓 → 非零;完成后归 0 |
| 8 | `aigateway_circuit_breaker_state{provider}` | 参见 5.6 |
| 9 | `aigateway_pii_detections_total` | PII 请求后 +1 |
| 10 | gen-opt 六插件 metric | 逐个触发路径 → 逐个 +1(具体名字从 `generation_optimization/metrics.py` 读) |
| 11 | `aigateway_cost_usd{api_key_group}` | 请求后 > 0(数值对账见 §8) |
| 12 | Prom scrape 路径通 | GET `:9090/api/v1/query?query=aigateway_request_duration_seconds_count` → 非零结果 |

**Agnes 真调:~5 次**

### 5.8 `test_admin_api.py`(35 条)

**35 个 endpoint(从 `admin_routes.py` / `template_routes.py` / `draft_routes.py` 精确核对)**,每个至少一条 happy path + 一条 auth 拒绝(去除 auth,期望 401)。

分组:

| 分组 | endpoints |
|---|---|
| API Key CRUD (4) | GET/POST `/admin/api-keys`,DELETE/PUT `/admin/api-keys/{key_id}` |
| Metrics JSON (1) | GET `/admin/metrics-json` |
| Plugin Config (2) | GET/PUT `/admin/plugins-config` |
| Plugin Debug (1) | POST `/admin/plugins/{name}/debug` |
| Debug Config (1) | GET `/admin/config/debug` |
| Global Config (2) | GET/PUT `/admin/global-config` |
| Config (2) | GET/PUT `/admin/config` |
| Quotas (1) | GET `/admin/quotas/{key_id}` |
| Logs (2) | GET/DELETE `/admin/logs` |
| Trace (1) | GET `/admin/trace/{id}` |
| RAG (3) | GET/POST `/admin/rag/documents`,DELETE `/admin/rag/documents/{id}` |
| Cache L3 (6) | GET/PUT `/admin/cache/l3/config`,GET `/admin/cache/l3/entries`,PUT/DELETE `/admin/cache/l3/entries/{id}[/mode]`,POST `/admin/cache/l3/cleanup` |
| Providers (2) | POST `/admin/providers/{p}/test`,GET `/admin/providers/{p}/models` |
| Templates (6) | POST/GET/GET-one/PUT/DELETE `/admin/templates[/{name}]`,POST `/admin/templates/{name}/render` |
| Drafts (1) | POST `/admin/drafts/{id}/action` |

共 **35 endpoint × 2 断言 = 70 assertion**,组织成 35 条参数化 test case(每 case 含 happy + auth-fail)。

**Agnes 真调:0 次**(admin 层不走 LLM)

---

## 6. 前端 UI e2e 用例大纲(Playwright)

### 6.1 通用约定

- **BASE**:`http://localhost:3000`
- **登录**:无登录页;`page` fixture 用 `add_init_script` 塞 `localStorage['aigateway_api_key']` = ADMIN_KEY
- **辅助造数据**:UI 用例需"先造条日志"时,通过后端 `httpx` 直接造,不通过 UI 点击造
- **console 错误断言**:每个 UI 用例末尾 assert 无 console.error(不放宽)

### 6.2 `test_plugins_page.py`(PR3 双级分组 + Debug 按钮,6 条)

| # | 用例 | 断言 |
|---|---|---|
| 1 | 双级分组渲染 | DOM 上有两个 pipeline_kind 分组标题(understanding / generation);内嵌类别子分组(缓存/安全/性能/路由/其他) |
| 2 | 全 11 插件卡片可见 | 找到 11 张卡片(用 data-testid 或卡片标题精确匹配) |
| 3 | prompt_compress 隐藏 Debug 按钮 | 定位 prompt_compress 卡片,内部无 Bug icon 按钮 |
| 4 | 点 Debug 按钮 → 真调后端 | 点 rag_retriever 卡的 Debug 按钮 → 抓到 `POST /admin/plugins/rag_retriever/debug`;GET `/admin/plugins-config` 该项 `debug === true` |
| 5 | toggle 反映到 UI | 刷新页面后 rag_retriever 的 Debug 按钮呈"已开启"态 |
| 6 | 关闭插件启用开关 | 点某插件启用开关 → 关 → 抓到 PUT `/admin/plugins-config`;后端 `plugins.<name>.enabled === false` |

**Agnes 真调:0 次**

### 6.3 `test_config_page.py`(PR3 五维度 debug 卡片,5 条)

| # | 用例 | 断言 |
|---|---|---|
| 1 | 五维度 toggle 可见 | 定位 5 个 toggle:frontend/entry/cache/bridge/plugins_enabled |
| 2 | 每维度 toggle 真调 admin | 依次开 5 个 → 每次命中 PUT `/admin/global-config`;GET `/admin/config/debug` 全 true |
| 3 | 关闭反向验证 | 依次关 → GET 全 false |
| 4 | 旧 debug_mode 已消失 | 页面不存在文本 "debug_mode" 或旧总开关 UI |
| 5 | hot-reload 生效闭环 | UI 开 cache 维度 → 立即发一次 chat → events 出现 `kind=debug` + `dimension=cache` |

**Agnes 真调:~1 次**(用例 5)

### 6.4 `test_logs_page.py`(PR3 trace modal events 瀑布流,6 条)

| # | 用例 | 断言 |
|---|---|---|
| 1 | 日志列表加载 | `/logs` 打开 → 至少 1 行日志(fixture 里先造一条) |
| 2 | 点 trace_id → 弹 modal | modal 打开,标题含该 trace_id |
| 3 | events 瀑布流渲染 | modal 里 ≥3 个事件节点,按时间纵向排列 |
| 4 | 三 kind 颜色区分 | DOM 上找到 `kind=stage` 和 `kind=plugin` 两类节点样式(class 或 color 不同) |
| 5 | payload 折叠展开 | 点某 event → 展开 payload JSON;再点 → 收起 |
| 6 | 旧 UI 元素消失 | modal 不出现 "plugin_trace 列表" 或 "耗时分布条" 文本 |

**Agnes 真调:~1 次**(用例 1 造日志)

### 6.5 `test_other_pages_smoke.py`(其余 6 页 smoke,6 条参数化)

Overview / Models / Costs / Quotas / Cache / Knowledge:

```python
@pytest.mark.parametrize("path,heading", [
  ("/",         "Overview"),
  ("/models",   "Models"),
  ("/costs",    "Costs"),
  ("/quotas",   "Quotas"),
  ("/cache",    "Cache"),
  ("/knowledge","Knowledge"),
])
def test_page_smoke(page, path, heading):
    console_errors = []
    page.on("console", lambda m: console_errors.append(m) if m.type == "error" else None)
    page.goto(f"{UI_BASE}{path}")
    expect(page.get_by_role("heading", name=heading)).to_be_visible()
    assert page.locator(".error-boundary").count() == 0
    page.wait_for_load_state("networkidle")
    assert not console_errors
```

**Agnes 真调:0 次**

---

## 7. 参数生效验证(6 条)

`tests/e2e/test_config_effects.py`。

| # | 用例 | 断言 |
|---|---|---|
| 1 | YAML 热重载 → 插件启停 | 编辑 `config.yaml` 把 `plugins.rag_retriever.enabled` false → 等 **3s** → chat events 无 rag_retriever 阶段;再改回 true 立即恢复 |
| 2 | 热重载 → 两条 Engine 重建 | 改 `plugins.pii_detector.enabled` → 等 3s → 后端日志有 `pipeline rebuilt` 标记(需 debug.entry 开着) |
| 3 | admin PUT `/admin/global-config` → 落盘 | PUT 修改 debug 段 → 直接 open(HOST_CONFIG_YAML) 读文件对比;并发两次 PUT 不崩(fcntl.flock 生效) |
| 4 | env 覆盖优先级(只读证据) | 若容器 env `AI_GATEWAY_REDIS_URL` 已设:GET `/admin/global-config` 的 redis_url 应来自 env(不等于 config.yaml 明文值);若未设 → pytest.skip("no env override configured") |
| 5 | Provider 级 vs 模型级 base_url | admin GET providers/{p}/models → 各模型 base_url;通过 events 的 model_router 记录的 endpoint 验证请求走对了 URL |
| 6 | gen-opt 无效值回退 | PUT 非法 `token_compressor.compression_ratio: "abc"` → 服务不崩;GET 保持前 valid 值;日志有 warning |

**Agnes 真调:~2 次**

---

## 8. 监控指标数值对账(9 条)

`tests/e2e/test_metrics_reconciliation.py`。**严格对账**,不放宽。

| # | 用例 | 断言 |
|---|---|---|
| 1 | tokens 计数对账 | 发 1 次 chat → 响应 `usage.prompt_tokens/completion_tokens` === metric `tokens_total{type=prompt/completion}` 增量 |
| 2 | duration histogram 对账 | 客户端记 wall-clock t1/t2 → histogram `_sum` 增量与 (t2-t1) 差在 ±200ms |
| 3 | cache_hits 与 events 对账 | 发 2 次相同 prompt → `cache_hits_total{tier=L1}` +1;第 2 次响应 events 含 `tier_hit=L1` 事件(两侧一致) |
| 4 | cost_usd 严格对账 | 读 Agnes 单价(config 或 metrics.py,见 P2)→ 期望 `= prompt×price_in + completion×price_out`;metric 增量与期望差 ≤ ±1% |
| 5 | PII detections 计数 | 发 3 个不同 PII prompt → `pii_detections_total` +3;分类 label 各 +1(若 metric 有 label) |
| 6 | active_requests gauge 精确 = 5 | asyncio.gather 5 并发慢请求(`max_tokens=500` + 复杂 prompt 让 2-4s);另起 async task 每 100ms 直接抓 gateway `:8000/metrics`(**不走 Prometheus scrape**,拿进程内 gauge 即时值);取峰值,断言 `max(samples) == 5`;全部完成后归 0 |
| 7 | gen-opt 每插件计数 | 逐个触发 6 个插件路径 → 各自 metric +1 |
| 8 | Prometheus scrape 拿得到 | GET `:9090/api/v1/query?query=aigateway_request_duration_seconds_count` → 非零结果 |
| 9 | Grafana 数据源 | GET `:3001/api/datasources`(admin/admin)→ 存在 `type=prometheus` 数据源 |

**Agnes 真调:~8 次**

---

## 9. Trace 内容一致性(9 条)

`tests/e2e/test_trace_consistency.py`。

| # | 用例 | 断言 |
|---|---|---|
| 1 | understanding 完整链路 | events 顺序含:`dispatch_start` → `media_optimization` → `pii_detector` → `classify_request(kind=understanding)` → 若干 `kind=plugin`(rag/conv/cache/quota/compress)→ `bridge` → `dispatch_end` |
| 2 | generation 完整链路 | events 含 6 gen-opt 插件全部,顺序即优先级 100→150 |
| 3 | stage/plugin/debug 三 kind 都出现 | 打开 entry+plugin debug → 三 kind 都存在;每 event 有 `kind`, `name`, `ts`, `duration_ms`, `status` 字段 |
| 4 | event.duration_ms 与 histogram 对账 | histogram `_sum` 增量 与 events 里所有 `kind=stage` 的 duration_ms 加总,差 **≤ 20 秒** |
| 5 | plugin_trace 兼容 shim | GET `/admin/trace/{id}` 的 `plugin_trace` 与 events 里 `kind=plugin` 的插件名一致(dual-write) |
| 6 | 早返回 emit skip | 配额耗尽的请求 → events 里存在 `status=skip` stage;`dispatch_end` 后无 bridge 事件 |
| 7 | 短路 `ctx.should_stop=True` | 缓存命中路径 → events 里 `prompt_cache` 后无 `bridge` 事件;`_meta.cache_hit=L1` |
| 8 | async L3 回填不阻塞 | 首次 MISS → 立刻响应(2s 内);60s 内轮询 qdrant 断言点存在;单用例 timeout 90s |
| 9 | logger.info trace_id 与 events 一致 | 抓 docker logs 找该 trace_id → 所有匹配日志行的 `trace_id` 字段 === 请求 header X-Request-ID |

**events 顺序断言用"包含 + 相对顺序"策略**(不强制整个序列,允许插入):

```python
def assert_events_order(events, must_contain_in_order):
    names = [e["name"] for e in events]
    positions = [names.index(n) for n in must_contain_in_order]
    assert positions == sorted(positions), f"order broken: {names}"
```

**Agnes 真调:~7 次**

---

## 10. Agnes 真调消耗汇总

| 文件 | Agnes 次数 |
|---|---|
| pipelines_and_dispatch | ~7 |
| trace_id_end_to_end | ~3 |
| debug_dimensions | ~4 |
| cache_three_tier | **~1005**(内含 1001 条 evict) |
| pii_detector | 0 |
| circuit_breaker | ~3 |
| prometheus_metrics | ~5 |
| admin_api | 0 |
| config_effects | ~2 |
| metrics_reconciliation | ~8 |
| trace_consistency | ~7 |
| ui plugins_page | 0 |
| ui config_page | ~1 |
| ui logs_page | ~1 |
| ui other_pages_smoke | 0 |
| **合计** | **~1046 次 / 全跑** |

---

## 11. Preconditions(测试落地前置改动)

跑测试前必须完成的 **非测试代码** 改动:

### P1 ~~新增 /admin/test-cleanup 接口~~

**移除**:测试 fixture 内部直连 redis/qdrant 前缀扫删,不改 gateway 代码。

### P2 核实 Agnes 单价配置

**动作**:检查 `config.yaml` 和 `aigateway-core/src/aigateway_core/generation_optimization/metrics.py` 是否有 Agnes 模型的 `price_per_1k_input_tokens` / `price_per_1k_output_tokens` 明确值。
**若缺失**:补充为 config 字段(建议 `providers.agnes.model_grouper[].models[].pricing`),让 §8 用例 4 有可靠对账依据。
**验收**:cost_tracker 读到明确单价并写入 `cost_usd` metric。

### P3 新增测试专用 provider `test-broken`

**动作**:在 `config.yaml` 增加:

```yaml
providers:
  test-broken:
    base_url: http://127.0.0.1:59999   # 不存在的端口
    model_grouper:
      - group: generative
        models:
          - name: test-broken-model
    fallbacks:
      - agnes             # 主失败 → agnes 顶上
```

只在测试期启用;或用 env `AI_GATEWAY_ENABLE_TEST_BROKEN=true` 控制,平时留在 config 但被 env gate 掉。
**用于**:§5.6 circuit breaker 全部用例。

### P4 useAuth 存储位置

**已确认**:控制台 `useAuth` hook 从 `localStorage['aigateway_api_key']` 读。Playwright 用 `page.context.add_init_script("localStorage.setItem('aigateway_api_key', ADMIN_KEY)")` 注入即可。**无需其他改动。**

### P5 安装 Playwright 依赖

**动作**:新建 `tests/requirements-test.txt`:

```
pytest>=8.0
pytest-asyncio>=0.23
playwright>=1.44
pytest-playwright>=0.5
httpx>=0.27
redis>=5.0
qdrant-client>=1.9
pyyaml>=6.0
```

安装:
```bash
pip install -r tests/requirements-test.txt
playwright install chromium
```

### P6 admin_routes.py 路由清单

**已确认**:从代码扫得 **35 个 endpoint**,§5.8 已列全。**无需改动。**

---

## 12. 落地步骤

1. **完成 P2 + P3 + P5**(P1/P4/P6 已解决)
2. 按 `tests/` 目录结构建 fixtures + conftest
3. 按 §5-§9 顺序实现各 test file(每个文件独立可跑)
4. 全套跑一次 `python3 -m pytest tests/ -v` 验证基线
5. 后续每次 PR 合入前手动跑一次,失败必修

---

## 13. 后续步骤

用户 review 本 spec → 确认无误 → 交接给 `superpowers:writing-plans` skill,生成分步实施计划(每 file 一个可独立执行的 checklist)。

---

**End of spec.**
