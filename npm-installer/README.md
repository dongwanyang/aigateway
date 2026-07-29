# aigateway-installer

AI Gateway 的 Docker Compose 安装器。Linux、Windows WSL2 与 Apple
Silicon 使用相同的四档套餐和服务拓扑；`image` 拉取 GHCR 镜像，`source`
从当前 checkout 构建同名 targets。

```bash
npm install -g aigateway-installer
aigateway-install --edition lite
aigateway-install --edition full --distribution source --build
```

主要参数：

- `--edition lite|knowledge|studio|full`
- `--distribution image|source`
- `--comfyui container|native|remote`
- `--embedding container|native|remote`
- `--monitoring`、`--production`、`--install-models`

Windows 必须从启用 NVIDIA GPU 的 Docker Desktop WSL2 环境运行。Apple
Silicon 的 Gateway/Redis/Qdrant/控制台保持容器化，ComfyUI 与 Embedding
作为用户级 MPS 服务运行。

旧 `--source`、`--docker`、`--profile`、`--accelerator` 和增量
`--add/--remove` 接口已移除；安装器会给出迁移提示，不会删除现有数据卷。
