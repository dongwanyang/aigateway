#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT_DIR="$(pwd)"
STATE_FILE="$ROOT_DIR/.aigateway-install.env"
RUNTIME_DIR="$ROOT_DIR/.aigateway/runtime"
RUNTIME_CONFIG="$RUNTIME_DIR/config.yaml"

info() { printf '[✓] %s\n' "$*"; }
warn() { printf '[!] %s\n' "$*" >&2; }
fail() { printf '[✗] %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
AI Gateway 跨平台安装器

用法:
  bash scripts/quickstart.sh --edition lite
  bash scripts/quickstart.sh --edition full --distribution source --build
  bash scripts/quickstart.sh --edition full --monitoring --production

选项:
  --edition lite|knowledge|studio|full
  --distribution image|source        默认 image（从 GHCR 拉取）
  --comfyui container|native|remote
  --embedding container|native|remote
  --comfyui-url URL                   remote 模式必需
  --embedding-url URL                 remote 模式必需
  --monitoring / --no-monitoring
  --production / --no-production
  --install-models                    通过模型管理器安装批准模型
  --build                             source 模式强制重建
  --no-start                          只生成安装状态和运行配置
  --show-plan                         显示方案，不写文件
  --non-interactive
  --down                              停止服务，保留卷和数据
EOF
}

edition="lite"
distribution="image"
comfyui_mode=""
embedding_mode=""
comfyui_url=""
embedding_url=""
monitoring="false"
production="false"
interactive="true"
start="true"
show_plan="false"
build="false"
install_models="false"
action="up"
edition_explicit="false"

if [[ -f "$STATE_FILE" ]]; then
  if grep -q '^GATEWAY_INSTALL_PROFILE=' "$STATE_FILE"; then
    fail "检测到旧安装状态。旧 --profile 参数已移除；请重新运行 --edition lite|knowledge|studio|full。数据卷不会被删除。"
  fi
  while IFS='=' read -r key value; do
    case "$key" in
      AIGATEWAY_EDITION) edition="$value" ;;
      AIGATEWAY_DISTRIBUTION) distribution="$value" ;;
      AIGATEWAY_COMFYUI_MODE) comfyui_mode="$value" ;;
      AIGATEWAY_EMBEDDING_MODE) embedding_mode="$value" ;;
      AIGATEWAY_MONITORING) monitoring="$value" ;;
      AIGATEWAY_PRODUCTION) production="$value" ;;
      COMFYUI_SERVER_URL) comfyui_url="$value" ;;
      EMBEDDING_API_BASE) embedding_url="$value" ;;
    esac
  done < "$STATE_FILE"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --edition)
      [[ $# -ge 2 ]] || fail "--edition 缺少值"
      edition="$2"; edition_explicit="true"; shift 2 ;;
    --distribution)
      [[ $# -ge 2 ]] || fail "--distribution 缺少值"
      distribution="$2"; shift 2 ;;
    --comfyui)
      [[ $# -ge 2 ]] || fail "--comfyui 缺少值"
      comfyui_mode="$2"; shift 2 ;;
    --embedding)
      [[ $# -ge 2 ]] || fail "--embedding 缺少值"
      embedding_mode="$2"; shift 2 ;;
    --comfyui-url)
      [[ $# -ge 2 ]] || fail "--comfyui-url 缺少值"
      comfyui_url="$2"; shift 2 ;;
    --embedding-url)
      [[ $# -ge 2 ]] || fail "--embedding-url 缺少值"
      embedding_url="$2"; shift 2 ;;
    --monitoring) monitoring="true"; shift ;;
    --no-monitoring) monitoring="false"; shift ;;
    --production) production="true"; shift ;;
    --no-production) production="false"; shift ;;
    --install-models) install_models="true"; shift ;;
    --build|build) build="true"; shift ;;
    --no-start) start="false"; shift ;;
    --show-plan) show_plan="true"; start="false"; interactive="false"; shift ;;
    --non-interactive) interactive="false"; shift ;;
    --down|down) action="down"; interactive="false"; shift ;;
    up) shift ;;
    -h|--help) usage; exit 0 ;;
    --profile|--accelerator|--add|--remove)
      fail "$1 已移除；请使用 --edition 和自动平台检测" ;;
    *) fail "未知参数: $1" ;;
  esac
done

case "$edition" in lite|knowledge|studio|full) ;; *) fail "不支持的 edition: $edition" ;; esac
case "$distribution" in image|source) ;; *) fail "不支持的 distribution: $distribution" ;; esac

os_name="$(uname -s)"
arch_name="$(uname -m)"
platform="linux"
accelerator="cpu"
if [[ "$os_name" == "Darwin" && "$arch_name" == "arm64" ]]; then
  platform="apple"
  accelerator="mps"
elif [[ "$os_name" == "Linux" ]]; then
  if grep -qi microsoft /proc/version 2>/dev/null; then
    platform="windows-wsl2"
  fi
  if [[ "$edition" != "lite" ]]; then
    accelerator="cuda"
  fi
else
  fail "当前仅支持 Linux、Windows WSL2 和 Apple Silicon"
fi

gpu_count=0
gpu_vram_mb=0
gateway_gpu_device=0
comfyui_gpu_device=0
gateway_memory_fraction=""
comfyui_vram_flag=""
model_dir="$ROOT_DIR/models"
[[ "$platform" == "apple" ]] && model_dir="$HOME/.aigateway/models"
if [[ "$accelerator" == "cuda" ]] && command -v nvidia-smi >/dev/null 2>&1; then
  mapfile -t gpu_memory < <(
    nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null \
      | awk '/^[[:space:]]*[0-9]+[[:space:]]*$/{gsub(/[[:space:]]/,""); print}'
  )
  gpu_count="${#gpu_memory[@]}"
  if (( gpu_count > 0 )); then
    gpu_vram_mb="${gpu_memory[0]}"
    if (( gpu_count > 1 )); then
      comfyui_gpu_device=1
      gateway_memory_fraction="0.90"
    elif [[ "$edition" == "knowledge" ]]; then
      gateway_memory_fraction="0.90"
    else
      gateway_memory_fraction="0.40"
    fi
    if (( gpu_vram_mb < 12288 )); then
      comfyui_vram_flag="--lowvram"
    fi
  fi
fi

needs_knowledge="false"
needs_studio="false"
case "$edition" in
  knowledge) needs_knowledge="true" ;;
  studio) needs_studio="true" ;;
  full) needs_knowledge="true"; needs_studio="true" ;;
esac

if [[ -z "$comfyui_mode" ]]; then
  if [[ "$needs_studio" != "true" ]]; then
    comfyui_mode="remote"
  elif [[ "$platform" == "apple" ]]; then
    comfyui_mode="native"
  else
    comfyui_mode="container"
  fi
fi
if [[ -z "$embedding_mode" ]]; then
  if [[ "$needs_knowledge" != "true" ]]; then
    embedding_mode="container"
  elif [[ "$platform" == "apple" ]]; then
    embedding_mode="native"
  else
    embedding_mode="container"
  fi
fi
case "$comfyui_mode" in container|native|remote) ;; *) fail "不支持的 comfyui 模式" ;; esac
case "$embedding_mode" in container|native|remote) ;; *) fail "不支持的 embedding 模式" ;; esac

if [[ "$platform" == "apple" && "$comfyui_mode" == "container" && "$needs_studio" == "true" ]]; then
  fail "Apple Silicon 的 Docker 容器无法使用 MPS；请选择 --comfyui native 或 remote"
fi
if [[ "$platform" != "apple" && "$comfyui_mode" == "native" ]]; then
  fail "native ComfyUI 当前仅支持 Apple Silicon"
fi
if [[ "$embedding_mode" == "remote" && "$needs_knowledge" == "true" && -z "$embedding_url" ]]; then
  fail "--embedding remote 必须同时提供 --embedding-url"
fi
if [[ "$comfyui_mode" == "remote" && "$needs_studio" == "true" && -z "$comfyui_url" ]]; then
  fail "--comfyui remote 必须同时提供 --comfyui-url"
fi

if [[ "$comfyui_mode" == "container" ]]; then
  comfyui_url="http://comfyui:8188"
elif [[ "$comfyui_mode" == "native" ]]; then
  comfyui_url="http://host.docker.internal:8188"
fi
if [[ "$embedding_mode" == "native" ]]; then
  embedding_url="http://host.docker.internal:8189/v1"
fi

version="${AIGATEWAY_VERSION:-latest}"
case "$edition:$platform" in
  lite:*) target="gateway-runtime"; image_suffix="lite" ;;
  knowledge:apple) target="gateway-rag-cpu"; image_suffix="knowledge" ;;
  knowledge:*) target="gateway-rag"; image_suffix="knowledge-cuda" ;;
  studio:apple) target="gateway-vision-cpu"; image_suffix="studio-arm64" ;;
  studio:*) target="gateway-vision"; image_suffix="studio-cuda" ;;
  full:apple) target="gateway-full-cpu"; image_suffix="full-arm64" ;;
  full:*) target="gateway-full"; image_suffix="full-cuda" ;;
esac
gateway_image="ghcr.io/dongwanyang/aigateway-gateway:${version}-${image_suffix}"
control_image="ghcr.io/dongwanyang/aigateway-control-panel:${version}"
comfy_image="ghcr.io/dongwanyang/aigateway-comfyui:${version}-cuda"
pull_policy=$([[ "$distribution" == "image" ]] && printf always || printf build)

profiles=()
[[ "$needs_knowledge" == "true" ]] && profiles+=(knowledge)
[[ "$needs_studio" == "true" && "$comfyui_mode" == "container" ]] && profiles+=(comfy-container)
[[ "$monitoring" == "true" ]] && profiles+=(monitoring)
compose_profiles="$(IFS=,; printf '%s' "${profiles[*]:-}")"

show_summary() {
  printf '\n安装方案\n'
  printf '  Edition      : %s\n' "$edition"
  printf '  Distribution : %s\n' "$distribution"
  printf '  Platform     : %s/%s\n' "$platform" "$arch_name"
  printf '  Accelerator  : %s\n' "$accelerator"
  if [[ "$accelerator" == "cuda" ]]; then
    printf '  NVIDIA GPUs  : %s (GPU 0 VRAM: %s MiB)\n' "$gpu_count" "$gpu_vram_mb"
    (( gpu_count > 0 && gpu_vram_mb < 12288 )) \
      && printf '  VRAM warning : ComfyUI 将使用 --lowvram；高分辨率任务仍可能 OOM\n'
  fi
  printf '  ComfyUI      : %s\n' "$comfyui_mode"
  printf '  Embedding    : %s\n' "$embedding_mode"
  printf '  Profiles     : %s\n' "${compose_profiles:-core}"
  printf '  Monitoring   : %s\n' "$monitoring"
  printf '  Production   : %s\n\n' "$production"
}

if [[ "$interactive" == "true" && "$edition_explicit" == "false" ]]; then
  printf '请选择套餐: 1) Lite 2) Knowledge 3) Studio 4) Full\n'
  read -r -p "选择 [1-4，当前 ${edition}]: " choice
  case "${choice:-}" in
    1) edition="lite" ;; 2) edition="knowledge" ;;
    3) edition="studio" ;; 4) edition="full" ;; "") ;;
    *) fail "无效选择" ;;
  esac
  exec "$0" --edition "$edition" --distribution "$distribution" \
    $([[ "$monitoring" == "true" ]] && printf -- --monitoring || printf -- --no-monitoring) \
    $([[ "$production" == "true" ]] && printf -- --production || printf -- --no-production)
fi

show_summary
[[ "$show_plan" == "true" ]] && exit 0

command -v python3 >/dev/null 2>&1 || fail "安装器需要 python3 生成运行配置"
python3 -c 'import yaml' >/dev/null 2>&1 || fail "安装器需要 PyYAML（python3 -m pip install pyyaml）"

mkdir -p "$RUNTIME_DIR"
tmp_state="$(mktemp "$ROOT_DIR/.aigateway-install.env.XXXXXX")"
trap 'rm -f "$tmp_state"' EXIT
{
  echo "# Generated by scripts/quickstart.sh; do not store secrets here."
  echo "AIGATEWAY_INSTALL_STATE_VERSION=2"
  echo "AIGATEWAY_EDITION=$edition"
  echo "AIGATEWAY_DISTRIBUTION=$distribution"
  echo "AIGATEWAY_PLATFORM=$platform"
  echo "AIGATEWAY_ACCELERATOR=$accelerator"
  echo "AIGATEWAY_COMFYUI_MODE=$comfyui_mode"
  echo "AIGATEWAY_EMBEDDING_MODE=$embedding_mode"
  echo "AIGATEWAY_MONITORING=$monitoring"
  echo "AIGATEWAY_PRODUCTION=$production"
  echo "AIGATEWAY_RUNTIME_CONFIG=$RUNTIME_CONFIG"
  echo "AIGATEWAY_MODEL_DIR=$model_dir"
  echo "AIGATEWAY_PULL_POLICY=$pull_policy"
  echo "GATEWAY_IMAGE_TARGET=$target"
  echo "GATEWAY_IMAGE=$gateway_image"
  echo "GATEWAY_BUILD_CACHE_FROM=type=registry,ref=$gateway_image"
  echo "CONTROL_PANEL_IMAGE=$control_image"
  echo "CONTROL_PANEL_BUILD_CACHE_FROM=type=registry,ref=$control_image"
  echo "COMFYUI_IMAGE=$comfy_image"
  echo "COMFYUI_BUILD_CACHE_FROM=type=registry,ref=$comfy_image"
  echo "COMFYUI_SERVER_URL=$comfyui_url"
  echo "EMBEDDING_API_BASE=$embedding_url"
  echo "GATEWAY_CUDA_VISIBLE_DEVICES=$gateway_gpu_device"
  echo "COMFYUI_CUDA_VISIBLE_DEVICES=$comfyui_gpu_device"
  echo "GATEWAY_CUDA_MEMORY_FRACTION=$gateway_memory_fraction"
  echo "COMFYUI_VRAM_FLAG=$comfyui_vram_flag"
  echo "COMPOSE_PROFILES=$compose_profiles"
} > "$tmp_state"
mv "$tmp_state" "$STATE_FILE"
trap - EXIT

render_args=(
  "$ROOT_DIR/scripts/render-deployment-config.py"
  --source "$ROOT_DIR/config.yaml"
  --output "$RUNTIME_CONFIG"
  --edition "$edition"
  --accelerator "$accelerator"
  --embedding-mode "$embedding_mode"
  --comfyui-url "${comfyui_url:-http://comfyui.invalid}"
  --embedding-url "$embedding_url"
)
[[ "$monitoring" == "true" ]] && render_args+=(--monitoring)
python3 "${render_args[@]}"

if [[ ! -f "$ROOT_DIR/.env" ]]; then
  cp "$ROOT_DIR/.env.example" "$ROOT_DIR/.env"
  warn "已创建 .env；请配置模型提供商密钥"
fi

env_value() {
  awk -v key="$1" 'index($0,key "=")==1{sub("^[^=]*=","");v=$0}END{print v}' \
    "$ROOT_DIR/.env" 2>/dev/null
}
set_env_value() {
  local key="$1" value="$2" temp
  temp="$(mktemp "$ROOT_DIR/.env.tmp.XXXXXX")"
  awk -v key="$key" -v value="$value" '
    BEGIN{done=0}
    index($0,key "=")==1{print key "=" value;done=1;next}
    {print}
    END{if(!done)print key "=" value}
  ' "$ROOT_DIR/.env" > "$temp"
  mv "$temp" "$ROOT_DIR/.env"
}

resolve_auth_db_path() {
  local configured
  configured="$(env_value AI_GATEWAY_AUTH_DB_PATH)"
  if [[ -z "$configured" ]]; then
    printf '%s\n' "$ROOT_DIR/data/auth.db"
  elif [[ "$configured" == /* ]]; then
    printf '%s\n' "$configured"
  else
    printf '%s\n' "$ROOT_DIR/$configured"
  fi
}

browser_admin_exists() {
  local db_path="$1"
  [[ -f "$db_path" ]] || return 1
  python3 - "$db_path" <<'PY'
import sqlite3
import sys

try:
    with sqlite3.connect(sys.argv[1]) as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_users'"
        ).fetchone()
        if not table:
            raise SystemExit(1)
        raise SystemExit(
            0 if conn.execute("SELECT 1 FROM admin_users LIMIT 1").fetchone() else 1
        )
except sqlite3.Error:
    raise SystemExit(0)
PY
}

if [[ -z "$(env_value ADMIN_API_KEY)" ]]; then
  command -v openssl >/dev/null 2>&1 || fail "生成本地凭据需要 openssl"
  set_env_value ADMIN_API_KEY "gw-$(openssl rand -hex 24)"
  warn "已生成新的 /v1 API Key，请从 .env 安全保存"
fi

if browser_admin_exists "$(resolve_auth_db_path)"; then
  set_env_value AI_GATEWAY_PREFILL_INITIAL_CREDENTIALS false
elif [[ -z "$(env_value AI_GATEWAY_INITIAL_ADMIN_PASSWORD)" ]]; then
  command -v openssl >/dev/null 2>&1 || fail "生成本地凭据需要 openssl"
  set_env_value AI_GATEWAY_INITIAL_ADMIN_PASSWORD "adm-$(openssl rand -hex 18)"
  set_env_value AI_GATEWAY_PREFILL_INITIAL_CREDENTIALS true
  warn "已生成一次性控制台初始密码，请从 .env 安全保存"
fi
if [[ "$monitoring" == "true" ]]; then
  grafana_password="$(env_value GRAFANA_ADMIN_PASSWORD)"
  if [[ -z "$grafana_password" || "$grafana_password" == "replace-with-a-long-random-password" ]]; then
    command -v openssl >/dev/null 2>&1 || fail "生成 Grafana 凭据需要 openssl"
    set_env_value GRAFANA_ADMIN_PASSWORD "graf-$(openssl rand -hex 18)"
    warn "已生成 Grafana 管理员密码，请从 .env 安全保存"
  fi
fi

if [[ "$start" == "true" || "$action" == "down" ]]; then
  command -v docker >/dev/null 2>&1 || fail "未找到 Docker"
  docker compose version >/dev/null 2>&1 || fail "需要 Docker Compose v2"
fi

if [[ "$platform" != "apple" && "$accelerator" == "cuda" && "$start" == "true" ]]; then
  command -v nvidia-smi >/dev/null 2>&1 || fail "未找到 nvidia-smi"
  nvidia-smi >/dev/null || fail "NVIDIA 驱动不可用，请修复或重启后再安装"
  docker run --rm --gpus all nvidia/cuda:13.0.1-base-ubuntu24.04 nvidia-smi >/dev/null \
    || fail "容器 GPU smoke test 失败"
fi

if [[ "$install_models" == "true" ]]; then
  if [[ "$needs_knowledge" == "true" && "$embedding_mode" != "remote" ]]; then
    AIGATEWAY_MODEL_DIR="$model_dir" \
      "$ROOT_DIR/scripts/model-manager.sh" install qwen3-embedding-0.6b
  fi
  if [[ "$needs_studio" == "true" && "$comfyui_mode" != "remote" ]]; then
    comfy_data_dir="$ROOT_DIR/comfyui"
    [[ "$platform" == "apple" ]] && comfy_data_dir="$HOME/.aigateway/comfyui"
    AIGATEWAY_COMFY_DATA_DIR="$comfy_data_dir" \
      "$ROOT_DIR/scripts/model-manager.sh" install sdxl-base
    AIGATEWAY_COMFY_DATA_DIR="$comfy_data_dir" \
      "$ROOT_DIR/scripts/model-manager.sh" install realesrgan-x4plus
    AIGATEWAY_COMFY_DATA_DIR="$comfy_data_dir" \
      "$ROOT_DIR/scripts/model-manager.sh" install qwen-image
    AIGATEWAY_COMFY_DATA_DIR="$comfy_data_dir" \
      "$ROOT_DIR/scripts/model-manager.sh" install wan2.2-ti2v-5b
  fi
fi

if [[ "$platform" == "apple" && "$start" == "true" \
      && ( "$comfyui_mode" == "native" || "$embedding_mode" == "native" ) ]]; then
  "$ROOT_DIR/scripts/native-macos-services.sh" install \
    --comfyui "$comfyui_mode" --embedding "$embedding_mode"
fi

compose=(docker compose --env-file "$STATE_FILE" -f docker-compose.yml)
[[ "$accelerator" == "cuda" ]] && compose+=(-f docker-compose.cuda.yml)
[[ "$production" == "true" ]] && compose+=(-f docker-compose.prod.yml)

if [[ "$action" == "down" ]]; then
  "${compose[@]}" down
  info "服务已停止；数据卷和模型均保留"
  exit 0
fi
if [[ "$start" != "true" ]]; then
  info "安装状态和运行配置已生成"
  exit 0
fi

if [[ "$distribution" == "image" ]]; then
  "${compose[@]}" pull
else
  build_args=(build)
  [[ "$build" == "true" ]] && info "将从当前源码重新执行 Compose build"
  "${compose[@]}" "${build_args[@]}"
fi
"${compose[@]}" up -d --remove-orphans

gateway_health_url="http://127.0.0.1:${AIGATEWAY_API_PORT:-8000}/health"
if [[ "$production" == "true" ]]; then
  gateway_health_url="https://127.0.0.1/aigateway/health"
fi
gateway_ready="false"
for _ in $(seq 1 45); do
  if curl -kfsS "$gateway_health_url" >/dev/null 2>&1; then
    info "Gateway 已就绪"
    gateway_ready="true"
    break
  fi
  sleep 2
done
[[ "$gateway_ready" == "true" ]] || fail "Gateway 健康检查超时: $gateway_health_url"

if [[ "$needs_studio" == "true" ]]; then
  docker compose --env-file "$STATE_FILE" exec -T gateway python -c \
    "import httpx; httpx.get('${comfyui_url}/system_stats', timeout=10).raise_for_status()" \
    || fail "Gateway 无法连接 ComfyUI"
fi
if [[ "$needs_knowledge" == "true" && "$embedding_mode" != "container" ]]; then
  docker compose --env-file "$STATE_FILE" exec -T gateway python -c \
    "import httpx; httpx.get('${embedding_url%/v1}/health', timeout=10).raise_for_status()" \
    || fail "Gateway 无法连接 Embedding 服务"
fi

if [[ "$production" == "true" ]]; then
  info "API: https://127.0.0.1/aigateway"
  info "控制台: https://127.0.0.1"
else
  info "API: http://127.0.0.1:${AIGATEWAY_API_PORT:-8000}"
  info "控制台: http://127.0.0.1:${AIGATEWAY_UI_PORT:-3000}"
fi
