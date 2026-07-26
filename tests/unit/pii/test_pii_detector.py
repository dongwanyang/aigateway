"""PIIDetector 行为单元测试.

覆盖:
- 三种策略: sanitize (default), reject, hash
- 排除模式: version, UUID, hex colors, ISO dates
- named-field 模式: 姓名, 密码, API key, 学号, 工号, 病历号, 性别
- standalone 模式: email, SSN, Visa/MC/Amex, 手机号, URL, 中国身份证,
  护照, 银行卡, 连接字符串, 私有密钥, IPv4
- 三遍处理顺序: exclusion → named → standalone
- 边界情况: 空文本, 无PII文本, 多PII混合
"""

import sys
import os
import hashlib
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aigateway-core", "src"))

from aigateway_core.prefix.pii.detector import PIIDetector


# ==================================================================
# 基础构造测试
# ==================================================================


class TestPIIDetectorInit:
    """PIIDetector 构造函数测试。"""

    def test_default_strategy_is_sanitize(self):
        det = PIIDetector()
        assert det.strategy == "sanitize"

    def test_custom_strategy(self):
        for strat in ("sanitize", "reject", "hash"):
            det = PIIDetector(strategy=strat)
            assert det.strategy == strat

    def test_detected_categories_starts_empty(self):
        det = PIIDetector()
        assert det.detected_categories == []

    def test_custom_patterns_accepted(self):
        custom = [(r'\bSECRET\b', '[CUSTOM_REDACTED]')]
        det = PIIDetector(patterns=custom)
        result = det.process("My SECRET is hidden")
        assert "[CUSTOM_REDACTED]" in result

    def test_custom_exclusion_patterns(self):
        # Exclude all digits
        excl = [(r'\d+', None)]
        det = PIIDetector(exclusion_patterns=excl)
        result = det.process("Phone is 13800138000")
        # Digits should be restored (excluded)
        assert "13800138000" in result


# ==================================================================
# Sanitize 策略测试
# ==================================================================


class TestSanitizeStrategy:
    """sanitize 策略 — 替换为掩码标记。"""

    def setup_method(self):
        self.det = PIIDetector(strategy="sanitize")

    def test_email_redacted(self):
        result = self.det.process("Contact me at user@example.com")
        assert "[EMAIL_REDACTED]" in result
        assert "user@example.com" not in result

    def test_chinese_name_redacted(self):
        result = self.det.process("姓名: 张三")
        assert "[NAME_REDACTED]" in result

    def test_password_redacted(self):
        result = self.det.process("password: mySecretPass123")
        assert "[CREDENTIAL_REDACTED]" in result

    def test_api_key_redacted(self):
        result = self.det.process("api_key: abcdef123456")
        assert "[CREDENTIAL_REDACTED]" in result

    def test_phone_china_mobile(self):
        result = self.det.process("Call me at 13800138000")
        assert "[PHONE_REDACTED]" in result

    def test_visa_card_redacted(self):
        result = self.det.process("Card: 4111111111111111")
        assert "[CC_REDACTED]" in result

    def test_mastercard_redacted(self):
        result = self.det.process("Card: 5105105105105100")
        assert "[CC_REDACTED]" in result

    def test_amex_redacted(self):
        result = self.det.process("Card: 371449635398431")
        assert "[CC_REDACTED]" in result

    def test_ssn_redacted(self):
        result = self.det.process("SSN: 123-45-6789")
        assert "[SSN_REDACTED]" in result

    def test_url_redacted(self):
        result = self.det.process("Visit https://evil.com/path?q=1")
        assert "[URL_REDACTED]" in result

    def test_ipv4_redacted(self):
        result = self.det.process("Server at 192.168.1.100")
        assert "[IP_REDACTED]" in result

    def test_private_key_redacted(self):
        result = self.det.process("Key: -----BEGIN RSA PRIVATE KEY-----")
        assert "[PRIVATE_KEY_REDACTED]" in result

    def test_connection_string_redacted(self):
        result = self.det.process("DB: postgresql://user:pass@localhost:5432/db")
        assert "[CONNSTR_REDACTED]" in result

    def test_chinese_id_18digit(self):
        result = self.det.process("ID: 110101199001011234")
        assert "[CN_ID_REDACTED]" in result

    def test_chinese_id_15digit(self):
        result = self.det.process("Old ID: 110101900101123")
        assert "[CN_ID_OLD_REDACTED]" in result

    def test_passport_redacted(self):
        result = self.det.process("Passport: G12345678")
        assert "[CN_PASSPORT_REDACTED]" in result

    def test_student_id_redacted(self):
        result = self.det.process("学号: 2024001234")
        assert "[STUDENT_ID_REDACTED]" in result

    def test_employee_id_redacted(self):
        result = self.det.process("工号: EMP1234")
        assert "[EMPLOYEE_ID_REDACTED]" in result

    def test_medical_record_redacted(self):
        result = self.det.process("病历号: MRN12345678")
        assert "[MEDICAL_REDACTED]" in result

    def test_gender_detected_but_not_masked(self):
        result = self.det.process("性别: 男")
        # Gender has no mask (None), so text unchanged but category recorded
        assert "男" in result
        # Category is "UNKNOWN" since mask is None → cat becomes "UNKNOWN"
        assert "UNKNOWN" in self.det.detected_categories

    def test_multiple_pii_in_one_text(self):
        text = "Email: alice@test.com, Phone: 13800138000, Card: 4111111111111111"
        result = self.det.process(text)
        assert "[EMAIL_REDACTED]" in result
        assert "[PHONE_REDACTED]" in result
        assert "[CC_REDACTED]" in result
        assert len(self.det.detected_categories) >= 3

    def test_access_key_redacted(self):
        result = self.det.process("access_key: AKIAIOSFODNN7EXAMPLE")
        assert "[CREDENTIAL_REDACTED]" in result

    def test_auth_token_redacted(self):
        result = self.det.process("auth_token: eyJhbGciOiJIUzI1NiJ9")
        assert "[CREDENTIAL_REDACTED]" in result

    def test_bearer_redacted(self):
        result = self.det.process("bearer: some_long_bearer_token_here")
        assert "[CREDENTIAL_REDACTED]" in result

    def test_unionpay_card_redacted(self):
        result = self.det.process("Card: 6200000000000000")
        assert "[CN_BANK_CARD_REDACTED]" in result

    def test_vin_redacted(self):
        result = self.det.process("VIN: WBADT43452G123456")
        assert "[VIN_REDACTED]" in result

    def test_empty_text_returns_as_is(self):
        assert self.det.process("") == ""

    def test_no_pii_text_returns_as_is(self):
        text = "This is a normal sentence with no sensitive data."
        result = self.det.process(text)
        assert result == text
        assert self.det.detected_categories == []


# ==================================================================
# Reject 策略测试
# ==================================================================


class TestRejectStrategy:
    """reject 策略 — 检测到 PII 时抛出 ValueError。"""

    def setup_method(self):
        self.det = PIIDetector(strategy="reject")

    def test_reject_on_email(self):
        with pytest.raises(ValueError) as exc_info:
            self.det.process("Contact: user@example.com")
        assert "[EMAIL]" in str(exc_info.value)

    def test_reject_on_password(self):
        with pytest.raises(ValueError):
            self.det.process("password: hunter2")

    def test_reject_does_not_modify_categories(self):
        """reject 时 detected_categories 仍应被填充。"""
        with pytest.raises(ValueError):
            self.det.process("Email: test@test.com")
        assert len(self.det.detected_categories) > 0

    def test_reject_clean_text_no_error(self):
        """无 PII 时不应抛异常。"""
        result = self.det.process("Hello world, this is clean text.")
        assert result == "Hello world, this is clean text."


# ==================================================================
# Hash 策略测试
# ==================================================================


class TestHashStrategy:
    """hash 策略 — 替换为 SHA256(mask_token + original) 的前16位。"""

    def setup_method(self):
        self.det = PIIDetector(strategy="hash")

    def test_email_hashed(self):
        result = self.det.process("Email: user@example.com")
        # Should contain a 16-char hex hash instead of email
        assert "user@example.com" not in result
        # Find the hash pattern (16 hex chars)
        import re
        match = re.search(r'[0-9a-f]{16}', result)
        assert match is not None

    def test_hash_is_deterministic(self):
        text = "password: secret123"
        r1 = self.det.process(text)
        self.det.detected_categories = []
        r2 = self.det.process(text)
        assert r1 == r2

    def test_hash_different_for_different_inputs(self):
        r1 = self.det.process("password: secret1")
        self.det.detected_categories = []
        r2 = self.det.process("password: secret2")
        assert r1 != r2

    def test_hash_no_category_for_cred(self):
        """credential hash should still record the category. """
        self.det.process("password: abcdefg123")
        assert len(self.det.detected_categories) > 0


# ==================================================================
# Exclusion 模式测试
# ==================================================================


class TestExclusionPatterns:
    """排除模式 — 不应被标记为 PII。"""

    def setup_method(self):
        self.det = PIIDetector(strategy="sanitize")

    def test_version_not_redacted(self):
        result = self.det.process("Version v1.2.3 released")
        assert "v1.2.3" in result
        assert "version" not in self.det.detected_categories

    def test_uuid_not_redacted(self):
        result = self.det.process("ID: 550e8400-e29b-41d4-a716-446655440000")
        assert "550e8400-e29b-41d4-a716-446655440000" in result

    def test_hex_color_not_redacted(self):
        result = self.det.process("Color #FF5733")
        assert "#FF5733" in result

    def test_iso_date_not_redacted(self):
        result = self.det.process("Date: 2024-01-15")
        assert "2024-01-15" in result

    def test_phone_with_version_only(self):
        """版本号和手机号共存时，版本号不被删除。"""
        result = self.det.process("v2.0.1 call 13800138000")
        assert "v2.0.1" in result
        assert "[PHONE_REDACTED]" in result


# ==================================================================
# 三遍处理顺序测试
# ==================================================================


class TestThreePassOrder:
    """验证 exclusion → named-field → standalone 的处理顺序。"""

    def setup_method(self):
        self.det = PIIDetector(strategy="sanitize")

    def test_named_field_before_standalone(self):
        """named-field 先于 standalone 运行，因此 "姓名: 张三" 匹配 named。"""
        result = self.det.process("姓名: 张三")
        assert "[NAME_REDACTED]" in result

    def test_standalone_catches_without_label(self):
        """没有标签的纯邮箱应被 standalone 捕获。"""
        result = self.det.process("Just an email: user@example.com in text")
        assert "[EMAIL_REDACTED]" in result

    def test_exclusion_protects_before_detection(self):
        """UUID 即使符合某些通用模式也应被排除。"""
        det = PIIDetector()
        result = det.process("UUID: 550e8400-e29b-41d4-a716-446655440000")
        assert "550e8400-e29b-41d4-a716-446655440000" in result


# ==================================================================
# detected_categories 追踪测试
# ==================================================================


class TestCategoryTracking:
    """验证 detected_categories 正确记录检测到的 PII 类型。"""

    def test_categories_populated_on_sanitize(self):
        det = PIIDetector(strategy="sanitize")
        det.process("Email: a@b.com and phone: 13800138000")
        # Category names are mask tokens with brackets/redacted stripped
        assert "EMAIL" in det.detected_categories
        assert "PHONE" in det.detected_categories

    def test_categories_cleared_between_calls(self):
        det = PIIDetector()
        det.process("user@example.com")
        assert len(det.detected_categories) > 0
        # Second call should clear previous categories
        det.process("clean text no pii here")
        assert det.detected_categories == []

    def test_gender_category_recorded(self):
        det = PIIDetector()
        det.process("性别: 男")
        # Gender has no mask → UNKNOWN category
        assert len(det.detected_categories) > 0
