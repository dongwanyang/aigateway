#!/usr/bin/env bash
# Install AI Gateway from the current checkout without deploying the application
# containers. Safe to re-run: packages are installed in the repository's venv.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT_DIR="$(pwd)"
VENV_DIR="$ROOT_DIR/.venv"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info() { echo -e "${GREEN}[✓]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
fail() { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
AI Gateway 源码安装器

用法:
  bash scripts/install-source.sh
  bash scripts/install-source.sh --profile full
  bash scripts/install-source.sh --python /path/to/python3.12

选项:
  --profile runtime|rag|vision|full  安装对应的 Python 可选能力（默认 runtime）
  --python <path>                    指定 Python 3.12 解释器
  --no-frontend                      不安装控制台 npm 依赖
  -h, --help                         显示帮助
EOF
}

profile="runtime"
python_command=""
install_frontend="true"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --profile)
      [[ $# -ge 2 ]] || fail "--profile 缺少值"
      profile="$2"; shift 2
      ;;
    --python)
      [[ $# -ge 2 ]] || fail "--python 缺少值"
      python_command="$2"; shift 2
      ;;
    --no-frontend)
      install_frontend="false"; shift
      ;;
    -h|--help)
      usage; exit 0
      ;;
    *)
      fail "源码安装不支持参数: $1；Docker 部署请使用 --docker"
      ;;
  esac
done

case "$profile" in
  runtime) api_extra="" ;;
  rag) api_extra="rag" ;;
  vision) api_extra="vision" ;;
  full) api_extra="all" ;;
  *) fail "不支持的 profile: $profile" ;;
esac

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  if [[ -z "$python_command" ]]; then
    if command -v python3.12 >/dev/null 2>&1; then
      python_command="$(command -v python3.12)"
    elif command -v uv >/dev/null 2>&1; then
      python_command="uv"
    else
      fail "未找到 Python 3.12；请先安装 Python 3.12 或 uv，或使用 --python 指定解释器"
    fi
  fi

  if [[ "$python_command" == "uv" ]]; then
    info "使用 uv 创建 Python 3.12 虚拟环境"
    uv venv --python 3.12 --seed "$VENV_DIR"
  else
    "$python_command" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))' \
      || fail "源码安装需要 Python 3.12"
    info "使用 $python_command 创建虚拟环境"
    "$python_command" -m venv "$VENV_DIR"
  fi
fi

venv_python="$VENV_DIR/bin/python"
"$venv_python" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))' \
  || fail "$VENV_DIR 不是 Python 3.12 虚拟环境，请删除或移动它后重试"

info "安装 aigateway-core"
"$venv_python" -m pip install -e "$ROOT_DIR/aigateway-core"

api_requirement="$ROOT_DIR/aigateway-api"
if [[ -n "$api_extra" ]]; then
  api_requirement="${api_requirement}[${api_extra}]"
fi
info "安装 aigateway-api（profile: $profile）"
"$venv_python" -m pip install -e "$api_requirement"

info "安装 aigateway-cli"
"$venv_python" -m pip install -e "$ROOT_DIR/aigateway-cli"

if [[ "$install_frontend" == "true" ]]; then
  command -v npm >/dev/null 2>&1 || fail "未找到 npm；也可使用 --no-frontend 跳过控制台依赖"
  info "安装控制台 npm 依赖"
  npm --prefix "$ROOT_DIR/control-panel" install
else
  warn "已跳过控制台 npm 依赖"
fi

if [[ ! -f "$ROOT_DIR/.env" && -f "$ROOT_DIR/.env.example" ]]; then
  cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
  warn "已创建 .env；请在其中配置至少一个模型提供商 API Key"
fi

# ---- Generate default admin API key (first-time only) ----
if ! grep -q '^ADMIN_API_KEY=' "$ROOT_DIR/.env" 2>/dev/null; then
  ADMIN_KEY="gw-$(openssl rand -hex 24)"
  echo "ADMIN_API_KEY=${ADMIN_KEY}" >> "$ROOT_DIR/.env"

  # Replace placeholder in config.yaml — fail loudly if config doesn't exist
  if [[ -f "$ROOT_DIR/config.yaml" ]]; then
    sed -i -E "s|key:[[:space:]]*\$\{ADMIN_API_KEY:-[^}]*\}|key: ${ADMIN_KEY}|g" "$ROOT_DIR/config.yaml"
  else
    error "config.yaml not found at $ROOT_DIR/config.yaml — cannot embed admin key"
    exit 1
  fi

  echo ""
  echo "=========================================="
  echo "  默认管理员凭据（请妥善保存！）"
  echo "=========================================="
  echo "  API Key : ${ADMIN_KEY}"
  echo "=========================================="
  echo ""
  warn "这是默认管理员密钥，首次登录后请务必重置！"
fi

echo
info "源码安装完成"
echo "激活环境 : source \"$VENV_DIR/bin/activate\""
echo "启动 API : uvicorn aigateway_api.main:create_app --factory --host 0.0.0.0 --port 8000"
if [[ "$install_frontend" == "true" ]]; then
  echo "启动控制台: npm --prefix \"$ROOT_DIR/control-panel\" run dev"
fi
