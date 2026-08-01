from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(relative_path: str, old: str, new: str) -> None:
    path = ROOT / relative_path
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0 and new in text:
        return
    if count != 1:
        raise RuntimeError(f"expected one match in {relative_path}, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def write(relative_path: str, content: str) -> None:
    path = ROOT / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ISSUE-1: keep the ComfyUI root cause when recovery reads a completed job.
replace_once(
    "aigateway-core/src/aigateway_core/pipelines/generation/draft/_draft_generator_impl.py",
    '''    async def _generate_draft_async(\n''',
    '''    @staticmethod\n    def _public_comfyui_error_code(\n        exc: BaseException, *, fallback: str\n    ) -> str:\n        """Return a stable public error code without discarding the root cause."""\n        error_text = str(exc).lower()\n        known_codes = (\n            "comfyui_gpu_out_of_memory",\n            "comfyui_execution_timeout",\n            "comfyui_storage_low",\n            "comfyui_workflow_execution_failed",\n            "comfyui_missing_dependencies",\n            "comfyui_model_budget_exceeded",\n            "comfyui_output_budget_exceeded",\n        )\n        for code in known_codes:\n            if code in error_text:\n                return code\n        if "out of memory" in error_text or "cuda error: memory" in error_text:\n            return "comfyui_gpu_out_of_memory"\n        if "执行超时" in error_text or "timeout" in error_text:\n            return "comfyui_execution_timeout"\n        if "storage" in error_text:\n            return "comfyui_storage_low"\n        return fallback\n\n    async def _generate_draft_async(\n''',
)
replace_once(
    "aigateway-core/src/aigateway_core/pipelines/generation/draft/_draft_generator_impl.py",
    '''                error_text = str(exc).lower()\n                if "gpu_out_of_memory" in error_text or "out of memory" in error_text:\n                    public_error = "comfyui_gpu_out_of_memory"\n                elif "执行超时" in error_text or "timeout" in error_text:\n                    public_error = "comfyui_execution_timeout"\n                elif "storage" in error_text:\n                    public_error = "comfyui_storage_low"\n                else:\n                    public_error = "comfyui_generation_failed"\n''',
    '''                public_error = self._public_comfyui_error_code(\n                    exc, fallback="comfyui_generation_failed"\n                )\n''',
)
replace_once(
    "aigateway-core/src/aigateway_core/pipelines/generation/draft/_draft_generator_impl.py",
    '''            except Exception as exc:\n                if isinstance(exc, asyncio.CancelledError):\n                    raise\n                return await self._mark_in_progress_draft_lost(\n                    draft,\n                    "comfyui_recovery_failed",\n                    f"Completed ComfyUI job could not be recovered: {type(exc).__name__}",\n                )\n''',
    '''            except Exception as exc:\n                if isinstance(exc, asyncio.CancelledError):\n                    raise\n                recovery_error = "comfyui_recovery_failed"\n                public_error = self._public_comfyui_error_code(\n                    exc, fallback=recovery_error\n                )\n                draft.generation_params["recovery_error"] = recovery_error\n                return await self._mark_in_progress_draft_lost(\n                    draft,\n                    public_error,\n                    f"Completed ComfyUI job could not be recovered: {type(exc).__name__}",\n                )\n''',
)

write(
    "tests/unit/pipeline/test_comfyui_recovery_errors.py",
    '''import time\nfrom unittest.mock import AsyncMock\n\nimport pytest\n\nfrom aigateway_core.pipelines.generation._common.config import DraftWorkflowConfig\nfrom aigateway_core.pipelines.generation._common.exceptions import DraftWorkflowError\nfrom aigateway_core.pipelines.generation._common.models import (\n    DRAFT_STATUS_FAILED,\n    DRAFT_STATUS_RUNNING,\n    DraftResult,\n)\nfrom aigateway_core.pipelines.generation.draft.draft_generator import (\n    DraftGeneratorStrategy,\n)\nfrom aigateway_core.shared.integration_configs import ComfyUIConfig\n\n\ndef make_strategy(tmp_path):\n    config = DraftWorkflowConfig(store_dir=str(tmp_path / "drafts"))\n    return DraftGeneratorStrategy(\n        config=config,\n        redis_client=None,\n        comfyui_config=ComfyUIConfig(),\n    )\n\n\ndef make_running_draft(code: str) -> DraftResult:\n    return DraftResult(\n        draft_id=code,\n        previews=[],\n        generation_params={"trace_id": f"trace-{code}"},\n        created_at=time.time() - 120,\n        expires_at=time.time() + 3600,\n        attempt_number=1,\n        max_attempts=5,\n        status=DRAFT_STATUS_RUNNING,\n        media_type="image",\n        session_id=f"session-{code}",\n        progress=0.5,\n        stage="running",\n        comfy_prompt_id=f"prompt-{code}",\n    )\n\n\n@pytest.mark.asyncio\nasync def test_runtime_recovery_preserves_comfyui_oom_root_cause(tmp_path):\n    strategy = make_strategy(tmp_path)\n    draft = make_running_draft("oom")\n    await strategy._store_draft(draft, ttl_seconds=3600)\n    strategy._get_comfy_prompt_state = AsyncMock(return_value="completed")\n    strategy._poll_results = AsyncMock(\n        side_effect=DraftWorkflowError("comfyui_gpu_out_of_memory")\n    )\n\n    synced = await strategy.sync_draft_runtime_state(draft.draft_id)\n\n    assert synced is not None\n    assert synced.status == DRAFT_STATUS_FAILED\n    assert synced.error == "comfyui_gpu_out_of_memory"\n    assert synced.stage == "comfyui_gpu_out_of_memory"\n    assert synced.generation_params["recovery_error"] == "comfyui_recovery_failed"\n\n\n@pytest.mark.asyncio\nasync def test_runtime_recovery_uses_generic_code_for_unknown_failures(tmp_path):\n    strategy = make_strategy(tmp_path)\n    draft = make_running_draft("unknown")\n    await strategy._store_draft(draft, ttl_seconds=3600)\n    strategy._get_comfy_prompt_state = AsyncMock(return_value="completed")\n    strategy._poll_results = AsyncMock(side_effect=ValueError("invalid output"))\n\n    synced = await strategy.sync_draft_runtime_state(draft.draft_id)\n\n    assert synced is not None\n    assert synced.status == DRAFT_STATUS_FAILED\n    assert synced.error == "comfyui_recovery_failed"\n''',
)

# ISSUE-1 UX: translate the stable OOM code into an actionable message.
replace_once(
    "control-panel/src/services/chatRuntime.ts",
    '''export interface DraftPollProgress {\n''',
    '''export function describeDraftFailure(message: string): string {\n  const normalized = message.toLowerCase()\n  if (normalized.includes('comfyui_gpu_out_of_memory')) {\n    return 'ComfyUI 显存不足，无法完成当前图片工作流。请降低分辨率或批量大小，释放显存后重试。（comfyui_gpu_out_of_memory）'\n  }\n  if (normalized.includes('comfyui_recovery_failed')) {\n    return 'ComfyUI 任务已结束，但结果恢复失败。请重试；若持续发生，请检查 ComfyUI 历史记录。（comfyui_recovery_failed）'\n  }\n  return message\n}\n\nexport interface DraftPollProgress {\n''',
)
replace_once(
    "control-panel/src/services/chatRuntime.ts",
    '''          return { kind: 'error', message }\n        }\n        if (message.includes('forbidden') || message.includes('unauthorized')) {\n''',
    '''          return { kind: 'error', message: describeDraftFailure(message) }\n        }\n        if (message.includes('forbidden') || message.includes('unauthorized')) {\n''',
)
write(
    "control-panel/src/services/chatRuntime.qa.test.ts",
    '''import { afterEach, expect, it, vi } from 'vitest'\nimport * as api from '@/api/client'\nimport { clearAllChatPolling, pollDraftUntilSettled } from './chatRuntime'\n\nafterEach(() => {\n  vi.restoreAllMocks()\n  vi.useRealTimers()\n  clearAllChatPolling()\n})\n\nit('shows actionable guidance while retaining the ComfyUI OOM code', async () => {\n  vi.useFakeTimers()\n  const preview = vi.spyOn(api, 'getDraftPreview')\n    .mockRejectedValue(new Error('comfyui_gpu_out_of_memory'))\n\n  const resultPromise = pollDraftUntilSettled('draft-oom')\n  await vi.advanceTimersByTimeAsync(1_000)\n  const result = await resultPromise\n\n  expect(result.kind).toBe('error')\n  if (result.kind !== 'error') throw new Error('expected an error result')\n  expect(result.message).toContain('显存不足')\n  expect(result.message).toContain('降低分辨率')\n  expect(result.message).toContain('comfyui_gpu_out_of_memory')\n  expect(preview).toHaveBeenCalledTimes(1)\n})\n''',
)

# ISSUE-2: make an intentionally uninitialized Gateway CUDA context explicit.
replace_once(
    "control-panel/src/pages/Config.tsx",
    '''              <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>\n                allocated {formatBytes(gpuQuery.data.gateway.allocated_bytes)} · reserved {formatBytes(gpuQuery.data.gateway.reserved_bytes)}\n              </div>\n''',
    '''              {gpuQuery.data.gateway.available ? (\n                <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>\n                  allocated {formatBytes(gpuQuery.data.gateway.allocated_bytes)} · reserved {formatBytes(gpuQuery.data.gateway.reserved_bytes)}\n                </div>\n              ) : (\n                <div role="status" className="space-y-1">\n                  <div className="text-xs font-medium" style={{ color: 'var(--color-text-secondary)' }}>未初始化 CUDA</div>\n                  <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>\n                    Gateway 当前未建立 CUDA 上下文，以避免空闲占用 ComfyUI 显存；这不表示 GPU 或驱动不可用。\n                  </div>\n                </div>\n              )}\n''',
)
write(
    "control-panel/src/pages/GpuStatus.uninitialized.test.tsx",
    '''import { QueryClient, QueryClientProvider } from '@tanstack/react-query'\nimport { render, screen } from '@testing-library/react'\nimport { afterEach, expect, it, vi } from 'vitest'\nimport Config from './Config'\n\nconst api = vi.hoisted(() => ({\n  getComfyUIStatus: vi.fn(async () => ({ data: { available: true, public_url: '', manager_url: '', queue: { running: 0, pending: 0 }, configuration_errors: [] }, message: 'success' })),\n  getGenerationPresets: vi.fn(async () => ({ data: [], message: 'success' })),\n  getGpuStatus: vi.fn(async () => ({\n    data: {\n      gateway: { available: false, allocated_bytes: 0, reserved_bytes: 0, device_used_bytes: 0, device_free_bytes: 0, device_total_bytes: 0 },\n      comfyui: { available: true, memory: { total_bytes: 16_000, free_bytes: 15_500, used_bytes: 500 } },\n      queue: { running: 0, pending: 0 },\n      queue_idle: true,\n      shared_gpu: true,\n      diagnosis: [],\n    },\n    message: 'success',\n  })),\n  releaseGpuMemory: vi.fn(async () => ({ data: {}, message: 'success' })),\n}))\nvi.mock('@/api/client', () => api)\n\nafterEach(() => { vi.unstubAllGlobals() })\n\nit('distinguishes an uninitialized Gateway CUDA context from an unavailable GPU', async () => {\n  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {\n    const url = String(input)\n    if (url.endsWith('/admin/config/schema')) return Response.json({ data: { items: [] }, message: 'success' })\n    if (url.endsWith('/admin/config')) return Response.json({ data: { server: { port: 8000 } }, message: 'success', revision: 'r1' })\n    throw new Error(`unexpected request: ${url}`)\n  }))\n  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })\n  render(<QueryClientProvider client={client}><Config /></QueryClientProvider>)\n\n  expect(await screen.findByText('未初始化 CUDA')).toBeInTheDocument()\n  expect(screen.getByText(/避免空闲占用 ComfyUI 显存/)).toBeInTheDocument()\n  expect(screen.getByText(/不表示 GPU 或驱动不可用/)).toBeInTheDocument()\n})\n''',
)

# ISSUE-3: make provider connectivity progress/result visible and accessible.
replace_once(
    "control-panel/src/pages/Models.tsx",
    '''                <button\n                  className="p-1.5 rounded cursor-pointer"\n                  style={{ color: 'var(--color-primary)', border: '1px solid var(--color-border)' }}\n                  onClick={() => handleTestConnectivity(providerName)}\n                  title="测试连通性"\n                  disabled={testResults[providerName]?.loading}\n                >\n                  {testResults[providerName]?.loading ? <RefreshCw size={14} className="animate-spin" /> : <Wifi size={14} />}\n                </button>\n''',
    '''                <button\n                  className="btn btn-secondary"\n                  style={{ padding: '6px 10px', fontSize: '12px' }}\n                  onClick={() => handleTestConnectivity(providerName)}\n                  aria-label={`测试 ${providerName} 连通性`}\n                  aria-busy={testResults[providerName]?.loading ?? false}\n                  disabled={testResults[providerName]?.loading}\n                >\n                  {testResults[providerName]?.loading ? <RefreshCw size={14} className="animate-spin" /> : <Wifi size={14} />}\n                  {testResults[providerName]?.loading ? '测试中...' : '测试连通性'}\n                </button>\n''',
)
replace_once(
    "control-panel/src/pages/Models.tsx",
    '''            {/* Provider 展开内容 */}\n''',
    '''            {testResults[providerName] && (\n              <div\n                role="status"\n                aria-live="polite"\n                className="mt-3 rounded-lg px-3 py-2 text-xs"\n                style={{\n                  backgroundColor: testResults[providerName].loading\n                    ? 'var(--color-bg-elevated)'\n                    : testResults[providerName].success\n                      ? 'rgba(16, 185, 129, 0.08)'\n                      : 'rgba(239, 68, 68, 0.08)',\n                  color: testResults[providerName].loading\n                    ? 'var(--color-text-secondary)'\n                    : testResults[providerName].success\n                      ? 'var(--color-success)'\n                      : 'var(--color-danger)',\n                }}\n              >\n                {testResults[providerName].loading\n                  ? `正在测试 ${providerName} 连通性…`\n                  : testResults[providerName].success\n                    ? `连接成功，延迟 ${testResults[providerName].latency_ms} ms`\n                    : `连接失败：${testResults[providerName].error || '提供商不可达'}`}\n              </div>\n            )}\n\n            {/* Provider 展开内容 */}\n''',
)
write(
    "control-panel/src/pages/Models.connectivity.test.tsx",
    '''import { render, screen } from '@testing-library/react'\nimport userEvent from '@testing-library/user-event'\nimport { beforeEach, expect, it, vi } from 'vitest'\nimport Models from './Models'\n\nconst api = vi.hoisted(() => ({\n  getFullConfig: vi.fn(),\n  updateFullConfig: vi.fn(),\n  testProviderConnectivity: vi.fn(),\n  fetchProviderModels: vi.fn(),\n}))\nvi.mock('@/api/client', () => api)\n\nbeforeEach(() => {\n  api.getFullConfig.mockResolvedValue({\n    data: {\n      providers: {\n        openai: {\n          api_key: '***',\n          base_url: 'https://api.openai.com/v1',\n          model_grouper: [{ models: [{ name: 'gpt-4o-mini', capabilities: ['text'] }], fallback_models: [], pricing: {} }],\n          num_retries: 3,\n          retry_after: 1000,\n          timeout: 120,\n        },\n      },\n      embedding: {},\n    },\n    message: 'success',\n  })\n  api.updateFullConfig.mockResolvedValue({ data: { updated: true }, message: 'success' })\n  api.fetchProviderModels.mockResolvedValue({ data: { models: [] }, message: 'success' })\n  api.testProviderConnectivity.mockReset()\n})\n\nit('shows testing progress and a visible success result', async () => {\n  let resolveTest: (value: unknown) => void = () => undefined\n  api.testProviderConnectivity.mockImplementationOnce(() => new Promise(resolve => { resolveTest = resolve }))\n  const user = userEvent.setup()\n  render(<Models />)\n\n  const button = await screen.findByRole('button', { name: '测试 openai 连通性' })\n  await user.click(button)\n  expect(screen.getByRole('status')).toHaveTextContent('正在测试 openai 连通性')\n  expect(button).toBeDisabled()\n\n  resolveTest({ data: { success: true, latency_ms: 42 }, message: 'success' })\n  expect(await screen.findByText('连接成功，延迟 42 ms')).toBeInTheDocument()\n})\n\nit('shows the provider error when connectivity fails', async () => {\n  api.testProviderConnectivity.mockRejectedValueOnce(new Error('认证失败'))\n  const user = userEvent.setup()\n  render(<Models />)\n\n  await user.click(await screen.findByRole('button', { name: '测试 openai 连通性' }))\n  expect(await screen.findByText('连接失败：认证失败')).toBeInTheDocument()\n})\n''',
)

# ISSUE-4: ignore and safely clean generated browser-QA evidence.
replace_once(
    ".gitignore",
    '''# QA evidence\ndocs/qa-evidence-*\n''',
    '''# QA evidence\ndocs/qa-evidence-*\nqa-report/\nqa-reports/\nreports/qa/\ntests/qa-report/\ntest-results/\nplaywright-report/\nscreenshots/\n''',
)
write(
    "scripts/clean-qa-artifacts.sh",
    '''#!/usr/bin/env bash\nset -euo pipefail\n\nrepo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"\ncd "$repo_root"\n\nfor path in qa-report qa-reports reports/qa tests/qa-report test-results playwright-report screenshots; do\n  rm -rf -- "$path"\ndone\n\nif [[ -d docs ]]; then\n  find docs -maxdepth 1 -type d -name 'qa-evidence-*' -prune -exec rm -rf -- {} +\nfi\n\nprintf 'Removed generated QA screenshots and reports.\\n'\n''',
)

print("QA concern fixes applied")
