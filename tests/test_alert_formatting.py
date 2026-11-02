"""
Comprehensive test suite for Milestone 2: Rich Alert Formatting & Deduplication.
Covers:
- All 6 required fields across all 4 notification channels (Telegram, Discord, Email, Console)
- Plain-text vs HTML formatting for Email
- Discord embed fields, inline layout, and length limits
- Telegram HTML escaping, entities, and direct checkout link rendering
- Backward compatibility: graceful omission of missing session/direct_url fields without crashing or displaying 'None'
- Deduplication: per-target cooldown isolation, reminder alerts, price drop alerts, and state isolation
"""
from datetime import datetime, timezone
from email.message import EmailMessage
import io
import json
import logging
from pathlib import Path
import smtplib
import tempfile
import time
from typing import List, Optional
import unittest
from unittest.mock import MagicMock, patch, call
import requests

from allaccess_monitor.models import (
    AvailabilityStatus,
    CheckResult,
    MonitoredTarget,
    SectionInfo,
    TicketAlert,
)
from allaccess_monitor.state import AlertStateManager, AlertDecision, TargetState
from allaccess_monitor.scheduler import PollingScheduler, GracefulShutdown
from allaccess_monitor.notifier.base import BaseNotifier
from allaccess_monitor.notifier.telegram import TelegramNotifier
from allaccess_monitor.notifier.discord import DiscordNotifier, EMBED_COLOR_AVAILABLE, EMBED_COLOR_REMINDER
from allaccess_monitor.notifier.email import EmailNotifier, _format_session_str as email_format_session
from allaccess_monitor.notifier.console import ConsoleNotifier, _format_session_str as console_format_session


class MockResponse:
    """Mock requests.Response object for unit tests."""

    def __init__(
        self,
        status_code: int = 200,
        json_data: Optional[dict] = None,
        text: str = "",
        headers: Optional[dict] = None,
    ):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text or json.dumps(self._json_data)
        self.headers = headers or {}

    def json(self):
        return self._json_data


def make_rich_alert(
    event_name: str = "Paul McCartney",
    section_name: str = "Campo Delantero",
    status: str = "AVAILABLE",
    event_url: str = "https://www.allaccess.com.ar/event/paul-mccartney",
    price: Optional[float] = 143750.0,
    currency: str = "ARS",
    is_reminder: bool = False,
    session_name: Optional[str] = "Noche 1 - 21:00",
    session_date: Optional[str] = "2026-11-15 21:00",
    direct_checkout_url: Optional[str] = "https://www.allaccess.com.ar/event/paul-mccartney/12345",
    timestamp: Optional[datetime] = None,
) -> TicketAlert:
    """Constructs a fully populated TicketAlert fixture."""
    return TicketAlert(
        event_name=event_name,
        section_name=section_name,
        status=status,
        event_url=event_url,
        price=price,
        currency=currency,
        timestamp=timestamp or datetime(2026, 11, 15, 21, 0, 0, tzinfo=timezone.utc),
        is_reminder=is_reminder,
        session_name=session_name,
        session_date=session_date,
        direct_checkout_url=direct_checkout_url,
    )


# ==============================================================================
# 1. Telegram Notifier Formatting Tests
# ==============================================================================

class TestTelegramAlertFormatting(unittest.TestCase):
    """Verifies Telegram Bot API message formatting and error resilience."""

    def setUp(self):
        self.notifier = TelegramNotifier(
            bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
            chat_id="-100987654321",
            parse_mode="HTML",
            timeout=5.0,
        )

    @patch("requests.post")
    def test_telegram_all_6_fields_present(self, mock_post):
        """Telegram alert text contains all 6 core fields plus direct checkout link."""
        mock_post.return_value = MockResponse(status_code=200, json_data={"ok": True})
        alert = make_rich_alert()

        success = self.notifier.send(alert)
        self.assertTrue(success)

        payload = mock_post.call_args[1]["json"]
        text = payload["text"]

        # 1. Event Identity
        self.assertIn("Paul McCartney", text)
        # 2. Section Name
        self.assertIn("Campo Delantero", text)
        # 3. Status
        self.assertIn("🟢 Available", text)
        # 4. Formatted Price
        self.assertIn("143,750 ARS", text)
        # 5. Timestamp
        self.assertIn("2026-11-15 21:00:00 UTC", text)
        # 6. Session and Date Info
        self.assertIn("2026-11-15 21:00 (Noche 1 - 21:00)", text)
        # 7. Direct Checkout Link and Event Landing Link
        self.assertIn('href="https://www.allaccess.com.ar/event/paul-mccartney/12345"', text)
        self.assertIn('href="https://www.allaccess.com.ar/event/paul-mccartney"', text)

    @patch("requests.post")
    def test_telegram_missing_session_omitted_without_none(self, mock_post):
        """When session metadata is None, no 'None' string or empty session row is printed."""
        mock_post.return_value = MockResponse(status_code=200, json_data={"ok": True})
        legacy_alert = TicketAlert(
            event_name="Foo Fighters",
            section_name="Sivori media",
            status="AVAILABLE",
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            price=125000.0,
        )

        success = self.notifier.send(legacy_alert)
        self.assertTrue(success)

        text = mock_post.call_args[1]["json"]["text"]
        self.assertNotIn("None", text)
        self.assertNotIn("Date/Session", text)
        self.assertIn("Foo Fighters", text)
        self.assertIn("Sivori media", text)
        self.assertIn('href="https://www.allaccess.com.ar/event/foo-fighters"', text)

    @patch("requests.post")
    def test_telegram_reminder_badge_and_status(self, mock_post):
        """When is_reminder=True, Telegram message title has [REMINDER] and reminder status."""
        mock_post.return_value = MockResponse(status_code=200, json_data={"ok": True})
        alert = make_rich_alert(is_reminder=True)

        self.notifier.send(alert)
        text = mock_post.call_args[1]["json"]["text"]
        self.assertIn("🔔 [REMINDER]", text)
        self.assertIn("🔔 Available (Reminder)", text)

    @patch("requests.post")
    def test_telegram_html_entity_escaping(self, mock_post):
        """HTML special chars are properly escaped to prevent parsing failures."""
        mock_post.return_value = MockResponse(status_code=200, json_data={"ok": True})
        hostile_alert = make_rich_alert(
            event_name='Rock & Roll <Show> "VIP"',
            section_name="Platea <Alta> & Baja",
            session_name="Show <Special> & 'Guest'",
            event_url='https://www.allaccess.com.ar/event/rock?ref="test"&auth=1',
        )

        self.notifier.send(hostile_alert)
        text = mock_post.call_args[1]["json"]["text"]

        self.assertNotIn("<Show>", text)
        self.assertNotIn("<Alta>", text)
        self.assertNotIn("<Special>", text)
        self.assertIn("&lt;Show&gt;", text)
        self.assertIn("&lt;Alta&gt;", text)
        self.assertIn("&amp; Roll", text)
        self.assertIn("&amp; Baja", text)
        self.assertIn("&quot;test&quot;", text)

    @patch("requests.post")
    def test_telegram_error_resilience(self, mock_post):
        """HTTP errors and network failures return False cleanly."""
        alert = make_rich_alert()

        # HTTP 429
        mock_post.return_value = MockResponse(
            status_code=429,
            json_data={"ok": False, "parameters": {"retry_after": 30}},
        )
        self.assertFalse(self.notifier.send(alert))

        # HTTP 400 Bad Request
        mock_post.return_value = MockResponse(
            status_code=400,
            json_data={"ok": False, "description": "Bad Request: can't parse entities"},
        )
        self.assertFalse(self.notifier.send(alert))

        # Timeout
        mock_post.side_effect = requests.exceptions.Timeout("Connection timed out")
        self.assertFalse(self.notifier.send(alert))

        # Connection error
        mock_post.side_effect = requests.exceptions.ConnectionError("DNS failure")
        self.assertFalse(self.notifier.send(alert))

    @patch("requests.post")
    def test_telegram_empty_event_and_direct_url_fallback(self, mock_post):
        """
        When alert.event_url is empty string or None and direct_checkout_url is None:
        Must NOT emit <a href="">. Must emit plain text fallback e.g. '👉 Visit AllAccess to purchase'.
        """
        mock_post.return_value = MockResponse(status_code=200, json_data={"ok": True})
        alert = TicketAlert(
            event_name="Foo Fighters",
            section_name="Sivori Media",
            status="AVAILABLE",
            event_url="",
            direct_checkout_url=None,
        )

        success = self.notifier.send(alert)
        self.assertTrue(success)

        text = mock_post.call_args[1]["json"]["text"]
        self.assertNotIn('<a href=""', text)
        self.assertNotIn("<a href=''", text)
        self.assertIn("👉 Visit AllAccess to purchase", text)

    @patch("requests.post")
    def test_telegram_direct_url_only_branch(self, mock_post):
        """
        When alert.direct_checkout_url is present but event_url is empty:
        Must emit direct purchase link and not emit empty event link.
        """
        mock_post.return_value = MockResponse(status_code=200, json_data={"ok": True})
        alert = TicketAlert(
            event_name="Foo Fighters",
            section_name="Sivori Media",
            status="AVAILABLE",
            event_url="",
            direct_checkout_url="https://www.allaccess.com.ar/checkout/123",
        )

        success = self.notifier.send(alert)
        self.assertTrue(success)

        text = mock_post.call_args[1]["json"]["text"]
        self.assertNotIn('<a href=""', text)
        self.assertIn('🛒 <a href="https://www.allaccess.com.ar/checkout/123">Direct Purchase / Checkout Link</a>', text)
        self.assertNotIn("Event Page on AllAccess", text)


# ==============================================================================
# 2. Discord Notifier Formatting Tests
# ==============================================================================

class TestDiscordAlertFormatting(unittest.TestCase):
    """Verifies Discord Webhook rich embed structure, inline layout, and limits."""

    def setUp(self):
        self.notifier = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/123456789/test-token",
            username="AllAccess Monitor Bot",
            timeout=5.0,
        )

    @patch("requests.post")
    def test_discord_rich_embed_all_6_fields(self, mock_post):
        """Discord embed includes all required fields with proper inline layout."""
        mock_post.return_value = MockResponse(status_code=204)
        alert = make_rich_alert()

        success = self.notifier.send(alert)
        self.assertTrue(success)

        payload = mock_post.call_args[1]["json"]
        self.assertEqual(payload["username"], "AllAccess Monitor Bot")
        self.assertIn("@everyone", payload["content"])
        self.assertIn("Campo Delantero", payload["content"])

        embed = payload["embeds"][0]
        self.assertIn("Paul McCartney", embed["title"])
        self.assertIn("Campo Delantero", embed["title"])
        self.assertEqual(embed["color"], EMBED_COLOR_AVAILABLE)
        self.assertEqual(embed["url"], alert.direct_checkout_url)

        fields = embed["fields"]
        field_map = {f["name"]: f for f in fields}

        # Sector / Section field (must contain 'section' for backward compatibility)
        self.assertIn("Sector / Section", field_map)
        self.assertEqual(field_map["Sector / Section"]["value"], "Campo Delantero")
        self.assertTrue(field_map["Sector / Section"]["inline"])

        # Date / Session field
        self.assertIn("Date / Session", field_map)
        self.assertIn("2026-11-15 21:00", field_map["Date / Session"]["value"])
        self.assertTrue(field_map["Date / Session"]["inline"])

        # Status field
        self.assertIn("Status", field_map)
        self.assertEqual(field_map["Status"]["value"], "🟢 Available")
        self.assertTrue(field_map["Status"]["inline"])

        # Price field
        self.assertIn("Price", field_map)
        self.assertIn("143,750 ARS", field_map["Price"]["value"])
        self.assertTrue(field_map["Price"]["inline"])

        # Direct Checkout Link field (full width)
        self.assertIn("Direct Checkout", field_map)
        self.assertIn(alert.direct_checkout_url, field_map["Direct Checkout"]["value"])
        self.assertFalse(field_map["Direct Checkout"]["inline"])

        # Event Link field (full width)
        self.assertIn("Event Link", field_map)
        self.assertIn(alert.event_url, field_map["Event Link"]["value"])
        self.assertFalse(field_map["Event Link"]["inline"])

    @patch("requests.post")
    def test_discord_reminder_color_and_content(self, mock_post):
        """Reminder alerts use amber color (0xF1C40F) and content contains '🔔 Reminder:'."""
        mock_post.return_value = MockResponse(status_code=204)
        alert = make_rich_alert(is_reminder=True)

        self.notifier.send(alert)
        payload = mock_post.call_args[1]["json"]
        self.assertIn("🔔 Reminder:", payload["content"])
        self.assertEqual(payload["embeds"][0]["color"], EMBED_COLOR_REMINDER)

    @patch("requests.post")
    def test_discord_missing_session_graceful(self, mock_post):
        """When session metadata is missing, Date/Session field is omitted cleanly without 'None'."""
        mock_post.return_value = MockResponse(status_code=204)
        legacy_alert = TicketAlert(
            event_name="Foo Fighters",
            section_name="Sivori media",
            status="AVAILABLE",
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            price=125000.0,
        )

        self.notifier.send(legacy_alert)
        embed = mock_post.call_args[1]["json"]["embeds"][0]
        field_names = [f["name"] for f in embed["fields"]]
        field_values = [f["value"] for f in embed["fields"]]

        self.assertNotIn("Date / Session", field_names)
        self.assertNotIn("Direct Checkout", field_names)
        self.assertIn("Event Link", field_names)
        self.assertFalse(any("none" in v.lower() for v in field_values))

    @patch("requests.post")
    def test_discord_field_value_length_truncation(self, mock_post):
        """Fields exceeding Discord character limits are safely clamped."""
        mock_post.return_value = MockResponse(status_code=204)
        huge_section = "S" * 2000
        huge_session = "D" * 2000
        alert = make_rich_alert(section_name=huge_section, session_name=huge_session)

        self.notifier.send(alert)
        embed = mock_post.call_args[1]["json"]["embeds"][0]
        for field in embed["fields"]:
            self.assertLessEqual(len(field["name"]), 256)
            self.assertLessEqual(len(field["value"]), 1024)
        self.assertLessEqual(len(embed["title"]), 256)

    @patch("requests.post")
    def test_discord_embed_total_character_limit_assessment(self, mock_post):
        """
        Stress test: Discord API limits total embed characters (title + description + fields + footer) to 6000.
        When alert fields are gigantic, notifier must clamp total embed characters <= 6000.
        """
        mock_post.return_value = MockResponse(status_code=204)
        alert = TicketAlert(
            event_name="E" * 12000,
            section_name="S" * 10500,
            status="AVAILABLE",
            event_url="https://example.com/" + "U" * 10000,
            direct_checkout_url="https://example.com/checkout/" + "C" * 10000,
            session_name="Session " + "X" * 5000,
            price=125000.0,
        )

        success = self.notifier.send(alert)
        self.assertTrue(success)
        mock_post.assert_called_once()

        payload = mock_post.call_args[1]["json"]
        embed = payload["embeds"][0]

        total_chars = (
            len(embed.get("title", ""))
            + len(embed.get("description", ""))
            + len(embed.get("footer", {}).get("text", ""))
            + sum(len(f.get("name", "")) + len(f.get("value", "")) for f in embed.get("fields", []))
        )
        self.assertLessEqual(
            total_chars,
            6000,
            f"Discord embed total characters ({total_chars}) exceeds Discord API maximum of 6000 characters."
        )
        # Verify valid schema: all field values non-empty, title non-empty
        self.assertGreater(len(embed["title"]), 0)
        for f in embed["fields"]:
            self.assertGreater(len(f["value"]), 0)

    @patch("requests.post")
    def test_discord_none_guards(self, mock_post):
        """When event_name and section_name are None, Discord notifier falls back to Event and Sector."""
        mock_post.return_value = MockResponse(status_code=204)
        alert = TicketAlert(
            event_name=None,
            section_name=None,
            status="AVAILABLE",
            event_url="https://example.com/event",
        )

        success = self.notifier.send(alert)
        self.assertTrue(success)
        payload = mock_post.call_args[1]["json"]
        self.assertIn("Sector", payload["content"])
        embed = payload["embeds"][0]
        self.assertIn("Event", embed["title"])
        self.assertIn("Sector", embed["title"])


# ==============================================================================
# 3. Email Notifier Formatting Tests
# ==============================================================================

class TestEmailAlertFormatting(unittest.TestCase):
    """Verifies Email SMTP multipart plain-text and responsive HTML formatting."""

    def setUp(self):
        self.notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_port=587,
            username="alerts@example.com",
            password="secret_password",
            from_addr="alerts@example.com",
            to_addrs=["fan@example.com"],
            use_tls=True,
        )

    @patch("smtplib.SMTP")
    def test_email_plain_text_all_6_fields(self, mock_smtp_cls):
        """Plain-text email alternative contains structured key-value layout of all 6 fields."""
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_smtp
        alert = make_rich_alert()

        success = self.notifier.send(alert)
        self.assertTrue(success)

        msg = mock_smtp.send_message.call_args[0][0]
        self.assertTrue(msg.is_multipart())

        text_part = next(p for p in msg.iter_parts() if p.get_content_type() == "text/plain")
        body = text_part.get_content()

        self.assertIn("Event:        Paul McCartney", body)
        self.assertIn("Date/Session: 2026-11-15 21:00 (Noche 1 - 21:00)", body)
        self.assertIn("Section:      Campo Delantero", body)
        self.assertIn("Status:       Available", body)
        self.assertIn("Price:        $143,750 ARS", body)
        self.assertIn("Time:         2026-11-15 21:00:00 UTC", body)
        self.assertIn("Purchase:     https://www.allaccess.com.ar/event/paul-mccartney/12345", body)
        self.assertIn("Event Page:   https://www.allaccess.com.ar/event/paul-mccartney", body)

    @patch("smtplib.SMTP")
    def test_email_html_all_6_fields_and_cta(self, mock_smtp_cls):
        """HTML email alternative contains responsive card, table rows, and CTA button."""
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_smtp
        alert = make_rich_alert()

        self.notifier.send(alert)
        msg = mock_smtp.send_message.call_args[0][0]

        html_part = next(p for p in msg.iter_parts() if p.get_content_type() == "text/html")
        body = html_part.get_content()

        self.assertIn("Paul McCartney", body)
        self.assertIn("Campo Delantero", body)
        self.assertIn("2026-11-15 21:00 (Noche 1 - 21:00)", body)
        self.assertIn("🟢 Available", body)
        self.assertIn("$143,750 ARS", body)
        self.assertIn("2026-11-15 21:00:00 UTC", body)
        self.assertIn('href="https://www.allaccess.com.ar/event/paul-mccartney/12345"', body)
        self.assertIn("Buy Tickets on AllAccess", body)
        self.assertIn("#2ECC71", body)

    @patch("smtplib.SMTP")
    def test_email_reminder_theme_color_and_subject(self, mock_smtp_cls):
        """Reminder email adopts amber theme #F39C12 and [REMINDER] subject prefix."""
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_smtp
        alert = make_rich_alert(is_reminder=True)

        self.notifier.send(alert)
        msg = mock_smtp.send_message.call_args[0][0]

        self.assertIn("🚨 [REMINDER]", msg["Subject"])
        html_part = next(p for p in msg.iter_parts() if p.get_content_type() == "text/html")
        body = html_part.get_content()
        self.assertIn("#F39C12", body)
        self.assertIn("🔔 Available (Reminder)", body)

    @patch("smtplib.SMTP")
    def test_email_missing_session_omitted_without_none(self, mock_smtp_cls):
        """When session metadata is missing, Date/Session is omitted and 'None' is not rendered."""
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_smtp
        legacy_alert = TicketAlert(
            event_name="Foo Fighters",
            section_name="Sivori media",
            status="AVAILABLE",
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            price=125000.0,
        )

        self.notifier.send(legacy_alert)
        msg = mock_smtp.send_message.call_args[0][0]

        text_part = next(p for p in msg.iter_parts() if p.get_content_type() == "text/plain").get_content()
        html_part = next(p for p in msg.iter_parts() if p.get_content_type() == "text/html").get_content()

        self.assertNotIn("Date/Session:", text_part)
        self.assertNotIn("None", text_part)
        self.assertNotIn("None", html_part)
        self.assertIn("Purchase:     https://www.allaccess.com.ar/event/foo-fighters", text_part)

    @patch("smtplib.SMTP")
    def test_email_none_event_and_section_name_guard(self, mock_smtp_cls):
        """
        When alert.event_name or alert.section_name is None:
        EmailNotifier must NOT raise AttributeError or TypeError in html.escape.
        Must fall back gracefully to 'Event' and 'Sector'.
        """
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server

        alert = TicketAlert(
            event_name=None,
            section_name=None,
            status="AVAILABLE",
            event_url="https://www.allaccess.com.ar/event/test",
        )

        success = self.notifier.send(alert)
        self.assertTrue(success)
        mock_server.send_message.assert_called_once()
        msg = mock_server.send_message.call_args[0][0]
        self.assertIn("Event", msg["Subject"])
        self.assertIn("Sector", msg["Subject"])


# ==============================================================================
# 4. Console Notifier Formatting Tests
# ==============================================================================

class TestConsoleAlertFormatting(unittest.TestCase):
    """Verifies Console / stdout banner formatting, ANSI styling, and Windows fallbacks."""

    def test_console_banner_all_6_fields(self):
        """Console banner outputs 78-col box with all required fields."""
        stream = io.StringIO()
        notifier = ConsoleNotifier(stream=stream, use_color=False)
        alert = make_rich_alert()

        success = notifier.send(alert)
        self.assertTrue(success)

        output = stream.getvalue()
        self.assertIn("=" * 78, output)
        self.assertIn("[ALERT] TICKETS AVAILABLE: Paul McCartney", output)
        self.assertIn("Date/Session: 2026-11-15 21:00 (Noche 1 - 21:00)", output)
        self.assertIn("Section:      Campo Delantero", output)
        self.assertIn("Status:       AVAILABLE", output)
        self.assertIn("Price:        $143,750 ARS", output)
        self.assertIn("Time:         2026-11-15 21:00:00 UTC", output)
        self.assertIn("Purchase:     https://www.allaccess.com.ar/event/paul-mccartney/12345", output)
        self.assertIn("Event Page:   https://www.allaccess.com.ar/event/paul-mccartney", output)

    def test_console_reminder_banner(self):
        """Reminder alert renders [REMINDER] header in console banner."""
        stream = io.StringIO()
        notifier = ConsoleNotifier(stream=stream, use_color=False)
        alert = make_rich_alert(is_reminder=True)

        notifier.send(alert)
        output = stream.getvalue()
        self.assertIn("[REMINDER] TICKETS AVAILABLE: Paul McCartney", output)
        self.assertIn("Status:       AVAILABLE (REMINDER)", output)

    def test_console_missing_session_omitted_without_none(self):
        """When session metadata is missing, Date/Session line is omitted and 'None' is absent."""
        stream = io.StringIO()
        notifier = ConsoleNotifier(stream=stream, use_color=False)
        legacy_alert = TicketAlert(
            event_name="Foo Fighters",
            section_name="Sivori media",
            status="AVAILABLE",
            event_url="https://www.allaccess.com.ar/event/foo-fighters",
            price=125000.0,
        )

        notifier.send(legacy_alert)
        output = stream.getvalue()
        self.assertNotIn("Date/Session:", output)
        self.assertNotIn("None", output)
        self.assertIn("Purchase:     https://www.allaccess.com.ar/event/foo-fighters", output)

    def test_console_ansi_colors_on_explicit_use_color(self):
        """Explicit use_color=True outputs ANSI color escape sequences."""
        stream = io.StringIO()
        notifier = ConsoleNotifier(stream=stream, use_color=True)
        alert = make_rich_alert()

        notifier.send(alert)
        output = stream.getvalue()
        self.assertIn("\033[1;32m", output)  # Bold green header
        self.assertIn("\033[1;36m", output)  # Cyan price
        self.assertIn("\033[4;34m", output)  # Underlined blue URL
        self.assertIn("\033[0m", output)

    def test_console_no_ansi_colors_on_stringio_default(self):
        """Default stream=io.StringIO() detects non-TTY and emits zero ANSI codes."""
        stream = io.StringIO()
        notifier = ConsoleNotifier(stream=stream)  # use_color=None (auto-detect)
        alert = make_rich_alert()

        notifier.send(alert)
        output = stream.getvalue()
        self.assertNotIn("\033[", output)

    def test_console_windows_ascii_fallback(self):
        """When target stream raises UnicodeEncodeError, notifier recovers with safe ASCII output."""
        class ASCIIOnlyStream(io.StringIO):
            def write(self, s: str):
                s.encode("ascii")
                return super().write(s)

        stream = ASCIIOnlyStream()
        notifier = ConsoleNotifier(stream=stream)
        alert = make_rich_alert(
            event_name="Paul McCartney 🎸 - Estadio River Plate",
            section_name="Platea Sívori Media 🎟️",
        )

        success = notifier.send(alert)
        self.assertTrue(success)
        output = stream.getvalue()
        self.assertIn("Paul McCartney", output)
        self.assertIn("Platea S?vori Media", output)
        output.encode("ascii")


# ==============================================================================
# 5. Deduplication, Cooldown & State Machine Enhancement Tests
# ==============================================================================

class TestDeduplicationAndStateEnhancements(unittest.TestCase):
    """Verifies per-target cooldown isolation, reminders, price drop triggers, and flapping defense."""

    def test_per_target_cooldown_isolation(self):
        """Cooldown on Target A does not suppress or throttle alerts for Target B."""
        manager = AlertStateManager(cooldown_seconds=1800)

        target_a = MonitoredTarget(event_url="https://allaccess.com.ar/event/foo", target_section="Sivori")
        target_b = MonitoredTarget(event_url="https://allaccess.com.ar/event/foo", target_section="Platea")

        # t=0: Target A becomes AVAILABLE -> Should alert
        res_a1 = CheckResult(status=AvailabilityStatus.AVAILABLE, section_name="Sivori", event_url=target_a.event_url)
        d_a1 = manager.process_check_result(res_a1, current_time=1000.0, target=target_a)
        self.assertTrue(d_a1.should_alert)
        self.assertFalse(d_a1.is_reminder)

        # t=10: Target B becomes AVAILABLE -> Should alert (independent partition!)
        res_b1 = CheckResult(status=AvailabilityStatus.AVAILABLE, section_name="Platea", event_url=target_b.event_url)
        d_b1 = manager.process_check_result(res_b1, current_time=1010.0, target=target_b)
        self.assertTrue(d_b1.should_alert, "Target B must alert independently despite Target A being in cooldown!")
        self.assertFalse(d_b1.is_reminder)

        # t=20: Target A is still available -> Throttled under Target A's cooldown
        d_a2 = manager.process_check_result(res_a1, current_time=1020.0, target=target_a)
        self.assertFalse(d_a2.should_alert)

    def test_cooldown_reminder_alert_dispatch(self):
        """When availability persists beyond cooldown_seconds, a reminder alert is dispatched."""
        manager = AlertStateManager(cooldown_seconds=1800)
        target = MonitoredTarget(event_url="https://allaccess.com.ar/event/foo", target_section="Sivori")
        result = CheckResult(status=AvailabilityStatus.AVAILABLE, section_name="Sivori", event_url=target.event_url)

        # t=1000: Initial alert
        d1 = manager.process_check_result(result, current_time=1000.0, target=target)
        self.assertTrue(d1.should_alert)
        self.assertFalse(d1.is_reminder)

        # t=2000 (< 2800): Throttled
        d2 = manager.process_check_result(result, current_time=2000.0, target=target)
        self.assertFalse(d2.should_alert)

        # t=2800 (elapsed == 1800): Reminder alert!
        d3 = manager.process_check_result(result, current_time=2800.0, target=target)
        self.assertTrue(d3.should_alert)
        self.assertTrue(d3.is_reminder)

    def test_price_drop_triggers_immediate_realert_within_cooldown(self):
        """If ticket price drops while remaining continuously available, an immediate alert fires."""
        manager = AlertStateManager(cooldown_seconds=1800)
        target = MonitoredTarget(event_url="https://allaccess.com.ar/event/foo", target_section="Sivori")

        # t=1000: Initial detection at $150,000
        sec1 = SectionInfo(name="Sivori", available=True, price=150000.0)
        res1 = CheckResult(status=AvailabilityStatus.AVAILABLE, section_info=sec1, event_url=target.event_url)
        d1 = manager.process_check_result(res1, current_time=1000.0, target=target)
        self.assertTrue(d1.should_alert)
        self.assertFalse(d1.is_reminder)

        # t=1060: Same price $150,000 -> Throttled under cooldown
        d2 = manager.process_check_result(res1, current_time=1060.0, target=target)
        self.assertFalse(d2.should_alert)

        # t=1120: Price drops to $120,000 -> Immediate re-alert!
        sec2 = SectionInfo(name="Sivori", available=True, price=120000.0)
        res2 = CheckResult(status=AvailabilityStatus.AVAILABLE, section_info=sec2, event_url=target.event_url)
        d3 = manager.process_check_result(res2, current_time=1120.0, target=target)
        self.assertTrue(d3.should_alert, "Price drop must trigger an immediate re-alert!")
        self.assertFalse(d3.is_reminder)
        self.assertIn("price drop", d3.reason.lower())

        # t=1180: Price increases back to $130,000 -> Throttled (price increase does not re-alert)
        sec3 = SectionInfo(name="Sivori", available=True, price=130000.0)
        res3 = CheckResult(status=AvailabilityStatus.AVAILABLE, section_info=sec3, event_url=target.event_url)
        d4 = manager.process_check_result(res3, current_time=1180.0, target=target)
        self.assertFalse(d4.should_alert)

    def test_rapid_flapping_defense_with_dampening(self):
        """When flapping_cooldown_seconds > 0, rapid status oscillation is dampened."""
        manager = AlertStateManager(cooldown_seconds=1800, flapping_cooldown_seconds=180.0)
        target = MonitoredTarget(event_url="https://allaccess.com.ar/event/foo", target_section="Sivori")

        res_avail = CheckResult(status=AvailabilityStatus.AVAILABLE, section_name="Sivori", event_url=target.event_url)
        res_unavail = CheckResult(status=AvailabilityStatus.UNAVAILABLE, section_name="Sivori", event_url=target.event_url)

        # t=1000: Initial alert
        d1 = manager.process_check_result(res_avail, current_time=1000.0, target=target)
        self.assertTrue(d1.should_alert)

        # t=1030: Cart hold creates temporary unavailability
        d2 = manager.process_check_result(res_unavail, current_time=1030.0, target=target)
        self.assertFalse(d2.should_alert)

        # t=1045: Cart expires, ticket returns after only 15s (< 180s flapping window) -> Suppressed!
        d3 = manager.process_check_result(res_avail, current_time=1045.0, target=target)
        self.assertFalse(d3.should_alert, "Rapid flapping within flapping_cooldown_seconds must be dampened!")

        # t=1300: Ticket becomes available again after 255s (> 180s) -> Genuine alert!
        manager.process_check_result(res_unavail, current_time=1046.0, target=target)
        d4 = manager.process_check_result(res_avail, current_time=1300.0, target=target)
        self.assertTrue(d4.should_alert, "Reappearance after flapping cooldown window must alert!")

    def test_state_persistence_and_reload(self):
        """Multi-target state and price history cleanly survive serialization and reload."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_file = Path(tmp_dir) / "monitor_state.json"
            mgr1 = AlertStateManager(cooldown_seconds=1800, state_file=state_file)

            target = MonitoredTarget(event_url="https://allaccess.com.ar/event/foo", target_section="Sivori")
            sec = SectionInfo(name="Sivori", available=True, price=125000.0)
            res = CheckResult(status=AvailabilityStatus.AVAILABLE, section_info=sec, event_url=target.event_url)

            # Trigger initial alert
            mgr1.process_check_result(res, current_time=1000.0, target=target)
            self.assertTrue(state_file.exists())

            # Load into fresh manager instance
            mgr2 = AlertStateManager(cooldown_seconds=1800, state_file=state_file)
            key = mgr2.get_target_key(target)
            ts = mgr2.get_target_state(target)

            self.assertTrue(ts.is_currently_available)
            self.assertEqual(ts.last_alert_time, 1000.0)
            self.assertEqual(ts.last_observed_price, 125000.0)
            self.assertEqual(ts.total_alerts_sent, 1)

    def test_scheduler_inter_alert_pacing(self):
        """PollingScheduler spaces consecutive alert dispatches when alert_burst_delay is configured."""
        checker = MagicMock()
        notifier = MagicMock()
        notifier.send.return_value = True

        targets = [
            MonitoredTarget(event_url="https://allaccess.com.ar/event/foo", target_section="Sivori"),
            MonitoredTarget(event_url="https://allaccess.com.ar/event/foo", target_section="Platea"),
        ]

        def fake_check(section):
            return CheckResult(
                status=AvailabilityStatus.AVAILABLE,
                section_name=section,
                section_info=SectionInfo(name=section, available=True, price=100000.0),
                event_url="https://allaccess.com.ar/event/foo",
            )
        checker.check_availability.side_effect = fake_check

        scheduler = PollingScheduler(
            checker=checker,
            notifier=notifier,
            targets=targets,
            alert_burst_delay=0.05,
        )

        with patch.object(scheduler._shutdown, "wait", wraps=scheduler._shutdown.wait) as mock_wait:
            scheduler.run_single_tick()
            self.assertEqual(notifier.send.call_count, 2)
            # Pacing wait should have been called with alert_burst_delay
            mock_wait.assert_any_call(0.05)


# ==============================================================================
# 6. Backward Compatibility Verification Tests
# ==============================================================================

class TestBackwardCompatibilityTier(unittest.TestCase):
    """Ensures legacy code, models, and tests continue to work without friction."""

    def test_legacy_ticket_alert_instantiation(self):
        """Instantiating TicketAlert with only the legacy 5 positional parameters succeeds."""
        alert = TicketAlert("Foo Fighters", "Sivori media", "AVAILABLE", "https://allaccess.com.ar", 125000.0)
        self.assertEqual(alert.event_name, "Foo Fighters")
        self.assertIsNone(alert.session_name)
        self.assertIsNone(alert.session_date)
        self.assertIsNone(alert.direct_checkout_url)

        # Verify notifiers accept legacy alert cleanly
        t_notif = TelegramNotifier(bot_token="tok", chat_id="123")
        d_notif = DiscordNotifier(webhook_url="https://discord.com/api/webhooks/123/token")
        e_notif = EmailNotifier(smtp_host="localhost", to_addrs=["a@b.com"])
        c_notif = ConsoleNotifier(stream=io.StringIO(), use_color=False)

        with patch("requests.post", return_value=MockResponse(200, {"ok": True})):
            self.assertTrue(t_notif.send(alert))
            self.assertTrue(d_notif.send(alert))
        self.assertTrue(c_notif.send(alert))

    def test_checkresult_to_alert_propagation(self):
        """CheckResult.to_alert() automatically forwards session and checkout URL metadata."""
        sec = SectionInfo(
            name="Sivori",
            available=True,
            price=125000.0,
            session_name="Show 1",
            session_date="2027-02-25",
            direct_checkout_url="https://allaccess.com.ar/event/foo/999",
        )
        res = CheckResult(
            status=AvailabilityStatus.AVAILABLE,
            section_info=sec,
            event_name="Foo Fighters",
            event_url="https://allaccess.com.ar/event/foo",
        )

        alert = res.to_alert(is_reminder=True)
        self.assertTrue(alert.is_reminder)
        self.assertEqual(alert.session_name, "Show 1")
        self.assertEqual(alert.session_date, "2027-02-25")
        self.assertEqual(alert.direct_checkout_url, "https://allaccess.com.ar/event/foo/999")


if __name__ == "__main__":
    unittest.main()
