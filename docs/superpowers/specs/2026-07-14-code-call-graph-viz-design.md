# Code Call Graph Visualization Design

**Date:** 2026-07-14
**Status:** Approved
**Component:** Control Panel Knowledge Code Tab

## Problem

When clicking the "调用关系" (Call Relationship) button on a repository row, the current UI opens a modal with only a search box. The user must know the exact symbol name to type in. Results are rendered as a flat list with no hierarchy. For large codebases (thousands of symbols), this is unusable.

## Goals

1. Show all symbols grouped by file, expandable/collapsible
2. Each symbol expands into a **call-relationship tree** (callers + callees), recursively expandable layer by layer
3. Fast initial load (file list only); symbols loaded lazily on first expand
4. In-memory cache during the panel's lifetime (clear on close)
5. **No backend changes** - use existing APIs only
6. **No modal/popup** - inline panel below the repository table

## Test Results (verified against production data)

Tested on repo `code_46aa48c4fde3` (23 files, 389 symbols) via both Python function calls and HTTP endpoints:

| Check | Result |
|-------|--------|
| `query_symbols(repo, '', kind='function', limit=1000)` | ✅ Returns 141 functions (empty-string search works) |
| `query_symbols(repo, '', limit=1000)` (no kind) | ✅ Returns 389 symbols (all kinds: function/class/method/import/component/...) |
| HTTP `GET /query?symbol=&kind=function&limit=5` | ✅ Returns symbols (empty `symbol=` param parses correctly) |
| HTTP `GET /query?kind=function&limit=5` (no symbol) | ❌ 422 (symbol is required - frontend must send empty string, not omit) |
| `/files` path format vs `/query` file_path format | ✅ Both carry `src/` prefix (grouping won't misalign) |
| Large limit behavior | ✅ `limit=500/1000/5000/10000` all return full 389 (no truncation) |
| `limit=5` with empty search | ⚠️ Returns 25 (codegraph `--limit` partially ignored on empty search - use large limit) |
| `get_callers`/`get_callees` hop depth | ⚠️ Only 1-hop (no depth param) |

**Conclusion:** Empty-string search works at the HTTP layer. No new backend endpoint needed. 1-hop callers/callees is sufficient because the tree is built by **recursive client-side expansion** (each node fetches its own 1-hop relations on expand).

## Non-Goals (out of scope)

- Graph visualization (force-directed / tree diagram) - v1 is a text-based indent tree
- Persistent cache across panel close/open
- Multi-repo relationship queries
- Backend changes (new endpoints)
- "One-click N-hop" depth selector - replaced by manual layer-by-layer expansion

## Architecture

### Backend (no changes)

Existing APIs used:

| API | Usage |
|-----|-------|
| `GET /admin/rag/code/repositories/{id}/files` | Load file list on panel open (lightweight: path/language/node_count/size) |
| `GET /admin/rag/code/repositories/{id}/query?symbol=&limit=5000` | Load all symbols on first file expand (empty-string search returns full set) |
| `GET /admin/rag/code/repositories/{id}/callers?symbol=X` | Expand a symbol node -> 1-hop callers |
| `GET /admin/rag/code/repositories/{id}/callees?symbol=X` | Expand a symbol node -> 1-hop callees |

### Frontend Changes

#### 1. New API client function (`control-panel/src/api/client.ts`)

```typescript
// List all symbols via empty-string search (no new backend endpoint needed)
export async function listAllSymbols(
  documentId: string,
  opts?: { kind?: string; limit?: number },
): Promise<CodeSymbolNode[]> {
  const headers = await ensureAuthHeaders()
  const params = new URLSearchParams({ symbol: '', limit: String(opts?.limit ?? 5000) })
  if (opts?.kind) params.set('kind', opts.kind)
  const res = await fetch(
    `${API_BASE}/admin/rag/code/repositories/${encodeURIComponent(documentId)}/query?${params}`,
    { headers },
  )
  if (!res.ok) throw new Error(`List symbols failed: ${res.status}`)
  return await res.json()
}
```

Also need a typed wrapper for `/files`:
```typescript
export interface CodeFile { path: string; language: string; node_count: number | null; size: number | null }
export async function listCodeFiles(documentId: string): Promise<CodeFile[]> { ... }
```

#### 2. Layout: inline panel (not modal)

Replace the `relationPanel && (...)` fixed-overlay modal with an inline `<Card>` rendered **below the repository table** when `relationPanel` is non-null. The panel:
- Has a header: title "调用关系" + current `document_id` + a "收起" (collapse) button (sets `relationPanel = null`)
- No backdrop, no `position: fixed`, no click-outside-to-close
- Takes full content width, `max-height: 70vh; overflow: auto` so it scrolls internally without hijacking the page

#### 3. State shape (`KnowledgeCodeTab.tsx`)

```typescript
interface RelationPanel {
  documentId: string
  loading: boolean            // file list loading
  error: string | null
  files: CodeFile[]
  symbolsByFile: Map<string, CodeSymbolNode[]>   // file_path -> symbols
  symbolsLoaded: boolean                         // true after first full-symbol fetch
  symbolsTruncated: boolean                      // true if returned count == limit (warn user)
  expandedFiles: Set<string>                     // files currently expanded
  expandedSymbols: Set<string>                   // symbol nodes currently expanded (key: "file:name")
  relationCache: Map<string, { callers: CodeSymbolRef[]; callees: CodeSymbolRef[] }>
  searchQuery: string                            // filters file paths AND symbol names
  filePage: number                               // file list pagination (100/page)
}
```

**React Map/Set note:** Mutating a `Map`/`Set` in place does NOT trigger re-render. Every update must create a new instance (e.g. `new Set(prev).add(x)`). This is a known footgun - the implementation must use immutable updates.

#### 4. Component hierarchy

```
RelationPanel (inline Card, below repo table)
├── PanelHeader (title, document_id, 收起 button)
├── Toolbar
│   └── SearchInput (filters file paths + symbol names, debounced 300ms)
├── FileGroupList (paginated, 100 files/page, "加载更多" at bottom)
│   └── FileGroup (per file)
│       ├── FileHeader (chevron, filename, language, node_count)
│       └── SymbolList (conditional on file expanded)
│           └── SymbolNode (recursive, per symbol)
│               ├── SymbolHeader (icon, name, kind, line, signature preview)
│               └── RelationTree (conditional on symbol expanded)
│                   ├── Callers section
│                   │   └── SymbolNode (recursive - can expand further)
│                   └── Callees section
│                       └── SymbolNode (recursive - can expand further)
```

`SymbolNode` is a **recursive component** - that's what builds the tree layer by layer.

#### 4a. Symbol location display (行号定位)

Every `SymbolNode` header shows the symbol's location prominently so the user can jump to it in their IDE:

- Display: `file_path:start_line` in monospace, secondary color, right-aligned on the header row (next to the symbol name/kind/signature). Both `CodeSymbolNode` (top-level file symbols) and `CodeSymbolRef` (caller/callee child nodes) carry `file_path` + `start_line`.
- Strip the `src/` prefix for display readability (db stores `src/src/App.tsx`; show `src/App.tsx`). Keep full path for the copy payload.
- Click-to-copy: clicking the location string copies `file_path:start_line` (full path, with `src/` prefix) to clipboard. Show a transient "已复制" tooltip for 1.5s. Reuse the existing copy-feedback pattern from `Logs.tsx` (`copyFeedback` state + `onCopy` handler).
- Hover: cursor-pointer + underline on the location string to signal it's clickable.

#### 5. Data loading flow

```
1. Click "调用关系" button
   -> GET /files -> populate file list (lightweight, millisecond-level)
2. Browse file list (no symbol data needed yet)
3. Click a file to expand
   -> IF !symbolsLoaded: GET /query?symbol=&limit=5000 (one-time full fetch)
      -> group by file_path into symbolsByFile
      -> set symbolsLoaded=true; if count==5000 set symbolsTruncated=true (warn)
   -> show that file's symbols from symbolsByFile (zero network after first load)
4. Click a symbol to expand
   -> IF relationCache[key] miss: GET /callers + GET /callees (parallel, 1-hop)
      -> cache in relationCache[key]
   -> render callers + callees as child SymbolNodes
5. Click a child symbol -> repeat step 4 (recursive, builds deeper layers)
6. Search input -> client-side filter on file paths + symbol names
```

#### 6. Cycle prevention (call graphs have cycles)

Call graphs can have cycles (A calls B, B calls A). Recursive expansion without guard causes infinite trees. Each `SymbolNode` receives an **ancestor path** (list of `"file:name"` from root to itself). When expanding:
- If a child's key is already in the ancestor path -> render it as a **disabled/collapsed node** labeled "↩ 循环引用" (click does nothing further), do NOT recurse.
- This bounds the tree depth to the longest acyclic path.

#### 7. Performance considerations for large codebases

- **File list pagination:** Show first 100 files, "加载更多" button at bottom
- **Symbol one-time fetch:** `limit=5000`; if returned == 5000, show warning "符号过多,建议用搜索过滤"
- **Debounce:** Search input debounced 300ms
- **Parallel loads:** Callers + callees queried in parallel per node
- **Timeout:** 10s per request, show "查询超时" on failure
- **Memory:** All caches live in `relationPanel` state; cleared when panel collapsed (set to null)

## Error Handling

| Failure | User-facing message | Recovery |
|---------|--------------------|----------|
| `/files` fails | "加载文件列表失败" | Retry button in panel body |
| `/query` (symbols) fails | "加载符号失败" | Retry button; file list still browsable |
| `/callers` or `/callees` fails | "加载调用关系失败" | Per-node error, doesn't block other nodes |
| Timeout | "查询超时,请重试" | Retry button on the failed node |

## Testing Plan

- Unit: `listAllSymbols` client function (mock fetch, verify empty `symbol=` param)
- Unit: cycle-prevention logic (ancestor path check)
- Integration: panel with 1000+ files (pagination, performance)
- Edge: empty repository, single-file repo, symbol with no callers/callees, cyclic call graph
- Manual: test on all 3 existing production repos

## Files Changed

| File | Change |
|------|--------|
| `control-panel/src/api/client.ts` | New `listAllSymbols()` + `listCodeFiles()` functions, `CodeFile` type |
| `control-panel/src/pages/KnowledgeCodeTab.tsx` | Replace modal with inline panel; add recursive `SymbolNode` component; rework `relationPanel` state |
