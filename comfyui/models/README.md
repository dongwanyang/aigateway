# ComfyUI model storage

This directory is mounted into the ComfyUI container. The image phase permits
only the checkpoint names listed in `generation_optimization.draft_workflow.comfyui.allowed_checkpoints`.

Models are deliberately not downloaded during image build or container startup.
Place the approved checkpoint under `checkpoints/` only after the GPU and disk
preflight checks pass.
