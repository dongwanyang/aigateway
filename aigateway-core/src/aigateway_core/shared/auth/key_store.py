"""API Key storage and quota management — KeyStore.

Moved from ``aigateway_core.security`` as part of the 总分总 runtime split
(Task 3). Manages API Key creation, validation, revocation, and quota
tracking. Data persists in Redis (Hash + String); rate limiting uses SortedSet.

Per DB_SCHEMA.md:
- §1 API Key storage (Hash)
- §2 Quota counting (Hash)
- §5 Rate-limit windows (SortedSet + String)
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import string
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from aigateway_core.shared.exceptions import AuthError

logger = logging.getLogger(__name__)


class KeyStore:
    """API Key storage and quota manager.

    All persisted data is stored in Redis, accessed via
    redis_client.RedisClientManager. Pub/Sub events sync Key changes
    across instances.

    Attributes:
        redis: Redis client manager instance.
    """

    # Constants — DB_SCHEMA-defined Key prefixes
    KEY_NAMESPACE = "aigateway:key:"
    KEY_LOOKUP_PREFIX = "aigateway:key_lookup:"
    QUOTA_PREFIX = "aigateway:quota:"
    RATELIMIT_RPM_PREFIX = "aigateway:ratelimit:"
    RATELIMIT_TPM_SUFFIX = ":tpm"
    PUBSUB_CHANNEL = "aigateway:keys:sync"
    CONFIG_RELOAD_CHANNEL = "aigateway:config:reload"

    # Default quota values (TECH_SPEC.md config.yaml defaults)
    DEFAULT_DAILY_TOKENS = 1_000_000
    DEFAULT_MONTHLY_COST = 50.0
    DEFAULT_RATE_LIMIT_RPM = 60
    DEFAULT_RATE_LIMIT_TPM = 100_000

    def __init__(self, redis) -> None:  # type: ignore[reportMissingTypeArgument]
        """
        Args:
            redis: RedisClientManager instance, already connected.
        """
        self.redis = redis

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_key(key_value: str) -> str:
        """Compute SHA-256 hash of API Key (first 16 hex chars).

        DB_SCHEMA §1: key_hash = SHA-256(key)[:16]
        """
        return hashlib.sha256(key_value.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _prefix_key(key_value: str) -> str:
        """Extract first 8 chars of Key for display."""
        return key_value[:8]

    @staticmethod
    def _now_iso() -> str:
        """Return current UTC time as ISO 8601 string."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _now_unix() -> int:
        """Return current Unix timestamp (seconds)."""
        return int(datetime.now(timezone.utc).timestamp())

    def _build_pubsub_message(
        self, event_type: str, key_id: str, user_id: str, **extra: Any
    ) -> Dict[str, Any]:
        """Build Pub/Sub sync message body.

        DB_SCHEMA §4: aigateway:keys:sync channel message format
        """
        msg: Dict[str, Any] = {
            "event_type": event_type,
            "key_id": key_id,
            "user_id": user_id,
            "timestamp": self._now_iso(),
        }
        msg.update(extra)
        return msg

    # ------------------------------------------------------------------
    # Core operations: validate / create / revoke
    # ------------------------------------------------------------------

    async def validate(self, key: str) -> Optional[Dict[str, Any]]:
        """Validate API Key.

        Looks up and validates status and quota from Redis
        `aigateway:key:{key_hash}`. If Redis has no API Keys at all
        (possibly cleared), auto-reseeds from config.

        Args:
            key: full API Key value.

        Returns:
            Key metadata dict, containing key_id, user_id, status, etc.
            None if invalid.

        Raises:
            AuthError: when Key has been revoked.
        """
        key_hash = self._hash_key(key)
        data = await self.redis.get_api_key(key_hash)

        if data is None:
            # Key not found — check if Redis was cleared and needs reseed
            if await self._try_auto_reseed():
                # Retry after reseed
                data = await self.redis.get_api_key(key_hash)

        if data is None:
            logger.warning("API Key hash=%s 未找到", key_hash)
            return None

        status = data.get("status", "")
        if status == "revoked":
            raise AuthError(f"API key '{data.get('key_id')}' has been revoked")
        if status == "suspended":
            raise AuthError(f"API key '{data.get('key_id')}' is suspended")

        # Normalize is_admin field (Redis stores as string)
        if "is_admin" in data:
            data["is_admin"] = data["is_admin"] in ("True", "true", "1", True)

        # Update last_used_at
        data["last_used_at"] = self._now_iso()
        # Ensure all values are str before writing back to Redis (bool breaks hset)
        serializable_data = {}
        for k, v in data.items():
            if isinstance(v, bool):
                serializable_data[k] = str(v)
            elif isinstance(v, (str, int, float, type(None))):
                serializable_data[k] = v
            else:
                serializable_data[k] = str(v)
        await self.redis.set_api_key(key_hash, serializable_data)

        return data

    async def _try_auto_reseed(self) -> bool:
        """Check if Redis has any API Key; if not, auto-reseed from config.

        Returns:
            True if reseed was performed, False otherwise.
        """
        if self.redis is None or self.redis.redis is None:
            return False

        try:
            cursor, keys = await self.redis.redis.scan(0, match="aigateway:key:*", count=5)
            if keys:
                return False  # Redis has keys, no need to reseed

            # Redis is empty — try to reseed from config
            # Import config_manager from app state (circular-import-safe)
            try:
                from aigateway_api.main import app
                config_manager = getattr(app.state, "config_manager", None)
                if config_manager:
                    auth_config = config_manager.get("auth", {})
                    keys_config = auth_config.get("api_keys", [])
                    if keys_config:
                        seeded = await self.seed_from_config(keys_config)
                        logger.info("API Keys re-seeded from config.yaml: %d keys imported", seeded)
                        return seeded > 0
            except Exception as exc:
                logger.warning("Auto-reseed failed: %s", exc)

        except Exception as exc:
            logger.warning("Auto-reseed check failed: %s", exc)

        return False

    async def create(
        self,
        user_id: str,
        quotas: Optional[Dict[str, Any]] = None,
        group_id: str = "",
        cache_scope: str = "group",
    ) -> Dict[str, Any]:
        """Create a new API Key and store in Redis.

        DB_SCHEMA §1: writes aigateway:key:{key_hash} Hash + aigateway:key_lookup Hash

        Args:
            user_id: associated user ID.
            quotas: quota config {daily_tokens, monthly_cost, rate_limit_rpm, rate_limit_tpm}.
            group_id: group this key belongs to.
            cache_scope: default cache scope (private/group/public).

        Returns:
            Dict with full key value and related info.
        """
        if not user_id:
            raise ValueError("user_id is required")

        # Generate unique Key value (gw- + 32 chars alphanumeric)
        _ALPHABET = string.ascii_letters + string.digits  # a-zA-Z0-9
        raw_key = f"gw-{''.join(secrets.choice(_ALPHABET) for _ in range(32))}"

        key_hash = self._hash_key(raw_key)
        key_prefix = self._prefix_key(raw_key)
        now_iso = self._now_iso()

        # Merge default quotas
        q = quotas or {}
        daily_tokens = q.get("daily_tokens", self.DEFAULT_DAILY_TOKENS)
        monthly_cost = q.get("monthly_cost", self.DEFAULT_MONTHLY_COST)
        rate_rpm = q.get("rate_limit_rpm", self.DEFAULT_RATE_LIMIT_RPM)
        rate_tpm = q.get("rate_limit_tpm", self.DEFAULT_RATE_LIMIT_TPM)

        key_id = f"key_{uuid.uuid4().hex[:8]}"

        # Check if user_id already has an active Key
        await self._check_duplicate_user_key(user_id)

        # Write Key Hash
        key_data: Dict[str, str] = {
            "key_id": key_id,
            "key_prefix": key_prefix,
            "user_id": user_id,
            "status": "active",
            "created_at": now_iso,
            "last_used_at": "",
            "group_id": group_id or "",
            "cache_scope": cache_scope or "group",
            "daily_tokens_limit": str(daily_tokens),
            "daily_tokens_used": "0",
            "monthly_cost_limit": str(monthly_cost),
            "monthly_cost_used": "0.0",
            "rate_limit_rpm": str(rate_rpm),
            "rate_limit_tpm": str(rate_tpm),
            "rpm_window_start": str(self._now_unix()),
            "rpm_window_count": "0",
            "tpm_window_start": str(self._now_unix()),
            "tpm_window_count": "0",
        }

        # Initialize daily and monthly quota records (DB_SCHEMA §2)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = datetime.now(timezone.utc).strftime("%Y-%m")

        quota_base = {
            "tokens_in": "0",
            "tokens_out": "0",
            "cost_usd": "0.0",
            "request_count": "0",
            "model_usage": "{}",
        }

        # Atomic batch: key hash + lookup + daily quota + monthly quota + member
        def _build(pipe):
            ops = [
                pipe.hset(f"aigateway:key:{key_hash}", mapping=key_data),
                pipe.set(f"aigateway:key_lookup:{key_prefix}", key_hash),
                pipe.hset(f"aigateway:quota:{key_hash}:daily:{today}", mapping=quota_base),
                pipe.hset(f"aigateway:quota:{key_hash}:monthly:{month}", mapping=quota_base),
            ]
            if group_id:
                ops.append(pipe.sadd(f"aigateway:group:{group_id}:members", key_hash))
            return ops

        await self.redis.pipe_batch(lambda pipe: _build(pipe))

        # Broadcast Key creation event via Pub/Sub (after batch so the key
        # record is guaranteed to exist when subscribers process the event)
        pub_msg = self._build_pubsub_message("key_created", key_id, user_id)
        await self.redis.publish(self.PUBSUB_CHANNEL, pub_msg)

        logger.info("API Key 创建成功: user_id=%s, key_id=%s, key_hash=%s", user_id, key_id, key_hash)

        return {
            "id": key_id,
            "key": raw_key,
            "key_prefix": key_prefix,
            "user_id": user_id,
            "group_id": group_id or "",
            "cache_scope": cache_scope or "group",
            "created_at": now_iso,
            "status": "active",
            "quotas": {
                "daily_tokens": daily_tokens,
                "monthly_cost": monthly_cost,
                "rate_limit_rpm": rate_rpm,
                "rate_limit_tpm": rate_tpm,
            },
        }

    async def seed_from_config(self, keys_config: List[Dict[str, Any]]) -> int:
        """Import API Keys from config.yaml auth.api_keys into Redis.

        Called at startup to ensure config-file keys are written to Redis.
        Existing keys get quota and is_admin flags updated.

        Args:
            keys_config: config api_keys list, each with key/user_id/quotas/is_admin.

        Returns:
            Number of successfully imported keys.
        """
        if not keys_config:
            return 0

        imported = 0
        now_iso = self._now_iso()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = datetime.now(timezone.utc).strftime("%Y-%m")

        quota_base = {
            "tokens_in": "0",
            "tokens_out": "0",
            "cost_usd": "0.0",
            "request_count": "0",
            "model_usage": "{}",
        }

        for cfg in keys_config:
            raw_key = cfg.get("key", "")
            user_id = cfg.get("user_id", "")
            if not raw_key or not user_id:
                logger.warning("config api_keys 条目缺少 key 或 user_id，跳过: %s", cfg)
                continue

            key_hash = self._hash_key(raw_key)
            key_prefix = raw_key[:8]
            quotas = cfg.get("quotas", {})
            is_admin = bool(cfg.get("is_admin", False))
            cfg_group = cfg.get("group") or ""

            # Check if already exists
            existing = await self.redis.get_api_key(key_hash)
            if existing:
                # Only update structural fields (is_admin/status/user_id), preserve runtime-modified quotas
                existing["user_id"] = user_id
                existing["status"] = "active"
                existing["is_admin"] = str(is_admin)
                if cfg_group:
                    existing["group_id"] = cfg_group
                if "group_id" not in existing:
                    existing["group_id"] = ""
                if "cache_scope" not in existing:
                    existing["cache_scope"] = "group"
                # Quota limits: only write from config if missing in Redis (don't overwrite API-modified values)
                if "daily_tokens_limit" not in existing:
                    existing["daily_tokens_limit"] = str(quotas.get("daily_tokens", self.DEFAULT_DAILY_TOKENS))
                if "monthly_cost_limit" not in existing:
                    existing["monthly_cost_limit"] = str(quotas.get("monthly_cost", self.DEFAULT_MONTHLY_COST))
                if "rate_limit_rpm" not in existing:
                    existing["rate_limit_rpm"] = str(quotas.get("rate_limit_rpm", self.DEFAULT_RATE_LIMIT_RPM))
                if "rate_limit_tpm" not in existing:
                    existing["rate_limit_tpm"] = str(quotas.get("rate_limit_tpm", self.DEFAULT_RATE_LIMIT_TPM))
                await self.redis.set_api_key(key_hash, existing)
                logger.info("API Key 已更新: user_id=%s, key_hash=%s", user_id, key_hash)
            else:
                key_id = f"key_{uuid.uuid4().hex[:8]}"
                key_data: Dict[str, str] = {
                    "key_id": key_id,
                    "key_prefix": key_prefix,
                    "user_id": user_id,
                    "status": "active",
                    "created_at": now_iso,
                    "last_used_at": "",
                    "group_id": cfg_group,
                    "cache_scope": "group",
                    "daily_tokens_limit": str(quotas.get("daily_tokens", self.DEFAULT_DAILY_TOKENS)),
                    "daily_tokens_used": "0",
                    "monthly_cost_limit": str(quotas.get("monthly_cost", self.DEFAULT_MONTHLY_COST)),
                    "monthly_cost_used": "0.0",
                    "rate_limit_rpm": str(quotas.get("rate_limit_rpm", self.DEFAULT_RATE_LIMIT_RPM)),
                    "rate_limit_tpm": str(quotas.get("rate_limit_tpm", self.DEFAULT_RATE_LIMIT_TPM)),
                    "rpm_window_start": str(self._now_unix()),
                    "rpm_window_count": "0",
                    "tpm_window_start": str(self._now_unix()),
                    "tpm_window_count": "0",
                    "is_admin": str(is_admin),
                }
                # Atomic batch: key hash + lookup + daily quota + monthly quota + member
                def _build(pipe):
                    ops = [
                        pipe.hset(f"aigateway:key:{key_hash}", mapping=key_data),
                        pipe.set(f"aigateway:key_lookup:{key_prefix}", key_hash),
                        pipe.hset(f"aigateway:quota:{key_hash}:daily:{today}", mapping=quota_base),
                        pipe.hset(f"aigateway:quota:{key_hash}:monthly:{month}", mapping=quota_base),
                    ]
                    if cfg_group:
                        ops.append(pipe.sadd(f"aigateway:group:{cfg_group}:members", key_hash))
                    return ops

                await self.redis.pipe_batch(lambda pipe: _build(pipe))
                logger.info("API Key 已创建: user_id=%s, key_hash=%s, is_admin=%s", user_id, key_hash, is_admin)

            imported += 1

        return imported

    async def revoke(self, key_id: str) -> bool:
        """Revoke the specified API Key.

        DB_SCHEMA §1: sets status to "revoked", deletes key_prefix reverse lookup.
        Broadcasts Pub/Sub event for all Gateway instances to sync.

        Args:
            key_id: API Key internal ID, e.g. "key_abc123".

        Returns:
            Whether revocation succeeded.
        """
        if not key_id.startswith("key_"):
            raise ValueError("Invalid key_id format, should be key_xxx")

        # Scan all key_hash to find matching key_id
        key_hashes = await self._find_key_hashes_by_id(key_id)

        if not key_hashes:
            logger.warning("未找到 key_id=%s 对应的 Key 记录", key_id)
            return False

        user_id = ""
        key_prefix = ""

        for kh in key_hashes:
            data = await self.redis.get_api_key(kh)
            if not data:
                continue
            data["status"] = "revoked"
            data["last_used_at"] = self._now_iso()
            await self.redis.set_api_key(kh, data)
            user_id = data.get("user_id", "")
            key_prefix = data.get("key_prefix", "")

        # Delete reverse lookup
        if key_prefix:
            lookup_key = f"{self.KEY_LOOKUP_PREFIX}{key_prefix}"
            if self.redis.redis is not None:
                await self.redis.redis.delete(lookup_key)

        # Broadcast revocation event
        pub_msg = self._build_pubsub_message("key_revoked", key_id, user_id)
        await self.redis.publish(self.PUBSUB_CHANNEL, pub_msg)

        logger.info("API Key 已撤销: key_id=%s", key_id)
        return True

    async def delete_permanently(self, key_id: str) -> bool:
        """Permanently delete all data for the specified API Key from Redis.

        Unlike revoke, this completely removes the key record with no trace.

        Args:
            key_id: API Key internal ID, e.g. "key_abc123".

        Returns:
            Whether deletion succeeded.
        """
        if not key_id.startswith("key_"):
            raise ValueError("Invalid key_id format, should be key_xxx")

        key_hashes = await self._find_key_hashes_by_id(key_id)
        if not key_hashes:
            logger.warning("未找到 key_id=%s 对应的 Key 记录", key_id)
            return False

        for kh in key_hashes:
            data = await self.redis.get_api_key(kh)
            if not data:
                continue
            key_prefix = data.get("key_prefix", "")
            # Delete key main record
            await self.redis.delete_api_key(kh, key_prefix)
            logger.info("API Key 已永久删除: key_id=%s, key_hash=%s", key_id, kh)

        # Broadcast deletion event
        pub_msg = self._build_pubsub_message("key_deleted", key_id, "")
        await self.redis.publish(self.PUBSUB_CHANNEL, pub_msg)

        return True

    async def ensure_seeded(self, keys_config: List[Dict[str, Any]]) -> int:
        """Check if Redis has any API Key; if empty, reseed.

        Used for auto-recovery after Redis is cleared.

        Args:
            keys_config: config.yaml auth.api_keys config list.

        Returns:
            Number of re-imported keys (0 means no reseed needed).
        """
        if self.redis is None or self.redis.redis is None:
            return 0

        # Check if any key exists
        cursor, keys = await self.redis.redis.scan(0, match="aigateway:key:*", count=10)
        if keys:
            return 0  # Keys exist, no reseed needed

        # Redis is empty, reseed
        logger.info("API Keys re-seeded from config.yaml")
        return await self.seed_from_config(keys_config)

    # ------------------------------------------------------------------
    # Quota check and accumulation
    # ------------------------------------------------------------------

    @staticmethod
    def _check_dims(
        data: Dict[str, Any],
        tokens: int,
        cost: float,
        now_unix: int,
    ) -> Tuple[bool, Optional[str], int, Dict[str, str]]:
        """Check RPM/TPM/daily/monthly against a data dict.

        Returns (passed, reason, retry_after, resets) where ``resets`` holds
        window fields to write back when a window expired (caller persists).
        """
        resets: Dict[str, str] = {}

        rpm_limit = int(data.get("rate_limit_rpm", KeyStore.DEFAULT_RATE_LIMIT_RPM))
        rpm_window_start = int(data.get("rpm_window_start", "0"))
        rpm_window_count = int(data.get("rpm_window_count", "0"))

        if now_unix - rpm_window_start >= 60:
            resets["rpm_window_start"] = str(now_unix)
            resets["rpm_window_count"] = "0"
            rpm_window_count = 0
            rpm_window_start = now_unix
        elif rpm_window_count >= rpm_limit:
            return (False, f"RPM limit exceeded: {rpm_window_count}/{rpm_limit}",
                    rpm_window_start + 60 - now_unix, resets)

        tpm_limit = int(data.get("rate_limit_tpm", KeyStore.DEFAULT_RATE_LIMIT_TPM))
        tpm_window_start = int(data.get("tpm_window_start", "0"))
        tpm_window_count = int(data.get("tpm_window_count", "0"))

        if now_unix - tpm_window_start >= 60:
            resets["tpm_window_start"] = str(now_unix)
            resets["tpm_window_count"] = "0"
            tpm_window_count = 0
            tpm_window_start = now_unix
        elif tpm_window_count + tokens > tpm_limit:
            return (False, f"TPM limit exceeded: {tpm_window_count + tokens}/{tpm_limit}",
                    tpm_window_start + 60 - now_unix, resets)

        daily_limit = int(data.get("daily_tokens_limit", KeyStore.DEFAULT_DAILY_TOKENS))
        daily_used = int(data.get("daily_tokens_used", "0"))
        if daily_used + tokens > daily_limit:
            return (False, f"Daily token limit exceeded: {daily_used}/{daily_limit}", 0, resets)

        monthly_limit = float(data.get("monthly_cost_limit", KeyStore.DEFAULT_MONTHLY_COST))
        monthly_used = float(data.get("monthly_cost_used", "0.0"))
        if monthly_used + cost > monthly_limit:
            return (False, f"Monthly cost limit exceeded: ${monthly_used:.2f}/${monthly_limit:.2f}", 0, resets)

        return (True, None, 0, resets)

    async def check_quota(
        self,
        key_hash: str,
        tokens: int,
        cost: float,
    ) -> Tuple[bool, Optional[str], int]:
        """Check group-level (if group_id set) then key-level quotas.

        DB_SCHEMA §2 quota counting + §5 rate-limit windows.

        Group is checked first; if the group has a record, all four dimensions
        (RPM/TPM/daily/monthly) are validated against it.  Then the key's own
        dimensions are checked.

        Args:
            key_hash: API Key SHA-256 hash first 16 chars.
            tokens: estimated token consumption for this request.
            cost: estimated cost (USD) for this request.

        Returns:
            (passed, failure_reason, retry_after). On pass failure=None.
        """
        data = await self.redis.get_api_key(key_hash)
        if not data:
            return False, "API Key does not exist", 0

        now_unix = self._now_unix()
        group_id = data.get("group_id") or ""

        # ---- Group-level check (first) ----
        if group_id:
            gdata = await self.redis.get_group(group_id)
            if gdata:
                gok, greason, gretry, gresets = self._check_dims(gdata, tokens, cost, now_unix)
                if gresets:
                    await self.redis.set_group(group_id, gresets)
                if not gok:
                    return False, f"Group {greason}", gretry

        # ---- Key-level check ----
        ok, reason, retry, resets = self._check_dims(data, tokens, cost, now_unix)
        if resets:
            await self.redis.set_api_key(key_hash, resets)
        if not ok:
            return False, reason, retry

        return True, None, 0

    @staticmethod
    def _compute_usage_updates(
        data: Dict[str, Any], tokens: int, cost: float, now_unix: int,
    ) -> Dict[str, str]:
        """Pure: compute RPM/TPM/daily/monthly counter updates from a data dict."""
        updates: Dict[str, str] = {}

        rpm_window_start = int(data.get("rpm_window_start", "0"))
        rpm_window_count = int(data.get("rpm_window_count", "0")) + 1
        if now_unix - rpm_window_start >= 60:
            rpm_window_start = now_unix
            rpm_window_count = 1
        updates["rpm_window_count"] = str(rpm_window_count)
        updates["rpm_window_start"] = str(rpm_window_start)

        tpm_window_start = int(data.get("tpm_window_start", "0"))
        tpm_window_count = int(data.get("tpm_window_count", "0")) + tokens
        if now_unix - tpm_window_start >= 60:
            tpm_window_start = now_unix
            tpm_window_count = tokens
        updates["tpm_window_count"] = str(tpm_window_count)
        updates["tpm_window_start"] = str(tpm_window_start)

        updates["daily_tokens_used"] = str(int(data.get("daily_tokens_used", "0")) + tokens)
        updates["monthly_cost_used"] = str(float(data.get("monthly_cost_used", "0.0")) + cost)
        return updates

    @staticmethod
    def _accumulate_quota_record(
        quota: Optional[Dict[str, Any]],
        tokens: int,
        cost: float,
        model: str,
        tokens_in: int,
        tokens_out: int,
    ) -> Dict[str, Any]:
        """Pure: accumulate one request into a quota record dict (DB_SCHEMA §2)."""
        if not quota:
            quota = {"tokens_in": "0", "tokens_out": "0", "cost_usd": "0.0",
                     "request_count": "0", "model_usage": "{}"}
        quota["tokens_in"] = str(int(quota.get("tokens_in", "0")) + tokens_in)
        quota["tokens_out"] = str(int(quota.get("tokens_out", "0")) + tokens_out)
        quota["cost_usd"] = str(float(quota.get("cost_usd", "0.0")) + cost)
        quota["request_count"] = str(int(quota.get("request_count", "0")) + 1)
        try:
            mu_raw = quota.get("model_usage", "{}")
            model_usage = json.loads(mu_raw) if isinstance(mu_raw, str) else mu_raw
        except (json.JSONDecodeError, TypeError):
            model_usage = {}
        entry = model_usage.get(model, {"in": 0, "out": 0})
        if isinstance(entry, dict):
            entry["in"] = entry.get("in", 0) + tokens_in
            entry["out"] = entry.get("out", 0) + tokens_out
        else:
            entry = {"in": tokens_in, "out": tokens_out}
        model_usage[model] = entry
        quota["model_usage"] = json.dumps(model_usage, ensure_ascii=False)
        return quota

    async def increment_usage(
        self,
        key_hash: str,
        tokens: int,
        cost: float,
        model: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        """Accumulate usage into the key AND its group (sync).

        DB_SCHEMA §2 quota counting + §5 rate-limit windows.

        Uses pure helpers ``_compute_usage_updates`` and ``_accumulate_quota_record``
        so both key-level and group-level mutations share the same logic.
        Group-level writes are wrapped in try/except so a group write failure
        never blocks key-level accounting.
        """
        data = await self.redis.get_api_key(key_hash)
        if not data:
            logger.warning("increment_usage: key_hash=%s 不存在", key_hash)
            return

        now_unix = self._now_unix()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = datetime.now(timezone.utc).strftime("%Y-%m")

        # ---- Key-level ----
        updates = self._compute_usage_updates(data, tokens, cost, now_unix)
        await self.redis.set_api_key(key_hash, updates)

        daily_quota = await self.redis.get_quota(key_hash, f"daily:{today}")
        await self.redis.set_quota(key_hash, f"daily:{today}",
                                   self._accumulate_quota_record(daily_quota, tokens, cost, model, tokens_in, tokens_out))
        monthly_quota = await self.redis.get_quota(key_hash, f"monthly:{month}")
        await self.redis.set_quota(key_hash, f"monthly:{month}",
                                   self._accumulate_quota_record(monthly_quota, tokens, cost, model, tokens_in, tokens_out))

        # ---- Group-level (sync, non-blocking on failure) ----
        group_id = data.get("group_id") or ""
        if group_id:
            try:
                gdata = await self.redis.get_group(group_id)
                if gdata:
                    gupdates = self._compute_usage_updates(gdata, tokens, cost, now_unix)
                    await self.redis.set_group(group_id, gupdates)
                    gdaily = await self.redis.get_quota(group_id, f"daily:{today}")
                    await self.redis.set_quota(group_id, f"daily:{today}",
                                               self._accumulate_quota_record(gdaily, tokens, cost, model, tokens_in, tokens_out))
                    gmonthly = await self.redis.get_quota(group_id, f"monthly:{month}")
                    await self.redis.set_quota(group_id, f"monthly:{month}",
                                               self._accumulate_quota_record(gmonthly, tokens, cost, model, tokens_in, tokens_out))
            except Exception as exc:
                logger.warning("组级 increment_usage 失败 group=%s: %s", group_id, exc)

        logger.debug("Usage incremented: key_hash=%s tokens=%d cost=$%.4f group=%s",
                     key_hash, tokens, cost, group_id or "-")

    # ------------------------------------------------------------------
    # Internal helper methods
    # ------------------------------------------------------------------

    async def _check_duplicate_user_key(self, user_id: str) -> None:
        """Check if user_id already has an active Key."""
        # Scan all aigateway:key:* records in Redis
        if self.redis.redis is None:
            return
        cursor = 0
        while True:
            cursor, keys = await self.redis.redis.scan(
                cursor, match="aigateway:key:*", count=100
            )
            for key in keys:
                if isinstance(key, bytes):
                    key = key.decode()
                data = await self.redis.get_api_key(key.split(":")[-1])
                if data and data.get("user_id") == user_id and data.get("status") == "active":
                    raise ValueError(f"用户 '{user_id}' 已存在活跃 Key: {data.get('key_id')}")
            if cursor == 0:
                break

    async def _find_key_hashes_by_id(self, key_id: str) -> List[str]:
        """Find all matching key_hash by key_id."""
        hashes: List[str] = []
        if self.redis.redis is None:
            return hashes
        cursor = 0
        while True:
            cursor, keys = await self.redis.redis.scan(
                cursor, match="aigateway:key:*", count=100
            )
            for raw_key in keys:
                kh = raw_key.decode().split(":")[-1] if isinstance(raw_key, bytes) else raw_key.split(":")[-1]
                data = await self.redis.get_api_key(kh)
                if data and data.get("key_id") == key_id:
                    hashes.append(kh)
            if cursor == 0:
                break
        return hashes

    async def migrate_groups(self, group_store) -> int:
        """Assign groupless keys to the default group. Call once at startup.

        Returns the number of keys migrated.
        """
        if self.redis is None or self.redis.redis is None:
            return 0
        default_id = await group_store.ensure_default_group()
        migrated = 0
        cursor = 0
        while True:
            cursor, keys = await self.redis.redis.scan(cursor, match="aigateway:key:*", count=100)
            for raw_key in keys:
                kh = raw_key.decode().split(":")[-1] if isinstance(raw_key, bytes) else raw_key.split(":")[-1]
                data = await self.redis.get_api_key(kh)
                if not data:
                    continue
                gid = data.get("group_id") or ""
                if not gid:
                    cs = data.get("cache_scope", "group")
                    # Atomic: key record + destination membership together
                    def _build(pipe, _kh=kh, _gid=default_id, _cs=cs):
                        return [
                            pipe.hset(f"aigateway:key:{_kh}",
                                      mapping={"group_id": _gid, "cache_scope": _cs}),
                            pipe.sadd(f"aigateway:group:{_gid}:members", _kh),
                        ]
                    await self.redis.pipe_batch(lambda pipe: _build(pipe))
                    migrated += 1
                else:
                    # ensure membership tracked even for already-grouped keys
                    await group_store.add_member(gid, kh)
            if cursor == 0:
                break
        if migrated:
            logger.info("迁移 %d 个无组 Key 到默认组 %s", migrated, default_id)
        return migrated

    # ------------------------------------------------------------------
    # Config hot-reload broadcast
    # ------------------------------------------------------------------

    async def broadcast_config_reload(self, config_version: str) -> None:
        """Broadcast config hot-reload notification.

        DB_SCHEMA §4: aigateway:config:reload channel
        """
        msg = {
            "event_type": "config_reload",
            "config_version": config_version,
            "timestamp": self._now_iso(),
        }
        await self.redis.publish(self.CONFIG_RELOAD_CHANNEL, msg)


__all__ = ["KeyStore"]
