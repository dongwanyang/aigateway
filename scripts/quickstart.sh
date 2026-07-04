#!/usr/bin/env bash
# ============================================================
# AI Gateway — 一键快速启动脚本
# ============================================================
# 用法:
#   bash scripts/quickstart.sh          # 启动(首次会引导创建 .env)
#   bash scripts/quickstart.sh --build  # 重新构建镜像后启动
#   bash scripts/quickstart.sh --down   # 停止所有服务
#
# 行为:
#   1. 若 .env 不存在,自动从 .env.example 复制并提示用户填 Key
#   2. 启用 BuildKit 加速构建(source .env.docker)
#   3. docker compose up -d 启动 6 个服务
#   4. 健康检查 + 打印访问地址
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT_DIR="$(pwd)"

# 颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[✓]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
err()   { echo -e "${RED}[✗]${NC} $*"; }

# ---- 处理子命令 ----
case "${1:-up}" in
  --down|down)
    docker compose down
    info "已停止所有服务"
    exit 0
    ;;
  --build|build)
    BUILD_FLAG="--build"
    ;;
  up|"")
    BUILD_FLAG=""
    ;;
  *)
    err "未知参数: $1"
    echo "用法: bash scripts/quickstart.sh [--build|--down]"
    exit 1
    ;;
esac

# ---- 1. 引导创建 .env ----
if [[ ! -f "$ROOT_DIR/.env" ]]; then
  warn "未检测到 .env 文件,正在从 .env.example 创建..."
  if [[ -f "$ROOT_DIR/.env.example" ]]; then
    cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
    info "已创建 .env"
    echo ""
    warn "请编辑 .env 填入你的 API Key(至少填一个 Provider):"
    echo "    nano .env"
    echo ""
    warn "必填项(按需):"
    echo "    AGNES_API_KEY     — https://agnes-ai.com"
    echo "    DEEPSEEK_API_KEY — https://platform.deepseek.com"
    echo ""
    read -r -p "$(echo -e ${YELLOW}[?]${NC} 已填好 Key 了吗?回车继续启动,或 Ctrl+C 退出:)" _
  else
    err ".env.example 不存在,无法创建 .env"
    exit 1
  fi
fi

# ---- 2. 启用 BuildKit ----
if [[ -f "$ROOT_DIR/.env.docker" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env.docker"
  set +a
  info "已启用 BuildKit(DOCKER_BUILDKIT=$DOCKER_BUILDKIT)"
fi

# ---- 3. 检查 Docker ----
if ! docker info >/dev/null 2>&1; then
  err "Docker 未运行或当前用户无 docker 权限"
  echo "    尝试:sudo $0 $*"
  exit 1
fi

# ---- 4. 启动 ----
info "启动服务..."
docker compose up -d $BUILD_FLAG

# ---- 5. 健康检查 ----
info "等待 gateway 就绪(最多 60 秒)..."
for i in $(seq 1 30); do
  if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
    info "gateway 已就绪"
    break
  fi
  sleep 2
  [[ $i -eq 30 ]] && warn "gateway 60 秒内未就绪,请查日志:docker compose logs gateway"
done

# ---- 6. 打印访问地址 ----
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  AI Gateway 已启动${NC}"
echo -e "${GREEN}========================================${NC}"
echo "  API Gateway :  http://localhost:8000"
echo "  控制面板    :  http://localhost:3000"
echo "  Prometheus  :  http://localhost:9090"
echo "  Grafana     :  http://localhost:3001  (admin/admin)"
echo ""
echo "  查看日志    :  docker compose logs -f gateway"
echo "  停止服务    :  bash scripts/quickstart.sh --down"
echo -e "${GREEN}========================================${NC}"
