"""Dynamic single-host GPU leases shared by Gateway and ComfyUI workers."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

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
    generation_wait_timeout_seconds: float = 120.0
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
    topology_auto_apply: bool = True
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
        if type(raw.get("topology_auto_apply", True)) is not bool:
            raise GpuSchedulerConfigError("topology_auto_apply must be a boolean")
        if type(raw.get("comfyui_dynamic_vram_enabled", False)) is not bool:
            raise GpuSchedulerConfigError(
                "comfyui_dynamic_vram_enabled must be a boolean"
            )
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
            enabled=bool(raw.get("enabled", True)),
            policy=policy,  # type: ignore[arg-type]
            generation_priority=bool(raw.get("generation_priority", True)),
            gateway_devices=_device_selector(raw.get("gateway_devices", "auto"), "gateway_devices"),
            comfyui_devices=_device_selector(raw.get("comfyui_devices", "auto"), "comfyui_devices"),
            gateway_fallback=fallback,  # type: ignore[arg-type]
            generation_wait_timeout_seconds=_positive_number(
                raw.get("generation_wait_timeout_seconds", 120),
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
            topology_auto_apply=raw.get("topology_auto_apply", True),
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

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("GPU coordinator is closed")
        task = asyncio.create_task(self._idle_release_loop(), name="gpu-worker-idle-release")
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

    def _gateway_candidates(self, requested: str) -> list[GpuDevice]:
        now = self._clock()
        devices = [
            item for item in self._devices.values()
            if self._allowed(item, self._config.gateway_devices)
            and not item.draining
            and item.generation_active == 0
            and item.reserved_until <= now
            and not item.comfy_resident
        ]
        if requested.startswith("cuda:"):
            try:
                index = int(requested.split(":", 1)[1])
            except ValueError as exc:
                raise GpuLeaseUnavailableError(f"invalid CUDA device: {requested}") from exc
            devices = [item for item in devices if item.logical_index == index]
        return sorted(devices, key=lambda item: (-item.free_memory_gb, item.logical_index))

    async def _redis_claim_gateway(self, lease_id: str, device_uuid: str, ttl: float) -> bool:
        if self._redis is None:
            return True
        script = """
        if redis.call('exists', KEYS[1]) == 1 then return 0 end
        redis.call('set', KEYS[2], ARGV[1], 'EX', ARGV[2])
        redis.call('sadd', KEYS[3], ARGV[3])
        redis.call('expire', KEYS[3], ARGV[2])
        return 1
        """
        drain = f"{self._redis_prefix}:drain:{device_uuid}"
        lease = f"{self._redis_prefix}:lease:{lease_id}"
        leases = f"{self._redis_prefix}:leases:{device_uuid}"
        try:
            result = await self._redis.eval(script, 3, drain, lease, leases, device_uuid, max(1, int(ttl)), lease_id)
        except Exception as exc:
            logger.warning("GPU Redis lease unavailable; failing closed for GPU claim: %s", type(exc).__name__)
            return False
        return bool(result)

    async def _heartbeat(self, lease_id: str, device_uuid: str, ttl: float, interval: float) -> None:
        key = f"{self._redis_prefix}:lease:{lease_id}"
        try:
            while True:
                await asyncio.sleep(interval)
                if self._redis is not None:
                    await self._redis.expire(key, max(1, int(ttl)))
                    await self._redis.expire(
                        f"{self._redis_prefix}:leases:{device_uuid}",
                        max(1, int(ttl)),
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("GPU lease heartbeat stopped: %s", type(exc).__name__)

    @contextlib.asynccontextmanager
    async def gateway_lease(
        self,
        component: str,
        requested_device: str = "auto",
    ) -> AsyncIterator[GatewayLease]:
        """Acquire an async Gateway lease for ``auto|cpu|cuda|cuda:N``."""
        if requested_device == "cpu" or not self._config.enabled:
            yield GatewayLease(uuid.uuid4().hex, component, requested_device, "cpu", None, None, None)
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
                    if not await self._redis_claim_gateway(lease_id, device.uuid, config.lease_ttl_seconds):
                        continue
                    device.gateway_leases.add(lease_id)
                    device.resident_components.add(component)
                    lease = GatewayLease(
                        lease_id,
                        component,
                        requested_device,
                        f"cuda:{device.logical_index}",
                        device.uuid,
                        device.logical_index,
                        self._clock() + config.lease_ttl_seconds,
                    )
                    task = asyncio.create_task(
                        self._heartbeat(
                            lease_id,
                            device.uuid,
                            config.lease_ttl_seconds,
                            config.lease_heartbeat_seconds,
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
            try:
                await self._redis.delete(f"{self._redis_prefix}:lease:{lease.lease_id}")
                await self._redis.srem(f"{self._redis_prefix}:leases:{lease.device_uuid}", lease.lease_id)
            except Exception as exc:
                logger.warning("GPU Redis lease cleanup failed: %s", type(exc).__name__)
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
        self, device_uuid: str, ticket: str, timeout_seconds: float
    ) -> bool:
        if self._redis is None:
            return True
        script = """
        local lease_ids = redis.call('smembers', KEYS[1])
        for _, lease_id in ipairs(lease_ids) do
          if redis.call('exists', ARGV[1] .. lease_id) == 1 then return 0 end
          redis.call('srem', KEYS[1], lease_id)
        end
        local owner = redis.call('get', KEYS[2])
        if owner and owner ~= ARGV[2] then return 0 end
        redis.call('set', KEYS[2], ARGV[2], 'EX', ARGV[3])
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
                max(1, int(timeout_seconds + self._config.lease_ttl_seconds)),
            )
        except Exception as exc:
            logger.warning("GPU Redis generation claim failed closed: %s", type(exc).__name__)
            return False
        return bool(result)

    async def _redis_reserve_after_generation(
        self, device_uuid: str, seconds: float
    ) -> None:
        if self._redis is None:
            return
        key = f"{self._redis_prefix}:drain:{device_uuid}"
        try:
            if seconds <= 0:
                await self._redis.delete(key)
            else:
                await self._redis.set(key, "comfyui_idle", ex=max(1, int(seconds)))
        except Exception as exc:
            logger.warning("GPU Redis idle reservation failed: %s", type(exc).__name__)

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
        try:
            async with self._condition:
                while selected is None:
                    candidates = self._worker_candidates(
                        capability,
                        memory_requirement_gb,
                        excluded,
                        preferred_worker_id,
                    )
                    if self._generation_queue and self._generation_queue[0] == ticket and candidates:
                        _, worker, device = candidates[0]
                        device.draining = self._config.generation_priority
                        draining_device = device
                        if (
                            not device.gateway_leases
                            and await self._redis_claim_generation(
                                device.uuid, ticket, max(0.0, deadline - self._clock())
                            )
                        ):
                            selected = (worker, device)
                            self._generation_queue.popleft()
                            self._set_queue_metric()
                            break
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise GpuQueueTimeoutError(
                            f"generation did not acquire a GPU within {self._config.generation_wait_timeout_seconds:g}s"
                        )
                    try:
                        await asyncio.wait_for(
                            self._condition.wait(),
                            timeout=min(remaining, self._config.lease_heartbeat_seconds),
                        )
                    except TimeoutError as exc:
                        if self._clock() >= deadline:
                            raise GpuQueueTimeoutError(
                                f"generation did not acquire a GPU within {self._config.generation_wait_timeout_seconds:g}s"
                            ) from exc
            worker, device = selected
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
            if ticket in self._generation_queue:
                self._generation_queue.remove(ticket)
                self._set_queue_metric()
            if (selected is None or not generation_started) and draining_device is not None:
                draining_device.draining = False
                if self._redis is not None:
                    try:
                        drain_key = f"{self._redis_prefix}:drain:{draining_device.uuid}"
                        owner = await self._redis.get(drain_key)
                        if owner in {ticket, ticket.encode()}:
                            await self._redis.delete(drain_key)
                    except Exception:
                        pass
            if selected is not None and generation_started:
                worker, device = selected
                async with self._condition:
                    worker.queue_running = max(0, worker.queue_running - 1)
                    device.generation_active = max(0, device.generation_active - 1)
                    device.draining = False
                    device.reserved_until = self._clock() + self._config.comfyui_idle_reservation_seconds
                    device.comfy_resident = True
                    self._condition.notify_all()
                await self._redis_reserve_after_generation(
                    device.uuid, self._config.comfyui_idle_reservation_seconds
                )

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
        """Explicitly unload every idle worker and reopen successfully freed devices."""
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
            hook_result = self._worker_release_hook(worker)
            released = await hook_result if isinstance(hook_result, Awaitable) else hook_result
            results[worker.worker_id] = bool(released)
            if released:
                device.reserved_until = 0.0
                device.comfy_resident = False
                self.record_event(
                    "model_eviction",
                    worker_id=worker.worker_id,
                    device_uuid=device.uuid,
                )
                if self._redis is not None:
                    try:
                        await self._redis.delete(
                            f"{self._redis_prefix}:drain:{device.uuid}"
                        )
                    except Exception:
                        pass
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
                    if self._worker_probe_hook is not None:
                        try:
                            probe = await self._worker_probe_hook(worker)
                            worker.healthy = bool(probe.get("healthy", False))
                            worker.last_probe_at = now
                            if worker.healthy:
                                worker.unhealthy_until = 0.0
                            else:
                                worker.unhealthy_until = (
                                    now
                                    + self._config.worker_unhealthy_cooldown_seconds
                                )
                            worker.queue_running = int(probe.get("running", 0) or 0)
                            worker.queue_pending = int(probe.get("pending", 0) or 0)
                            free_memory_gb = probe.get("free_memory_gb")
                            if device is not None and free_memory_gb is not None:
                                device.free_memory_gb = float(free_memory_gb)
                            worker_reserved_memory_gb = probe.get(
                                "worker_reserved_memory_gb"
                            )
                            if (
                                device is not None
                                and worker_reserved_memory_gb is not None
                            ):
                                device.worker_reserved_memory_gb = max(
                                    0.0, float(worker_reserved_memory_gb)
                                )
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            worker.healthy = False
                            worker.unhealthy_until = (
                                now + self._config.worker_unhealthy_cooldown_seconds
                            )
                            logger.warning(
                                "ComfyUI worker probe failed",
                                extra={
                                    "worker_id": worker.worker_id,
                                    "device_uuid": worker.device_uuid,
                                    "error_type": type(exc).__name__,
                                },
                            )
                    if (
                        device is None
                        or device.generation_active
                        or device.reserved_until <= 0
                        or device.reserved_until > now
                        or self._worker_release_hook is None
                    ):
                        continue
                    hook_result = self._worker_release_hook(worker)
                    released = await hook_result if isinstance(hook_result, Awaitable) else hook_result
                    if released:
                        device.reserved_until = 0.0
                        device.comfy_resident = False
                        self.record_event(
                            "model_eviction",
                            worker_id=worker.worker_id,
                            device_uuid=device.uuid,
                        )
                        if self._redis is not None:
                            try:
                                await self._redis.delete(
                                    f"{self._redis_prefix}:drain:{device.uuid}"
                                )
                            except Exception as exc:
                                logger.warning(
                                    "GPU Redis idle release cleanup failed: %s",
                                    type(exc).__name__,
                                )
                        async with self._condition:
                            self._condition.notify_all()
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
    """Parse generated worker topology, retaining the single-URL compatibility path."""
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
                capabilities=frozenset(str(item) for item in capabilities),
            ))
    if workers or not devices:
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
