import { useEffect, useMemo, useRef, useState } from 'react'
import { Trash2, X, Code2, FolderOpen, Server, GitBranch, Package } from 'lucide-react'
import Card from '@/components/Card'
import {
  importCodeRepository,
  getCodeImportTask,
  listCodeRepositories,
  deleteCodeRepository,
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

const TERMINAL_STATUS: Array<CodeImportTask['status']> = ['completed', 'failed']

/**
 * 代码知识库标签页 —— 与文本知识库解耦的独立子系统。
 *
 * 四种导入源:
 *   folder  — 拖拽 / 目录选择器,保留 webkitRelativePath 相对路径
 *   server_path — 服务器绝对路径 (需要落在后端 allowed_server_paths 白名单)
 *   git    — 公共 https:// 仓库浅克隆
 *   zip    — 单个 .zip 包
 *
 * 导入是异步的:提交后拿到 task_id,组件每 2s 轮询进度,达到 completed/failed 停止。
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
  const [task, setTask] = useState<CodeImportTask | null>(null)

  const folderInputRef = useRef<HTMLInputElement | null>(null)
  const zipInputRef = useRef<HTMLInputElement | null>(null)

  // 初次加载 + 每次任务终态刷新仓库列表
  useEffect(() => {
    void reloadRepositories()
  }, [])

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

  // 任务进度轮询
  useEffect(() => {
    if (!task) return
    if (TERMINAL_STATUS.includes(task.status)) {
      if (task.status === 'completed') void reloadRepositories()
      return
    }
    const timer = window.setInterval(() => {
      void getCodeImportTask(task.task_id)
        .then(next => {
          setTask(next)
        })
        .catch(exc => {
          setTask(prev =>
            prev
              ? { ...prev, status: 'failed', error: exc instanceof Error ? exc.message : '轮询失败' }
              : prev,
          )
        })
    }, 2000)
    return () => window.clearInterval(timer)
  }, [task])

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
      setTask({
        task_id: response.task_id,
        status: 'pending',
        current_file: null,
        done: 0,
        total: 0,
        error: null,
      })
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

  const progressPercent = useMemo(() => {
    if (!task || !task.total) return 0
    return Math.min(100, Math.round((task.done / task.total) * 100))
  }, [task])

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
            {submitting ? '提交中...' : '开始导入'}
          </button>
        </div>

        {submitError && (
          <div className="mt-3 p-3 rounded-lg" style={{ backgroundColor: 'rgba(239, 68, 68, 0.1)', color: 'var(--color-danger)', fontSize: '13px' }}>
            ❌ {submitError}
          </div>
        )}
      </Card>

      {/* 任务进度 */}
      {task && (
        <Card>
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-md font-semibold">导入任务进度</h3>
            <span className="badge badge-neutral">{task.status}</span>
          </div>
          <div className="text-xs mb-2" style={{ color: 'var(--color-text-tertiary)' }}>
            task_id: <span style={{ fontFamily: 'var(--font-mono)' }}>{task.task_id}</span>
          </div>
          <div className="w-full rounded overflow-hidden" style={{ height: 6, backgroundColor: 'var(--color-bg-tertiary)' }}>
            <div
              style={{
                height: '100%',
                width: `${progressPercent}%`,
                backgroundColor: task.status === 'failed' ? 'var(--color-danger)' : 'var(--color-primary)',
                transition: 'width 200ms',
              }}
            />
          </div>
          <div className="text-xs mt-2" style={{ color: 'var(--color-text-tertiary)' }}>
            {task.done}/{task.total} · 当前文件: {task.current_file || '-'}
          </div>
          {task.error && (
            <div className="mt-3 p-3 rounded-lg" style={{ backgroundColor: 'rgba(239, 68, 68, 0.1)', color: 'var(--color-danger)', fontSize: '13px' }}>
              ❌ {task.error}
            </div>
          )}
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
                      <button
                        className="p-1.5 rounded cursor-pointer"
                        style={{ color: 'var(--color-danger)' }}
                        onClick={() => handleDeleteRepo(repo.document_id, repo.source_label)}
                        title="删除"
                      >
                        <Trash2 size={16} />
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  )
}
