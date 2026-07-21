import { useEffect, useMemo, useRef, useState } from 'react'
import { Trash2, X, Code2, FolderOpen, Server, GitBranch, Package, Loader2, XCircle, CheckCircle, PlayCircle, RefreshCw, GitCompareArrows } from 'lucide-react'
import Card from '@/components/Card'
import CodeRelationPanel from './CodeRelationPanel'
import {
  importCodeRepository,
  listCodeImportTasks,
  getCodeImportTask,
  cancelCodeImportTask,
  listCodeRepositories,
  deleteCodeRepository,
  syncCodeRepository,
} from '@/api/client'
import type {
  CodeImportSourceType,
  CodeImportTask,
  CodeRepositoryImport,
} from '@/api/client'

const SOURCE_LABEL: Record<CodeImportSourceType, string> = {
  folder: '拖拽 / 选择文件夹',
  server_path: '服务器目录路径',
  git: 'Git 仓库 URL',
  zip: 'ZIP 上传',
}

const SOURCE_ICON: Record<CodeImportSourceType, JSX.Element> = {
  folder: <FolderOpen size={14} />,
  server_path: <Server size={14} />,
  git: <GitBranch size={14} />,
  zip: <Package size={14} />,
}

const STATUS_LABEL: Record<CodeImportTask['status'], string> = {
  pending: '排队中',
  scanning: '扫描中',
  splitting: '分块中',
  building_graph: '构建图谱中',
  embedding: '嵌入中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

const STATUS_ICON: Record<CodeImportTask['status'], JSX.Element> = {
  pending: <Loader2 size={14} className="animate-spin" />,
  scanning: <Loader2 size={14} className="animate-spin" />,
  splitting: <Loader2 size={14} className="animate-spin" />,
  building_graph: <Loader2 size={14} className="animate-spin" />,
  embedding: <Loader2 size={14} className="animate-spin" />,
  completed: <CheckCircle size={14} />,
  failed: <XCircle size={14} />,
  cancelled: <XCircle size={14} />,
}

const TERMINAL_STATUS: Array<CodeImportTask['status']> = ['completed', 'failed', 'cancelled']
const POLL_INTERVAL_MS = 2000
const AUTO_DISMISS_MS = 5000 // 终端任务自动移除延迟
const TASK_STORAGE_KEY = 'code_import_tasks'
const DISMISSED_TASK_STORAGE_KEY = 'code_import_tasks_dismissed'

/** 清除某任务的所有 timer(轮询 + dismiss) */
function clearTaskTimers(map: Map<string, ReturnType<typeof setTimeout>>, taskId: string) {
  const timer = map.get(taskId)
  if (timer) { clearTimeout(timer); map.delete(taskId) }
  const dismissKey = `_${taskId}_dismiss`
  const dismissTimer = map.get(dismissKey)
  if (dismissTimer) { clearTimeout(dismissTimer); map.delete(dismissKey) }
}

function loadDismissedTaskIds(): string[] {
  try {
    const raw = localStorage.getItem(DISMISSED_TASK_STORAGE_KEY)
    if (raw) return JSON.parse(raw) as string[]
  } catch { /* corrupt → 清空 */ }
  return []
}

function saveDismissedTaskIds(taskIds: string[]) {
  try {
    localStorage.setItem(DISMISSED_TASK_STORAGE_KEY, JSON.stringify(taskIds))
  } catch { /* quota exceeded, ignore */ }
}

/** 持久化到 localStorage 的任务快照,页面刷新后恢复 */
function loadSavedTasks(): CodeImportTask[] {
  try {
    const raw = localStorage.getItem(TASK_STORAGE_KEY)
    if (raw) return JSON.parse(raw) as CodeImportTask[]
  } catch { /* corrupt → 清空 */ }
  return []
}

function saveTasks(tasks: CodeImportTask[]) {
  try {
    localStorage.setItem(TASK_STORAGE_KEY, JSON.stringify(tasks))
  } catch { /* quota exceeded, ignore */ }
}

/**
 * 代码知识库标签页 —— 与文本知识库解耦的独立子系统。
 *
 * 四种导入源:
 *   folder  — 拖拽 / 目录选择器,保留 webkitRelativePath 相对路径
 *   server_path — 服务器绝对路径 (需要落在后端 allowed_server_paths 白名单)
 *   git    — 公共 https:// 仓库浅克隆
 *   zip    — 单个 .zip 包
 *
 * 导入是异步的:提交后拿到 task_id,组件为每个任务独立轮询进度。
 * 支持多任务队列 + 手动取消 + 页面刷新恢复。
 */
export default function KnowledgeCodeTab() {
  const [repositories, setRepositories] = useState<CodeRepositoryImport[]>([])
  const [repoLoading, setRepoLoading] = useState(true)
  const [repoError, setRepoError] = useState<string | null>(null)

  const [sourceType, setSourceType] = useState<CodeImportSourceType>('folder')
  const [embeddingModel, setEmbeddingModel] = useState('Qwen/Qwen3-Embedding-0.6B')

  const [serverPath, setServerPath] = useState('')
  const [gitUrl, setGitUrl] = useState('')
  const [gitBranch, setGitBranch] = useState('')
  const [zipFile, setZipFile] = useState<File | null>(null)
  const [folderFiles, setFolderFiles] = useState<File[]>([])

  const [submitting, setSubmitting] = useState(false)
  const [submitError, setSubmitError] = useState<string | null>(null)

  // 增量同步状态: per-document syncing 标记 + 结果提示
  const [syncingRepo, setSyncingRepo] = useState<string | null>(null)
  const [syncResult, setSyncResult] = useState<{ document_id: string; text: string } | null>(null)

  // 调用关系面板:非弹窗,内嵌在仓库表格下方。null = 收起。
  const [relationDocumentId, setRelationDocumentId] = useState<string | null>(null)

  // 任务队列:每个任务独立轮询
  const [tasks, setTasks] = useState<CodeImportTask[]>(loadSavedTasks)
  const dismissedTaskIdsRef = useRef<Set<string>>(new Set(loadDismissedTaskIds()))
  // 每任务的轮询 timer 引用 (key = task_id)
  const timersRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())

  const folderInputRef = useRef<HTMLInputElement | null>(null)
  const zipInputRef = useRef<HTMLInputElement | null>(null)

  // ---------- 挂载: 恢复仓库列表 + 从后端同步活跃任务 ----------
  useEffect(() => {
    void reloadRepositories()
    void syncActiveTasks()
    return () => {
      // 卸载时清所有轮询 timer
      timersRef.current.forEach(t => clearTimeout(t))
      timersRef.current.clear()
    }
  }, [])

  // ---------- 持久化: 任务队列变化时写 localStorage ----------
  useEffect(() => {
    saveTasks(tasks)
  }, [tasks])

  // ---------- 轮询: 每个非终态任务独立计时器 ----------
  useEffect(() => {
    const activeIds = new Set<string>()
    for (const task of tasks) {
      if (TERMINAL_STATUS.includes(task.status)) continue
      if (timersRef.current.has(task.task_id)) continue // 已有 timer
      const timer = setTimeout(() => pollTask(task.task_id), POLL_INTERVAL_MS)
      timersRef.current.set(task.task_id, timer)
      activeIds.add(task.task_id)
    }
    // 清理已变为终态的任务的 timer
    for (const [tid, timer] of timersRef.current) {
      if (!activeIds.has(tid) && !tid.startsWith('_')) {
        clearTimeout(timer)
        timersRef.current.delete(tid)
      }
    }
  }, [tasks])

  async function reloadRepositories() {
    setRepoLoading(true)
    setRepoError(null)
    try {
      const r = await listCodeRepositories()
      setRepositories(r)
    } catch (exc) {
      setRepoError(exc instanceof Error ? exc.message : '加载代码知识库失败')
    } finally {
      setRepoLoading(false)
    }
  }

  /** 从后端拉取所有活跃任务,合并到前端队列,并补充前端缺失的任务 */
  async function syncActiveTasks() {
    try {
      const backendTasks = await listCodeImportTasks()
      const backendMap = new Map(backendTasks.map(t => [t.task_id, t]))
      const backendIds = new Set(backendTasks.map(t => t.task_id))

      setTasks(prev => {
        // 用后端状态覆盖本地任务; 后端不存在的本地任务:
        //   - 终态任务 → 丢弃(幽灵残留)
        //   - 非终态任务 → 保留,交由 pollTask 单独判真伪(避免后端列表瞬时缺失误删正在跑的任务)
        const merged = prev
          .filter(pt => !dismissedTaskIdsRef.current.has(pt.task_id))
          .map(pt => backendMap.get(pt.task_id) ?? pt)
          .filter(pt => backendIds.has(pt.task_id) || !TERMINAL_STATUS.includes(pt.status))
        const mergedIds = new Set(merged.map(t => t.task_id))
        // 只补回后端非终态任务,避免把用户已 dismiss 的旧终态任务重新拉回
        for (const bt of backendTasks) {
          if (dismissedTaskIdsRef.current.has(bt.task_id)) continue
          if (TERMINAL_STATUS.includes(bt.status)) continue
          if (!mergedIds.has(bt.task_id)) {
            merged.push(bt)
            mergedIds.add(bt.task_id)
          }
        }
        return merged
      })
    } catch (exc) {
      // 后端不可用(如 Redis 断开),保持前端状态但记录警告
      console.warn('[CodeTab] syncActiveTasks failed:', exc instanceof Error ? exc.message : String(exc))
    }
  }

  /** 轮询单个任务 */
  function pollTask(taskId: string) {
    getCodeImportTask(taskId)
      .then(next => {
        setTasks(prev =>
          prev.map(t => (t.task_id === taskId ? next : t)),
        )
        // 到达终态 → 停止轮询,延迟移除
        if (TERMINAL_STATUS.includes(next.status)) {
          clearTaskTimers(timersRef.current, taskId)
          // 如果是 completed,刷新仓库列表
          if (next.status === 'completed') {
            void reloadRepositories()
          }
          // 延迟自动移除
          const dismissKey = `_${taskId}_dismiss`
          const dismissTimer2 = setTimeout(() => {
            dismissTask(taskId)
          }, AUTO_DISMISS_MS)
          timersRef.current.set(dismissKey, dismissTimer2)
        }
      })
      .catch(exc => {
        // 404 → 任务已过期/不存在，直接 dismiss
        if (
          exc instanceof Error &&
          (exc.message.toLowerCase().includes('404') ||
           exc.message.toLowerCase().includes('not found'))
        ) {
          dismissTask(taskId)
          return
        }
        // 轮询失败 → 标记 failed,停止轮询,保留已有 error 信息
        setTasks(prev =>
          prev.map(t =>
            t.task_id === taskId
              ? { ...t, status: 'failed', error: t.error || (exc instanceof Error ? exc.message : '轮询失败') }
              : t,
          ),
        )
        // 清 timer
        clearTaskTimers(timersRef.current, taskId)
      })
  }

  /** 从前端队列移除任务，并记录为已 dismiss，避免再次拉回 */
  function dismissTask(taskId: string) {
    clearTaskTimers(timersRef.current, taskId)
    dismissedTaskIdsRef.current.add(taskId)
    saveDismissedTaskIds(Array.from(dismissedTaskIdsRef.current))
    setTasks(prev => prev.filter(t => t.task_id !== taskId))
  }

  /** 取消任务 — cancel API 同步返回实际状态,用后端返回的 status 更新 */
  async function handleCancelTask(task: CodeImportTask) {
    try {
      const resp = await cancelCodeImportTask(task.task_id)
      // 后端可能返回已终态的真实 status(如 completed),以实际为准
      const actualStatus = (resp as { status: string }).status || 'cancelled'
      setTasks(prev =>
        prev.map(t =>
          t.task_id === task.task_id
            ? { ...t, status: actualStatus as CodeImportTask['status'], error: null }
            : t,
        ),
      )
      clearTaskTimers(timersRef.current, task.task_id)
    } catch (exc) {
      alert(`取消失败: ${exc instanceof Error ? exc.message : '未知错误'}`)
    }
  }

  function resetFormFiles() {
    setZipFile(null)
    setFolderFiles([])
    if (folderInputRef.current) folderInputRef.current.value = ''
    if (zipInputRef.current) zipInputRef.current.value = ''
  }

  async function handleStartImport() {
    setSubmitError(null)
    setSubmitting(true)
    try {
      let response: { task_id: string; status: 'pending' }
      if (sourceType === 'server_path') {
        if (!serverPath) throw new Error('请输入 server_path')
        response = await importCodeRepository({
          source_type: 'server_path',
          server_path: serverPath,
          embedding_model: embeddingModel,
        })
      } else if (sourceType === 'git') {
        if (!gitUrl.startsWith('https://')) throw new Error('git_url 必须以 https:// 开头')
        response = await importCodeRepository({
          source_type: 'git',
          git_url: gitUrl,
          git_branch: gitBranch || undefined,
          embedding_model: embeddingModel,
        })
      } else if (sourceType === 'zip') {
        if (!zipFile) throw new Error('请选择要上传的 .zip 文件')
        const form = new FormData()
        form.append('source_type', 'zip')
        form.append('embedding_model', embeddingModel)
        form.append('file', zipFile, zipFile.name)
        response = await importCodeRepository(form)
      } else {
        if (folderFiles.length === 0) throw new Error('请选择要导入的文件夹')
        const form = new FormData()
        form.append('source_type', 'folder')
        form.append('embedding_model', embeddingModel)
        for (const file of folderFiles) {
          const rel =
            (file as File & { webkitRelativePath?: string }).webkitRelativePath ||
            file.name
          form.append('files', file, file.name)
          form.append('relative_paths', rel)
        }
        response = await importCodeRepository(form)
      }

      // 新任务清掉旧的 dismiss 记录,避免同 task_id 被恢复逻辑忽略
      dismissedTaskIdsRef.current.delete(response.task_id)
      saveDismissedTaskIds(Array.from(dismissedTaskIdsRef.current))

      // 加入队列,不阻塞表单
      const newTask: CodeImportTask = {
        task_id: response.task_id,
        status: 'pending',
        current_file: null,
        done: 0,
        total: 0,
        error: null,
        source_label: '',
        source_type: sourceType,
        created_at: 0,
      }
      setTasks(prev => [...prev, newTask])
      resetFormFiles()
    } catch (exc) {
      setSubmitError(exc instanceof Error ? exc.message : '提交导入任务失败')
    } finally {
      setSubmitting(false)
    }
  }

  async function handleDeleteRepo(documentId: string, sourceLabel: string) {
    if (!confirm(`确定删除代码知识库 "${sourceLabel}"？将同时清理 Qdrant 向量与调用图谱。`)) return
    try {
      await deleteCodeRepository(documentId)
      setRepositories(prev => prev.filter(r => r.document_id !== documentId))
    } catch {
      alert('删除失败')
    }
  }

  async function handleSyncRepo(documentId: string) {
    setSyncingRepo(documentId)
    setSyncResult(null)
    try {
      const result = await syncCodeRepository(documentId)
      setSyncResult({
        document_id: documentId,
        text: `同步完成: ${result.synced_files} 文件、${result.refreshed_symbols} 符号刷新${result.deleted_files ? `、${result.deleted_files} 文件移除` : ''}`,
      })
    } catch (err) {
      setSyncResult({
        document_id: documentId,
        text: `同步失败: ${err instanceof Error ? err.message : String(err)}`,
      })
    } finally {
      setSyncingRepo(null)
    }
  }

  function handleOpenRelationPanel(documentId: string) {
    setRelationDocumentId(documentId)
  }

  const activeCount = useMemo(
    () => tasks.filter(t => !TERMINAL_STATUS.includes(t.status)).length,
    [tasks],
  )

  return (
    <div className="space-y-6">
      <Card>
        <h3 className="text-md font-semibold mb-4">导入代码到知识库</h3>

        {/* 导入方式切换 */}
        <div className="flex flex-wrap gap-2 mb-4">
          {(Object.keys(SOURCE_LABEL) as CodeImportSourceType[]).map(key => (
            <button
              key={key}
              className={`btn ${sourceType === key ? 'btn-primary' : 'btn-secondary'}`}
              style={{ padding: '6px 16px', fontSize: '13px' }}
              onClick={() => {
                setSourceType(key)
                resetFormFiles()
                setSubmitError(null)
              }}
            >
              {SOURCE_ICON[key]}
              {SOURCE_LABEL[key]}
            </button>
          ))}
        </div>

        {/* 源特定表单 */}
        {sourceType === 'folder' && (
          <div className="mb-4">
            <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>
              选择本地文件夹(会保留相对路径)
            </label>
            <input
              ref={folderInputRef}
              type="file"
              // 使用 webkitdirectory 让浏览器把整个目录以 File[] 传入
              /* @ts-expect-error webkitdirectory 是非标准属性,React 类型未覆盖 */
              webkitdirectory=""
              multiple
              style={{ display: 'none' }}
              onChange={e => setFolderFiles(Array.from(e.target.files || []))}
            />
            <button
              className="btn btn-secondary"
              onClick={() => folderInputRef.current?.click()}
              style={{ padding: '10px 20px' }}
            >
              <FolderOpen size={16} /> 点击选择目录
            </button>
            {folderFiles.length > 0 && (
              <div className="text-xs mt-2" style={{ color: 'var(--color-text-tertiary)' }}>
                已选择 {folderFiles.length} 个文件
              </div>
            )}
          </div>
        )}

        {sourceType === 'server_path' && (
          <div className="mb-4">
            <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>
              服务器上的目录路径(需在后端 allowed_server_paths 白名单内)
            </label>
            <input
              className="input w-full"
              placeholder="/home/ubuntu/your-project"
              value={serverPath}
              onChange={e => setServerPath(e.target.value)}
            />
          </div>
        )}

        {sourceType === 'git' && (
          <div className="mb-4 grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>
                Git URL(仅支持 https:// 公共仓库)
              </label>
              <input
                className="input w-full"
                placeholder="https://github.com/org/repo"
                value={gitUrl}
                onChange={e => setGitUrl(e.target.value)}
              />
            </div>
            <div>
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>
                分支(可留空,默认远端 HEAD)
              </label>
              <input
                className="input w-full"
                placeholder="main"
                value={gitBranch}
                onChange={e => setGitBranch(e.target.value)}
              />
            </div>
          </div>
        )}

        {sourceType === 'zip' && (
          <div className="mb-4">
            <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>
              上传 .zip 包(会做 zip-slip / 总大小检查)
            </label>
            <input
              ref={zipInputRef}
              type="file"
              accept=".zip"
              style={{ display: 'none' }}
              onChange={e => setZipFile(e.target.files?.[0] || null)}
            />
            {!zipFile ? (
              <button
                className="btn btn-secondary"
                onClick={() => zipInputRef.current?.click()}
                style={{ padding: '10px 20px' }}
              >
                <Package size={16} /> 选择 ZIP
              </button>
            ) : (
              <div className="flex items-center gap-2 p-3 rounded-lg" style={{ backgroundColor: 'var(--color-bg-tertiary)' }}>
                <Package size={16} style={{ color: 'var(--color-primary)' }} />
                <span style={{ fontFamily: 'var(--font-mono)', fontSize: '13px' }}>{zipFile.name}</span>
                <span className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                  {(zipFile.size / 1024 / 1024).toFixed(2)} MB
                </span>
                <button
                  className="p-1 rounded cursor-pointer ml-auto"
                  onClick={() => setZipFile(null)}
                  title="清除"
                >
                  <X size={16} />
                </button>
              </div>
            )}
          </div>
        )}

        {/* 嵌入模型 */}
        <div className="mb-4">
          <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>
            嵌入模型(可自定义,导入时按模型分独立 Qdrant 集合)
          </label>
          <input
            className="input w-full"
            placeholder="Qwen/Qwen3-Embedding-0.6B"
            value={embeddingModel}
            onChange={e => setEmbeddingModel(e.target.value)}
          />
        </div>

        {/* 操作 */}
        <div className="flex gap-2">
          <button
            className="btn btn-primary"
            onClick={handleStartImport}
            disabled={submitting}
          >
            {submitting ? <><Loader2 size={14} className="inline animate-spin mr-1" /> 提交中...</> : <><PlayCircle size={14} className="inline mr-1" /> 开始导入</>}
          </button>
        </div>

        {submitError && (
          <div className="mt-3 p-3 rounded-lg" style={{ backgroundColor: 'rgba(239, 68, 68, 0.1)', color: 'var(--color-danger)', fontSize: '13px' }}>
            ❌ {submitError}
          </div>
        )}
      </Card>

      {/* 任务队列 */}
      {tasks.length > 0 && (
        <Card>
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-md font-semibold">导入任务队列</h3>
            <span className="badge badge-neutral">
              {activeCount} 活跃 / {tasks.length} 总计
            </span>
          </div>

          <div className="space-y-3">
            {tasks.map(task => {
              const isTerminal = TERMINAL_STATUS.includes(task.status)
              const isRunning = !isTerminal
              const progressPercent = task.total > 0 ? Math.min(100, Math.round((task.done / task.total) * 100)) : 0

              return (
                <div
                  key={task.task_id}
                  className="p-3 rounded-lg border"
                  style={{
                    borderColor: isTerminal
                      ? task.status === 'failed'
                        ? 'var(--color-danger)'
                        : task.status === 'cancelled'
                          ? 'var(--color-warning)'
                          : 'var(--color-success)'
                      : 'var(--color-bg-tertiary)',
                    backgroundColor: isRunning
                      ? 'rgba(59, 130, 246, 0.05)'
                      : 'transparent',
                  }}
                >
                  <div className="flex items-center gap-3">
                    {/* 状态图标 */}
                    <div style={{ color: isTerminal
                      ? task.status === 'failed'
                        ? 'var(--color-danger)'
                        : task.status === 'cancelled'
                          ? 'var(--color-warning)'
                          : 'var(--color-success)'
                      : 'var(--color-primary)'
                    }}>
                      {STATUS_ICON[task.status]}
                    </div>

                    {/* 信息 */}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium truncate">
                          {task.source_label || '未知来源'}
                        </span>
                        <span className="badge badge-neutral" style={{ fontSize: '11px' }}>
                          {STATUS_LABEL[task.status]}
                        </span>
                      </div>
                      <div className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
                        task_id: <span style={{ fontFamily: 'var(--font-mono)', fontSize: '11px' }}>{task.task_id.slice(0, 8)}…</span>
                        {task.source_type && ` · ${task.source_type}`}
                      </div>

                      {/* 进度条 */}
                      {isRunning && task.total > 0 && (
                        <div className="mt-2">
                          <div className="w-full rounded overflow-hidden" style={{ height: 4, backgroundColor: 'var(--color-bg-tertiary)' }}>
                            <div
                              style={{
                                height: '100%',
                                width: `${progressPercent}%`,
                                backgroundColor: 'var(--color-primary)',
                                transition: 'width 200ms',
                              }}
                            />
                          </div>
                          <div className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>
                            {task.done}/{task.total} · 当前文件: {task.current_file || '-'}
                            {progressPercent > 0 && ` (${progressPercent}%)`}
                          </div>
                        </div>
                      )}

                      {/* 错误信息 */}
                      {task.error && (
                        <div className="mt-1 text-xs" style={{ color: 'var(--color-danger)' }}>
                          ❌ {task.error}
                        </div>
                      )}
                    </div>

                    {/* 操作按钮 */}
                    <div className="flex items-center gap-1">
                      {isRunning && (
                        <button
                          className="btn btn-secondary"
                          style={{ padding: '4px 10px', fontSize: '12px' }}
                          onClick={() => handleCancelTask(task)}
                          title="取消任务"
                        >
                          <XCircle size={14} /> 取消
                        </button>
                      )}
                      {isTerminal && (
                        <button
                          className="p-1 rounded cursor-pointer"
                          style={{ color: 'var(--color-text-tertiary)' }}
                          onClick={() => dismissTask(task.task_id)}
                          title="移除"
                        >
                          <X size={14} />
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        </Card>
      )}

      {/* 仓库列表 */}
      <Card>
        <div className="table-container">
          <table>
            <thead>
              <tr>
                <th>来源</th>
                <th>类型</th>
                <th>语言</th>
                <th>文件</th>
                <th>函数</th>
                <th>类</th>
                <th>分块</th>
                <th>嵌入模型</th>
                <th>导入时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {repoLoading ? (
                <tr><td colSpan={10} className="text-center py-8">加载中...</td></tr>
              ) : repoError ? (
                <tr><td colSpan={10} className="text-center py-8" style={{ color: 'var(--color-danger)' }}>{repoError}</td></tr>
              ) : repositories.length === 0 ? (
                <tr>
                  <td colSpan={10} className="text-center py-8" style={{ color: 'var(--color-text-tertiary)' }}>
                    <Code2 size={32} className="mx-auto mb-2 opacity-40" />
                    <div>暂无代码知识库,导入一个开始</div>
                  </td>
                </tr>
              ) : (
                repositories.map(repo => (
                  <tr key={repo.document_id}>
                    <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-sm)' }}>{repo.source_label}</td>
                    <td><span className="badge badge-neutral">{repo.source_type}</span></td>
                    <td style={{ fontSize: 'var(--font-size-sm)' }}>{repo.language_summary.join(', ') || '-'}</td>
                    <td style={{ fontFamily: 'var(--font-mono)' }}>{repo.file_count}</td>
                    <td style={{ fontFamily: 'var(--font-mono)' }}>{repo.function_count}</td>
                    <td style={{ fontFamily: 'var(--font-mono)' }}>{repo.class_count}</td>
                    <td style={{ fontFamily: 'var(--font-mono)' }}>{repo.chunk_count}</td>
                    <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-sm)' }}>{repo.embedding_model}</td>
                    <td style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-tertiary)' }}>
                      {repo.import_time ? new Date(repo.import_time).toLocaleString() : '-'}
                    </td>
                    <td>
                      <div className="flex items-center gap-1">
                        {/* 同步:仅 git/server_path 显示(managed 源可增量);folder/zip 置灰 */}
                        {(repo.source_type === 'git' || repo.source_type === 'server_path') ? (
                          <button
                            className="p-1.5 rounded cursor-pointer"
                            style={{ color: 'var(--color-text-secondary)' }}
                            onClick={() => handleSyncRepo(repo.document_id)}
                            disabled={syncingRepo === repo.document_id}
                            title="增量同步"
                          >
                            {syncingRepo === repo.document_id ? (
                              <Loader2 size={16} className="animate-spin" />
                            ) : (
                              <RefreshCw size={16} />
                            )}
                          </button>
                        ) : (
                          <button
                            className="p-1.5 rounded cursor-not-allowed opacity-40"
                            title="快照源(folder/zip)不支持同步,请重新导入"
                          >
                            <RefreshCw size={16} />
                          </button>
                        )}
                        {/* 调用关系查询 */}
                        <button
                          className="p-1.5 rounded cursor-pointer"
                          style={{ color: 'var(--color-text-secondary)' }}
                          onClick={() => handleOpenRelationPanel(repo.document_id)}
                          title="调用关系"
                        >
                          <GitCompareArrows size={16} />
                        </button>
                        <button
                          className="p-1.5 rounded cursor-pointer"
                          style={{ color: 'var(--color-danger)' }}
                          onClick={() => handleDeleteRepo(repo.document_id, repo.source_label)}
                          title="删除"
                        >
                          <Trash2 size={16} />
                        </button>
                      </div>
                      {syncResult && syncResult.document_id === repo.document_id && (
                        <div style={{ fontSize: 'var(--font-size-xs)', color: 'var(--color-text-tertiary)', marginTop: 4 }}>
                          {syncResult.text}
                        </div>
                      )}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>

      {/* 调用关系面板(内嵌,非弹窗) */}
      {relationDocumentId && (
        <div className="mt-4">
          <CodeRelationPanel
            documentId={relationDocumentId}
            onClose={() => setRelationDocumentId(null)}
          />
        </div>
      )}
    </div>
  )
}
