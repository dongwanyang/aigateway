from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(relative: str, old: str, new: str) -> None:
    path = ROOT / relative
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"expected block not found in {relative}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def append_once(relative: str, marker: str, addition: str) -> None:
    path = ROOT / relative
    text = path.read_text(encoding="utf-8")
    if marker in text:
        return
    path.write_text(text.rstrip() + "\n\n" + addition.strip() + "\n", encoding="utf-8")


# 1. Correct GPU status semantics: device availability and Torch CUDA context
# initialization are separate states.
replace_once(
    "control-panel/src/pages/Config.tsx",
    """interface ComfyStatusView {\n  available?: boolean\n  public_url?: string\n  manager_url?: string\n  queue?: { running?: number; pending?: number } | null\n  configuration_status?: string\n  configuration_errors?: unknown\n  error?: string | null\n}\n\ninterface GenerationPresetView {""",
    """interface ComfyStatusView {\n  available?: boolean\n  public_url?: string\n  manager_url?: string\n  queue?: { running?: number; pending?: number } | null\n  configuration_status?: string\n  configuration_errors?: unknown\n  error?: string | null\n}\n\ninterface GatewayGpuStatusView {\n  available?: boolean\n  torch_initialized?: boolean\n  allocated_bytes?: number\n  reserved_bytes?: number\n  error?: string | null\n}\n\ninterface GenerationPresetView {""",
)
replace_once(
    "control-panel/src/pages/Config.tsx",
    """  const comfyStatus = comfyQuery.data as ComfyStatusView | undefined\n  const comfyConfigurationErrors = stringList(comfyStatus?.configuration_errors)""",
    """  const comfyStatus = comfyQuery.data as ComfyStatusView | undefined\n  const gatewayStatus = gpuQuery.data?.gateway as GatewayGpuStatusView | undefined\n  const comfyConfigurationErrors = stringList(comfyStatus?.configuration_errors)""",
)
replace_once(
    "control-panel/src/pages/Config.tsx",
    """              {gpuQuery.data.gateway.available ? (\n                <div className=\"text-xs\" style={{ color: 'var(--color-text-tertiary)' }}>\n                  allocated {formatBytes(gpuQuery.data.gateway.allocated_bytes)} · reserved {formatBytes(gpuQuery.data.gateway.reserved_bytes)}\n                </div>\n              ) : (\n                <div role=\"status\" className=\"space-y-1\">\n                  <div className=\"text-xs font-medium\" style={{ color: 'var(--color-text-secondary)' }}>未初始化 CUDA</div>\n                  <div className=\"text-xs\" style={{ color: 'var(--color-text-tertiary)' }}>\n                    Gateway 当前未建立 CUDA 上下文，以避免空闲占用 ComfyUI 显存；这不表示 GPU 或驱动不可用。\n                  </div>\n                </div>\n              )}""",
    """              {gatewayStatus?.torch_initialized ? (\n                <div className=\"text-xs\" style={{ color: 'var(--color-text-tertiary)' }}>\n                  allocated {formatBytes(gatewayStatus.allocated_bytes)} · reserved {formatBytes(gatewayStatus.reserved_bytes)}\n                </div>\n              ) : gatewayStatus?.available ? (\n                <div role=\"status\" className=\"space-y-1\">\n                  <div className=\"text-xs font-medium\" style={{ color: 'var(--color-text-secondary)' }}>未初始化 CUDA</div>\n                  <div className=\"text-xs\" style={{ color: 'var(--color-text-tertiary)' }}>\n                    GPU 设备可用，但 Gateway 尚未建立 CUDA 上下文，以避免空闲占用 ComfyUI 显存。\n                  </div>\n                </div>\n              ) : (\n                <div role=\"alert\" className=\"space-y-1\">\n                  <div className=\"text-xs font-medium\" style={{ color: 'var(--color-danger)' }}>GPU 状态不可用</div>\n                  <div className=\"text-xs\" style={{ color: 'var(--color-text-tertiary)' }}>\n                    无法通过 nvidia-smi 或 PyTorch 获取设备状态{gatewayStatus?.error ? `：${gatewayStatus.error}` : '。'}\n                  </div>\n                </div>\n              )}""",
)

(ROOT / "control-panel/src/pages/GpuStatus.uninitialized.test.tsx").write_text(
    """import { QueryClient, QueryClientProvider } from '@tanstack/react-query'\nimport { cleanup, render, screen } from '@testing-library/react'\nimport { afterEach, beforeEach, expect, it, vi } from 'vitest'\nimport Config from './Config'\n\nconst state = vi.hoisted(() => ({\n  gateway: {\n    available: true,\n    torch_initialized: false,\n    allocated_bytes: 0,\n    reserved_bytes: 0,\n    device_used_bytes: 500,\n    device_free_bytes: 15_500,\n    device_total_bytes: 16_000,\n    error: null as string | null,\n  },\n}))\n\nconst api = vi.hoisted(() => ({\n  getComfyUIStatus: vi.fn(async () => ({ data: { available: true, public_url: '', manager_url: '', queue: { running: 0, pending: 0 }, configuration_errors: [] }, message: 'success' })),\n  getGenerationPresets: vi.fn(async () => ({ data: [], message: 'success' })),\n  getGpuStatus: vi.fn(async () => ({\n    data: {\n      gateway: { ...state.gateway },\n      comfyui: { available: true, memory: { total_bytes: 16_000, free_bytes: 15_500, used_bytes: 500 } },\n      queue: { running: 0, pending: 0 },\n      queue_idle: true,\n      shared_gpu: true,\n      diagnosis: [],\n    },\n    message: 'success',\n  })),\n  releaseGpuMemory: vi.fn(async () => ({ data: {}, message: 'success' })),\n}))\nvi.mock('@/api/client', () => api)\n\nfunction renderConfig() {\n  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })\n  render(<QueryClientProvider client={client}><Config /></QueryClientProvider>)\n}\n\nbeforeEach(() => {\n  state.gateway = {\n    available: true,\n    torch_initialized: false,\n    allocated_bytes: 0,\n    reserved_bytes: 0,\n    device_used_bytes: 500,\n    device_free_bytes: 15_500,\n    device_total_bytes: 16_000,\n    error: null,\n  }\n  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {\n    const url = String(input)\n    if (url.endsWith('/admin/config/schema')) return Response.json({ data: { items: [] }, message: 'success' })\n    if (url.endsWith('/admin/config')) return Response.json({ data: { server: { port: 8000 } }, message: 'success', revision: 'r1' })\n    throw new Error(`unexpected request: ${url}`)\n  }))\n})\n\nafterEach(() => {\n  cleanup()\n  vi.unstubAllGlobals()\n})\n\nit('shows an uninitialized CUDA context when the GPU device is healthy', async () => {\n  renderConfig()\n  expect(await screen.findByText('未初始化 CUDA')).toBeInTheDocument()\n  expect(screen.getByText(/GPU 设备可用/)).toBeInTheDocument()\n  expect(screen.queryByText('GPU 状态不可用')).not.toBeInTheDocument()\n})\n\nit('shows a real GPU status failure separately from an uninitialized context', async () => {\n  state.gateway.available = false\n  state.gateway.error = 'gpu_status_unavailable'\n  renderConfig()\n  expect(await screen.findByText('GPU 状态不可用')).toBeInTheDocument()\n  expect(screen.getByText(/gpu_status_unavailable/)).toBeInTheDocument()\n  expect(screen.queryByText('未初始化 CUDA')).not.toBeInTheDocument()\n})\n\nit('shows allocator counters after Torch initializes CUDA', async () => {\n  state.gateway.torch_initialized = true\n  state.gateway.allocated_bytes = 1024\n  state.gateway.reserved_bytes = 2048\n  renderConfig()\n  expect(await screen.findByText(/allocated 1 KiB · reserved 2 KiB/)).toBeInTheDocument()\n  expect(screen.queryByText('未初始化 CUDA')).not.toBeInTheDocument()\n})\n""",
    encoding="utf-8",
)

# 2. Explicit Qwen requests must respect the deployment disable switch.
replace_once(
    "aigateway-core/src/aigateway_core/pipelines/generation/draft/_draft_generator_impl.py",
    """    def _validate_qwen_image_models(self) -> tuple[str, str, str]:\n        config = self._comfyui_config\n        approved = (""",
    """    def _validate_qwen_image_models(self) -> tuple[str, str, str]:\n        config = self._comfyui_config\n        if not config.qwen_image_enabled:\n            raise DraftWorkflowError(\"comfyui_qwen_image_disabled\")\n        approved = (""",
)
replace_once(
    "aigateway-core/src/aigateway_core/pipelines/generation/draft/_draft_generator_impl.py",
    """        if request.preset_id:\n            return request.preset_id == \"qwen-image\"\n        if not self._comfyui_config.qwen_image_auto_select:\n            return False""",
    """        if request.preset_id:\n            if request.preset_id != \"qwen-image\":\n                return False\n            if not self._comfyui_config.qwen_image_enabled:\n                raise DraftWorkflowError(\"comfyui_qwen_image_disabled\")\n            return True\n        if not self._comfyui_config.qwen_image_auto_select:\n            return False""",
)
append_once(
    "tests/unit/pipeline/test_qwen_image_selection_policy.py",
    "test_explicit_qwen_preset_respects_disabled_policy",
    """def test_explicit_qwen_preset_respects_disabled_policy(tmp_path):\n    import pytest\n\n    from aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError\n\n    strategy = DraftGeneratorStrategy(\n        config=DraftWorkflowConfig(store_dir=str(tmp_path / \"drafts\")),\n        comfyui_config=ComfyUIConfig(qwen_image_enabled=False),\n    )\n    request = GenerationRequest(prompt=\"一只金毛犬\", preset_id=\"qwen-image\")\n\n    with pytest.raises(DraftWorkflowError, match=\"comfyui_qwen_image_disabled\"):\n        strategy._should_use_qwen_image(request)""",
)

# 3. A completed ComfyUI job with transient output recovery failures stays
# retryable. Only explicit terminal workflow errors fail immediately; repeated
# recovery failures eventually fail closed with a recovery-specific code.
replace_once(
    "aigateway-core/src/aigateway_core/pipelines/generation/draft/_draft_generator_impl.py",
    """            except Exception as exc:\n                if isinstance(exc, asyncio.CancelledError):\n                    raise\n                recovery_error = \"comfyui_recovery_failed\"\n                public_error = self._public_comfyui_error_code(\n                    exc, fallback=recovery_error\n                )\n                draft.generation_params[\"recovery_error\"] = recovery_error\n                return await self._mark_in_progress_draft_lost(\n                    draft,\n                    public_error,\n                    f\"Completed ComfyUI job could not be recovered: {type(exc).__name__}\",\n                )""",
    """            except Exception as exc:\n                if isinstance(exc, asyncio.CancelledError):\n                    raise\n                recovery_error = \"comfyui_recovery_failed\"\n                public_error = self._public_comfyui_error_code(\n                    exc, fallback=recovery_error\n                )\n                terminal_errors = {\n                    \"comfyui_gpu_out_of_memory\",\n                    \"comfyui_storage_low\",\n                    \"comfyui_workflow_execution_failed\",\n                    \"comfyui_missing_dependencies\",\n                    \"comfyui_model_budget_exceeded\",\n                    \"comfyui_output_budget_exceeded\",\n                }\n                draft.generation_params[\"recovery_error\"] = recovery_error\n                if public_error in terminal_errors:\n                    return await self._mark_in_progress_draft_lost(\n                        draft,\n                        public_error,\n                        f\"Completed ComfyUI job reported a terminal error: {type(exc).__name__}\",\n                    )\n\n                recovery_attempts = int(\n                    draft.generation_params.get(\"recovery_attempts\", 0) or 0\n                ) + 1\n                draft.generation_params[\"recovery_attempts\"] = recovery_attempts\n                draft.generation_params[\"last_recovery_error_type\"] = type(exc).__name__\n                reason = (\n                    \"Completed ComfyUI job output could not be recovered: \"\n                    f\"{type(exc).__name__}\"\n                )\n                if recovery_attempts >= 3:\n                    return await self._mark_in_progress_draft_lost(\n                        draft, recovery_error, reason\n                    )\n\n                await self._store_draft(\n                    draft, max(1, int(draft.expires_at - time.time()))\n                )\n                logger.warning(\n                    \"generation_optimization.draft_generator.runtime_recovery_retry\",\n                    extra={\n                        \"draft_id\": draft_id,\n                        \"prompt_id\": prompt_id,\n                        \"recovery_attempt\": recovery_attempts,\n                        \"error_type\": type(exc).__name__,\n                    },\n                )\n                return draft""",
)
replace_once(
    "tests/unit/pipeline/test_comfyui_recovery_errors.py",
    """@pytest.mark.asyncio\nasync def test_runtime_recovery_uses_generic_code_for_unknown_failures(tmp_path):\n    strategy = make_strategy(tmp_path)\n    draft = make_running_draft(\"unknown\")\n    await strategy._store_draft(draft, ttl_seconds=3600)\n    strategy._get_comfy_prompt_state = AsyncMock(return_value=\"completed\")\n    strategy._poll_results = AsyncMock(side_effect=ValueError(\"invalid output\"))\n\n    synced = await strategy.sync_draft_runtime_state(draft.draft_id)\n\n    assert synced is not None\n    assert synced.status == DRAFT_STATUS_FAILED\n    assert synced.error == \"comfyui_recovery_failed\"""",
    """@pytest.mark.asyncio\nasync def test_runtime_recovery_retries_unknown_failures_before_failing_closed(tmp_path):\n    strategy = make_strategy(tmp_path)\n    draft = make_running_draft(\"unknown\")\n    await strategy._store_draft(draft, ttl_seconds=3600)\n    strategy._get_comfy_prompt_state = AsyncMock(return_value=\"completed\")\n    strategy._poll_results = AsyncMock(side_effect=ValueError(\"invalid output\"))\n\n    first = await strategy.sync_draft_runtime_state(draft.draft_id)\n    second = await strategy.sync_draft_runtime_state(draft.draft_id)\n    third = await strategy.sync_draft_runtime_state(draft.draft_id)\n\n    assert first is not None and first.status == DRAFT_STATUS_RUNNING\n    assert second is not None and second.status == DRAFT_STATUS_RUNNING\n    assert first.generation_params[\"recovery_attempts\"] == 1\n    assert second.generation_params[\"recovery_attempts\"] == 2\n    assert third is not None\n    assert third.status == DRAFT_STATUS_FAILED\n    assert third.error == \"comfyui_recovery_failed\"\n    assert third.generation_params[\"recovery_attempts\"] == 3\n\n\n@pytest.mark.asyncio\nasync def test_completed_job_download_timeout_is_not_execution_timeout(tmp_path):\n    strategy = make_strategy(tmp_path)\n    draft = make_running_draft(\"download-timeout\")\n    await strategy._store_draft(draft, ttl_seconds=3600)\n    strategy._get_comfy_prompt_state = AsyncMock(return_value=\"completed\")\n    strategy._poll_results = AsyncMock(\n        side_effect=DraftWorkflowError(\n            \"ComfyUI 工作流执行超时 (1s): prompt_id=prompt-download-timeout\"\n        )\n    )\n\n    synced = await strategy.sync_draft_runtime_state(draft.draft_id)\n\n    assert synced is not None\n    assert synced.status == DRAFT_STATUS_RUNNING\n    assert synced.error is None\n    assert synced.generation_params[\"recovery_error\"] == \"comfyui_recovery_failed\"\n    assert synced.generation_params[\"recovery_attempts\"] == 1""",
)

# 4. Scope generated QA paths to the repository root and avoid deleting a
# generic product/documentation screenshots directory.
replace_once(
    ".gitignore",
    """docs/qa-evidence-*\nqa-report/\nqa-reports/\nreports/qa/\ntests/qa-report/\ntest-results/\nplaywright-report/\nscreenshots/""",
    """/docs/qa-evidence-*/\n/qa-report/\n/qa-reports/\n/reports/qa/\n/tests/qa-report/\n/test-results/\n/playwright-report/\n/qa-screenshots/""",
)
(ROOT / "scripts/clean-qa-artifacts.sh").write_text(
    """#!/usr/bin/env bash\nset -euo pipefail\n\nrepo_root=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")/..\" && pwd)\"\ncd \"$repo_root\"\n\nfor path in qa-report qa-reports reports/qa tests/qa-report test-results playwright-report qa-screenshots; do\n  rm -rf -- \"$path\"\ndone\n\nif [[ -d docs ]]; then\n  find docs -maxdepth 1 -type d -name 'qa-evidence-*' -prune -exec rm -rf -- {} +\nfi\n\nprintf 'Removed generated QA screenshots and reports.\\n'\n""",
    encoding="utf-8",
)
