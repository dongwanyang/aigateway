# AI Gateway 部署

客户部署统一使用 Docker Compose。源码分发只改变镜像来源，不改变配置、
服务拓扑、入口、健康检查或数据卷。

## 套餐

| Edition | 服务 | 加速 |
|---|---|---|
| Lite | Gateway、控制台、Redis | CPU |
| Knowledge | Lite + Qdrant + RAG | NVIDIA CUDA 或 Apple MPS |
| Studio | Lite + ComfyUI | NVIDIA CUDA 或 Apple MPS |
| Full | Knowledge + Studio | NVIDIA CUDA 或 Apple MPS |

Qdrant 始终在 CPU/内存上维护向量索引；GPU 用于文档/查询 Embedding、
可选 Reranker 和媒体生成。

## 安装

```bash
# GHCR 预构建镜像（默认）
bash scripts/quickstart.sh --edition lite
bash scripts/quickstart.sh --edition knowledge --install-models
bash scripts/quickstart.sh --edition studio --install-models
bash scripts/quickstart.sh --edition full --monitoring --install-models

# 从当前 checkout 构建相同 targets
bash scripts/quickstart.sh \
  --edition full \
  --distribution source \
  --monitoring \
  --install-models \
  --build
```

首次安装时，安装器从仓库 `config.yaml` 原子生成
`.aigateway/runtime/config.yaml`，不修改仓库基础配置。首次安装完成后，运行配置
成为可变配置源：控制台保存的 provider、模型、路由和其他配置会写入该文件，
后续 quickstart 重建会以它为输入，只刷新套餐、平台、ComfyUI、Embedding 和
GPU 拓扑等部署拥有的字段。

切换套餐会复用并保留 Redis、Qdrant、模型、监控及业务数据卷。只有明确希望
丢弃控制台修改、恢复仓库默认配置时才使用：

```bash
bash scripts/quickstart.sh \
  --non-interactive \
  --reset-config \
  --no-start
```

`--reset-config` 是破坏性配置重置，不应加入日常重建命令。它不会删除数据库、
模型或 Docker 数据卷，但会用仓库 `config.yaml` 替换当前运行配置中的可变设置。

公开参数：

```text
--edition lite|knowledge|studio|full
--distribution image|source
--comfyui container|native|remote
--embedding container|native|remote
--comfyui-url URL
--embedding-url URL
--monitoring
--production
--install-models
--build
--reset-config
--no-start
--show-plan
--down
```

旧 `runtime/rag/vision/full profile`、`--accelerator` 和 `--add/--remove`
接口不再接受；检测到旧状态时安装器停止并显示迁移命令。

## 重建容器与 BuildKit 缓存

已有安装需要从当前源码重建时，继续使用安装器，不要重新拼装 Compose 命令：

```bash
# 复用 .aigateway-install.env 中原有 Edition、GPU 拓扑和服务模式
# 同时保留 .aigateway/runtime/config.yaml 中的控制台配置修改
BUILDKIT_PROGRESS=plain \
  bash scripts/quickstart.sh \
    --non-interactive \
    --distribution source \
    --build
```

首次安装或需要显式切换套餐时再指定 Edition：

```bash
BUILDKIT_PROGRESS=plain \
  bash scripts/quickstart.sh \
    --non-interactive \
    --edition full \
    --distribution source \
    --build
```

`--build` 表示执行源码镜像构建，仍然允许正常使用缓存；它不等同于
`docker compose build --no-cache`。安装器会同时加载 `.env` 和
`.aigateway-install.env`，选择正确的 Docker target、GHCR cache 引用、CUDA
覆盖文件及动态生成的 GPU 拓扑文件。

CUDA 本地容器模式下，`scripts/render-gpu-topology.py` 每次根据当前宿主机
`nvidia-smi` 结果刷新 `.aigateway/runtime/config.yaml` 中的
`gpu_scheduler.devices`、`gpu_scheduler.workers` 和 `inventory_source`。仓库
`config.yaml` 不保存任何机器专属 GPU UUID。切换到 CPU、MPS、native 或 remote
ComfyUI 时，安装器会清除旧的本地 GPU inventory，避免过期 UUID 被误报为可用。

不要让自动化工具或编码代理默认执行以下命令：

```bash
# 缺少安装状态，可能回退到 Lite target 和错误的 cache 引用
docker compose build
docker compose up --build

# 明确禁用或删除缓存，只用于有证据的缓存损坏排查
docker compose build --no-cache
docker builder prune
docker system prune -a
```

缓存分为两层：

- 当前 Docker builder 的本地 BuildKit layer/cache mount；
- GHCR 已发布镜像携带的 inline cache，供新机器或空 builder 通过
  Compose `cache_from=type=registry` 导入。

镜像发布流水线同时导出 GitHub Actions cache 和 inline cache。若使用的是该
修复之前发布的旧镜像，它不包含 inline cache，首次源码构建仍可能较慢；发布
一次新镜像后，新的本地环境才能从 GHCR 导入缓存。

以下变化会产生合理的 cache miss：

- 修改 `aigateway-core/pyproject.toml` 或 `aigateway-api/pyproject.toml`；
- 在 Lite、Knowledge、Studio、Full 或 CPU/CUDA target 之间切换；
- 修改 PyTorch、ComfyUI 或 ComfyUI Manager 版本参数；
- 基础镜像 digest 变化，或主动要求重新拉取基础镜像；
- 清理/切换 Docker builder，且对应 GHCR 镜像尚无 inline cache。

普通 Python 源码变化只应使末端本地包安装层失效，不应重新安装 PyTorch、
CUDA 和全部系统依赖。构建日志中应看到 `importing cache manifest from`
以及大量 `CACHED`。可用以下命令检查当前 builder 的缓存占用：

```bash
docker buildx du
```

## 平台行为

### Linux / Windows NVIDIA

Windows 只支持 Docker Desktop WSL2 NVIDIA GPU 模式。安装器必须同时通过：

```bash
nvidia-smi
docker run --rm --gpus all \
  nvidia/cuda:13.0.1-base-ubuntu24.04 nvidia-smi
```

Knowledge/Full 的本地 Embedding 明确使用 CUDA。Studio/Full 运行独立
ComfyUI 容器。多 GPU 时 Gateway 使用 GPU 0、ComfyUI 使用 GPU 1；单 GPU
时二者可直接并发，Gateway 进程显存比例受限。安装器不设置最低显存，只在
低显存时启用 ComfyUI `--lowvram` 并提示 OOM 风险。

### Apple Silicon

Gateway、控制台、Redis、Qdrant 和监控仍由 Docker 运行。安装器在
`~/.aigateway/` 创建固定版本的共享 Python 环境、模型、日志与 LaunchAgent：

- ComfyUI：`127.0.0.1:8188`，MPS
- OpenAI 兼容 Embedding：`127.0.0.1:8189`，MPS

容器通过 `host.docker.internal` 连接。服务不需要 `sudo`，也不监听局域网。

```bash
scripts/native-macos-services.sh status
scripts/native-macos-services.sh stop
```

## 模型和空间

模型不进入镜像。`--install-models` 会逐一展示许可证，只有确认后才下载；
下载前要求至少 80GB 可用空间，下载到临时路径，校验固定版本及 SHA256 后
原子移动：

- Qwen3-Embedding-0.6B（Apache-2.0）
- SDXL Base 1.0（CreativeML Open RAIL++-M）
- Wan2.2 TI2V 5B（Apache-2.0，ComfyUI 原生图片条件视频）

运行时 ComfyUI 在可用空间低于 30GB 时拒绝新任务。GPU OOM 会终止当前
任务、清理设备缓存并返回可重试错误，不会切到 CPU 或外部媒体 API。

## Compose 文件

- `docker-compose.yml`：核心服务及 `knowledge`、`comfy-container`、
  `monitoring` profiles
- `docker-compose.cuda.yml`：NVIDIA 设备分配
- `docker-compose.prod.yml`：TLS 与单一公网入口

生产模式只公开控制台反向代理的 80/443：

```bash
TLS_CERT_PATH=/path/fullchain.pem \
TLS_KEY_PATH=/path/privkey.pem \
bash scripts/quickstart.sh --edition full --production
```

Gateway、ComfyUI、Embedding、Redis、Qdrant 与 Prometheus 保持内部或仅
本机可访问。

## 故障排查

```bash
docker compose --env-file .env --env-file .aigateway-install.env config
docker compose --env-file .env --env-file .aigateway-install.env ps
docker compose logs --tail=200 gateway
docker system df -v
docker buildx du
```

若宿主机 `nvidia-smi` 报内核模块与用户态库版本不一致，先重启以加载已安装
驱动；仍不一致时再重装匹配驱动。GPU smoke test 通过前不要下载生成模型。
