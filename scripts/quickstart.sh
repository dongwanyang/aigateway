#!/usr/bin/env bash
# AI Gateway guided installer. Safe to re-run: it only rewrites the generated
# install-state file and never removes Docker volumes.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT_DIR="$(pwd)"
INSTALL_STATE="$ROOT_DIR/.aigateway-install.env"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info() { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
fail() { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
AI Gateway 安装向导

用法:
  bash scripts/quickstart.sh
  bash scripts/quickstart.sh --profile full --accelerator cuda --build
  bash scripts/quickstart.sh --add rag
  bash scripts/quickstart.sh --remove vision
  bash scripts/quickstart.sh --non-interactive --profile runtime

选项:
  --profile runtime|rag|vision|full  选择经过测试的能力组合
  --add rag|vision|gpu               在当前安装上增加能力
  --remove rag|vision|gpu            移除能力（不会删除数据卷）
  --accelerator cpu|cuda             选择运行硬件
  --monitoring / --no-monitoring     启用或关闭 Prometheus + Grafana
  --build                            构建镜像后启动
  --no-start                         只保存安装方案
  --show-plan                        显示当前安装方案后退出
  --non-interactive                  不显示问题，未指定项沿用当前值或默认值
  --down                             停止当前方案中的服务（保留数据卷）
  -h, --help                         显示帮助
EOF
}

profile="runtime"
accelerator="cpu"
monitoring="false"
profile_explicit="false"
accelerator_explicit="false"
monitoring_explicit="false"
interactive="true"
build="false"
start="true"
show_plan="false"
action="up"
add_features=""
remove_features=""

if [[ -f "$INSTALL_STATE" ]]; then
  # Parse known keys instead of sourcing the file as shell code.
  while IFS='=' read -r key value; do
    case "$key" in
      GATEWAY_INSTALL_PROFILE) profile="$value" ;;
      GATEWAY_ACCELERATOR) accelerator="$value" ;;
      GATEWAY_MONITORING) monitoring="$value" ;;
    esac
  done < "$INSTALL_STATE"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      [[ $# -ge 2 ]] || fail "--profile 缺少值"
      profile="$2"; profile_explicit="true"; shift 2
      ;;
    --add)
      [[ $# -ge 2 ]] || fail "--add 缺少值"
      add_features="$add_features $2"; shift 2
      ;;
    --remove)
      [[ $# -ge 2 ]] || fail "--remove 缺少值"
      remove_features="$remove_features $2"; shift 2
      ;;
    --accelerator)
      [[ $# -ge 2 ]] || fail "--accelerator 缺少值"
      accelerator="$2"; accelerator_explicit="true"; shift 2
      ;;
    --monitoring)
      monitoring="true"; monitoring_explicit="true"; shift
      ;;
    --no-monitoring)
      monitoring="false"; monitoring_explicit="true"; shift
      ;;
    --build|build)
      build="true"; shift
      ;;
    --no-start)
      start="false"; shift
      ;;
    --show-plan)
      show_plan="true"; start="false"; interactive="false"; shift
      ;;
    --non-interactive)
      interactive="false"; shift
      ;;
    --down|down)
      action="down"; interactive="false"; shift
      ;;
    up)
      shift
      ;;
    -h|--help)
      usage; exit 0
      ;;
    *)
      fail "未知参数: $1"
      ;;
  esac
done

validate_profile() {
  case "$1" in runtime|rag|vision|full) ;; *) fail "不支持的 profile: $1" ;; esac
}

set_feature() {
  local feature="$1"
  local enabled="$2"
  case "$feature" in
    rag)
      if [[ "$enabled" == "true" ]]; then
        case "$profile" in runtime) profile="rag" ;; vision) profile="full" ;; esac
      else
        case "$profile" in rag) profile="runtime" ;; full) profile="vision" ;; esac
      fi
      ;;
    vision)
      if [[ "$enabled" == "true" ]]; then
        case "$profile" in runtime) profile="vision" ;; rag) profile="full" ;; esac
      else
        case "$profile" in vision) profile="runtime" ;; full) profile="rag" ;; esac
      fi
      ;;
    gpu)
      accelerator=$([[ "$enabled" == "true" ]] && echo "cuda" || echo "cpu")
      accelerator_explicit="true"
      ;;
    *) fail "不支持的能力: $feature" ;;
  esac
}

validate_profile "$profile"
for feature in $add_features; do set_feature "$feature" "true"; done
for feature in $remove_features; do set_feature "$feature" "false"; done

if [[ "$interactive" == "true" && "$profile_explicit" == "false" && -z "$add_features" && -z "$remove_features" ]]; then
  echo
  echo "请选择安装版本:"
  echo "  1) Runtime  基础网关、控制台、Redis（最小镜像）"
  echo "  2) RAG      Runtime + 知识库、Code RAG、本地 Embedding"
  echo "  3) Vision   Runtime + OCR、音视频处理、RealESRGAN"
  echo "  4) Full     RAG + Vision（完整单机体验）"
  read -r -p "选择 [1-4，当前 ${profile}]: " choice
  case "${choice:-}" in
    1) profile="runtime" ;;
    2) profile="rag" ;;
    3) profile="vision" ;;
    4) profile="full" ;;
    "") ;;
    *) fail "无效选择: $choice" ;;
  esac
fi

if [[ "$interactive" == "true" && "$accelerator_explicit" == "false" ]]; then
  read -r -p "是否允许容器使用 NVIDIA GPU？[y/N，当前 ${accelerator}]: " choice
  case "${choice:-}" in
    y|Y|yes|YES) accelerator="cuda" ;;
    n|N|no|NO|"") ;;
    *) fail "请输入 y 或 n" ;;
  esac
fi

if [[ "$interactive" == "true" && "$monitoring_explicit" == "false" ]]; then
  read -r -p "是否安装 Prometheus + Grafana 监控？[y/N，当前 ${monitoring}]: " choice
  case "${choice:-}" in
    y|Y|yes|YES) monitoring="true" ;;
    n|N|no|NO|"") monitoring="false" ;;
    *) fail "请输入 y 或 n" ;;
  esac
fi

case "$accelerator" in cpu|cuda) ;; *) fail "不支持的 accelerator: $accelerator" ;; esac

case "$profile" in
  runtime)
    target=$([[ "$accelerator" == "cuda" ]] && echo "gateway-gpu" || echo "gateway-runtime")
    estimate="轻量；不包含本地模型与视觉工具链"
    ;;
  rag)
    target="gateway-rag"
    estimate="较大；包含 Torch、本地 Embedding 和 Qwen 权重"
    ;;
  vision)
    target="gateway-vision"
    estimate="很大；包含 Torch、OCR、FFmpeg 和 RealESRGAN"
    ;;
  full)
    target="gateway-full"
    estimate="最大；包含 RAG、视觉和全部本地模型能力"
    ;;
esac

show_summary() {
  echo
  echo "安装方案"
  echo "  Profile    : $profile"
  echo "  Docker target: $target"
  echo "  Accelerator: $accelerator"
  echo "  Monitoring : $monitoring"
  echo "  资源提示   : $estimate"
  echo
}

if [[ "$show_plan" == "true" ]]; then
  show_summary
  exit 0
fi

tmp_state="$(mktemp "$ROOT_DIR/.aigateway-install.env.XXXXXX")"
trap 'rm -f "$tmp_state"' EXIT
{
  echo "# Generated by scripts/quickstart.sh. Re-run the installer to change it."
  echo "GATEWAY_INSTALL_PROFILE=$profile"
  echo "GATEWAY_IMAGE_TARGET=$target"
  echo "GATEWAY_ACCELERATOR=$accelerator"
  echo "GATEWAY_MONITORING=$monitoring"
} > "$tmp_state"
mv "$tmp_state" "$INSTALL_STATE"
trap - EXIT

compose=(docker compose --env-file "$INSTALL_STATE" -f docker-compose.yml)
if [[ "$accelerator" == "cuda" ]]; then
  compose+=(-f docker-compose.gpu.yml)
fi
if [[ "$monitoring" == "true" ]]; then
  compose+=(-f docker-compose.monitoring.yml)
fi

if [[ "$action" == "down" ]]; then
  "${compose[@]}" down
  info "服务已停止；数据卷与安装方案均已保留"
  exit 0
fi

show_summary
if [[ "$start" != "true" ]]; then
  info "安装方案已保存到 .aigateway-install.env"
  exit 0
fi

command -v docker >/dev/null 2>&1 || fail "未找到 Docker"
docker info >/dev/null 2>&1 || fail "Docker 未运行或当前用户无访问权限"

if [[ ! -f "$ROOT_DIR/.env" ]]; then
  [[ -f "$ROOT_DIR/.env.example" ]] || fail ".env.example 不存在"
  cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
  warn "已创建 .env；请在其中配置至少一个模型提供商 API Key"
fi

# ---- Generate default admin API key (first-time only) ----
if ! grep -q '^ADMIN_API_KEY=' "$ROOT_DIR/.env" 2>/dev/null; then
  ADMIN_KEY="gw-$(openssl rand -hex 24)"
  echo "ADMIN_API_KEY=${ADMIN_KEY}" >> "$ROOT_DIR/.env"
  # Opt in to one-time bootstrap credential prefill on the login page for this
  # freshly-installed local instance. Operators can remove/disable it for
  # shared or internet-facing deployments.
  echo "AI_GATEWAY_PREFILL_INITIAL_CREDENTIALS=true" >> "$ROOT_DIR/.env"

  echo ""
  echo "=========================================="
  echo "  默认管理员凭据（请妥善保存！）"
  echo "=========================================="
  echo "  API Key : ${ADMIN_KEY}"
  echo "=========================================="
  echo ""
  warn "这是默认管理员密钥，首次登录后请务必重置！"
fi

up_args=(up -d)
if [[ "$build" == "true" ]]; then
  up_args+=(--build)
fi
"${compose[@]}" "${up_args[@]}"

info "等待 Gateway 就绪（最多 60 秒）..."
ready="false"
for _ in $(seq 1 30); do
  if curl -sf http://localhost:8000/health >/dev/null 2>&1; then
    ready="true"
    break
  fi
  sleep 2
done
if [[ "$ready" == "true" ]]; then
  info "Gateway 已就绪"
else
  warn "Gateway 60 秒内未就绪，请运行: ${compose[*]} logs gateway"
fi

echo
echo "API Gateway : http://localhost:8000"
echo "控制台      : http://localhost:3000"
[[ "$monitoring" == "true" ]] && echo "Prometheus  : http://localhost:9090" && echo "Grafana     : http://localhost:3001"
echo "调整能力    : bash scripts/quickstart.sh --add rag|vision|gpu"
echo "查看方案    : bash scripts/quickstart.sh --show-plan"
