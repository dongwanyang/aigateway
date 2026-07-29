# ComfyUI model storage

This directory is mounted into the ComfyUI container. The image phase permits
only the checkpoint names listed in `generation_optimization.draft_workflow.comfyui.allowed_checkpoints`.

Models are deliberately **not** committed to git (they total ~23 GB) and are
**not** downloaded during image build or container startup. Place the approved
checkpoint under `checkpoints/` only after the GPU and disk preflight checks
pass.

## 安装模型

用仓库自带的下载脚本（带 sha256 校验、许可证确认、原子写入）：

```bash
bash scripts/model-manager.sh install sdxl-base              # 图片草稿/精修
bash scripts/model-manager.sh install wan2.2-ti2v-5b         # 视频（含 diffusion + text encoder + vae 三个文件）
bash scripts/model-manager.sh install qwen3-embedding-0.6b   # 知识库 RAG embedding
```

- 模型从 HuggingFace 固定 revision 下载，下载后自动校验 sha256。
- 首次安装需接受模型许可证；非交互场景设 `AIGATEWAY_ACCEPT_MODEL_LICENSE=yes` 跳过。
- 需要 ≥80 GB 可用磁盘空间。
- Embedding 模型落在 `models/qwen3-embedding-0.6b/`（仓库根，非本目录）。

## 检查安装状态

```bash
bash scripts/model-manager.sh list                 # 列出全部已批准模型 + 是否已安装 + 校验状态
bash scripts/model-manager.sh verify wan2.2-ti2v-5b # 只校验不下载（不指定 model 则校验全部）
```

`list` / `verify` 在所有已批准模型都到位且校验通过时返回 0，否则返回非零——
适合在部署脚本或 CI 里作为前置检查。

## 自动路径

`scripts/quickstart.sh --install-models` 在选择 Studio/Full 模式时会自动调用
本脚本安装所需模型，无需手动执行。
