# Code RAG 导入卡死修复 — 设计文档

**日期**: 2026-07-23
**状态**: 设计已确认，待写实现计划
**背景**: 导入 GitHub 仓库 `dongwanyang/aigateway`（5,383 符号 / 12,329 edges）卡在 `splitting` 状态永远不推进，前端永远显示"导入中"。

## 根因

两条独立原因叠加：

1. **子进程风暴**：`splitter.build_symbol_chunks` 对每个符号节点调用 `_callers_list` + `_callees_list`（各 spawn 一次 `codegraph` CLI 子进程，`graph_query.py:86`）。5,383 符号 × 2 = ~10,766 次子进程启动，实测耗时数分钟。期间 `total` 不写（`code_rag_routes.py:608` 在整个循环结束后才写），状态冻结在 `splitting, total=0`。
2. **无孤儿任务清理**：网关重启后后台 `asyncio.Task` 死亡，但 Redis 任务 key（24h TTL）保留在非终态 `splitting`，无启动扫描推进。`list_code_tasks` 重新浮出，前端永久显示"导入中"。

## 验证的关键事实

codegraph db（`graph_repo_path/.codegraph/codegraph.db`）schema：

- **`nodes`**: `id`（TEXT，hash 稳定 join key），`kind`（function|method|class|file|import），`name`，`file_path`（恒带 `src/` 前缀），`start_line`，`end_line`，`signature`，`docstring` 等。
- **`edges`**: `source`/`target`（node id），`kind`（`'calls'` | `'contains'`）。
- 语义：`kind='calls'` 的边 `source→target` 表示 **source 调用 target**。
  - callees of X = `edges.source = X.id, kind='calls'` → join target
  - callers of X = `edges.target = X.id, kind='calls'` → join source

**性能实测**（真实规模 5,383 符号 / 12,329 edges）：两条 SQL 各 ~57ms，共 114ms，内存 ~2.8MB。对比旧方案 ~10,766 次子进程 spawn，提速 5,000–50,000×。正确性已用 probe db 验证（alpha callees=`[beta]`，beta callers=`[alpha]`）。

## 设计

### 1. callers/callees 改 db 直读（核心）

新增 `graph_query.read_call_edges(graph_repo_path)`，一次查全图：

```sql
-- callees map: {symbol_id: [target node rows]}
SELECT e.source AS sym_id, t.name, t.kind, t.file_path, t.start_line, t.end_line
FROM edges e JOIN nodes t ON t.id = e.target
WHERE e.kind = 'calls';

-- callers map: {symbol_id: [source node rows]}
SELECT e.target AS sym_id, s.name, s.kind, s.file_path, s.start_line, s.end_line
FROM edges e JOIN nodes s ON s.id = e.source
WHERE e.kind = 'calls';
```

返回两张 dict，key 是 node `id`（精确），value 是 ref 行列表。Python groupby。

改动函数：
- `splitter.build_symbol_chunks`：循环前调一次 `read_call_edges`，循环内按当前符号 node id 查 map（O(1)），不再调 `_callers_list`/`_callees_list`。
- `graph_query.get_callers`/`get_callees`/`lookup_related_symbols`/`lookup_symbol_metadata`：对外签名不变（`code_rag_routes.py` 的 `/callers`/`/callees` 端点仍用），内部从 CLI 改 db 读。BFS 多跳在内存 map 上走。
- `graph_query.get_node`：**保留 CLI**——它要源码块/签名/trail 的 markdown 输出，db 无现成 markdown，改写代价不值；仅 Control Panel 单符号点开时触发，量小。

CLI 只在 `build_code_graph`（`codegraph init`）出现一次，split/retrieve 阶段零子进程。

### 2. 检索路径懒加载缓存（方案 A）

每个 `graph_repo_path` 首次检索时建 map，进程内缓存。失效靠 `files.content_hash` 快照比对：

```
缓存 value: (file_hashes_snapshot, callers_map, callees_map)
检索时:
  1. 读当前 files 表 hash 快照(SELECT path, content_hash FROM files, ~5ms)
  2. 与缓存快照比对
  3. 一致 → 用缓存 map (O(1))
  4. 不一致 → 重建 map, 更新快照
```

不需要 sync 端点主动清缓存——`/sync` 改了 db 的 `files.content_hash`，下次检索自动失效重建。内存：每仓库 ~2.8MB map + ~200KB hash 快照，10 仓库 ~30MB。

**split 阶段不污染缓存**：导入时只跑一次建 map 用完即丢，不进缓存；首次检索时才懒加载。

### 3. 启动时孤儿任务清理

`main.py` lifespan 里 `sqlite_store` 初始化后调 `code_rag_routes.sweep_orphaned_tasks(sqlite_store)`（不依赖 Redis，可放在 sqlite_store 就绪后、yield 前）：

```
查询 code_rag_tasks WHERE status NOT IN (completed, failed, cancelled)
  每个命中行:
    UPDATE status='failed', error='worker restarted during import'
    (保留行不删, 让前端查到失败原因)
```

幂等：终态行不进分支，failed 是终态，多次重启不重复处理。

### 4. SQLite 任务表（替换 Redis 任务 key）

新增 `code_rag_tasks` 表，加到 `SQLiteStore`（复用 `aigateway.db`）：

```sql
CREATE TABLE IF NOT EXISTS code_rag_tasks (
    task_id         TEXT PRIMARY KEY,
    document_id     TEXT NOT NULL,
    status          TEXT NOT NULL,          -- pending|building_graph|splitting|embedding|completed|failed|cancelled
    done            INTEGER NOT NULL DEFAULT 0,
    total           INTEGER NOT NULL DEFAULT 0,
    current_file    TEXT,
    source_type     TEXT,
    source_label    TEXT,
    embedding_model TEXT,
    graph_repo_path TEXT,
    error           TEXT,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_code_rag_tasks_created ON code_rag_tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_code_rag_tasks_document ON code_rag_tasks(document_id);
```

读写改造（`code_rag_routes.py`）：

| 函数 | 现在 | 改后 |
|---|---|---|
| `_write_task_state` | Redis `HSET`+`EXPIRE` | `sqlite_store.upsert_code_rag_task`（upsert by task_id，刷 `updated_at`） |
| `_read_task_state` | Redis `HGETALL` | `sqlite_store.read_code_rag_task` |
| `_delete_task_key` | Redis `DEL` | **移除**——终态留表当历史 |
| `list_code_tasks` | Redis `SCAN` | `sqlite_store.list_code_rag_tasks(limit, offset)` ORDER BY created_at DESC |
| `cancel_code_task` | Lua 比对 + `HSET` | `UPDATE ... SET status='cancelled' WHERE task_id=? AND status NOT IN (completed, failed, cancelled)` |
| 进度回写 | `await _mark(...)` 写 Redis | `await _mark(...)` 写 SQLite |

关键点：
- `list_code_tasks` 加分页 `?limit=50&offset=0`（默认 50）。表不清理，必须有界查询。Redis 靠 TTL 清，SQLite 不清就得有界。
- 前端 JSON shape 不变（`task_id/status/done/total/current_file/error/source_label/source_type/created_at`），前端无感。
- **sync 端点不进任务表**——它是同步阻塞调用（一次性返回 `synced_files/refreshed_symbols`），无 task_id 无进度回写。但 sync 触发段 2 的缓存失效（自动）。
- `_run_code_import_task_with_deadline` 的超时分支写 `status=failed` 改走 SQLite。

### 5. 导入进度回写

`build_symbol_chunks` 加可选 `progress_cb` 参数：

```
total_nodes = len(nodes)   # 循环前已知(_read_symbol_nodes 已返回完整 list)
for i, node in enumerate(nodes):
    ...处理...
    if progress_cb and i % 200 == 0:
        progress_cb(done=i, total=total_nodes, current_file=rel_path)
progress_cb and progress_cb(done=total_nodes, total=total_nodes)  # 收尾
```

`_run_code_import_task` 传回调。线程跨界：`build_symbol_chunks` 跑在 `run_in_executor` 线程，回调内用 `asyncio.run_coroutine_threadsafe(_mark(...), loop).result()` 把 async 的 `_mark` 调度回主 event loop。

回写频率：每 200 符号一次（5,383 符号 ≈ 27 次）。embedding 阶段已有进度（`code_rag_routes.py:696`），复用 splitting 写入的 `total`，进度条平滑过渡。前端 `KnowledgeCodeTab.tsx` 已轮询 done/total/current_file，**前端不用改**。

### 6. 次要修复

- **`source_label` 去前缀**（`code_rag_routes.py:861`）：`f"git://{body.git_url}"` → 直接用 `body.git_url`（或 `f"git: {body.git_url}"`），消除畸形 `git://https://...`。
- **`config.py` 白名单**（`shared/config.py:219`）：`allowed_top_level` 加 `"code_rag"`，消除启动告警。
- **deadline 杀子进程**（`graph_builder.py` + `graph_query.py`）：`subprocess.run` 改 `subprocess.Popen(..., start_new_session=True)`，超时/取消时 `os.killpg(os.getpgid(pid), SIGTERM)` 回收进程组。次要加固——db 直读后 deadline 触发概率低，但防御性。
- **孤儿临时目录清理**：`sweep_orphaned_tasks` 顺手扫 `/tmp/code_rag_folder_*` 和 `/data/code_graphs/*/.tmp/`，清掉旧失败残留。一次性，不做长期机制。

### 不做（YAGNI）

- 任务重试机制（用户手动重导即可）。
- 任务取消的信号文件/状态机（现有 `cancel()` + `asyncio.Task.cancel()` 够用）。
- 不动 `split_code_directory`（老兼容路径，0 生产引用，单测在用，不删不改）。
- 不测真实 5k 符号端到端性能（benchmark 性质，非回归测试）。

## 测试

1. **graph_query 单测（无 codegraph 依赖）**：临时 SQLite db 手建 nodes+edges，验证 `read_call_edges` 返回正确；验证 file-hash 缓存失效（改 hash → 重建）；验证 `get_callers`/`get_callees`/`lookup_related_symbols` db 直读后行为与旧 CLI 路径一致。
2. **splitter 单测**：`build_symbol_chunks` 传 `progress_cb`，验证回调被调用、done/total 正确；复用 `_build_codegraph_repo` fixture（真实 CLI，skip if not installed）回归保险。
3. **code_rag_routes 单测**：`upsert/read/list_code_rag_task` SQLite 读写（`:memory:` 或 tmp db）；`sweep_orphaned_tasks`（非终态标 failed，终态不动）；`list_code_tasks` 分页（60 条 → limit=50 返 50 且 DESC）；进度回写（mock build_symbol_chunks 触发 progress_cb 验证 SQLite 更新）。
4. **边界**：`start_new_session`+`killpg`（mock Popen 验证调 `os.killpg`）；`source_label` 无 `git://` 前缀；`config.py` 白名单加 `code_rag` 后不告警。
5. **现有回归**：`test_code_rag_helpers.py:279` 的 callers=`["register"]`/callees=`["hash_password"]` 断言应仍成立（join 改 id 但语义不变，有差异则更新断言）；`test_code_rag_routes.py` 的 `redis_mgr` mock 改 `sqlite_store` mock。

## 预期效果

- 导入：`codegraph init`（~30s，一次）+ `read_call_edges`（~114ms）+ split（O(1) 查 map）+ embedding（已有进度），全程可观测进度，不再卡死。
- 重启后孤儿任务自动标 failed，前端不再永远"导入中"。
- 任务历史永久保留可查。
- 检索：单符号点开从 ~100ms（2×子进程）→ <1ms（内存 map）。

## 改动文件清单

| 文件 | 改动 |
|---|---|
| `aigateway-core/.../code_rag/graph_query.py` | 新增 `read_call_edges` + 懒加载缓存（file-hash 失效）；callers/callees 系列改 db 读；`get_node` 保留 CLI；`start_new_session`+`killpg` |
| `aigateway-core/.../code_rag/splitter.py` | `build_symbol_chunks` 加 `progress_cb`；循环前建 map，循环内 O(1) 查 |
| `aigateway-core/.../shared/config.py` | `allowed_top_level` 加 `code_rag` |
| `aigateway-api/.../code_rag_routes.py` | 任务状态从 Redis 改 SQLite；`_delete_task_key` 移除；`list_code_tasks` 分页；新增 `sweep_orphaned_tasks`；进度回写走 SQLite；`source_label` 去前缀；`/sync` 末段加缓存失效（自动） |
| `aigateway-api/.../main.py` | lifespan 调 `sweep_orphaned_tasks` |
| `SQLiteStore` | 新增 `code_rag_tasks` 表 + upsert/read/list 方法 |
| `graph_builder.py` | `start_new_session`+`killpg` |
| 测试 | `test_code_rag_helpers.py` / `test_code_rag_routes.py` 更新 |
