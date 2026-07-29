#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
model="${2:-}"

# 已批准模型规格表：name|label|license|size|target|sha256
# install / verify / list 共用这一份，避免三处各写一份路径与校验和。
emit_specs() {
  local comfy_models="${AIGATEWAY_COMFY_DATA_DIR:-$(cd "$(dirname "$0")/.." && pwd)/comfyui}/models"
  local model_dir="${AIGATEWAY_MODEL_DIR:-$(cd "$(dirname "$0")/.." && pwd)/models}"
  cat <<EOF
sdxl-base|Stability AI SDXL Base 1.0|CreativeML Open RAIL++-M|约 6.94GB|$comfy_models/checkpoints/sd_xl_base_1.0.safetensors|31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b
wan2.2-ti2v-5b-diffusion|Wan2.2 TI2V 5B diffusion (fp16)|Apache-2.0|约 9.4GB|$comfy_models/diffusion_models/wan2.2_ti2v_5B_fp16.safetensors|456f901338bd9eadbded3828b819109a9b68e8a525ca5cf8d0049a69fcfeca1e
wan2.2-ti2v-5b-encoder|Wan2.2 TI2V 5B text encoder (umt5_xxl fp8)|Apache-2.0|约 6.3GB|$comfy_models/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors|c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68
wan2.2-ti2v-5b-vae|Wan2.2 TI2V 5B VAE|Apache-2.0|约 1.4GB|$comfy_models/vae/wan2.2_vae.safetensors|e40321bd36b9709991dae2530eb4ac303dd168276980d3e9bc4b6e2b75fed156
qwen-image-diffusion|Qwen-Image diffusion (fp8)|Apache-2.0|约 20.4GB|$comfy_models/diffusion_models/qwen_image_fp8_e4m3fn.safetensors|98763a127701eb6fb59096f7742cb3aa7d64ed510b9f4e882d8351f8176e3ce3
qwen-image-encoder|Qwen-Image text encoder (Qwen2.5-VL 7B fp8)|Apache-2.0|约 9.38GB|$comfy_models/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors|cb5636d852a0ea6a9075ab1bef496c0db7aef13c02350571e388aea959c5c0b4
qwen-image-vae|Qwen-Image VAE|Apache-2.0|约 254MB|$comfy_models/vae/qwen_image_vae.safetensors|a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f
realesrgan-x4plus|RealESRGAN x4plus|BSD-3-Clause|约 67MB|$comfy_models/upscale_models/RealESRGAN_x4plus.pth|4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1
qwen3-embedding-0.6b|Qwen3-Embedding-0.6B|Apache-2.0|约 1.21GB|$model_dir/qwen3-embedding-0.6b/model.safetensors|0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd
EOF
}

usage() {
  cat >&2 <<EOF
usage: $0 <command> [model]

commands:
  install <model>   下载并校验模型（已存在且校验通过则跳过）
  verify  [model]   只校验已存在模型，不下载（不指定则校验全部）
  list              列出已批准模型、是否已安装、校验状态

approved models:
  sdxl-base                 SDXL Base 1.0（图片草稿/精修）
  wan2.2-ti2v-5b            Wan2.2 TI2V 5B（视频，含 diffusion+encoder+vae 三个文件）
  qwen-image                Qwen-Image FP8（中英文图片，含 diffusion+encoder+vae）
  realesrgan-x4plus         RealESRGAN x4plus（4K 保真放大）
  qwen3-embedding-0.6b      Qwen3 Embedding（知识库 RAG）
EOF
}

case "$action" in
  install|verify|list) ;;
  *) usage; exit 1 ;;
esac

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
model_root="${AIGATEWAY_MODEL_DIR:-$repo_root/models}"
comfy_root="${AIGATEWAY_COMFY_DATA_DIR:-$repo_root/comfyui}"
mkdir -p "$model_root" "$comfy_root"

require_download_space() {
  local minimum_gb="${1:-80}"
  local available_kb
  available_kb="$(df -Pk "$comfy_root" | awk 'NR==2 {print $4}')"
  (( available_kb >= minimum_gb * 1024 * 1024 )) || {
    echo "模型下载已拒绝：可用空间低于 ${minimum_gb}GB" >&2
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

install_realesrgan() {
  local target_dir="$comfy_root/models/upscale_models"
  local target="$target_dir/RealESRGAN_x4plus.pth"
  local url="https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth"
  local sha256="4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1"
  confirm_license "RealESRGAN x4plus" "BSD-3-Clause" "约 67MB"
  mkdir -p "$target_dir"
  if [[ -f "$target" ]]; then
    printf '%s  %s\n' "$sha256" "$target" | sha256sum -c -
    echo "模型已安装且校验通过: $target"
    return
  fi
  local temp
  temp="$(mktemp "$target_dir/.realesrgan-x4plus.XXXXXX")"
  trap 'rm -f "$temp"' EXIT
  curl -fL --retry 3 -o "$temp" "$url"
  printf '%s  %s\n' "$sha256" "$temp" | sha256sum -c -
  chmod 0644 "$temp"
  mv "$temp" "$target"
  trap - EXIT
  echo "已安装: $target"
}

install_qwen_image() {
  local revision="46839d338df81ce625d5fae27d7e370314c0fbc9"
  confirm_license "Qwen-Image FP8 (ComfyUI repackaged)" "Apache-2.0" "约 30.1GB"

  local diffusion_dir="$comfy_root/models/diffusion_models"
  local encoder_dir="$comfy_root/models/text_encoders"
  local vae_dir="$comfy_root/models/vae"
  mkdir -p "$diffusion_dir" "$encoder_dir" "$vae_dir"
  local specs=(
    "split_files/diffusion_models/qwen_image_fp8_e4m3fn.safetensors|$diffusion_dir/qwen_image_fp8_e4m3fn.safetensors|98763a127701eb6fb59096f7742cb3aa7d64ed510b9f4e882d8351f8176e3ce3"
    "split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors|$encoder_dir/qwen_2.5_vl_7b_fp8_scaled.safetensors|cb5636d852a0ea6a9075ab1bef496c0db7aef13c02350571e388aea959c5c0b4"
    "split_files/vae/qwen_image_vae.safetensors|$vae_dir/qwen_image_vae.safetensors|a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f"
  )
  local all_installed="true"
  local spec source target sha256
  for spec in "${specs[@]}"; do
    IFS='|' read -r source target sha256 <<< "$spec"
    if [[ ! -f "$target" ]]; then
      all_installed="false"
      break
    fi
  done
  [[ "$all_installed" == "true" ]] || require_download_space 40

  local temp url
  for spec in "${specs[@]}"; do
    IFS='|' read -r source target sha256 <<< "$spec"
    if [[ -f "$target" ]]; then
      printf '%s  %s\n' "$sha256" "$target" | sha256sum -c -
      continue
    fi
    temp="$(mktemp "$(dirname "$target")/.qwen-image.XXXXXX")"
    trap 'rm -f "$temp"' EXIT
    url="https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/$revision/$source"
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
  [[ "$all_installed" == "true" ]] || require_download_space 40

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

# 校验单个已存在文件。返回 0=通过，1=校验和不符，2=缺失（不下载）。
check_one() {
  local target="$1" sha256="$2"
  if [[ ! -f "$target" ]]; then
    echo "missing"
    return 2
  fi
  if printf '%s  %s\n' "$sha256" "$target" | sha256sum -c - >/dev/null 2>&1; then
    echo "ok"
    return 0
  fi
  echo "mismatch"
  return 1
}

# verify/list 共用：打印每条 spec 的状态。返回码 = 缺失或校验失败的条数。
report_specs() {
  local missing=0 bad=0
  printf '%-28s %-8s %-10s %s\n' "MODEL" "STATUS" "SIZE" "PATH"
  local spec name label license size target sha256 status
  while IFS='|' read -r name label license size target sha256; do
    [[ -z "$name" ]] && continue
    status="$(check_one "$target" "$sha256")" || true
    case "$status" in
      ok) ;;
      missing) missing=$((missing + 1)) ;;
      mismatch) bad=$((bad + 1)) ;;
    esac
    printf '%-28s %-8s %-10s %s\n' "$name" "$status" "$size" "$target"
  done < <(emit_specs)
  echo
  if (( bad > 0 )); then
    echo "⚠️  $bad 个文件校验和不符（可能下载损坏或被篡改），请重新 install" >&2
  fi
  if (( missing > 0 )); then
    echo "ℹ️  $missing 个模型未安装（运行 $0 install <model> 下载）" >&2
  fi
  if (( bad + missing == 0 )); then
    echo "✓ 全部已批准模型已安装且校验通过"
    return 0
  fi
  return 1
}

case "$action" in
  list)
    report_specs
    exit $?
    ;;
  verify)
    # 指定 model 时只校验该模型的文件（wan2.2-ti2v-5b 展开为 3 个文件）
    if [[ -z "$model" ]]; then
      report_specs
      exit $?
    fi
    # 复用 install 的别名 → 过滤相关 spec 行
    printf '%s\n' "$model" | grep -qE '^(sdxl-base|wan2.2-ti2v-5b|qwen-image|realesrgan-x4plus|qwen3-embedding-0.6b)$' \
      || { echo "unknown approved model: $model" >&2; exit 1; }
    missing=0; bad=0
    while IFS='|' read -r name label license size target sha256; do
      [[ -z "$name" ]] && continue
      case "$model" in
        sdxl-base) [[ "$name" == "sdxl-base" ]] || continue ;;
        realesrgan-x4plus) [[ "$name" == "realesrgan-x4plus" ]] || continue ;;
        qwen3-embedding-0.6b) [[ "$name" == "qwen3-embedding-0.6b" ]] || continue ;;
        wan2.2-ti2v-5b) [[ "$name" == wan2.2-ti2v-5b-* ]] || continue ;;
        qwen-image) [[ "$name" == qwen-image-* ]] || continue ;;
      esac
      status="$(check_one "$target" "$sha256")" || true
      printf '%-28s %-8s %-10s %s\n' "$name" "$status" "$size" "$target"
      case "$status" in
        missing) missing=$((missing + 1)) ;;
        mismatch) bad=$((bad + 1)) ;;
      esac
    done < <(emit_specs)
    (( bad + missing == 0 )) && exit 0 || exit 1
    ;;
esac

case "$model" in
  sdxl-base) install_sdxl ;;
  realesrgan-x4plus) install_realesrgan ;;
  qwen3-embedding-0.6b) install_qwen_embedding ;;
  wan2.2-ti2v-5b) install_wan_video ;;
  qwen-image) install_qwen_image ;;
  *) echo "unknown approved model: $model" >&2; exit 1 ;;
esac
