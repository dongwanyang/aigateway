import { useEffect, useState, useCallback, useRef, useMemo } from 'react'
import { X, GitCompareArrows, ChevronRight, ChevronDown, Loader2, Search, Copy, CornerLeftUp } from 'lucide-react'
import Card from '@/components/Card'
import { listCodeFiles, listAllSymbols, getCodeCallers, getCodeCallees } from '@/api/client'
import type { CodeFile, CodeSymbolNode, CodeSymbolRef } from '@/api/client'

const FILES_PER_PAGE = 100

// Strip one leading "src/" segment for display: "src/src/App.tsx" -> "src/App.tsx"
function displayPath(p: string): string {
  return p.startsWith('src/') ? p.slice(4) : p
}

// Stable key for a symbol node within the tree (file_path + name + line).
function nodeKey(filePath: string | null, name: string | null, line: number | null): string {
  return `${filePath ?? ''}::${name ?? ''}::${line ?? 0}`
}

interface SymbolNodeData {
  name: string | null
  kind: string | null
  file_path: string | null
  start_line: number | null
}

export default function CodeRelationPanel({
  documentId,
  onClose,
}: {
  documentId: string
  onClose: () => void
}) {
  const [files, setFiles] = useState<CodeFile[]>([])
  const [filesLoading, setFilesLoading] = useState(true)
  const [filesError, setFilesError] = useState<string | null>(null)
  const [searchInput, setSearchInput] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [filePage, setFilePage] = useState(1)

  // Symbol cache (one-time full fetch, grouped by file_path). Keyed by full db path.
  const [symbolsByFile, setSymbolsByFile] = useState<Map<string, CodeSymbolNode[]>>(new Map())
  const [symbolsLoaded, setSymbolsLoaded] = useState(false)
  const [symbolsLoading, setSymbolsLoading] = useState(false)
  const [symbolsError, setSymbolsError] = useState<string | null>(null)
  const [symbolsTruncated, setSymbolsTruncated] = useState(false)
  const [expandedFiles, setExpandedFiles] = useState<Set<string>>(new Set())

  // Per-symbol 1-hop relation cache. Key = nodeKey(file, name, line).
  const [relationCache, setRelationCache] = useState<Map<string, { callers: CodeSymbolRef[]; callees: CodeSymbolRef[] }>>(new Map())
  const [expandedSymbols, setExpandedSymbols] = useState<Set<string>>(new Set())
  const [relationLoading, setRelationLoading] = useState<Set<string>>(new Set())
  const [relationErrors, setRelationErrors] = useState<Map<string, string>>(new Map())

  // Debounce search input 300ms
  useEffect(() => {
    const t = setTimeout(() => setSearchQuery(searchInput), 300)
    return () => clearTimeout(t)
  }, [searchInput])

  // Mounted guard for in-flight requests on unmount
  const mountedRef = useRef<boolean | null>(null)
  if (mountedRef.current === null) mountedRef.current = true // set true on first render only
  useEffect(() => {
    return () => { mountedRef.current = false }
  }, [])

  // Load file list on mount
  const loadFiles = useCallback(async () => {
    setFilesLoading(true)
    setFilesError(null)
    try {
      const result = await listCodeFiles(documentId)
      if (mountedRef.current) setFiles(result)
    } catch (err) {
      if (mountedRef.current) setFilesError(err instanceof Error ? err.message : String(err))
    } finally {
      if (mountedRef.current) setFilesLoading(false)
    }
  }, [documentId])

  useEffect(() => {
    void loadFiles()
  }, [loadFiles])

  // Promise ref to prevent concurrent ensureSymbolsLoaded calls
  const symbolsFetchRef = useRef<Promise<void> | null>(null)

  const ensureSymbolsLoaded = useCallback(async () => {
    // Concurrent callers share the same in-flight fetch:
    //   - First caller: symbolsFetchRef.current is null → starts the fetch,
    //     stores its promise in the ref, awaits it, clears the ref on settle.
    //   - Concurrent caller (e.g. file toggle + search auto-load firing the
    //     same tick): symbolsFetchRef.current is set → returns the SAME promise
    //     and awaits it. No duplicate request.
    // On error, symbolsLoaded stays false and the ref is cleared (line below),
    // so a later retry re-fetches — correct. On success symbolsLoaded=true,
    // and the loaded-check at the top short-circuits all future calls.
    if (symbolsLoaded) return
    if (symbolsFetchRef.current) return symbolsFetchRef.current
    setSymbolsLoading(true)
    setSymbolsError(null)
    const fetchPromise = (async () => {
      try {
        const all = await listAllSymbols(documentId, { limit: 5000 })
        if (!mountedRef.current) return
        const grouped = new Map<string, CodeSymbolNode[]>()
        for (const sym of all) {
          const fp = sym.file_path ?? ''
          if (!grouped.has(fp)) grouped.set(fp, [])
          grouped.get(fp)!.push(sym)
        }
        setSymbolsByFile(grouped)
        setSymbolsLoaded(true)
        setSymbolsTruncated(all.length >= 5000)
      } catch (err) {
        if (mountedRef.current) setSymbolsError(err instanceof Error ? err.message : String(err))
      } finally {
        if (mountedRef.current) setSymbolsLoading(false)
      }
    })()
    symbolsFetchRef.current = fetchPromise
    await fetchPromise
    symbolsFetchRef.current = null
  }, [documentId, symbolsLoaded])

  // Auto-load symbols when search starts
  useEffect(() => {
    if (searchQuery.trim() && !symbolsLoaded && !symbolsLoading) {
      void ensureSymbolsLoaded()
    }
  }, [searchQuery, symbolsLoaded, symbolsLoading, ensureSymbolsLoaded])

  const relationCacheRef = useRef(relationCache)
  useEffect(() => { relationCacheRef.current = relationCache }, [relationCache])
  const relationLoadingRef = useRef(relationLoading)
  useEffect(() => { relationLoadingRef.current = relationLoading }, [relationLoading])
  const expandedSymbolsRef = useRef(expandedSymbols)
  useEffect(() => { expandedSymbolsRef.current = expandedSymbols }, [expandedSymbols])

  const loadRelation = useCallback(async (key: string, symbolName: string) => {
    // Check loading set first (ref is always current), then cache ref (may lag by one render).
    if (relationLoadingRef.current.has(key)) return
    if (relationCacheRef.current.has(key)) return
    setRelationLoading(prev => new Set(prev).add(key))
    setRelationErrors(prev => { const n = new Map(prev); n.delete(key); return n })
    try {
      const [callers, callees] = await Promise.all([
        getCodeCallers(documentId, symbolName),
        getCodeCallees(documentId, symbolName),
      ])
      if (!mountedRef.current) return
      setRelationCache(prev => {
        const n = new Map(prev)
        n.set(key, { callers, callees })
        return n
      })
    } catch (err) {
      if (!mountedRef.current) return
      setRelationErrors(prev => {
        const n = new Map(prev)
        n.set(key, err instanceof Error ? err.message : String(err))
        return n
      })
    } finally {
      if (mountedRef.current) setRelationLoading(prev => { const n = new Set(prev); n.delete(key); return n })
    }
  }, [documentId])

  const toggleSymbol = useCallback((key: string, symbolName: string) => {
    if (!symbolName) return
    // Read current expanded state from a ref (always current) so the side effect
    // (loadRelation) runs OUTSIDE the state updater. Putting it inside the
    // functional updater double-fires under React StrictMode (dev), and the
    // loading-ref guard hasn't synced yet at that point → duplicate API calls.
    const isExpanded = expandedSymbolsRef.current.has(key)
    if (!isExpanded) {
      void loadRelation(key, symbolName)
    }
    setExpandedSymbols(prev => {
      const n = new Set(prev)
      if (n.has(key)) n.delete(key)
      else n.add(key)
      return n
    })
  }, [loadRelation])

  const toggleFile = useCallback(async (filePath: string) => {
    // Expanding requires symbols loaded; collapsing does not.
    if (!expandedFiles.has(filePath)) {
      await ensureSymbolsLoaded()
      if (!mountedRef.current) return
    }
    if (mountedRef.current) setExpandedFiles(prev => {
      const next = new Set(prev)
      if (next.has(filePath)) next.delete(filePath)
      else next.add(filePath)
      return next
    })
  }, [ensureSymbolsLoaded])

  const [copyFeedback, setCopyFeedback] = useState<string | null>(null)
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const handleCopy = useCallback(async (text: string, id: string) => {
    if (copyTimerRef.current) { clearTimeout(copyTimerRef.current); copyTimerRef.current = null }
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      const ta = document.createElement('textarea')
      ta.value = text
      document.body.appendChild(ta)
      ta.select()
      document.execCommand('copy')
      document.body.removeChild(ta)
    }
    setCopyFeedback(id)
    copyTimerRef.current = setTimeout(() => setCopyFeedback(null), 1500)
  }, [])

  // Cleanup copy timer on unmount
  useEffect(() => {
    return () => { if (copyTimerRef.current) clearTimeout(copyTimerRef.current) }
  }, [])

  // Filter files by search query (path match)
  const sq = searchQuery.trim().toLowerCase()
  const filterSymbols = useCallback((syms: CodeSymbolNode[]) => {
    if (!sq) return syms
    return syms.filter(s => (s.name ?? '').toLowerCase().includes(sq) || (s.qualified_name ?? '').toLowerCase().includes(sq))
  }, [sq])
  const matchingFiles = useMemo(
    () => (sq ? files.filter(f => f.path.toLowerCase().includes(sq)) : files),
    [files, sq],
  )
  const visibleCount = Math.min(filePage * FILES_PER_PAGE, matchingFiles.length)
  const hasMoreFiles = visibleCount < matchingFiles.length

  // Precompute per-file row data (symbols + expanded) so a re-render triggered
  // by unrelated state (e.g. copyFeedback timer) doesn't re-filter every file's
  // symbol list. Slices + filters in one pass from the stable matchingFiles ref,
  // so it only recomputes when matchingFiles / page / symbols / expansion change.
  const fileRows = useMemo(() => {
    const rows: { file: CodeFile; expanded: boolean; symbols: CodeSymbolNode[] }[] = []
    for (let i = 0; i < visibleCount; i++) {
      const f = matchingFiles[i]
      const fileSyms = symbolsByFile.get(f.path) ?? []
      const filtered = sq ? filterSymbols(fileSyms) : (expandedFiles.has(f.path) ? fileSyms : [])
      rows.push({ file: f, expanded: sq ? filtered.length > 0 : expandedFiles.has(f.path), symbols: filtered })
    }
    return rows
  }, [matchingFiles, visibleCount, symbolsByFile, expandedFiles, sq, filterSymbols])

  return (
    <Card>
      <div className="flex items-center justify-between p-4 border-b" style={{ borderColor: 'var(--color-border)' }}>
        <h4 className="text-md font-semibold flex items-center gap-2">
          <GitCompareArrows size={16} /> 调用关系
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: 'var(--font-size-xs)', color: 'var(--color-text-tertiary)' }}>
            {documentId}
          </span>
        </h4>
        <button className="p-1 rounded cursor-pointer" onClick={onClose} title="收起">
          <X size={18} />
        </button>
      </div>

      <div className="p-4 space-y-3">
        {/* 搜索 */}
        <div className="flex gap-2">
          <div className="flex-1 flex items-center gap-2 px-3 py-1.5 rounded border" style={{ background: 'var(--color-bg-secondary)', borderColor: 'var(--color-border)' }}>
            <Search size={14} style={{ color: 'var(--color-text-tertiary)' }} />
            <input
              className="flex-1 bg-transparent outline-none text-sm"
              style={{ color: 'var(--color-text-primary)' }}
              placeholder="搜索文件或符号..."
              value={searchInput}
              onChange={e => { setSearchInput(e.target.value); setFilePage(1) }}
            />
          </div>
        </div>

        {symbolsTruncated && (
          <div className="text-xs px-3 py-1.5 rounded" style={{ background: 'rgba(234, 179, 8, 0.1)', color: 'var(--color-warning, #b45309)' }}>
            ⚠ 符号数量较多(≥5000),可能已截断。建议用搜索过滤。
          </div>
        )}
        {symbolsError && (
          <div className="text-sm" style={{ color: 'var(--color-danger)' }}>
            加载符号失败: {symbolsError}
            <button className="ml-2 underline cursor-pointer" onClick={() => void ensureSymbolsLoaded()}>重试</button>
          </div>
        )}
        {filesError && (
          <div className="text-sm" style={{ color: 'var(--color-danger)' }}>
            加载文件列表失败: {filesError}
            <button className="ml-2 underline cursor-pointer" onClick={() => void loadFiles()}>重试</button>
          </div>
        )}

        {filesLoading ? (
          <div className="text-center py-4 text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
            <Loader2 size={16} className="animate-spin inline mr-2" />加载文件列表...
          </div>
        ) : matchingFiles.length === 0 ? (
          <div className="text-center py-4 text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
            {sq ? '没有找到匹配的文件' : '无文件'}
          </div>
        ) : (
          <div className="space-y-1">
            {fileRows.map(row => (
                <FileGroup
                  key={row.file.path}
                  file={row.file}
                  expanded={row.expanded}
                  symbols={row.symbols}
                  symbolsLoading={symbolsLoading && !symbolsLoaded}
                  onToggle={() => void toggleFile(row.file.path)}
                  onCopy={handleCopy}
                  copyFeedback={copyFeedback}
                  expandedSymbols={expandedSymbols}
                  relationCache={relationCache}
                  relationLoading={relationLoading}
                  relationErrors={relationErrors}
                  onToggleSymbol={toggleSymbol}
                />
              ))}
            {hasMoreFiles && (
              <button
                className="w-full py-2 text-sm cursor-pointer rounded"
                style={{ color: 'var(--color-accent)' }}
                onClick={() => setFilePage(p => p + 1)}
              >
                加载更多 ({matchingFiles.length - visibleCount} 个文件)
              </button>
            )}
          </div>
        )}
      </div>
    </Card>
  )
}

function FileGroup({
  file,
  expanded,
  symbols,
  symbolsLoading,
  onToggle,
  onCopy,
  copyFeedback,
  expandedSymbols,
  relationCache,
  relationLoading,
  relationErrors,
  onToggleSymbol,
}: {
  file: CodeFile
  expanded: boolean
  symbols: CodeSymbolNode[]
  symbolsLoading: boolean
  onToggle: () => void
  onCopy: (text: string, id: string) => void
  copyFeedback: string | null
  expandedSymbols: Set<string>
  relationCache: Map<string, { callers: CodeSymbolRef[]; callees: CodeSymbolRef[] }>
  relationLoading: Set<string>
  relationErrors: Map<string, string>
  onToggleSymbol: (key: string, symbolName: string) => void
}) {
  return (
    <div className="rounded" style={{ background: 'var(--color-bg-secondary)' }}>
      <button
        className="w-full flex items-center gap-2 px-3 py-1.5 text-sm cursor-pointer"
        onClick={onToggle}
      >
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <span style={{ color: 'var(--color-text-primary)' }}>📄 {displayPath(file.path)}</span>
        <span style={{ color: 'var(--color-text-tertiary)' }}>({file.node_count ?? 0} 个符号)</span>
      </button>
      {expanded && (
        <div className="pb-1">
          {symbolsLoading ? (
            <div className="px-3 py-2 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              <Loader2 size={12} className="animate-spin inline mr-1" />加载符号...
            </div>
          ) : symbols.length === 0 ? (
            <div className="px-3 py-2 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>该文件无符号</div>
          ) : (
            <div className="px-1 pb-1 space-y-0.5">
              {symbols.map((sym, _idx) => (
                <SymbolNode
                  key={nodeKey(sym.file_path, sym.name, sym.start_line)}
                  node={{ name: sym.name, kind: sym.kind, file_path: sym.file_path, start_line: sym.start_line }}
                  ancestorKeys={[]}
                  depth={0}
                  expandedSymbols={expandedSymbols}
                  relationCache={relationCache}
                  relationLoading={relationLoading}
                  relationErrors={relationErrors}
                  onToggleSymbol={onToggleSymbol}
                  onCopy={onCopy}
                  copyFeedback={copyFeedback}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// Recursive call-tree node. Expands to show 1-hop callers + callees as child
// SymbolNodes. ancestorKeys prevents infinite recursion on cyclic call graphs.
function SymbolNode({
  node, ancestorKeys, depth,
  expandedSymbols, relationCache, relationLoading, relationErrors,
  onToggleSymbol, onCopy, copyFeedback,
}: {
  node: SymbolNodeData
  ancestorKeys: string[]
  depth: number
  expandedSymbols: Set<string>
  relationCache: Map<string, { callers: CodeSymbolRef[]; callees: CodeSymbolRef[] }>
  relationLoading: Set<string>
  relationErrors: Map<string, string>
  onToggleSymbol: (key: string, symbolName: string) => void
  onCopy: (text: string, id: string) => void
  copyFeedback: string | null
}) {
  const key = nodeKey(node.file_path, node.name, node.start_line)
  const isExpanded = expandedSymbols.has(key)
  const isLoading = relationLoading.has(key)
  const errMsg = relationErrors.get(key)
  const cached = relationCache.get(key)

  // Cycle guard: if this node already appears in the ancestor path, render as
  // a disabled "循环引用" marker and do not allow further expansion.
  const isCycle = ancestorKeys.includes(key)

  const locId = `loc-${node.file_path}-${node.start_line}-${depth}`
  const copyText = `${node.file_path}:${node.start_line}`

  const childAncestors = [...ancestorKeys, key]

  // Prefix caller-/callee- so a symbol appearing in BOTH sections gets distinct
  // React keys (otherwise index-based keys collide in the shared parent div).
  const renderChild = (child: SymbolNodeData, idx: number, prefix: string) => (
    <SymbolNode
      key={`${prefix}-${child.file_path}-${child.name}-${child.start_line}-${idx}`}
      node={child}
      ancestorKeys={childAncestors}
      depth={depth + 1}
      expandedSymbols={expandedSymbols}
      relationCache={relationCache}
      relationLoading={relationLoading}
      relationErrors={relationErrors}
      onToggleSymbol={onToggleSymbol}
      onCopy={onCopy}
      copyFeedback={copyFeedback}
    />
  )

  return (
    <div style={{ paddingLeft: depth > 0 ? 16 : 0 }}>
      <div className="flex items-center justify-between px-2 py-1 text-xs rounded" style={{ background: depth === 0 ? 'transparent' : 'var(--color-bg-overlay)' }}>
        <span className="flex items-center gap-1" style={{ fontFamily: 'var(--font-mono)' }}>
          {isCycle ? (
            <CornerLeftUp size={11} style={{ color: 'var(--color-text-tertiary)' }} />
          ) : isExpanded ? (
            <ChevronDown size={11} style={{ cursor: 'pointer' }} onClick={() => onToggleSymbol(key, node.name ?? '')} />
          ) : (
            <ChevronRight size={11} style={{ cursor: 'pointer' }} onClick={() => onToggleSymbol(key, node.name ?? '')} />
          )}
          <span
            style={{ color: isCycle ? 'var(--color-text-tertiary)' : 'var(--color-accent)', cursor: isCycle ? 'default' : 'pointer' }}
            onClick={() => { if (!isCycle) onToggleSymbol(key, node.name ?? '') }}
          >
            {isCycle ? '↩ 循环引用 ' : ''}{node.name ?? '?'}
          </span>
          <span style={{ color: 'var(--color-text-tertiary)' }}> ({node.kind ?? '?'})</span>
        </span>
        <button
          className="cursor-pointer hover:underline flex items-center gap-1"
          style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-text-tertiary)' }}
          title={`复制 ${copyText}`}
          onClick={() => onCopy(copyText, locId)}
        >
          {copyFeedback === locId ? <span style={{ color: 'var(--color-success)' }}>已复制</span> : (
            <>{displayPath(node.file_path ?? '')}:{node.start_line ?? '?'} <Copy size={11} /></>
          )}
        </button>
      </div>

      {isExpanded && !isCycle && (
        <div style={{ paddingLeft: 12, borderLeft: '1px solid var(--color-border)', marginLeft: 8 }}>
          {isLoading ? (
            <div className="px-2 py-1 text-xs" style={{ color: 'var(--color-text-tertiary)' }}>
              <Loader2 size={12} className="animate-spin inline mr-1" />加载调用关系...
            </div>
          ) : errMsg ? (
            <div className="px-2 py-1 text-xs" style={{ color: 'var(--color-danger)' }}>
              加载调用关系失败: {errMsg}
              <button className="ml-1 underline cursor-pointer" onClick={() => onToggleSymbol(key, node.name ?? '')}>重试</button>
            </div>
          ) : cached ? (
            <div className="space-y-1 py-1">
              <div className="text-xs" style={{ color: 'var(--color-text-tertiary)' }}>📥 调用者 (Callers) — {cached.callers.length}</div>
              {cached.callers.length === 0 ? (
                <div className="text-xs px-2" style={{ color: 'var(--color-text-tertiary)' }}>(无)</div>
              ) : cached.callers.map((c, idx) => renderChild(
                { name: c.name, kind: c.kind, file_path: c.file_path, start_line: c.start_line }, idx, 'caller',
              ))}
              <div className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>📤 被调用 (Callees) — {cached.callees.length}</div>
              {cached.callees.length === 0 ? (
                <div className="text-xs px-2" style={{ color: 'var(--color-text-tertiary)' }}>(无)</div>
              ) : cached.callees.map((c, idx) => renderChild(
                { name: c.name, kind: c.kind, file_path: c.file_path, start_line: c.start_line }, idx, 'callee',
              ))}
            </div>
          ) : null}
        </div>
      )}
    </div>
  )
}
