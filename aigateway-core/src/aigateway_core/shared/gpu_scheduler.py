"""Dynamic single-host GPU leases shared by Gateway and ComfyUI workers."""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import logging
import math
import os
import subprocess
import tempfile
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, BinaryIO, Literal

logger = logging.getLogger(__name__)

DeviceSelector = Literal["auto"] | tuple[str, ...]
ReleaseHook = Callable[[str], Awaitable[bool] | bool]
WorkerReleaseHook = Callable[["ComfyWorker"], Awaitable[bool] | bool]
WorkerProbeHook = Callable[["ComfyWorker"], Awaitable[Mapping[str, Any]]]


class GpuSchedulerConfigError(ValueError):
    """Raised when ``gpu_scheduler`` contains an invalid value."""


class GpuLeaseUnavailableError(RuntimeError):
    """No eligible GPU is currently available and fallback is disabled."""


class GpuQueueTimeoutError(TimeoutError):
    """A generation task could not drain a GPU before its configured deadline."""

    code = "gpu_queue_timeout"


def _positive_number(value: Any, name: str, *, allow_zero: bool = False) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise GpuSchedulerConfigError(f"{name} must be a number") from exc
    if not math.isfinite(parsed):
        raise GpuSchedulerConfigError(f"{name} must be finite")
    invalid = parsed < 0 if allow_zero else parsed <= 0
    if invalid:
        qualifier = "non-negative" if allow_zero else "positive"
        raise GpuSchedulerConfigError(f"{name} must be {qualifier}")
    return parsed


def _device_selector(value: Any, name: str) -> DeviceSelector:
    if value == "auto" or value is None:
        return "auto"
    if not isinstance(value, list) or not value or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise GpuSchedulerConfigError(f"{name} must be 'auto' or a GPU UUID list")
    return tuple(dict.fromkeys(item.strip() for item in value))


@dataclass(frozen=True, slots=True)
class GpuSchedulerConfig:
    enabled: bool = True
    policy: Literal["auto", "manual"] = "auto"
    generation_priority: bool = True
    gateway_devices: DeviceSelector = "auto"
    comfyui_devices: DeviceSelector = "auto"
    gateway_fallback: Literal["cpu", "wait", "fail"] = "cpu"
    generation_wait_timeout_seconds: float = 600.0
    comfyui_idle_reservation_seconds: float = 60.0
    lease_ttl_seconds: float = 15.0
    lease_heartbeat_seconds: float = 5.0
    worker_probe_interval_seconds: float = 10.0
    worker_unhealthy_cooldown_seconds: float = 30.0
    oom_quarantine_seconds: float = 300.0
    max_worker_failover_attempts: int = 1
    device_safety_margin_gb: float = 2.0
    gateway_memory_limit_percent: float | None = None
    device_overrides: tuple[Mapping[str, Any], ...] = ()
    comfyui_dynamic_vram_enabled: bool = False
    topology_auto_apply: bool = False
    topology_reconcile_interval_seconds: float = 10.0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> GpuSchedulerConfig:
        raw = dict(value or {})
        policy = str(raw.get("policy", "auto"))
        fallback = str(raw.get("gateway_fallback", "cpu"))
        if policy not in {"auto", "manual"}:
            raise GpuSchedulerConfigError("policy must be auto or manual")
        if fallback not in {"cpu", "wait", "fail"}:
            raise GpuSchedulerConfigError("gateway_fallback must be cpu, wait, or fail")
        for field_name, default in (
            ("enabled", True),
            ("generation_priority", True),
            ("topology_auto_apply", False),
            ("comfyui_dynamic_vram_enabled", False),
        ):
            if type(raw.get(field_name, default)) is not bool:
                raise GpuSchedulerConfigError(f"{field_name} must be a boolean")
        ttl = _positive_number(raw.get("lease_ttl_seconds", 15), "lease_ttl_seconds")
        heartbeat = _positive_number(
            raw.get("lease_heartbeat_seconds", 5), "lease_heartbeat_seconds"
        )
        if heartbeat >= ttl:
            raise GpuSchedulerConfigError("lease_heartbeat_seconds must be less than lease_ttl_seconds")
        percentage = raw.get("gateway_memory_limit_percent")
        if percentage is not None:
            percentage = _positive_number(percentage, "gateway_memory_limit_percent")
            if percentage > 100:
                raise GpuSchedulerConfigError("gateway_memory_limit_percent cannot exceed 100")
        overrides = raw.get("device_overrides", [])
        if not isinstance(overrides, list) or not all(isinstance(item, dict) for item in overrides):
            raise GpuSchedulerConfigError("device_overrides must be a list of objects")
        for index, override in enumerate(overrides):
            if not isinstance(override.get("uuid"), str) or not override["uuid"].strip():
                raise GpuSchedulerConfigError(
                    f"device_overrides[{index}].uuid must be a GPU UUID"
                )
            if "enabled" in override and type(override["enabled"]) is not bool:
                raise GpuSchedulerConfigError(
                    f"device_overrides[{index}].enabled must be a boolean"
                )
            capabilities = override.get("capabilities")
            if capabilities is not None and (
                not isinstance(capabilities, list)
                or not all(isinstance(item, str) and item for item in capabilities)
            ):
                raise GpuSchedulerConfigError(
                    f"device_overrides[{index}].capabilities must be a string list"
                )
            if "safety_margin_gb" in override:
                _positive_number(
                    override["safety_margin_gb"],
                    f"device_overrides[{index}].safety_margin_gb",
                    allow_zero=True,
                )
        try:
            failovers = int(raw.get("max_worker_failover_attempts", 1))
        except (TypeError, ValueError) as exc:
            raise GpuSchedulerConfigError("max_worker_failover_attempts must be an integer") from exc
        if failovers < 0:
            raise GpuSchedulerConfigError("max_worker_failover_attempts must be non-negative")
        return cls(
            enabled=raw.get("enabled", True),
            policy=policy,  # type: ignore[arg-type]
            generation_priority=raw.get("generation_priority", True),
            gateway_devices=_device_selector(raw.get("gateway_devices", "auto"), "gateway_devices"),
            comfyui_devices=_device_selector(raw.get("comfyui_devices", "auto"), "comfyui_devices"),
            gateway_fallback=fallback,  # type: ignore[arg-type]
            generation_wait_timeout_seconds=_positive_number(
                raw.get("generation_wait_timeout_seconds", 600),
                "generation_wait_timeout_seconds",
            ),
            comfyui_idle_reservation_seconds=_positive_number(
                raw.get("comfyui_idle_reservation_seconds", 60),
                "comfyui_idle_reservation_seconds",
                allow_zero=True,
            ),
            lease_ttl_seconds=ttl,
            lease_heartbeat_seconds=heartbeat,
            worker_probe_interval_seconds=_positive_number(
                raw.get("worker_probe_interval_seconds", 10),
                "worker_probe_interval_seconds",
            ),
            worker_unhealthy_cooldown_seconds=_positive_number(
                raw.get("worker_unhealthy_cooldown_seconds", 30),
                "worker_unhealthy_cooldown_seconds",
                allow_zero=True,
            ),
            oom_quarantine_seconds=_positive_number(
                raw.get("oom_quarantine_seconds", 300),
                "oom_quarantine_seconds",
                allow_zero=True,
            ),
            max_worker_failover_attempts=failovers,
            device_safety_margin_gb=_positive_number(
                raw.get("device_safety_margin_gb", 2),
                "device_safety_margin_gb",
                allow_zero=True,
            ),
            gateway_memory_limit_percent=percentage,
            device_overrides=tuple(dict(item) for item in overrides),
            comfyui_dynamic_vram_enabled=raw.get(
                "comfyui_dynamic_vram_enabled", False
            ),
            topology_auto_apply=raw.get("topology_auto_apply", False),
            topology_reconcile_interval_seconds=_positive_number(
                raw.get("topology_reconcile_interval_seconds", 10),
                "topology_reconcile_interval_seconds",
            ),
        )

    def public_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for key in ("gateway_devices", "comfyui_devices"):
            if isinstance(result[key], tuple):
                result[key] = list(result[key])
        result["device_overrides"] = [dict(item) for item in self.device_overrides]
        return result


@dataclass(slots=True)
class GpuDevice:
    uuid: str
    logical_index: int
    name: str = "GPU"
    total_memory_gb: float = 0.0
    free_memory_gb: float = 0.0
    worker_reserved_memory_gb: float = 0.0
    gateway_leases: set[str] = field(default_factory=set)
    resident_components: set[str] = field(default_factory=set)
    draining: bool = False
    generation_active: int = 0
    reserved_until: float = 0.0
    comfy_resident: bool = False


@dataclass(slots=True)
class ComfyWorker:
    worker_id: str
    device_uuid: str
    server_url: str
    capabilities: frozenset[str] = frozenset({"image", "video", "upscale"})
    healthy: bool = True
    queue_running: int = 0
    queue_pending: int = 0
    unhealthy_until: float = 0.0
    oom_until: float = 0.0
    last_probe_at: float = 0.0


@dataclass(frozen=True, slots=True)
class GatewayLease:
    lease_id: str
    component: str
    requested_device: str
    device: str
    device_uuid: str | None
    logical_index: int | None
    expires_at_monotonic: float | None
    lost_event: asyncio.Event | None = field(default=None, compare=False, repr=False)


def discover_nvidia_devices() -> list[GpuDevice]:
    """Return physical NVIDIA devices using stable UUIDs; no CUDA import required."""
    command = [
        "nvidia-smi",
        "--query-gpu=index,uuid,name,memory.total,memory.free",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    devices: list[GpuDevice] = []
    for line in completed.stdout.splitlines():
        parts = [part.strip() for part in line.split(",", 4)]
        if len(parts) != 5:
            continue
        try:
            index = int(parts[0])
            total = float(parts[3]) / 1024
            free = float(parts[4]) / 1024
        except ValueError:
            continue
        devices.append(GpuDevice(parts[1], index, parts[2], total, free))
    return devices


class GpuResourceCoordinator:
    """Coordinate borrowable Gateway GPU leases and generation reservations."""

    _redis_prefix = "aigateway:gpu_scheduler"

    def __init__(
        self,
        config: GpuSchedulerConfig,
        *,
        devices: Sequence[GpuDevice] = (),
        workers: Sequence[ComfyWorker] = (),
        redis: Any = None,
        metrics_collector: Any = None,
        clock: Callable[[], float] = time.monotonic,
        lock_dir: str | Path | None = None,
    ) -> None:
        self._config = config
        self._devices = {item.uuid: item for item in devices}
        self._workers = {item.worker_id: item for item in workers}
        self._redis = redis
        self._metrics = metrics_collector
        self._clock = clock
        self._condition = asyncio.Condition()
        self._generation_queue: deque[str] = deque()
        self._release_hooks: dict[str, ReleaseHook] = {}
        self._worker_release_hook: WorkerReleaseHook | None = None
        self._worker_probe_hook: WorkerProbeHook | None = None
        self._heartbeat_tasks: dict[str, asyncio.Task[None]] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()
        configured_lock_dir = os.getenv("AI_GATEWAY_GPU_LOCK_DIR", "").strip()
        if lock_dir is not None:
            self._lock_dir = Path(lock_dir)
        elif configured_lock_dir:
            self._lock_dir = Path(configured_lock_dir)
        elif Path("/app/data").is_dir():
            self._lock_dir = Path("/app/data/gpu-locks")
        else:
            self._lock_dir = Path(tempfile.gettempdir()) / "aigateway-gpu-locks"
        self._device_file_locks: dict[str, BinaryIO] = {}
        self._started = False
        self._closed = False

    @property
    def config(self) -> GpuSchedulerConfig:
        return self._config

    def get_worker(self, worker_id: str | None) -> ComfyWorker | None:
        return self._workers.get(worker_id) if worker_id else None

    def record_event(
        self,
        event: str,
        *,
        worker_id: str = "",
        device_uuid: str = "",
    ) -> None:
        """Record a scheduling event without making metrics a correctness dependency."""
        try:
            if self._metrics is not None:
                self._metrics.record_gpu_scheduler_event(
                    event,
                    worker_id=worker_id,
                    device_uuid=device_uuid,
                )
        except Exception as exc:
            logger.debug("GPU scheduler metric update failed: %s", type(exc).__name__)

    def _set_queue_metric(self) -> None:
        try:
            if self._metrics is not None:
                self._metrics.set_gpu_generation_queue_depth(
                    len(self._generation_queue)
                )
        except Exception as exc:
            logger.debug("GPU scheduler queue metric update failed: %s", type(exc).__name__)

    def refresh_inventory(self, devices: Sequence[GpuDevice]) -> None:
        """Refresh mutable telemetry without applying restart-only topology changes."""
        for incoming in devices:
            current = self._devices.get(incoming.uuid)
            if current is None:
                continue
            current.logical_index = incoming.logical_index
            current.name = incoming.name
            current.total_memory_gb = incoming.total_memory_gb
            current.free_memory_gb = incoming.free_memory_gb

    def update_config(self, value: Mapping[str, Any] | GpuSchedulerConfig) -> None:
        """Atomically replace hot parameters; existing leases retain their TTL."""
        self._config = value if isinstance(value, GpuSchedulerConfig) else GpuSchedulerConfig.from_mapping(value)

    def update_hot_config(self, value: Mapping[str, Any] | GpuSchedulerConfig) -> None:
        """Apply runtime-safe fields while retaining restart-only device topology."""
        incoming = value if isinstance(value, GpuSchedulerConfig) else GpuSchedulerConfig.from_mapping(value)
        self._config = replace(
            incoming,
            gateway_devices=self._config.gateway_devices,
            comfyui_devices=self._config.comfyui_devices,
            device_overrides=self._config.device_overrides,
            comfyui_dynamic_vram_enabled=(
                self._config.comfyui_dynamic_vram_enabled
            ),
        )

    def register_release_hook(self, component: str, hook: ReleaseHook) -> None:
        self._release_hooks[component] = hook

    def set_worker_release_hook(self, hook: WorkerReleaseHook) -> None:
        self._worker_release_hook = hook

    def set_worker_probe_hook(self, hook: WorkerProbeHook) -> None:
        self._worker_probe_hook = hook

    def _device_lock_path(self, device_uuid: str) -> Path:
        digest = hashlib.sha256(device_uuid.encode("utf-8")).hexdigest()
        return self._lock_dir / f"{digest}.lock"

    def _try_device_file_lock(
        self,
        device_uuid: str,
        owner_id: str,
        *,
        exclusive: bool,
    ) -> bool:
        if owner_id in self._device_file_locks:
            return True
        try:
            self._lock_dir.mkdir(parents=True, exist_ok=True)
            handle = self._device_lock_path(device_uuid).open("a+b")
            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(handle.fileno(), operation | fcntl.LOCK_NB)
        except OSError as exc:
            try:
                handle.close()
            except (NameError, OSError):
                pass
            logger.debug(
                "GPU device file lock unavailable",
                extra={
                    "device_uuid": device_uuid,
                    "exclusive": exclusive,
                    "error_type": type(exc).__name__,
                },
            )
            return False
        self._device_file_locks[owner_id] = handle
        return True

    def _release_device_file_lock(self, owner_id: str) -> None:
        handle = self._device_file_locks.pop(owner_id, None)
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    async def _probe_worker_once(
        self, worker: ComfyWorker, *, now: float | None = None
    ) -> None:
        if self._worker_probe_hook is None:
            return
        observed_at = self._clock() if now is None else now
        device = self._devices.get(worker.device_uuid)
        try:
            probe = await self._worker_probe_hook(worker)
            worker.healthy = bool(probe.get("healthy", False))
            worker.last_probe_at = observed_at
            worker.unhealthy_until = (
                0.0
                if worker.healthy
                else observed_at + self._config.worker_unhealthy_cooldown_seconds
            )
            worker.queue_running = max(0, int(probe.get("running", 0) or 0))
            worker.queue_pending = max(0, int(probe.get("pending", 0) or 0))
            free_memory_gb = probe.get("free_memory_gb")
            if device is not None and free_memory_gb is not None:
                device.free_memory_gb = float(free_memory_gb)
            worker_reserved_memory_gb = probe.get("worker_reserved_memory_gb")
            if device is not None and worker_reserved_memory_gb is not None:
                device.worker_reserved_memory_gb = max(
                    0.0, float(worker_reserved_memory_gb)
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            worker.healthy = False
            worker.last_probe_at = observed_at
            worker.unhealthy_until = (
                observed_at + self._config.worker_unhealthy_cooldown_seconds
            )
            logger.warning(
                "ComfyUI worker probe failed",
                extra={
                    "worker_id": worker.worker_id,
                    "device_uuid": worker.device_uuid,
                    "error_type": type(exc).__name__,
                },
            )

    async def _probe_all_workers(self) -> None:
        if self._worker_probe_hook is None or not self._workers:
            return
        await asyncio.gather(
            *(self._probe_worker_once(worker) for worker in self._workers.values())
        )
        async with self._condition:
            self._condition.notify_all()

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("GPU coordinator is closed")
        if self._started:
            return
        # Probe before serving requests so a job left running by a previous
        # Gateway process cannot be mistaken for an available GPU.
        await self._probe_all_workers()
        self._started = True
        task = asyncio.create_task(
            self._idle_release_loop(), name="gpu-worker-idle-release"
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def close(self) -> None:
        self._closed = True
        tasks = [*self._heartbeat_tasks.values(), *self._background_tasks]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._heartbeat_tasks.clear()
        self._background_tasks.clear()
        if self._device_file_locks:
            logger.warning(
                "GPU coordinator closed with active file-fenced work; "
                "locks remain held until their owners finish or the process exits",
                extra={"active_lock_count": len(self._device_file_locks)},
            )
        self._started = False

    def _allowed(self, device: GpuDevice, selector: DeviceSelector) -> bool:
        if selector != "auto" and device.uuid not in selector:
            return False
        for override in self._config.device_overrides:
            if override.get("uuid") == device.uuid and override.get("enabled") is False:
                return False
        return True

    def _device_override(self, device_uuid: str) -> Mapping[str, Any]:
        return next(
            (
                override
                for override in self._config.device_overrides
                if override.get("uuid") == device_uuid
            ),
            {},
        )

    def _worker_blocks_gateway(self, device_uuid: str, now: float) -> bool:
        workers = [
            worker
            for worker in self._workers.values()
            if worker.device_uuid == device_uuid
        ]
        return any(
            not worker.healthy
            or worker.unhealthy_until > now
            or worker.queue_running > 0
            or worker.queue_pending > 0
            for worker in workers
        )

    def _gateway_candidates(self, requested: str) -> list[GpuDevice]:
        now = self._clock()
        devices = [
            item for item in self._devices.values()
            if self._allowed(item, self._config.gateway_devices)
            and not item.draining
            and item.generation_active == 0
            and item.reserved_until <= now
            and not item.comfy_resident
            and not self._worker_blocks_gateway(item.uuid, now)
        ]
        if requested.startswith("cuda:"):
            try:
                index = int(requested.split(":", 1)[1])
            except ValueError as exc:
                raise GpuLeaseUnavailableError(f"invalid CUDA device: {requested}") from exc
            devices = [item for item in devices if item.logical_index == index]
        return sorted(devices, key=lambda item: (-item.free_memory_gb, item.logical_index))

    async def _redis_claim_gateway(
        self, lease_id: str, device_uuid: str, ttl: float
    ) -> bool:
        if self._redis is None:
            return True
        script = """
        if redis.call('exists', KEYS[1]) == 1 then return 0 end
        redis.call('set', KEYS[2], ARGV[1], 'EX', ARGV[2])
        redis.call('sadd', KEYS[3], ARGV[1])
        redis.call('expire', KEYS[3], ARGV[2])
        return 1
        """
        drain = f"{self._redis_prefix}:drain:{device_uuid}"
        lease = f"{self._redis_prefix}:lease:{lease_id}"
        leases = f"{self._redis_prefix}:leases:{device_uuid}"
        try:
            result = await self._redis.eval(
                script,
                3,
                drain,
                lease,
                leases,
                lease_id,
                max(1, math.ceil(ttl)),
            )
        except Exception as exc:
            logger.warning(
                "GPU Redis lease unavailable; failing closed for GPU claim: %s",
                type(exc).__name__,
            )
            return False
        return bool(result)

    def _cancel_lease_owner(
        self,
        owner_task: asyncio.Task[Any] | None,
        *,
        event: str,
        device_uuid: str,
        lost_event: asyncio.Event | None = None,
    ) -> None:
        if lost_event is not None:
            lost_event.set()
        self.record_event(event, device_uuid=device_uuid)
        if (
            owner_task is not None
            and owner_task is not asyncio.current_task()
            and not owner_task.done()
        ):
            owner_task.cancel()

    async def _heartbeat(
        self,
        lease_id: str,
        device_uuid: str,
        ttl: float,
        interval: float,
        lost_event: asyncio.Event,
        owner_task: asyncio.Task[Any] | None,
    ) -> None:
        key = f"{self._redis_prefix}:lease:{lease_id}"
        membership_key = f"{self._redis_prefix}:leases:{device_uuid}"
        redis_ttl = max(1, math.ceil(ttl))
        script = """
        if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
        redis.call('expire', KEYS[1], ARGV[2])
        redis.call('sadd', KEYS[2], ARGV[1])
        redis.call('expire', KEYS[2], ARGV[2])
        return 1
        """
        # Stop the owner before the last successful Redis lease can expire. A
        # transient outage may consume one heartbeat, but never an entire TTL.
        fence_at = self._clock() + max(interval, ttl - interval)
        while True:
            try:
                await asyncio.sleep(interval)
                if self._redis is None:
                    continue
                renewed = await self._redis.eval(
                    script,
                    2,
                    key,
                    membership_key,
                    lease_id,
                    redis_ttl,
                )
                if renewed is False or renewed == 0:
                    self._cancel_lease_owner(
                        owner_task,
                        event="gateway_lease_lost",
                        device_uuid=device_uuid,
                        lost_event=lost_event,
                    )
                    return
                fence_at = self._clock() + max(interval, ttl - interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "GPU lease heartbeat retrying after Redis error: %s",
                    type(exc).__name__,
                )
                if self._clock() >= fence_at:
                    self._cancel_lease_owner(
                        owner_task,
                        event="gateway_lease_lost",
                        device_uuid=device_uuid,
                        lost_event=lost_event,
                    )
                    return

    @contextlib.asynccontextmanager
    async def gateway_lease(
        self,
        component: str,
        requested_device: str = "auto",
    ) -> AsyncIterator[GatewayLease]:
        """Acquire an async Gateway lease for ``auto|cpu|cuda|cuda:N``."""
        if requested_device == "cpu":
            yield GatewayLease(
                uuid.uuid4().hex,
                component,
                requested_device,
                "cpu",
                None,
                None,
                None,
            )
            return
        if not self._config.enabled:
            # Disabled means legacy device handling, not forced CPU fallback.
            yield GatewayLease(
                uuid.uuid4().hex,
                component,
                requested_device,
                requested_device,
                None,
                None,
                None,
            )
            return
        if requested_device not in {"auto", "cuda"} and not requested_device.startswith("cuda:"):
            raise GpuLeaseUnavailableError(f"unsupported device selector: {requested_device}")
        strict = requested_device != "auto" or self._config.gateway_fallback == "fail"
        lease: GatewayLease | None = None
        cpu_fallback: GatewayLease | None = None
        while lease is None:
            config = self._config
            async with self._condition:
                for device in self._gateway_candidates(requested_device):
                    lease_id = uuid.uuid4().hex
                    if not self._try_device_file_lock(
                        device.uuid, lease_id, exclusive=False
                    ):
                        continue
                    if not await self._redis_claim_gateway(
                        lease_id, device.uuid, config.lease_ttl_seconds
                    ):
                        self._release_device_file_lock(lease_id)
                        continue
                    device.gateway_leases.add(lease_id)
                    device.resident_components.add(component)
                    lost_event = asyncio.Event()
                    owner_task = asyncio.current_task()
                    lease = GatewayLease(
                        lease_id,
                        component,
                        requested_device,
                        f"cuda:{device.logical_index}",
                        device.uuid,
                        device.logical_index,
                        self._clock() + config.lease_ttl_seconds,
                        lost_event,
                    )
                    task = asyncio.create_task(
                        self._heartbeat(
                            lease_id,
                            device.uuid,
                            config.lease_ttl_seconds,
                            config.lease_heartbeat_seconds,
                            lost_event,
                            owner_task,
                        ),
                        name=f"gpu-lease-heartbeat-{lease_id}",
                    )
                    self._heartbeat_tasks[lease_id] = task
                    self.record_event(
                        "gateway_borrow",
                        device_uuid=device.uuid,
                    )
                    break
                if lease is None:
                    if requested_device == "auto" and config.gateway_fallback == "cpu":
                        cpu_fallback = GatewayLease(
                            uuid.uuid4().hex,
                            component,
                            requested_device,
                            "cpu",
                            None,
                            None,
                            None,
                        )
                    elif strict:
                        raise GpuLeaseUnavailableError("no eligible GPU is available")
                    else:
                        await self._condition.wait()
            if cpu_fallback is not None:
                yield cpu_fallback
                return
        try:
            yield lease
        finally:
            await self._release_gateway_lease(lease)

    async def _release_gateway_lease(self, lease: GatewayLease) -> None:
        if lease.device_uuid is None:
            return
        task = self._heartbeat_tasks.pop(lease.lease_id, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self._redis is not None:
            script = """
            if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
            redis.call('del', KEYS[1])
            redis.call('srem', KEYS[2], ARGV[1])
            return 1
            """
            try:
                await self._redis.eval(
                    script,
                    2,
                    f"{self._redis_prefix}:lease:{lease.lease_id}",
                    f"{self._redis_prefix}:leases:{lease.device_uuid}",
                    lease.lease_id,
                )
            except Exception as exc:
                logger.warning(
                    "GPU Redis lease cleanup failed: %s", type(exc).__name__
                )
        self._release_device_file_lock(lease.lease_id)
        async with self._condition:
            device = self._devices.get(lease.device_uuid)
            if device is not None:
                device.gateway_leases.discard(lease.lease_id)
            self._condition.notify_all()

    def _worker_candidates(
        self,
        capability: str,
        memory_requirement_gb: float,
        excluded_workers: set[str],
        preferred_worker_id: str | None = None,
    ) -> list[tuple[float, ComfyWorker, GpuDevice]]:
        now = self._clock()
        candidates: list[tuple[float, ComfyWorker, GpuDevice]] = []
        for worker in self._workers.values():
            device = self._devices.get(worker.device_uuid)
            if device is None or worker.worker_id in excluded_workers:
                continue
            if not self._allowed(device, self._config.comfyui_devices):
                continue
            override = self._device_override(device.uuid)
            override_capabilities = override.get("capabilities")
            capabilities = (
                frozenset(str(item) for item in override_capabilities)
                if isinstance(override_capabilities, list)
                else worker.capabilities
            )
            if capability not in capabilities:
                continue
            if not worker.healthy or worker.unhealthy_until > now or worker.oom_until > now:
                continue
            margin = override.get(
                "safety_margin_gb", self._config.device_safety_margin_gb
            )
            try:
                margin_gb = max(0.0, float(margin))
            except (TypeError, ValueError):
                margin_gb = self._config.device_safety_margin_gb
            # ComfyUI's torch allocator keeps model weights reserved after a
            # prompt finishes. That memory is not reported as device-free, but
            # it is reusable by the same worker for its next workflow. Treating
            # only raw free VRAM as capacity makes a successfully loaded model
            # permanently disqualify its worker from subsequent generations.
            reported_capacity = (
                device.free_memory_gb + device.worker_reserved_memory_gb
            )
            reusable_capacity = (
                min(device.total_memory_gb, reported_capacity)
                if device.total_memory_gb > 0
                else reported_capacity
            )
            usable = reusable_capacity - margin_gb
            if usable < memory_requirement_gb:
                continue
            score = usable - memory_requirement_gb - worker.queue_running * 4 - worker.queue_pending * 2
            if worker.worker_id == preferred_worker_id:
                score += 1_000_000
            candidates.append((score, worker, device))
        return sorted(candidates, key=lambda item: (-item[0], item[1].worker_id))

    async def _release_gateway_residents(
        self, device: GpuDevice, deadline: float
    ) -> None:
        # Hooks own their busy counters and may report an already-empty model.
        # Calling every registered hook also covers legacy call sites that have
        # not yet attached a component label to their GPU lease.
        while True:
            all_idle = True
            for component, hook in tuple(self._release_hooks.items()):
                result = hook(device.uuid)
                released = await result if isinstance(result, Awaitable) else result
                if released:
                    device.resident_components.discard(component)
                    self.record_event(
                        "model_eviction",
                        device_uuid=device.uuid,
                    )
                else:
                    all_idle = False
            if all_idle:
                return
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise GpuQueueTimeoutError(
                    f"Gateway models did not become idle within "
                    f"{self._config.generation_wait_timeout_seconds:g}s"
                )
            await asyncio.sleep(min(0.1, remaining))

    async def _redis_claim_generation(
        self, device_uuid: str, ticket: str, ttl_seconds: float
    ) -> int:
        """Acquire/renew a generation drain.

        Returns 0 when another generation owns the device, 1 when the drain is
        owned and no live Gateway leases remain, and 2 while existing leases are
        still draining. The drain is installed before checking leases so new
        Gateway claims cannot continuously starve generation.
        """
        device = self._devices.get(device_uuid)
        if self._redis is None:
            return 2 if device is not None and device.gateway_leases else 1
        script = """
        local owner = redis.call('get', KEYS[2])
        if owner and owner ~= ARGV[2] then return 0 end
        redis.call('set', KEYS[2], ARGV[2], 'EX', ARGV[3])
        local active = 0
        local lease_ids = redis.call('smembers', KEYS[1])
        for _, lease_id in ipairs(lease_ids) do
          if redis.call('exists', ARGV[1] .. lease_id) == 1 then
            active = 1
          else
            redis.call('srem', KEYS[1], lease_id)
          end
        end
        if active == 1 then return 2 end
        return 1
        """
        leases = f"{self._redis_prefix}:leases:{device_uuid}"
        drain = f"{self._redis_prefix}:drain:{device_uuid}"
        try:
            result = await self._redis.eval(
                script,
                2,
                leases,
                drain,
                f"{self._redis_prefix}:lease:",
                ticket,
                max(1, int(ttl_seconds)),
            )
        except Exception as exc:
            logger.warning(
                "GPU Redis generation claim failed closed: %s",
                type(exc).__name__,
            )
            return 0
        try:
            return int(result or 0)
        except (TypeError, ValueError):
            return 0

    async def _redis_release_generation_claim(
        self, device_uuid: str, ticket: str
    ) -> bool:
        if self._redis is None:
            return True
        key = f"{self._redis_prefix}:drain:{device_uuid}"
        script = """
        if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
        redis.call('del', KEYS[1])
        return 1
        """
        try:
            return bool(await self._redis.eval(script, 1, key, ticket))
        except Exception as exc:
            logger.warning(
                "GPU Redis generation release failed: %s", type(exc).__name__
            )
            return False

    async def _generation_heartbeat(
        self,
        device_uuid: str,
        ticket: str,
        ttl: float,
        interval: float,
        owner_task: asyncio.Task[Any] | None,
    ) -> None:
        key = f"{self._redis_prefix}:drain:{device_uuid}"
        redis_ttl = max(1, math.ceil(ttl))
        script = """
        if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
        return redis.call('expire', KEYS[1], ARGV[2])
        """
        fence_at = self._clock() + max(interval, ttl - interval)
        while True:
            try:
                await asyncio.sleep(interval)
                if self._redis is None:
                    continue
                renewed = await self._redis.eval(
                    script, 1, key, ticket, redis_ttl
                )
                if renewed is False or renewed == 0:
                    self._cancel_lease_owner(
                        owner_task,
                        event="generation_lease_lost",
                        device_uuid=device_uuid,
                    )
                    return
                fence_at = self._clock() + max(interval, ttl - interval)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "GPU generation heartbeat retrying after Redis error: %s",
                    type(exc).__name__,
                )
                if self._clock() >= fence_at:
                    self._cancel_lease_owner(
                        owner_task,
                        event="generation_lease_lost",
                        device_uuid=device_uuid,
                    )
                    return

    async def _redis_reserve_after_generation(
        self, device_uuid: str, ticket: str, seconds: float
    ) -> bool:
        if self._redis is None:
            return True
        key = f"{self._redis_prefix}:drain:{device_uuid}"
        if seconds <= 0:
            return await self._redis_release_generation_claim(device_uuid, ticket)
        script = """
        if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
        redis.call('set', KEYS[1], 'comfyui_idle', 'EX', ARGV[2])
        return 1
        """
        try:
            return bool(
                await self._redis.eval(
                    script, 1, key, ticket, max(1, int(seconds))
                )
            )
        except Exception as exc:
            logger.warning(
                "GPU Redis idle reservation failed: %s", type(exc).__name__
            )
            return False

    async def _redis_claim_idle_release(
        self, device_uuid: str
    ) -> tuple[str, bool] | None:
        token = f"comfyui_release:{uuid.uuid4().hex}"
        if self._redis is None:
            return (
                (token, False)
                if self._try_device_file_lock(
                    device_uuid, token, exclusive=True
                )
                else None
            )
        key = f"{self._redis_prefix}:drain:{device_uuid}"
        ttl = max(30, math.ceil(self._config.lease_ttl_seconds * 2))
        script = """
        local owner = redis.call('get', KEYS[1])
        if owner and owner ~= 'comfyui_idle' then return {0, 0} end
        local restore_idle = owner == 'comfyui_idle' and 1 or 0
        redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[2])
        return {1, restore_idle}
        """
        try:
            result = await self._redis.eval(script, 1, key, token, ttl)
            claimed = bool(result and int(result[0]))
            restore_idle = bool(result and int(result[1]))
        except Exception as exc:
            logger.warning(
                "GPU Redis idle release claim failed: %s",
                type(exc).__name__,
            )
            return None
        if not claimed:
            return None
        if self._try_device_file_lock(device_uuid, token, exclusive=True):
            return token, restore_idle
        await self._redis_finish_idle_release(
            device_uuid,
            token,
            released=False,
            restore_idle=restore_idle,
        )
        return None

    async def _redis_finish_idle_release(
        self,
        device_uuid: str,
        token: str,
        *,
        released: bool,
        restore_idle: bool,
    ) -> bool:
        if self._redis is None:
            self._release_device_file_lock(token)
            return True
        key = f"{self._redis_prefix}:drain:{device_uuid}"
        retry_ttl = max(1, math.ceil(self._config.worker_probe_interval_seconds))
        script = """
        if redis.call('get', KEYS[1]) ~= ARGV[1] then return 0 end
        if ARGV[2] == '1' or ARGV[3] == '0' then
          redis.call('del', KEYS[1])
        else
          redis.call('set', KEYS[1], 'comfyui_idle', 'EX', ARGV[4])
        end
        return 1
        """
        try:
            return bool(
                await self._redis.eval(
                    script,
                    1,
                    key,
                    token,
                    "1" if released else "0",
                    "1" if restore_idle else "0",
                    retry_ttl,
                )
            )
        except Exception as exc:
            logger.warning(
                "GPU Redis idle release finalization failed: %s",
                type(exc).__name__,
            )
            return False
        finally:
            self._release_device_file_lock(token)

    @contextlib.asynccontextmanager
    async def generation_lease(
        self,
        capability: str,
        *,
        memory_requirement_gb: float = 0.0,
        excluded_workers: set[str] | None = None,
        preferred_worker_id: str | None = None,
    ) -> AsyncIterator[ComfyWorker]:
        """FIFO generation claim that drains Gateway leases before yielding a worker."""
        excluded = excluded_workers or set()
        ticket = uuid.uuid4().hex
        deadline = self._clock() + self._config.generation_wait_timeout_seconds
        queued_at = self._clock()
        self._generation_queue.append(ticket)
        self._set_queue_metric()
        selected: tuple[ComfyWorker, GpuDevice] | None = None
        draining_device: GpuDevice | None = None
        generation_started = False
        generation_heartbeat: asyncio.Task[None] | None = None
        owner_task = asyncio.current_task()
        try:
            async with self._condition:
                while selected is None:
                    config = self._config
                    candidates = self._worker_candidates(
                        capability,
                        memory_requirement_gb,
                        excluded,
                        preferred_worker_id,
                    )
                    if self._generation_queue and self._generation_queue[0] == ticket:
                        pending: tuple[ComfyWorker, GpuDevice] | None = None
                        claim_ttl = max(
                            config.lease_ttl_seconds,
                            config.lease_heartbeat_seconds * 3,
                        )
                        for _, worker, device in sorted(
                            candidates,
                            key=lambda item: (
                                bool(item[2].gateway_leases),
                                -item[0],
                                item[1].worker_id,
                            ),
                        ):
                            claim = await self._redis_claim_generation(
                                device.uuid, ticket, claim_ttl
                            )
                            if (
                                claim == 1
                                and not device.gateway_leases
                                and self._try_device_file_lock(
                                    device.uuid, ticket, exclusive=True
                                )
                            ):
                                selected = (worker, device)
                                break
                            if claim in {1, 2}:
                                if pending is None:
                                    pending = (worker, device)
                                else:
                                    await self._redis_release_generation_claim(
                                        device.uuid, ticket
                                    )

                        if selected is not None:
                            _, selected_device = selected
                            if (
                                pending is not None
                                and pending[1].uuid != selected_device.uuid
                            ):
                                await self._redis_release_generation_claim(
                                    pending[1].uuid, ticket
                                )
                                pending[1].draining = False
                            if (
                                draining_device is not None
                                and draining_device.uuid != selected_device.uuid
                            ):
                                await self._redis_release_generation_claim(
                                    draining_device.uuid, ticket
                                )
                                draining_device.draining = False
                            draining_device = selected_device
                            selected_device.draining = config.generation_priority
                            self._generation_queue.popleft()
                            self._set_queue_metric()
                            break

                        if pending is not None:
                            _, pending_device = pending
                            if (
                                draining_device is not None
                                and draining_device.uuid != pending_device.uuid
                            ):
                                await self._redis_release_generation_claim(
                                    draining_device.uuid, ticket
                                )
                                draining_device.draining = False
                            draining_device = pending_device
                            pending_device.draining = config.generation_priority

                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise GpuQueueTimeoutError(
                            "generation did not acquire a GPU within "
                            f"{self._config.generation_wait_timeout_seconds:g}s"
                        )
                    try:
                        await asyncio.wait_for(
                            self._condition.wait(),
                            timeout=min(
                                remaining,
                                self._config.lease_heartbeat_seconds,
                            ),
                        )
                    except TimeoutError as exc:
                        if self._clock() >= deadline:
                            raise GpuQueueTimeoutError(
                                "generation did not acquire a GPU within "
                                f"{self._config.generation_wait_timeout_seconds:g}s"
                            ) from exc

            worker, device = selected
            if self._redis is not None:
                generation_heartbeat = asyncio.create_task(
                    self._generation_heartbeat(
                        device.uuid,
                        ticket,
                        max(
                            self._config.lease_ttl_seconds,
                            self._config.lease_heartbeat_seconds * 3,
                        ),
                        self._config.lease_heartbeat_seconds,
                        owner_task,
                    ),
                    name=f"gpu-generation-heartbeat-{ticket}",
                )
            await self._release_gateway_residents(device, deadline)
            async with self._condition:
                device.generation_active += 1
                worker.queue_running += 1
                generation_started = True
            logger.info(
                "GPU generation worker allocated",
                extra={"worker_id": worker.worker_id, "device_uuid": device.uuid},
            )
            try:
                if self._metrics is not None:
                    self._metrics.record_gpu_generation_wait(
                        self._clock() - queued_at
                    )
            except Exception as exc:
                logger.debug(
                    "GPU generation wait metric update failed: %s",
                    type(exc).__name__,
                )
            self.record_event(
                "worker_allocation",
                worker_id=worker.worker_id,
                device_uuid=device.uuid,
            )
            yield worker
        finally:
            if generation_heartbeat is not None:
                generation_heartbeat.cancel()
                await asyncio.gather(generation_heartbeat, return_exceptions=True)
            queue_changed = False
            if ticket in self._generation_queue:
                self._generation_queue.remove(ticket)
                self._set_queue_metric()
                queue_changed = True
            if not generation_started and draining_device is not None:
                draining_device.draining = False
                await self._redis_release_generation_claim(
                    draining_device.uuid, ticket
                )
                self._release_device_file_lock(ticket)
                queue_changed = True
            if selected is not None and generation_started:
                worker, device = selected
                async with self._condition:
                    worker.queue_running = max(0, worker.queue_running - 1)
                    device.generation_active = max(0, device.generation_active - 1)
                    device.draining = False
                    device.reserved_until = (
                        self._clock()
                        + self._config.comfyui_idle_reservation_seconds
                    )
                    device.comfy_resident = True
                    self._condition.notify_all()
                try:
                    await self._redis_reserve_after_generation(
                        device.uuid,
                        ticket,
                        self._config.comfyui_idle_reservation_seconds,
                    )
                finally:
                    self._release_device_file_lock(ticket)
            elif queue_changed:
                async with self._condition:
                    self._condition.notify_all()

    async def quarantine_oom(self, worker_id: str) -> None:
        worker = self._workers.get(worker_id)
        if worker is None:
            return
        worker.oom_until = self._clock() + self._config.oom_quarantine_seconds
        self.record_event(
            "oom_quarantine",
            worker_id=worker.worker_id,
            device_uuid=worker.device_uuid,
        )
        async with self._condition:
            self._condition.notify_all()

    async def mark_worker_health(self, worker_id: str, healthy: bool) -> None:
        worker = self._workers.get(worker_id)
        if worker is None:
            return
        worker.healthy = healthy
        worker.last_probe_at = self._clock()
        worker.unhealthy_until = 0.0 if healthy else self._clock() + self._config.worker_unhealthy_cooldown_seconds
        async with self._condition:
            self._condition.notify_all()

    async def release_idle_workers_now(self) -> dict[str, bool]:
        """Explicitly unload every idle worker without racing another owner."""
        results: dict[str, bool] = {}
        if self._worker_release_hook is None:
            return results
        for worker in tuple(self._workers.values()):
            device = self._devices.get(worker.device_uuid)
            if (
                device is None
                or device.generation_active
                or worker.queue_running
                or worker.queue_pending
            ):
                results[worker.worker_id] = False
                continue
            release_claim = await self._redis_claim_idle_release(device.uuid)
            if release_claim is None:
                results[worker.worker_id] = False
                continue
            release_token, restore_idle = release_claim
            released = False
            try:
                hook_result = self._worker_release_hook(worker)
                released = bool(
                    await hook_result
                    if isinstance(hook_result, Awaitable)
                    else hook_result
                )
            except asyncio.CancelledError:
                await self._redis_finish_idle_release(
                    device.uuid,
                    release_token,
                    released=False,
                    restore_idle=restore_idle,
                )
                raise
            except Exception as exc:
                logger.warning(
                    "ComfyUI worker release failed",
                    extra={
                        "worker_id": worker.worker_id,
                        "device_uuid": worker.device_uuid,
                        "error_type": type(exc).__name__,
                    },
                )
            finalized = await self._redis_finish_idle_release(
                device.uuid,
                release_token,
                released=released,
                restore_idle=restore_idle,
            )
            results[worker.worker_id] = released and finalized
            if released:
                device.reserved_until = 0.0
                device.comfy_resident = False
                self.record_event(
                    "model_eviction",
                    worker_id=worker.worker_id,
                    device_uuid=device.uuid,
                )
        async with self._condition:
            self._condition.notify_all()
        return results

    async def _idle_release_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._config.worker_probe_interval_seconds)
                now = self._clock()
                for worker in tuple(self._workers.values()):
                    device = self._devices.get(worker.device_uuid)
                    release_token: str | None = None
                    try:
                        await self._probe_worker_once(worker, now=now)
                        if (
                            device is None
                            or device.generation_active
                            or worker.queue_running
                            or worker.queue_pending
                            or device.reserved_until <= 0
                            or device.reserved_until > now
                            or self._worker_release_hook is None
                        ):
                            continue
                        release_claim = await self._redis_claim_idle_release(
                            device.uuid
                        )
                        if release_claim is None:
                            continue
                        release_token, restore_idle = release_claim
                        hook_result = self._worker_release_hook(worker)
                        released = bool(
                            await hook_result
                            if isinstance(hook_result, Awaitable)
                            else hook_result
                        )
                        finalized = await self._redis_finish_idle_release(
                            device.uuid,
                            release_token,
                            released=released,
                            restore_idle=restore_idle,
                        )
                        if not released:
                            continue
                        device.reserved_until = 0.0
                        device.comfy_resident = False
                        self.record_event(
                            "model_eviction",
                            worker_id=worker.worker_id,
                            device_uuid=device.uuid,
                        )
                        if not finalized:
                            logger.warning(
                                "ComfyUI worker released after Redis ownership changed",
                                extra={
                                    "worker_id": worker.worker_id,
                                    "device_uuid": device.uuid,
                                },
                            )
                        async with self._condition:
                            self._condition.notify_all()
                    except asyncio.CancelledError:
                        if release_token is not None and device is not None:
                            await self._redis_finish_idle_release(
                                device.uuid,
                                release_token,
                                released=False,
                                restore_idle=restore_idle,
                            )
                        raise
                    except Exception as exc:
                        if release_token is not None and device is not None:
                            await self._redis_finish_idle_release(
                                device.uuid,
                                release_token,
                                released=False,
                                restore_idle=restore_idle,
                            )
                        worker.healthy = False
                        worker.unhealthy_until = (
                            now + self._config.worker_unhealthy_cooldown_seconds
                        )
                        logger.warning(
                            "ComfyUI worker maintenance failed",
                            extra={
                                "worker_id": worker.worker_id,
                                "device_uuid": worker.device_uuid,
                                "error_type": type(exc).__name__,
                            },
                        )
        except asyncio.CancelledError:  # noqa: TRY203 - preserve task cancellation explicitly
            raise

    def status(self) -> dict[str, Any]:
        now = self._clock()
        worker_by_device = {worker.device_uuid: worker for worker in self._workers.values()}
        devices = []
        for device in sorted(self._devices.values(), key=lambda item: item.logical_index):
            worker = worker_by_device.get(device.uuid)
            if device.generation_active:
                state = "generation_active"
            elif device.draining:
                state = "draining"
            elif device.reserved_until > now:
                state = "comfyui_idle_reserved"
            elif device.comfy_resident:
                state = "comfyui_release_pending"
            elif device.gateway_leases:
                state = "gateway_borrowed"
            else:
                state = "available"
            devices.append({
                "uuid": device.uuid,
                "logical_index": device.logical_index,
                "name": device.name,
                "total_memory_gb": round(device.total_memory_gb, 3),
                "free_memory_gb": round(device.free_memory_gb, 3),
                "worker_reserved_memory_gb": round(
                    device.worker_reserved_memory_gb, 3
                ),
                "state": state,
                "gateway_leases": len(device.gateway_leases),
                "resident_components": sorted(device.resident_components),
                "worker_id": worker.worker_id if worker else None,
                "queue": {
                    "running": worker.queue_running if worker else 0,
                    "pending": worker.queue_pending if worker else 0,
                },
                "cooldown_remaining_seconds": round(max(0.0, device.reserved_until - now), 3),
                "oom_quarantine_remaining_seconds": round(
                    max(0.0, (worker.oom_until if worker else 0.0) - now), 3
                ),
            })
        return {
            "enabled": self._config.enabled,
            "policy": self._config.policy,
            "generation_priority": self._config.generation_priority,
            "generation_queue_depth": len(self._generation_queue),
            "devices": devices,
            "workers": [
                {
                    "worker_id": worker.worker_id,
                    "device_uuid": worker.device_uuid,
                    "server_url": worker.server_url,
                    "capabilities": sorted(worker.capabilities),
                    "healthy": worker.healthy and worker.unhealthy_until <= now,
                    "queue_running": worker.queue_running,
                    "queue_pending": worker.queue_pending,
                    "unhealthy_cooldown_remaining_seconds": round(max(0.0, worker.unhealthy_until - now), 3),
                    "oom_quarantine_remaining_seconds": round(max(0.0, worker.oom_until - now), 3),
                }
                for worker in sorted(self._workers.values(), key=lambda item: item.worker_id)
            ],
        }


def workers_from_config(config: Mapping[str, Any], devices: Sequence[GpuDevice]) -> list[ComfyWorker]:
    """Parse explicit pool workers without assigning remote endpoints locally.

    An enabled scheduler may only receive workers whose stable ``device_uuid``
    mapping was generated explicitly. A fixed ComfyUI URL is not evidence that
    the endpoint owns a local GPU; synthesizing such a mapping would let remote
    queue and memory telemetry drain or lock local Gateway devices.

    The historical single-URL compatibility path remains available only when
    the scheduler is absent or explicitly disabled.
    """
    scheduler = config.get("gpu_scheduler", {})
    raw_workers = scheduler.get("workers", []) if isinstance(scheduler, Mapping) else []
    workers: list[ComfyWorker] = []
    if isinstance(raw_workers, list):
        for index, raw in enumerate(raw_workers):
            if not isinstance(raw, Mapping):
                continue
            device_uuid = str(raw.get("device_uuid") or "")
            server_url = str(raw.get("server_url") or "").rstrip("/")
            if not device_uuid or not server_url:
                continue
            capabilities = raw.get("capabilities", ["image", "video", "upscale"])
            if not isinstance(capabilities, list):
                capabilities = ["image", "video", "upscale"]
            workers.append(ComfyWorker(
                worker_id=str(raw.get("worker_id") or f"comfyui-{index}"),
                device_uuid=device_uuid,
                server_url=server_url,
                capabilities=frozenset(str(item) for item in capabilities if item),
            ))

    scheduler_enabled = (
        isinstance(scheduler, Mapping) and scheduler.get("enabled") is True
    )
    if workers or not devices or scheduler_enabled:
        return workers

    generation = config.get("generation_optimization", {})
    draft = generation.get("draft_workflow", {}) if isinstance(generation, Mapping) else {}
    comfy = draft.get("comfyui", {}) if isinstance(draft, Mapping) else {}
    server_url = str(comfy.get("server_url") or "").rstrip("/") if isinstance(comfy, Mapping) else ""
    if server_url:
        workers.append(ComfyWorker("comfyui-0", devices[0].uuid, server_url))
    return workers


__all__ = [
    "ComfyWorker",
    "GatewayLease",
    "GpuDevice",
    "GpuLeaseUnavailableError",
    "GpuQueueTimeoutError",
    "GpuResourceCoordinator",
    "GpuSchedulerConfig",
    "GpuSchedulerConfigError",
    "discover_nvidia_devices",
    "workers_from_config",
]
