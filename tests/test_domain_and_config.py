"""
Unit tests verifying domain models, configuration management, and logging credential redaction.
Zero external dependencies; runs directly via python -m unittest.
"""
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import logging
from pathlib import Path
import tempfile
import unittest

from allaccess_monitor.models import (
    AvailabilityStatus,
    SectionInfo,
    CheckResult,
    TicketAlert,
)
from allaccess_monitor.config import (
    TelegramConfig,
    DiscordConfig,
    EmailConfig,
    MonitorConfig,
    ConfigurationError,
    load_config,
    parse_bool,
    parse_int,
    parse_float,
    parse_comma_list,
)
from allaccess_monitor.logging_setup import (
    CredentialRedactionFilter,
    redact_credentials,
    setup_logging,
)


class TestDomainModels(unittest.TestCase):
    def test_availability_status_enum(self):
        self.assertEqual(AvailabilityStatus.AVAILABLE.value, "AVAILABLE")
        self.assertEqual(AvailabilityStatus.UNAVAILABLE.value, "UNAVAILABLE")
        self.assertEqual(AvailabilityStatus.SECTION_NOT_FOUND.value, "SECTION_NOT_FOUND")
        self.assertEqual(AvailabilityStatus.ERROR.value, "ERROR")
        self.assertTrue(AvailabilityStatus.AVAILABLE.is_available)
        self.assertFalse(AvailabilityStatus.UNAVAILABLE.is_available)

    def test_section_info_pricing_and_immutability(self):
        sec = SectionInfo(
            name="Sivori Media",
            available=True,
            price=125000.0,
            currency="ARS",
            service_charge=18750.0
        )
        self.assertEqual(sec.total_price, 143750.0)
        self.assertIn("143,750", sec.formatted_price)
        with self.assertRaises(FrozenInstanceError):
            sec.price = 100000.0

    def test_check_result_conversion_to_alert(self):
        sec = SectionInfo(name="Sivori Media", available=True, price=125000.0, service_charge=18750.0)
        res = CheckResult(
            status=AvailabilityStatus.AVAILABLE,
            section_name="Sivori media",
            section_info=sec
        )
        self.assertTrue(res.is_available)
        self.assertIsInstance(res.timestamp, datetime)
        self.assertEqual(res.timestamp.tzinfo, timezone.utc)

        alert = res.to_alert(is_reminder=False)
        self.assertEqual(alert.event_name, "Foo Fighters")
        self.assertEqual(alert.section_name, "Sivori media")
        self.assertEqual(alert.status, "AVAILABLE")
        self.assertEqual(alert.price, 143750.0)
        self.assertFalse(alert.is_reminder)

        reminder_alert = res.to_alert(is_reminder=True)
        self.assertEqual(reminder_alert.status, "REMINDER")
        self.assertTrue(reminder_alert.is_reminder)


class TestConfiguration(unittest.TestCase):
    def test_primitive_parsers(self):
        self.assertTrue(parse_bool("true"))
        self.assertTrue(parse_bool("1"))
        self.assertTrue(parse_bool("YES"))
        self.assertFalse(parse_bool("false"))
        self.assertFalse(parse_bool("0"))
        self.assertFalse(parse_bool(""))
        self.assertEqual(parse_int("120", 60), 120)
        self.assertEqual(parse_int("", 60), 60)
        with self.assertRaises(ConfigurationError):
            parse_int("invalid", 60)
        self.assertEqual(parse_float("15.5", 10.0), 15.5)
        self.assertEqual(parse_comma_list("a@b.com, c@d.com"), ["a@b.com", "c@d.com"])

    def test_secret_masking_in_repr(self):
        tg = TelegramConfig(enabled=True, bot_token="123456789:ABCdefGHIjklMNOpqrsTUVwxyz123456789", chat_id="123")
        dc = DiscordConfig(enabled=True, webhook_url="https://discord.com/api/webhooks/123456789/token-abc123xyz")
        em = EmailConfig(enabled=True, smtp_host="smtp.gmail.com", username="me@gmail.com", password="secret_password", to_addrs=["target@gmail.com"])

        self.assertNotIn("ABCdefGHIjklMNOpqrsTUVwxyz123456789", repr(tg))
        self.assertIn("1234***", repr(tg))
        self.assertNotIn("token-abc123xyz", repr(dc))
        self.assertIn("[REDACTED]", repr(dc))
        self.assertNotIn("secret_password", repr(em))
        self.assertIn("***", repr(em))

    def test_config_validation(self):
        mc = MonitorConfig(check_interval_seconds=2)
        errors = mc.validate()
        self.assertTrue(any("must be at least 5 seconds" in err for err in errors))

        with self.assertRaises(ConfigurationError):
            load_config(overrides={"check_interval_seconds": 1})


class TestLoggingRedaction(unittest.TestCase):
    def test_redact_credentials_utility(self):
        text = (
            "Sending alert to https://api.telegram.org/bot123456789:ABCdefGHIjklMNOpqrsTUVwxyz123456789/sendMessage "
            "and https://discord.com/api/webhooks/1234567890/token-abc123xyz with password='my_secret_pass'"
        )
        redacted = redact_credentials(text)
        self.assertNotIn("123456789:ABCdefGHIjklMNOpqrsTUVwxyz123456789", redacted)
        self.assertNotIn("token-abc123xyz", redacted)
        self.assertNotIn("my_secret_pass", redacted)
        self.assertIn("[REDACTED_TELEGRAM_TOKEN]", redacted)
        self.assertIn("[REDACTED_DISCORD_TOKEN]", redacted)
        self.assertIn("[REDACTED]", redacted)

    def test_filter_redacts_log_record_args(self):
        logger = logging.getLogger("test_redaction_logger")
        logger.setLevel(logging.INFO)
        log_output = []

        class CapturingHandler(logging.Handler):
            def emit(self, record):
                log_output.append(self.format(record))

        handler = CapturingHandler()
        handler.addFilter(CredentialRedactionFilter())
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

        logger.info("Bot token is %s", "123456789:ABCdefGHIjklMNOpqrsTUVwxyz123456789")
        self.assertEqual(len(log_output), 1)
        self.assertNotIn("ABCdefGHIjklMNOpqrsTUVwxyz123456789", log_output[0])
        self.assertIn("[REDACTED_TELEGRAM_TOKEN]", log_output[0])


if __name__ == "__main__":
    unittest.main()
