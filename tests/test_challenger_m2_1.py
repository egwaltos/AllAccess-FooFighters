"""
Milestone 2 Adversarial & Empirical Stress Testing Suite (Challenger 1).
Targets:
- TelegramNotifier: XML/HTML escaping, tag injection, payload size, HTTP 429/500, network faults.
- DiscordNotifier: Rich embeds, rate limits (headers vs body), 500s, malformed responses, connection drops.
- EmailNotifier: Full unicode range, accented headers (Sívori Media), SSL vs STARTTLS, SMTP disconnects.
- CompositeNotifier: Fault isolation under child exceptions, empty list, channel name collisions in report.
- ConsoleNotifier: Stream I/O errors, legacy codepages (UnicodeEncodeError).
- AlertStateManager: Rapid flapping, clock skew, transient errors, corrupted state persistence, polymorphic inputs.

100% deterministic, zero live network requests, pure standard library unittest and unittest.mock.
"""
from email.message import EmailMessage
import io
import json
import logging
from pathlib import Path
import smtplib
import socket
import ssl
import tempfile
import time
from typing import List, Optional
import unittest
from unittest.mock import MagicMock, patch, call
import requests

from allaccess_monitor.models import AvailabilityStatus, CheckResult, SectionInfo, TicketAlert
from allaccess_monitor.notifier.base import BaseNotifier
from allaccess_monitor.notifier.telegram import TelegramNotifier
from allaccess_monitor.notifier.discord import DiscordNotifier
from allaccess_monitor.notifier.email import EmailNotifier
from allaccess_monitor.notifier.console import ConsoleNotifier
from allaccess_monitor.notifier.composite import CompositeNotifier
from allaccess_monitor.state import AlertStateManager, AlertDecision


class MockHTTPResponse:
    """Mock for requests.Response with customizable status, json, headers, and text."""
    def __init__(self, status_code: int = 200, json_data: dict = None, text: str = "", headers: dict = None):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text or (json.dumps(self._json_data) if json_data is not None else "")
        self.headers = headers or {}

    def json(self):
        if self._json_data is not None:
            return self._json_data
        raise json.JSONDecodeError("Expecting value", self.text, 0)


def make_alert(
    event_name: str = "Foo Fighters",
    section_name: str = "Sivori media",
    status: str = "AVAILABLE",
    event_url: str = "https://www.allaccess.com.ar/event/foo-fighters",
    price: Optional[float] = 125000.0,
    is_reminder: bool = False
) -> TicketAlert:
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
# 1. Telegram Notifier Adversarial Tests
# ==============================================================================

class TestAdversarialTelegramNotifier(unittest.TestCase):
    """Stress tests for TelegramNotifier edge cases and hostile inputs."""

    def setUp(self):
        self.bot_token = "123456789:TEST_BOT_TOKEN"
        self.chat_id = "-100987654321"
        self.notifier = TelegramNotifier(
            bot_token=self.bot_token,
            chat_id=self.chat_id,
            parse_mode="HTML",
            timeout=5.0
        )

    @patch("requests.post")
    def test_telegram_xml_html_injection_escaping(self, mock_post):
        """
        Adversarial attack: Injected HTML/XML tags and entities in event_name, section_name, and price.
        Tags like <script>, <img>, <b>, <a href="..."> must be escaped so Telegram HTML parser
        does not crash or execute unexpected markup.
        """
        mock_post.return_value = MockHTTPResponse(200, {"ok": True, "result": {"message_id": 999}})

        hostile_alert = make_alert(
            event_name='Foo <script>alert("XSS")</script> & "Rockers"',
            section_name="Sívori <Media> & 'VIP' <b>Unclosed",
            event_url='https://www.allaccess.com.ar/event/foo?ref=alert&utm="tag"<x>',
            price=150000.0
        )

        success = self.notifier.send(hostile_alert)
        self.assertTrue(success)

        called_payload = mock_post.call_args[1]["json"]
        sent_text = called_payload["text"]

        # Ensure dangerous unescaped tags are NOT present in raw form
        self.assertNotIn("<script>", sent_text)
        self.assertNotIn("</script>", sent_text)
        self.assertNotIn("<Media>", sent_text)
        self.assertNotIn("<b>Unclosed", sent_text)

        # Ensure proper entity escaping occurred
        self.assertIn("&lt;script&gt;", sent_text)
        self.assertIn("&lt;Media&gt;", sent_text)
        self.assertIn("&amp;", sent_text)

    @patch("requests.post")
    def test_telegram_huge_payload_handling(self, mock_post):
        """
        Adversarial test: Section name or event name with 20,000 characters.
        If Telegram API rejects with HTTP 400 'message too long', notifier must return False cleanly.
        """
        mock_post.return_value = MockHTTPResponse(
            400,
            {"ok": False, "error_code": 400, "description": "Bad Request: message is too long"},
            text="Bad Request: message is too long"
        )

        huge_section = "A" * 20000
        alert = make_alert(section_name=huge_section)

        success = self.notifier.send(alert)
        self.assertFalse(success)
        mock_post.assert_called_once()

    @patch("requests.post")
    def test_telegram_rate_limit_with_and_without_retry_after(self, mock_post):
        """HTTP 429 rate limit with valid retry_after parameter and malformed JSON."""
        # 1. Valid retry_after
        mock_post.return_value = MockHTTPResponse(
            429,
            {"ok": False, "error_code": 429, "parameters": {"retry_after": 45}}
        )
        self.assertFalse(self.notifier.send(make_alert()))

        # 2. Malformed non-JSON 429 response
        mock_post.return_value = MockHTTPResponse(429, None, text="Too Many Requests HTML error")
        self.assertFalse(self.notifier.send(make_alert()))

    @patch("requests.post")
    def test_telegram_server_errors_and_network_drops(self, mock_post):
        """HTTP 500, 502, 503, 504 and connection drops."""
        # HTTP 500
        mock_post.return_value = MockHTTPResponse(500, None, text="Internal Server Error")
        self.assertFalse(self.notifier.send(make_alert()))

        # Connection drop / DNS failure
        mock_post.side_effect = requests.exceptions.ConnectionError("Failed to establish a new connection")
        self.assertFalse(self.notifier.send(make_alert()))

        # Socket read timeout
        mock_post.side_effect = requests.exceptions.ReadTimeout("Read timed out")
        self.assertFalse(self.notifier.send(make_alert()))

    def test_telegram_empty_credentials(self):
        """Empty bot_token or chat_id returns False without making network request."""
        n1 = TelegramNotifier(bot_token="", chat_id="12345")
        self.assertFalse(n1.send(make_alert()))

        n2 = TelegramNotifier(bot_token="12345", chat_id="")
        self.assertFalse(n2.send(make_alert()))


# ==============================================================================
# 2. Discord Notifier Adversarial Tests
# ==============================================================================

class TestAdversarialDiscordNotifier(unittest.TestCase):
    """Stress tests for Discord webhook notification channel."""

    def setUp(self):
        self.webhook_url = "https://discord.com/api/webhooks/123456789/ABCDEFGHIJK"
        self.notifier = DiscordNotifier(webhook_url=self.webhook_url, timeout=5.0)

    @patch("requests.post")
    def test_discord_rich_embed_edge_cases(self, mock_post):
        """
        Adversarial inputs: Price None, price 0, float price, accents, markdown characters.
        Verifies correct JSON payload structure without serialization errors.
        """
        mock_post.return_value = MockHTTPResponse(204)

        # None price
        alert_none_price = make_alert(price=None)
        self.assertTrue(self.notifier.send(alert_none_price))
        payload = mock_post.call_args[1]["json"]
        embed = payload["embeds"][0]
        field_names = [f["name"] for f in embed["fields"]]
        self.assertNotIn("Price", field_names)

        # Accented section and special characters
        mock_post.reset_mock()
        alert_accents = make_alert(
            event_name="Föó Fïghtërs 🎸",
            section_name="Sívori Media & [VIP] *Special*",
            price=125500.50
        )
        self.assertTrue(self.notifier.send(alert_accents))
        payload = mock_post.call_args[1]["json"]
        self.assertIn("Sívori Media", payload["content"])
        self.assertIn("125,500.50", payload["embeds"][0]["fields"][2]["value"])

    @patch("requests.post")
    def test_discord_rate_limit_header_vs_body(self, mock_post):
        """HTTP 429 handled when retry-after is in JSON vs HTTP headers."""
        # Case 1: In JSON body
        mock_post.return_value = MockHTTPResponse(429, {"retry_after": 1.25})
        self.assertFalse(self.notifier.send(make_alert()))

        # Case 2: In HTTP headers with non-JSON body
        mock_post.return_value = MockHTTPResponse(
            429,
            None,
            text="Rate limited",
            headers={"Retry-After": "3"}
        )
        self.assertFalse(self.notifier.send(make_alert()))

    @patch("requests.post")
    def test_discord_server_errors_and_ssl_network_failures(self, mock_post):
        """Discord 5xx errors, SSLError, ConnectTimeout."""
        for code in (500, 502, 503, 504):
            mock_post.return_value = MockHTTPResponse(code, text="Server Error")
            self.assertFalse(self.notifier.send(make_alert()))

        mock_post.side_effect = requests.exceptions.SSLError("SSL verification failed")
        self.assertFalse(self.notifier.send(make_alert()))

        mock_post.side_effect = requests.exceptions.ConnectTimeout("Connection timed out")
        self.assertFalse(self.notifier.send(make_alert()))

    def test_discord_empty_or_none_webhook(self):
        """Empty or None webhook URL returns False without unhandled exceptions."""
        notifier_empty = DiscordNotifier(webhook_url="")
        self.assertFalse(notifier_empty.send(make_alert()))

        notifier_none = DiscordNotifier(webhook_url=None)
        self.assertFalse(notifier_none.send(make_alert()))


# ==============================================================================
# 3. Email Notifier Adversarial Tests
# ==============================================================================

class TestAdversarialEmailNotifier(unittest.TestCase):
    """Stress tests for Email SMTP notification channel."""

    def setUp(self):
        self.notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_port=587,
            username="bot@example.com",
            password="secret_password",
            to_addrs=["fan1@example.com", "fan2@example.com"],
            use_tls=True,
            use_ssl=False,
            timeout=5.0
        )

    @patch("smtplib.SMTP")
    def test_email_unicode_and_accents_everywhere(self, mock_smtp_cls):
        """
        Adversarial test: Accents, Emojis, and special characters across
        Subject, Body text, and HTML parts.
        """
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server

        alert = make_alert(
            event_name="Foo Fighters 🎸 2026",
            section_name="Sívori Media — Platea Alta & Baja",
            price=125000.0
        )

        success = self.notifier.send(alert)
        self.assertTrue(success)

        # Inspect sent EmailMessage
        mock_server.send_message.assert_called_once()
        sent_msg: EmailMessage = mock_server.send_message.call_args[0][0]

        # Subject contains accented section name
        self.assertIn("Sívori Media", sent_msg["Subject"])
        self.assertTrue(sent_msg.is_multipart())

        # Extract text and html payloads
        body_parts = list(sent_msg.walk())
        text_content = [p.get_content() for p in body_parts if p.get_content_type() == "text/plain"][0]
        html_content = [p.get_content() for p in body_parts if p.get_content_type() == "text/html"][0]

        self.assertIn("Sívori Media", text_content)
        self.assertIn("Sívori Media", html_content)
        self.assertIn("🎸", text_content)

    @patch("smtplib.SMTP_SSL")
    def test_email_direct_ssl_port_465(self, mock_ssl_cls):
        """Verifies SSL negotiation when smtp_port=465 or use_ssl=True."""
        mock_server = MagicMock()
        mock_ssl_cls.return_value.__enter__.return_value = mock_server

        ssl_notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_port=465,
            username="bot@example.com",
            password="secret_password",
            to_addrs="single@example.com",
            use_tls=False,
            use_ssl=True
        )

        success = ssl_notifier.send(make_alert())
        self.assertTrue(success)
        mock_ssl_cls.assert_called_once()
        mock_server.login.assert_called_once_with("bot@example.com", "secret_password")
        mock_server.send_message.assert_called_once()

    @patch("smtplib.SMTP")
    def test_email_smtp_failures_during_handshake(self, mock_smtp_cls):
        """Simulate SMTP failures during STARTTLS, LOGIN, and DATA transfer."""
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server

        # 1. STARTTLS not supported
        mock_server.starttls.side_effect = smtplib.SMTPNotSupportedError("STARTTLS not supported")
        self.assertFalse(self.notifier.send(make_alert()))

        # 2. Authentication error
        mock_server.starttls.side_effect = None
        mock_server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Authentication failed")
        self.assertFalse(self.notifier.send(make_alert()))

        # 3. Server disconnect mid-message
        mock_server.login.side_effect = None
        mock_server.send_message.side_effect = smtplib.SMTPServerDisconnected("Connection abruptly closed")
        self.assertFalse(self.notifier.send(make_alert()))

        # 4. Socket timeout
        mock_server.send_message.side_effect = socket.timeout("Timed out")
        self.assertFalse(self.notifier.send(make_alert()))

    def test_email_empty_host_or_recipients(self):
        """Empty host or empty to_addrs recipient list returns False immediately."""
        n1 = EmailNotifier(smtp_host="", to_addrs=["test@example.com"])
        self.assertFalse(n1.send(make_alert()))

        n2 = EmailNotifier(smtp_host="smtp.example.com", to_addrs=[])
        self.assertFalse(n2.send(make_alert()))

        n3 = EmailNotifier(smtp_host="smtp.example.com", to_addrs=["", "   "])
        self.assertFalse(n3.send(make_alert()))


# ==============================================================================
# 4. Console Notifier Adversarial Tests
# ==============================================================================

class TestAdversarialConsoleNotifier(unittest.TestCase):
    """Stress tests for ConsoleNotifier terminal I/O and encoding resilience."""

    def test_console_closed_stream_handles_ioerror(self):
        """Writing to a closed stream logs error and returns False without raising."""
        s = io.StringIO()
        s.close()
        notifier = ConsoleNotifier(stream=s)
        success = notifier.send(make_alert())
        self.assertFalse(success)

    def test_console_unicode_encode_error_fallback(self):
        """
        Simulate Windows terminal cp1252 codepage throwing UnicodeEncodeError on emojis.
        Verifies fallback to sanitized ASCII.
        """
        class MockCP1252Stream:
            def __init__(self):
                self.writes = []
                self.raised = False

            def write(self, s: str):
                if not self.raised and any(ord(c) > 127 for c in s):
                    self.raised = True
                    raise UnicodeEncodeError("charmap", "\U0001f3b8", 0, 1, "character maps to <undefined>")
                self.writes.append(s)

            def flush(self):
                pass

        stream = MockCP1252Stream()
        notifier = ConsoleNotifier(stream=stream)
        alert = make_alert(event_name="Foo Fighters 🎸", section_name="Sívori")
        success = notifier.send(alert)
        self.assertTrue(success)
        self.assertTrue(len(stream.writes) > 0)
        # Sanitized output should not raise error and contains replaced characters
        self.assertIn("Foo Fighters", stream.writes[0])


# ==============================================================================
# 5. Composite Notifier Adversarial Tests
# ==============================================================================

class TestAdversarialCompositeNotifier(unittest.TestCase):
    """Stress tests for CompositeNotifier fault isolation and multiplexing."""

    def test_composite_child_raises_uncaught_exception(self):
        """
        Adversarial test: A child notifier raises an unhandled RuntimeError or TypeError.
        CompositeNotifier must contain the exception, log it, and continue delivering to remaining channels.
        """
        exploding_notifier = MagicMock(spec=BaseNotifier)
        exploding_notifier.channel_name = "exploding_channel"
        exploding_notifier.send.side_effect = RuntimeError("Catastrophic unexpected failure!")

        working_notifier = MagicMock(spec=BaseNotifier)
        working_notifier.channel_name = "working_channel"
        working_notifier.send.return_value = True

        composite = CompositeNotifier([exploding_notifier, working_notifier])
        success = composite.send(make_alert())

        self.assertTrue(success, "Composite must succeed if at least one channel succeeds!")
        working_notifier.send.assert_called_once()
        self.assertIn("exploding_channel", composite.last_delivery_report)
        self.assertFalse(composite.last_delivery_report["exploding_channel"])
        self.assertTrue(composite.last_delivery_report["working_channel"])

    def test_composite_all_channels_fail(self):
        """Returns False when all underlying channels return False or fail."""
        n1 = MagicMock(spec=BaseNotifier)
        n1.channel_name = "c1"
        n1.send.return_value = False

        n2 = MagicMock(spec=BaseNotifier)
        n2.channel_name = "c2"
        n2.send.side_effect = ConnectionError("Network down")

        composite = CompositeNotifier([n1, n2])
        self.assertFalse(composite.send(make_alert()))
        self.assertEqual(composite.last_delivery_report, {"c1": False, "c2": False})

    def test_composite_empty_channels(self):
        """Empty composite returns False and empty report."""
        composite = CompositeNotifier([])
        self.assertFalse(composite.send(make_alert()))
        self.assertEqual(composite.last_delivery_report, {})

    def test_composite_duplicate_channel_name_investigation(self):
        """
        Adversarial investigation:
        When multiple notifiers share the same channel_name (e.g. two Telegram bots,
        or two Webhook endpoints), CompositeNotifier maps results into Dict[str, bool]
        using channel_name as the dictionary key.
        If the first succeeds and the second fails, the dictionary entry is overwritten with False.
        Because send() returns any(self.last_delivery_report.values()), send() returns False
        even though channel 1 successfully delivered the alert!
        """
        n1 = MagicMock(spec=BaseNotifier)
        n1.channel_name = "telegram"
        n1.send.return_value = True

        n2 = MagicMock(spec=BaseNotifier)
        n2.channel_name = "telegram"
        n2.send.return_value = False

        # Order 1: Success then Failure
        c1 = CompositeNotifier([n1, n2])
        res1 = c1.send(make_alert())

        # Order 2: Failure then Success
        c2 = CompositeNotifier([n2, n1])
        res2 = c2.send(make_alert())

        # Document key collision behavior:
        # res1 is False due to overwrite, while res2 is True!
        self.assertFalse(res1)
        self.assertTrue(res2)
        self.assertEqual(len(c1.last_delivery_report), 1)


# ==============================================================================
# 6. AlertStateManager Adversarial Tests
# ==============================================================================

class TestAdversarialAlertStateManager(unittest.TestCase):
    """Stress tests for AlertStateManager state machine, flapping, clock skew, and persistence."""

    def test_rapid_alternating_availability_flapping(self):
        """
        Adversarial scenario: Rapid flapping between AVAILABLE and UNAVAILABLE.
        Flapping 50 times in rapid succession (e.g. 50 ticks of True -> False -> True -> False).
        Every transition to AVAILABLE must trigger an alert (edge detection),
        and every transition to UNAVAILABLE must reset the state.
        """
        manager = AlertStateManager(cooldown_seconds=1800)
        t = 1000.0

        for cycle in range(50):
            # 1. Becomes AVAILABLE -> Must alert!
            d_avail = manager.process_check_result(is_available=True, current_time=t)
            self.assertTrue(d_avail.should_alert, f"Cycle {cycle}: initial availability must alert!")
            self.assertFalse(d_avail.is_reminder)
            self.assertTrue(manager.is_currently_available)

            t += 1.0

            # 2. Becomes UNAVAILABLE -> Must reset!
            d_unavail = manager.process_check_result(is_available=False, current_time=t)
            self.assertFalse(d_unavail.should_alert)
            self.assertFalse(manager.is_currently_available)
            self.assertIsNone(manager.last_alert_timestamp)

            t += 1.0

        self.assertEqual(manager.total_alerts_sent, 50)

    def test_clock_skew_backward_while_available(self):
        """
        Adversarial scenario: System clock steps backward while tickets are available.
        (e.g. NTP sync, VM migration, daylight savings adjustment).
        """
        manager = AlertStateManager(cooldown_seconds=1800)

        # 1. Initial alert at t=5000.0
        d1 = manager.process_check_result(is_available=True, current_time=5000.0)
        self.assertTrue(d1.should_alert)
        self.assertEqual(manager.last_alert_timestamp, 5000.0)

        # 2. Clock jumps backward to t=4000.0 (1000 seconds in the past)
        d_skew = manager.process_check_result(is_available=True, current_time=4000.0)
        self.assertFalse(d_skew.should_alert, "Backward clock skew must not trigger false reminder!")
        self.assertIn("Clock skew", d_skew.reason)
        # Reference timestamp should adjust to prevent future negative elapsed
        self.assertEqual(manager.last_alert_timestamp, 4000.0)

        # 3. Advance to t=4500.0 (500s after adjusted reference, within 1800s cooldown)
        d_cooldown = manager.process_check_result(is_available=True, current_time=4500.0)
        self.assertFalse(d_cooldown.should_alert, "Must be throttled under cooldown")

        # 4. Advance past cooldown to t=5800.0 (1800s after 4000.0) -> Reminder!
        d_reminder = manager.process_check_result(is_available=True, current_time=5800.0)
        self.assertTrue(d_reminder.should_alert)
        self.assertTrue(d_reminder.is_reminder)

    def test_transient_error_streak_and_recovery(self):
        """
        Adversarial scenario:
        1. Tickets available -> alert sent.
        2. Long streak of 20 transient errors (e.g. AWS WAF, 502, timeouts).
        3. Tickets still available -> must not resend initial alert!
        """
        manager = AlertStateManager(cooldown_seconds=1800)

        # 1. Alert at t=1000
        d1 = manager.process_check_result(is_available=True, current_time=1000.0)
        self.assertTrue(d1.should_alert)

        # 2. 20 consecutive errors
        for i in range(20):
            err_res = CheckResult(
                status=AvailabilityStatus.ERROR,
                section_name="Sivori media",
                error_message=f"Network drop #{i+1}"
            )
            d_err = manager.process_check_result(err_res, current_time=1000.0 + i * 10)
            self.assertFalse(d_err.should_alert)
            self.assertTrue(manager.is_currently_available)

        self.assertEqual(manager.consecutive_errors, 20)

        # 3. Successful check at t=1300 (300s since t=1000, cooldown is 1800s)
        d_recovered = manager.process_check_result(is_available=True, current_time=1300.0)
        self.assertFalse(d_recovered.should_alert, "Must be throttled by cooldown upon recovery!")
        self.assertEqual(manager.consecutive_errors, 0, "Error streak must be reset to 0")

    def test_state_persistence_corrupted_files(self):
        """
        Adversarial scenario: State file contains corrupted data:
        1. Malformed JSON syntax
        2. JSON array instead of object
        3. String for float timestamps
        4. Truncated 0-byte file
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"

            # 1. Malformed JSON
            state_path.write_text("{{corrupted_json: true", encoding="utf-8")
            m1 = AlertStateManager(state_file=state_path)
            self.assertFalse(m1.is_currently_available)
            self.assertIsNone(m1.last_alert_timestamp)

            # 2. JSON array
            state_path.write_text("[1, 2, 3]", encoding="utf-8")
            m2 = AlertStateManager(state_file=state_path)
            self.assertFalse(m2.is_currently_available)

            # 3. String in last_alert_time
            state_path.write_text(
                json.dumps({"is_currently_available": True, "last_alert_time": "not_a_float"}),
                encoding="utf-8"
            )
            m3 = AlertStateManager(state_file=state_path)
            self.assertFalse(m3.is_currently_available)

            # 4. Truncated 0-byte file
            state_path.write_text("", encoding="utf-8")
            m4 = AlertStateManager(state_file=state_path)
            self.assertFalse(m4.is_currently_available)

    def test_state_persistence_atomic_replacement_under_error(self):
        """
        Adversarial scenario: Simulated failure during save_state (e.g. read-only disk).
        Verifies OSError is caught and logged without raising unhandled exceptions.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            manager = AlertStateManager(state_file=state_path)

            # Patch Path.replace to raise OSError (e.g. access denied)
            with patch.object(Path, "replace", side_effect=OSError("Access denied / Disk full")):
                # Should not raise exception
                d = manager.process_check_result(is_available=True, current_time=1000.0)
                self.assertTrue(d.should_alert)

    def test_polymorphic_input_handling(self):
        """
        Adversarial test: process_check_result receives various polymorphic forms:
        - CheckResult with AVAILABLE, UNAVAILABLE, SECTION_NOT_FOUND, ERROR
        - AvailabilityStatus enum
        - Raw boolean
        - Object with .is_available attribute
        - None / missing status raises ValueError
        """
        manager = AlertStateManager(cooldown_seconds=1800)

        # CheckResult SECTION_NOT_FOUND is treated as unavailable
        r_not_found = CheckResult(status=AvailabilityStatus.SECTION_NOT_FOUND, section_name="Fake")
        d1 = manager.process_check_result(r_not_found, current_time=100.0)
        self.assertFalse(d1.should_alert)
        self.assertFalse(manager.is_currently_available)

        # AvailabilityStatus enum directly
        d2 = manager.process_check_result(AvailabilityStatus.AVAILABLE, current_time=110.0)
        self.assertTrue(d2.should_alert)
        self.assertTrue(manager.is_currently_available)

        # Duck-typed object with is_available
        class DuckCheck:
            is_available = True
        d3 = manager.process_check_result(DuckCheck(), current_time=120.0)
        self.assertFalse(d3.should_alert)  # Throttled by cooldown

        # None should raise ValueError
        with self.assertRaises(ValueError):
            manager.process_check_result(None)


if __name__ == "__main__":
    unittest.main()
