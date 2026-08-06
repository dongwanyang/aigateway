# AI Gateway 部署

客户部署统一由 `scripts/quickstart.sh` 编排 Docker Compose。源码分发只改变镜像
来源，不改变配置、服务拓扑、入口、健康检查或数据卷。

> **不要直接使用**
> `docker compose -f docker-compose.yml -f docker-compose.cuda.yml up -d`
> 启动 Studio/Full。该命令只给 Gateway 分配 GPU，不会生成运行配置、启用
> `comfy-container` profile，也不会创建本机 ComfyUI worker 拓扑，Gateway 会因
> `gpu_scheduler.workers` 为空而报：
> `RuntimeError: GPU scheduler topology incomplete; local ComfyUI pool has no workers`。

## 套餐

| Edition | 服务 | 加速 |
|---|---|---|
| Lite | Gateway、控制台、Redis | CPU |
| Knowledge | Lite + Qdrant + RAG | NVIDIA CUDA 或 Apple MPS |
| Studio | Lite + ComfyUI | NVIDIA CUDA 或 Apple MPS |
| Full | Knowledge + Studio | NVIDIA CUDA 或 Apple MPS |

Qdrant 始终在 CPU/内存上维护向量索引；GPU 用于文档/查询 Embedding、可选
Reranker 和媒体生成。

## 前置检查

Linux 和 Windows WSL2 的 NVIDIA 部署必须同时通过宿主机和容器 GPU 检查：

```bash
nvidia-smi

docker run --rm --gpus all \
  nvidia/cuda:13.0.1-base-ubuntu24.04 \
  nvidia-smi
```

安装器还需要 Python 3 和 PyYAML：

```bash
python3 -c 'import yaml; print(yaml.__version__)'
```

缺少 PyYAML 时安装：

```bash
python3 -m pip install pyyaml
```

## 推荐启动方式

### 首次安装：GHCR 预构建镜像

```bash
bash scripts/quickstart.sh --edition lite
bash scripts/quickstart.sh --edition knowledge --install-models
bash scripts/quickstart.sh --edition studio --install-models
bash scripts/quickstart.sh --edition full --monitoring --install-models
```

Studio/Full 在 Linux 或 Windows WSL2 上默认使用本地 NVIDIA ComfyUI 容器。
需要明确指定时可执行：

```bash
bash scripts/quickstart.sh \
  --non-interactive \
  --edition studio \
  --comfyui container
```

### 首次安装：从当前源码构建

只需要本地生成能力时使用 Studio：

```bash
BUILDKIT_PROGRESS=plain \
  bash scripts/quickstart.sh \
    --non-interactive \
    --edition studio \
    --distribution source \
    --comfyui container \
    --build
```

同时需要 RAG、Qdrant、Embedding 和 ComfyUI 时使用 Full：

```bash
BUILDKIT_PROGRESS=plain \
  bash scripts/quickstart.sh \
    --non-interactive \
    --edition full \
    --distribution source \
    --comfyui container \
    --embedding container \
    --build
```

### 已有安装：重建或更新

已有 `.aigateway-install.env` 时，不要重新拼装 Compose 命令。安装器会复用原有
Edition、GPU 拓扑、ComfyUI/Embedding 模式及控制台保存的运行配置：

```bash
BUILDKIT_PROGRESS=plain \
  bash scripts/quickstart.sh \
    --non-interactive \
    --distribution source \
    --build
```

使用 GHCR 镜像更新现有安装：

```bash
bash scripts/quickstart.sh --non-interactive
```

修改 `.env` 后也应重新运行上述安装器命令，使容器按新环境变量重新创建；单纯执行
`docker compose restart gateway` 不会重新读取容器环境变量。

## 正确关闭方式

### 推荐：由安装器关闭完整服务栈

在仓库根目录执行：

```bash
bash scripts/quickstart.sh --down
```

安装器会读取现有 `.aigateway-install.env`，使用与启动时相同的 Edition、profiles、
CUDA overlay 和生产覆盖文件执行 `docker compose down`。该命令会删除当前项目的
容器和网络，但会保留：

- Redis、Qdrant、Prometheus、Grafana 等 Docker named volumes；
- `.aigateway/runtime/config.yaml` 和 `.aigateway-install.env`；
- `data/`、`models/`、`comfyui/` 等宿主机目录；
- 已构建或已拉取的 Docker 镜像与 BuildKit 缓存。

再次启动时执行：

```bash
bash scripts/quickstart.sh --non-interactive
```

源码分发需要重新构建当前 checkout 时执行：

```bash
BUILDKIT_PROGRESS=plain \
  bash scripts/quickstart.sh \
    --non-interactive \
    --distribution source \
    --build
```

> 不要把 `docker compose down -v` 用作日常关闭命令。`-v` 会删除项目的 named
> volumes，可能丢失 Redis、Qdrant、监控和其他持久化数据。除非明确执行数据重置，
> 也不要删除 `.aigateway/runtime`、`data`、`models` 或 `comfyui`。

### 高级：手动 Compose 关闭

手动关闭必须加载与启动时完全相同的 env 文件和 Compose 文件。CUDA
Studio/Full 示例：

```bash
docker compose \
  --env-file .env \
  --env-file .aigateway-install.env \
  -f docker-compose.yml \
  -f docker-compose.cuda.yml \
  -f .aigateway/runtime/docker-compose.gpu.generated.yml \
  down --remove-orphans
```

生产模式还必须追加：

```text
-f docker-compose.prod.yml
```

如果只需要临时停止进程并保留容器，可把 `down --remove-orphans` 改为 `stop`；
恢复时使用相同文件集合执行 `start`。日常运维仍优先使用
`bash scripts/quickstart.sh --down`，避免遗漏 profile、GPU worker 或生产覆盖文件。

## 安装器生成的文件

首次安装时，安装器从仓库 `config.yaml` 原子生成运行配置，不修改仓库基础配置：

```text
.env
.aigateway-install.env
.aigateway/runtime/config.yaml
.aigateway/runtime/docker-compose.gpu.generated.yml  # 仅本地 CUDA ComfyUI 池
```

这些文件分别承担以下职责：

- `.env`：密钥和用户环境变量；
- `.aigateway-install.env`：Edition、镜像 target、profiles、运行路径和 GPU 选择；
- `.aigateway/runtime/config.yaml`：控制台可变配置和部署拥有的运行配置；
- `docker-compose.gpu.generated.yml`：Gateway 可见 GPU、每张卡对应的 ComfyUI
  worker 服务和端口覆盖。

首次安装完成后，`.aigateway/runtime/config.yaml` 成为可变配置源。控制台保存的
provider、模型、路由和其他配置会写入该文件；后续 quickstart 重建以它为输入，
只刷新套餐、平台、ComfyUI、Embedding 和 GPU 拓扑等部署拥有的字段。

CUDA 本地容器模式下，`scripts/render-gpu-topology.py` 每次根据当前宿主机
`nvidia-smi` 刷新：

```text
gpu_scheduler.devices
gpu_scheduler.workers
gpu_scheduler.inventory_source
```

仓库 `config.yaml` 不保存机器专属 GPU UUID。更换 GPU、迁移实例或调整设备池后，
重新运行 quickstart 即可按当前逻辑索引和 UUID 重建拓扑。切换到 CPU、MPS、
native 或 remote ComfyUI 时，安装器会清除旧的本地 GPU inventory。

## 为什么不能只加载两个 Compose 文件

以下命令是不完整的：

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.cuda.yml \
  up -d
```

它缺少：

1. `.env` 和 `.aigateway-install.env` 中的镜像 target、profiles 和运行路径；
2. `.aigateway/runtime/config.yaml` 中生成的 `gpu_scheduler.workers`；
3. `.aigateway/runtime/docker-compose.gpu.generated.yml` 中每张 GPU 的 ComfyUI
   worker 服务定义；
4. Studio/Full 所需的 `comfy-container` profile。

`docker-compose.cuda.yml` 只负责向 Gateway 申请 NVIDIA 设备，不负责创建
ComfyUI worker。

## 高级用法：手动执行 Compose

通常不需要手动运行 Compose。确有自动化或运维集成需求时，必须先让安装器生成
状态和 GPU 拓扑，再加载完整文件集合。

### 1. 只生成配置，不启动

Studio：

```bash
bash scripts/quickstart.sh \
  --non-interactive \
  --edition studio \
  --distribution source \
  --comfyui container \
  --no-start
```

Full：

```bash
bash scripts/quickstart.sh \
  --non-interactive \
  --edition full \
  --distribution source \
  --comfyui container \
  --embedding container \
  --no-start
```

### 2. 检查生成结果

```bash
ls -l \
  .env \
  .aigateway-install.env \
  .aigateway/runtime/config.yaml \
  .aigateway/runtime/docker-compose.gpu.generated.yml

grep -E \
  'AIGATEWAY_(EDITION|ACCELERATOR|COMFYUI_MODE|SHARED_GPU)|COMPOSE_PROFILES|GATEWAY_IMAGE_TARGET' \
  .aigateway-install.env
```

Studio CUDA 环境应至少包含：

```text
AIGATEWAY_EDITION=studio
AIGATEWAY_ACCELERATOR=cuda
AIGATEWAY_COMFYUI_MODE=container
COMPOSE_PROFILES=comfy-container
```

检查 worker：

```bash
python3 - <<'PY'
import yaml
from pprint import pprint

with open('.aigateway/runtime/config.yaml', encoding='utf-8') as handle:
    config = yaml.safe_load(handle) or {}

scheduler = config.get('gpu_scheduler', {})
pprint({
    'enabled': scheduler.get('enabled'),
    'inventory_source': scheduler.get('inventory_source'),
    'devices': scheduler.get('devices'),
    'workers': scheduler.get('workers'),
})
PY
```

`workers` 至少应有一项，例如：

```yaml
workers:
  - worker_id: comfyui-gpu-0
    logical_index: 0
    device_uuid: GPU-...
    server_url: http://comfyui:8188
```

### 3. 构建与启动

源码分发：

```bash
docker compose \
  --env-file .env \
  --env-file .aigateway-install.env \
  -f docker-compose.yml \
  -f docker-compose.cuda.yml \
  -f .aigateway/runtime/docker-compose.gpu.generated.yml \
  build

docker compose \
  --env-file .env \
  --env-file .aigateway-install.env \
  -f docker-compose.yml \
  -f docker-compose.cuda.yml \
  -f .aigateway/runtime/docker-compose.gpu.generated.yml \
  up -d --remove-orphans
```

GHCR 镜像分发时将 `build` 替换为 `pull`。

生产模式还必须在最后追加：

```text
-f docker-compose.prod.yml
```

生产模式建议直接使用安装器：

```bash
TLS_CERT_PATH=/path/fullchain.pem \
TLS_KEY_PATH=/path/privkey.pem \
bash scripts/quickstart.sh --edition full --production
```

## 切换套餐与重置配置

切换套餐会复用并保留 Redis、Qdrant、模型、监控及业务数据卷。只有明确希望
丢弃控制台修改并恢复仓库默认配置时才使用：

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

旧 `runtime/rag/vision/full profile`、`--accelerator` 和 `--add/--remove` 接口不再
接受；检测到旧状态时安装器会停止并显示迁移命令。

## BuildKit 缓存

`--build` 表示执行源码镜像构建，仍然允许正常使用缓存；它不等同于
`docker compose build --no-cache`。安装器会同时加载 `.env` 和
`.aigateway-install.env`，选择正确的 Docker target、GHCR cache 引用、CUDA
覆盖文件及动态生成的 GPU 拓扑文件。

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
- GHCR 已发布镜像携带的 inline cache，供新机器或空 builder 通过 Compose
  `cache_from=type=registry` 导入。

镜像发布流水线同时导出 GitHub Actions cache 和 inline cache。若使用的是修复前
发布的旧镜像，它不包含 inline cache，首次源码构建仍可能较慢；发布新镜像后，
新的本地环境才能从 GHCR 导入缓存。

以下变化会产生合理的 cache miss：

- 修改 `aigateway-core/pyproject.toml` 或 `aigateway-api/pyproject.toml`；
- 在 Lite、Knowledge、Studio、Full 或 CPU/CUDA target 之间切换；
- 修改 PyTorch、ComfyUI 或 ComfyUI Manager 版本参数；
- 基础镜像 digest 变化，或主动要求重新拉取基础镜像；
- 清理或切换 Docker builder，且对应 GHCR 镜像尚无 inline cache。

普通 Python 源码变化只应使末端本地包安装层失效，不应重新安装 PyTorch、CUDA
和全部系统依赖。构建日志中应看到 `importing cache manifest from` 以及大量
`CACHED`。检查当前 builder 缓存占用：

```bash
docker buildx du
```

## 平台行为

### Linux / Windows NVIDIA

Windows 只支持 Docker Desktop WSL2 NVIDIA GPU 模式。

Knowledge/Full 的本地 Embedding 使用 CUDA。Studio/Full 运行独立 ComfyUI
容器。安装器按当前 GPU inventory 生成动态池：

- Gateway 可见被允许的本机 GPU；
- 每张被选中的 ComfyUI GPU 运行一个 worker；
- 单 GPU 环境由 Gateway 和 ComfyUI 共享同一设备；
- 多 GPU 环境不再固定切成“Gateway GPU 0、ComfyUI GPU 1”，而由调度器根据
  租约、任务优先级、显存和 worker 状态协调使用；
- 低显存设备会启用 ComfyUI `--lowvram` 并提示 OOM 风险，但安装器不设置硬性
  最低显存门槛。

### Apple Silicon

Gateway、控制台、Redis、Qdrant 和监控仍由 Docker 运行。安装器在
`~/.aigateway/` 创建固定版本的共享 Python 环境、模型、日志与 LaunchAgent：

- ComfyUI：`127.0.0.1:8188`，MPS；
- OpenAI 兼容 Embedding：`127.0.0.1:8189`，MPS。

容器通过 `host.docker.internal` 连接。服务不需要 `sudo`，也不监听局域网。

```bash
scripts/native-macos-services.sh status
scripts/native-macos-services.sh stop
```

## 模型和空间

模型不进入镜像。`--install-models` 会逐一展示许可证，只有确认后才下载；下载前
要求至少 80GB 可用空间，下载到临时路径，校验固定版本及 SHA256 后原子移动：

- Qwen3-Embedding-0.6B（Apache-2.0）；
- SDXL Base 1.0（CreativeML Open RAIL++-M）；
- Wan2.2 TI2V 5B（Apache-2.0，ComfyUI 原生图片条件视频）。

运行时 ComfyUI 在可用空间低于 30GB 时拒绝新任务。GPU OOM 会终止当前任务、
清理设备缓存并返回可重试错误，不会切到 CPU 或外部媒体 API。

## Compose 文件

- `docker-compose.yml`：核心服务及 `knowledge`、`comfy-container`、
  `monitoring` profiles；
- `docker-compose.cuda.yml`：Gateway NVIDIA 设备分配；
- `.aigateway/runtime/docker-compose.gpu.generated.yml`：当前主机 GPU 可见性、
  ComfyUI worker 服务和端口；
- `docker-compose.prod.yml`：TLS 与单一公网入口。

Gateway、ComfyUI、Embedding、Redis、Qdrant 与 Prometheus 保持内部或仅本机可
访问。生产模式只公开控制台反向代理的 80/443。

## 故障排查

### 拓扑不完整或没有 workers

遇到以下错误：

```text
RuntimeError: GPU scheduler topology incomplete; local ComfyUI pool has no workers
```

按顺序执行：

```bash
# 1. 验证宿主机 GPU
nvidia-smi

# 2. 验证 Docker GPU runtime
docker run --rm --gpus all \
  nvidia/cuda:13.0.1-base-ubuntu24.04 \
  nvidia-smi

# 3. 重新生成运行配置、worker 和 Compose overlay
BUILDKIT_PROGRESS=plain \
  bash scripts/quickstart.sh \
    --non-interactive \
    --distribution source \
    --build
```

如果之前从未运行过安装器，显式指定 Studio 或 Full：

```bash
BUILDKIT_PROGRESS=plain \
  bash scripts/quickstart.sh \
    --non-interactive \
    --edition studio \
    --distribution source \
    --comfyui container \
    --build
```

### 查看最终服务集合

```bash
docker compose \
  --env-file .env \
  --env-file .aigateway-install.env \
  -f docker-compose.yml \
  -f docker-compose.cuda.yml \
  -f .aigateway/runtime/docker-compose.gpu.generated.yml \
  config --services
```

Studio 应至少显示：

```text
gateway
control-panel
redis
comfyui
```

Full 还应显示 `qdrant`。

### 查看状态和日志

```bash
docker compose \
  --env-file .env \
  --env-file .aigateway-install.env \
  -f docker-compose.yml \
  -f docker-compose.cuda.yml \
  -f .aigateway/runtime/docker-compose.gpu.generated.yml \
  ps

docker compose \
  --env-file .env \
  --env-file .aigateway-install.env \
  -f docker-compose.yml \
  -f docker-compose.cuda.yml \
  -f .aigateway/runtime/docker-compose.gpu.generated.yml \
  logs --tail=200 gateway comfyui
```

通用磁盘和构建缓存检查：

```bash
docker system df -v
docker buildx du
```

若宿主机 `nvidia-smi` 报内核模块与用户态库版本不一致，先重启以加载已安装
驱动；仍不一致时再重装匹配驱动。GPU smoke test 通过前不要下载生成模型。
