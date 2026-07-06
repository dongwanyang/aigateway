import { useEffect, useRef, useState } from 'react'
import { BookOpen, Plus, Trash2, Globe, Upload, Settings, X } from 'lucide-react'
import Card from '@/components/Card'
import { listRagDocuments, importRagDocument, deleteRagDocument } from '@/api/client'
import type { RagDocument } from '@/api/client'

export default function Knowledge() {
  const [documents, setDocuments] = useState<RagDocument[]>([])
  const [loading, setLoading] = useState(true)
  const [showImport, setShowImport] = useState(false)
  const [importing, setImporting] = useState(false)
  const [importResult, setImportResult] = useState<string | null>(null)
  const [importError, setImportError] = useState<string | null>(null)

  // Import form
  const [importMode, setImportMode] = useState<'url' | 'file'>('url')
  const [url, setUrl] = useState('')
  const [content, setContent] = useState('')
  const [filename, setFilename] = useState('')
  const [fileReading, setFileReading] = useState(false)
  const [fileError, setFileError] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)
  const [chunkStrategy, setChunkStrategy] = useState('fixed_size')
  const [chunkSize, setChunkSize] = useState(512)
  const [chunkOverlap, setChunkOverlap] = useState(64)

  const MAX_FILE_BYTES = 5 * 1024 * 1024 // 5MB — 纯文本文档单次导入上限
  const ACCEPT_EXT = '.txt,.md,.markdown,.json,.csv,.tsv,.log,.html,.htm,.xml,.yaml,.yml,.py,.js,.ts,.tsx,.jsx,.java,.go,.rs,.c,.cpp,.h,.sh'

  useEffect(() => {
    loadDocuments()
  }, [])

  async function loadDocuments() {
    try {
      const r = await listRagDocuments()
      setDocuments(r.data.documents)
    } catch {
      // ignore
    } finally {
      setLoading(false)
    }
  }

  async function handleImport() {
    setImporting(true)
    setImportError(null)
    setImportResult(null)
    try {
      const params: Record<string, unknown> = {
        chunk_strategy: chunkStrategy,
        chunk_size: chunkSize,
        chunk_overlap: chunkOverlap,
      }
      if (importMode === 'url') {
        params.url = url
      } else {
        if (!content) throw new Error('请先选择要上传的文件')
        params.content = content
        params.filename = filename || 'document.txt'
      }
      const r = await importRagDocument(params as any)
      setImportResult(`导入成功: ${r.data.chunk_count} 个分块, ${r.data.total_tokens} tokens, 耗时 ${r.data.elapsed_ms}ms`)
      setUrl('')
      setContent('')
      setFilename('')
      setFileError(null)
      if (fileInputRef.current) fileInputRef.current.value = ''
      await loadDocuments()
    } catch (e: any) {
      setImportError(e.message || '导入失败')
    } finally {
      setImporting(false)
    }
  }

  async function handleFilePick(file: File | undefined | null) {
    setFileError(null)
    if (!file) return
    if (file.size > MAX_FILE_BYTES) {
      setFileError(`文件过大（${(file.size / 1024 / 1024).toFixed(2)} MB），上限 ${MAX_FILE_BYTES / 1024 / 1024} MB`)
      setContent('')
      setFilename('')
      return
    }
    setFileReading(true)
    try {
      const text = await file.text()
      setContent(text)
      setFilename(file.name)
    } catch (e: any) {
      setFileError(`读取失败: ${e?.message || '未知错误'}`)
      setContent('')
      setFilename('')
    } finally {
      setFileReading(false)
    }
  }

  function clearPickedFile() {
    setContent('')
    setFilename('')
    setFileError(null)
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  async function handleDelete(docId: string, docName: string) {
    if (!confirm(`确定删除文档 "${docName}"？此操作将同时删除 Qdrant 中的所有向量。`)) return
    try {
      await deleteRagDocument(docId)
      setDocuments(prev => prev.filter(d => d.doc_id !== docId))
    } catch {
      alert('删除失败')
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">知识库</h2>
        <button className="btn btn-primary" onClick={() => { setShowImport(true); setImportResult(null); setImportError(null) }}>
          <Plus size={16} /> 导入文档
        </button>
      </div>

      {/* 导入面板 */}
      {showImport && (
        <Card>
          <h3 className="text-md font-semibold mb-4">导入文档到知识库</h3>

          {/* 导入方式切换 */}
          <div className="flex gap-2 mb-4">
            <button
              className={`btn ${importMode === 'url' ? 'btn-primary' : 'btn-secondary'}`}
              style={{ padding: '6px 16px', fontSize: '13px' }}
              onClick={() => setImportMode('url')}
            >
              <Globe size={14} /> 网页 URL
            </button>
            <button
              className={`btn ${importMode === 'file' ? 'btn-primary' : 'btn-secondary'}`}
              style={{ padding: '6px 16px', fontSize: '13px' }}
              onClick={() => setImportMode('file')}
            >
              <Upload size={14} /> 上传文件
            </button>
          </div>

          {/* 输入区域 */}
          {importMode === 'url' ? (
            <div className="mb-4">
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>网页 URL</label>
              <input
                className="input w-full"
                placeholder="https://example.com/article..."
                value={url}
                onChange={e => setUrl(e.target.value)}
              />
            </div>
          ) : (
            <div className="mb-4">
              <label className="block text-sm mb-1" style={{ color: 'var(--color-text-secondary)' }}>从本地选择文件</label>
              <input
                ref={fileInputRef}
                type="file"
                accept={ACCEPT_EXT}
                style={{ display: 'none' }}
                onChange={e => handleFilePick(e.target.files?.[0])}
              />
              {!filename ? (
                <button
                  className="btn btn-secondary"
                  style={{ padding: '10px 20px', width: '100%', justifyContent: 'center' }}
                  onClick={() => fileInputRef.current?.click()}
                  disabled={fileReading}
                >
                  <Upload size={16} />
                  {fileReading ? '读取中...' : '点击选择文件'}
                </button>
              ) : (
                <div
                  className="flex items-center justify-between p-3 rounded-lg"
                  style={{ backgroundColor: 'var(--color-bg-tertiary)', border: '1px solid var(--color-border)' }}
                >
                  <div className="flex items-center gap-2" style={{ minWidth: 0, flex: 1 }}>
                    <Upload size={16} style={{ color: 'var(--color-primary)', flexShrink: 0 }} />
                    <div style={{ minWidth: 0 }}>
                      <div style={{ fontFamily: 'var(--font-mono)', fontSize: '13px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {filename}
                      </div>
                      <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
                        {content.length.toLocaleString()} 字符 · ~{Math.ceil(content.length / 4).toLocaleString()} tokens
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-2" style={{ flexShrink: 0 }}>
                    <button
                      className="btn btn-secondary"
                      style={{ padding: '4px 12px', fontSize: '12px' }}
                      onClick={() => fileInputRef.current?.click()}
                    >
                      重选
                    </button>
                    <button
                      className="p-1.5 rounded cursor-pointer"
                      style={{ color: 'var(--color-text-tertiary)' }}
                      onClick={clearPickedFile}
                      title="清除"
                    >
                      <X size={16} />
                    </button>
                  </div>
                </div>
              )}
              <div className="text-xs mt-2" style={{ color: 'var(--color-text-tertiary)' }}>
                支持文本类文件（txt/md/json/csv/log/html/xml/yaml 及常见源码）,单文件 ≤ 5 MB
              </div>
              {fileError && (
                <div className="mt-2 p-2 rounded" style={{ backgroundColor: 'rgba(239, 68, 68, 0.1)', color: 'var(--color-danger)', fontSize: '13px' }}>
                  {fileError}
                </div>
              )}
            </div>
          )}

          {/* 分块配置 */}
          <div className="mb-4">
            <div className="flex items-center gap-2 mb-2">
              <Settings size={14} style={{ color: 'var(--color-text-tertiary)' }} />
              <span className="text-sm font-medium">分块配置</span>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <div>
                <label className="block text-xs mb-1" style={{ color: 'var(--color-text-tertiary)' }}>分块策略</label>
                <select className="input w-full" value={chunkStrategy} onChange={e => setChunkStrategy(e.target.value)} style={{ cursor: 'pointer' }}>
                  <option value="fixed_size">固定字符数</option>
                  <option value="paragraph">按段落</option>
                  <option value="sentence">按句子</option>
                </select>
              </div>
              <div>
                <label className="block text-xs mb-1" style={{ color: 'var(--color-text-tertiary)' }}>Chunk 大小 (字符)</label>
                <input className="input w-full" type="number" value={chunkSize} onChange={e => setChunkSize(Number(e.target.value))} />
              </div>
              <div>
                <label className="block text-xs mb-1" style={{ color: 'var(--color-text-tertiary)' }}>重叠度 (字符)</label>
                <input className="input w-full" type="number" value={chunkOverlap} onChange={e => setChunkOverlap(Number(e.target.value))} />
              </div>
            </div>
          </div>

          {/* 操作按钮 */}
          <div className="flex gap-2">
            <button
              className="btn btn-primary"
              onClick={handleImport}
              disabled={importing || fileReading || (importMode === 'url' ? !url : !content)}
            >
              {importing ? '导入中...' : '开始导入'}
            </button>
            <button className="btn btn-secondary" onClick={() => setShowImport(false)}>取消</button>
          </div>

          {/* 结果/错误 */}
          {importResult && (
            <div className="mt-3 p-3 rounded-lg" style={{ backgroundColor: 'rgba(16, 185, 129, 0.1)', color: 'var(--color-success)', fontSize: '13px' }}>
              ✅ {importResult}
            </div>
          )}
          {importError && (
            <div className="mt-3 p-3 rounded-lg" style={{ backgroundColor: 'rgba(239, 68, 68, 0.1)', color: 'var(--color-danger)', fontSize: '13px' }}>
              ❌ {importError}
            </div>
          )}
        </Card>
      )}

      {/* 文档列表 */}
      <Card>
        <div className="table-container">
          <table>
            <thead>
              <tr>
                <th>文件名</th>
                <th>类型</th>
                <th>分块数</th>
                <th>分块策略</th>
                <th>Tokens</th>
                <th>导入时间</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {loading ? (
                <tr><td colSpan={7} className="text-center py-8">加载中...</td></tr>
              ) : documents.length === 0 ? (
                <tr><td colSpan={7} className="text-center py-8" style={{ color: 'var(--color-text-tertiary)' }}>
                  <BookOpen size={32} className="mx-auto mb-2 opacity-40" />
                  <div>暂无文档，点击"导入文档"开始添加</div>
                </td></tr>
              ) : (
                documents.map(doc => (
                  <tr key={doc.doc_id}>
                    <td style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-sm)' }}>
                      {doc.filename}
                      {doc.url && (
                        <div className="text-xs mt-1" style={{ color: 'var(--color-text-quaternary)' }}>
                          {doc.url.slice(0, 50)}{doc.url.length > 50 ? '...' : ''}
                        </div>
                      )}
                    </td>
                    <td><span className="badge badge-neutral">{doc.file_type}</span></td>
                    <td style={{ fontFamily: 'var(--font-mono)' }}>{doc.chunk_count}</td>
                    <td style={{ fontSize: 'var(--font-size-sm)' }}>
                      {doc.chunk_strategy} ({doc.chunk_size}/{doc.chunk_overlap})
                    </td>
                    <td style={{ fontFamily: 'var(--font-mono)' }}>{doc.total_tokens.toLocaleString()}</td>
                    <td style={{ fontSize: 'var(--font-size-sm)', color: 'var(--color-text-tertiary)' }}>
                      {new Date(doc.created_at * 1000).toLocaleDateString()}
                    </td>
                    <td>
                      <button
                        className="p-1.5 rounded cursor-pointer"
                        style={{ color: 'var(--color-danger)' }}
                        onClick={() => handleDelete(doc.doc_id, doc.filename)}
                        title="删除文档"
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
