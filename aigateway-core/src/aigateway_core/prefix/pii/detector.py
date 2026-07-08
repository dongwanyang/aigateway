"""PII detection and masking — PIIDetector.

Moved from ``aigateway_core.security`` as part of the 总分总 runtime split
(Task 3). Three-pass detection: exclusion → named-field → standalone.
"""
from __future__ import annotations

import hashlib
import re
import uuid
from re import Pattern
from typing import List, Optional, Tuple

# ------------------------------------------------------------------
# PII detection patterns
# ------------------------------------------------------------------

# Exclusion patterns (reduce false positives) — only exclude clearly non-PII patterns
_EXCLUSION_PATTERNS = [
    (r'\b(?:v|version|ver)\s*\d+\.\d+\.\d+\b', None),   # version numbers (e.g. v1.2.3, version 2.10.1)
    (r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', None),  # UUID
    (r'#[0-9a-fA-F]{3,8}\b', None),                       # hex colors
    (r'\b\d{4}-\d{2}-\d{2}\b', None),                     # ISO dates
]

# PII detection patterns (by priority: named-field → standalone)
_PII_NAMED_FIELDS = [
    (r'(?:姓名|名字|称呼|name)\s*[:：]\s*([^\s\n]{2,20})', '[NAME_REDACTED]'),
    (r'(?:出生|生日|dob|出生日期)\s*[:：]?\s*(?:19|20)\d{2}[年\-/.](?:0[1-9]|1[0-2])[月\-/.]\d{1,2}', '[DOB_REDACTED]'),
    (r'(?:性别|sex|gender)\s*[:：]\s*(?:男|女|male|female|M|F)', None),  # detect only, no mask
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
    (r'\b1[3-9]\d{9}\b', '[PHONE_REDACTED]'),                                    # China mobile
    (r'\b(?:0\d{2,3}-)?\d{7,8}\b', '[PHONE_REDACTED]'),                         # China landline
    (r'\+\d{1,3}[\s\-]?\d{4,14}[\s\-]?\d{4,14}', '[PHONE_REDACTED]'),           # E164
    (r'https?://[^\s<>"{}|\\^`[\]]+', '[URL_REDACTED]'),
    # Specific patterns before generic ones, to avoid being swallowed by generalization
    (r'\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b', '[CN_ID_REDACTED]'),  # 18-digit ID
    (r'\b[1-9]\d{7}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}\b', '[CN_ID_OLD_REDACTED]'),  # 15-digit ID
    (r'\b[GpP]\d{8,9}\b', '[CN_PASSPORT_REDACTED]'),                             # China passport
    (r'\b[A-HJ-NPR-Z0-9]{17}\b', '[VIN_REDACTED]'),                              # VIN
    (r'\b[62][0-9]{14,18}\b', '[CN_BANK_CARD_REDACTED]'),                        # UnionPay
    (r'\b[A-Z0-9]{20}\b', None),                                                 # AWS Key (needs context, detect only)
    (r'(?:password|passwd|pwd|pass|pw|secret|token|api[_\-]?key|apikey|access[_\-]?key|auth[_\-]?token|bearer)\s*[:=]\s*["\']?[^\s"\']{6,}["\']?', '[CREDENTIAL_REDACTED]'),
    (r'-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----', '[PRIVATE_KEY_REDACTED]'),
    (r'(?:mongodb(?:\+srv)?|mysql|postgres(?:ql)?|redis|mssql|amqp|oracle)://[^\s"' + r"'" + r']{10,}', '[CONNSTR_REDACTED]'),
    # Generic patterns last
    (r'\b\d{10,}\b', '[PHONE_REDACTED]'),                                        # generic long-digit phone (last)
    (r'\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b', '[IP_REDACTED]'),  # IPv4
]


class PIIDetector:
    """PII detection and masking handler.

    Supports three strategies:
    - sanitize: replace with mask token (default)
    - reject: return 400 on detection
    - hash: replace with SHA256(mask_token + original)

    Processing flow:
    1. Exclusion pass — remove UUID/version/color/ISO date/SKU
    2. named-field pass — match "key: value" patterns
    3. standalone pass — regex match raw text
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
        """Process text, detect and mask PII.

        Args:
            text: original text.

        Returns:
            Processed text. If strategy is reject and PII detected, raises ValueError.
        """
        self.detected_categories = []

        # Step 1: exclusion pass — temporarily replace excluded patterns
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

        # Step 4: restore excluded patterns
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
        """Apply mask replacement."""
        for pattern, mask in patterns:
            for match in pattern.finditer(text):
                cat = mask.replace("[", "").replace("_REDACTED]", "") if mask else "UNKNOWN"
                if cat not in self.detected_categories:
                    self.detected_categories.append(cat)
                if self.strategy == "hash" and mask:
                    original = match.group(0)
                    hashed = hashlib.sha256((mask + original).encode()).hexdigest()[:16]
                    text = text[:match.start()] + hashed + text[match.end():]
                elif mask:
                    text = text[:match.start()] + mask + text[match.end():]
        return text


__all__ = ["PIIDetector"]
