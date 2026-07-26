# aigateway-installer

AI Gateway 安装器。默认从源码安装，也可通过 `--docker` 使用 Docker Compose
部署。支持 macOS、Linux 和 Windows WSL2，需要 Node.js 18+、Git 与 Bash。
源码安装需要 Python 3.12；Docker 部署需要 Docker Engine/Desktop 与
Docker Compose v2。

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
`~/.aigateway/runtime`。默认创建 `.venv`，以 editable 模式安装 Python
源码，并安装控制台依赖：

```bash
aigateway-install                 # 等同于 --source
aigateway-install --source --profile full
aigateway-install --source --no-frontend
```

## Docker 部署

```bash
aigateway-install --docker \
  --non-interactive \
  --profile full \
  --accelerator cuda \
  --monitoring \
  --build
```

升级现有安装的能力：

```bash
aigateway-install --docker --add rag --build
aigateway-install --docker --add vision --build
aigateway-install --docker --show-plan
```

指定源码目录、Git 仓库或版本：

```bash
aigateway-install --dir /opt/aigateway
aigateway-install --repo https://github.com/example/aigateway.git --ref v0.2.0
```

安装器不在 npm `postinstall` 阶段运行命令。Docker 模式不会自动删除数据卷。
