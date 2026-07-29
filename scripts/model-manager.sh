#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
model="${2:-}"
[[ "$action" == "install" ]] || {
  echo "usage: $0 install sdxl-base|qwen3-embedding-0.6b|wan2.2-ti2v-5b" >&2
  exit 1
}

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
model_root="${AIGATEWAY_MODEL_DIR:-$repo_root/models}"
comfy_root="${AIGATEWAY_COMFY_DATA_DIR:-$repo_root/comfyui}"
mkdir -p "$model_root" "$comfy_root"

require_download_space() {
  local available_kb
  available_kb="$(df -Pk "$model_root" | awk 'NR==2 {print $4}')"
  (( available_kb >= 80 * 1024 * 1024 )) || {
    echo "模型下载已拒绝：可用空间低于 80GB" >&2
    exit 1
  }
}

confirm_license() {
  local name="$1" license="$2" size="$3"
  echo "模型: $name"
  echo "许可: $license"
  echo "大小: $size"
  if [[ "${AIGATEWAY_ACCEPT_MODEL_LICENSE:-}" != "yes" ]]; then
    read -r -p "是否已阅读并接受模型许可证？输入 yes 继续: " accepted
    [[ "$accepted" == "yes" ]] || { echo "已取消"; exit 1; }
  fi
}

install_sdxl() {
  local target_dir="$comfy_root/models/checkpoints"
  local target="$target_dir/sd_xl_base_1.0.safetensors"
  local url="https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors"
  local sha256="31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b"
  confirm_license "Stability AI SDXL Base 1.0" "CreativeML Open RAIL++-M" "约 6.94GB"
  mkdir -p "$target_dir"
  if [[ -f "$target" ]]; then
    printf '%s  %s\n' "$sha256" "$target" | sha256sum -c -
    echo "模型已安装且校验通过: $target"
    return
  fi
  require_download_space
  local temp
  temp="$(mktemp "$target_dir/.sdxl-base.XXXXXX")"
  trap 'rm -f "$temp"' EXIT
  local headers=()
  [[ -n "${HF_TOKEN:-}" ]] && headers=(-H "Authorization: Bearer $HF_TOKEN")
  curl -fL --retry 3 "${headers[@]}" -o "$temp" "$url"
  printf '%s  %s\n' "$sha256" "$temp" | sha256sum -c -
  chmod 0644 "$temp"
  mv "$temp" "$target"
  trap - EXIT
  echo "已安装: $target"
}

install_qwen_embedding() {
  local revision="97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
  local weight_sha256="0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd"
  local target="$model_root/qwen3-embedding-0.6b"
  confirm_license "Qwen3-Embedding-0.6B" "Apache-2.0" "约 1.21GB"
  if [[ -f "$target/model.safetensors" ]]; then
    printf '%s  %s\n' "$weight_sha256" "$target/model.safetensors" | sha256sum -c -
    echo "模型已安装且校验通过: $target"
    return
  fi
  require_download_space

  local temp_dir
  temp_dir="$(mktemp -d "$model_root/.qwen3-embedding.XXXXXX")"
  trap 'rm -rf "$temp_dir"' EXIT
  local files=(
    1_Pooling/config.json
    config.json
    config_sentence_transformers.json
    generation_config.json
    merges.txt
    model.safetensors
    modules.json
    tokenizer.json
    tokenizer_config.json
    vocab.json
  )
  local file url
  for file in "${files[@]}"; do
    mkdir -p "$temp_dir/$(dirname "$file")"
    url="https://huggingface.co/Qwen/Qwen3-Embedding-0.6B/resolve/$revision/$file"
    curl -fL --retry 3 -o "$temp_dir/$file" "$url"
  done
  printf '%s  %s\n' "$weight_sha256" "$temp_dir/model.safetensors" \
    | sha256sum -c -
  printf '%s\n' "$revision" > "$temp_dir/.aigateway-revision"
  chmod -R a+rX "$temp_dir"
  mv "$temp_dir" "$target"
  trap - EXIT
  echo "已安装: $target"
}

install_wan_video() {
  local revision="fb1388adc906ab39ffc26ee40e96b22886b56bc4"
  confirm_license "Wan2.2 TI2V 5B (ComfyUI repackaged)" "Apache-2.0" "约 18.1GB"

  local diffusion_dir="$comfy_root/models/diffusion_models"
  local encoder_dir="$comfy_root/models/text_encoders"
  local vae_dir="$comfy_root/models/vae"
  mkdir -p "$diffusion_dir" "$encoder_dir" "$vae_dir"

  local specs=(
    "split_files/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors|$diffusion_dir/wan2.2_ti2v_5B_fp16.safetensors|456f901338bd9eadbded3828b819109a9b68e8a525ca5cf8d0049a69fcfeca1e"
    "split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors|$encoder_dir/umt5_xxl_fp8_e4m3fn_scaled.safetensors|c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68"
    "split_files/vae/wan2.2_vae.safetensors|$vae_dir/wan2.2_vae.safetensors|e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156"
  )
  local all_installed="true"
  local installed_spec installed_source installed_target installed_sha
  for installed_spec in "${specs[@]}"; do
    IFS='|' read -r installed_source installed_target installed_sha \
      <<< "$installed_spec"
    if [[ ! -f "$installed_target" ]]; then
      all_installed="false"
      break
    fi
  done
  [[ "$all_installed" == "true" ]] || require_download_space

  local spec source target sha256 temp url
  for spec in "${specs[@]}"; do
    IFS='|' read -r source target sha256 <<< "$spec"
    if [[ -f "$target" ]]; then
      printf '%s  %s\n' "$sha256" "$target" | sha256sum -c -
      continue
    fi
    temp="$(mktemp "$(dirname "$target")/.wan2.2.XXXXXX")"
    trap 'rm -f "$temp"' EXIT
    url="https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/$revision/$source"
    local headers=()
    [[ -n "${HF_TOKEN:-}" ]] && headers=(-H "Authorization: Bearer $HF_TOKEN")
    curl -fL --retry 3 "${headers[@]}" -o "$temp" "$url"
    printf '%s  %s\n' "$sha256" "$temp" | sha256sum -c -
    chmod 0644 "$temp"
    mv "$temp" "$target"
    trap - EXIT
    echo "已安装: $target"
  done
}

case "$model" in
  sdxl-base) install_sdxl ;;
  qwen3-embedding-0.6b) install_qwen_embedding ;;
  wan2.2-ti2v-5b) install_wan_video ;;
  *) echo "unknown approved model: $model" >&2; exit 1 ;;
esac
