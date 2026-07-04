# AI Gateway 完整测试计划

> 本文档给出**从零到一**的端到端测试方案：任何人拿着这份文档 + 一个低端模型的 API Key 就能验证整个 Gateway 是否正常工作，并量化"token 到底省了多少"。
>
> - **手动章节 §1–§8**：无需写代码，全部 `curl` + 浏览器，10~15 分钟走完。
> - **自动化章节 §9**：一条 `python scripts/ab_test.py` 命令跑压测 + A/B 对比，输出 CSV + Markdown 报告。
> - **回归章节 §10**：`pytest tests/` 覆盖单元/集成层的 582+ 用例。

---

## 0. 测试前置

### 0.1 环境准备

按 [README.md](../README.md) 方式二完成本地开发环境搭建：

```bash
# Python 3.12 venv 已就绪
source .venv/bin/activate

# Redis + Qdrant 已启动
docker run -d --name redis  -p 6379:6379           redis:7-alpine
docker run -d --name qdrant -p 6333:6333 -p 6334:6334 qdrant/qdrant:latest

# Gateway API 已启动
uvicorn aigateway_api.main:app --host 0.0.0.0 --port 8000 --app-dir aigateway-api/src

# 前端可选
cd control-panel && npm run dev
```

### 0.2 测试变量

以下变量在整个测试中固定，请在 shell 中先 `export`：

```bash
export GW="http://localhost:8000"                              # Gateway 地址
export KEY="gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o"               # 引导 API Key
export MODEL="deepseek-v4-flash"                               # 低成本测试模型
export AUTH="Authorization: Bearer $KEY"
export CTYPE="Content-Type: application/json"
```

> **模型选择建议**：`deepseek-v4-flash`（$0.02/1K prompt）是当前 config 里最便宜的文本模型，`agnes-2.0-flash` 用于多模态；如果需要接自己的 provider，去 `config.yaml` 的 `providers.*` 节增加即可。

### 0.3 判读约定

- ✅ **PASS**：完全符合预期。
- ⚠️ **WARN**：功能可用但有告警（比如 Qdrant 未启会 fail-open）。
- ❌ **FAIL**：功能异常，需要排查。

每个测试步骤都写明了"期望输出"和"排查提示"。

---

## 1. 基础健康检查

### 1.1 /health（无需认证）

```bash
curl -s "$GW/health" | jq
```

**期望**：`data.status == "healthy"`，`dependencies.redis.status == "connected"`，12 个 plugin 全部 `enabled=true, status=healthy`。

**排查**：
- Redis disconnected → 确认 `docker ps | grep redis` 正在运行、`redis-cli ping` 返回 PONG。
- Qdrant disconnected → 语义缓存不可用但主流程不阻断（fail-open），如需 L3 请启动 qdrant 容器。

### 1.2 /metrics（Prometheus 格式）

```bash
curl -s "$GW/metrics" | head -40
```

**期望**：能看到 `aigateway_requests_total`、`aigateway_cache_hits_total`、`aigateway_request_duration_seconds_bucket` 等指标。数值可能都是 0（刚启动）。

### 1.3 OpenAI 兼容基础

```bash
curl -s "$GW/v1/models" -H "$AUTH" | jq '.data.data[].id'
```

**期望**：至少返回 `deepseek-v4-flash`、`agnes-2.0-flash`、`agnes-image-2.1-flash`、`agnes-video-v2.0` 四个模型。

---

## 2. Chat Completions（核心路径）

### 2.1 非流式 · 首次请求（未命中缓存）

```bash
curl -s "$GW/v1/chat/completions" \
  -H "$AUTH" -H "$CTYPE" \
  -d '{
    "model": "'"$MODEL"'",
    "messages": [{"role": "user", "content": "用一句话解释什么是缓存"}]
  }' | jq '.data | {id, model, choices: [.choices[0].message.content], usage}'
```

**期望**：`choices[0].message.content` 有内容，`usage.total_tokens > 0`。

**记下这次**：`total_tokens_1st` 值，稍后对比。

### 2.2 非流式 · 重复请求（应命中 L1 缓存）

**5 秒内**发**完全相同**的请求：

```bash
time curl -s "$GW/v1/chat/completions" \
  -H "$AUTH" -H "$CTYPE" \
  -d '{
    "model": "'"$MODEL"'",
    "messages": [{"role": "user", "content": "用一句话解释什么是缓存"}]
  }' -o /dev/null -w "HTTP %{http_code}  latency=%{time_total}s\n"
```

**期望**：
- 延迟 **<50ms**（vs 2.1 通常 500ms~3s）。
- 从 admin logs 查询验证 cache_hit：

```bash
curl -s "$GW/admin/logs?limit=2" -H "$AUTH" | jq '.data.items[] | {endpoint, cache_hit, tier, duration_ms}'
```

期望最新一条 `cache_hit == true`，`tier == "L1"`。

### 2.3 流式（SSE）

```bash
curl -N -s "$GW/v1/chat/completions" \
  -H "$AUTH" -H "$CTYPE" \
  -d '{
    "model": "'"$MODEL"'",
    "stream": true,
    "messages": [{"role": "user", "content": "写一首 4 行的诗"}]
  }' | head -30
```

**期望**：多条 `data: {...}` 事件持续输出，最后一条为 `data: [DONE]`。

### 2.4 流式 · 重复请求（缓存流回放）

再发一次相同 stream 请求。**期望**：SSE 事件依然按顺序返回，但速度显著更快（cache_chunk_delay_ms 控制的 20ms 间隔模拟节奏）。

---

## 3. 三级缓存分层验证

### 3.1 L1 → L2 → L3 命中路径

| 阶段 | 操作 | 期望 tier |
|------|------|-----------|
| A | 首次问 "北京是中国的首都吗？" | 无 cache_hit |
| B | 5 秒内重问相同问题 | **L1** |
| C | 等 10 秒后再重问（L1 未过期，仍 L1） | **L1** |
| D | `docker restart gateway`（清 L1）后重问 | **L2**（Redis） |
| E | 换个近似问法 "中国的首都是北京对吗？" | **L3**（语义相似，需 Qdrant） |

**A/B/C/D 逐条验证**：每次通过 `/admin/logs?limit=1` 看 `tier`。

**E 需要 Qdrant 在线**：确认 `curl -s $GW/health | jq '.data.dependencies.qdrant.status'` 为 `connected`；首次 L3 落盘约需一次真实调用产生 embedding（`min_token_count: 100` 控制门槛，参见 config.yaml `cache.l3`）。

### 3.2 L3 缓存管理

```bash
# 列出 L3 中已缓存的条目
curl -s "$GW/admin/cache/l3/entries?limit=10" -H "$AUTH" | jq '.data.items[] | {point_id, model, mode, hits, ttl_remaining}'

# 手动触发过期清理
curl -X POST -s "$GW/admin/cache/l3/cleanup" -H "$AUTH" | jq

# 删除某条
# curl -X DELETE -s "$GW/admin/cache/l3/entries/<point_id>" -H "$AUTH"
```

---

## 4. 插件管线（Understanding Pipeline）

### 4.1 PII 脱敏

```bash
curl -s "$GW/v1/chat/completions" \
  -H "$AUTH" -H "$CTYPE" \
  -d '{
    "model": "'"$MODEL"'",
    "messages": [{"role": "user", "content": "我的邮箱是 alice@example.com，手机 13800138000，请你确认收到"}]
  }' | jq '.data.choices[0].message.content'
```

**期望**：模型收到的其实是已脱敏内容（如 `<EMAIL_1>` / `<PHONE_1>`），响应里可能保留占位符或按上下文回答。

**验证脱敏日志**：

```bash
curl -s "$GW/admin/logs?limit=1" -H "$AUTH" | jq '.data.items[0].plugin_trace[] | select(.plugin=="pii_detector")'
```

期望 `matched_count >= 2`。

### 4.2 语义缓存（近似匹配）

准备两条**含义相同但字面不同**的请求：

```bash
Q1='请介绍一下 Python 编程语言的历史'
Q2='能不能讲讲 Python 这门语言是怎么发展来的'

# 先跑 Q1（产生 L3 缓存）
curl -s "$GW/v1/chat/completions" -H "$AUTH" -H "$CTYPE" \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"$Q1\"}]}" > /dev/null

# 等 3 秒（保证不是 L1）后跑 Q2
sleep 3
curl -s "$GW/v1/chat/completions" -H "$AUTH" -H "$CTYPE" \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"$Q2\"}]}" > /dev/null

# 查看 Q2 的 cache_hit
curl -s "$GW/admin/logs?limit=1" -H "$AUTH" | jq '.data.items[0] | {cache_hit, tier}'
```

**期望**：`cache_hit == true, tier == "L3"`（若阈值 0.95 未通过就是 MISS，可临时把 config `plugins[semantic_cache].config.threshold` 调到 0.85 重试）。

### 4.3 Prompt Compression（LLMLingua-2）

装了 `llmlingua` extra 才生效。给一个啰嗦的 prompt：

```bash
VERBOSE='请你详细地、非常仔细地、认真而全面地思考并回答这个问题：什么是人工智能？'
curl -s "$GW/v1/chat/completions" -H "$AUTH" -H "$CTYPE" \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"$VERBOSE\"}]}" > /dev/null

# 检查插件执行 trace
curl -s "$GW/admin/logs?limit=1" -H "$AUTH" | \
  jq '.data.items[0].plugin_trace[] | select(.plugin=="prompt_compress")'
```

**期望**：`before_tokens > after_tokens`，`compression_ratio ≈ 0.5`。

### 4.4 Model Router

发一个明显更适合大模型的复杂请求 vs 简单问候：

```bash
# 简单请求
curl -s "$GW/v1/chat/completions" -H "$AUTH" -H "$CTYPE" \
  -d '{"model":"auto","messages":[{"role":"user","content":"你好"}]}' > /dev/null

# 复杂请求
curl -s "$GW/v1/chat/completions" -H "$AUTH" -H "$CTYPE" \
  -d '{"model":"auto","messages":[{"role":"user","content":"请从算法复杂度角度分析归并排序和快速排序在最坏情况下的差异"}]}' > /dev/null

# 查看 model_router 是否分别路由到不同模型
curl -s "$GW/admin/logs?limit=2" -H "$AUTH" | jq '.data.items[] | {model, plugin_trace: [.plugin_trace[] | select(.plugin=="model_router")]}'
```

**期望**：两条日志的最终 `model` 字段可能不同（简单 → flash，复杂 → 更强模型）。

---

## 5. 多模态（Media Optimization Layer）

### 5.1 图片 URL

需要一张网络图片，比如 <https://picsum.photos/512/512>：

```bash
curl -s "$GW/v1/chat/completions" -H "$AUTH" -H "$CTYPE" \
  -d '{
    "model": "agnes-2.0-flash",
    "messages": [{
      "role": "user",
      "content": [
        {"type": "text", "text": "描述这张图片"},
        {"type": "image_url", "image_url": {"url": "https://picsum.photos/id/237/512/512"}}
      ]
    }]
  }' | jq '.data.choices[0].message.content'
```

**期望**：非空描述。

**验证 OCR/Caption 有执行**：

```bash
curl -s "$GW/admin/logs?limit=1" -H "$AUTH" | \
  jq '.data.items[0].plugin_trace[] | select(.plugin=="media_optimizer")'
```

期望有 `image_processed: 1, ocr_backend, caption_generated` 等字段。

### 5.2 图片生成（Generation Optimization）

```bash
curl -s "$GW/v1/chat/completions" -H "$AUTH" -H "$CTYPE" \
  -d '{
    "model": "agnes-image-2.1-flash",
    "messages": [{"role": "user", "content": "生成一张：夕阳下的富士山，浮世绘风格"}]
  }' | jq '.data.choices[0].message.content'
```

**期望**：返回图片 URL 或 Base64。

**验证 AI Director / Intent Evaluator**：

```bash
curl -s "$GW/admin/logs?limit=1" -H "$AUTH" | \
  jq '.data.items[0].plugin_trace[] | select(.plugin | test("ai_director|intent_evaluator|gen_model_router"))'
```

### 5.3 音频/视频（可选，需 ffmpeg + whisper）

跳过时不影响主验收。

---

## 6. 认证 & 配额 & 限流

### 6.1 Auth：错 Key / 无 Key

```bash
curl -s -o /dev/null -w "no-key: %{http_code}\n" "$GW/v1/models"
curl -s -o /dev/null -w "bad-key: %{http_code}\n" "$GW/v1/models" -H "Authorization: Bearer wrong"
curl -s -o /dev/null -w "ok-key: %{http_code}\n" "$GW/v1/models" -H "$AUTH"
```

**期望**：`no-key: 401`、`bad-key: 401`、`ok-key: 200`。

### 6.2 配额查询

```bash
# 计算 key 的 hash id（该 key 的哈希前 12 位）
KEY_HASH=$(echo -n "$KEY" | sha256sum | cut -c1-12)
curl -s "$GW/admin/quotas/$KEY_HASH" -H "$AUTH" | jq
```

或直接列所有 API Key 拿到 key_id：

```bash
curl -s "$GW/admin/api-keys" -H "$AUTH" | jq '.data.items[] | {key_id, user_id, daily_tokens_used, daily_tokens_limit}'
```

**期望**：能看到 `daily_tokens_used > 0`（前面测试消耗的）。

### 6.3 Rate Limit（IP 级，30 req/60s，见 config `rate_limiter`）

```bash
for i in $(seq 1 35); do
  curl -s -o /dev/null -w "$i: %{http_code}\n" "$GW/v1/models" -H "$AUTH"
done | tail -10
```

**期望**：前 30 条 `200`，之后开始出现 `429`。

---

## 7. Admin & 管理面

### 7.1 API Key CRUD

```bash
# 创建
NEW=$(curl -s -X POST "$GW/admin/api-keys" -H "$AUTH" -H "$CTYPE" \
  -d '{"user_id":"tester","daily_tokens":100000,"monthly_cost":5}')
echo "$NEW" | jq
NEW_KEY_ID=$(echo "$NEW" | jq -r '.data.key_id')

# 更新配额
curl -s -X PUT "$GW/admin/api-keys/$NEW_KEY_ID" -H "$AUTH" -H "$CTYPE" \
  -d '{"daily_tokens":50000}' | jq

# 删除
curl -s -X DELETE "$GW/admin/api-keys/$NEW_KEY_ID" -H "$AUTH" | jq
```

**期望**：三步都返回 `code:0`（或 `success:true`）。

### 7.2 Plugins 配置读写

```bash
# 读取
curl -s "$GW/admin/plugins-config" -H "$AUTH" | jq '.data.plugins[] | {name, enabled}'

# 关掉 prompt_cache 试试（仅测试，之后记得恢复）
curl -s -X PUT "$GW/admin/plugins-config" -H "$AUTH" -H "$CTYPE" \
  -d '{"plugins":[{"name":"prompt_cache","enabled":false}]}' | jq

# 恢复
curl -s -X PUT "$GW/admin/plugins-config" -H "$AUTH" -H "$CTYPE" \
  -d '{"plugins":[{"name":"prompt_cache","enabled":true}]}' | jq
```

### 7.3 前端页面（浏览器）

打开 `http://localhost:5173` 或 `http://localhost:3000`，逐个访问：

| 路径 | 验收点 |
|------|--------|
| `/` Overview | RPM/成功率/缓存命中率、模型 top5、最近错误 |
| `/models` | 4 个模型全部展示，编辑 pricing、modality 能保存 |
| `/plugins` | 12 个插件列表，开关可切换 |
| `/costs` | 按 user/model 汇总，图表非空 |
| `/quotas` | API Key 表格，编辑对话框可提交 |
| `/cache` | L3 条目分页，能删除单条、能触发 cleanup |
| `/logs` | 请求日志分页，能过滤 status/model |
| `/knowledge` | 上传 PDF/txt（如果开启 RAG）、检索测试 |
| `/config` | 全局配置 YAML 编辑器 |

---

## 8. Provider 连接与错误恢复

### 8.1 Provider 连通性测试

```bash
curl -s -X POST "$GW/admin/providers/deepseek/test" -H "$AUTH" | jq
curl -s -X POST "$GW/admin/providers/agnes/test" -H "$AUTH" | jq
```

**期望**：`data.success == true`。

### 8.2 熔断验证（可选，破坏性测试）

临时把某个 provider 的 `base_url` 改成不可达地址，连发 6 次以上（超过 `circuit_breaker.failure_threshold: 5`），第 7 次应立即 `CircuitBreakerOpenError`（不等待超时）。60 秒后自动 HALF-OPEN 恢复。

改配置：`config.yaml` → `providers.deepseek.base_url: http://127.0.0.1:59999`（不存在的端口），保存后 hot-reload 生效。

---

## 9. 自动化压测 & A/B token 对比

**用途**：一条命令完成"缓存打开 vs 关闭"的对照实验，量化 token 到底节省了多少。

### 9.1 使用方式

```bash
source .venv/bin/activate
pip install httpx  # 已含在 core 依赖里

# 基础用法（32 个并发，300 条请求，用 20 条 prompt 模板轮流命中缓存）
python scripts/ab_test.py \
  --base-url http://localhost:8000 \
  --api-key gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o \
  --model deepseek-v4-flash \
  --concurrency 32 \
  --requests 300 \
  --output docs/qa-evidence-$(date +%Y%m%d)

# 高级用法（自定义场景）
python scripts/ab_test.py \
  --scenario semantic     # exact | semantic | mixed | random
  --repeat-ratio 0.7      # 70% 请求是重复 prompt（用来测缓存）
  --streaming             # 测流式接口
```

### 9.2 脚本会做什么

1. **Baseline 组**：调用 `PUT /admin/plugins-config` 关掉 `prompt_cache` + `semantic_cache` + `prompt_compress`，跑一遍全量请求，记录 total_tokens / latency / cost。
2. **Optimized 组**：恢复所有插件，跑相同请求，记录 total_tokens / latency / cost。
3. **对比报告**：算出 token 节省率、延迟改善、成本节省，输出到：
   - `docs/qa-evidence-YYYYMMDD/ab-report.md`（可读报告）
   - `docs/qa-evidence-YYYYMMDD/baseline.csv` + `optimized.csv`（原始数据）
   - `docs/qa-evidence-YYYYMMDD/summary.json`（机器可读）

### 9.3 判读期望

| 场景 | 期望结果 |
|------|---------|
| `--scenario exact --repeat-ratio 0.8` | token 节省 **>60%**（L1/L2 精确命中） |
| `--scenario semantic --repeat-ratio 0.5` | token 节省 **20%-50%**（L3 语义命中） |
| `--scenario mixed`（默认） | token 节省 **>30%**，p95 latency 降低 **>40%** |
| `--scenario random` | 节省 **≈0**（无缓存机会，验证系统开销） |

---

## 10. 单元/集成回归

```bash
python -m pytest tests/ -v 2>&1 | tail -40
# 全绿即通过；已知 skip 的用例数会在末尾汇总
```

**核心必过用例**：

```bash
python -m pytest tests/test_pipeline.py \
                 tests/test_caching.py \
                 tests/test_security.py \
                 tests/test_model_router_strategy.py \
                 tests/test_generation_optimization_config_watcher.py -v
```

---

## 11. 收尾清单

- [ ] `/health` 全绿
- [ ] `/v1/models` 返回全部 provider 模型
- [ ] chat completions 非流式 + 流式各通一次
- [ ] 相同请求 L1 命中（`/admin/logs` 里 `cache_hit=true`）
- [ ] 近似请求 L3 命中
- [ ] PII 有脱敏 trace
- [ ] 图片理解回响非空
- [ ] Auth 401 / RateLimit 429 都能触发
- [ ] 前端 9 个页面全部可打开、无 console error
- [ ] `scripts/ab_test.py` 输出的报告里 token 节省率 > 30%
- [ ] `pytest tests/` 无 FAIL

全部勾选后视为该 build 可发布。
