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

安装器原子写入 `.aigateway/runtime/config.yaml`，不修改仓库基础
`config.yaml`。切换套餐会复用并保留 Redis、Qdrant、模型、监控及业务数据卷。

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
--no-start
--show-plan
--down
```

旧 `runtime/rag/vision/full profile`、`--accelerator` 和 `--add/--remove`
接口不再接受；检测到旧状态时安装器停止并显示迁移命令。

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
docker compose --env-file .aigateway-install.env config
docker compose --env-file .aigateway-install.env ps
docker compose logs --tail=200 gateway
docker system df -v
```

若宿主机 `nvidia-smi` 报内核模块与用户态库版本不一致，先重启以加载已安装
驱动；仍不一致时再重装匹配驱动。GPU smoke test 通过前不要下载生成模型。
