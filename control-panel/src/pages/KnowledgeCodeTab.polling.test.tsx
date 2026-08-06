import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import KnowledgeCodeTab from './KnowledgeCodeTab'

const api = vi.hoisted(() => ({
  importCodeRepository: vi.fn(),
  listCodeImportTasks: vi.fn(),
  getCodeImportTask: vi.fn(),
  cancelCodeImportTask: vi.fn(),
  listCodeRepositories: vi.fn(),
  deleteCodeRepository: vi.fn(),
  syncCodeRepository: vi.fn(),
}))

vi.mock('@/api/client', () => api)

const pendingTask = {
  task_id: 'task-polling',
  status: 'pending' as const,
  current_file: null,
  done: 0,
  total: 0,
  error: null,
  source_label: 'https://example.test/repo.git',
  source_type: 'git' as const,
  created_at: 1,
}

const repository = {
  document_id: 'repo-new',
  source_type: 'git' as const,
  source_label: 'https://example.test/repo.git',
  file_count: 2,
  language_summary: ['python'],
  function_count: 3,
  class_count: 1,
  chunk_count: 4,
  embedding_model: 'Qwen/Qwen3-Embedding-0.6B',
  import_time: '2026-08-06T00:00:00Z',
}

async function flushEffects() {
  await act(async () => {
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('KnowledgeCodeTab task polling', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    localStorage.clear()
    Object.values(api).forEach(mock => mock.mockReset())

    localStorage.setItem('code_import_tasks', JSON.stringify([pendingTask]))
    api.listCodeImportTasks.mockResolvedValue([pendingTask])
    api.listCodeRepositories
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([repository])
    api.getCodeImportTask
      .mockResolvedValueOnce({
        ...pendingTask,
        status: 'splitting',
        done: 2,
        total: 4,
        current_file: 'src/auth.py',
      })
      .mockResolvedValueOnce({
        ...pendingTask,
        status: 'completed',
        done: 4,
        total: 4,
        current_file: 'src/main.py',
      })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('continues polling without a page refresh and reloads repositories on completion', async () => {
    render(<KnowledgeCodeTab />)
    await flushEffects()

    expect(screen.getByText('排队中')).toBeInTheDocument()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    expect(api.getCodeImportTask).toHaveBeenCalledTimes(1)
    expect(screen.getByText('分块中')).toBeInTheDocument()
    expect(screen.getByText(/2\/4 · 当前文件: src\/auth.py/)).toBeInTheDocument()

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    await flushEffects()

    expect(api.getCodeImportTask).toHaveBeenCalledTimes(2)
    expect(screen.getByText('已完成')).toBeInTheDocument()
    expect(api.listCodeRepositories).toHaveBeenCalledTimes(2)
    expect(screen.getAllByText('https://example.test/repo.git').length).toBeGreaterThan(1)
  })
})
