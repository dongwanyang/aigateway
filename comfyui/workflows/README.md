# Versioned ComfyUI workflows

This read-only directory is reserved for reviewed ComfyUI workflow exports.
The image phase currently uses the built-in `image-v1` API workflow contract;
store any future node-graph change here under a new version before selecting it
with `generation_optimization.draft_workflow.comfyui.workflow_version`.

Video confirmation uses the versioned native ComfyUI workflow
`wan2.2-ti2v-5b-v1`. A video request first produces one inexpensive SDXL
keyframe. Confirmation uploads that exact keyframe and reuses its prompt and
seed with Wan2.2 TI2V 5B, then stores the resulting MP4 as the draft result.

Required model files are installed outside the image with:

```bash
./scripts/model-manager.sh install wan2.2-ti2v-5b
```
