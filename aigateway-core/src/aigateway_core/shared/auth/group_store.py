"""User group storage and group-level quota management - GroupStore.

Mirrors KeyStore's hash-counter quota model: each group has a Redis Hash
`aigateway:group:{group_id}` holding limits + used counters (daily_tokens,
monthly_cost, rpm/tpm windows), isomorphic to `aigateway:key:{key_hash}`.

Per the user-groups design:
- group quota = shared pool for all member keys
- personal (key) quota = sub-limit within the group
- both checked (group first, then key) and both incremented per request
- group_id replaces the unused tenant_id slot in cache keys
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def slugify(name: str) -> str:
    """Lowercase, non-alphanumeric -> '-', collapse repeats, strip ends.

    CJK characters are alphanumeric under Python str.isalnum() and are kept.
    """
    s = name.strip().lower()
    out: List[str] = []
    prev_dash = False
    for ch in s:
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        else:
            if not prev_dash:
                out.append("-")
                prev_dash = True
    return "".join(out).strip("-")


class GroupStore:
    """用户组存储 + 组级配额管理器。

    所有数据存 Redis，经 redis_client.RedisClientManager 访问。
    组 CRUD 事件通过 aigateway:groups:sync Pub/Sub 跨实例同步。
    """

    GROUP_NAMESPACE = "aigateway:group:"
    GROUP_LOOKUP_PREFIX = "aigateway:group_lookup:"
    GROUP_MEMBERS_SUFFIX = ":members"
    GROUPS_INDEX = "aigateway:groups:index"
    PUBSUB_CHANNEL = "aigateway:groups:sync"

    DEFAULT_GROUP_ID = "grp-default"
    DEFAULT_GROUP_NAME = "default"

    DEFAULT_DAILY_TOKENS = 1_000_000
    DEFAULT_MONTHLY_COST = 50.0
    DEFAULT_RATE_LIMIT_RPM = 60
    DEFAULT_RATE_LIMIT_TPM = 100_000

    def __init__(self, redis) -> None:  # type: ignore[reportMissingTypeArgument]
        self.redis = redis

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _now_unix() -> int:
        return int(datetime.now(timezone.utc).timestamp())

    def _default_group_fields(self, name: str, quotas: Optional[Dict[str, Any]]) -> Dict[str, str]:
        q = quotas or {}
        now_u = self._now_unix()
        return {
            "name": name,
            "status": "active",
            "created_at": self._now_iso(),
            "updated_at": self._now_iso(),
            "daily_tokens_limit": str(q.get("daily_tokens", self.DEFAULT_DAILY_TOKENS)),
            "daily_tokens_used": "0",
            "monthly_cost_limit": str(q.get("monthly_cost", self.DEFAULT_MONTHLY_COST)),
            "monthly_cost_used": "0.0",
            "rate_limit_rpm": str(q.get("rate_limit_rpm", self.DEFAULT_RATE_LIMIT_RPM)),
            "rate_limit_tpm": str(q.get("rate_limit_tpm", self.DEFAULT_RATE_LIMIT_TPM)),
            "rpm_window_start": str(now_u),
            "rpm_window_count": "0",
            "tpm_window_start": str(now_u),
            "tpm_window_count": "0",
        }

    async def create_group(self, name: str, quotas: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Create a group. Raises ValueError if name already exists."""
        if not name or not name.strip():
            raise ValueError("group name is required")
        name = name.strip()

        existing = await self.redis.get_group_lookup(name)
        if existing:
            raise ValueError(f"group '{name}' already exists")

        base_slug = slugify(name)
        group_id = f"grp-{base_slug}" if base_slug else "grp-group"
        suffix = 2
        while await self.redis.get_group(group_id) is not None:
            group_id = f"grp-{base_slug or 'group'}-{suffix}"
            suffix += 1

        fields = self._default_group_fields(name, quotas)
        await self.redis.set_group(group_id, fields)
        await self.redis.set_group_lookup(name, group_id)
        await self._add_to_index(group_id)
        await self._init_group_quota_periods(group_id)

        await self.redis.publish(self.PUBSUB_CHANNEL, {
            "event_type": "group_created", "group_id": group_id,
            "name": name, "timestamp": self._now_iso(),
        })
        logger.info("Group 创建: group_id=%s name=%s", group_id, name)
        return {"group_id": group_id, "name": name, **fields}

    async def get_group(self, group_id: str) -> Optional[Dict[str, Any]]:
        return await self.redis.get_group(group_id)

    async def list_groups(self) -> List[Dict[str, Any]]:
        ids = await self._all_group_ids()
        out: List[Dict[str, Any]] = []
        for gid in ids:
            g = await self.redis.get_group(gid)
            if g:
                g["group_id"] = gid
                g["member_count"] = await self.get_member_count(gid)
                out.append(g)
        return out

    async def update_group(
        self,
        group_id: str,
        quotas: Optional[Dict[str, Any]] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        data = await self.redis.get_group(group_id)
        if not data:
            raise ValueError(f"group {group_id} not found")
        if quotas:
            if "daily_tokens" in quotas:
                data["daily_tokens_limit"] = str(quotas["daily_tokens"])
            if "monthly_cost" in quotas:
                data["monthly_cost_limit"] = str(quotas["monthly_cost"])
            if "rate_limit_rpm" in quotas:
                data["rate_limit_rpm"] = str(quotas["rate_limit_rpm"])
            if "rate_limit_tpm" in quotas:
                data["rate_limit_tpm"] = str(quotas["rate_limit_tpm"])
        if status:
            data["status"] = status
        data["updated_at"] = self._now_iso()
        await self.redis.set_group(group_id, data)
        await self.redis.publish(self.PUBSUB_CHANNEL, {
            "event_type": "group_updated", "group_id": group_id,
            "timestamp": self._now_iso(),
        })
        return data

    async def delete_group(self, group_id: str) -> bool:
        if group_id == self.DEFAULT_GROUP_ID:
            raise ValueError("default group cannot be deleted")
        data = await self.redis.get_group(group_id)
        if not data:
            return False
        members = await self._get_members(group_id)
        if members:
            raise ValueError(f"group {group_id} still has {len(members)} members; reassign first")
        name = data.get("name", "")
        await self.redis.delete_group(group_id)
        if name:
            await self.redis.delete_group_lookup(name)
        await self._remove_from_index(group_id)
        await self.redis.publish(self.PUBSUB_CHANNEL, {
            "event_type": "group_deleted", "group_id": group_id,
            "timestamp": self._now_iso(),
        })
        return True

    def _members_key(self, group_id: str) -> str:
        return f"{self.GROUP_NAMESPACE}{group_id}{self.GROUP_MEMBERS_SUFFIX}"

    async def add_member(self, group_id: str, key_hash: str) -> None:
        if self.redis.redis is None:
            raise RuntimeError("Redis client is not connected")
        await self.redis.redis.sadd(self._members_key(group_id), key_hash)

    async def remove_member(self, group_id: str, key_hash: str) -> None:
        if self.redis.redis is None:
            raise RuntimeError("Redis client is not connected")
        await self.redis.redis.srem(self._members_key(group_id), key_hash)

    async def _get_members(self, group_id: str) -> List[str]:
        if self.redis.redis is None:
            return []
        raw = await self.redis.redis.smembers(self._members_key(group_id))
        return sorted(m.decode() if isinstance(m, bytes) else m for m in raw)

    async def get_member_count(self, group_id: str) -> int:
        return len(await self._get_members(group_id))

    async def get_group_detail(self, group_id: str) -> Optional[Dict[str, Any]]:
        data = await self.redis.get_group(group_id)
        if not data:
            return None
        data["group_id"] = group_id
        data["members"] = await self._get_members(group_id)
        data["member_count"] = len(data["members"])
        return data

    async def ensure_default_group(self) -> str:
        existing = await self.redis.get_group_lookup(self.DEFAULT_GROUP_NAME)
        if existing:
            return existing
        try:
            g = await self.create_group(self.DEFAULT_GROUP_NAME, {})
        except ValueError:
            return self.DEFAULT_GROUP_ID
        return g["group_id"]

    async def assign_key_to_group(self, key_hash: str, new_group_id: str) -> None:
        """Move a key from its current group to a new group, migrating usage counters.

        Transfers ``daily_tokens_used`` and ``monthly_cost_used`` from the old
        group to the new group so the sum across all groups stays constant.
        Also updates the key's ``group_id``, and the member SETs of both groups.
        """
        if new_group_id == self.DEFAULT_GROUP_ID:
            raise ValueError("cannot assign to default group via this method")

        # 1. Read current key data
        key_data = await self.redis.get_api_key(key_hash)
        if not key_data:
            raise ValueError(f"key {key_hash} not found")

        old_group_id = key_data.get("group_id") or ""
        if old_group_id == new_group_id:
            return  # already in the target group

        cache_scope = key_data.get("cache_scope", "group")

        # 2. Read both groups
        old_data = await self.redis.get_group(old_group_id) if old_group_id else None
        new_data = await self.redis.get_group(new_group_id)
        if not new_data:
            raise ValueError(f"target group {new_group_id} not found")

        # 3. Transfer usage from old → new
        if old_data:
            old_daily = int(old_data.get("daily_tokens_used", "0"))
            new_daily = int(new_data.get("daily_tokens_used", "0"))
            new_data["daily_tokens_used"] = str(old_daily + new_daily)
            old_data["daily_tokens_used"] = "0"

            old_monthly = float(old_data.get("monthly_cost_used", "0"))
            new_monthly = float(new_data.get("monthly_cost_used", "0"))
            new_data["monthly_cost_used"] = str(round(old_monthly + new_monthly, 4))
            old_data["monthly_cost_used"] = "0.0"

            await self.redis.set_group(old_group_id, old_data)
        await self.redis.set_group(new_group_id, new_data)

        # 4. Update member SETs (before key record so a failure leaves the key
        #    in its old group — quota checks remain consistent).
        await self.remove_member(old_group_id, key_hash)
        await self.add_member(new_group_id, key_hash)

        # 5. Update key record
        key_data["group_id"] = new_group_id
        if "cache_scope" not in key_data:
            key_data["cache_scope"] = cache_scope
        await self.redis.set_api_key(key_hash, key_data)

        await self.redis.publish(self.PUBSUB_CHANNEL, {
            "event_type": "key_assigned",
            "key_hash": key_hash,
            "from_group": old_group_id,
            "to_group": new_group_id,
            "timestamp": self._now_iso(),
        })
        logger.info("Key %s assigned to group %s (was %s)", key_hash, new_group_id, old_group_id)

    async def _add_to_index(self, group_id: str) -> None:
        if self.redis.redis is not None:
            await self.redis.redis.sadd(self.GROUPS_INDEX, group_id)

    async def _remove_from_index(self, group_id: str) -> None:
        if self.redis.redis is not None:
            await self.redis.redis.srem(self.GROUPS_INDEX, group_id)

    async def _all_group_ids(self) -> List[str]:
        if self.redis.redis is None:
            return []
        raw = await self.redis.redis.smembers(self.GROUPS_INDEX)
        return sorted(m.decode() if isinstance(m, bytes) else m for m in raw)

    async def _init_group_quota_periods(self, group_id: str) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        base = {
            "tokens_in": "0",
            "tokens_out": "0",
            "cost_usd": "0.0",
            "request_count": "0",
            "model_usage": "{}",
        }
        await self.redis.set_quota(group_id, f"daily:{today}", base)
        await self.redis.set_quota(group_id, f"monthly:{month}", base)


__all__ = ["GroupStore", "slugify"]
