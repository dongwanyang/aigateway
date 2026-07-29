#!/usr/bin/env bash
set -euo pipefail

[[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] \
  || { echo "native services require Apple Silicon macOS" >&2; exit 1; }

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
NATIVE_ROOT="${AIGATEWAY_NATIVE_ROOT:-$HOME/.aigateway/native}"
MODEL_ROOT="${AIGATEWAY_MODEL_DIR:-$HOME/.aigateway/models}"
COMFY_DATA="${AIGATEWAY_COMFY_DATA_DIR:-$HOME/.aigateway/comfyui}"
LOG_DIR="$HOME/.aigateway/logs"
LAUNCH_AGENTS="$HOME/Library/LaunchAgents"
COMFY_VERSION="${COMFYUI_VERSION:-v0.28.0}"
RERANK_MODEL_PATH="${AIGATEWAY_RERANK_MODEL:-}"
action="${1:-status}"
shift || true
comfy_mode="remote"
embedding_mode="remote"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --comfyui) comfy_mode="$2"; shift 2 ;;
    --embedding) embedding_mode="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

mkdir -p "$NATIVE_ROOT" "$COMFY_DATA"/{models,input,output,workflows} \
  "$LOG_DIR" "$LAUNCH_AGENTS"

install_embedding() {
  local env_dir="$NATIVE_ROOT/ml-venv"
  python3 -m venv "$env_dir"
  "$env_dir/bin/pip" install --upgrade pip
  "$env_dir/bin/pip" install \
    "torch==2.13.0" \
    "sentence-transformers==5.6.1" \
    "fastapi==0.140.9" \
    "uvicorn[standard]==0.51.0"
  cp "$ROOT_DIR/scripts/native-embedding-service.py" \
    "$NATIVE_ROOT/native-embedding-service.py"
  cat > "$LAUNCH_AGENTS/com.aigateway.embedding.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.aigateway.embedding</string>
<key>ProgramArguments</key><array>
<string>$env_dir/bin/uvicorn</string><string>native-embedding-service:app</string>
<string>--app-dir</string><string>$NATIVE_ROOT</string>
<string>--host</string><string>127.0.0.1</string>
<string>--port</string><string>8189</string>
</array>
<key>EnvironmentVariables</key><dict>
<key>AIGATEWAY_EMBEDDING_API_KEY</key><string>local-mps</string>
<key>AIGATEWAY_EMBEDDING_MODEL</key><string>$MODEL_ROOT/qwen3-embedding-0.6b</string>
<key>AIGATEWAY_RERANK_MODEL</key><string>$RERANK_MODEL_PATH</string>
</dict>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>$LOG_DIR/embedding.log</string>
<key>StandardErrorPath</key><string>$LOG_DIR/embedding-error.log</string>
</dict></plist>
EOF
}

install_comfyui() {
  local source_dir="$NATIVE_ROOT/ComfyUI"
  local env_dir="$NATIVE_ROOT/ml-venv"
  if [[ ! -d "$source_dir/.git" ]]; then
    git clone --branch "$COMFY_VERSION" --depth 1 \
      https://github.com/Comfy-Org/ComfyUI.git "$source_dir"
  fi
  python3 -m venv "$env_dir"
  "$env_dir/bin/pip" install --upgrade pip
  "$env_dir/bin/pip" install "torch==2.13.0" \
    "torchvision==0.28.0" "torchaudio==2.11.0"
  printf '%s\n' \
    'torch==2.13.0' \
    'torchvision==0.28.0' \
    'torchaudio==2.11.0' \
    > "$NATIVE_ROOT/comfyui-constraints.txt"
  "$env_dir/bin/pip" install \
    --constraint "$NATIVE_ROOT/comfyui-constraints.txt" \
    -r "$source_dir/requirements.txt"
  cat > "$NATIVE_ROOT/extra_model_paths.yaml" <<EOF
aigateway:
  base_path: $COMFY_DATA
  checkpoints: models/checkpoints
  vae: models/vae
  loras: models/loras
  controlnet: models/controlnet
EOF
  cat > "$LAUNCH_AGENTS/com.aigateway.comfyui.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>com.aigateway.comfyui</string>
<key>WorkingDirectory</key><string>$source_dir</string>
<key>ProgramArguments</key><array>
<string>$env_dir/bin/python</string><string>$source_dir/main.py</string>
<string>--listen</string><string>127.0.0.1</string>
<string>--port</string><string>8188</string>
<string>--disable-auto-launch</string>
<string>--input-directory</string><string>$COMFY_DATA/input</string>
<string>--output-directory</string><string>$COMFY_DATA/output</string>
<string>--extra-model-paths-config</string><string>$NATIVE_ROOT/extra_model_paths.yaml</string>
</array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>StandardOutPath</key><string>$LOG_DIR/comfyui.log</string>
<key>StandardErrorPath</key><string>$LOG_DIR/comfyui-error.log</string>
</dict></plist>
EOF
}

reload_service() {
  local label="$1" plist="$2"
  launchctl bootout "gui/$UID/$label" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$UID" "$plist"
}

case "$action" in
  install|start)
    if [[ "$embedding_mode" == "native" ]]; then
      [[ "$action" == "install" ]] && install_embedding
      reload_service com.aigateway.embedding \
        "$LAUNCH_AGENTS/com.aigateway.embedding.plist"
    fi
    if [[ "$comfy_mode" == "native" ]]; then
      [[ "$action" == "install" ]] && install_comfyui
      reload_service com.aigateway.comfyui \
        "$LAUNCH_AGENTS/com.aigateway.comfyui.plist"
    fi
    ;;
  stop)
    launchctl bootout "gui/$UID/com.aigateway.embedding" >/dev/null 2>&1 || true
    launchctl bootout "gui/$UID/com.aigateway.comfyui" >/dev/null 2>&1 || true
    ;;
  status)
    launchctl print "gui/$UID/com.aigateway.embedding" 2>/dev/null || true
    launchctl print "gui/$UID/com.aigateway.comfyui" 2>/dev/null || true
    ;;
  *) echo "usage: $0 install|start|stop|status" >&2; exit 1 ;;
esac
