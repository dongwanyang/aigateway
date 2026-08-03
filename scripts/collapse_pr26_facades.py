"""Collapse temporary PR #26 compatibility facades into canonical modules."""
from __future__ import annotations

import re
from pathlib import Path


def require_replace(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"anchor not found: {label}")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# GPU scheduler: preserve the canonical module and tighten topology loading.
# ---------------------------------------------------------------------------
gpu_impl = Path("aigateway-core/src/aigateway_core/shared/_gpu_scheduler_impl.py")
gpu_public = Path("aigateway-core/src/aigateway_core/shared/gpu_scheduler.py")
gpu_text = gpu_impl.read_text(encoding="utf-8")
start = gpu_text.index("def workers_from_config(")
end = gpu_text.index("\n\n\n__all__ = [", start)
new_workers = '''def workers_from_config(config: Mapping[str, Any], devices: Sequence[GpuDevice]) -> list[ComfyWorker]:
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
    return workers'''
gpu_text = gpu_text[:start] + new_workers + gpu_text[end:]
gpu_public.write_text(gpu_text, encoding="utf-8")
gpu_impl.unlink()


# ---------------------------------------------------------------------------
# Model router: keep the canonical class and make pricing units explicit.
# ---------------------------------------------------------------------------
router_impl = Path("aigateway-core/src/aigateway_core/route/model_resolution/_model_router_impl.py")
router_public = Path("aigateway-core/src/aigateway_core/route/model_resolution/model_router.py")
router_text = router_impl.read_text(encoding="utf-8")
pricing_helpers = '''logger = logging.getLogger(__name__)

# Static routing cannot know final usage before selecting a model. Convert the
# configured USD/token rates to one stable representative USD/request estimate.
# Actual billing continues to use measured prompt/completion tokens.
_ROUTING_ESTIMATED_PROMPT_TOKENS = 1_000
_ROUTING_ESTIMATED_COMPLETION_TOKENS = 500


def _non_negative_rate(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result > 0 else 0.0


def estimate_routing_request_cost(pricing: Any) -> float:
    """Convert one model's USD/token pricing to representative USD/request."""
    if not isinstance(pricing, dict):
        return 0.0
    return (
        _non_negative_rate(pricing.get("prompt", 0.0))
        * _ROUTING_ESTIMATED_PROMPT_TOKENS
        + _non_negative_rate(pricing.get("completion", 0.0))
        * _ROUTING_ESTIMATED_COMPLETION_TOKENS
    )'''
router_text = require_replace(
    router_text,
    "logger = logging.getLogger(__name__)",
    pricing_helpers,
    "model-router logger",
)
old_price = '''                    # 获取价格: 使用 pricing 中的 prompt 价格作为 price_per_request
                    price = 0.0
                    model_pricing = group_pricing.get(model_name, {})
                    if isinstance(model_pricing, dict):
                        prompt_price = model_pricing.get("prompt", 0.0)
                        try:
                            price = float(prompt_price)
                        except (TypeError, ValueError):
                            price = 0.0
'''
new_price = '''                    # Route comparisons and exposed estimates use
                    # USD/request; provider configuration remains USD/token.
                    price = estimate_routing_request_cost(
                        group_pricing.get(model_name, {})
                    )
'''
router_text = require_replace(router_text, old_price, new_price, "model-router price")
router_public.write_text(router_text, encoding="utf-8")
router_impl.unlink()


# ---------------------------------------------------------------------------
# Plugin registry: serialize first construction in the canonical class.
# ---------------------------------------------------------------------------
registry_impl = Path("aigateway-core/src/aigateway_core/shared/_plugin_registry_impl.py")
registry_public = Path("aigateway-core/src/aigateway_core/shared/plugin_registry.py")
registry_text = registry_impl.read_text(encoding="utf-8")
registry_text = require_replace(
    registry_text,
    "        return self._registrations.get(name)\n\n    def _get_or_create_instance",
    "        with self._lock:\n            return self._registrations.get(name)\n\n    def _get_or_create_instance",
    "plugin-registry get lock",
)
method_start = registry_text.index("    def _get_or_create_instance(")
method_end = registry_text.index("\n    def get_all(", method_start)
new_method = '''    def _get_or_create_instance(self, reg: PluginRegistration) -> Any | None:
        """Return the single runtime instance for the live registration.

        Constructors may allocate model memory, threads, sockets or file handles.
        Serialize first construction with register/unregister so concurrent
        health and engine queries cannot create duplicate heavyweight instances,
        and an obsolete registration cannot publish into a later same-name one.
        """
        with self._lock:
            if self._registrations.get(reg.name) is not reg:
                return None
            cached = self._instances.get(reg.name)
            if cached is not None:
                return cached
            try:
                instance = reg.plugin_class(**reg.config)
            except TypeError as exc:
                logger.warning(
                    "插件 '%s' 实例化失败（配置参数不匹配）: %s",
                    reg.name,
                    exc,
                )
                return None
            self._instances[reg.name] = instance
            return instance
'''
registry_text = registry_text[:method_start] + new_method + registry_text[method_end:]
registry_public.write_text(registry_text, encoding="utf-8")
registry_impl.unlink()
