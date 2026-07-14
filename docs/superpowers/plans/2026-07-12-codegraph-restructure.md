# Plan: 代码知识图谱重构 — codegraph CLI + 结构描述嵌入

> **与 spec 的关系（重要）**：同目录 spec `2026-07-12-codegraph-code-knowledge-base-workbench-design.md`
> 提出过 Node `codegraph-service` sidecar 架构。**本计划已放弃 sidecar 方案**，改走 codegraph CLI
> 子进程直查 `.codegraph` 目录。理由：(1) sidecar 引入新服务、新 HTTP 契约、watcher 生命周期管理，
> 与 MVP 目标（修嵌入内容 + 替换手写 SQL + 增量更新）不成比例；(2) codegraph CLI 离线可用、
> 无进程间状态，子进程调用足够；(3) spec §3 已把 "CLI-based CodeGraph integration as the primary
> runtime path" 列为 Non-goal，本计划显式反转该决定。spec 中的 repo catalog / operation model /
> watcher / workbench UI 等**不在本计划范围**，留待后续按需评估。本计划**取代 spec §6（架构）与
> §10（sidecar 设计）**；spec 其余部分（源类型能力矩阵、生命周期语义）仍可作为参考。

## Context

当前代码知识库（Code RAG）存在三个核心问题：

1. **重复造轮子**：`graph_query.py` 手写 ~308 行 SQLite 查询（callers/callees/BFS），而官方 codegraph CLI（`@colbymchenry/codegraph` v1.4.1，由 Dockerfile `npm install -g` 安装）一个命令就能完成，且更准（它自己产的 db，schema 最清楚；我们手写还踩过 `src/` 前缀、`_` 通配符等坑）。
2. **嵌入内容不当**：现在 splitter 把**纯源码**送 embedding，导致代码实现细节淹没意图语义——用户问「登录认证」搜不到 `login` 函数，因为源码里是 `jwt.encode`/`hash_password` 这些实现词。
3. **反复查 Qdrant**：检索时 `_expand_code_hits_with_graph` 对每个 hit 回 Qdrant scroll 邻居源码，N 个符号 N 次往返，且增量更新做不到（源码嵌入每次改动都要重算向量）。

实测数据（本仓库 aigateway-core + aigateway-api，112 文件）：
- codegraph 索引：5915 节点、13039 边、19MB db、3.5s 建完
- 全量图谱 ~117K token，**不能全塞 prompt**，必须按需查询
- `codegraph query "登录bug"` 返回 `[]` —— **codegraph 不认自然语言意图，只认符号名**
- codegraph db 不存源码，`codegraph node` 拿源码依赖源文件在场
- `codegraph sync` 改一个文件只重处理该文件，`files` 表有 `content_hash` 可做增量对比

## 已确认的决策

| 决策 | 选择 | 理由 |
|---|---|---|
| 查询实现 | 全部替换为 codegraph CLI 子进程 | 不重复造轮子，更准 |
| 图谱存储 | 保留整个 `.codegraph/` 目录（不只 db） | codegraph CLI 查询需要目录结构 |
| CLI 连接方式 | 本地直查 `.codegraph` 目录 | 离线可用，不走 gateway |
| 嵌入内容 | **只嵌结构描述**（符号名/签名/callers/callees/docstring） | 意图语义强、向量稳定、增量友好 |
| 向量粒度 | **方案 A：一个符号一个向量** | codegraph node 与 Qdrant point 一一对应 |
| 源码存储 | 存 Qdrant payload `chunk_text`，不嵌入 | 检索命中返回，源码改动不重算向量 |
| 源码切分 | 保留 splitter 文件遍历，改用 codegraph 行号切源码 | 复用 splitter 的路径白名单/语言识别 |
| 增量更新 | 本次一并实现 | codegraph sync + 按 file_hash 刷新变了文件的符号 |
| CLI/API | 都做 | 给 AI 助手本地直查入口 |
| 存量数据 | 直接重新导入 | 存量仓库少，不维护兼容代码 |
| 实施顺序 | 一次性全部实现 | 8 个步骤一个 PR |

## 检索流程（重构后）

```
用户: "帮我修登录 bug"
   │
   ▼ 1. Qdrant 向量检索 (嵌入的是结构描述,非源码)
   命中: login 函数 (结构描述里 auth/callers/callees 命中意图)
   payload 直接带: chunk_text(源码) + callers/callees
   │
   ▼ 2. (可选) codegraph impact --depth N 拿多跳影响范围
   │
   ▼ 3. 喂 LLM: 源码 + 调用关系,命中即拿全,不用回查 Qdrant
```

## 实现步骤

### 步骤 1：改 graph_builder.py — 保留 .codegraph/ 目录

**文件**：`aigateway-core/src/aigateway_core/pipelines/understanding/code_rag/graph_builder.py`

当前 `build_code_graph` 把 `.codegraph/codegraph.db` 复制到 `{graph_db_dir}/{document_id}.db` 后删临时目录。改为保留整个 `.codegraph/` 目录到 `{graph_db_dir}/{document_id}/.codegraph/`。

- `build_code_graph(source_dir, graph_repo_path, timeout)` 参数从 `graph_db_path`（db 文件）改为 `graph_repo_path`（目录）
- 在 `{graph_repo_path}` 下创建 work_dir 跑 `codegraph init <work_dir>`（位置参数，**不接 `-p`**），完成后把 `.codegraph/` 移到 `{graph_repo_path}/.codegraph/`（不复制单个 db）
- 返回 `{graph_repo_path}`（codegraph CLI 用 `-p {graph_repo_path}` 查询）
- 删除「复制 db + 删临时目录」逻辑

**配套：`git` 源持久化（为步骤 6 sync 服务）**。当前 `git` 源 clone 到临时目录、加入 `cleanup_dirs`，导入完在 `finally`（`code_rag_routes.py:676`）删掉 → sync 时无源码。改为：

- import endpoint（`code_rag_routes.py:771-775`）：`git` 源 clone 到持久目录 `{graph_repo_path}/src/`（即 `{graph_db_dir}/{document_id}/src/`），**不进 `cleanup_dirs`**。注意：路径段必须是 `src/`——graph_builder 把源码 symlink 成 `work_dir/src` 后跑 `codegraph init`，db 里所有 `file_path` 都带 `src/` 前缀（如 `src/auth.py`）；持久化源码落在 `src/` 下才能让 `codegraph node`（输出源码块）与 `codegraph sync`（增量重扫）按 `files.path` 正确解析。
- `_materialize_git_repo` 增加可选 `dest_dir` 参数，指定时 clone 到该目录而非 `tempfile.mkdtemp`。
- `repo_meta` 新增 `workspace_path`（`{graph_repo_path}/src/`）与 `source_type`，供 sync 端点判断该仓是否可增量（`git`/`server_path` 可，`folder`/`zip` 不可）。
- 删仓时（步骤 4）删整个 `{graph_repo_path}/` 目录，顺带清掉 `src/`。
- `folder`/`zip` 源**仍走临时目录 + cleanup**（快照源，无 sync 需求；其检索源码来自 Qdrant payload 的 `chunk_text`，不依赖持久源文件）。

> `server_path` 不复制源码（源本就在场，路径白名单内的目录直接读），sync 时重扫原路径；但 `codegraph sync` 需要 db `files.path` 对应的源文件可解析，故 server_path 仓的 `workspace_path` 记录原 `server_path` 目录，sync 时把 `.codegraph/` 临时放到一个 `src/` symlink 指向该目录的结构下（或直接在原路径旁建 `.codegraph/`，见步骤 6 实现细节）。
> 这样 `git` 与 `server_path` 都成为 spec §5.1 的 managed 源，sync 可拉取/重扫源码后跑 `codegraph sync`。`folder`/`zip` 是 snapshot 源，sync 返回 400。

### 步骤 2：重写 graph_query.py — 替换为 codegraph CLI 子进程

**文件**：`aigateway-core/src/aigateway_core/pipelines/understanding/code_rag/graph_query.py`

删除所有手写 SQL（`_open_db`/`_find_symbol_node`/`_get_calls_from`/`_get_imports_for_file`/BFS），改为调用 codegraph CLI 并解析输出。

**CLI 接口实测（v1.4.1）—— `--json` 与 `-p` 支持不一致，必须分两类 helper**：

| 命令 | `--json` | `-p/--path` | 用法 |
|---|---|---|---|
| `query`/`callers`/`callees`/`impact`/`files` | ✅ | ✅ | JSON helper |
| `node` | ❌ 无 | ✅ | raw helper（markdown-ish 输出） |
| `init`/`index`/`sync` | ❌ | ❌（位置参数 `[path]`） | raw helper（位置参数） |

```python
def _run_codegraph_json(args: list[str], *, repo_path: str, timeout: float) -> Any:
    """跑支持 --json 的查询类命令(query/callers/callees/impact/files)。
    构造: codegraph <args...> -p <repo_path> --json。失败抛 RuntimeError。"""
    # subprocess.run(["codegraph", *args, "-p", repo_path, "--json"], ...)
    # 解析 stdout 为 JSON

def _run_codegraph_raw(args: list[str], *, timeout: float) -> str:
    """跑不支持 --json 的命令(init/index/sync/node),返回原始 stdout 文本。
    init/sync 用位置参数: codegraph sync <path>;不接 -p。"""
```

> 把现有 `graph_builder.py:29` 的 `_run_codegraph`（目前只在 build 阶段用）提取为这两个
> helper 的共用底层；`graph_query.py` 当前是纯 `sqlite3`、无 subprocess 代码，helper 落在
> `graph_query.py`（或新建 `_cli.py`）均可，但别引用成 `graph_query._run_codegraph`——它不存在。

```python
def lookup_symbol_metadata_strict(graph_repo_path, file_path, symbol_name, chunk_text) -> dict:
    """导入时用:调 codegraph query + callers + callees,失败抛异常。"""
    # 1. _run_codegraph_json(["query", symbol], repo=graph_repo_path) → node(kind/file_path/start_line)
    # 2. _run_codegraph_json(["callers", symbol], ...) → callers 列表
    # 3. _run_codegraph_json(["callees", symbol], ...) → callees 列表
    # 4. imports 从 query 结果的 node 关系推断

def lookup_related_symbols_strict(graph_repo_path, file_path, symbol_name, *, hops=1) -> list[dict]:
    """BFS 替换为 codegraph impact --depth N --json。"""
    # _run_codegraph_json(["impact", symbol, "--depth", str(hops)], ...) → affected 列表
```

保留 strict/tolerant 双模式（`lookup_symbol_metadata` tolerant 包装仍 catch 所有异常返回空）。

**新增**（给 CLI/API 层用）：
- `query_symbols(graph_repo_path, search, *, kind=None, limit=10)` — 包装 `codegraph query`（JSON）
- `get_callers(graph_repo_path, symbol)` / `get_callees(...)` / `get_impact(..., depth)` — 直接对应 CLI 命令（JSON）
- `list_files(graph_repo_path)` — 包装 `codegraph files --json`
- `get_node(graph_repo_path, symbol)` — 包装 `codegraph node`，**用 raw helper**（node 无 `--json`）。
  注意：`node` 符号模式只输出 location+signature+caller/callee trail，**不输出源码块**（无论源文件是否在场）；
  源码块仅在 `node -f <file>` 文件模式才有。所以 `get_node` 主要用于展示调用链，**源码获取走步骤 3
  的「用 db 的 start/end_line 从源文件切片」路径，不依赖 `node` 的源码输出**。

### 步骤 3：改 splitter.py — 用 codegraph 行号切源码 + 构造结构描述

**文件**：`aigateway-core/src/aigateway_core/pipelines/understanding/code_rag/splitter.py`

保留 `is_path_allowed`/`compute_line_span`/`_CODE_SUFFIXES`/文件遍历逻辑，但切分逻辑改为：

新增 `build_symbol_chunks(source_dir, graph_repo_path, ignore_patterns)`：
1. 从 codegraph db 读所有 `kind IN ('function','method','class')` 的节点（name/file_path/start_line/end_line/signature/docstring）
2. 对每个符号节点，用 `start_line`/`end_line` 从对应源文件切出源码（`chunk_text`）
3. 查该符号的 callers/callees（调 graph_query）
4. 构造**结构描述嵌入文本**：
   ```
   function login in auth.py
   signature: (user, pw) -> str
   callers: register
   callees: hash_password, jwt_encode
   docstring: 用户登录认证
   ```
5. 返回 chunk 列表，每个含：`embed_text`(结构描述) + `chunk_text`(源码) + `file_path`/`start_line`/`end_line`/`function_name`/`callers`/`callees`/`imports`/`signature`/`docstring`

`split_code_directory` 保留作为兼容入口，内部委托给 `build_symbol_chunks`。

### 步骤 4：改 code_rag_routes.py — 嵌入结构描述 + 保留 .codegraph 目录

**文件**：`aigateway-api/src/aigateway_api/code_rag_routes.py`

`_run_code_import_task` 改动：
1. `build_code_graph(source_dir, graph_repo_path)` — 参数改目录（步骤1）
2. `split_code_directory` → 改为 `build_symbol_chunks`（步骤3），拿到含 `embed_text` 的 chunk
3. embedding：`encode_texts(embedding_model, [c["embed_text"] for c in batch])` —— **嵌入结构描述而非源码**
4. payload 保持现有字段集（`chunk_text` 存源码），新增 `signature`/`docstring` 字段
5. 删除仓库时改删 `{graph_db_dir}/{document_id}/` 整个目录（不只是 .db 文件）

### 步骤 5：改 rag_retriever_plugin.py — 简化检索，减少回查

**文件**：`aigateway-core/src/aigateway_core/pipelines/understanding/rag/rag_retriever_plugin.py`

`_expand_code_hits_with_graph` 简化：
- **删掉 retrieval 里的冗余重查**：当前 `rag_retriever_plugin.py:563` 不信 payload、又调一次 `lookup_symbol_metadata` 并覆盖（`_expand_code_hit_metadata` @ :569）。导入时 `code_rag_routes.py:598-600` 已把 callers/callees/imports 存进 payload，所以直接读 payload 即可，**移除这条重查 + 覆盖逻辑**，否则改动后行为不变。
- 多跳扩展：调 `lookup_related_symbols`（已是 codegraph impact），结果按符号名回 Qdrant 取源码。**当前是串行最坏情况**：`_expand_code_hits_with_graph` 对每个 hit 调 `_fetch_related_code_chunks`（:580），后者在 `:496` 对每个相关文件 `await client.post(scroll)` 且无 `asyncio.gather` → 实际往返数 = `hits × 相关文件数`。改为**一次 scroll 拉全部相关符号**（用 `must` 过滤符号名集合），或用 `asyncio.gather` 并发各文件 scroll；别只合并成 "N 次"。
- `_format_code_hit` 加入 signature/docstring 展示

### 步骤 6：新增增量更新 — codegraph sync + 按文件刷新向量

**新增 API**：`POST /admin/rag/code/repositories/{document_id}/sync` in `code_rag_routes.py`

增量流程：
1. 读 `repo_meta`，校验 `source_type ∈ {git, server_path}`；`folder`/`zip` 返回 400「快照源不支持 sync，请重新导入」。
2. **刷新源码**（managed 源才有）：
   - `git`：在持久化的 `{graph_repo_path}/src/` 里 `git fetch + reset --hard origin/<branch>`（不重新 clone）。源码在 `src/` 下，db `files.path` 前缀就是 `src/`，`codegraph sync` 可直接解析。
   - `server_path`：原 `server_path` 目录本就在场（白名单内），重扫即可。由于 server_path 仓的 `.codegraph/` 在 `{graph_repo_path}/`、而源码在原路径，sync 前需让 `files.path`（`src/...` 前缀）能解析到源文件——做法：sync 时临时在 `{graph_repo_path}/` 下确保 `src` 是指向 `workspace_path`（原 server_path 目录）的 symlink（导入时已建立，sync 复用）。
3. `codegraph sync <graph_repo_path>` — 增量更新图谱（位置参数，**不接 `-p`**，走 `_run_codegraph_raw`，cwd 指向 `{graph_repo_path}`）
4. 对比 sync 前后 `files` 表的 `content_hash`，找出变化文件
5. 对每个变化文件：
   - Qdrant `delete_by_filter`（按 `document_id` + `file_path` 删旧 chunk）
   - 用 codegraph 新行号重切该文件的符号 → 重算结构描述向量 → upsert 新 chunk
6. 未变文件完全不动
7. 返回 `{synced_files, refreshed_symbols}`

**注意**：步骤 1 已把 `git` 源 clone 持久化到 `{graph_repo_path}/src/`（不进 cleanup_dirs），所以 git 可增量 sync。`server_path` 导入时把原路径 symlink 成 `{graph_repo_path}/src`（同样不进 cleanup），sync 时复用。`folder`/`zip` 仍走临时目录 + cleanup，sync 返回 400。

### 步骤 7：新增 CLI — aigateway codegraph 子命令

**新增文件**：`aigateway-cli/src/aigateway_cli/codegraph.py`

子命令（**全部走 admin API**，不本地直查 `.codegraph` —— 本地 CLI 用户通常无法访问服务端 `/data/code_graphs/...`，故统一通过 `GET/POST /admin/rag/code/repositories/{document_id}/*` 转发，由服务端跑 codegraph CLI）：
- `aigateway codegraph status` — 列仓库（走 API `GET /admin/rag/code/repositories`）
- `aigateway codegraph query <symbol> -d <document_id> [--kind K] [--limit N] [--json]`
- `aigateway codegraph callers <symbol> -d <document_id> [--json]`
- `aigateway codegraph callees <symbol> -d <document_id> [--json]`
- `aigateway codegraph impact <symbol> -d <document_id> [--depth N] [--json]`
- `aigateway codegraph node <symbol> -d <document_id>` — 调用链 trail（服务端走 `codegraph node -p <repo>`，符号模式在源在场时也会输出源码块；源码获取另有 db 行号切片兜底，见步骤 2/3）
- `aigateway codegraph files -d <document_id> [--json]`
- `aigateway codegraph sync -d <document_id>` — 走 API `POST .../sync` 触发增量

`-d/--document` 指定仓库 `document_id`（映射到服务端 `{graph_db_dir}/{document_id}` 目录）。`-p/--path` 本地路径不可用，已改为 API 路由。

**修改**：`aigateway-cli/src/aigateway_cli/__main__.py` 注册 `codegraph` 子解析器 + main 分支。

### 步骤 8：新增查询 API + Control Panel 功能按钮

**修改**：`aigateway-api/src/aigateway_api/code_rag_routes.py`

新增端点（供 CLI 与 Control Panel 调用，内部走 codegraph CLI）：
- `POST /admin/rag/code/repositories/{document_id}/sync` — 增量同步（步骤 6）
- `GET /admin/rag/code/repositories/{document_id}/query?symbol=X&kind=function&limit=10`
- `GET /admin/rag/code/repositories/{document_id}/callers?symbol=X`
- `GET /admin/rag/code/repositories/{document_id}/callees?symbol=X`
- `GET /admin/rag/code/repositories/{document_id}/impact?symbol=X&depth=2`
- `GET /admin/rag/code/repositories/{document_id}/files`

路径参数 `document_id` 映射到 `{graph_db_dir}/{document_id}` 目录，调 graph_query 的新函数。

**修改**：`control-panel/src/api/client.ts` — 新增 `syncCodeRepository(documentId)`、`queryCodeSymbols(...)`、`getCodeCallers/Callees/Impact(...)`、`listCodeFiles(...)` 封装。

**修改**：`control-panel/src/pages/KnowledgeCodeTab.tsx` — 在仓库列表每行（现仅有「删除」按钮，`KnowledgeCodeTab.tsx:705`）追加功能按钮：

- **同步** 按钮：仅 `source_type ∈ {git, server_path}` 显示；点击调 `syncCodeRepository`，loading 态 + 完成后提示 `synced_files/refreshed_symbols`。`folder`/`zip` 源隐藏该按钮（或置灰 tooltip「快照源不支持同步」）。
- **调用关系** 按钮：点开行内 drawer/弹窗，含符号搜索框 → 调 `queryCodeSymbols` → 选中符号后展示 `callers`/`callees`/`impact`（三个 tab，分别调对应 API）。无源码块展示（关系列表只展示 name/kind/file_path/start_line）。
- 保留现有「删除」按钮。

> UI 形态参考 spec §15（list/tree 优先，不做高级图谱可视化）。本计划只加按钮 + 最小可用查询面板；workbench 级 UI（repo catalog、operations panel、watcher）属 spec 范畴，不在本计划。

## 关键文件清单

| 文件 | 改动 |
|---|---|
| `aigateway-core/.../code_rag/graph_builder.py` | 保留 `.codegraph/` 目录，参数改目录 |
| `aigateway-core/.../code_rag/graph_query.py` | **重写**：删手写 SQL，改 codegraph CLI 子进程 |
| `aigateway-core/.../code_rag/splitter.py` | 新增 `build_symbol_chunks`：codegraph 行号切源码 + 构造结构描述 |
| `aigateway-core/.../code_rag/embedding_router.py` | 不变（encode_texts 复用） |
| `aigateway-core/.../code_rag/__init__.py` | 更新导出 |
| `aigateway-api/.../code_rag_routes.py` | 嵌入结构描述 + git 源持久化 + 增量 sync 端点 + query 系列端点 + 删目录改删 |
| `aigateway-core/.../rag/rag_retriever_plugin.py` | 简化 `_expand_code_hits_with_graph`，删冗余重查 + 批量/gather scroll |
| `aigateway-cli/src/aigateway_cli/codegraph.py` | **新增** CLI 模块 |
| `aigateway-cli/src/aigateway_cli/__main__.py` | 注册 codegraph 子命令 |
| `control-panel/src/api/client.ts` | 加 codegraph 查询/sync 函数 |
| `control-panel/src/pages/KnowledgeCodeTab.tsx` | 仓库行加「同步」按钮（git/server_path）+「调用关系」查询面板（callers/callees/impact） |

## 复用的现有代码

- `embedding_router.encode_texts` / `resolve_collection_name` / `probe_embedding_dimension` — 向量编码不变
- `splitter.is_path_allowed` / `_CODE_SUFFIXES` — 路径白名单和后缀过滤复用
- `graph_builder._run_codegraph`（`graph_builder.py:29` 的 subprocess 封装）— 提取为步骤 2 的 `_run_codegraph_json` / `_run_codegraph_raw` 共用底层（`graph_query.py` 当前无 subprocess 代码）
- `code_rag_routes._write_task_state` / `_spawn_import_task` — 任务状态管理复用
- Qdrant `delete_by_filter` / `upsert_collection` — 已有，增量更新复用

## 验证方式

1. **单元测试**（开发机可跑，不依赖 codegraph CLI）：
   - `python3 -m pytest tests/test_code_rag_helpers.py -v` — 更新现有断言（schema 路径改目录、payload 新增 signature/docstring）
   - 新增 `tests/test_code_graph_cli_wrapper.py` — mock subprocess 测试 graph_query 的 codegraph 调用 + JSON 解析
   - 新增 `tests/test_code_graph_incremental.py` — 测试增量 sync 的 file_hash 对比逻辑

2. **集成测试**（需 codegraph CLI，Docker 内跑）：
   - 建测试仓库 → `aigateway codegraph status` 列出
   - `aigateway codegraph query login -p <repo> --json` 返回符号
   - `aigateway codegraph callers login -p <repo>` 返回调用者
   - git 仓：改一个文件 → 推到 origin → `aigateway codegraph sync -p <repo>` → 验证只有该文件符号刷新、`source/` 持久化在
   - server_path 仓：改一个文件 → sync → 验证只刷该文件
   - folder/zip 仓：sync 返回 400

3. **Control Panel 验证**（`npm run dev`）：
   - git/server_path 仓库行显示「同步」按钮，folder/zip 不显示（或置灰）
   - 点「同步」→ loading → 完成提示 `synced_files/refreshed_symbols`
   - 点「调用关系」→ 搜符号 → callers/callees/impact 三 tab 正确展示

4. **端到端验证**：
   - 导入本仓库 → 检索「认证逻辑」→ 应命中 auth 相关函数（结构描述 embedding 优势）
   - 对比改前改后 token 消耗：结构描述嵌入文本更短 → 上下文 token 下降是可承诺的；
     **「命中更准」仅作期望、不作验收线**——结构描述对无 docstring 函数（实测 ~88%）语义偏弱
     （见风险项），精度提升需 A/B 对比实测，本次不设硬指标。**不回归**是验收底线。

5. **Docker 重建**（后端 Python 改动 + 新 CLI）：
   ```bash
   sudo DOCKER_BUILDKIT=1 docker compose up -d --build gateway
   curl -sf localhost:8000/health
   docker compose logs --tail=50 gateway | grep -i error
   ```

## 风险与回退

- **codegraph CLI 必须在镜像里**：由 Dockerfile `npm install -g @colbymchenry/codegraph` 安装（**不在 requirements.txt**——PyPI 同名包 `codegraph` 是 xnuinside 的无关工具，已在本次清理中删除该死行）。本机开发需 `npm i -g @colbymchenry/codegraph`。
- **存量数据不兼容**：旧导入的 `{document_id}.db`（裸文件）无法用新代码查询。决策为直接重新导入，不维护兼容代码。当前 `/data/code_graphs/` 为空，无存量数据需迁移。
- **结构描述对无 docstring 函数（实测 ~88%）语义较弱**：靠符号名 + callers/callees 补偿。**低成本兜底**：`build_symbol_chunks` 构造 `embed_text` 时，无 docstring 的符号可拼上源码前 N 行（如注释行 + 函数首行签名），避免嵌入文本过短导致向量区分度低。LLM 生成符号摘要属后续增强，本次不做。
- **git 源持久化占用磁盘**：`{graph_repo_path}/source/` 保留完整 clone，删仓时随目录一并清除。大仓需关注 `/data/code_graphs` 配额。
