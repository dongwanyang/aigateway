# LLM Router

AI Gateway 使用“语义分类、策略约束、运行时路由”三层设计。三层职责不可合并：

```text
Prompt
  │
  ├─ High-confidence fast rules
  │
  └─ Low-cost LLM classifier
          │
          ▼
      Task Profile
          │
          ▼
      Policy Engine
      (eligible/preferred models)
          │
          ▼
      Runtime Router
      (health/quality/latency/cost)
          │
          ▼
      Task-compatible fallback
          │
          ▼
       Provider
```

## Task Profile

分类器同时判断调用 endpoint 和文本任务特征。典型结果：

```json
{
  "generation": "understanding",
  "hint": "None",
  "task_profile": {
    "operation": "coding",
    "domain": "software",
    "modalities": ["text", "image"],
    "complexity": 72,
    "requirements": ["vision", "tool_calling"],
    "confidence": 0.86,
    "source": "llm"
  }
}
```

- `generation` 决定 chat、images 或 videos endpoint。
- `operation` 当前支持 `coding/reasoning/summary/vision/general`。
- `requirements` 是硬约束。没有模型声明所需能力时返回
  `no_model_for_policy`，不会猜测一个可能不兼容的模型。
- 高置信度明确请求使用本地 fast path；模糊请求调用低成本分类模型。
- 分类模型超时或输出无效时使用保守的本地 Task Profile。

## 模型配置

```yaml
providers:
  example:
    model_grouper:
      - models:
          - name: example-coder
            capabilities: [text]
            tasks: [coding]
            features: [tool_calling, structured_output, long_context]

task_routing:
  enabled: true
  version: "2"
  min_confidence: 0.6
  model_selection_mode: policy
  expose_debug_metadata: false
  model_preferences:
    coding: [example-coder]
```

字段含义：

- `capabilities`: endpoint 能力，支持 `text/image/video`。
- `tasks`: 语义任务能力，支持 `coding/reasoning/summary/vision/general/*`。
- `features`: 硬运行时能力，支持
  `vision/tool_calling/structured_output/long_context`。
- `model_preferences`: Policy 的排序偏好，不是最终选择。
- `model_selection_mode`:
  - `strict`: 客户端显式 model 是契约，不允许策略覆盖；
  - `policy`: 策略优先，兼容的客户端 model 作为偏好；
  - `auto`: 忽略客户端 model 偏好，完全自动选择。

未知任务、能力、未注册偏好模型及错误字段类型会导致启动失败。

## Runtime Router

Router 只在 Policy 允许的候选集中选择，依次考虑：

1. 排除处于 `OPEN` 状态的模型；
2. 模型质量分是否满足请求复杂度；
3. 最近失败次数；
4. Policy 偏好顺序；
5. 实际请求延迟 EWMA；
6. 配置价格；
7. 能力评分。

其余健康候选成为任务兼容 fallback。若所有候选都处于 OPEN 状态，
Router 会进入显式降级状态并在元数据中记录 `all_models_unhealthy`。

## 缓存与可观测性

路由策略语义参与所有缓存层：

- L1：组合后的 `pipeline_version` 参与 hash；
- L2：独立 `pipeline_version` TAG，并使用 v3 索引；
- L3：Qdrant payload/filter 包含同一版本。

修改 Task Profile、模型标签或 Policy 时必须提升
`task_routing.version`；其他管道语义变化提升 `cache.pipeline_version`。

路由 trace 包含：

- Task Profile 与分类来源；
- eligible/preferred models；
- 最终模型和 fallback；
- 被排除的熔断模型；
- Policy 与 Router 决策原因。

完整候选集默认只进入 trace。只有显式设置
`task_routing.expose_debug_metadata=true` 时才返回给客户端，避免泄露内部模型池。

内部分类和 Prompt 改写调用不启用任务路由，避免递归。
