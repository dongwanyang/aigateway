# 安装指南

AI Gateway 的两种安装方式。**新手推荐 Docker Compose**,一条命令起全套服务。

---

## 前置要求

| 组件 | 版本 | 用途 |
|------|------|------|
| Docker | 20.10+ | 容器运行时 |
| Docker Compose | v2+ | 多服务编排 |
| Git | 任意 | 克隆仓库 |

> 本地开发(方式二)还需要 **Python 3.12**(不要用 3.13/3.14,paddlepaddle 无对应 wheel)和 **Node.js 20+**。

---

## 方式一:Docker Compose(推荐)

### 1. 克隆仓库

```bash
git clone <repo-url> gateway2
cd gateway2
```

### 2. 配置环境变量

```bash
cp .env.example .env
nano .env   # 或用你喜欢的编辑器
```

至少填入**一个** LLM 提供商的 API Key:

| 变量 | 注册地址 | 说明 |
|------|---------|------|
| `AGNES_API_KEY` | https://agnes-ai.com | 多模态理解 + 图片/视频生成 |
| `DEEPSEEK_API_KEY` | https://platform.deepseek.com | 纯文本 LLM(注册送额度) |

> 💡 **不填也能启动**:Gateway 用 `${VAR:-}` 优雅降级,空 Key 不影响启动,只是调用对应 LLM 会鉴权失败。填好后 `docker compose restart gateway` 即可。

### 3. 启动

**快速启动脚本(推荐首次使用)**:

```bash
bash scripts/quickstart.sh --build
```

脚本会自动:引导创建 `.env` → 启用 BuildKit → 构建并启动 → 健康检查 → 打印访问地址。

**或手动启动**:

```bash
docker compose up -d --build
```

首次构建约 10-15 分钟(下载 torch + Qwen3-Embedding 模型约 1.2GB)。后续改源码重建,**依赖层缓存命中,秒级完成**。

### 4. 验证

```bash
curl http://localhost:8000/health   # 应返回 {"data":{"status":"healthy",...}}
```

访问地址:

| 服务 | 地址 |
|------|------|
| API Gateway | http://localhost:8000 |
| 控制面板 | http://localhost:3000 |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3001 (admin/admin) |

---

## 方式二:本地开发

适用于二次开发场景。详见 [README.md](README.md) 的"方式二:本地开发"章节,核心步骤:

```bash
# 1. 准备 Python 3.12(用 uv 拉独立解释器,不污染系统)
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12

# 2. 创建虚拟环境
uv venv --python 3.12 --seed .venv
source .venv/bin/activate

# 3. 安装(顺序:core → api → cli)
cd aigateway-core && pip install -e . && cd ..
cd aigateway-api  && pip install -e . && cd ..
cd aigateway-cli  && pip install -e . && cd ..

# 4. 安装可选集成(按需)
pip install -e "aigateway-core[all-integrations]"   # 全部,约 5GB

# 5. 配置 .env
cp .env.example .env && nano .env

# 6. 启动 Redis + Qdrant(可用 docker compose 只起依赖)
docker compose up -d redis qdrant

# 7. 启动 API
cd aigateway-api
uvicorn src.aigateway_api.main:create_app --factory --host 0.0.0.0 --port 8000 --reload

# 8. 启动前端(另一终端)
cd control-panel
npm install && npm run dev   # http://localhost:5173
```

---

## 配置说明

### 配置优先级(高 → 低)

1. **进程环境变量** — docker-compose `environment:` 段 / shell `export`
2. **`.env` 文件** — `python-dotenv` 加载(`override=False`,不覆盖已存在的环境变量)
3. **`config.yaml` 明文值** — 主配置文件
4. **代码内默认值** — `_DEFAULT_CONFIG`

### 配置文件

| 文件 | 说明 |
|------|------|
| `config.yaml` | 主配置(server/auth/plugins/providers/embedding/observability 等) |
| `config.yaml.template` | 完整参数文档(带注释) |
| `.env` | 环境变量(含密钥,**不入库**) |
| `.env.example` | 环境变量模板(入库) |

### 常用环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `AI_GATEWAY_ENV` | development | `production` 强制关闭 debug |
| `AI_GATEWAY_REDIS_URL` | redis://localhost:6379/0 | Redis 连接 |
| `AI_GATEWAY_QDRANT_URL` | http://localhost:6333 | Qdrant 连接 |
| `AI_GATEWAY_SERVER_PORT` | 8000 | 服务端口 |
| `AI_GATEWAY_OBSERVABILITY_LOG_LEVEL` | info | 日志级别 |
| `AGNES_API_KEY` | — | Agnes provider 密钥 |
| `DEEPSEEK_API_KEY` | — | DeepSeek provider 密钥 |

> 所有 `AI_GATEWAY_*` 变量会自动覆盖 `config.yaml` 对应路径(如 `AI_GATEWAY_SERVER_PORT` → `config.server.port`)。

---

## 常见问题排查

### Gateway 启动后调 LLM 报 401/403

→ `.env` 里的 Provider Key 没填或填错。检查 `AGNES_API_KEY` / `DEEPSEEK_API_KEY`,改完 `docker compose restart gateway`。

### Gateway 一直不就绪(健康检查超时)

首次启动需加载 Qwen3-Embedding 模型(约 600MB),可能 30-60 秒。查日志:
```bash
docker compose logs -f gateway
```

### 构建很慢

确认启用了 BuildKit:
```bash
export DOCKER_BUILDKIT=1   # 或 source .env.docker
docker compose up -d --build
```
首次构建必须下载 torch + 模型,无法避免;后续重建应秒级(依赖层缓存命中)。

### Redis / Qdrant 连接失败

确认依赖服务已启动:
```bash
docker compose ps
docker compose up -d redis qdrant   # 单独重启依赖
```

### 端口被占用

修改 `docker-compose.yml` 的端口映射(左边的宿主机端口):
```yaml
ports:
  - "8001:8000"   # 宿主机 8001 → 容器 8000
```

### 想看实时日志

```bash
docker compose logs -f gateway        # 只看 gateway
docker compose logs -f                # 看全部
```

---

## 卸载

```bash
docker compose down -v   # 停止服务并删除数据卷(redis/qdrant 数据会丢失)
rm -rf gateway2          # 删除项目目录
```

---

## 参与贡献

1. Fork 仓库
2. 创建分支:`git checkout -b feat/your-feature`
3. 提交改动(遵循 [Conventional Commits](https://www.conventionalcommits.org/):`feat:` / `fix:` / `docs:` 等)
4. 推送并创建 PR

开发约定详见 [CLAUDE.md](CLAUDE.md) 的 Workflow Rules 章节。

---

## 下一步

- 阅读 [README.md](README.md) 了解架构与双管线设计
- 阅读 [docs/TECH_SPEC.md](docs/TECH_SPEC.md) 了解技术选型
- 阅读 [docs/API_CONTRACT.md](docs/API_CONTRACT.md) 了解 API 接口规范
