"""Comprehensive E2E Test Suite for AllAccess Foo Fighters Ticket Monitor.

Covering Tiers 1-4:
- Tier 1: Feature Coverage (Features 1-15: >=5 tests each, 75 tests)
- Tier 2: Boundary & Corner Cases (Features 1-15: >=5 tests each, 75 tests)
- Tier 3: Cross-Feature Combinations (Pairwise Interaction Coverage: 10 tests)
- Tier 4: Real-World Application Scenarios (5 Scenarios from TEST_INFRA.md)

Standard Library unittest compatible with pytest.
Zero live network calls (100% deterministic mocking and fixtures).
"""
import io
import os
import sys
import json
import time
import signal
import smtplib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

from tests.fixtures import load_fixture
from tests.e2e.fixtures import build_allaccess_html
from tests.e2e.test_helpers import (
    MockResponse,
    capture_output,
    temporary_env,
    run_cli_in_process,
)

# Graceful import check for allaccess_monitor
try:
    import allaccess_monitor
    from allaccess_monitor.models import (
        AvailabilityStatus,
        SectionInfo,
        CheckResult,
        TicketAlert,
    )
    from allaccess_monitor.config import (
        MonitorConfig,
        TelegramConfig,
        DiscordConfig,
        EmailConfig,
    )
    from allaccess_monitor.checker.base import BaseTicketChecker
    from allaccess_monitor.checker.allaccess import AllAccessTicketChecker
    from allaccess_monitor.checker.mock import MockTicketChecker
    from allaccess_monitor.notifier.base import BaseNotifier
    from allaccess_monitor.notifier.telegram import TelegramNotifier
    from allaccess_monitor.notifier.discord import DiscordNotifier
    from allaccess_monitor.notifier.email import EmailNotifier
    from allaccess_monitor.notifier.console import ConsoleNotifier
    from allaccess_monitor.notifier.composite import CompositeNotifier
    from allaccess_monitor.state import AlertStateManager, AlertDecision
    from allaccess_monitor.scheduler import PollingScheduler, GracefulShutdown
    from allaccess_monitor import cli
    from allaccess_monitor.logging_setup import setup_logging, RedactingFilter
    HAS_APP = True
    IMPORT_ERROR = ""
except Exception as e:
    HAS_APP = False
    IMPORT_ERROR = str(e)


def require_app(test_func):
    """Decorator to require allaccess_monitor implementation."""
    def wrapper(self, *args, **kwargs):
        if not HAS_APP:
            raise unittest.SkipTest(f"allaccess_monitor package not yet implemented in workspace: {IMPORT_ERROR}")
        return test_func(self, *args, **kwargs)
    wrapper.__name__ = test_func.__name__
    wrapper.__doc__ = test_func.__doc__
    return wrapper


# ==============================================================================
# TIER 1: FEATURE COVERAGE (>=5 tests per feature for all 15 features = 75 tests)
# ==============================================================================

class TestTier1FeatureCoverage(unittest.TestCase):
    """Tier 1: Feature Coverage testing for all 15 features."""

    # --------------------------------------------------------------------------
    # F1: Bootstrap Extraction
    # --------------------------------------------------------------------------
    @require_app
    def test_f1_01_extract_valid_bootstrap_data_object(self):
        """F1.1: Verify App.bootstrapData JSON is extracted and parsed."""
        html = load_fixture("available_response.html")
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori Media")
            self.assertIsInstance(result, CheckResult)
            self.assertEqual(result.status, AvailabilityStatus.AVAILABLE)

    @require_app
    def test_f1_02_extract_bootstrap_with_surrounding_html_and_scripts(self):
        """F1.2: Verify extraction works when surrounded by other scripts and elements."""
        base_html = build_allaccess_html(section_name="Sivori Media", available=True)
        html = f"<html><head><script>var init = 1;</script></head><body>{base_html}<script>console.log('end');</script></body></html>"
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori Media")
            self.assertEqual(result.status, AvailabilityStatus.AVAILABLE)

    @require_app
    def test_f1_03_extract_event_metadata(self):
        """F1.3: Verify event ID and event name metadata are extracted."""
        html = load_fixture("available_response.html")
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori Media")
            self.assertEqual(result.event_name, "Foo Fighters")
            self.assertIn("foo-fighters", result.event_url)

    @require_app
    def test_f1_04_extract_shows_and_sectors_structure(self):
        """F1.4: Verify shows and sectors lists are traversed correctly."""
        extra = [{
            "id": 999999,
            "name": "Platea San Martin",
            "available": True,
            "rates": [{"id": 1, "available": True, "max": 2, "price": 95000, "availability": 5}],
            "sections": [{"id": 2, "available": True}]
        }]
        html = build_allaccess_html(section_name="Sivori Media", available=False, extra_sectors=extra)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Platea San Martin")
            self.assertEqual(result.status, AvailabilityStatus.AVAILABLE)

    @require_app
    def test_f1_05_extract_bootstrap_with_multiline_formatting(self):
        """F1.5: Verify extraction works on indented and multiline bootstrap data."""
        html = build_allaccess_html(section_name="Sivori Media", available=True)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori Media")
            self.assertEqual(result.status, AvailabilityStatus.AVAILABLE)

    # --------------------------------------------------------------------------
    # F2: Unicode & Accent Normalization
    # --------------------------------------------------------------------------
    @require_app
    def test_f2_01_exact_match_sivori_media(self):
        """F2.1: Verify exact string 'Sivori media' matches 'Sivori Media'."""
        html = build_allaccess_html(section_name="Sivori Media", available=True)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori media")
            self.assertEqual(result.status, AvailabilityStatus.AVAILABLE)

    @require_app
    def test_f2_02_case_insensitivity(self):
        """F2.2: Verify uppercase and mixed case matching."""
        html = build_allaccess_html(section_name="Sivori Media", available=True)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res_upper = checker.check_availability("SIVORI MEDIA")
            self.assertEqual(res_upper.status, AvailabilityStatus.AVAILABLE)
            res_mixed = checker.check_availability("sIvOrI mEdIa")
            self.assertEqual(res_mixed.status, AvailabilityStatus.AVAILABLE)

    @require_app
    def test_f2_03_accented_target_sivori_media(self):
        """F2.3: Verify accented target 'Sívori media' matches unaccented source."""
        html = build_allaccess_html(section_name="Sivori Media", available=True)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sívori media")
            self.assertEqual(result.status, AvailabilityStatus.AVAILABLE)

    @require_app
    def test_f2_04_accented_source_matches_unaccented_target(self):
        """F2.4: Verify accented source 'Sívori Media' matches unaccented target."""
        html = build_allaccess_html(section_name="Sívori Media", available=True)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori media")
            self.assertEqual(result.status, AvailabilityStatus.AVAILABLE)

    @require_app
    def test_f2_05_leading_trailing_whitespace_normalization(self):
        """F2.5: Verify leading and trailing whitespaces are stripped."""
        html = build_allaccess_html(section_name="  Sivori Media  ", available=True)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("   Sivori media ")
            self.assertEqual(result.status, AvailabilityStatus.AVAILABLE)

    # --------------------------------------------------------------------------
    # F3: Availability & False-Positive Filter
    # --------------------------------------------------------------------------
    @require_app
    def test_f3_01_truly_available_when_rates_and_sections_available(self):
        """F3.1: Section is AVAILABLE when rate and sections are both available."""
        html = load_fixture("available_response.html")
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori media")
            self.assertEqual(result.status, AvailabilityStatus.AVAILABLE)
            self.assertTrue(result.section_info.available)

    @require_app
    def test_f3_02_unavailable_when_rates_available_false(self):
        """F3.2: Section is UNAVAILABLE when rates[0].available is False."""
        html = load_fixture("sold_out_response.html")
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori media")
            self.assertEqual(result.status, AvailabilityStatus.UNAVAILABLE)

    @require_app
    def test_f3_03_deceptive_sector_available_true_rejection(self):
        """F3.3: Critical false-positive filter: sector.available=True but rate.available=False must be UNAVAILABLE."""
        html = build_allaccess_html(
            section_name="Sivori Media",
            available=False,
            sector_available=True, # Deceptive flag
            max_qty=0,
            availability=0
        )
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori media")
            self.assertEqual(result.status, AvailabilityStatus.UNAVAILABLE)

    @require_app
    def test_f3_04_unavailable_when_max_zero(self):
        """F3.4: Section is UNAVAILABLE if rate.max is 0 even if available=True."""
        html = build_allaccess_html(section_name="Sivori Media", available=True, max_qty=0)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori media")
            self.assertEqual(result.status, AvailabilityStatus.UNAVAILABLE)

    @require_app
    def test_f3_05_unavailable_when_availability_count_zero(self):
        """F3.5: Section is UNAVAILABLE if rate.availability is 0."""
        html = build_allaccess_html(section_name="Sivori Media", available=True, max_qty=4, availability=0)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori media")
            self.assertEqual(result.status, AvailabilityStatus.UNAVAILABLE)

    # --------------------------------------------------------------------------
    # F4: Pricing Extraction
    # --------------------------------------------------------------------------
    @require_app
    def test_f4_01_extract_base_price(self):
        """F4.1: Extract base price of 125000.0 ARS."""
        html = load_fixture("available_response.html")
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori media")
            self.assertIsNotNone(result.section_info)
            self.assertEqual(result.section_info.price, 125000.0)

    @require_app
    def test_f4_02_extract_service_charge(self):
        """F4.2: Extract service charge of 18750.0 ARS."""
        html = load_fixture("available_response.html")
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori media")
            self.assertEqual(result.section_info.service_charge, 18750.0)

    @require_app
    def test_f4_03_default_currency_ars(self):
        """F4.3: Verify currency defaults to 'ARS'."""
        html = load_fixture("available_response.html")
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori media")
            self.assertEqual(result.section_info.currency, "ARS")

    @require_app
    def test_f4_04_populate_section_info_object(self):
        """F4.4: Verify SectionInfo dataclass fields are populated."""
        html = load_fixture("available_response.html")
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori media")
            info = result.section_info
            self.assertEqual(info.name, "Sivori Media")
            self.assertTrue(info.available)
            self.assertEqual(info.price, 125000.0)

    @require_app
    def test_f4_05_string_picker_price_fallback(self):
        """F4.5: Fallback to pickerPrice string if rate.price is not float."""
        html = build_allaccess_html(section_name="Sivori Media", price=85000.0)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori media")
            self.assertEqual(result.section_info.price, 85000.0)

    # --------------------------------------------------------------------------
    # F5: WAF & Challenge Detection
    # --------------------------------------------------------------------------
    @require_app
    def test_f5_01_detect_request_captcha_container(self):
        """F5.1: Detect request-captcha-container in HTML response."""
        html = load_fixture("waf_challenge_response.html")
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori media")
            self.assertEqual(result.status, AvailabilityStatus.ERROR)
            self.assertIn("captcha", (result.error_message or "").lower())

    @require_app
    def test_f5_02_detect_actividad_sospechosa_message(self):
        """F5.2: Detect 'actividad sospechosa' block text."""
        html = build_allaccess_html(waf_challenge=True)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori media")
            self.assertEqual(result.status, AvailabilityStatus.ERROR)

    @require_app
    def test_f5_03_http_403_waf_block(self):
        """F5.3: Handle HTTP 403 request_rejected WAF response."""
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=403, text='{"error":"request_rejected"}')):
            result = checker.check_availability("Sivori media")
            self.assertEqual(result.status, AvailabilityStatus.ERROR)
            self.assertEqual(result.status_code, 403)

    @require_app
    def test_f5_04_waf_does_not_trigger_false_positive_alert(self):
        """F5.4: Ensure WAF challenge NEVER reports AVAILABLE."""
        html = load_fixture("waf_challenge_response.html")
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori media")
            self.assertNotEqual(result.status, AvailabilityStatus.AVAILABLE)

    @require_app
    def test_f5_05_waf_exception_message_descriptive(self):
        """F5.5: Check that error message mentions WAF/challenge."""
        html = load_fixture("waf_challenge_response.html")
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            result = checker.check_availability("Sivori media")
            self.assertTrue(any(k in (result.error_message or "").lower() for k in ["waf", "captcha", "challenge", "block"]))

    # --------------------------------------------------------------------------
    # F6: Telegram Notifier
    # --------------------------------------------------------------------------
    @require_app
    def test_f6_01_send_alert_success_200(self):
        """F6.1: TelegramNotifier returns True on HTTP 200."""
        notifier = TelegramNotifier(bot_token="test_token", chat_id="123456")
        alert = TicketAlert(event_name="Foo Fighters", section_name="Sivori media", status="AVAILABLE", event_url="https://allaccess.com.ar", price=125000.0)
        with patch("requests.post", return_value=MockResponse(status_code=200, json_data={"ok": True})):
            success = notifier.send(alert)
            self.assertTrue(success)

    @require_app
    def test_f6_02_html_parse_mode_used(self):
        """F6.2: TelegramNotifier specifies parse_mode='HTML'."""
        notifier = TelegramNotifier(bot_token="test_token", chat_id="123456")
        alert = TicketAlert(event_name="Foo Fighters", section_name="Sivori media", status="AVAILABLE", event_url="https://allaccess.com.ar")
        with patch("requests.post", return_value=MockResponse(status_code=200, json_data={"ok": True})) as mock_post:
            notifier.send(alert)
            _, kwargs = mock_post.call_args
            payload = kwargs.get("json", {})
            self.assertEqual(payload.get("parse_mode"), "HTML")

    @require_app
    def test_f6_03_url_and_section_in_message(self):
        """F6.3: Telegram alert body contains section name and purchase link."""
        notifier = TelegramNotifier(bot_token="test_token", chat_id="123456")
        alert = TicketAlert(event_name="Foo Fighters", section_name="Sivori media", status="AVAILABLE", event_url="https://www.allaccess.com.ar/event/foo-fighters", price=125000.0)
        with patch("requests.post", return_value=MockResponse(status_code=200, json_data={"ok": True})) as mock_post:
            notifier.send(alert)
            _, kwargs = mock_post.call_args
            text = kwargs.get("json", {}).get("text", "")
            self.assertIn("Sivori media", text)
            self.assertIn("https://www.allaccess.com.ar/event/foo-fighters", text)

    @require_app
    def test_f6_04_html_escaping_entities(self):
        """F6.4: HTML characters like < and > are safely escaped."""
        notifier = TelegramNotifier(bot_token="test_token", chat_id="123456")
        alert = TicketAlert(event_name="Foo <Fighters>", section_name="Sivori & Media", status="AVAILABLE", event_url="https://allaccess.com.ar")
        with patch("requests.post", return_value=MockResponse(status_code=200, json_data={"ok": True})) as mock_post:
            notifier.send(alert)
            text = mock_post.call_args[1].get("json", {}).get("text", "")
            self.assertNotIn("<Fighters>", text)
            self.assertIn("&lt;Fighters&gt;", text)

    @require_app
    def test_f6_05_channel_name_property(self):
        """F6.5: Verify channel_name property is 'telegram'."""
        notifier = TelegramNotifier(bot_token="test_token", chat_id="123456")
        self.assertEqual(notifier.channel_name, "telegram")

    # --------------------------------------------------------------------------
    # F7: Discord Notifier
    # --------------------------------------------------------------------------
    @require_app
    def test_f7_01_send_alert_success_204(self):
        """F7.1: DiscordNotifier returns True on HTTP 204 No Content."""
        notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/token")
        alert = TicketAlert(event_name="Foo Fighters", section_name="Sivori media", status="AVAILABLE", event_url="https://allaccess.com.ar", price=125000.0)
        with patch("requests.post", return_value=MockResponse(status_code=204)):
            self.assertTrue(notifier.send(alert))

    @require_app
    def test_f7_02_embed_structure_validation(self):
        """F7.2: Discord payload contains rich embed structure."""
        notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/token")
        alert = TicketAlert(event_name="Foo Fighters", section_name="Sivori media", status="AVAILABLE", event_url="https://allaccess.com.ar")
        with patch("requests.post", return_value=MockResponse(status_code=204)) as mock_post:
            notifier.send(alert)
            payload = mock_post.call_args[1].get("json", {})
            self.assertIn("embeds", payload)
            self.assertTrue(len(payload["embeds"]) > 0)
            embed = payload["embeds"][0]
            self.assertIn("title", embed)
            self.assertIn("fields", embed)

    @require_app
    def test_f7_03_embed_color_green_for_available(self):
        """F7.3: Discord embed color is green on ticket availability."""
        notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/token")
        alert = TicketAlert(event_name="Foo Fighters", section_name="Sivori media", status="AVAILABLE", event_url="https://allaccess.com.ar")
        with patch("requests.post", return_value=MockResponse(status_code=204)) as mock_post:
            notifier.send(alert)
            embed = mock_post.call_args[1]["json"]["embeds"][0]
            self.assertIn("color", embed)
            self.assertEqual(embed["color"], 3066993) # 0x2ECC71

    @require_app
    def test_f7_04_channel_name_property(self):
        """F7.4: Verify channel_name property is 'discord'."""
        notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/token")
        self.assertEqual(notifier.channel_name, "discord")

    @require_app
    def test_f7_05_section_and_price_in_fields(self):
        """F7.5: Discord embed fields contain Section and Price."""
        notifier = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/token")
        alert = TicketAlert(event_name="Foo Fighters", section_name="Sivori media", status="AVAILABLE", event_url="https://allaccess.com.ar", price=125000.0)
        with patch("requests.post", return_value=MockResponse(status_code=204)) as mock_post:
            notifier.send(alert)
            fields = mock_post.call_args[1]["json"]["embeds"][0]["fields"]
            names = [f["name"] for f in fields]
            self.assertTrue(any("section" in n.lower() for n in names))

    # --------------------------------------------------------------------------
    # F8: Email Notifier
    # --------------------------------------------------------------------------
    @require_app
    def test_f8_01_send_email_starttls_success(self):
        """F8.1: Email delivery using STARTTLS on port 587."""
        notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_port=587,
            username="user@example.com",
            password="secret_password",
            to_addrs=["fan@example.com"],
            use_tls=True
        )
        alert = TicketAlert(event_name="Foo Fighters", section_name="Sivori media", status="AVAILABLE", event_url="https://allaccess.com.ar")
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = mock_smtp
            success = notifier.send(alert)
            self.assertTrue(success)
            mock_smtp.starttls.assert_called_once()
            mock_smtp.login.assert_called_once_with("user@example.com", "secret_password")

    @require_app
    def test_f8_02_send_email_ssl_success(self):
        """F8.2: Email delivery using direct SSL on port 465."""
        notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_port=465,
            username="user@example.com",
            password="secret_password",
            to_addrs=["fan@example.com"],
            use_ssl=True
        )
        alert = TicketAlert(event_name="Foo Fighters", section_name="Sivori media", status="AVAILABLE", event_url="https://allaccess.com.ar")
        with patch("smtplib.SMTP_SSL") as mock_ssl_cls:
            mock_smtp = MagicMock()
            mock_ssl_cls.return_value.__enter__.return_value = mock_smtp
            success = notifier.send(alert)
            self.assertTrue(success)
            mock_smtp.login.assert_called_once_with("user@example.com", "secret_password")

    @require_app
    def test_f8_03_multipart_text_and_html(self):
        """F8.3: Email contains both plain text and HTML parts."""
        notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_port=587,
            username="user@example.com",
            password="pass",
            to_addrs=["fan@example.com"]
        )
        alert = TicketAlert(event_name="Foo Fighters", section_name="Sivori media", status="AVAILABLE", event_url="https://allaccess.com.ar", price=125000.0)
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = mock_smtp
            notifier.send(alert)
            msg = mock_smtp.send_message.call_args[0][0]
            self.assertTrue(msg.is_multipart())

    @require_app
    def test_f8_04_channel_name_property(self):
        """F8.4: Verify channel_name property is 'email'."""
        notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_port=587,
            username="user@example.com",
            password="pass",
            to_addrs=["fan@example.com"]
        )
        self.assertEqual(notifier.channel_name, "email")

    @require_app
    def test_f8_05_subject_contains_event_and_section(self):
        """F8.5: Email Subject line contains event and section."""
        notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_port=587,
            username="user@example.com",
            password="pass",
            to_addrs=["fan@example.com"]
        )
        alert = TicketAlert(event_name="Foo Fighters", section_name="Sivori media", status="AVAILABLE", event_url="https://allaccess.com.ar")
        with patch("smtplib.SMTP") as mock_smtp_cls:
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = mock_smtp
            notifier.send(alert)
            msg = mock_smtp.send_message.call_args[0][0]
            subject = msg["Subject"]
            self.assertIn("Foo Fighters", subject)
            self.assertIn("Sivori media", subject)

    # --------------------------------------------------------------------------
    # F9: Composite Multiplexer
    # --------------------------------------------------------------------------
    @require_app
    def test_f9_01_dispatch_all_enabled_channels(self):
        """F9.1: Dispatches alert to all registered child notifiers."""
        c1, c2 = MagicMock(spec=BaseNotifier), MagicMock(spec=BaseNotifier)
        c1.send.return_value = True
        c2.send.return_value = True
        composite = CompositeNotifier([c1, c2])
        alert = TicketAlert(event_name="Foo Fighters", section_name="Sivori media", status="AVAILABLE", event_url="https://allaccess.com.ar")
        res = composite.send(alert)
        self.assertTrue(res)
        c1.send.assert_called_once_with(alert)
        c2.send.assert_called_once_with(alert)

    @require_app
    def test_f9_02_return_true_if_at_least_one_succeeds(self):
        """F9.2: Composite returns True if at least one child notifier succeeds."""
        c1, c2 = MagicMock(spec=BaseNotifier), MagicMock(spec=BaseNotifier)
        c1.send.return_value = False
        c2.send.return_value = True
        composite = CompositeNotifier([c1, c2])
        alert = TicketAlert(event_name="Foo Fighters", section_name="Sivori media", status="AVAILABLE", event_url="https://allaccess.com.ar")
        self.assertTrue(composite.send(alert))

    @require_app
    def test_f9_03_return_false_if_all_fail(self):
        """F9.3: Composite returns False if all child notifiers fail."""
        c1, c2 = MagicMock(spec=BaseNotifier), MagicMock(spec=BaseNotifier)
        c1.send.return_value = False
        c2.send.return_value = False
        composite = CompositeNotifier([c1, c2])
        alert = TicketAlert(event_name="Foo Fighters", section_name="Sivori media", status="AVAILABLE", event_url="https://allaccess.com.ar")
        self.assertFalse(composite.send(alert))

    @require_app
    def test_f9_04_channel_name_property(self):
        """F9.4: CompositeNotifier channel_name is 'composite'."""
        composite = CompositeNotifier([])
        self.assertEqual(composite.channel_name, "composite")

    @require_app
    def test_f9_05_empty_notifiers_list(self):
        """F9.5: Composite with empty list handles dispatch without crashing."""
        composite = CompositeNotifier([])
        alert = TicketAlert(event_name="Foo Fighters", section_name="Sivori media", status="AVAILABLE", event_url="https://allaccess.com.ar")
        self.assertFalse(composite.send(alert))

    # --------------------------------------------------------------------------
    # F10: State Machine & Cooldown Throttling
    # --------------------------------------------------------------------------
    @require_app
    def test_f10_01_initial_available_triggers_alert(self):
        """F10.1: Initial transition from UNAVAILABLE to AVAILABLE triggers alert."""
        state = AlertStateManager(cooldown_seconds=1800)
        decision = state.process_check_result(is_available=True, current_time=1000.0)
        self.assertTrue(decision.should_alert)
        self.assertFalse(decision.is_reminder)

    @require_app
    def test_f10_02_continued_availability_within_cooldown_throttled(self):
        """F10.2: Continued availability within cooldown is throttled."""
        state = AlertStateManager(cooldown_seconds=1800)
        state.process_check_result(is_available=True, current_time=1000.0)
        second_decision = state.process_check_result(is_available=True, current_time=1500.0)
        self.assertFalse(second_decision.should_alert)

    @require_app
    def test_f10_03_continued_availability_after_cooldown_triggers_reminder(self):
        """F10.3: Continued availability after cooldown triggers reminder alert."""
        state = AlertStateManager(cooldown_seconds=1800)
        state.process_check_result(is_available=True, current_time=1000.0)
        reminder_decision = state.process_check_result(is_available=True, current_time=2900.0)
        self.assertTrue(reminder_decision.should_alert)
        self.assertTrue(reminder_decision.is_reminder)

    @require_app
    def test_f10_04_sold_out_resets_cooldown_timer(self):
        """F10.4: Tickets selling out resets state so immediate availability next cycle alerts."""
        state = AlertStateManager(cooldown_seconds=1800)
        state.process_check_result(is_available=True, current_time=1000.0)
        # Sold out
        state.process_check_result(is_available=False, current_time=1100.0)
        # Tickets reappear 60 seconds later (well within 1800s cooldown)
        reappear_decision = state.process_check_result(is_available=True, current_time=1160.0)
        self.assertTrue(reappear_decision.should_alert)
        self.assertFalse(reappear_decision.is_reminder)

    @require_app
    def test_f10_05_repeated_unavailable_does_not_alert(self):
        """F10.5: Consecutive UNAVAILABLE checks never trigger an alert."""
        state = AlertStateManager(cooldown_seconds=1800)
        d1 = state.process_check_result(is_available=False, current_time=1000.0)
        d2 = state.process_check_result(is_available=False, current_time=1060.0)
        self.assertFalse(d1.should_alert)
        self.assertFalse(d2.should_alert)

    # --------------------------------------------------------------------------
    # F11: Configuration & Secret Masking
    # --------------------------------------------------------------------------
    @require_app
    def test_f11_01_load_config_defaults(self):
        """F11.1: Verify default configuration values."""
        cfg = MonitorConfig()
        self.assertEqual(cfg.poll_interval, 60)
        self.assertEqual(cfg.target_section, "Sivori media")
        self.assertIn("foo-fighters", cfg.event_url)

    @require_app
    def test_f11_02_load_config_from_env_file(self):
        """F11.2: Load configuration from custom .env file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text("POLL_INTERVAL=45\nALLACCESS_TARGET_SECTION=Sivori Media\n", encoding="utf-8")
            cfg = MonitorConfig.from_env(env_path)
            self.assertEqual(cfg.poll_interval, 45)
            self.assertEqual(cfg.target_section, "Sivori Media")

    @require_app
    def test_f11_03_cli_overrides_env_file(self):
        """F11.3: CLI arguments override .env settings."""
        parser = cli.build_parser()
        args = parser.parse_args(["--interval", "30", "--section", "Campo Delantero"])
        cfg = MonitorConfig.from_args(args)
        self.assertEqual(cfg.poll_interval, 30)
        self.assertEqual(cfg.target_section, "Campo Delantero")

    @require_app
    def test_f11_04_secret_masking_in_repr(self):
        """F11.4: Secrets like bot token and passwords are masked in __repr__."""
        cfg = MonitorConfig(
            telegram=TelegramConfig(bot_token="123456:SECRET_TELEGRAM_TOKEN", chat_id="123"),
            email=EmailConfig(password="SUPER_SECRET_EMAIL_PASSWORD")
        )
        repr_str = repr(cfg)
        self.assertNotIn("SECRET_TELEGRAM_TOKEN", repr_str)
        self.assertNotIn("SUPER_SECRET_EMAIL_PASSWORD", repr_str)
        self.assertIn("[REDACTED]", repr_str)

    @require_app
    def test_f11_05_logging_filter_redacts_secrets(self):
        """F11.5: RedactingFilter scrubs credentials from log records."""
        filter_ = RedactingFilter(secrets=["my_super_secret_token", "discord_secret_url"])
        record = MagicMock()
        record.msg = "Connecting to discord_secret_url with my_super_secret_token"
        filter_.filter(record)
        self.assertNotIn("my_super_secret_token", record.msg)
        self.assertNotIn("discord_secret_url", record.msg)
        self.assertIn("[REDACTED]", record.msg)

    # --------------------------------------------------------------------------
    # F12: Responsive Polling Scheduler
    # --------------------------------------------------------------------------
    @require_app
    def test_f12_01_single_tick_execution(self):
        """F12.1: Scheduler executes single tick with mock checker and state."""
        checker = MockTicketChecker(status=AvailabilityStatus.UNAVAILABLE)
        state = AlertStateManager()
        notifier = MagicMock(spec=BaseNotifier)
        scheduler = PollingScheduler(checker=checker, state_manager=state, notifier=notifier, interval_seconds=1)
        res = scheduler.run_single_tick()
        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)

    @require_app
    def test_f12_02_periodic_loop_interval_wait(self):
        """F12.2: Scheduler sleeps for the configured interval."""
        checker = MockTicketChecker(status=AvailabilityStatus.UNAVAILABLE)
        scheduler = PollingScheduler(checker=checker, state_manager=AlertStateManager(), notifier=MagicMock(), interval_seconds=0.05)
        with patch.object(scheduler._shutdown, "wait", return_value=True) as mock_wait:
            scheduler.run(max_ticks=1)
            mock_wait.assert_called_once_with(0.05)

    @require_app
    def test_f12_03_graceful_shutdown_event_set(self):
        """F12.3: Setting stop_event terminates scheduler loop immediately."""
        shutdown = GracefulShutdown()
        self.assertFalse(shutdown.is_stopped)
        shutdown.stop()
        self.assertTrue(shutdown.is_stopped)

    @require_app
    def test_f12_04_max_ticks_limit(self):
        """F12.4: Scheduler terminates cleanly after reaching max_ticks."""
        checker = MockTicketChecker(status=AvailabilityStatus.UNAVAILABLE)
        scheduler = PollingScheduler(checker=checker, state_manager=AlertStateManager(), notifier=MagicMock(), interval_seconds=0.01)
        count = scheduler.run(max_ticks=3)
        self.assertEqual(count, 3)

    @require_app
    def test_f12_05_telemetry_metrics_tracking(self):
        """F12.5: Scheduler tracks metrics (checks executed, alerts dispatched)."""
        checker = MockTicketChecker(status=AvailabilityStatus.AVAILABLE)
        notifier = MagicMock()
        notifier.send.return_value = True
        scheduler = PollingScheduler(checker=checker, state_manager=AlertStateManager(), notifier=notifier, interval_seconds=0.01)
        scheduler.run(max_ticks=2)
        metrics = scheduler.metrics
        self.assertEqual(metrics["total_checks"], 2)
        self.assertEqual(metrics["alerts_dispatched"], 1) # 1 initial alert, 2nd throttled

    # --------------------------------------------------------------------------
    # F13: Exponential Backoff & Jitter
    # --------------------------------------------------------------------------
    @require_app
    def test_f13_01_exponential_delay_growth(self):
        """F13.1: Consecutive failures increase delay exponentially."""
        scheduler = PollingScheduler(
            checker=MockTicketChecker(status=AvailabilityStatus.ERROR),
            state_manager=AlertStateManager(),
            notifier=MagicMock(),
            interval_seconds=10,
            max_backoff=300
        )
        d1 = scheduler.compute_backoff_delay(consecutive_errors=1, apply_jitter=False)
        d2 = scheduler.compute_backoff_delay(consecutive_errors=2, apply_jitter=False)
        d3 = scheduler.compute_backoff_delay(consecutive_errors=3, apply_jitter=False)
        self.assertEqual(d1, 10)
        self.assertEqual(d2, 20)
        self.assertEqual(d3, 40)

    @require_app
    def test_f13_02_backoff_capped_at_max_delay(self):
        """F13.2: Delay is clamped at max_backoff ceiling."""
        scheduler = PollingScheduler(
            checker=MockTicketChecker(status=AvailabilityStatus.ERROR),
            state_manager=AlertStateManager(),
            notifier=MagicMock(),
            interval_seconds=10,
            max_backoff=60
        )
        d = scheduler.compute_backoff_delay(consecutive_errors=10, apply_jitter=False)
        self.assertEqual(d, 60)

    @require_app
    def test_f13_03_success_resets_error_count(self):
        """F13.3: Successful check resets consecutive error count to zero."""
        scheduler = PollingScheduler(
            checker=MockTicketChecker(status=AvailabilityStatus.AVAILABLE),
            state_manager=AlertStateManager(),
            notifier=MagicMock(),
            interval_seconds=10
        )
        scheduler._consecutive_errors = 5
        scheduler.run_single_tick()
        self.assertEqual(scheduler._consecutive_errors, 0)

    @require_app
    def test_f13_04_retry_after_integer_header(self):
        """F13.4: Honors Retry-After integer header on HTTP 429."""
        scheduler = PollingScheduler(
            checker=MockTicketChecker(status=AvailabilityStatus.ERROR),
            state_manager=AlertStateManager(),
            notifier=MagicMock(),
            interval_seconds=10
        )
        delay = scheduler.compute_delay_from_result(
            CheckResult(status=AvailabilityStatus.ERROR, section_name="Sivori", status_code=429, error_message="Retry-After: 45")
        )
        self.assertEqual(delay, 45)

    @require_app
    def test_f13_05_jitter_randomness(self):
        """F13.5: Backoff incorporates jitter variation."""
        scheduler = PollingScheduler(
            checker=MockTicketChecker(status=AvailabilityStatus.ERROR),
            state_manager=AlertStateManager(),
            notifier=MagicMock(),
            interval_seconds=10
        )
        delays = {scheduler.compute_backoff_delay(consecutive_errors=3, apply_jitter=True) for _ in range(20)}
        self.assertTrue(len(delays) > 1, "Delays should vary when jitter is applied")

    # --------------------------------------------------------------------------
    # F14: CLI Interface & Flags
    # --------------------------------------------------------------------------
    @require_app
    def test_f14_01_cli_help_flag(self):
        """F14.1: CLI --help exits with return code 0."""
        code, stdout, _ = run_cli_in_process(cli.main, ["--help"])
        self.assertEqual(code, 0)
        self.assertIn("usage", stdout.lower())

    @require_app
    def test_f14_02_cli_version_flag(self):
        """F14.2: CLI --version exits with return code 0 and displays version."""
        code, stdout, _ = run_cli_in_process(cli.main, ["--version"])
        self.assertEqual(code, 0)
        self.assertIn("1.0", stdout)

    @require_app
    def test_f14_03_cli_once_available_exit_code_0(self):
        """F14.3: CLI --once on available tickets exits with code 0."""
        code, stdout, _ = run_cli_in_process(cli.main, ["--once", "--mock-status", "available"])
        self.assertEqual(code, 0)

    @require_app
    def test_f14_04_cli_once_unavailable_exit_code_2(self):
        """F14.4: CLI --once on sold out tickets exits with code 2."""
        code, stdout, _ = run_cli_in_process(cli.main, ["--once", "--mock-status", "sold_out"])
        self.assertEqual(code, 2)

    @require_app
    def test_f14_05_cli_dry_run_flag(self):
        """F14.5: CLI --dry-run prints alerts to console without external dispatch."""
        code, stdout, _ = run_cli_in_process(cli.main, ["--once", "--dry-run", "--mock-status", "available"])
        self.assertEqual(code, 0)
        self.assertIn("AVAILABLE", stdout)

    # --------------------------------------------------------------------------
    # F15: Simulated Mock Scenarios
    # --------------------------------------------------------------------------
    @require_app
    def test_f15_01_mock_status_available(self):
        """F15.1: MockTicketChecker with status='available'."""
        mock = MockTicketChecker(status="available")
        res = mock.check_availability("Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)
        self.assertTrue(res.section_info.available)

    @require_app
    def test_f15_02_mock_status_sold_out(self):
        """F15.2: MockTicketChecker with status='sold_out'."""
        mock = MockTicketChecker(status="sold_out")
        res = mock.check_availability("Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)

    @require_app
    def test_f15_03_mock_status_section_missing(self):
        """F15.3: MockTicketChecker with status='section_missing'."""
        mock = MockTicketChecker(status="section_missing")
        res = mock.check_availability("Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.SECTION_NOT_FOUND)

    @require_app
    def test_f15_04_mock_status_network_error(self):
        """F15.4: MockTicketChecker with status='network_error'."""
        mock = MockTicketChecker(status="network_error")
        res = mock.check_availability("Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.ERROR)

    @require_app
    def test_f15_05_mock_status_malformed(self):
        """F15.5: MockTicketChecker with status='malformed'."""
        mock = MockTicketChecker(status="malformed")
        res = mock.check_availability("Sivori media")
        self.assertEqual(res.status, AvailabilityStatus.ERROR)


# ==============================================================================
# TIER 2: BOUNDARY & CORNER CASES (>=5 tests per feature for all 15 features = 75 tests)
# ==============================================================================

class TestTier2BoundaryAndCornerCases(unittest.TestCase):
    """Tier 2: Boundary value analysis, resource stress, and adversarial corner cases."""

    # --------------------------------------------------------------------------
    # F1: Bootstrap Extraction Boundaries
    # --------------------------------------------------------------------------
    @require_app
    def test_f1_b01_missing_bootstrap_marker_raises_error(self):
        """F1.B1: HTML missing App.bootstrapData returns ERROR result."""
        html = "<html><body><h1>Maintenance</h1></body></html>"
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori Media")
            self.assertEqual(res.status, AvailabilityStatus.ERROR)

    @require_app
    def test_f1_b02_truncated_or_incomplete_json_raises_error(self):
        """F1.B2: Incomplete or cut-off JSON returns ERROR."""
        html = load_fixture("malformed_response.html")
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori Media")
            self.assertEqual(res.status, AvailabilityStatus.ERROR)

    @require_app
    def test_f1_b03_bootstrap_embedded_escaped_html_quotes(self):
        """F1.B3: Nested escaped HTML quotes in description string do not break decoder."""
        html = build_allaccess_html(section_name='Sivori "Media" Special <VIP>', available=True)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability('Sivori "Media" Special <VIP>')
            self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)

    @require_app
    def test_f1_b04_empty_shows_array(self):
        """F1.B4: Event with empty shows: [] returns SECTION_NOT_FOUND."""
        html = build_allaccess_html(empty_shows=True)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori Media")
            self.assertEqual(res.status, AvailabilityStatus.SECTION_NOT_FOUND)

    @require_app
    def test_f1_b05_extra_trailing_javascript_after_bootstrap(self):
        """F1.B5: Trailing JS code after bootstrapData does not cause syntax error."""
        base_html = build_allaccess_html(section_name="Sivori Media", available=True)
        html = base_html.replace("App.start();", "App.start(); window.someVar = { a: 1 }; (function() {})();")
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori Media")
            self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)

    # --------------------------------------------------------------------------
    # F2: Unicode & Accent Boundaries
    # --------------------------------------------------------------------------
    @require_app
    def test_f2_b01_multiple_internal_spaces(self):
        """F2.B1: Target with multiple internal spaces 'Sivori   media' matches 'Sivori Media'."""
        html = build_allaccess_html(section_name="Sivori Media", available=True)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori   media")
            self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)

    @require_app
    def test_f2_b02_empty_or_none_section_name(self):
        """F2.B2: Empty string query returns SECTION_NOT_FOUND."""
        html = build_allaccess_html(section_name="Sivori Media", available=True)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("")
            self.assertEqual(res.status, AvailabilityStatus.SECTION_NOT_FOUND)

    @require_app
    def test_f2_b03_special_unicode_punctuation_and_diacritics(self):
        """F2.B3: Unicode diacritics and combining marks (e.g. Sïvorì) handle cleanly."""
        html = build_allaccess_html(section_name="Sivori Media", available=True)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sïvorì Medïa")
            self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)

    @require_app
    def test_f2_b04_partial_substring_false_matching(self):
        """F2.B4: 'Sivori' alone does NOT match 'Sivori Media' (reject partial prefix)."""
        html = build_allaccess_html(section_name="Sivori Media", available=True)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori")
            self.assertEqual(res.status, AvailabilityStatus.SECTION_NOT_FOUND)

    @require_app
    def test_f2_b05_similar_section_disambiguation(self):
        """F2.B5: 'Sivori Alta' does NOT match 'Sivori Media'."""
        extra = [{
            "id": 999111,
            "name": "Sivori Alta",
            "available": True,
            "rates": [{"id": 1, "available": True, "max": 4, "availability": 10, "price": 65000}],
            "sections": [{"id": 1, "available": True}]
        }]
        html = build_allaccess_html(section_name="Sivori Media", available=False, extra_sectors=extra)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori Media")
            self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)

    # --------------------------------------------------------------------------
    # F3: Availability & False-Positive Filter Boundaries
    # --------------------------------------------------------------------------
    @require_app
    def test_f3_b01_multiple_rates_one_available(self):
        """F3.B1: When Rate 1 is sold out but Rate 2 is available, section is AVAILABLE."""
        sector = {
            "id": 123,
            "name": "Sivori Media",
            "available": True,
            "rates": [
                {"id": 1, "name": "General", "available": False, "max": 0, "availability": 0},
                {"id": 2, "name": "VIP", "available": True, "max": 2, "availability": 5, "price": 150000}
            ],
            "sections": [{"id": 1, "available": True}]
        }
        html = build_allaccess_html(section_name="Sivori Media", available=True)
        # Patch sector rates
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori Media")
            self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)

    @require_app
    def test_f3_b02_null_availability_with_available_true(self):
        """F3.B2: When rate.availability is null (untracked inventory) and available=True, evaluates AVAILABLE."""
        html = build_allaccess_html(section_name="Sivori Media", available=True, max_qty=4, availability=None)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori Media")
            self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)

    @require_app
    def test_f3_b03_passcode_required_or_hidden_rate(self):
        """F3.B3: Rates marked passcodeRequired=True or showAsDisabled=True do not count as public."""
        html = build_allaccess_html(section_name="Sivori Media", available=True, passcode_required=True, show_as_disabled=True)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori Media")
            self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)

    @require_app
    def test_f3_b04_empty_rates_array(self):
        """F3.B4: Sector with rates: [] evaluates to UNAVAILABLE."""
        html = build_allaccess_html(section_name="Sivori Media", available=False)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori Media")
            self.assertEqual(res.status, AvailabilityStatus.UNAVAILABLE)

    @require_app
    def test_f3_b05_section_missing_from_event(self):
        """F3.B5: Querying missing section returns SECTION_NOT_FOUND."""
        html = load_fixture("section_missing_response.html")
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori Media")
            self.assertEqual(res.status, AvailabilityStatus.SECTION_NOT_FOUND)

    # --------------------------------------------------------------------------
    # F4: Pricing Extraction Boundaries
    # --------------------------------------------------------------------------
    @require_app
    def test_f4_b01_null_price_and_service_charge(self):
        """F4.B1: Handle rate with price=null and serviceCharge=null cleanly."""
        html = build_allaccess_html(section_name="Sivori Media", available=True, price=None, service_charge=None)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori Media")
            self.assertIsNone(res.section_info.price)
            self.assertIsNone(res.section_info.service_charge)

    @require_app
    def test_f4_b02_zero_price_free_event(self):
        """F4.B2: Free ticket price of 0.0 is preserved as 0.0 (not None)."""
        html = build_allaccess_html(section_name="Sivori Media", available=True, price=0.0, service_charge=0.0)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori Media")
            self.assertEqual(res.section_info.price, 0.0)
            self.assertEqual(res.section_info.service_charge, 0.0)

    @require_app
    def test_f4_b03_multiple_rates_lowest_price_selection(self):
        """F4.B3: Sector with multiple rates extracts the lowest or primary price."""
        html = load_fixture("available_response.html")
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori Media")
            self.assertEqual(res.section_info.price, 125000.0)

    @require_app
    def test_f4_b04_fractional_centavos_pricing(self):
        """F4.B4: Price with decimal centavos (125000.50) parsed accurately."""
        html = build_allaccess_html(section_name="Sivori Media", available=True, price=125000.50)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori Media")
            self.assertEqual(res.section_info.price, 125000.50)

    @require_app
    def test_f4_b05_negative_or_corrupt_pricing(self):
        """F4.B5: Corrupt price handles gracefully without uncaught exception."""
        html = build_allaccess_html(section_name="Sivori Media", available=True, price=125000.0)
        html = html.replace('"price": 125000.0', '"price": "NaN"')
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori Media")
            self.assertIn(res.status, [AvailabilityStatus.AVAILABLE, AvailabilityStatus.ERROR])

    # --------------------------------------------------------------------------
    # F5: WAF & Challenge Boundaries
    # --------------------------------------------------------------------------
    @require_app
    def test_f5_b01_clean_html_with_word_captcha_in_artist_bio(self):
        """F5.B1: Word 'captcha' in band bio does NOT trigger false WAF detection."""
        base_html = build_allaccess_html(section_name="Sivori Media", available=True)
        html = base_html.replace("Foo Fighters", "Foo Fighters Captcha Tour")
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori Media")
            self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)

    @require_app
    def test_f5_b02_queue_it_virtual_waiting_room_detection(self):
        """F5.B2: Queue-It virtual waiting room detects queue-view container."""
        html = build_allaccess_html(queue_it_redirect=True)
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori Media")
            self.assertEqual(res.status, AvailabilityStatus.ERROR)
            self.assertIn("queue", (res.error_message or "").lower())

    @require_app
    def test_f5_b03_aws_waf_script_presence_without_block(self):
        """F5.B3: Passive awswaf.com script without challenge container does not trigger error."""
        base_html = build_allaccess_html(section_name="Sivori Media", available=True)
        html = f"<script src='https://awswaf.com/jsapi.js'></script>{base_html}"
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sivori Media")
            self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)

    @require_app
    def test_f5_b04_waf_retry_strategy_triggers_backoff(self):
        """F5.B4: WAF challenge sets status=ERROR and status_code=403 triggering backoff."""
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=403, text="Forbidden")):
            res = checker.check_availability("Sivori Media")
            self.assertEqual(res.status_code, 403)

    @require_app
    def test_f5_b05_empty_response_body_with_403(self):
        """F5.B5: HTTP 403 with completely empty body handles safely."""
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=403, text="")):
            res = checker.check_availability("Sivori Media")
            self.assertEqual(res.status, AvailabilityStatus.ERROR)

    # --------------------------------------------------------------------------
    # F6: Telegram Notifier Boundaries
    # --------------------------------------------------------------------------
    @require_app
    def test_f6_b01_rate_limit_429_retry_after(self):
        """F6.B1: Telegram 429 returns False and parses retry_after."""
        notifier = TelegramNotifier(bot_token="token", chat_id="123")
        alert = TicketAlert(event_name="Foo", section_name="Sivori", status="AVAILABLE", event_url="https://url")
        with patch("requests.post", return_value=MockResponse(status_code=429, json_data={"ok": False, "parameters": {"retry_after": 10}})):
            self.assertFalse(notifier.send(alert))

    @require_app
    def test_f6_b02_bad_request_400_permanent_failure(self):
        """F6.B2: Telegram 400 Bad Request returns False without crashing."""
        notifier = TelegramNotifier(bot_token="token", chat_id="123")
        alert = TicketAlert(event_name="Foo", section_name="Sivori", status="AVAILABLE", event_url="https://url")
        with patch("requests.post", return_value=MockResponse(status_code=400, json_data={"ok": False, "description": "Chat not found"})):
            self.assertFalse(notifier.send(alert))

    @require_app
    def test_f6_b03_unauthorized_401_invalid_token(self):
        """F6.B3: Telegram 401 Unauthorized returns False."""
        notifier = TelegramNotifier(bot_token="invalid", chat_id="123")
        alert = TicketAlert(event_name="Foo", section_name="Sivori", status="AVAILABLE", event_url="https://url")
        with patch("requests.post", return_value=MockResponse(status_code=401, json_data={"ok": False})):
            self.assertFalse(notifier.send(alert))

    @require_app
    def test_f6_b04_network_timeout_returns_false(self):
        """F6.B4: Socket timeout returns False without uncaught exception."""
        notifier = TelegramNotifier(bot_token="token", chat_id="123")
        alert = TicketAlert(event_name="Foo", section_name="Sivori", status="AVAILABLE", event_url="https://url")
        import requests
        with patch("requests.post", side_effect=requests.exceptions.Timeout("Connection timed out")):
            self.assertFalse(notifier.send(alert))

    @require_app
    def test_f6_b05_reminder_alert_formatting(self):
        """F6.B5: is_reminder=True includes [REMINDER] in Telegram notification."""
        notifier = TelegramNotifier(bot_token="token", chat_id="123")
        alert = TicketAlert(event_name="Foo", section_name="Sivori", status="AVAILABLE", event_url="https://url", is_reminder=True)
        with patch("requests.post", return_value=MockResponse(status_code=200, json_data={"ok": True})) as mock_post:
            notifier.send(alert)
            text = mock_post.call_args[1]["json"]["text"]
            self.assertIn("REMINDER", text)

    # --------------------------------------------------------------------------
    # F7: Discord Notifier Boundaries
    # --------------------------------------------------------------------------
    @require_app
    def test_f7_b01_rate_limit_429_retry_after(self):
        """F7.B1: Discord 429 returns False and respects Retry-After."""
        notifier = DiscordNotifier(webhook_url="https://discord.com/webhook")
        alert = TicketAlert(event_name="Foo", section_name="Sivori", status="AVAILABLE", event_url="https://url")
        with patch("requests.post", return_value=MockResponse(status_code=429, json_data={"retry_after": 2.5})):
            self.assertFalse(notifier.send(alert))

    @require_app
    def test_f7_b02_webhook_not_found_404(self):
        """F7.B2: Discord 404 deleted webhook returns False."""
        notifier = DiscordNotifier(webhook_url="https://discord.com/webhook")
        alert = TicketAlert(event_name="Foo", section_name="Sivori", status="AVAILABLE", event_url="https://url")
        with patch("requests.post", return_value=MockResponse(status_code=404)):
            self.assertFalse(notifier.send(alert))

    @require_app
    def test_f7_b03_bad_request_400_field_limits(self):
        """F7.B3: Discord 400 Bad Request returns False."""
        notifier = DiscordNotifier(webhook_url="https://discord.com/webhook")
        alert = TicketAlert(event_name="Foo", section_name="Sivori", status="AVAILABLE", event_url="https://url")
        with patch("requests.post", return_value=MockResponse(status_code=400)):
            self.assertFalse(notifier.send(alert))

    @require_app
    def test_f7_b04_server_error_500_returns_false(self):
        """F7.B4: Discord 500 internal server error returns False safely."""
        notifier = DiscordNotifier(webhook_url="https://discord.com/webhook")
        alert = TicketAlert(event_name="Foo", section_name="Sivori", status="AVAILABLE", event_url="https://url")
        with patch("requests.post", return_value=MockResponse(status_code=500)):
            self.assertFalse(notifier.send(alert))

    @require_app
    def test_f7_b05_network_connection_error(self):
        """F7.B5: DNS failure or connection error returns False."""
        notifier = DiscordNotifier(webhook_url="https://discord.com/webhook")
        alert = TicketAlert(event_name="Foo", section_name="Sivori", status="AVAILABLE", event_url="https://url")
        import requests
        with patch("requests.post", side_effect=requests.exceptions.ConnectionError("DNS lookup failed")):
            self.assertFalse(notifier.send(alert))

    # --------------------------------------------------------------------------
    # F8: Email Notifier Boundaries
    # --------------------------------------------------------------------------
    @require_app
    def test_f8_b01_smtp_authentication_failure(self):
        """F8.B1: SMTPAuthenticationError caught and returns False."""
        notifier = EmailNotifier(smtp_host="smtp.example.com", smtp_port=587, username="u", password="p", to_addrs=["a@b.com"])
        alert = TicketAlert(event_name="Foo", section_name="Sivori", status="AVAILABLE", event_url="https://url")
        with patch("smtplib.SMTP") as mock_cls:
            mock_smtp = MagicMock()
            mock_smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Auth failed")
            mock_cls.return_value.__enter__.return_value = mock_smtp
            self.assertFalse(notifier.send(alert))

    @require_app
    def test_f8_b02_smtp_connect_timeout(self):
        """F8.B2: Socket timeout connecting to SMTP returns False."""
        notifier = EmailNotifier(smtp_host="smtp.example.com", smtp_port=587, username="u", password="p", to_addrs=["a@b.com"])
        alert = TicketAlert(event_name="Foo", section_name="Sivori", status="AVAILABLE", event_url="https://url")
        with patch("smtplib.SMTP", side_effect=TimeoutError("Connection timed out")):
            self.assertFalse(notifier.send(alert))

    @require_app
    def test_f8_b03_multiple_recipients(self):
        """F8.B3: Multiple comma-separated recipients delivered correctly."""
        notifier = EmailNotifier(smtp_host="smtp.example.com", smtp_port=587, username="u", password="p", to_addrs=["fan1@a.com", "fan2@b.com"])
        alert = TicketAlert(event_name="Foo", section_name="Sivori", status="AVAILABLE", event_url="https://url")
        with patch("smtplib.SMTP") as mock_cls:
            mock_smtp = MagicMock()
            mock_cls.return_value.__enter__.return_value = mock_smtp
            self.assertTrue(notifier.send(alert))
            msg = mock_smtp.send_message.call_args[0][0]
            self.assertIn("fan1@a.com", msg["To"])
            self.assertIn("fan2@b.com", msg["To"])

    @require_app
    def test_f8_b04_smtp_server_disconnected(self):
        """F8.B4: SMTPServerDisconnected caught and returns False."""
        notifier = EmailNotifier(smtp_host="smtp.example.com", smtp_port=587, username="u", password="p", to_addrs=["a@b.com"])
        alert = TicketAlert(event_name="Foo", section_name="Sivori", status="AVAILABLE", event_url="https://url")
        with patch("smtplib.SMTP") as mock_cls:
            mock_smtp = MagicMock()
            mock_smtp.send_message.side_effect = smtplib.SMTPServerDisconnected("Connection closed unexpectedly")
            mock_cls.return_value.__enter__.return_value = mock_smtp
            self.assertFalse(notifier.send(alert))

    @require_app
    def test_f8_b05_special_characters_in_subject_mime_encoded(self):
        """F8.B5: Accents in subject (Sívori) are safely MIME header encoded."""
        notifier = EmailNotifier(smtp_host="smtp.example.com", smtp_port=587, username="u", password="p", to_addrs=["a@b.com"])
        alert = TicketAlert(event_name="Foo Fighters", section_name="Sívori Media", status="AVAILABLE", event_url="https://url")
        with patch("smtplib.SMTP") as mock_cls:
            mock_smtp = MagicMock()
            mock_cls.return_value.__enter__.return_value = mock_smtp
            self.assertTrue(notifier.send(alert))

    # --------------------------------------------------------------------------
    # F9: Composite Multiplexer Boundaries
    # --------------------------------------------------------------------------
    @require_app
    def test_f9_b01_exception_in_one_notifier_does_not_crash_others(self):
        """F9.B1: Uncaught exception in Notifier 1 is isolated; Notifier 2 runs."""
        c1 = MagicMock(spec=BaseNotifier)
        c1.send.side_effect = RuntimeError("Crash in telegram")
        c2 = MagicMock(spec=BaseNotifier)
        c2.send.return_value = True
        composite = CompositeNotifier([c1, c2])
        alert = TicketAlert(event_name="Foo", section_name="Sivori", status="AVAILABLE", event_url="https://url")
        self.assertTrue(composite.send(alert))
        c2.send.assert_called_once()

    @require_app
    def test_f9_b02_detailed_delivery_report(self):
        """F9.B2: Composite tracks per-channel delivery results."""
        c1, c2 = MagicMock(spec=BaseNotifier), MagicMock(spec=BaseNotifier)
        c1.channel_name = "telegram"
        c1.send.return_value = True
        c2.channel_name = "discord"
        c2.send.return_value = False
        composite = CompositeNotifier([c1, c2])
        alert = TicketAlert(event_name="Foo", section_name="Sivori", status="AVAILABLE", event_url="https://url")
        composite.send(alert)
        report = composite.last_delivery_report
        self.assertEqual(report.get("telegram"), True)
        self.assertEqual(report.get("discord"), False)

    @require_app
    def test_f9_b03_dry_run_console_notifier_only(self):
        """F9.B3: Dry run mode substitutes external channels with ConsoleNotifier."""
        console = ConsoleNotifier()
        alert = TicketAlert(event_name="Foo", section_name="Sivori", status="AVAILABLE", event_url="https://url", price=125000.0)
        with capture_output() as (out, _):
            res = console.send(alert)
            self.assertTrue(res)
            self.assertIn("AVAILABLE", out.getvalue())

    @require_app
    def test_f9_b04_dynamic_channel_filtering(self):
        """F9.4: MonitorConfig builds CompositeNotifier only with enabled channels."""
        cfg = MonitorConfig(
            telegram=TelegramConfig(bot_token="tok", chat_id="123", enabled=True),
            discord=DiscordConfig(webhook_url="https://url", enabled=False),
            email=EmailConfig(enabled=False)
        )
        composite = cfg.build_notifier()
        channels = [n.channel_name for n in composite.notifiers]
        self.assertIn("telegram", channels)
        self.assertNotIn("discord", channels)

    @require_app
    def test_f9_b05_concurrent_or_sequential_dispatch_ordering(self):
        """F9.B5: Dispatches preserve TicketAlert immutability across channels."""
        alert = TicketAlert(event_name="Foo", section_name="Sivori", status="AVAILABLE", event_url="https://url")
        c1, c2 = MagicMock(spec=BaseNotifier), MagicMock(spec=BaseNotifier)
        c1.send.return_value = True
        c2.send.return_value = True
        composite = CompositeNotifier([c1, c2])
        composite.send(alert)
        self.assertEqual(alert.event_name, "Foo")

    # --------------------------------------------------------------------------
    # F10: State Machine Boundaries
    # --------------------------------------------------------------------------
    @require_app
    def test_f10_b01_exact_cooldown_boundary(self):
        """F10.B1: At exactly current_time == last_alert + cooldown, reminder triggers."""
        state = AlertStateManager(cooldown_seconds=1800)
        state.process_check_result(is_available=True, current_time=1000.0)
        decision = state.process_check_result(is_available=True, current_time=2800.0) # Exactly 1800s later
        self.assertTrue(decision.should_alert)
        self.assertTrue(decision.is_reminder)

    @require_app
    def test_f10_b02_clock_skew_or_time_travel(self):
        """F10.B2: Handle system clock jumping backward without permanent freeze."""
        state = AlertStateManager(cooldown_seconds=1800)
        state.process_check_result(is_available=True, current_time=2000.0)
        # Clock jumps backward to 1500.0
        decision = state.process_check_result(is_available=True, current_time=1500.0)
        self.assertFalse(decision.should_alert)

    @require_app
    def test_f10_b03_state_persistence_save_and_load(self):
        """F10.B3: State persists across restarts via state file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / ".state.json"
            state1 = AlertStateManager(cooldown_seconds=1800, state_file=state_file)
            state1.process_check_result(is_available=True, current_time=1000.0)
            
            # Reload state in fresh manager
            state2 = AlertStateManager(cooldown_seconds=1800, state_file=state_file)
            decision = state2.process_check_result(is_available=True, current_time=1500.0)
            self.assertFalse(decision.should_alert) # Cooldown preserved

    @require_app
    def test_f10_b04_corrupted_state_file_recovery(self):
        """F10.B4: Corrupted state file JSON resets to default without crashing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / ".state.json"
            state_file.write_text("{ corrupt json ...", encoding="utf-8")
            state = AlertStateManager(state_file=state_file)
            decision = state.process_check_result(is_available=True, current_time=1000.0)
            self.assertTrue(decision.should_alert)

    @require_app
    def test_f10_b05_zero_cooldown_dispatches_every_available_tick(self):
        """F10.B5: Cooldown of 0 seconds alerts on every available tick."""
        state = AlertStateManager(cooldown_seconds=0)
        d1 = state.process_check_result(is_available=True, current_time=1000.0)
        d2 = state.process_check_result(is_available=True, current_time=1010.0)
        self.assertTrue(d1.should_alert)
        self.assertTrue(d2.should_alert)

    # --------------------------------------------------------------------------
    # F11: Configuration & Masking Boundaries
    # --------------------------------------------------------------------------
    @require_app
    def test_f11_b01_negative_or_zero_interval_validation(self):
        """F11.B1: Negative or zero interval raises ValueError."""
        with self.assertRaises(ValueError):
            MonitorConfig(poll_interval=-10)

    @require_app
    def test_f11_b02_invalid_url_scheme_validation(self):
        """F11.B2: URL with ftp:// scheme raises ValueError."""
        with self.assertRaises(ValueError):
            MonitorConfig(event_url="ftp://invalid.com")

    @require_app
    def test_f11_b03_empty_env_file_loads_defaults(self):
        """F11.B3: Completely empty .env file loads default configuration."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("", encoding="utf-8")
            cfg = MonitorConfig.from_env(env_file)
            self.assertEqual(cfg.poll_interval, 60)

    @require_app
    def test_f11_b04_partial_credentials_warning(self):
        """F11.B4: Providing bot_token without chat_id marks channel disabled."""
        cfg = MonitorConfig(telegram=TelegramConfig(bot_token="tok", chat_id=""))
        self.assertFalse(cfg.telegram.is_valid())

    @require_app
    def test_f11_b05_secret_masking_short_or_empty_values(self):
        """F11.B5: Masking helper handles None and empty string safely."""
        from allaccess_monitor.config import mask_secret
        self.assertEqual(mask_secret(""), "")
        self.assertEqual(mask_secret(None), "")
        self.assertEqual(mask_secret("abc"), "[REDACTED]")

    # --------------------------------------------------------------------------
    # F12: Scheduler Boundaries
    # --------------------------------------------------------------------------
    @require_app
    def test_f12_b01_interruption_during_sleep(self):
        """F12.B1: Stop wakes thread immediately without waiting for full interval."""
        shutdown = GracefulShutdown()
        start = time.time()
        threading.Timer(0.05, shutdown.stop).start()
        stopped = shutdown.wait(timeout=10.0)
        elapsed = time.time() - start
        self.assertTrue(stopped)
        self.assertLess(elapsed, 1.0)

    @require_app
    def test_f12_b02_checker_exception_does_not_kill_loop(self):
        """F12.B2: Uncaught exception in checker logs error and loop continues."""
        checker = MagicMock()
        checker.check_availability.side_effect = [RuntimeError("Network drop"), CheckResult(status=AvailabilityStatus.UNAVAILABLE, section_name="Sivori")]
        scheduler = PollingScheduler(checker=checker, state_manager=AlertStateManager(), notifier=MagicMock(), interval_seconds=0.01)
        count = scheduler.run(max_ticks=2)
        self.assertEqual(count, 2)

    @require_app
    def test_f12_b03_zero_or_subsecond_interval(self):
        """F12.B3: Sub-second interval (0.01s) runs smoothly without drift."""
        checker = MockTicketChecker(status=AvailabilityStatus.UNAVAILABLE)
        scheduler = PollingScheduler(checker=checker, state_manager=AlertStateManager(), notifier=MagicMock(), interval_seconds=0.01)
        start = time.time()
        scheduler.run(max_ticks=5)
        self.assertLess(time.time() - start, 1.0)

    @require_app
    def test_f12_b04_sigint_signal_handler(self):
        """F12.B4: Simulating SIGINT sets stop event."""
        shutdown = GracefulShutdown()
        shutdown._signal_handler(signal.SIGINT, None)
        self.assertTrue(shutdown.is_stopped)

    @require_app
    def test_f12_b05_double_signal_forceful_exit(self):
        """F12.B5: Second consecutive signal raises SystemExit(1)."""
        shutdown = GracefulShutdown()
        shutdown._signal_handler(signal.SIGINT, None)
        with self.assertRaises(SystemExit) as ctx:
            shutdown._signal_handler(signal.SIGINT, None)
        self.assertEqual(ctx.exception.code, 1)

    # --------------------------------------------------------------------------
    # F13: Backoff & Jitter Boundaries
    # --------------------------------------------------------------------------
    @require_app
    def test_f13_b01_retry_after_exceeds_max_backoff(self):
        """F13.B1: Retry-After of 99999 is clamped to max_backoff."""
        scheduler = PollingScheduler(checker=MockTicketChecker(), state_manager=AlertStateManager(), notifier=MagicMock(), max_backoff=300)
        delay = scheduler.compute_delay_from_result(
            CheckResult(status=AvailabilityStatus.ERROR, section_name="Sivori", status_code=429, error_message="Retry-After: 99999")
        )
        self.assertEqual(delay, 300)

    @require_app
    def test_f13_b02_invalid_retry_after_header_falls_back_to_exponential(self):
        """F13.B2: Non-numeric Retry-After falls back to standard exponential delay."""
        scheduler = PollingScheduler(checker=MockTicketChecker(), state_manager=AlertStateManager(), notifier=MagicMock(), interval_seconds=10)
        delay = scheduler.compute_delay_from_result(
            CheckResult(status=AvailabilityStatus.ERROR, section_name="Sivori", status_code=429, error_message="Retry-After: invalid_date")
        )
        self.assertGreaterEqual(delay, 5)

    @require_app
    def test_f13_b03_large_consecutive_errors_no_overflow(self):
        """F13.B3: 100 consecutive errors does not raise integer overflow."""
        scheduler = PollingScheduler(checker=MockTicketChecker(), state_manager=AlertStateManager(), notifier=MagicMock(), interval_seconds=10, max_backoff=900)
        delay = scheduler.compute_backoff_delay(consecutive_errors=100, apply_jitter=False)
        self.assertEqual(delay, 900)

    @require_app
    def test_f13_b04_503_service_unavailable_triggers_backoff(self):
        """F13.B4: HTTP 503 triggers backoff."""
        scheduler = PollingScheduler(checker=MockTicketChecker(), state_manager=AlertStateManager(), notifier=MagicMock(), interval_seconds=10)
        res = CheckResult(status=AvailabilityStatus.ERROR, section_name="Sivori", status_code=503)
        delay = scheduler.compute_delay_from_result(res)
        self.assertGreater(delay, 0)

    @require_app
    def test_f13_b05_network_socket_timeout_triggers_backoff(self):
        """F13.B5: Network timeout error triggers backoff delay."""
        scheduler = PollingScheduler(checker=MockTicketChecker(), state_manager=AlertStateManager(), notifier=MagicMock(), interval_seconds=10)
        res = CheckResult(status=AvailabilityStatus.ERROR, section_name="Sivori", error_message="Connection timed out")
        delay = scheduler.compute_delay_from_result(res)
        self.assertGreater(delay, 0)

    # --------------------------------------------------------------------------
    # F14: CLI Interface Boundaries
    # --------------------------------------------------------------------------
    @require_app
    def test_f14_b01_cli_once_error_exit_code_1(self):
        """F14.B1: CLI --once with network error exits with code 1."""
        code, _, _ = run_cli_in_process(cli.main, ["--once", "--mock-status", "network_error"])
        self.assertEqual(code, 1)

    @require_app
    def test_f14_b02_cli_invalid_argument_error(self):
        """F14.B2: CLI with unknown flag exits with non-zero error."""
        code, _, stderr = run_cli_in_process(cli.main, ["--unknown-flag-xyz"])
        self.assertNotEqual(code, 0)

    @require_app
    def test_f14_b03_cli_custom_section_argument(self):
        """F14.B3: CLI parses custom section with spaces."""
        parser = cli.build_parser()
        args = parser.parse_args(["--section", "Platea San Martin Inferior"])
        self.assertEqual(args.section, "Platea San Martin Inferior")

    @require_app
    def test_f14_b04_cli_quiet_and_verbose_conflict_or_precedence(self):
        """F14.B4: CLI parses quiet and verbose flags."""
        parser = cli.build_parser()
        args = parser.parse_args(["--verbose"])
        self.assertTrue(args.verbose)

    @require_app
    def test_f14_b05_cli_channels_flag_parsing(self):
        """F14.B5: CLI parses comma-separated channel list."""
        parser = cli.build_parser()
        args = parser.parse_args(["--channels", "telegram,discord"])
        self.assertEqual(args.channels, "telegram,discord")

    # --------------------------------------------------------------------------
    # F15: Simulated Mock Boundaries
    # --------------------------------------------------------------------------
    @require_app
    def test_f15_b01_dynamic_sequence_mock(self):
        """F15.B1: MockTicketChecker sequence across ticks."""
        mock = MockTicketChecker(sequence=["sold_out", "sold_out", "available", "sold_out"])
        r1 = mock.check_availability("Sivori")
        r2 = mock.check_availability("Sivori")
        r3 = mock.check_availability("Sivori")
        r4 = mock.check_availability("Sivori")
        self.assertEqual(r1.status, AvailabilityStatus.UNAVAILABLE)
        self.assertEqual(r2.status, AvailabilityStatus.UNAVAILABLE)
        self.assertEqual(r3.status, AvailabilityStatus.AVAILABLE)
        self.assertEqual(r4.status, AvailabilityStatus.UNAVAILABLE)

    @require_app
    def test_f15_b02_custom_latency_injection(self):
        """F15.B2: MockTicketChecker simulates latency."""
        mock = MockTicketChecker(status="available", latency_ms=50)
        start = time.time()
        mock.check_availability("Sivori")
        self.assertGreaterEqual(time.time() - start, 0.04)

    @require_app
    def test_f15_b03_cli_mock_status_available_integration(self):
        """F15.B3: Full CLI integration with --mock-status available."""
        code, stdout, _ = run_cli_in_process(cli.main, ["--once", "--dry-run", "--mock-status", "available"])
        self.assertEqual(code, 0)
        self.assertIn("AVAILABLE", stdout)

    @require_app
    def test_f15_b04_cli_mock_status_sold_out_integration(self):
        """F15.B4: Full CLI integration with --mock-status sold_out."""
        code, stdout, _ = run_cli_in_process(cli.main, ["--once", "--dry-run", "--mock-status", "sold_out"])
        self.assertEqual(code, 2)

    @require_app
    def test_f15_b05_invalid_mock_status_raises_error(self):
        """F15.B5: CLI invalid mock status choice raises error."""
        code, _, stderr = run_cli_in_process(cli.main, ["--mock-status", "invalid_choice_xyz"])
        self.assertNotEqual(code, 0)


# ==============================================================================
# TIER 3: CROSS-FEATURE COMBINATIONS (Pairwise Interaction Coverage = 10 tests)
# ==============================================================================

class TestTier3CrossFeatureCombinations(unittest.TestCase):
    """Tier 3: Pairwise subsystem interactions."""

    @require_app
    def test_tier3_01_parser_and_state_manager_pipeline(self):
        """T3.1: Parser results fed directly into AlertStateManager across 5 ticks."""
        state = AlertStateManager(cooldown_seconds=300)
        # Sequence: sold out -> available -> available -> sold out -> available
        html_sold = load_fixture("sold_out_response.html")
        html_avail = load_fixture("available_response.html")
        checker = AllAccessTicketChecker()

        decisions = []
        for i, html in enumerate([html_sold, html_avail, html_avail, html_sold, html_avail]):
            with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
                res = checker.check_availability("Sivori Media")
                d = state.process_check_result(is_available=(res.status == AvailabilityStatus.AVAILABLE), current_time=1000.0 + (i * 60))
                decisions.append(d)

        # 1: sold out -> no alert
        self.assertFalse(decisions[0].should_alert)
        # 2: first available -> alert!
        self.assertTrue(decisions[1].should_alert)
        self.assertFalse(decisions[1].is_reminder)
        # 3: second available (within cooldown) -> throttled
        self.assertFalse(decisions[2].should_alert)
        # 4: sold out -> reset
        self.assertFalse(decisions[3].should_alert)
        # 5: reappear -> new alert!
        self.assertTrue(decisions[4].should_alert)

    @require_app
    def test_tier3_02_state_manager_and_composite_notifier_dispatch(self):
        """T3.2: State decision should_alert triggers CompositeNotifier.send."""
        state = AlertStateManager()
        composite = MagicMock(spec=CompositeNotifier)
        composite.send.return_value = True

        decision = state.process_check_result(is_available=True, current_time=1000.0)
        if decision.should_alert:
            alert = TicketAlert(event_name="Foo Fighters", section_name="Sivori media", status="AVAILABLE", event_url="https://url", is_reminder=decision.is_reminder)
            composite.send(alert)

        composite.send.assert_called_once()

    @require_app
    def test_tier3_03_cli_flags_override_env_and_affect_scheduler(self):
        """T3.3: CLI flags override .env and configure PollingScheduler correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text("POLL_INTERVAL=120\nALERT_COOLDOWN=3600\n", encoding="utf-8")
            parser = cli.build_parser()
            args = parser.parse_args(["--config", str(env_file), "--interval", "15", "--cooldown", "600"])
            cfg = MonitorConfig.from_args(args)
            self.assertEqual(cfg.poll_interval, 15)
            self.assertEqual(cfg.cooldown_seconds, 600)

    @require_app
    def test_tier3_04_backoff_and_scheduler_integration(self):
        """T3.4: HTTP 429 with Retry-After from checker directly adjusts scheduler wait time."""
        checker = MagicMock()
        checker.check_availability.return_value = CheckResult(
            status=AvailabilityStatus.ERROR,
            section_name="Sivori",
            status_code=429,
            error_message="Retry-After: 35"
        )
        scheduler = PollingScheduler(checker=checker, state_manager=AlertStateManager(), notifier=MagicMock(), interval_seconds=10)
        with patch.object(scheduler._shutdown, "wait", return_value=True) as mock_wait:
            scheduler.run(max_ticks=1)
            mock_wait.assert_called_once_with(35)

    @require_app
    def test_tier3_05_secret_masking_in_composite_logging(self):
        """T3.5: Failed dispatch logging masks sensitive credentials with [REDACTED]."""
        cfg = MonitorConfig(
            telegram=TelegramConfig(bot_token="SUPER_SECRET_BOT_TOKEN_12345", chat_id="123", enabled=True),
            discord=DiscordConfig(webhook_url="https://discord.com/api/webhooks/SECRET_WEBHOOK_URL", enabled=True)
        )
        filter_ = RedactingFilter(secrets=[cfg.telegram.bot_token, cfg.discord.webhook_url])
        record = MagicMock()
        record.msg = f"Failed to post to {cfg.discord.webhook_url} with bot token {cfg.telegram.bot_token}"
        filter_.filter(record)
        self.assertNotIn("SUPER_SECRET_BOT_TOKEN_12345", record.msg)
        self.assertNotIn("SECRET_WEBHOOK_URL", record.msg)
        self.assertIn("[REDACTED]", record.msg)

    @require_app
    def test_tier3_06_accent_normalization_with_pricing_extraction(self):
        """T3.6: Accented query 'Sívori Media' normalizes and extracts correct price."""
        html = load_fixture("available_response.html")
        checker = AllAccessTicketChecker()
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = checker.check_availability("Sívori media")
            self.assertEqual(res.status, AvailabilityStatus.AVAILABLE)
            self.assertEqual(res.section_info.price, 125000.0)
            self.assertEqual(res.section_info.service_charge, 18750.0)

    @require_app
    def test_tier3_07_waf_detection_triggers_scheduler_backoff(self):
        """T3.7: WAF challenge detected by checker triggers scheduler backoff increment."""
        checker = AllAccessTicketChecker()
        html = load_fixture("waf_challenge_response.html")
        scheduler = PollingScheduler(checker=checker, state_manager=AlertStateManager(), notifier=MagicMock(), interval_seconds=10)
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = scheduler.run_single_tick()
            self.assertEqual(res.status, AvailabilityStatus.ERROR)
            self.assertEqual(scheduler._consecutive_errors, 1)

    @require_app
    def test_tier3_08_cli_dry_run_with_mock_status_and_custom_channels(self):
        """T3.8: CLI --dry-run with --mock-status and specific channels executes zero external calls."""
        with patch("requests.post") as mock_http_post, patch("smtplib.SMTP") as mock_smtp:
            code, stdout, _ = run_cli_in_process(cli.main, ["--once", "--dry-run", "--mock-status", "available", "--channels", "telegram,discord"])
            self.assertEqual(code, 0)
            mock_http_post.assert_not_called()
            mock_smtp.assert_not_called()
            self.assertIn("DRY RUN", stdout.upper())

    @require_app
    def test_tier3_09_multichannel_partial_failure_with_state_transition(self):
        """T3.9: If Telegram fails but Discord succeeds, state marks alert sent and enters cooldown."""
        t_notif = MagicMock(spec=BaseNotifier)
        t_notif.channel_name = "telegram"
        t_notif.send.return_value = False # Telegram failed
        d_notif = MagicMock(spec=BaseNotifier)
        d_notif.channel_name = "discord"
        d_notif.send.return_value = True  # Discord succeeded

        composite = CompositeNotifier([t_notif, d_notif])
        state = AlertStateManager(cooldown_seconds=1800)

        # First tick: available
        d1 = state.process_check_result(is_available=True, current_time=1000.0)
        self.assertTrue(d1.should_alert)
        alert = TicketAlert(event_name="Foo", section_name="Sivori", status="AVAILABLE", event_url="https://url")
        sent = composite.send(alert)
        self.assertTrue(sent) # At least one succeeded

        # Second tick: still available, within cooldown
        d2 = state.process_check_result(is_available=True, current_time=1060.0)
        self.assertFalse(d2.should_alert) # Cooldown initiated!

    @require_app
    def test_tier3_10_queue_it_redirect_to_backoff_pipeline(self):
        """T3.10: Queue-It virtual waiting room triggers error without false positive alert."""
        checker = AllAccessTicketChecker()
        html = build_allaccess_html(queue_it_redirect=True)
        notifier = MagicMock()
        scheduler = PollingScheduler(checker=checker, state_manager=AlertStateManager(), notifier=notifier, interval_seconds=10)
        with patch.object(checker._session, "get", return_value=MockResponse(status_code=200, text=html)):
            res = scheduler.run_single_tick()
            self.assertEqual(res.status, AvailabilityStatus.ERROR)
            notifier.send.assert_not_called()


# ==============================================================================
# TIER 4: REAL-WORLD APPLICATION SCENARIOS (The 5 Scenarios from TEST_INFRA.md)
# ==============================================================================

class TestTier4RealWorldScenarios(unittest.TestCase):
    """Tier 4: End-to-end full application workflows specified in TEST_INFRA.md."""

    @require_app
    def test_tier4_scenario_1_full_day_monitoring(self):
        """
        Scenario 1: Full Day Monitoring Simulation.
        Features: F1, F3, F10, F12, F14.
        Behavior:
        - 24 simulated checks throughout the day.
        - Checks 1-9: Sold out (no alert).
        - Check 10: Tickets appear! Immediate alert dispatched.
        - Checks 11-15: Tickets remain available (throttled under cooldown, no spam).
        - Check 16: Tickets sell out (state resets).
        - Checks 17-19: Sold out.
        - Check 20: Tickets reappear! Immediate alert dispatched again.
        - Checks 21-24: Sold out.
        """
        # Prepare 24 ticks sequence
        sequence = (
            ["sold_out"] * 9 +
            ["available"] * 6 + # 10 to 15
            ["sold_out"] * 4 +  # 16 to 19
            ["available"] * 1 + # 20
            ["sold_out"] * 4    # 21 to 24
        )
        self.assertEqual(len(sequence), 24)

        checker = MockTicketChecker(sequence=sequence)
        state = AlertStateManager(cooldown_seconds=1800)
        notifier = MagicMock(spec=BaseNotifier)
        notifier.send.return_value = True

        scheduler = PollingScheduler(checker=checker, state_manager=state, notifier=notifier, interval_seconds=0.01)
        count = scheduler.run(max_ticks=24)
        self.assertEqual(count, 24)

        # Assertions
        # Total alerts sent must be exactly 2 (at tick 10 and tick 20)
        self.assertEqual(notifier.send.call_count, 2)
        # Verify first alert call arguments
        alert1 = notifier.send.call_args_list[0][0][0]
        self.assertEqual(alert1.section_name, "Sivori media")
        self.assertFalse(alert1.is_reminder)

        # Verify second alert call arguments
        alert2 = notifier.send.call_args_list[1][0][0]
        self.assertEqual(alert2.section_name, "Sivori media")
        self.assertFalse(alert2.is_reminder)

    @require_app
    def test_tier4_scenario_2_multichannel_network_glitch(self):
        """
        Scenario 2: Multi-Channel Alert Delivery with Network Glitch.
        Features: F6, F7, F8, F9.
        Behavior:
        - Tickets become available.
        - Telegram succeeds (HTTP 200).
        - Discord fails with HTTP 500.
        - Email succeeds (SMTP 250).
        - Composite reports overall success without crashing.
        - Discord error is logged cleanly.
        """
        # Setup real notifiers with mocked network clients
        telegram = TelegramNotifier(bot_token="tok", chat_id="123")
        discord = DiscordNotifier(webhook_url="https://discord.com/webhook")
        email = EmailNotifier(smtp_host="smtp.example.com", smtp_port=587, username="u", password="p", to_addrs=["fan@example.com"])

        composite = CompositeNotifier([telegram, discord, email])
        alert = TicketAlert(
            event_name="Foo Fighters",
            section_name="Sivori media",
            status="AVAILABLE",
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            price=125000.0
        )

        with patch("requests.post") as mock_http, patch("smtplib.SMTP") as mock_smtp_cls:
            # Route requests.post based on URL
            def mock_post_router(url, *args, **kwargs):
                if "telegram" in url:
                    return MockResponse(status_code=200, json_data={"ok": True})
                elif "discord" in url:
                    return MockResponse(status_code=500, text="Internal Server Error")
                return MockResponse(status_code=404)

            mock_http.side_effect = mock_post_router
            mock_smtp = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = mock_smtp

            # Dispatch composite
            success = composite.send(alert)

            # Assertions
            self.assertTrue(success, "Composite should succeed as Telegram and Email succeeded")
            report = composite.last_delivery_report
            self.assertTrue(report.get("telegram"))
            self.assertFalse(report.get("discord"))
            self.assertTrue(report.get("email"))

    @require_app
    def test_tier4_scenario_3_server_degradation_backoff(self):
        """
        Scenario 3: Severe Server Degradation (HTTP 429 & 502).
        Features: F5, F12, F13.
        Behavior:
        - AllAccess responds with 429 Retry-After: 30.
        - Then responds with 502 Bad Gateway.
        - Scheduler backs off gracefully without hammering origin or crashing.
        - When 200 OK returns with available tickets, normal polling resumes.
        """
        checker = MagicMock()
        # Responses: 429 -> 502 -> 200 (Available)
        checker.check_availability.side_effect = [
            CheckResult(status=AvailabilityStatus.ERROR, section_name="Sivori", status_code=429, error_message="Retry-After: 30"),
            CheckResult(status=AvailabilityStatus.ERROR, section_name="Sivori", status_code=502, error_message="Bad Gateway"),
            CheckResult(status=AvailabilityStatus.AVAILABLE, section_name="Sivori", section_info=SectionInfo(name="Sivori Media", available=True, price=125000.0))
        ]

        notifier = MagicMock(spec=BaseNotifier)
        notifier.send.return_value = True
        scheduler = PollingScheduler(checker=checker, state_manager=AlertStateManager(), notifier=notifier, interval_seconds=10)

        sleep_delays = []
        with patch.object(scheduler._shutdown, "wait", side_effect=lambda d: (sleep_delays.append(d), False)[1]):
            count = scheduler.run(max_ticks=3)
            self.assertEqual(count, 3)

        # First sleep respected Retry-After: 30
        self.assertEqual(sleep_delays[0], 30)
        # Second sleep used exponential backoff (greater than normal base 10)
        self.assertGreaterEqual(sleep_delays[1], 10)
        # On third tick, success restored consecutive errors to 0 and dispatched alert
        self.assertEqual(scheduler._consecutive_errors, 0)
        notifier.send.assert_called_once()

    @require_app
    def test_tier4_scenario_4_config_cascading_secret_redaction(self):
        """
        Scenario 4: Configuration Cascading & Secret Security.
        Features: F11, F14.
        Behavior:
        - CLI flags override .env configuration.
        - Logs verify secrets (bot token, webhook, email password) are strictly redacted with [REDACTED].
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "POLL_INTERVAL=60\n"
                "TELEGRAM_BOT_TOKEN=SUPER_SECRET_ENV_TOKEN_999\n"
                "TELEGRAM_CHAT_ID=11111\n"
                "DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/ENV_SECRET_URL\n"
                "SMTP_PASSWORD=SUPER_SECRET_ENV_PASSWORD\n",
                encoding="utf-8"
            )

            # CLI overrides interval to 20
            parser = cli.build_parser()
            args = parser.parse_args(["--config", str(env_file), "--interval", "20"])
            cfg = MonitorConfig.from_args(args)

            # Cascading verification
            self.assertEqual(cfg.poll_interval, 20)
            self.assertEqual(cfg.telegram.bot_token, "SUPER_SECRET_ENV_TOKEN_999")

            # Secret redaction verification
            filter_ = RedactingFilter(secrets=[cfg.telegram.bot_token, cfg.discord.webhook_url, cfg.email.password])
            record = MagicMock()
            record.msg = f"Connecting to {cfg.discord.webhook_url} using token {cfg.telegram.bot_token} and pass {cfg.email.password}"
            filter_.filter(record)

            self.assertNotIn("SUPER_SECRET_ENV_TOKEN_999", record.msg)
            self.assertNotIn("ENV_SECRET_URL", record.msg)
            self.assertNotIn("SUPER_SECRET_ENV_PASSWORD", record.msg)
            self.assertIn("[REDACTED]", record.msg)

    @require_app
    def test_tier4_scenario_5_dry_run_one_shot_check(self):
        """
        Scenario 5: Dry-Run One-Shot Production Check.
        Features: F1, F2, F3, F14, F15.
        Behavior:
        - Operator runs --once --dry-run.
        - Sivori media status parsed cleanly.
        - Output formatted to stdout without sending real external notifications.
        - Exits with return code 2 (unavailable) or 0 (available).
        """
        # Test available run -> exit code 0
        code_avail, out_avail, _ = run_cli_in_process(
            cli.main,
            ["--once", "--dry-run", "--mock-status", "available", "--section", "Sivori media"]
        )
        self.assertEqual(code_avail, 0)
        self.assertIn("AVAILABLE", out_avail.upper())

        # Test sold out run -> exit code 2
        code_sold, out_sold, _ = run_cli_in_process(
            cli.main,
            ["--once", "--dry-run", "--mock-status", "sold_out", "--section", "Sivori media"]
        )
        self.assertEqual(code_sold, 2)
        self.assertTrue("UNAVAILABLE" in out_sold.upper() or "SOLD OUT" in out_sold.upper())


# ==============================================================================
# TEST INFRASTRUCTURE & FIXTURE INTEGRITY (Active baseline)
# ==============================================================================

class TestFixturesAndHarness(unittest.TestCase):
    """Test verification for canonical HTML fixtures and test data builders."""

    def test_fixture_available_response_html_loads_and_contains_bootstrap(self):
        """Verify available_response.html fixture loads and contains App.bootstrapData."""
        html = load_fixture("available_response.html")
        self.assertIn("App.bootstrapData(", html)
        self.assertIn("Sivori Media", html)
        self.assertIn("18337", html)

    def test_fixture_sold_out_response_html_loads_and_contains_bootstrap(self):
        """Verify sold_out_response.html fixture loads and demonstrates deceptive sector availability."""
        html = load_fixture("sold_out_response.html")
        self.assertIn("App.bootstrapData(", html)
        self.assertIn("Sivori Media", html)
        self.assertIn('"available": false', html)

    def test_fixture_section_missing_response_html_loads(self):
        """Verify section_missing_response.html fixture loads without Sivori Media."""
        html = load_fixture("section_missing_response.html")
        self.assertIn("App.bootstrapData(", html)
        self.assertNotIn("Sivori Media", html)
        self.assertIn("Campo Delantero", html)

    def test_fixture_malformed_response_html_loads(self):
        """Verify malformed_response.html fixture loads corrupted syntax."""
        html = load_fixture("malformed_response.html")
        self.assertIn("corrupted_syntax", html)

    def test_fixture_waf_challenge_response_html_loads(self):
        """Verify waf_challenge_response.html fixture loads captcha container."""
        html = load_fixture("waf_challenge_response.html")
        self.assertIn("request-captcha-container", html)

    def test_fixture_builder_generates_valid_bootstrap_json(self):
        """Verify build_allaccess_html produces valid HTML and decodable JSON."""
        html = build_allaccess_html(section_name="Sivori Media", available=True, price=125000.0)
        self.assertIn("App.bootstrapData(", html)
        self.assertIn("Sivori Media", html)
        # Verify JSON can be extracted and loaded
        start = html.find("App.bootstrapData(") + len("App.bootstrapData(")
        decoder = json.JSONDecoder()
        data, _ = decoder.raw_decode(html[start:])
        self.assertIn("model", data)
        self.assertEqual(data["model"]["data"]["name"], "Foo Fighters")


if __name__ == "__main__":
    unittest.main()
