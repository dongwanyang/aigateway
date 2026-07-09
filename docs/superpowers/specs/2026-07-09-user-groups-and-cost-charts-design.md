# 用户组 + 成本图表修复 设计

**日期:** 2026-07-09
**状态:** Draft (待用户审核)
**作者:** Brainstorming session

## 背景与动机

四个相互关联的需求:

1. **用户组(user group)** - 给用户添加用户组,Redis key 也添加用户组 id。当前系统 API-key-centric,每个 active key 1:1 对应一个 user_id;`config.yaml` 里虽有 `group` 字段,但仅作 Prometheus 标签,不持久化、不做资源隔离。
2. **Miss 柱状图改红色** - Cache 页的 L1/L2/L3/MISS 柱状图,四根柱共享一个绿色 fill,需把 MISS 柱单独改红。
3. **成本分布改为 by 用户组** - Costs 页饼图当前 by model,改为 by group。
4. **成本趋势近7天修复** - 当前前端把 `gateway_cost_total` 除以 7 伪造每日成本,需显示真实每日成本。

## 设计决策(已与用户确认)

| 维度 | 决策 |
|---|---|
| 组的作用 | 组级配额 + 个人配额(组为主、个人为辅,同步扣减) + 组内缓存共享 |
| 配额方案 | 方案 1:组级计数器与 key 级同步扣减(原子 pipeline) |
| 每日成本来源 | 方案 A:Prometheus range query `increase(gateway_cost_total[24h])`,Prom 保留期 30d 已确认 |
| 组管理 | 控制面板 CRUD,集成进配额管理页 Quotas.tsx(Tab 切换 API Keys / 用户组) |
| 组配额 + 个人配额 | 组配额 = 组内总池,个人 = 组内子限额,两边同步扣减,任一超限即拒 |
| 组 vs 租户 | 单租户部署,废弃 tenant_id 槽位,改为 group_id |
| cache_scope | 从二值(shared/private)升级为三档 **private / group / public**,按 key 配置默认 scope |
| scope 设置位置 | 编辑 key 时配置该 key 的默认 cache_scope(取代原来的"默认 shared") |
| 无组 key 处理 | 创建 key 时强制必须属于某个组 |
| 存量迁移 | 硬编码系统默认组(`default`),接收所有无组 key |

## 当前状态(代码现状)

### 配额 / KeyStore

- `aigateway-core/src/aigateway_core/shared/auth/key_store.py` - `KeyStore` 类,Redis hash per key `aigateway:key:{key_hash}`。
- 字段:`key_id, key_prefix, user_id, status, created_at, last_used_at, daily_tokens_limit/used, monthly_cost_limit/used, rate_limit_rpm/tpm, rpm_window_start/count, tpm_window_start/count, is_admin`。
- `check_quota(key_hash, tokens, cost)` @ key_store.py:494 - 查 RPM/TPM 窗口 + 日 token + 月成本。
- `increment_usage(key_hash, tokens, cost, model, ...)` @ key_store.py:566 - 更新 key hash + per-period quota hash。
- 1:1 约束:`_check_duplicate_user_key` @ key_store.py:671-688(一个 user_id 至多一个 active key)。
- `seed_from_config(keys_config)` @ key_store.py:296 - 启动时从 config.yaml 导入,**不读 `group` 字段**。
- 调用方:`dispatcher.py:359/512`(check)、`:660/841`(increment)。

### Redis key schema(现状)

| Key | 类型 | 用途 |
|---|---|---|
| `aigateway:key:{key_hash}` | Hash | key 记录 + 配额计数器 |
| `aigateway:key_lookup:{key_prefix}` | String | 前缀反查 |
| `aigateway:quota:{key_hash}:{period}` | Hash | per-period 用量(period=daily:{date}/monthly:{month}) |
| `aigateway:ratelimit:{key_hash}:rpm` | ZSet | RPM 窗口 |
| `aigateway:ratelimit:{key_hash}:tpm` | String | TPM 窗口 |
| `aigateway:cache:v2:{cache_key_hash}` | String | L2 prompt 缓存 |
| `aigateway:feature:{api_key_id}:{character_id}:{model_version}` | varies | feature 缓存(per-key) |
| `aigateway:prompt_template:{api_key_id}:{template_name}` | varies | prompt 模板(per-key) |
| `aigateway:prompt_template_index:{api_key_id}` | SET | 模板名索引 |
| `aigateway:keys:sync` | Pub/Sub | key CRUD 事件 |

### Cache key 构造(现状)

`cache_manager.py:476 generate_cache_key`,Redis key = `aigateway:cache:v2:{sha256}`:

```
SHA-256( "v2" | tenant_id | pipeline_kind | model_family | temp_bucket | mt_bucket
          [ | u=user_id (仅 scope=private) ] | normalized_prompt )
```

- `tenant_id` 默认空串(单租户)。
- `cache_scope` 默认 shared -> 不含 user_id(全员共享);private -> 含 `u=user_id`(个人隔离)。
- scope 来源 `dispatcher._resolve_cache_scope` @ dispatcher.py:67:① 请求头 `X-Cache-Scope` ② PII 命中强制 private ③ 默认 shared。**config/控制台均无设置入口。**

### Admin API(现状)

`admin_routes.py:229-245`:`CreateApiKeyRequest(user_id, daily_tokens, monthly_cost, rate_limit_rpm, rate_limit_tpm)`、`UpdateQuotaRequest`。无 group 字段。端点:GET/POST/DELETE/PUT `/admin/api-keys`。

### 控制面板(现状)

`Quotas.tsx` - 创建表单(user_id + 4 配额字段)、编辑表单(4 配额字段)、key 列表表格。无 group 字段。

### 三个图表(现状)

- **Miss 柱状图** `Cache.tsx:263-271` - 单个 `<Bar fill="var(--color-success)">`,四柱共享绿色。数据 `gateway_cache_misses_total` + `gateway_cache_hits_total{tier}`。
- **成本分布饼图** `Costs.tsx:118-147` - by model,数据 `gateway_cost_by_model_total`。颜色 `CHART_COLORS`。
- **成本趋势 7 天** `Costs.tsx:36-47` - **伪造**:`gateway_cost_total / 7` 均摊到 7 天。后端无每日成本序列。

### 成本记录(现状)

`metrics.py:340 record_cost(cost_usd, model, user_id)` - 累加 `gateway_cost_total`(gauge)、`gateway_cost_by_model{model}`、`gateway_cost_by_user{user_id}`。**无 group label。** 调用方 dispatcher.py:674/836、streaming/metrics_wrapper.py:47。

### Prometheus

`docker-compose.yml:105` `--storage.tsdb.retention.time=30d`,保留期 30 天(>7 天,方案 A 可行)。

---

## 详细设计

### §1 数据模型与 Redis key schema

#### 1.1 组实体(Group)

新增 Group 实体,存 Redis。字段:
- `group_id` - `grp-{slug}`(slug 来自组名,人可读;冲突时加后缀)
- `name` - 组显示名(唯一)
- `status` - active / suspended
- `created_at`, `updated_at`
- 组配额上限:`daily_tokens_limit`, `monthly_cost_limit`, `rate_limit_rpm`, `rate_limit_tpm`(与 key 级同构)
- 组级已用:`daily_tokens_used`, `monthly_cost_used`, `rpm_window_start`, `rpm_window_count`, `tpm_window_start`, `tpm_window_count`(与 key 级同构)

#### 1.2 新增 Redis key

| Key | 类型 | 用途 |
|---|---|---|
| `aigateway:group:{group_id}` | Hash | 组记录 + 组级已用计数器(同构于 `aigateway:key:{hash}`) |
| `aigateway:group_lookup:{name}` | String | 组名 -> group_id 反查(组名唯一) |
| `aigateway:group:{group_id}:members` | SET | 成员 key_hash 集合(列表/聚合/换组迁移) |
| `aigateway:quota:{group_id}:{period}` | Hash | 组级历史用量(同构于 per-key quota hash) |
| `aigateway:ratelimit:{group_id}:rpm` | ZSet | 组级 RPM 窗口 |
| `aigateway:ratelimit:{group_id}:tpm` | String | 组级 TPM 窗口 |
| `aigateway:groups:index` | SET | 所有 group_id(列表/扫描) |
| `aigateway:groups:sync` | Pub/Sub | 组 CRUD 事件(跨实例同步,对应 `aigateway:keys:sync`) |

#### 1.3 Key 记录变更

`aigateway:key:{key_hash}` Hash 新增字段:
- `group_id` - 所属组(强制非空,见 §6 迁移)
- `cache_scope` - 该 key 的默认 cache scope(`private` / `group` / `public`,默认 `group`)

#### 1.4 Cache key 构造(修订)

废弃 `tenant_id` 参数,改用 `group_id`。三档 scope:

```
SHA-256( "v2" | pipeline_kind | model_family | temp_bucket | mt_bucket
          [ scope 段 ] | normalized_prompt )
```

- `public`:不加 scope 段(全员共享,≈ 原 shared)
- `group`:加 `g={group_id}`(组内共享,组间隔离)
- `private`:加 `u={user_id}`(个人隔离)

因 key 强制有 group_id(§6),group scope 一定有 group_id 可用,无需 fallback。

> 调用方现状:两处调用 `generate_cache_key` -- `dispatcher.py:305` 和 `prefix/cache/plugin.py:46`。两者**当前都不传 tenant_id**(默认空串,即 tenant_id 实际从未被使用)。改造时两处都需补传 `group_id`:dispatcher 从 `request.state.api_key_data` 取;plugin 从 `ctx`(PipelineContext)取(需把 group_id 透传进 ctx.extra 或 ctx 字段)。
>
> 测试:`tests/test_cache_key_v2.py:186-188` 有"不同 tenant_id 生成不同 key"用例,需改为 group_id 隔离用例(scope=group 时不同组不同 key)。

#### 1.5 feature / prompt_template 缓存

从 per-api-key 改为按 scope 决定隔离维度:
- `public`:`aigateway:feature::{character}:{model}`(全局)
- `group`:`aigateway:feature:{group_id}:{character}:{model}`
- `private`:`aigateway:feature:{user_id}:{character}:{model}`
- prompt_template / prompt_template_index 同理。

无组 key 已不存在(§6 强制),无需 per-key fallback。

### §2 配额检查与扣减(方案 1 核心)

#### 2.1 扣减流程 increment_usage

`increment_usage(key_hash, tokens, cost, model, ...)` 改为:
1. 它已在内部 `redis.get_api_key(key_hash)` 拿到 key 的 `data` 字典(含新增的 `group_id` 字段),直接取 `group_id`,无需新增参数、无需读 `request.state`。
2. **原子 pipeline** 同时更新:
   - key 级:`aigateway:key:{hash}` 计数器 + `aigateway:quota:{hash}:{period}` + `aigateway:ratelimit:{hash}:{rpm/tpm}`
   - 组级(有 group_id 时):`aigateway:group:{group_id}` 计数器 + `aigateway:quota:{group_id}:{period}` + `aigateway:ratelimit:{group_id}:{rpm/tpm}`
3. 用 Redis pipeline / MULTI 一次往返。组级失败只记日志不阻塞请求(宁可少计不阻塞)。

> 注:dispatcher 调用方(dispatcher.py:660/841)不传 group_id,KeyStore 自取。`record_cost`(metrics 层)则由 dispatcher 传 group(dispatcher 已有 `user_id` 和 `request.state.api_key_data`,见 §5.2)。

#### 2.2 检查流程 check_quota

改为(同样已在内部 `redis.get_api_key(key_hash)` 拿到 `data`,含 `group_id`):
1. 从 `data` 取 `group_id`。
2. 有 group_id 时,先查组级四维(组 RPM/TPM/日token/月成本,读 `aigateway:group:{group_id}`)-> 任一超限返回组级超限错误。
3. 再查 key 级四维(读 `data` 自身)-> 任一超限返回个人超限错误。
4. 两者都过才放行。无 group_id 时维持现有仅 key 级行为(存量迁移后不存在,但防御性保留)。

#### 2.3 错误码区分

dispatcher.py:373-380 已按 fail_msg 关键字分流,新增:
- 组级:`quota_exceeded_group_daily_tokens` / `quota_exceeded_group_monthly_cost` / `rate_limit_group_rpm` / `rate_limit_group_tpm`
- 个人级:沿用现有 `quota_exceeded_daily_tokens` 等

#### 2.4 例子验证

组配 5000,A 配 200 用 50,B 配 100 用 20:
- A 请求:扣 A 个人 +50(50/200 ✓),扣组 +50(50/5000 ✓)-> 放行
- B 请求:扣 B 个人 +20(20/100 ✓),扣组 +20(组已用 70/5000 ✓)-> 放行
- 组已用 = 70 = A+B 之和(同步扣减保证)
- A 个人满 200 但组有余 -> 个人超限拒 ✓
- 组满 5000 但 A 个人未满 -> 组超限拒 ✓

#### 2.5 一致性边界

组级计数器极端情况可能与 SUM(成员)有微小偏差(单边写入失败),偏差偏少计(不致多用),可接受。不引入对账任务(YAGNI)。

### §3 组 CRUD(admin API + 控制面板)

#### 3.1 新增 admin 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/admin/groups` | 列出所有组(分页,含组配额、已用、成员数) |
| POST | `/admin/groups` | 创建组(CreateGroupRequest: name, daily_tokens, monthly_cost, rate_limit_rpm, rate_limit_tpm) |
| GET | `/admin/groups/{group_id}` | 组详情(含成员列表) |
| PUT | `/admin/groups/{group_id}` | 更新组配额/状态(UpdateGroupRequest) |
| DELETE | `/admin/groups/{group_id}` | 删除组(仅当无成员;有成员拒绝,要求先迁移) |
| PUT | `/admin/api-keys/{key_id}/group` | 给 key 分配/更换组(body: group_id) |

#### 3.2 GroupStore

新建 `shared/auth/group_store.py`(单一职责,key_store.py 已 688 行偏大)。方法:`create_group / list_groups / get_group / update_group / delete_group / assign_key_to_group`。复用现有 Redis client 与 Pub/Sub 同步模式。

#### 3.3 assign_key_to_group 迁移逻辑

key 从组 A 换到组 B:
- 从 `aigateway:quota:{hash}:{period}` 读该 key 当期已用,反向 HINCRBY 到 A,正向 HINCRBY 到 B。
- 日 token / 月成本迁移;RPM/TPM 窗口不迁移(短期窗口,换组罕见,自然过期)。
- 更新 `aigateway:key:{hash}.group_id` + 两个组的 members SET。

#### 3.4 控制面板(Quotas.tsx,Tab 切换布局)

页面顶部 Tab:`[API Keys] [用户组]`。

**用户组 Tab:**
- 组列表表格:组名、组配额(四维)、组已用、成员数、操作(编辑/删除)
- 创建/编辑组表单:组名 + 四个配额字段(复用现有编辑表单样式)
- 删除组:仅当无成员允许,有成员提示先迁移

**API Keys Tab:**
- 创建 key 表单新增"用户组"下拉(必选,从 `/admin/groups` 拉)
- 创建 key 表单新增"缓存共享范围"下拉(private/group/public,默认 group)
- key 列表表格新增"用户组"列、"缓存范围"列
- 编辑 key 表单新增"更换用户组"、"缓存共享范围"

#### 3.5 types.ts

新增 `Group` / `CreateGroupRequest` / `UpdateGroupRequest` 类型。`ApiKey` 类型加 `group_id` / `group_name` / `cache_scope`。`CreateApiKeyRequest` 加 `group_id`(必填) / `cache_scope`。

#### 3.6 config.yaml 兼容

现有 `auth.api_keys[].group` 字段:seed_from_config 时,若同名组不存在则自动创建组并分配。向后兼容 `group: admin-team` -- 启动时确保 `admin-team` 组存在。模板注释更新:group 字段现在会真正创建组并做配额/缓存隔离,不再是"仅标签"。

### §4 cache_scope 三档与运行时融合

#### 4.1 三档语义

| scope | 缓存共享范围 | cache key 维度 | 适用场景 |
|---|---|---|---|
| private | 仅本人 | `u=user_id` | 敏感数据,严格个人隔离 |
| group | 组内成员共享 | `g=group_id` | 组内协作复用,组间隔离(默认) |
| public | 全系统共享 | 无用户/组段 | 通用问答,最大化命中率 |

#### 4.2 运行时优先级(_resolve_cache_scope 修订)

1. 请求头 `X-Cache-Scope`(最高优先,调用方显式覆盖;接受 private/group/public)
2. PII 命中 -> 强制 private(安全底线)
3. key 上配置的默认 scope(`aigateway:key:{hash}.cache_scope`,默认 group)
4. 都没有 -> group(因 key 强制有组)

> 注意:scope 解析有**两条路径**需统一:
> - `dispatcher._resolve_cache_scope` @ dispatcher.py:67 -- 主路径(请求头/PII/默认),决定后传给 `generate_cache_key`。
> - `prefix/cache/plugin.py:45` -- 从 `ctx.extra.get("cache_scope")` 读,默认 "shared",独立于 dispatcher 逻辑。
>
> 改造:dispatcher 解析出三档 scope 后写入 `ctx.extra["cache_scope"]`,plugin 读取时已是最终权衡值;或 plugin 改为从 ctx 取 key 的默认 scope。两条路径须收敛到同一三档语义,默认值从 "shared" 改为 "group"。

#### 4.3 scope 一致性

group scope 要求 key 有 group_id(§6 强制保证)。public/private 不依赖 group。无 fallback 分支。

### §5 三个图表改动

#### 5.1 Miss 柱状图改红色(Cache.tsx:263-271)

用 recharts `<Cell>` 逐根着色:
```tsx
<Bar dataKey="hits" radius={[4, 4, 0, 0]}>
  {chartData.map((entry) => (
    <Cell fill={entry.tier === 'MISS' ? 'var(--color-danger)' : 'var(--color-success)'} />
  ))}
</Bar>
```
数据源不变。

#### 5.2 成本分布改为 by 用户组(Costs.tsx:118-147)

**后端:** `MetricsCollector` 新增 `gateway_cost_by_group` Counter(labelnames=`["group"]`)。`record_cost()` 加 `group` 参数,从 `request.state.api_key_data` 取 group_id(无组时 label=`"ungrouped"`,但 §6 后无组 key 不存在)。dispatcher:660/841、streaming/metrics_wrapper.py:47 调用处补传 group。

**前端:** Costs.tsx 饼图数据源从 `gateway_cost_by_model_total` 改为 `gateway_cost_by_group_total`,字段映射 `labels.group`。颜色复用 `CHART_COLORS`。直接替换 by-model(YAGNI)。

#### 5.3 成本趋势近7天修复(Costs.tsx:36-47,方案 A)

**后端:** 新增通用 Prom 查询代理端点 `GET /admin/metrics-query?query=...&start=...&end=...&step=...`,代理到 Prometheus HTTP API `/api/v1/query_range`。后端访问内网 Prom(localhost:9090 或容器名)。返回 Prom JSON。

**前端:** 删除伪造逻辑。Costs.tsx 改为调 `/admin/metrics-query`:
- query=`increase(gateway_cost_total[24h])`,start=7天前,end=now,step=86400
- 解析 7 个每日增量数据点渲染柱状图
- 按组拆分(可选,后续):`increase(gateway_cost_by_group_total{group="X"}[24h])`(依赖 §5.2 的 group label)

#### 5.4 依赖关系

§5.3 按组拆分趋势依赖 §5.2 的 group label 先落地。§5.2 的 group label 依赖 §1.3 key 上有 group_id。整体顺序:组数据模型 -> group label -> 图表。

### §6 迁移与启动

#### 6.1 强制属于组

创建 key 时 `group_id` 必填(表单下拉必选)。不再允许无组 key。

#### 6.2 硬编码默认组

系统默认组 `group_id=default`,name=`default`。启动时(KeyStore init)确保该组存在:
- 若 `aigateway:group:default` 不存在,创建之(配额用 config `auth.defaults`)。
- 扫描所有 `aigateway:key:*`,对无 `group_id` 字段的 key,设 `group_id=default` 并加入 default 组 members。
- default 组不可删除(系统组)。

#### 6.3 config.yaml seed 迁移

seed_from_config 读 `group` 字段:若组不存在则创建(配额用 config defaults),key 归入。现有 `group: admin-team` -> 启动时创建 `admin-team` 组。

#### 6.4 缓存 key 版本

cache key 构造变了(去 tenant_id、加 group/scope 段),会产生新 key。旧 `aigateway:cache:v2:*` 自然过期(L2 TTL ~3600s)。feature/template 缓存无 TTL,迁移后首次重建(可接受)。无需主动 purge。

---

## 涉及文件

**后端:**
- `aigateway-core/src/aigateway_core/shared/auth/group_store.py` (新建)
- `aigateway-core/src/aigateway_core/shared/auth/key_store.py` (create/seed 加 group_id+cache_scope;check_quota/increment_usage 加组级;assign_key_to_group)
- `aigateway-core/src/aigateway_core/shared/metrics.py` (新增 gateway_cost_by_group Counter;record_cost 加 group 参数)
- `aigateway-core/src/aigateway_core/dispatch/dispatcher.py` (record_cost/increment_usage 传 group;_resolve_cache_scope 三档;cache key 传 group_id)
- `aigateway-core/src/aigateway_core/prefix/cache/cache_manager.py` (generate_cache_key 去 tenant_id 加 group_id;三档 scope)
- `aigateway-core/src/aigateway_core/pipelines/generation/token/feature_cache.py` (per-scope 隔离)
- `aigateway-core/src/aigateway_core/pipelines/generation/token/prompt_template_manager.py` (per-scope 隔离)
- `aigateway-core/src/aigateway_core/route/streaming/metrics_wrapper.py` (record_cost 传 group)
- `aigateway-api/src/aigateway_api/admin_routes.py` (组 CRUD 端点 + metrics-query 代理端点 + CreateApiKeyRequest 加 group_id/cache_scope)
- `aigateway-api/src/aigateway_api/auth_middleware.py` (api_key_data 已含 group_id,确认透传)

**前端:**
- `control-panel/src/pages/Quotas.tsx` (Tab 切换 + 组管理 + key 表单加 group/scope)
- `control-panel/src/pages/Cache.tsx` (MISS 柱红色)
- `control-panel/src/pages/Costs.tsx` (饼图 by group + 趋势调 metrics-query)
- `control-panel/src/api/client.ts` (新增 getGroups/createGroup/... + metricsQuery)
- `control-panel/src/types.ts` (Group/CreateGroupRequest/UpdateGroupRequest + ApiKey 扩展)

**配置/文档:**
- `config.yaml` / `config.yaml.template` (注释更新)
- `docs/DB_SCHEMA.md` (新增组 key schema、cache key v2 变更)

## 测试

- `tests/test_group_store.py` (新建):组 CRUD、members、换组迁移计数器
- `tests/test_group_quota.py` (新建):组级+个人级同步扣减、超限拒绝、错误码
- `tests/test_cache_key_v2.py` (扩展):三档 scope 的 cache key 构造
- `tests/test_metrics.py` (扩展):gateway_cost_by_group label
- 手动验证:控制面板组 CRUD、key 分组、三个图表、趋势按组拆分

## 不做(YAGNI)

- 组级对账任务(偏差可接受)
- cache_scope 的 config 全局默认(改为 per-key 配置)
- 多租户(单租户,tenant_id 废弃)
- 成本分布保留 by-model 切换(直接替换)
- 趋势按组拆分作为首期必须(依赖 group label,后续可选)
