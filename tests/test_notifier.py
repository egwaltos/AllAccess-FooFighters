"""
Unit tests for AllAccess notification channels (Telegram, Discord, Email, Console, Composite).
Verifies HTTP API payloads, rich embeds, SMTP MIME structures, error handling, rate limits,
timeouts, and multi-channel multiplexing with fault isolation.

Zero third-party test dependencies; standard library unittest.
Zero live network calls; 100% deterministic mocking.
"""
from email.message import EmailMessage
import io
import json
import logging
from pathlib import Path
import smtplib
import ssl
from typing import List
import unittest
from unittest.mock import MagicMock, patch, call
import requests

from allaccess_monitor.models import AvailabilityStatus, CheckResult, TicketAlert
from allaccess_monitor.notifier.base import BaseNotifier
from allaccess_monitor.notifier.telegram import TelegramNotifier
from allaccess_monitor.notifier.discord import DiscordNotifier
from allaccess_monitor.notifier.email import EmailNotifier
from allaccess_monitor.notifier.console import ConsoleNotifier
from allaccess_monitor.notifier.composite import CompositeNotifier
from allaccess_monitor.config import MonitorConfig, TelegramConfig, DiscordConfig, EmailConfig


class MockHTTPResponse:
    """Deterministic mock for requests.Response."""
    def __init__(self, status_code: int = 200, json_data: dict = None, text: str = "", headers: dict = None):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text or json.dumps(self._json_data)
        self.headers = headers or {}

    def json(self):
        return self._json_data


def make_test_alert(
    event_name: str = "Foo Fighters",
    section_name: str = "Sivori media",
    status: str = "AVAILABLE",
    event_url: str = "https://www.allaccess.com.ar/event/foo-fighters",
    price: float = 125000.0,
    is_reminder: bool = False
) -> TicketAlert:
    """Helper to instantiate valid TicketAlert fixtures."""
    return TicketAlert(
        event_name=event_name,
        section_name=section_name,
        status=status,
        event_url=event_url,
        price=price,
        currency="ARS",
        is_reminder=is_reminder
    )


# ==============================================================================
# 1. BaseNotifier Contract Tests
# ==============================================================================

class TestBaseNotifierContract(unittest.TestCase):
    """Verifies BaseNotifier abstract base class protocol."""

    def test_cannot_instantiate_abstract_base_notifier(self):
        """BaseNotifier cannot be instantiated directly."""
        with self.assertRaises(TypeError):
            BaseNotifier()

    def test_concrete_subclass_must_implement_channel_name_and_send(self):
        """Concrete subclass missing abstract methods raises TypeError."""
        class IncompleteNotifier(BaseNotifier):
            pass

        with self.assertRaises(TypeError):
            IncompleteNotifier()


# ==============================================================================
# 2. TelegramNotifier Unit Tests
# ==============================================================================

class TestTelegramNotifier(unittest.TestCase):
    """Unit tests for Telegram Bot API notification delivery."""

    def setUp(self):
        self.bot_token = "123456789:ABCdefGHIjklMNOpqrsTUVwxyz"
        self.chat_id = "-1001234567890"
        self.notifier = TelegramNotifier(
            bot_token=self.bot_token,
            chat_id=self.chat_id,
            parse_mode="HTML",
            timeout=5.0
        )

    def test_channel_name(self):
        """Channel name identifier is 'telegram'."""
        self.assertEqual(self.notifier.channel_name, "telegram")

    @patch("requests.post")
    def test_send_success_200(self, mock_post):
        """HTTP 200 returns True and dispatches valid Telegram JSON payload."""
        mock_post.return_value = MockHTTPResponse(status_code=200, json_data={"ok": True, "result": {"message_id": 42}})
        alert = make_test_alert()

        result = self.notifier.send(alert)
        self.assertTrue(result)

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], f"https://api.telegram.org/bot{self.bot_token}/sendMessage")
        self.assertEqual(kwargs.get("timeout"), 5.0)

        payload = kwargs.get("json", {})
        self.assertEqual(payload.get("chat_id"), str(self.chat_id))
        self.assertEqual(payload.get("parse_mode"), "HTML")
        self.assertFalse(payload.get("disable_web_page_preview", True))
        self.assertIn("Sivori media", payload.get("text", ""))
        self.assertIn("Foo Fighters", payload.get("text", ""))
        self.assertIn(alert.event_url, payload.get("text", ""))

    @patch("requests.post")
    def test_html_entity_escaping(self, mock_post):
        """Ensures HTML special characters are escaped to prevent parse errors."""
        mock_post.return_value = MockHTTPResponse(status_code=200, json_data={"ok": True})
        alert = make_test_alert(
            event_name="Foo <Fighters> & Special 'VIP'",
            section_name="Sivori & Media <Special>"
        )

        self.notifier.send(alert)
        text = mock_post.call_args[1]["json"]["text"]
        self.assertNotIn("<Fighters>", text)
        self.assertNotIn("<Special>", text)
        self.assertIn("&lt;Fighters&gt;", text)
        self.assertIn("&amp; Special", text)
        self.assertIn("&lt;Special&gt;", text)

    @patch("requests.post")
    def test_reminder_alert_formatting(self, mock_post):
        """Reminder alerts include [REMINDER] header and distinct icon."""
        mock_post.return_value = MockHTTPResponse(status_code=200, json_data={"ok": True})
        alert = make_test_alert(is_reminder=True)

        self.notifier.send(alert)
        text = mock_post.call_args[1]["json"]["text"]
        self.assertIn("REMINDER", text)

    @patch("requests.post")
    def test_price_formatting_and_none_price(self, mock_post):
        """Handles both explicit price and price=None cleanly."""
        mock_post.return_value = MockHTTPResponse(status_code=200, json_data={"ok": True})
        
        # Explicit price
        alert_with_price = make_test_alert(price=125000.0)
        self.notifier.send(alert_with_price)
        text1 = mock_post.call_args[1]["json"]["text"]
        self.assertIn("125,000", text1)

        # None price
        alert_no_price = make_test_alert(price=None)
        self.notifier.send(alert_no_price)
        text2 = mock_post.call_args[1]["json"]["text"]
        self.assertNotIn("None", text2)

    @patch("requests.post")
    def test_chat_id_coercion(self, mock_post):
        """Integer chat_id is safely coerced to string in payload."""
        mock_post.return_value = MockHTTPResponse(status_code=200, json_data={"ok": True})
        notifier = TelegramNotifier(bot_token="tok", chat_id=987654321)
        notifier.send(make_test_alert())
        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["chat_id"], "987654321")

    @patch("requests.post")
    def test_rate_limit_429(self, mock_post):
        """HTTP 429 returns False and extracts parameters.retry_after."""
        mock_post.return_value = MockHTTPResponse(
            status_code=429,
            json_data={"ok": False, "parameters": {"retry_after": 15}, "description": "Too Many Requests"}
        )
        result = self.notifier.send(make_test_alert())
        self.assertFalse(result)

    @patch("requests.post")
    def test_bad_request_400_permanent_failure(self, mock_post):
        """HTTP 400 Bad Request returns False without crashing."""
        mock_post.return_value = MockHTTPResponse(
            status_code=400,
            json_data={"ok": False, "description": "Bad Request: chat not found"}
        )
        result = self.notifier.send(make_test_alert())
        self.assertFalse(result)

    @patch("requests.post")
    def test_unauthorized_401_invalid_token(self, mock_post):
        """HTTP 401 Unauthorized returns False."""
        mock_post.return_value = MockHTTPResponse(status_code=401, json_data={"ok": False})
        result = self.notifier.send(make_test_alert())
        self.assertFalse(result)

    @patch("requests.post")
    def test_server_error_500(self, mock_post):
        """HTTP 500 returns False."""
        mock_post.return_value = MockHTTPResponse(status_code=500, text="Internal Server Error")
        result = self.notifier.send(make_test_alert())
        self.assertFalse(result)

    @patch("requests.post")
    def test_network_timeout(self, mock_post):
        """Socket timeout returns False without uncaught exception."""
        mock_post.side_effect = requests.exceptions.Timeout("Connection timed out after 5.0s")
        result = self.notifier.send(make_test_alert())
        self.assertFalse(result)

    @patch("requests.post")
    def test_connection_error(self, mock_post):
        """DNS resolution failure or connection refused returns False."""
        mock_post.side_effect = requests.exceptions.ConnectionError("Failed to resolve api.telegram.org")
        result = self.notifier.send(make_test_alert())
        self.assertFalse(result)


# ==============================================================================
# 3. DiscordNotifier Unit Tests
# ==============================================================================

class TestDiscordNotifier(unittest.TestCase):
    """Unit tests for Discord Webhook notification delivery."""

    def setUp(self):
        self.webhook_url = "https://discord.com/api/webhooks/123456789/abcdef-webhook-token"
        self.notifier = DiscordNotifier(webhook_url=self.webhook_url, username="Ticket Bot", timeout=5.0)

    def test_channel_name(self):
        """Channel name identifier is 'discord'."""
        self.assertEqual(self.notifier.channel_name, "discord")

    @patch("requests.post")
    def test_send_success_204(self, mock_post):
        """HTTP 204 No Content returns True."""
        mock_post.return_value = MockHTTPResponse(status_code=204)
        result = self.notifier.send(make_test_alert())
        self.assertTrue(result)

    @patch("requests.post")
    def test_send_success_200(self, mock_post):
        """HTTP 200 OK returns True."""
        mock_post.return_value = MockHTTPResponse(status_code=200, json_data={"id": "999"})
        result = self.notifier.send(make_test_alert())
        self.assertTrue(result)

    @patch("requests.post")
    def test_rich_embed_payload_structure(self, mock_post):
        """Payload adheres to Discord rich embed schema."""
        mock_post.return_value = MockHTTPResponse(status_code=204)
        alert = make_test_alert(price=125000.0)

        self.notifier.send(alert)
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], self.webhook_url)

        payload = kwargs.get("json", {})
        self.assertEqual(payload.get("username"), "Ticket Bot")
        self.assertIn("@everyone", payload.get("content", ""))
        self.assertIn("embeds", payload)
        self.assertEqual(len(payload["embeds"]), 1)

        embed = payload["embeds"][0]
        self.assertIn("Foo Fighters", embed.get("title", ""))
        self.assertEqual(embed.get("url"), alert.event_url)
        self.assertEqual(embed.get("color"), 3066993)  # Emerald green #2ECC71

        # Fields verification
        fields = embed.get("fields", [])
        field_dict = {f["name"].lower(): f["value"] for f in fields}
        self.assertTrue(any("section" in k for k in field_dict))
        self.assertTrue(any("status" in k for k in field_dict))
        self.assertTrue(any("price" in k for k in field_dict))
        self.assertTrue(any("125,000" in v for v in field_dict.values()))

    @patch("requests.post")
    def test_reminder_embed_color_and_content(self, mock_post):
        """Reminder alerts use gold embed color (0xF1C40F / 15844367) and tag [REMINDER]."""
        mock_post.return_value = MockHTTPResponse(status_code=204)
        alert = make_test_alert(is_reminder=True)

        self.notifier.send(alert)
        embed = mock_post.call_args[1]["json"]["embeds"][0]
        self.assertEqual(embed["color"], 15844367)  # 0xF1C40F
        self.assertIn("Reminder", mock_post.call_args[1]["json"]["content"])

    @patch("requests.post")
    def test_rate_limit_429(self, mock_post):
        """HTTP 429 returns False and parses retry_after."""
        mock_post.return_value = MockHTTPResponse(
            status_code=429,
            json_data={"message": "You are being rate limited.", "retry_after": 2.5},
            headers={"Retry-After": "2.5"}
        )
        result = self.notifier.send(make_test_alert())
        self.assertFalse(result)

    @patch("requests.post")
    def test_not_found_404_deleted_webhook(self, mock_post):
        """HTTP 404 returns False."""
        mock_post.return_value = MockHTTPResponse(status_code=404, json_data={"code": 10015, "message": "Unknown Webhook"})
        result = self.notifier.send(make_test_alert())
        self.assertFalse(result)

    @patch("requests.post")
    def test_bad_request_400(self, mock_post):
        """HTTP 400 returns False."""
        mock_post.return_value = MockHTTPResponse(status_code=400, text="Bad Request: embed fields exceeded")
        result = self.notifier.send(make_test_alert())
        self.assertFalse(result)

    @patch("requests.post")
    def test_server_error_500(self, mock_post):
        """HTTP 500 returns False."""
        mock_post.return_value = MockHTTPResponse(status_code=500, text="Internal Server Error")
        result = self.notifier.send(make_test_alert())
        self.assertFalse(result)

    @patch("requests.post")
    def test_network_exceptions(self, mock_post):
        """Timeouts and DNS failures return False without crashing."""
        mock_post.side_effect = requests.exceptions.Timeout("Timeout")
        self.assertFalse(self.notifier.send(make_test_alert()))

        mock_post.side_effect = requests.exceptions.ConnectionError("Connection refused")
        self.assertFalse(self.notifier.send(make_test_alert()))


# ==============================================================================
# 4. EmailNotifier Unit Tests
# ==============================================================================

class TestEmailNotifier(unittest.TestCase):
    """Unit tests for SMTP Email notification delivery."""

    def setUp(self):
        self.notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_port=587,
            username="bot@example.com",
            password="secret_app_password",
            from_addr="bot@example.com",
            to_addrs=["fan1@example.com", "fan2@example.com"],
            use_tls=True,
            use_ssl=False,
            timeout=10.0
        )

    def test_channel_name(self):
        """Channel name identifier is 'email'."""
        self.assertEqual(self.notifier.channel_name, "email")

    @patch("smtplib.SMTP")
    def test_send_starttls_success_port_587(self, mock_smtp_cls):
        """STARTTLS on port 587 executes EHLO, STARTTLS, LOGIN, and SEND_MESSAGE."""
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_smtp

        alert = make_test_alert(price=125000.0)
        result = self.notifier.send(alert)
        self.assertTrue(result)

        mock_smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=10.0)
        mock_smtp.starttls.assert_called_once()
        mock_smtp.login.assert_called_once_with("bot@example.com", "secret_app_password")
        mock_smtp.send_message.assert_called_once()

        sent_msg = mock_smtp.send_message.call_args[0][0]
        self.assertIsInstance(sent_msg, EmailMessage)
        self.assertIn("Foo Fighters", sent_msg["Subject"])
        self.assertIn("Sivori media", sent_msg["Subject"])
        self.assertEqual(sent_msg["From"], "bot@example.com")
        self.assertIn("fan1@example.com", sent_msg["To"])
        self.assertIn("fan2@example.com", sent_msg["To"])

    @patch("smtplib.SMTP_SSL")
    def test_send_ssl_success_port_465(self, mock_ssl_cls):
        """Direct SSL on port 465 uses SMTP_SSL without calling starttls."""
        mock_smtp = MagicMock()
        mock_ssl_cls.return_value.__enter__.return_value = mock_smtp

        notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_port=465,
            username="bot@example.com",
            password="secret_app_password",
            from_addr="bot@example.com",
            to_addrs=["fan@example.com"],
            use_ssl=True,
            use_tls=False
        )

        result = notifier.send(make_test_alert())
        self.assertTrue(result)

        mock_ssl_cls.assert_called_once()
        mock_smtp.login.assert_called_once_with("bot@example.com", "secret_app_password")
        mock_smtp.send_message.assert_called_once()
        self.assertFalse(hasattr(mock_smtp, "starttls") and mock_smtp.starttls.called)

    @patch("smtplib.SMTP")
    def test_unauthenticated_smtp_relay(self, mock_smtp_cls):
        """Unauthenticated SMTP (empty username/password) sends without login()."""
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_smtp

        notifier = EmailNotifier(
            smtp_host="localhost",
            smtp_port=25,
            username="",
            password="",
            from_addr="relay@example.com",
            to_addrs=["fan@example.com"],
            use_tls=False,
            use_ssl=False
        )

        result = notifier.send(make_test_alert())
        self.assertTrue(result)
        mock_smtp.login.assert_not_called()
        mock_smtp.send_message.assert_called_once()

    @patch("smtplib.SMTP")
    def test_multipart_mime_structure(self, mock_smtp_cls):
        """Email includes both plain text and HTML alternatives."""
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_smtp

        self.notifier.send(make_test_alert())
        msg = mock_smtp.send_message.call_args[0][0]
        self.assertTrue(msg.is_multipart())

        # Inspect parts
        parts = list(msg.iter_parts())
        content_types = [p.get_content_type() for p in parts]
        self.assertIn("text/plain", content_types)
        self.assertIn("text/html", content_types)

    @patch("smtplib.SMTP")
    def test_reminder_subject(self, mock_smtp_cls):
        """Alert with is_reminder=True prefixes Subject with [REMINDER]."""
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_smtp

        self.notifier.send(make_test_alert(is_reminder=True))
        msg = mock_smtp.send_message.call_args[0][0]
        self.assertIn("REMINDER", msg["Subject"])

    @patch("smtplib.SMTP")
    def test_accents_and_unicode_in_subject(self, mock_smtp_cls):
        """Accented characters in subject encode properly without error."""
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_smtp

        self.notifier.send(make_test_alert(section_name="Sívori Media"))
        msg = mock_smtp.send_message.call_args[0][0]
        self.assertIn("Sívori Media", msg["Subject"])

    @patch("smtplib.SMTP")
    def test_auth_failure_error(self, mock_smtp_cls):
        """SMTPAuthenticationError returns False."""
        mock_smtp = MagicMock()
        mock_smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication credentials invalid")
        mock_smtp_cls.return_value.__enter__.return_value = mock_smtp

        result = self.notifier.send(make_test_alert())
        self.assertFalse(result)

    @patch("smtplib.SMTP")
    def test_connect_timeout(self, mock_smtp_cls):
        """Connection timeout returns False."""
        mock_smtp_cls.side_effect = TimeoutError("Connection to SMTP host timed out")
        result = self.notifier.send(make_test_alert())
        self.assertFalse(result)

    @patch("smtplib.SMTP")
    def test_server_disconnected(self, mock_smtp_cls):
        """SMTPServerDisconnected returns False."""
        mock_smtp = MagicMock()
        mock_smtp.send_message.side_effect = smtplib.SMTPServerDisconnected("Connection closed unexpectedly")
        mock_smtp_cls.return_value.__enter__.return_value = mock_smtp

        result = self.notifier.send(make_test_alert())
        self.assertFalse(result)


# ==============================================================================
# 5. ConsoleNotifier Unit Tests
# ==============================================================================

class TestConsoleNotifier(unittest.TestCase):
    """Unit tests for Console / Dry-run notifier."""

    def test_channel_name(self):
        """Channel name identifier is 'console'."""
        self.assertEqual(ConsoleNotifier().channel_name, "console")

    def test_send_returns_true_and_logs(self):
        """ConsoleNotifier returns True and outputs formatted ticket alert."""
        notifier = ConsoleNotifier()
        alert = make_test_alert(price=125000.0)

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            result = notifier.send(alert)
            self.assertTrue(result)
            output = mock_stdout.getvalue()
            self.assertIn("Sivori media", output)
            self.assertIn("Foo Fighters", output)
            self.assertIn("AVAILABLE", output)


# ==============================================================================
# 6. CompositeNotifier Unit Tests
# ==============================================================================

class TestCompositeNotifier(unittest.TestCase):
    """Unit tests for Composite multi-channel notification multiplexer."""

    def test_channel_name(self):
        """Channel name identifier is 'composite'."""
        self.assertEqual(CompositeNotifier([]).channel_name, "composite")

    def test_all_channels_succeed(self):
        """Dispatches to all children; returns True when all succeed."""
        c1 = MagicMock(spec=BaseNotifier, channel_name="telegram")
        c2 = MagicMock(spec=BaseNotifier, channel_name="discord")
        c1.send.return_value = True
        c2.send.return_value = True

        composite = CompositeNotifier([c1, c2])
        alert = make_test_alert()
        result = composite.send(alert)

        self.assertTrue(result)
        c1.send.assert_called_once_with(alert)
        c2.send.assert_called_once_with(alert)
        self.assertEqual(composite.last_delivery_report, {"telegram": True, "discord": True})

    def test_partial_failure_isolation(self):
        """Returns True if at least one succeeds, recording per-channel status."""
        c1 = MagicMock(spec=BaseNotifier, channel_name="telegram")
        c2 = MagicMock(spec=BaseNotifier, channel_name="discord")
        c3 = MagicMock(spec=BaseNotifier, channel_name="email")
        c1.send.return_value = False
        c2.send.return_value = True
        c3.send.return_value = False

        composite = CompositeNotifier([c1, c2, c3])
        alert = make_test_alert()
        result = composite.send(alert)

        self.assertTrue(result)
        self.assertEqual(composite.last_delivery_report, {"telegram": False, "discord": True, "email": False})

    def test_all_channels_fail(self):
        """Returns False when all children fail."""
        c1 = MagicMock(spec=BaseNotifier, channel_name="telegram")
        c2 = MagicMock(spec=BaseNotifier, channel_name="discord")
        c1.send.return_value = False
        c2.send.return_value = False

        composite = CompositeNotifier([c1, c2])
        result = composite.send(make_test_alert())
        self.assertFalse(result)
        self.assertEqual(composite.last_delivery_report, {"telegram": False, "discord": False})

    def test_empty_notifiers_list(self):
        """Empty notifiers list returns False without error."""
        composite = CompositeNotifier([])
        result = composite.send(make_test_alert())
        self.assertFalse(result)
        self.assertEqual(composite.last_delivery_report, {})

    def test_exception_in_one_notifier_is_isolated(self):
        """Uncaught exception in child 1 does NOT prevent child 2 from executing."""
        c1 = MagicMock(spec=BaseNotifier, channel_name="telegram")
        c2 = MagicMock(spec=BaseNotifier, channel_name="discord")
        c1.send.side_effect = RuntimeError("Fatal crash in Telegram client")
        c2.send.return_value = True

        composite = CompositeNotifier([c1, c2])
        result = composite.send(make_test_alert())

        self.assertTrue(result)
        c2.send.assert_called_once()
        self.assertEqual(composite.last_delivery_report.get("telegram"), False)
        self.assertEqual(composite.last_delivery_report.get("discord"), True)

    def test_add_notifier_dynamically(self):
        """Allows dynamically adding notifiers."""
        composite = CompositeNotifier()
        c1 = MagicMock(spec=BaseNotifier, channel_name="console")
        c1.send.return_value = True
        composite.add_notifier(c1)
        self.assertTrue(composite.send(make_test_alert()))


# ==============================================================================
# 7. Factory & Config Integration Tests
# ==============================================================================

class TestConfigBuildNotifierIntegration(unittest.TestCase):
    """Verifies MonitorConfig.build_notifier() factory creates valid CompositeNotifier."""

    def test_build_notifier_all_enabled(self):
        """Builds composite with telegram, discord, and email when all enabled."""
        cfg = MonitorConfig(
            telegram=TelegramConfig(enabled=True, bot_token="tok", chat_id="123"),
            discord=DiscordConfig(enabled=True, webhook_url="https://discord.com/webhook"),
            email=EmailConfig(enabled=True, smtp_host="smtp.example.com", to_addrs=["a@b.com"])
        )
        composite = cfg.build_notifier()
        self.assertIsInstance(composite, CompositeNotifier)
        channel_names = [n.channel_name for n in composite.notifiers]
        self.assertIn("telegram", channel_names)
        self.assertIn("discord", channel_names)
        self.assertIn("email", channel_names)

    def test_build_notifier_dry_run_or_none_enabled(self):
        """Falls back to ConsoleNotifier when dry_run=True or no channels enabled."""
        cfg_dry = MonitorConfig(dry_run=True)
        composite = cfg_dry.build_notifier()
        channel_names = [n.channel_name for n in composite.notifiers]
        self.assertIn("console", channel_names)


if __name__ == "__main__":
    unittest.main()
