# CLAUDE.md

Claude and other coding agents must read and follow [`AGENTS.md`](AGENTS.md) before modifying this repository. This file contains only Claude-specific durable workflow notes; product, architecture, security, and testing contracts remain authoritative in `AGENTS.md`.

## Cached container rebuilds

For an existing installation, rebuild from the current checkout through the installer so the persisted Edition, accelerator, Compose profiles, generated GPU topology, image target, registry cache reference, and control-panel configuration changes are preserved:

```bash
BUILDKIT_PROGRESS=plain \
  bash scripts/quickstart.sh \
    --non-interactive \
    --distribution source \
    --build
```

After the first installation, `.aigateway/runtime/config.yaml` is the mutable runtime source of truth. A normal quickstart run re-renders deployment-owned fields from that file; it must not replace it wholesale with repository `config.yaml`. Provider/model removals and other settings saved from the control panel therefore survive image rebuilds.

For a first installation or an intentional Edition change, add the required `--edition lite|knowledge|studio|full` argument. Use `--reset-config` only when the user explicitly wants to discard the runtime configuration and restore repository defaults. Do not add `--reset-config` to routine rebuild commands.

CUDA device and worker inventory is generated from the current host by `scripts/render-gpu-topology.py`. Never commit a machine-specific GPU UUID to `config.yaml`; generated `gpu_scheduler.devices`, `gpu_scheduler.workers`, and `gpu_scheduler.inventory_source` belong only in `.aigateway/runtime/config.yaml`.

`--build` requests a source build but does **not** bypass BuildKit cache. Do not replace the command above with bare `docker compose build` or `docker compose up --build`: those commands do not automatically load `.aigateway-install.env`, and can silently select the default Lite target or the wrong GHCR cache image.

Do not use any of the following unless the user explicitly requests cache bypass/cleanup or there is concrete evidence of corrupted cache:

```bash
docker compose build --no-cache
docker builder prune
docker system prune -a
```

The build cache has two distinct sources:

- the current Docker builder's local BuildKit layers and cache mounts;
- inline cache embedded in published GHCR images and imported through Compose `cache_from=type=registry`.

GitHub Actions cache (`type=gha`) accelerates CI only. Published Gateway, ComfyUI, and control-panel images must also export `type=inline` so a new local builder can reuse dependency layers. Images published before inline-cache export was enabled cannot accelerate a fresh local builder; the first rebuild may remain slow until a new image is published.

Expected cache behavior:

- ordinary Python source changes should invalidate only the final source-copy/package-install layers;
- changes to either `pyproject.toml`, Docker build arguments, PyTorch/ComfyUI versions, the selected CPU/CUDA target, or the base-image digest legitimately invalidate earlier layers;
- a healthy cached build should show `importing cache manifest from` and many `CACHED` steps when `BUILDKIT_PROGRESS=plain` is set.

See [`INSTALL.md`](INSTALL.md#重建容器与-buildkit-缓存) for operator commands and cache-miss diagnostics.
