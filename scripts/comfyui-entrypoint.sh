#!/usr/bin/env bash
set -euo pipefail

comfy_root="${COMFYUI_ROOT:-/opt/ComfyUI}"
manager_dir="$comfy_root/user/__manager"
manager_config="$manager_dir/config.ini"
manager_template="${COMFYUI_MANAGER_CONFIG_TEMPLATE:-/opt/comfyui-manager-config.ini}"
python_bin="${COMFYUI_PYTHON:-python}"

mkdir -p \
  "$comfy_root/custom_nodes" \
  "$comfy_root/input" \
  "$comfy_root/output" \
  "$comfy_root/user/default/workflows" \
  "$manager_dir"

# Only seed a new persistent user volume. Manager owns this file afterwards, so
# administrator changes and restored snapshots survive container replacement.
if [[ ! -e "$manager_config" ]]; then
  install -m 0644 "$manager_template" "$manager_config"
fi

vram_args=()
case "${COMFYUI_VRAM_FLAG:-}" in
  "") ;;
  --highvram|--normalvram|--lowvram|--novram|--cpu) vram_args=("$COMFYUI_VRAM_FLAG") ;;
  *)
    echo "Unsupported COMFYUI_VRAM_FLAG: $COMFYUI_VRAM_FLAG" >&2
    exit 2
    ;;
esac

# ComfyUI 0.28 enables Dynamic VRAM by default.  On some NVIDIA/CUDA allocator
# combinations (observed on T4 + cudaMallocAsync), its allocator accounting can
# reject even a four-byte model allocation while almost all VRAM is free.  Keep
# the mature allocator path as the product default until the upstream path is
# reliable for the pinned image.  Operators can explicitly opt back in.
dynamic_vram_args=()
case "${COMFYUI_DISABLE_DYNAMIC_VRAM:-true}" in
  true) dynamic_vram_args=(--disable-dynamic-vram) ;;
  false) ;;
  *)
    echo "Unsupported COMFYUI_DISABLE_DYNAMIC_VRAM: $COMFYUI_DISABLE_DYNAMIC_VRAM" >&2
    exit 2
    ;;
esac

# CORS: the control panel (e.g. localhost:3000) opens ComfyUI (localhost:8188) in
# a new tab — different port = cross-site, so ComfyUI's origin_only_middleware
# would 403 the link-click. --enable-cors-header replaces that middleware with a
# pass-through. Default "*" is safe because ComfyUI is loopback-bound
# (COMFYUI_HOST_BIND=127.0.0.1 in docker-compose); set COMFYUI_CORS_ORIGIN to a
# specific origin (e.g. http://host:3000) if you expose ComfyUI beyond loopback.
cors_args=()
if [[ -n "${COMFYUI_CORS_ORIGIN:-}" ]]; then
  cors_args=(--enable-cors-header "$COMFYUI_CORS_ORIGIN")
elif [[ "${COMFYUI_CORS_ENABLED:-true}" == "true" ]]; then
  cors_args=(--enable-cors-header)
fi

exec "$python_bin" "$comfy_root/main.py" \
  --listen 0.0.0.0 \
  --port 8188 \
  --disable-auto-launch \
  --enable-manager \
  --output-directory "$comfy_root/output" \
  --input-directory "$comfy_root/input" \
  "${cors_args[@]}" \
  "${dynamic_vram_args[@]}" \
  "${vram_args[@]}" \
  "$@"
