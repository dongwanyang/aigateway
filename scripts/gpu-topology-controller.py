#!/usr/bin/env python3
"""Reconcile GPU runtime topology and safely recreate affected local services."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import logging
import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

logger = logging.getLogger("aigateway.gpu_topology_controller")
TOPOLOGY_FIELDS = (
    "gateway_devices",
    "comfyui_devices",
    "device_overrides",
    "comfyui_dynamic_vram_enabled",
)

TOPOLOGY_DEFAULTS: dict[str, Any] = {
    "gateway_devices": "auto",
    "comfyui_devices": "auto",
    "device_overrides": [],
    "comfyui_dynamic_vram_enabled": False,
}


def _load_renderer(repo_root: Path) -> ModuleType:
    path = repo_root / "scripts" / "render-gpu-topology.py"
    spec = importlib.util.spec_from_file_location("aigateway_gpu_topology_renderer", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load topology renderer: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise RuntimeError(f"expected YAML object: {path}")
    return loaded


def _read_locked_yaml(handle: Any, path: Path) -> dict[str, Any]:
    handle.seek(0)
    loaded = yaml.safe_load(handle.read()) or {}
    if not isinstance(loaded, dict):
        raise RuntimeError(f"expected YAML object: {path}")
    return loaded


def _write_locked_yaml(handle: Any, value: dict[str, Any]) -> None:
    rendered = yaml.safe_dump(value, sort_keys=False, allow_unicode=True)
    handle.seek(0)
    original = handle.read()
    try:
        handle.seek(0)
        handle.write(rendered)
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())
    except BaseException:
        try:
            handle.seek(0)
            handle.write(original)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            logger.exception("failed to restore runtime configuration")
        raise


def _read_state(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _fingerprint(
    scheduler: dict[str, Any], inventory: list[dict[str, Any]]
) -> str:
    value = {
        "config": {
            field: scheduler.get(field, TOPOLOGY_DEFAULTS[field])
            for field in TOPOLOGY_FIELDS
        },
        "inventory": [
            {
                "index": item.get("index"),
                "uuid": item.get("uuid"),
                "name": item.get("name"),
                "memory_total_mb": item.get("memory_total_mb"),
            }
            for item in inventory
        ],
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_runtime_topology(
    devices: list[dict[str, Any]], workers: list[dict[str, Any]]
) -> None:
    device_uuids = {
        str(item.get("uuid")) for item in devices if item.get("uuid")
    }
    worker_uuids = {
        str(item.get("device_uuid"))
        for item in workers
        if item.get("device_uuid")
    }
    missing = worker_uuids - device_uuids
    if missing:
        raise RuntimeError(
            "generated GPU topology contains workers without local devices: "
            + ", ".join(sorted(missing))
        )
    worker_ids = [str(item.get("worker_id") or "") for item in workers]
    if not all(worker_ids) or len(worker_ids) != len(set(worker_ids)):
        raise RuntimeError("generated GPU topology contains duplicate worker IDs")


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _compose_command(
    repo_root: Path,
    install_state: Path,
    generated_compose: Path,
    state: dict[str, str],
) -> list[str]:
    command = [
        "docker",
        "compose",
        "--env-file",
        str(repo_root / ".env"),
        "--env-file",
        str(install_state),
        "-f",
        str(repo_root / "docker-compose.yml"),
    ]
    if state.get("AIGATEWAY_ACCELERATOR") == "cuda":
        command.extend(["-f", str(repo_root / "docker-compose.cuda.yml")])
    command.extend(["-f", str(generated_compose)])
    if state.get("AIGATEWAY_PRODUCTION") == "true":
        command.extend(["-f", str(repo_root / "docker-compose.prod.yml")])
    return command


@contextlib.contextmanager
def _config_write_lock(path: Path):
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as sibling_lock:
        fcntl.flock(sibling_lock.fileno(), fcntl.LOCK_EX)
        try:
            with path.open("r+", encoding="utf-8") as config_handle:
                fcntl.flock(config_handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield config_handle
                finally:
                    fcntl.flock(config_handle.fileno(), fcntl.LOCK_UN)
        finally:
            fcntl.flock(sibling_lock.fileno(), fcntl.LOCK_UN)


def reconcile(
    repo_root: Path,
    *,
    apply: bool = True,
    respect_auto_apply: bool = False,
) -> bool:
    """Apply one topology revision; return True only when files changed."""
    runtime_dir = repo_root / ".aigateway" / "runtime"
    runtime_config = runtime_dir / "config.yaml"
    generated_compose = runtime_dir / "docker-compose.gpu.generated.yml"
    controller_state = runtime_dir / ".gpu-topology-controller.json"
    install_state = repo_root / ".aigateway-install.env"
    if not runtime_config.exists() or not install_state.exists():
        raise RuntimeError("quickstart runtime files are missing")

    config = _read_yaml(runtime_config)
    scheduler = config.get("gpu_scheduler", {})
    if not isinstance(scheduler, dict):
        raise RuntimeError("gpu_scheduler must be an object")
    if respect_auto_apply and scheduler.get("topology_auto_apply", False) is False:
        logger.info("automatic GPU topology apply is disabled")
        return False

    renderer = _load_renderer(repo_root)
    inventory = renderer.discover_devices()
    if not inventory:
        raise RuntimeError("no NVIDIA GPU UUIDs discovered; retaining current topology")
    runtime_inventory = renderer._runtime_inventory(inventory)
    if not runtime_inventory:
        raise RuntimeError("no valid NVIDIA GPU inventory discovered")
    selected = renderer.select_comfyui_devices(inventory, scheduler)
    if not selected:
        raise RuntimeError("configured ComfyUI GPU UUID pool has no available devices")
    fingerprint = _fingerprint(scheduler, inventory)
    compose, workers = renderer.render_topology(
        selected,
        scheduler,
        gateway_devices=inventory,
    )
    _validate_runtime_topology(runtime_inventory, workers)
    desired_compose = yaml.safe_dump(compose, sort_keys=False, allow_unicode=True)
    current_compose = (
        generated_compose.read_text(encoding="utf-8")
        if generated_compose.exists()
        else ""
    )
    current_workers = scheduler.get("workers", [])
    current_devices = scheduler.get("devices", [])
    current_inventory_source = scheduler.get("inventory_source")
    current_inventory_fingerprint = scheduler.get("inventory_fingerprint")
    if (
        current_compose == desired_compose
        and current_workers == workers
        and current_devices == runtime_inventory
        and current_inventory_source == "host_generated"
        and current_inventory_fingerprint == fingerprint
    ):
        if not controller_state.exists():
            # Quickstart already applied this initial topology before it
            # installs the controller; record it without a redundant restart.
            _atomic_text(
                controller_state,
                json.dumps({"fingerprint": fingerprint}) + "\n",
            )
            return False
        try:
            recorded = json.loads(controller_state.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            recorded = {}
        if recorded.get("fingerprint") == fingerprint:
            return False

    descriptor, candidate_name = tempfile.mkstemp(
        prefix=".docker-compose.gpu.candidate.", suffix=".yml", dir=runtime_dir
    )
    os.close(descriptor)
    candidate_path = Path(candidate_name)
    try:
        candidate_path.write_text(desired_compose, encoding="utf-8")
        state = _read_state(install_state)
        validation = subprocess.run(
            [
                *_compose_command(repo_root, install_state, candidate_path, state),
                "config",
                "--quiet",
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if validation.returncode != 0:
            detail = (validation.stderr or validation.stdout).strip()
            raise RuntimeError(f"generated Compose topology is invalid: {detail}")

        with _config_write_lock(runtime_config) as config_handle:
            latest = _read_locked_yaml(config_handle, runtime_config)
            latest_scheduler = latest.get("gpu_scheduler", {})
            if not isinstance(latest_scheduler, dict) or _fingerprint(
                latest_scheduler, inventory
            ) != fingerprint:
                raise RuntimeError(
                    "GPU topology configuration changed during reconcile; retrying"
                )
            candidate_config = dict(latest)
            candidate_scheduler = dict(latest_scheduler)
            candidate_scheduler.update(
                {
                    "inventory_source": "host_generated",
                    "inventory_fingerprint": fingerprint,
                    "devices": runtime_inventory,
                    "workers": workers,
                }
            )
            _validate_runtime_topology(
                candidate_scheduler["devices"], candidate_scheduler["workers"]
            )
            candidate_config["gpu_scheduler"] = candidate_scheduler

            # The controller and container API coordinate through an exclusive
            # flock on the mounted config inode. The sibling lock still
            # serializes multiple host-side controller processes.
            _atomic_text(
                controller_state,
                json.dumps({"pending_fingerprint": fingerprint}) + "\n",
            )
            renderer._atomic_yaml(generated_compose, compose)
            _write_locked_yaml(config_handle, candidate_config)
        if apply:
            services = ["gateway", *compose["services"].keys()]
            services = list(dict.fromkeys(services))
            command = _compose_command(repo_root, install_state, generated_compose, state)
            completed = subprocess.run(
                [
                    *command,
                    "--profile",
                    "comfy-container",
                    "up",
                    "-d",
                    "--remove-orphans",
                    "--force-recreate",
                    *services,
                ],
                cwd=repo_root,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError("docker compose topology apply failed")
        with _config_write_lock(runtime_config) as config_handle:
            applied = _read_locked_yaml(config_handle, runtime_config)
            applied_scheduler = applied.get("gpu_scheduler", {})
            if not isinstance(applied_scheduler, dict) or _fingerprint(
                applied_scheduler, inventory
            ) != fingerprint:
                raise RuntimeError(
                    "GPU topology configuration changed during apply; retrying"
                )
            if (
                applied_scheduler.get("devices") != runtime_inventory
                or applied_scheduler.get("workers") != workers
            ):
                raise RuntimeError(
                    "GPU topology devices/workers changed during apply; retrying"
                )
            _atomic_text(
                controller_state,
                json.dumps({"fingerprint": fingerprint}) + "\n",
            )
        logger.info("GPU topology applied: %d ComfyUI worker(s)", len(workers))
        return True
    finally:
        candidate_path.unlink(missing_ok=True)


def watch(repo_root: Path, *, apply: bool) -> int:
    runtime_dir = repo_root / ".aigateway" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    lock_path = runtime_dir / ".gpu-topology-controller.lock"
    stopping = False

    def _stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    with lock_path.open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while not stopping:
            try:
                reconcile(
                    repo_root,
                    apply=apply,
                    respect_auto_apply=True,
                )
            except Exception as exc:
                logger.error("GPU topology reconcile failed: %s", exc)
            try:
                scheduler = _read_yaml(runtime_dir / "config.yaml").get(
                    "gpu_scheduler", {}
                )
                interval = max(
                    1.0,
                    float(
                        scheduler.get("topology_reconcile_interval_seconds", 10)
                        if isinstance(scheduler, dict)
                        else 10
                    ),
                )
            except (OSError, TypeError, ValueError, yaml.YAMLError):
                interval = 10.0
            deadline = time.monotonic() + interval
            while not stopping and time.monotonic() < deadline:
                time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--no-apply", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    repo_root = args.repo_root.resolve()
    if args.watch:
        return watch(repo_root, apply=not args.no_apply)
    reconcile(repo_root, apply=not args.no_apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
