# Draft worker 丢失与 ComfyUI 进度同步的根因分析报告

日期：2026-08-04

## 结论摘要

这次问题的根因不是“前端无限轮询”，而是两条不同的失败路径在同一张草稿卡片上被混用了：

1. 草稿 worker 丢失（`draft_worker_lost`）
   - 这是后端在只读同步点（`sync_draft_runtime_state()`）做的“失败闭环”。
   - 触发条件：草稿已处于 `running/queued/refining`，但没有 `comfy_prompt_id`，且超出 stale grace、后台任务也不存在时，后端会判定为 worker 丢失并把草稿置成失败。
   - 结果：浏览器轮询会收到失败信号，前端应立即停止继续等待。

2. ComfyUI 真实进度来源（`progress_source == "comfyui"`）
   - 这是前端展示真实采样百分比与节点执行名的前提。
   - 若后端只返回 `stage`/`progress` 但没带 `progress_source="comfyui"`，前端会按“非真实进度”处理，展示为不确定动画（`aria-valuenow` 缺失），而不是 `60%` 这种真实百分比。
   - 所以“看起来像进度丢失”，可能是来源字段缺失，而非整体任务真的失败。

## 证据链

### 1) 后端已实现 worker 丢失的强制收口

文件： [aigateway-core/src/aigateway_core/pipelines/generation/draft/_draft_generator_impl.py](../../aigateway-core/src/aigateway_core/pipelines/generation/draft/_draft_generator_impl.py#L1144-L1198)

关键事实：

- `sync_draft_runtime_state()` 会在草稿处于 `generating/queued/running/refining` 时做一次运行时重连。
- 如果 `comfy_prompt_id` 缺失，并且：
  - 草稿已经足够老（超过 stale grace）
  - 当前无任何 `draft-generate-* / draft-regenerate-* / draft-confirm-*` 的活跃后台任务
- 那么它会对草稿调用 `_mark_in_progress_draft_lost(...)`，并写入错误码：`draft_worker_lost`。

这条逻辑的设计意图非常明确：

> 浏览器轮询可能已经超过 Python 的原始后台任务生命周期；如果没有重新同步到真实 ComfyUI 任务，就不能继续把草稿“伪装成还在跑”。

### 2) 预览/状态只读接口会主动执行这个同步

文件： [aigateway-core/src/aigateway_core/pipelines/generation/draft/_draft_generator_impl.py](../../aigateway-core/src/aigateway_core/pipelines/generation/draft/_draft_generator_impl.py#L1144-L1198)

与读接口的关联：

- 正常状态读取路径会经过 `get_draft()` / `sync_draft_runtime_state()`。
- 预览接口在返回 202 的过程中不会继续假设草稿还活着；它会读同步后的状态，并在发现 `draft_worker_lost` 时直接收口为失败。

这说明：

- worker 丢失不是前端单独猜出来的，
- 而是后端有一套“只读重连 + fail-closed”机制来收口。

### 3) 前端只在真正的失败信号上停轮询

文件： [control-panel/src/services/chatRuntime.ts](../../control-panel/src/services/chatRuntime.ts#L74-L115)

关键事实：

- `pollDraftUntilSettled()` 在预览轮询中会把以下错误码视为可终止错误：
  - `draft_failed`
  - `draft_worker_lost`
  - `comfyui_job_lost`
  - `comfyui_recovery_failed`
  - `comfyui_*`
  - `draft_cancelled`
- 这意味着前端本身不会“继续死等”一个已经被后端判定失效的草稿。

### 4) 前端的“进度显示”只有在 `progress_source == "comfyui"` 时才会显示真实百分比

文件： [control-panel/src/components/chat/DraftCard.tsx](../../control-panel/src/components/chat/DraftCard.tsx#L13-L38)

关键事实：

- `hasRealComfyProgress = draft.progressSource === 'comfyui'`
- 只有实时 `progress_source == "comfyui"` 时，UI 才会把当前进度展示成 `xx%`。
- 如果是 `stage` 来源，UI 只会显示“状态文本 + indeterminate progress bar”，不会声称实时采样已完成到某个比例。

这解释了为什么同一个“正在运行”的草稿，可能在 UI 上表现为：

- 真实 ComfyUI 进度：`60% · 采样 6/12`
- 仅阶段性状态：`ComfyUI 正在生成草稿预览…`，并带一个不可判定的动画进度条

### 5) 后端意图明确区分“真实 ComfyUI 进度”和“后置 stage 进度”

文件： [tests/unit/pipeline/test_comfyui_progressive_workflow.py](../../tests/unit/pipeline/test_comfyui_progressive_workflow.py#L640-L689)

关键事实：

- `test_comfyui_executing_resets_previous_node_progress()` 断言：
  - `progress==0.0`
  - `stage == "executing vae-decode"`
  - `generation_params["progress_source"] == "comfyui"`
- `test_postprocess_stage_is_indeterminate_not_invented_percentage()` 断言：
  - `stage == "finalizing"`
  - `progress_source == "stage"`
  - UI 不能据此伪造 `100%`。

所以当前代码的分界是清晰的：

- `comfyui` → 真实采样/节点进度
- `stage` → 仅阶段文案，不代表真实百分比

## 已验证的回归测试

### A. worker 丢失会被判定为失败，不再继续挂住浏览器

命令：

```bash
cd /home/ubuntu/aigateway && source .test-venv/bin/activate && python3 -m pytest tests/unit/pipeline/test_draft_generator_strategy.py -q -k 'sync_marks_stale_running_draft_without_prompt_failed or sync_keeps_stale_draft_while_owned_worker_is_waiting'
```

结果：`2 passed, 41 deselected in 0.93s`

验证点：

- `sync_marks_stale_running_draft_without_prompt_failed()` 证明：无 `prompt_id` 且 worker 消失时，草稿会被标成失败并且错误码是 `draft_worker_lost`。
- `sync_keeps_stale_draft_while_owned_worker_is_waiting()` 证明：真实还在等待 GPU lease 的 worker，不应被误报为丢失。

### B. 只读 preview 路径会提前重走同步并返回失效终止

命令：

```bash
cd /home/ubuntu/aigateway && source .test-venv/bin/activate && python3 -m pytest tests/unit/admin/test_draft_routes.py -q -k 'preview_syncs_lost_running_draft_before_returning_progress'
```

结果：`1 passed, 27 deselected in 1.20s`

验证点：

- 预览接口会先调用 `strategy.sync_draft_runtime_state('lost-draft')`。
- 当它返回 `failed + draft_worker_lost` 时，接口直接抛 `410` 并返回该错误码。
- 说明浏览器不会继续轮询一个已经被后端判定为 worker 丢失的草稿。

### C. ComfyUI 恢复逻辑会保留真正的底层根因

命令：

```bash
cd /home/ubuntu/aigateway && source .test-venv/bin/activate && python3 -m pytest tests/unit/pipeline/test_comfyui_recovery_errors.py -q
```

结果：`3 passed in 0.75s`

验证点：

- `comfyui_gpu_out_of_memory` 会被保留在草稿错误中，并额外写入 `recovery_error = "comfyui_recovery_failed"`。
- 这说明系统在“真实执行异常”和“恢复失败”之间做了区分，而不是把所有失败都吞成同一个错误。

### D. 前端 UI 对真实 ComfyUI 进度与非真实阶段进度的显示边界已被测试锁定

文件： [control-panel/src/components/components.integration.test.tsx](../../control-panel/src/components/components.integration.test.tsx#L304-L378)

关键断言：

- 无 `progressSource='comfyui'` 时，UI 不应声称有真实 `60%` 进度。
- 有 `progressSource='comfyui'` 时，UI 才会显示 `60%` + `采样 6/12`。
- `stage='finalizing'` 时，不应出现伪造的 `100%`。

## 根因总结

### 根因 1：草稿 worker 生命周期与 ComfyUI 任务生命周期并非总能一一对应

在 `sync_draft_runtime_state()` 的设计中，草稿可能已进入 `running`，但实际上后台工作线程已经消失、或者没有真正提交到 ComfyUI。此时它会触发 `draft_worker_lost`，而不是继续假设任务仍在跑。

### 根因 2：前端把“状态进度”和“真实 ComfyUI 进度”当成了一件事

前端在 `DraftCard` 中会读 `progressSource`；只有当它来自 `comfyui`，UI 才会把百分比上报成真实采样进度。否则就会退化为“只展示阶段性文案 + 炫动进度条”。

### 根因 3：真正的执行失败（例如 OOM）和同步时的恢复失败（例如历史结果恢复失败）是两回事

后端用 `comfyui_gpu_out_of_memory` / `comfyui_recovery_failed` 做了区分，说明问题根因应该定位在执行阶段、恢复阶段和同步阶段的边界，而不是简单归纳为“draft worker 丢失”。

## 结论

这次回归分析的事实证据表明：

- “草稿 worker 丢失”的失败路径是已经定义且可验证的。
- “ComfyUI 真实采样进度不展示”并不是同一类根因，而是 `progress_source` 字段缺失/不匹配带来的 UI 表现差异。
- 目前代码已经在测试层把这两种路径区别得很清楚，标明了前端只应在明确的失败码或真实 `comfyui` 进度来源下做状态收口。

## 仅保留为后续修复的可操作建议

1. 如果用户反馈是“草稿一直卡住但没有明确失败”，优先检查 `comfy_prompt_id` 是否丢失、是否经过长期不活跃同步。
2. 如果用户反馈是“进度条一直动但百分比没涨”，优先检查 `progress_source` 是否真的为 `comfyui`，而不是仅仅 `stage`。
3. 如果用户反馈是“确认后直接报错”，优先检查 `confirm_draft()` 分支是否真的走到了图片/视频的正确结果路径，而不是保留了旧的落盘或恢复路径。
