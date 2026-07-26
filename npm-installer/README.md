# aigateway-installer

AI Gateway 的交互式 Docker 安装器。支持 macOS、Linux 和 Windows WSL2，
需要 Node.js 18+、Git、Bash、Docker Engine/Desktop 与 Docker Compose v2。

## 安装

```bash
npm install -g aigateway-installer
aigateway-install
```

也可以不做全局安装：

```bash
npx aigateway-installer
```

安装器会在当前 AI Gateway 仓库中运行；如果当前目录不是仓库，则下载到
`~/.aigateway/runtime`，然后展示 Runtime、RAG、Vision、Full、CPU/CUDA 和监控选项。

## 自动化

```bash
aigateway-install \
  --non-interactive \
  --profile full \
  --accelerator cuda \
  --monitoring \
  --build
```

升级现有安装的能力：

```bash
aigateway-install --add rag --build
aigateway-install --add vision --build
aigateway-install --show-plan
```

指定源码目录、Git 仓库或版本：

```bash
aigateway-install --dir /opt/aigateway
aigateway-install --repo https://github.com/example/aigateway.git --ref v0.2.0
```

安装器不在 npm `postinstall` 阶段运行命令，不会自动删除 Docker 数据卷。
