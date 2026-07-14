# Code Call Graph Visualization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the call-relationship search-only modal in the Knowledge Code tab with an inline, expandable/collapsible call-graph tree panel that shows symbols grouped by file, each symbol expandable into recursive caller/callee relationships with file:line location display.

**Architecture:** Pure-frontend change. A new `CodeRelationPanel` component (with a recursive `SymbolNode` sub-component) renders below the repository table as an inline Card (not a modal). It loads the file list on open, fetches all symbols once on first file expand, and fetches 1-hop callers/callees per symbol node on expand. A new `listAllSymbols` API client function uses the existing `/query` endpoint with an empty `symbol=` parameter (verified working). No backend changes.

**Tech Stack:** React 18 + TypeScript, Vite, lucide-react icons, existing `@/api/client` fetch helpers. No frontend test framework - verification via `tsc -b` + `vite build` + manual browser testing against the running docker stack.

## Global Constraints

- No backend changes - use only existing `/admin/rag/code/*` endpoints.
- Empty-string symbol search: `?symbol=&kind=Y&limit=5000` (symbol param is required by the API, must send empty string not omit).
- File paths in API responses carry a `src/` prefix (e.g. `src/src/App.tsx`); strip one `src/` for display, keep full path for copy payload.
- React `Map`/`Set` state updates must be immutable (create new instances) to trigger re-renders.
- Frontend has no test runner; each task verifies with `cd control-panel && npx tsc -b` then `npm run build`.
- Match existing code style: inline `style={{...}}` with CSS vars (`var(--color-*)`), Tailwind utility classes for layout, lucide-react icons.
- Commits stay in working tree until user reviews (per CLAUDE.md workflow rule 0).

---

## File Structure

| File | Responsibility |
|------|----------------|
| `control-panel/src/api/client.ts` (modify) | Add `CodeFile` type, `listCodeFiles()`, `listAllSymbols()` functions |
| `control-panel/src/pages/CodeRelationPanel.tsx` (create) | Self-contained inline panel component + recursive `SymbolNode`. Owns all relation-panel state (files, symbols, caches, expansion). Exports `CodeRelationPanel`. |
| `control-panel/src/pages/KnowledgeCodeTab.tsx` (modify) | Replace `relationPanel` modal state with a `relationDocumentId: string \| null`; render `<CodeRelationPanel>` below the repo table; drop the old modal JSX and handlers. |

---

## Task 1: Add API client functions

**Files:**
- Modify: `control-panel/src/api/client.ts` (append after the existing `getCodeImpact` function, ~line 804)

**Interfaces:**
- Produces: `CodeFile` type, `listCodeFiles(documentId: string): Promise<CodeFile[]>`, `listAllSymbols(documentId: string, opts?: { kind?: string; limit?: number }): Promise<CodeSymbolNode[]>`

- [ ] **Step 1: Add `CodeFile` type and `listCodeFiles` + `listAllSymbols` functions**

Append this code to `control-panel/src/api/client.ts` immediately after the `getCodeImpact` function (after line 804, before the L3 cache section comment at line 807):

```typescript
export interface CodeFile {
  path: string
  language: string
  node_count: number | null
  size: number | null
}

export async function listCodeFiles(documentId: string): Promise<CodeFile[]> {
  const headers = await ensureAuthHeaders()
  const res = await fetch(
    `${API_BASE}/admin/rag/code/repositories/${encodeURIComponent(documentId)}/files`,
    { headers },
  )
  if (!res.ok) throw new Error(`Code files failed: ${res.status}`)
  return await res.json()
}

// List all symbols via empty-string search (no new backend endpoint needed).
// codegraph CLI ignores --limit partially on empty search, so pass a large limit
// to fetch the full set; caller checks length === limit to detect truncation.
export async function listAllSymbols(
  documentId: string,
  opts?: { kind?: string; limit?: number },
): Promise<CodeSymbolNode[]> {
  const headers = await ensureAuthHeaders()
  const limit = opts?.limit ?? 5000
  const params = new URLSearchParams({ symbol: '', limit: String(limit) })
  if (opts?.kind) params.set('kind', opts.kind)
  const res = await fetch(
    `${API_BASE}/admin/rag/code/repositories/${encodeURIComponent(documentId)}/query?${params}`,
    { headers },
  )
  if (!res.ok) throw new Error(`List symbols failed: ${res.status}`)
  return await res.json()
}
```

- [ ] **Step 2: Verify type check + build pass**

Run: `cd control-panel && npx tsc -b`
Expected: no errors.

Run: `cd control-panel && npm run build`
Expected: build succeeds (tsc + vite build).

- [ ] **Step 3: Manually verify the functions hit the API correctly**

Run (uses the running gateway container + admin key from `config.yaml`):
```bash
sudo docker exec aigateway-gateway-1 curl -s "http://localhost:8000/admin/rag/code/repositories/code_46aa48c4fde3/files" -H "Authorization: Bearer gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o" | python3 -c "import sys,json; d=json.load(sys.stdin); print('files:', len(d), '| first:', d[0] if d else None)"
```
Expected: `files: 23 | first: {'path': 'src/src/App.tsx', ...}` (non-empty list).

```bash
sudo docker exec aigateway-gateway-1 curl -s "http://localhost:8000/admin/rag/code/repositories/code_46aa48c4fde3/query?symbol=&limit=5000" -H "Authorization: Bearer gw-rRIop4dpcyJJNUTJbHmHpr9Bj3M11s5o" | python3 -c "import sys,json; d=json.load(sys.stdin); print('symbols:', len(d))"
```
Expected: `symbols: 389` (full symbol set, confirming empty-string search + large limit returns everything).

- [ ] **Step 4: Commit**

```bash
git add control-panel/src/api/client.ts
git commit -m "feat(code-rag): add listCodeFiles + listAllSymbols API client functions

Empty-string symbol search verified working against production data.
No backend changes needed."
```

---

## Task 2: Create `CodeRelationPanel` skeleton (inline panel + file list)

**Files:**
- Create: `control-panel/src/pages/CodeRelationPanel.tsx`
- Modify: `control-panel/src/pages/KnowledgeCodeTab.tsx` (state + render site; detailed in Task 2 Step 3)

**Interfaces:**
- Produces: `CodeRelationPanel` component with props `{ documentId: string; onClose: () => void }`. On mount, loads file list via `listCodeFiles`. Renders an inline Card with header, search input, and the file list (collapsed). This task delivers a visible, working file list.

- [ ] **Step 1: Create `CodeRelationPanel.tsx` with panel shell + file list**

Create `control-panel/src/pages/CodeRelationPanel.tsx` (note: `noUnusedLocals: true` is on, so this task imports only what the stub uses; later tasks add the rest):

```tsx
import { useEffect, useState, useCallback } from 'react'
import { X, GitCompareArrows, Loader2, Search } from 'lucide-react'
import Card from '@/components/Card'
import { listCodeFiles } from '@/api/client'
import type { CodeFile } from '@/api/client'

const FILES_PER_PAGE = 100

// Strip one leading "src/" segment for display: "src/src/App.tsx" -> "src/App.tsx"
function displayPath(p: string): string {
  return p.startsWith('src/') ? p.slice(4) : p
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
  const [searchQuery, setSearchQuery] = useState('')
  const [filePage, setFilePage] = useState(1)

  // Load file list on mount
  const loadFiles = useCallback(async () => {
    setFilesLoading(true)
    setFilesError(null)
    try {
      const result = await listCodeFiles(documentId)
      setFiles(result)
    } catch (err) {
      setFilesError(err instanceof Error ? err.message : String(err))
    } finally {
      setFilesLoading(false)
    }
  }, [documentId])

  useEffect(() => {
    void loadFiles()
  }, [loadFiles])

  // Filter files by search query (path match)
  const filteredFiles = searchQuery.trim()
    ? files.filter(f => f.path.toLowerCase().includes(searchQuery.trim().toLowerCase()))
    : files
  const visibleFiles = filteredFiles.slice(0, filePage * FILES_PER_PAGE)
  const hasMoreFiles = visibleFiles.length < filteredFiles.length

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
              value={searchQuery}
              onChange={e => { setSearchQuery(e.target.value); setFilePage(1) }}
            />
          </div>
        </div>

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
        ) : filteredFiles.length === 0 ? (
          <div className="text-center py-4 text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
            无文件
          </div>
        ) : (
          <div className="space-y-1">
            {visibleFiles.map(f => (
              <FileGroup key={f.path} file={f} />
            ))}
            {hasMoreFiles && (
              <button
                className="w-full py-2 text-sm cursor-pointer rounded"
                style={{ color: 'var(--color-accent)' }}
                onClick={() => setFilePage(p => p + 1)}
              >
                加载更多 ({filteredFiles.length - visibleFiles.length} 个文件)
              </button>
            )}
          </div>
        )}
      </div>
    </Card>
  )
}

// FileGroup is a stub here; fully implemented in Task 3.
function FileGroup({ file }: { file: CodeFile }) {
  return (
    <div className="px-3 py-1.5 rounded text-sm" style={{ background: 'var(--color-bg-secondary)' }}>
      📄 {displayPath(file.path)} <span style={{ color: 'var(--color-text-tertiary)' }}>({file.node_count ?? 0} 个符号)</span>
    </div>
  )
}
```

(Note: the `displayPath` helper is used by the stub, so it's not unused. `nodeKey`/`SymbolNodeData` are added in Task 4 where they're first used.)

- [ ] **Step 2: Verify type check + build**

Run: `cd control-panel && npx tsc -b`
Expected: no errors. This task imports only what the stub uses (`listCodeFiles`, `CodeFile`, `displayPath`, `X`, `GitCompareArrows`, `Loader2`, `Search`, `Card`) - `noUnusedLocals: true` is satisfied. Later tasks add the remaining imports (`listAllSymbols`, `getCodeCallers`, `getCodeCallees`, `ChevronRight`, `ChevronDown`, `Copy`, `CornerLeftUp`, `CodeSymbolNode`, `CodeSymbolRef`, `nodeKey`, `SymbolNodeData`) at the step where they're first used.

Run: `cd control-panel && npm run build`
Expected: build succeeds.

- [ ] **Step 3: Wire `CodeRelationPanel` into `KnowledgeCodeTab.tsx`**

In `control-panel/src/pages/KnowledgeCodeTab.tsx`:

3a. Add import at top (after the existing `import Card from '@/components/Card'`):
```tsx
import CodeRelationPanel from './CodeRelationPanel'
```

3b. Replace the `relationPanel` state (lines ~138-146) with a simple document-id state. Find:
```tsx
  // 调用关系查询面板状态
  const [relationPanel, setRelationPanel] = useState<{
    documentId: string
    symbolInput: string
    selectedSymbol: string | null
    tab: 'callers' | 'callees' | 'impact'
    loading: boolean
    refs: CodeSymbolRef[]
    error: string | null
  } | null>(null)
```
Replace with:
```tsx
  // 调用关系面板:非弹窗,内嵌在仓库表格下方。null = 收起。
  const [relationDocumentId, setRelationDocumentId] = useState<string | null>(null)
```

3c. Replace `handleOpenRelationPanel` (lines ~409-419). Find:
```tsx
  async function handleOpenRelationPanel(documentId: string) {
    setRelationPanel({
      documentId,
      symbolInput: '',
      selectedSymbol: null,
      tab: 'callers',
      loading: false,
      refs: [],
      error: null,
    })
  }
```
Replace with:
```tsx
  function handleOpenRelationPanel(documentId: string) {
    setRelationDocumentId(documentId)
  }
```

3d. Delete the now-unused `handleSearchSymbol` and `loadRelationTab` functions (lines ~421-460). They reference the old `relationPanel` state and are no longer called. Remove them entirely.

3e. Replace the modal overlay JSX (lines ~855-962, the `{relationPanel && (...)}` block) with the inline panel rendered below the repository table Card. Find the entire `{/* 调用关系查询面板(弹窗) */}` block through its closing `)}` and replace with:
```tsx
      {/* 调用关系面板(内嵌,非弹窗) */}
      {relationDocumentId && (
        <div className="mt-4">
          <CodeRelationPanel
            documentId={relationDocumentId}
            onClose={() => setRelationDocumentId(null)}
          />
        </div>
      )}
```

3f. Remove `getCodeCallers, getCodeCallees, getCodeImpact, queryCodeSymbols` from the imports in `KnowledgeCodeTab.tsx` if they are no longer used there (they moved to `CodeRelationPanel`). Check the import block (lines ~4-16) and remove any that become unused. Also remove `CodeSymbolRef` from the type import (lines ~17-22) if unused. Run `npx tsc -b` to confirm no dangling references.

- [ ] **Step 4: Verify type check + build + manual render**

Run: `cd control-panel && npx tsc -b && npm run build`
Expected: succeeds.

Rebuild the frontend container and verify in browser:
```bash
sudo DOCKER_BUILDKIT=1 docker compose up -d --build control-panel
```
Open the Knowledge Code tab, click "调用关系" on a repo row. Expected: an inline Card appears below the table with a search box and a list of files (each showing `📄 path (N 个符号)`). Clicking X / 收起 collapses it.

- [ ] **Step 5: Commit**

```bash
git add control-panel/src/pages/CodeRelationPanel.tsx control-panel/src/pages/KnowledgeCodeTab.tsx
git commit -m "feat(code-rag): replace call-relation modal with inline panel shell

CodeRelationPanel renders below the repo table (non-modal). Loads file
list on open. Symbols + relationships wired in subsequent tasks."
```

---

## Task 3: Implement `FileGroup` expand/collapse + symbol list with file:line

**Files:**
- Modify: `control-panel/src/pages/CodeRelationPanel.tsx` (replace the stub `FileGroup` with full implementation; lift symbol cache to panel level)

**Interfaces:**
- Produces: `FileGroup` expands to show that file's symbols. First expand of any file triggers a one-time `listAllSymbols` fetch, grouped by `file_path` into a panel-level cache. Each symbol row shows name, kind, and a clickable `file:line` location that copies to clipboard. This task delivers the symbol list with location display.

- [ ] **Step 1: Update imports (add symbols + icons first used in this task)**

In `CodeRelationPanel.tsx`, update the import lines to add `listAllSymbols`, `CodeSymbolNode`, and the icons used by the full `FileGroup`/`SymbolLocationRow`:

```tsx
import { useEffect, useState, useCallback } from 'react'
import { X, GitCompareArrows, ChevronRight, ChevronDown, Loader2, Search, Copy } from 'lucide-react'
import Card from '@/components/Card'
import { listCodeFiles, listAllSymbols } from '@/api/client'
import type { CodeFile, CodeSymbolNode } from '@/api/client'
```

- [ ] **Step 2: Add panel-level symbol cache + loader to `CodeRelationPanel`**

In `CodeRelationPanel.tsx`, inside the `CodeRelationPanel` component, add these state hooks after the `filePage` state:

```tsx
  // Symbol cache (one-time full fetch, grouped by file_path). Keyed by full db path.
  const [symbolsByFile, setSymbolsByFile] = useState<Map<string, CodeSymbolNode[]>>(new Map())
  const [symbolsLoaded, setSymbolsLoaded] = useState(false)
  const [symbolsLoading, setSymbolsLoading] = useState(false)
  const [symbolsError, setSymbolsError] = useState<string | null>(null)
  const [symbolsTruncated, setSymbolsTruncated] = useState(false)
  const [expandedFiles, setExpandedFiles] = useState<Set<string>>(new Set())

  const ensureSymbolsLoaded = useCallback(async () => {
    if (symbolsLoaded || symbolsLoading) return
    setSymbolsLoading(true)
    setSymbolsError(null)
    try {
      const all = await listAllSymbols(documentId, { limit: 5000 })
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
      setSymbolsError(err instanceof Error ? err.message : String(err))
    } finally {
      setSymbolsLoading(false)
    }
  }, [documentId, symbolsLoaded, symbolsLoading])

  const toggleFile = useCallback(async (filePath: string) => {
    // Expanding requires symbols loaded; collapsing does not.
    if (!expandedFiles.has(filePath)) {
      await ensureSymbolsLoaded()
    }
    setExpandedFiles(prev => {
      const next = new Set(prev)
      if (next.has(filePath)) next.delete(filePath)
      else next.add(filePath)
      return next
    })
  }, [expandedFiles, ensureSymbolsLoaded])
```

- [ ] **Step 3: Add a clipboard-copy hook for the location strings**

Add this inside `CodeRelationPanel` (above the `return`):

```tsx
  const [copyFeedback, setCopyFeedback] = useState<string | null>(null)
  const handleCopy = useCallback(async (text: string, id: string) => {
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
    setTimeout(() => setCopyFeedback(null), 1500)
  }, [])
```

- [ ] **Step 4: Render the truncation warning + pass props to `FileGroup`**

In the `CodeRelationPanel` return, inside the `<div className="p-4 space-y-3">`, add the truncation/error banners right after the search box (before the `filesError` block):

```tsx
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
```

Then change the `FileGroup` usage in the file list to pass the new props. Find:
```tsx
              <FileGroup key={f.path} file={f} documentId={documentId} searchQuery={searchQuery} />
```
Replace with:
```tsx
              <FileGroup
                key={f.path}
                file={f}
                document={documentId}
                expanded={expandedFiles.has(f.path)}
                symbols={expandedFiles.has(f.path) ? (symbolsByFile.get(f.path) ?? []) : []}
                symbolsLoading={symbolsLoading && !symbolsLoaded}
                onToggle={() => void toggleFile(f.path)}
                onCopy={handleCopy}
                copyFeedback={copyFeedback}
              />
```

- [ ] **Step 5: Replace the stub `FileGroup` with the full implementation**

Replace the entire stub `FileGroup` function at the bottom of the file with:

```tsx
function FileGroup({
  file,
  expanded,
  symbols,
  symbolsLoading,
  onToggle,
  onCopy,
  copyFeedback,
}: {
  file: CodeFile
  document: string
  expanded: boolean
  symbols: CodeSymbolNode[]
  symbolsLoading: boolean
  onToggle: () => void
  onCopy: (text: string, id: string) => void
  copyFeedback: string | null
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
              {symbols.map((sym, idx) => (
                <SymbolLocationRow
                  key={`${sym.name}-${idx}`}
                  name={sym.name}
                  kind={sym.kind}
                  file_path={sym.file_path}
                  start_line={sym.start_line}
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

// Read-only symbol row showing name/kind + clickable file:line location.
// (Expandable tree version is SymbolNode in Task 5.)
function SymbolLocationRow({
  name, kind, file_path, start_line, onCopy, copyFeedback,
}: {
  name: string | null
  kind: string | null
  file_path: string | null
  start_line: number | null
  onCopy: (text: string, id: string) => void
  copyFeedback: string | null
}) {
  const locId = `loc-${file_path}-${start_line}`
  const copyText = `${file_path}:${start_line}`
  return (
    <div className="flex items-center justify-between px-3 py-1 text-xs">
      <span style={{ fontFamily: 'var(--font-mono)' }}>
        <span style={{ color: 'var(--color-accent)' }}>{name ?? '?'}</span>
        <span style={{ color: 'var(--color-text-tertiary)' }}> ({kind ?? '?'})</span>
      </span>
      <button
        className="cursor-pointer hover:underline flex items-center gap-1"
        style={{ fontFamily: 'var(--font-mono)', color: 'var(--color-text-tertiary)' }}
        title={`复制 ${copyText}`}
        onClick={() => onCopy(copyText, locId)}
      >
        {copyFeedback === locId ? <span style={{ color: 'var(--color-success)' }}>已复制</span> : (
          <>{displayPath(file_path ?? '')}:{start_line ?? '?'} <Copy size={11} /></>
        )}
      </button>
    </div>
  )
}
```

- [ ] **Step 6: Verify type check + build + manual render**

Run: `cd control-panel && npx tsc -b && npm run build`
Expected: succeeds.

Rebuild + verify:
```bash
sudo DOCKER_BUILDKIT=1 docker compose up -d --build control-panel
```
Click "调用关系", then click a file row. Expected: file expands, shows its symbols (name + kind + `file:line` with copy icon). Click the `file:line` -> "已复制" appears, clipboard has `src/src/App.tsx:15`. First expand of any file shows a brief "加载符号..." then all files expand instantly after.

- [ ] **Step 7: Commit**

```bash
git add control-panel/src/pages/CodeRelationPanel.tsx
git commit -m "feat(code-rag): expandable file groups with symbol list + file:line copy

One-time full symbol fetch on first file expand, grouped by file_path.
Each symbol row shows clickable file:line location (copies to clipboard)."
```

---

## Task 4: Recursive `SymbolNode` with caller/callee expansion

**Files:**
- Modify: `control-panel/src/pages/CodeRelationPanel.tsx` (add `SymbolNode` recursive component; wire it into `FileGroup`'s symbol list so symbols are expandable into a call tree)

**Interfaces:**
- Produces: `SymbolNode` recursive component (props: node data + ancestor path + shared cache/callbacks). Expanding a symbol fetches 1-hop callers + callees in parallel, caches them, renders child `SymbolNode`s under "调用者"/"被调用" sections. `FileGroup` switches from `SymbolLocationRow` to `SymbolNode`.

- [ ] **Step 1: Update imports + add `nodeKey`/`SymbolNodeData` helpers (first used in this task)**

Update the import lines in `CodeRelationPanel.tsx`:

```tsx
import { useEffect, useState, useCallback } from 'react'
import { X, GitCompareArrows, ChevronRight, ChevronDown, Loader2, Search, Copy, CornerLeftUp } from 'lucide-react'
import Card from '@/components/Card'
import { listCodeFiles, listAllSymbols, getCodeCallers, getCodeCallees } from '@/api/client'
import type { CodeFile, CodeSymbolNode, CodeSymbolRef } from '@/api/client'
```

Add these helpers near the top of the file (after `displayPath`):

```tsx
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
```

- [ ] **Step 2: Add relation-cache state + loader to `CodeRelationPanel`**

Inside `CodeRelationPanel`, after the `expandedFiles` state from Task 3, add:

```tsx
  // Per-symbol 1-hop relation cache. Key = nodeKey(file, name, line).
  const [relationCache, setRelationCache] = useState<Map<string, { callers: CodeSymbolRef[]; callees: CodeSymbolRef[] }>>(new Map())
  const [expandedSymbols, setExpandedSymbols] = useState<Set<string>>(new Set())
  const [relationLoading, setRelationLoading] = useState<Set<string>>(new Set())
  const [relationErrors, setRelationErrors] = useState<Map<string, string>>(new Map())

  const loadRelation = useCallback(async (key: string, symbolName: string) => {
    if (relationCache.has(key)) return
    setRelationLoading(prev => new Set(prev).add(key))
    setRelationErrors(prev => { const n = new Map(prev); n.delete(key); return n })
    try {
      const [callers, callees] = await Promise.all([
        getCodeCallers(documentId, symbolName),
        getCodeCallees(documentId, symbolName),
      ])
      setRelationCache(prev => {
        const n = new Map(prev)
        n.set(key, { callers, callees })
        return n
      })
    } catch (err) {
      setRelationErrors(prev => {
        const n = new Map(prev)
        n.set(key, err instanceof Error ? err.message : String(err))
        return n
      })
    } finally {
      setRelationLoading(prev => { const n = new Set(prev); n.delete(key); return n })
    }
  }, [documentId, relationCache])

  const toggleSymbol = useCallback((key: string, symbolName: string) => {
    const willExpand = !expandedSymbols.has(key)
    if (willExpand) {
      void loadRelation(key, symbolName)
    }
    setExpandedSymbols(prev => {
      const n = new Set(prev)
      if (n.has(key)) n.delete(key)
      else n.add(key)
      return n
    })
  }, [expandedSymbols, loadRelation])
```

- [ ] **Step 3: Pass relation state/callbacks down through `FileGroup` to `SymbolNode`**

Add to `CodeRelationPanel`'s `FileGroup` usage props (extend the props object from Task 3 Step 4):

```tsx
                expandedSymbols={expandedSymbols}
                relationCache={relationCache}
                relationLoading={relationLoading}
                relationErrors={relationErrors}
                onToggleSymbol={toggleSymbol}
```

Update `FileGroup`'s props signature to accept and forward these:

```tsx
function FileGroup({
  file, expanded, symbols, symbolsLoading, onToggle, onCopy, copyFeedback,
  expandedSymbols, relationCache, relationLoading, relationErrors, onToggleSymbol,
}: {
  file: CodeFile
  document: string
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
```

In `FileGroup`'s expanded body, replace the `SymbolLocationRow` mapping with `SymbolNode`:

```tsx
            <div className="px-1 pb-1 space-y-0.5">
              {symbols.map((sym, idx) => (
                <SymbolNode
                  key={`${sym.name}-${idx}`}
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
```

- [ ] **Step 4: Implement the recursive `SymbolNode` component**

Add this component to `CodeRelationPanel.tsx` (replaces the use of `SymbolLocationRow` inside the tree; `SymbolLocationRow` can stay for reference or be deleted - delete it to avoid an unused warning):

```tsx
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

  const renderChild = (child: SymbolNodeData, idx: number) => (
    <SymbolNode
      key={`${child.file_path}-${child.name}-${child.start_line}-${idx}`}
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
                { name: c.name, kind: c.kind, file_path: c.file_path, start_line: c.start_line }, idx,
              ))}
              <div className="text-xs mt-1" style={{ color: 'var(--color-text-tertiary)' }}>📤 被调用 (Callees) — {cached.callees.length}</div>
              {cached.callees.length === 0 ? (
                <div className="text-xs px-2" style={{ color: 'var(--color-text-tertiary)' }}>(无)</div>
              ) : cached.callees.map((c, idx) => renderChild(
                { name: c.name, kind: c.kind, file_path: c.file_path, start_line: c.start_line }, idx,
              ))}
            </div>
          ) : null}
        </div>
      )}
    </div>
  )
}
```

Now delete the `SymbolLocationRow` function (it's no longer used - `FileGroup` uses `SymbolNode` now).

- [ ] **Step 5: Verify type check + build**

Run: `cd control-panel && npx tsc -b && npm run build`
Expected: succeeds with no unused-symbol errors.

- [ ] **Step 6: Manual verification of the recursive tree**

Rebuild:
```bash
sudo DOCKER_BUILDKIT=1 docker compose up -d --build control-panel
```
Open the panel, expand a file, click a symbol. Expected: the symbol expands, showing "📥 调用者" and "📤 被调用" sections with child symbol rows. Click a child symbol -> it expands further (recursive). Each row shows `file:line` + copy icon. Child rows are indented with a left border.

- [ ] **Step 7: Commit**

```bash
git add control-panel/src/pages/CodeRelationPanel.tsx
git commit -m "feat(code-rag): recursive SymbolNode with caller/callee expansion

Each symbol expands into 1-hop callers + callees (parallel fetch, cached).
Child nodes are recursive SymbolNodes - builds the call tree layer by layer.
Cycle guard via ancestor-path key check."
```

---

## Task 5: Search-symbol filtering + final polish

**Files:**
- Modify: `control-panel/src/pages/CodeRelationPanel.tsx` (extend search to also filter symbols within expanded files; verify debounce; final visual polish)

**Interfaces:**
- Consumes: all prior tasks. Produces: search filters both file paths and symbol names within expanded files.

- [ ] **Step 1: Extend search to filter symbols within expanded files**

Currently `searchQuery` only filters files. When a search is active, auto-expand matching files and filter their symbols. In `CodeRelationPanel`, change the `FileGroup` rendering to pass a filtered symbol list when searching:

Find the `visibleFiles.map` block and the `FileGroup` usage. Replace the `symbols={...}` prop computation so that when `searchQuery` is non-empty, each file's symbols are filtered to those matching the query, and matching files are force-expanded:

Add a helper inside `CodeRelationPanel` (before `return`):
```tsx
  const sq = searchQuery.trim().toLowerCase()
  const fileMatchesSearch = (f: CodeFile) => !sq || f.path.toLowerCase().includes(sq)
  const filterSymbols = (syms: CodeSymbolNode[]) => {
    if (!sq) return syms
    return syms.filter(s => (s.name ?? '').toLowerCase().includes(sq) || (s.qualified_name ?? '').toLowerCase().includes(sq))
  }
```

Update the `visibleFiles` line to also restrict to matching files when searching:
```tsx
  const matchingFiles = searchQuery.trim()
    ? files.filter(fileMatchesSearch)
    : files
  const visibleFiles = matchingFiles.slice(0, filePage * FILES_PER_PAGE)
  const hasMoreFiles = visibleFiles.length < matchingFiles.length
```

(Replace the prior `filteredFiles` references with `matchingFiles`.)

In the `FileGroup` usage, when searching, force-expand files that have matching symbols:
```tsx
              <FileGroup
                key={f.path}
                file={f}
                document={documentId}
                expanded={sq ? filterSymbols(symbolsByFile.get(f.path) ?? []).length > 0 : expandedFiles.has(f.path)}
                symbols={sq ? filterSymbols(symbolsByFile.get(f.path) ?? []) : (expandedFiles.has(f.path) ? (symbolsByFile.get(f.path) ?? []) : [])}
                symbolsLoading={symbolsLoading && !symbolsLoaded}
                onToggle={() => void toggleFile(f.path)}
                onCopy={handleCopy}
                copyFeedback={copyFeedback}
                expandedSymbols={expandedSymbols}
                relationCache={relationCache}
                relationLoading={relationLoading}
                relationErrors={relationErrors}
                onToggleSymbol={toggleSymbol}
              />
```

Note: when `sq` is non-empty, symbols must already be loaded for filtering to work. Add an effect to auto-load symbols when a search starts:

```tsx
  useEffect(() => {
    if (searchQuery.trim() && !symbolsLoaded && !symbolsLoading) {
      void ensureSymbolsLoaded()
    }
  }, [searchQuery, symbolsLoaded, symbolsLoading, ensureSymbolsLoaded])
```

- [ ] **Step 2: Add 300ms debounce to the search input**

Replace the search `onChange` handler. Add a debounced query state at the top of `CodeRelationPanel`:

```tsx
  const [searchInput, setSearchInput] = useState('')
  useEffect(() => {
    const t = setTimeout(() => setSearchQuery(searchInput), 300)
    return () => clearTimeout(t)
  }, [searchInput])
```

Change the search input to bind to `searchInput` and just update it:
```tsx
              onChange={e => { setSearchInput(e.target.value); setFilePage(1) }}
              value={searchInput}
```

(Remove the old direct `setSearchQuery` binding.)

- [ ] **Step 3: Verify type check + build**

Run: `cd control-panel && npx tsc -b && npm run build`
Expected: succeeds.

- [ ] **Step 4: Manual verification of search + full flow**

Rebuild:
```bash
sudo DOCKER_BUILDKIT=1 docker compose up -d --build control-panel
```
- Type a symbol name (e.g. "App") in the search box. After 300ms, files containing matching symbols auto-expand and show only matching symbols.
- Clear search -> files collapse back to normal state.
- Expand a symbol -> callers/callees load. Expand a child -> deeper level loads.
- Click any `file:line` -> "已复制", clipboard has the full `src/...:line` string.
- Test on a repo with a known cycle (if available) -> "↩ 循环引用" appears grayed out.

- [ ] **Step 5: Commit**

```bash
git add control-panel/src/pages/CodeRelationPanel.tsx
git commit -m "feat(code-rag): symbol search with debounce + auto-expand

Search filters file paths and symbol names; matching files auto-expand
with only matching symbols shown. 300ms debounce on input."
```

---

## Task 6: Verify production build + update CLAUDE.md

**Files:**
- Verify: full docker rebuild + health check
- Modify: `CLAUDE.md` (Known States section)

- [ ] **Step 1: Full rebuild + health check (per CLAUDE.md workflow rule 1)**

```bash
sudo DOCKER_BUILDKIT=1 docker compose up -d --build control-panel
sudo docker exec aigateway-gateway-1 curl -sf localhost:8000/health
sudo docker compose logs --tail=50 control-panel 2>&1 | grep -i error || echo "no errors"
```
Expected: health OK, no errors in control-panel logs.

- [ ] **Step 2: Update CLAUDE.md Known States**

In `CLAUDE.md`, under "Known States & Gotchas", update the "Code RAG is now a separate subsystem" bullet to note the new inline call-graph panel. Add a short sentence:

```
- Control Panel "调用关系" now opens an inline (non-modal) expandable call-graph panel below the repo table: file list -> symbols (file:line copy) -> recursive caller/callee tree with cycle guard.
```

Run `wc -l CLAUDE.md` first; if over ~300 lines, prune before adding (per workflow rule 4).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: note inline call-graph panel in CLAUDE.md Known States"
```

- [ ] **Step 4: Final user review**

Surface the working feature to the user for review (per workflow rule 0 - never auto-merge). Summarize what was built and the verification done. Do NOT push or merge until the user approves.

---

## Self-Review

**1. Spec coverage:**
- ✅ Symbols grouped by file, expandable/collapsible → Task 3 (`FileGroup` + `SymbolNode`)
- ✅ Each symbol expands into callers + callees → Task 4 (`SymbolNode` recursive)
- ✅ Fast initial load (file list only) → Task 2 (`listCodeFiles` on mount)
- ✅ Symbols loaded lazily on first expand → Task 3 (`ensureSymbolsLoaded`)
- ✅ In-memory cache, cleared on close → Task 3/4 (state lives in `CodeRelationPanel`, unmounts on close)
- ✅ No backend changes → Task 1 uses existing endpoints
- ✅ No modal → Task 2 (inline Card)
- ✅ file:line display + click-to-copy → Task 3/4 (`SymbolNode` location button)
- ✅ Cycle prevention → Task 4 (`ancestorKeys` + `isCycle`)
- ✅ File list pagination (100/page) → Task 2 (`FILES_PER_PAGE` + `filePage`)
- ✅ Symbol truncation warning → Task 3 (`symbolsTruncated`)
- ✅ Search with debounce → Task 5
- ✅ Error handling per failure mode → Tasks 2/3/4 (filesError, symbolsError, relationErrors)
- ✅ CLAUDE.md update → Task 6

**2. Placeholder scan:** No TBD/TODO. All code blocks are complete. Manual verification steps have exact commands + expected output.

**3. Type consistency:**
- `CodeFile` defined Task 1, used Task 2/3/5 ✅
- `listCodeFiles` / `listAllSymbols` defined Task 1, used Task 2/3 ✅
- `SymbolNodeData` defined Task 2, used Task 4 (`SymbolNode` props) ✅
- `nodeKey(filePath, name, line)` signature consistent across Task 2/4 ✅
- `SymbolNode` props (`expandedSymbols`, `relationCache`, `relationLoading`, `relationErrors`, `onToggleSymbol`) defined Task 4, threaded through `FileGroup` consistently ✅
- `toggleSymbol(key, symbolName)` signature consistent ✅

`noUnusedLocals: true` is enabled, so each task's import block contains only the symbols first used in that task (Task 2 imports the stub minimum; Task 3 adds `listAllSymbols`/`CodeSymbolNode`/chevron+copy icons; Task 4 adds `getCodeCallers`/`getCodeCallees`/`CodeSymbolRef`/`CornerLeftUp` + `nodeKey`/`SymbolNodeData` helpers). Step 2 of each task runs `npx tsc -b` to confirm no unused-symbol errors before moving on.
