"""
API Key 存储与配额管理
=====================

管理 API Key 的创建、验证、撤销及配额追踪。
数据持久化在 Redis（Hash + String），速率限制使用 SortedSet。

根据 DB_SCHEMA.md:
- §1 API Key 存储（Hash）
- §2 配额计数（Hash）
- §5 速率限制窗口（SortedSet + String）

结合 TECH_SPEC.md 的安全异常层次:
GatewayError -> AuthError, QuotaExceededError, CircuitBreakerOpenError
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from re import Pattern
from typing import Any, Dict, List, Optional, Tuple

from .exceptions import AuthError, CircuitBreakerOpenError, GatewayError, QuotaExceededError

logger = logging.getLogger(__name__)

# Re-export for backward compatibility (existing code imports from security.py)
__all__ = [
    "GatewayError",
    "AuthError",
    "QuotaExceededError",
    "CircuitBreakerOpenError",
    "KeyStore",
    "PIIDetector",
]


class KeyStore:
    """API Key 存储与配额管理器。

    所有持久化数据存储在 Redis 中，通过 redis_client.RedisClientManager 访问。
    Pub/Sub 事件用于多实例间的 Key 变更同步。

    属性:
        redis: Redis 客户端管理器实例。
    """

    # 常量 — DB_SCHEMA 定义的 Key 前缀
    KEY_NAMESPACE = "aigateway:key:"
    KEY_LOOKUP_PREFIX = "aigateway:key_lookup:"
    QUOTA_PREFIX = "aigateway:quota:"
    RATELIMIT_RPM_PREFIX = "aigateway:ratelimit:"
    RATELIMIT_TPM_SUFFIX = ":tpm"
    PUBSUB_CHANNEL = "aigateway:keys:sync"
    CONFIG_RELOAD_CHANNEL = "aigateway:config:reload"

    # 默认配额值（TECH_SPEC.md config.yaml 默认值）
    DEFAULT_DAILY_TOKENS = 1_000_000
    DEFAULT_MONTHLY_COST = 50.0
    DEFAULT_RATE_LIMIT_RPM = 60
    DEFAULT_RATE_LIMIT_TPM = 100_000

    def __init__(self, redis) -> None:  # type: ignore[reportMissingTypeArgument]
        """
        Args:
            redis: RedisClientManager 实例，已建立连接。
        """
        self.redis = redis

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _hash_key(key_value: str) -> str:
        """计算 API Key 的 SHA-256 哈希（取前 16 位 hex）。

        DB_SCHEMA §1: key_hash = SHA-256(key)[:16]
        """
        return hashlib.sha256(key_value.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _prefix_key(key_value: str) -> str:
        """提取 Key 前 8 字符用于展示。"""
        return key_value[:8]

    @staticmethod
    def _now_iso() -> str:
        """返回当前 UTC 时间的 ISO 8601 字符串。"""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _now_unix() -> int:
        """返回当前 Unix 时间戳（秒）。"""
        return int(datetime.now(timezone.utc).timestamp())

    def _build_pubsub_message(
        self, event_type: str, key_id: str, user_id: str, **extra: Any
    ) -> Dict[str, Any]:
        """构建 Pub/Sub 同步消息体。

        DB_SCHEMA §4: aigateway:keys:sync 频道消息格式
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
    # 核心操作：验证 / 创建 / 撤销
    # ------------------------------------------------------------------

    async def validate(self, key: str) -> Optional[Dict[str, Any]]:
        """验证 API Key 有效性。

        从 Redis `aigateway:key:{key_hash}` 查找并验证状态和配额。

        Args:
            key: 完整 API Key 值。

        Returns:
            Key 元数据字典，包含 key_id、user_id、status 等，无效时返回 None。

        Raises:
            AuthError: Key 已撤销时抛出。
        """
        key_hash = self._hash_key(key)
        data = await self.redis.get_api_key(key_hash)

        if data is None:
            logger.warning("API Key hash=%s 未找到", key_hash)
            return None

        status = data.get("status", "")
        if status == "revoked":
            raise AuthError(f"API key '{data.get('key_id')}' has been revoked")
        if status == "suspended":
            raise AuthError(f"API key '{data.get('key_id')}' is suspended")

        # 更新 last_used_at
        data["last_used_at"] = self._now_iso()
        await self.redis.set_api_key(key_hash, data)

        return data

    async def create(self, user_id: str, quotas: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """创建新 API Key 并存入 Redis。

        DB_SCHEMA §1: 写入 aigateway:key:{key_hash} Hash + aigateway:key_lookup Hash

        Args:
            user_id: 关联的用户 ID。
            quotas: 配额配置 {daily_tokens, monthly_cost, rate_limit_rpm, rate_limit_tpm}。

        Returns:
            包含完整 key 值和相关信息的字典。
        """
        if not user_id:
            raise ValueError("user_id is required")

        # 生成唯一 Key 值
        import uuid
        raw_key = f"sk-{uuid.uuid4().hex[:16]}"

        key_hash = self._hash_key(raw_key)
        key_prefix = self._prefix_key(raw_key)
        now_iso = self._now_iso()

        # 合并默认配额
        q = quotas or {}
        daily_tokens = q.get("daily_tokens", self.DEFAULT_DAILY_TOKENS)
        monthly_cost = q.get("monthly_cost", self.DEFAULT_MONTHLY_COST)
        rate_rpm = q.get("rate_limit_rpm", self.DEFAULT_RATE_LIMIT_RPM)
        rate_tpm = q.get("rate_limit_tpm", self.DEFAULT_RATE_LIMIT_TPM)

        key_id = f"key_{uuid.uuid4().hex[:8]}"

        # 检查同一 user_id 是否已有活跃 Key
        await self._check_duplicate_user_key(user_id)

        # 写入 Key Hash
        key_data: Dict[str, str] = {
            "key_id": key_id,
            "key_prefix": key_prefix,
            "user_id": user_id,
            "status": "active",
            "created_at": now_iso,
            "last_used_at": "",
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

        await self.redis.set_api_key(key_hash, key_data)

        # 写入反向查找记录（key_prefix -> key_hash）
        await self.redis.set_key_lookup(key_prefix, key_hash)

        # 初始化日配额和月配额记录（DB_SCHEMA §2）
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = datetime.now(timezone.utc).strftime("%Y-%m")

        quota_base = {
            "tokens_in": "0",
            "tokens_out": "0",
            "cost_usd": "0.0",
            "request_count": "0",
            "model_usage": "{}",
        }

        # 设置日配额 TTL（当天 23:59:59 UTC）
        await self.redis.set_quota(f"{key_hash}:daily:{today}", quota_base)

        # 设置月配额 TTL（当月最后一天 23:59:59 UTC）
        await self.redis.set_quota(f"{key_hash}:monthly:{month}", quota_base)

        # 通过 Pub/Sub 广播 Key 创建事件
        pub_msg = self._build_pubsub_message("key_created", key_id, user_id)
        await self.redis.publish(self.PUBSUB_CHANNEL, pub_msg)

        logger.info("API Key 创建成功: user_id=%s, key_id=%s, key_hash=%s", user_id, key_id, key_hash)

        return {
            "id": key_id,
            "key": raw_key,
            "key_prefix": key_prefix,
            "user_id": user_id,
            "created_at": now_iso,
            "status": "active",
            "quotas": {
                "daily_tokens": daily_tokens,
                "monthly_cost": monthly_cost,
                "rate_limit_rpm": rate_rpm,
                "rate_limit_tpm": rate_tpm,
            },
        }

    async def revoke(self, key_id: str) -> bool:
        """撤销指定 API Key。

        DB_SCHEMA §1: status 设为 "revoked"，同时删除 key_prefix 反向查找。
        广播 Pub/Sub 事件让所有 Gateway 实例同步。

        Args:
            key_id: API Key 内部 ID，如 "key_abc123"。

        Returns:
            是否撤销成功。
        """
        if not key_id.startswith("key_"):
            raise ValueError("Invalid key_id format, should be key_xxx")

        # 遍历所有 key_hash 找到匹配的 key_id
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

        # 删除反向查找
        if key_prefix:
            lookup_key = f"{self.KEY_LOOKUP_PREFIX}{key_prefix}"
            if self.redis.redis is not None:
                await self.redis.redis.delete(lookup_key)

        # 广播撤销事件
        pub_msg = self._build_pubsub_message("key_revoked", key_id, user_id)
        await self.redis.publish(self.PUBSUB_CHANNEL, pub_msg)

        logger.info("API Key 已撤销: key_id=%s", key_id)
        return True

    # ------------------------------------------------------------------
    # 配额检查与累加
    # ------------------------------------------------------------------

    async def check_quota(
        self,
        key_hash: str,
        tokens: int,
        cost: float,
    ) -> Tuple[bool, Optional[str], int]:
        """检查日/月配额和 RPM/TPM 速率限制。

        DB_SCHEMA §2 配额计数 + §5 速率限制窗口

        Args:
            key_hash: API Key 的 SHA-256 哈希前 16 位。
            tokens: 本次请求预计消耗的 token 数。
            cost: 本次请求预计成本（美元）。

        Returns:
            (是否通过, 失败原因, retry_after 秒数)。通过时 failure=None, retry_after=0。
        """
        data = await self.redis.get_api_key(key_hash)
        if not data:
            return False, "API Key does not exist", 0

        now_unix = self._now_unix()

        # ---- 检查 RPM 窗口 ----
        rpm_limit = int(data.get("rate_limit_rpm", self.DEFAULT_RATE_LIMIT_RPM))
        rpm_window_start = int(data.get("rpm_window_start", "0"))
        rpm_window_count = int(data.get("rpm_window_count", "0"))

        # RPM 窗口 60 秒
        if now_unix - rpm_window_start >= 60:
            # 重置 RPM 窗口
            await self.redis.set_api_key(key_hash, {
                "rpm_window_start": str(now_unix),
                "rpm_window_count": "0",
            })
            rpm_window_count = 0
            rpm_window_start = now_unix
        elif rpm_window_count >= rpm_limit:
            retry_after = rpm_window_start + 60 - now_unix
            return False, f"RPM limit exceeded: {rpm_window_count}/{rpm_limit}", retry_after

        # ---- 检查 TPM 窗口 ----
        tpm_limit = int(data.get("rate_limit_tpm", self.DEFAULT_RATE_LIMIT_TPM))
        tpm_window_start = int(data.get("tpm_window_start", "0"))
        tpm_window_count = int(data.get("tpm_window_count", "0"))

        if now_unix - tpm_window_start >= 60:
            await self.redis.set_api_key(key_hash, {
                "tpm_window_start": str(now_unix),
                "tpm_window_count": "0",
            })
            tpm_window_count = 0
            tpm_window_start = now_unix
        elif tpm_window_count + tokens > tpm_limit:
            retry_after = tpm_window_start + 60 - now_unix
            return False, f"TPM limit exceeded: {tpm_window_count + tokens}/{tpm_limit}", retry_after

        # ---- 检查日 token 配额 ----
        daily_limit = int(data.get("daily_tokens_limit", self.DEFAULT_DAILY_TOKENS))
        daily_used = int(data.get("daily_tokens_used", "0"))
        if daily_used + tokens > daily_limit:
            return False, f"Daily token limit exceeded: {daily_used}/{daily_limit}", 0

        # ---- 检查月成本配额 ----
        monthly_limit = float(data.get("monthly_cost_limit", self.DEFAULT_MONTHLY_COST))
        monthly_used = float(data.get("monthly_cost_used", "0.0"))
        if monthly_used + cost > monthly_limit:
            return False, f"Monthly cost limit exceeded: ${monthly_used:.2f}/${monthly_limit:.2f}", 0

        return True, None

    async def increment_usage(
        self,
        key_hash: str,
        tokens: int,
        cost: float,
        model: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        """累加配额使用计数。

        DB_SCHEMA §2 配额计数 + §5 速率限制窗口

        Args:
            key_hash: API Key 的 SHA-256 哈希前 16 位。
            tokens: 本次请求总 token 数。
            cost: 本次请求成本（美元）。
            model: 使用的模型名称。
            tokens_in: 输入 token 数。
            tokens_out: 输出 token 数。
        """
        data = await self.redis.get_api_key(key_hash)
        if not data:
            logger.warning("increment_usage: key_hash=%s 不存在", key_hash)
            return

        now_unix = self._now_unix()
        updates: Dict[str, str] = {}

        # ---- 更新 RPM/TPM 窗口计数 ----
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

        # ---- 累加日配额 ----
        daily_used = int(data.get("daily_tokens_used", "0")) + tokens
        updates["daily_tokens_used"] = str(daily_used)

        # ---- 累加月成本 ----
        monthly_used = float(data.get("monthly_cost_used", "0.0")) + cost
        updates["monthly_cost_used"] = str(monthly_used)

        # 更新 Key 主记录
        await self.redis.set_api_key(key_hash, updates)

        # ---- 累加配额记录（DB_SCHEMA §2）----
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = datetime.now(timezone.utc).strftime("%Y-%m")

        quota_key_daily = f"{key_hash}:daily:{today}"
        quota_key_monthly = f"{key_hash}:monthly:{month}"

        # 日配额
        daily_quota = await self.redis.get_quota(key_hash, f"daily:{today}")
        if daily_quota:
            daily_quota["tokens_in"] = str(int(daily_quota.get("tokens_in", "0")) + tokens_in)
            daily_quota["tokens_out"] = str(int(daily_quota.get("tokens_out", "0")) + tokens_out)
            daily_quota["cost_usd"] = str(float(daily_quota.get("cost_usd", "0.0")) + cost)
            daily_quota["request_count"] = str(int(daily_quota.get("request_count", "0")) + 1)
            # 累加 model_usage
            model_usage_raw = daily_quota.get("model_usage", "{}")
            try:
                model_usage = json.loads(model_usage_raw) if isinstance(model_usage_raw, str) else model_usage_raw
            except (json.JSONDecodeError, TypeError):
                model_usage = {}
            entry = model_usage.get(model, {"in": 0, "out": 0})
            if isinstance(entry, dict):
                entry["in"] = entry.get("in", 0) + tokens_in
                entry["out"] = entry.get("out", 0) + tokens_out
            else:
                entry = {"in": tokens_in, "out": tokens_out}
            model_usage[model] = entry
            daily_quota["model_usage"] = json.dumps(model_usage, ensure_ascii=False)
            await self.redis.set_quota(key_hash, f"daily:{today}", daily_quota)

        # 月配额
        monthly_quota = await self.redis.get_quota(key_hash, f"monthly:{month}")
        if monthly_quota:
            monthly_quota["tokens_in"] = str(int(monthly_quota.get("tokens_in", "0")) + tokens_in)
            monthly_quota["tokens_out"] = str(int(monthly_quota.get("tokens_out", "0")) + tokens_out)
            monthly_quota["cost_usd"] = str(float(monthly_quota.get("cost_usd", "0.0")) + cost)
            monthly_quota["request_count"] = str(int(monthly_quota.get("request_count", "0")) + 1)
            await self.redis.set_quota(key_hash, f"monthly:{month}", monthly_quota)

        logger.debug(
            "Usage incremented: key_hash=%s tokens=%d cost=$%.4f model=%s",
            key_hash, tokens, cost, model,
        )

    # ------------------------------------------------------------------
    # 内部辅助方法
    # ------------------------------------------------------------------

    async def _check_duplicate_user_key(self, user_id: str) -> None:
        """检查同一 user_id 是否已有活跃 Key。"""
        # 扫描 Redis 中所有 aigateway:key:* 记录
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
        """通过 key_id 查找所有匹配的 key_hash。"""
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

    # ------------------------------------------------------------------
    # 配置热加载广播
    # ------------------------------------------------------------------

    async def broadcast_config_reload(self, config_version: str) -> None:
        """广播配置热加载通知。

        DB_SCHEMA §4: aigateway:config:reload 频道
        """
        msg = {
            "event_type": "config_reload",
            "config_version": config_version,
            "timestamp": self._now_iso(),
        }
        await self.redis.publish(self.CONFIG_RELOAD_CHANNEL, msg)


# ------------------------------------------------------------------
# PII 检测与脱敏
# ------------------------------------------------------------------

# 排除模式（减少误报）— 只排除明确的非PII模式
_EXCLUSION_PATTERNS = [
    (r'\b(?:v|version|ver)\s*\d+\.\d+\.\d+\b', None),   # 版本号 (如 v1.2.3, version 2.10.1)
    (r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', None),  # UUID
    (r'#[0-9a-fA-F]{3,8}\b', None),                       # 十六进制颜色
    (r'\b\d{4}-\d{2}-\d{2}\b', None),                     # ISO 日期
]

# PII 检测模式（按优先级：named-field → standalone）
_PII_NAMED_FIELDS = [
    (r'(?:姓名|名字|称呼|name)\s*[:：]\s*([^\s\n]{2,20})', '[NAME_REDACTED]'),
    (r'(?:出生|生日|dob|出生日期)\s*[:：]?\s*(?:19|20)\d{2}[年\-/.](?:0[1-9]|1[0-2])[月\-/.]\d{1,2}', '[DOB_REDACTED]'),
    (r'(?:性别|sex|gender)\s*[:：]\s*(?:男|女|male|female|M|F)', None),  # 不脱敏，仅检测
    (r'(?:密码|passwd|pwd|pass|pw|secret)\s*[:=]\s*["\']?([^\s"\']{6,})["\']?', '[CREDENTIAL_REDACTED]'),
    (r'(?:api[_\-]?key|apikey)\s*[:=]\s*["\']?([^\s"\']{6,})["\']?', '[CREDENTIAL_REDACTED]'),
    (r'(?:access[_\-]?key|auth[_\-]?token|bearer)\s*[:=]\s*["\']?([^\s"\']{6,})["\']?', '[CREDENTIAL_REDACTED]'),
    (r'(?:病历号|住院号|门诊号|MRN|病案号)\s*[:：]\s*(\S{4,20})', '[MEDICAL_REDACTED]'),
    (r'(?:学号|student[_\-]?id)\s*[:：]\s*(\S{6,20})', '[STUDENT_ID_REDACTED]'),
    (r'(?:工号|employee[_\-]?id|emp[_\-]?id)\s*[:：]\s*(\S{4,20})', '[EMPLOYEE_ID_REDACTED]'),
]

_PII_STANDALONE = [
    (r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', '[EMAIL_REDACTED]'),
    (r'\b\d{3}-?\d{2}-?\d{4}\b', '[SSN_REDACTED]'),                             # US SSN
    (r'\b(?:4\d{3})[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b', '[CC_REDACTED]'),        # Visa
    (r'\b(?:5[1-5]\d{2})[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b', '[CC_REDACTED]'),   # MasterCard
    (r'\b(?:3[47])\d{2}[- ]?\d{6}[- ]?\d{5}\b', '[CC_REDACTED]'),              # Amex
    (r'\b1[3-9]\d{9}\b', '[PHONE_REDACTED]'),                                    # 中国手机号
    (r'\b(?:0\d{2,3}-)?\d{7,8}\b', '[PHONE_REDACTED]'),                         # 中国座机
    (r'\+\d{1,3}[\s\-]?\d{4,14}[\s\-]?\d{4,14}', '[PHONE_REDACTED]'),           # E164
    (r'https?://[^\s<>"{}|\\^`[\]]+', '[URL_REDACTED]'),
    # 具体模式放在通用模式之前，避免被泛化匹配吞掉
    (r'\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b', '[CN_ID_REDACTED]'),  # 18位身份证
    (r'\b[1-9]\d{7}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}\b', '[CN_ID_OLD_REDACTED]'),  # 15位身份证
    (r'\b[GpP]\d{8,9}\b', '[CN_PASSPORT_REDACTED]'),                             # 中国护照
    (r'\b[A-HJ-NPR-Z0-9]{17}\b', '[VIN_REDACTED]'),                              # VIN
    (r'\b[62][0-9]{14,18}\b', '[CN_BANK_CARD_REDACTED]'),                        # 银联卡
    (r'\b[A-Z0-9]{20}\b', None),                                                 # AWS Key（需上下文判断，仅检测）
    (r'(?:password|passwd|pwd|pass|pw|secret|token|api[_\-]?key|apikey|access[_\-]?key|auth[_\-]?token|bearer)\s*[:=]\s*["\']?[^\s"\']{6,}["\']?', '[CREDENTIAL_REDACTED]'),
    (r'-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----', '[PRIVATE_KEY_REDACTED]'),
    (r'(?:mongodb(?:\+srv)?|mysql|postgres(?:ql)?|redis|mssql|amqp|oracle)://[^\s"' + r"'" + r']{10,}', '[CONNSTR_REDACTED]'),
    # 通用模式放最后
    (r'\b\d{10,}\b', '[PHONE_REDACTED]'),                                        # 通用长数字电话（放最后）
    (r'\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b', '[IP_REDACTED]'),  # IPv4
]


class PIIDetector:
    """PII 检测与脱敏处理器。

    支持三种策略：
    - sanitize: 替换为掩码标记（默认）
    - reject: 检测到即返回 400
    - hash: 替换为 SHA256(mask_token + original)

    处理流程:
    1. 排除 pass — 移除 UUID/版本号/颜色/ISO 日期/SKU
    2. named-field pass — 匹配 "key: value" 模式
    3. standalone pass — 正则匹配原始文本
    """

    def __init__(
        self,
        strategy: str = "sanitize",
        patterns: Optional[List[Tuple[str, str]]] = None,
        exclusion_patterns: Optional[List[Tuple[str, Optional[str]]]] = None,
    ) -> None:
        self.strategy = strategy
        self._compiled_exclusions = [(re.compile(p), m) for p, m in (exclusion_patterns or _EXCLUSION_PATTERNS)]
        self._compiled_named = [(re.compile(p), m) for p, m in (patterns or _PII_NAMED_FIELDS)]
        self._compiled_standalone = [(re.compile(p), m) for p, m in (patterns or _PII_STANDALONE)]
        self.detected_categories: List[str] = []

    def process(self, text: str) -> str:
        """处理文本，检测并脱敏 PII。

        Args:
            text: 原始文本。

        Returns:
            处理后文本。如果策略为 reject 且检测到 PII，抛出 ValueError。
        """
        self.detected_categories = []

        # Step 1: 排除 pass — 临时替换排除模式
        excluded: List[Tuple[str, str, str]] = []  # (placeholder, original, mask)
        temp_text = text
        for pattern, _ in self._compiled_exclusions:
            for match in pattern.finditer(temp_text):
                placeholder = f"__EXCLUDE_{uuid.uuid4().hex[:8]}__"
                excluded.append((placeholder, match.group(0), match.group(0)))
                temp_text = temp_text[:match.start()] + placeholder + temp_text[match.end():]

        # Step 2: named-field pass
        temp_text = self._apply_masks(temp_text, self._compiled_named)

        # Step 3: standalone pass
        temp_text = self._apply_masks(temp_text, self._compiled_standalone)

        # Step 4: 恢复排除模式
        for placeholder, original, _ in excluded:
            temp_text = temp_text.replace(placeholder, original)

        if self.strategy == "reject" and self.detected_categories:
            cats = ", ".join(set(self.detected_categories))
            raise ValueError(f"PII detected: [{cats}]")

        return temp_text

    def _apply_masks(
        self,
        text: str,
        patterns: List[Tuple[Pattern, str]],
    ) -> str:
        """应用掩码替换。"""
        for pattern, mask in patterns:
            for match in pattern.finditer(text):
                cat = mask.replace("[", "").replace("_REDACTED]", "") if mask else "UNKNOWN"
                if cat not in self.detected_categories:
                    self.detected_categories.append(cat)
                if self.strategy == "hash" and mask:
                    original = match.group(0)
                    import hashlib
                    hashed = hashlib.sha256((mask + original).encode()).hexdigest()[:16]
                    text = text[:match.start()] + hashed + text[match.end():]
                elif mask:
                    text = text[:match.start()] + mask + text[match.end():]
        return text
