"""
Milestone 2 Iteration 2 Empirical Challenger Stress Tests.
Independently written and executed by Challenger M2-1 (Iter 2).

Verifies the 3 defect areas identified in Milestone 2:
1. Discord 6000 total character ceiling enforcement under extreme and pathological inputs
2. Telegram empty URL anchor guard preventing invalid <a href=""> entities
3. Email NoneType guard preventing AttributeError / TypeError in html.escape and string formatting
"""
from datetime import datetime, timezone
from email.message import EmailMessage
import html
import json
import logging
from typing import Optional
import unittest
from unittest.mock import MagicMock, patch

from allaccess_monitor.models import TicketAlert
from allaccess_monitor.notifier.discord import DiscordNotifier
from allaccess_monitor.notifier.telegram import TelegramNotifier
from allaccess_monitor.notifier.email import EmailNotifier


class MockHTTPResponse:
    def __init__(self, status_code: int = 200, json_data: dict = None, text: str = ""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text or (json.dumps(self._json_data) if json_data is not None else "")
        self.headers = {}

    def json(self):
        return self._json_data


class TestEmpiricalDiscordCeiling(unittest.TestCase):
    """Stress tests for Discord embed character clamping."""

    def setUp(self):
        self.notifier = DiscordNotifier(
            webhook_url="https://discord.com/api/webhooks/test/empiric",
            username="Empirical Tester",
            timeout=5.0,
        )

    def _calc_total_chars(self, embed: dict) -> int:
        total = len(embed.get("title", "")) + len(embed.get("description", ""))
        total += len(embed.get("footer", {}).get("text", ""))
        total += sum(len(f.get("name", "")) + len(f.get("value", "")) for f in embed.get("fields", []))
        return total

    @patch("requests.post")
    def test_discord_extreme_pathological_payload(self, mock_post):
        """Massive 100k+ payload clamped strictly to <= 6000 with valid schema."""
        mock_post.return_value = MockHTTPResponse(204)

        alert = TicketAlert(
            event_name="Mega Festival " + "X" * 100000,
            section_name="VIP Zone " + "Y" * 50000,
            status="AVAILABLE",
            event_url="https://allaccess.com.ar/event/" + "U" * 20000,
            direct_checkout_url="https://allaccess.com.ar/checkout/" + "C" * 20000,
            session_name="Session Name " + "S" * 10000,
            session_date="2026-12-31 23:59 " + "D" * 5000,
            price=999999999.99,
        )

        success = self.notifier.send(alert)
        self.assertTrue(success)

        payload = mock_post.call_args[1]["json"]
        embed = payload["embeds"][0]

        total_chars = self._calc_total_chars(embed)
        self.assertLessEqual(total_chars, 6000, f"Total chars {total_chars} exceeded 6000")

        # Discord schema requirements: title and field values cannot be empty
        self.assertGreater(len(embed["title"]), 0)
        for field in embed["fields"]:
            self.assertGreater(len(field["name"]), 0)
            self.assertGreater(len(field["value"]), 0)

    @patch("requests.post")
    def test_discord_unicode_and_emojis_clamping(self, mock_post):
        """Multi-byte Unicode, astral symbols, and emojis."""
        mock_post.return_value = MockHTTPResponse(204)

        alert = TicketAlert(
            event_name="🎸 Rock Fest \U0001F600 \U0001F389 " + "🔥" * 2000,
            section_name="Platea Alta \u3042\u3044\u3046 " + "🎟️" * 1500,
            status="AVAILABLE",
            event_url="https://allaccess.com.ar/event/rock",
            price=150000.0,
        )

        success = self.notifier.send(alert)
        self.assertTrue(success)

        embed = mock_post.call_args[1]["json"]["embeds"][0]
        total_chars = self._calc_total_chars(embed)
        self.assertLessEqual(total_chars, 6000)

    @patch("requests.post")
    def test_discord_none_field_values_graceful_recovery(self, mock_post):
        """None fields in TicketAlert do not crash DiscordNotifier."""
        mock_post.return_value = MockHTTPResponse(204)

        alert = TicketAlert(
            event_name=None,
            section_name=None,
            status=None,
            event_url=None,
            price=None,
            session_name=None,
            session_date=None,
            direct_checkout_url=None,
        )

        success = self.notifier.send(alert)
        self.assertTrue(success)

        embed = mock_post.call_args[1]["json"]["embeds"][0]
        total_chars = self._calc_total_chars(embed)
        self.assertLessEqual(total_chars, 6000)
        self.assertIn("Event", embed["title"])
        self.assertIn("Sector", embed["title"])


class TestEmpiricalTelegramAnchorGuard(unittest.TestCase):
    """Stress tests for Telegram anchor tag formatting and empty URL handling."""

    def setUp(self):
        self.notifier = TelegramNotifier(
            bot_token="123456:FAKE_TOKEN",
            chat_id="12345678",
            parse_mode="HTML",
            timeout=5.0,
        )

    @patch("requests.post")
    def test_telegram_both_urls_empty_string(self, mock_post):
        """event_url='' and direct_checkout_url='' must NOT produce empty href."""
        mock_post.return_value = MockHTTPResponse(200, {"ok": True})
        alert = TicketAlert(
            event_name="Foo Fighters",
            section_name="Platea",
            status="AVAILABLE",
            event_url="",
            direct_checkout_url="",
        )
        self.assertTrue(self.notifier.send(alert))
        text = mock_post.call_args[1]["json"]["text"]
        self.assertNotIn('href=""', text)
        self.assertNotIn("href=''", text)
        self.assertIn("👉 Visit AllAccess to purchase", text)

    @patch("requests.post")
    def test_telegram_both_urls_whitespace(self, mock_post):
        """Whitespace-only URLs must NOT produce empty or whitespace href."""
        mock_post.return_value = MockHTTPResponse(200, {"ok": True})
        alert = TicketAlert(
            event_name="Foo Fighters",
            section_name="Platea",
            status="AVAILABLE",
            event_url="    ",
            direct_checkout_url="  \t  ",
        )
        self.assertTrue(self.notifier.send(alert))
        text = mock_post.call_args[1]["json"]["text"]
        self.assertNotIn("href=", text)
        self.assertIn("👉 Visit AllAccess to purchase", text)

    @patch("requests.post")
    def test_telegram_both_urls_none(self, mock_post):
        """None URLs must NOT produce empty href."""
        mock_post.return_value = MockHTTPResponse(200, {"ok": True})
        alert = TicketAlert(
            event_name="Foo Fighters",
            section_name="Platea",
            status="AVAILABLE",
            event_url=None,
            direct_checkout_url=None,
        )
        self.assertTrue(self.notifier.send(alert))
        text = mock_post.call_args[1]["json"]["text"]
        self.assertNotIn("href=", text)
        self.assertIn("👉 Visit AllAccess to purchase", text)

    @patch("requests.post")
    def test_telegram_direct_url_only(self, mock_post):
        """direct_checkout_url present, event_url None or empty -> direct link only."""
        mock_post.return_value = MockHTTPResponse(200, {"ok": True})
        alert = TicketAlert(
            event_name="Foo Fighters",
            section_name="Platea",
            status="AVAILABLE",
            event_url=None,
            direct_checkout_url="https://allaccess.com.ar/checkout/999",
        )
        self.assertTrue(self.notifier.send(alert))
        text = mock_post.call_args[1]["json"]["text"]
        self.assertNotIn('href=""', text)
        self.assertIn('🛒 <a href="https://allaccess.com.ar/checkout/999">Direct Purchase / Checkout Link</a>', text)
        self.assertNotIn("Event Page on AllAccess", text)

    @patch("requests.post")
    def test_telegram_event_url_only(self, mock_post):
        """event_url present, direct_checkout_url None or empty -> event page link only."""
        mock_post.return_value = MockHTTPResponse(200, {"ok": True})
        alert = TicketAlert(
            event_name="Foo Fighters",
            section_name="Platea",
            status="AVAILABLE",
            event_url="https://allaccess.com.ar/event/foo",
            direct_checkout_url="",
        )
        self.assertTrue(self.notifier.send(alert))
        text = mock_post.call_args[1]["json"]["text"]
        self.assertNotIn('href=""', text)
        self.assertIn('👉 <a href="https://allaccess.com.ar/event/foo">Click here to purchase on AllAccess</a>', text)
        self.assertNotIn("Direct Purchase", text)

    @patch("requests.post")
    def test_telegram_same_urls(self, mock_post):
        """When event_url == direct_checkout_url, avoid duplicate links."""
        mock_post.return_value = MockHTTPResponse(200, {"ok": True})
        url = "https://allaccess.com.ar/event/foo"
        alert = TicketAlert(
            event_name="Foo Fighters",
            section_name="Platea",
            status="AVAILABLE",
            event_url=url,
            direct_checkout_url=url,
        )
        self.assertTrue(self.notifier.send(alert))
        text = mock_post.call_args[1]["json"]["text"]
        self.assertIn(f'👉 <a href="{url}">Click here to purchase on AllAccess</a>', text)
        self.assertNotIn("Direct Purchase", text)

    @patch("requests.post")
    def test_telegram_adversarial_url_injection(self, mock_post):
        """Malicious characters in URLs must be properly quoted in HTML attribute."""
        mock_post.return_value = MockHTTPResponse(200, {"ok": True})
        alert = TicketAlert(
            event_name="Foo Fighters",
            section_name="Platea",
            status="AVAILABLE",
            event_url='https://allaccess.com.ar/event/foo?ref="><script>alert(1)</script>',
            direct_checkout_url=None,
        )
        self.assertTrue(self.notifier.send(alert))
        text = mock_post.call_args[1]["json"]["text"]
        self.assertNotIn('"><script>', text)
        self.assertIn('&quot;&gt;&lt;script&gt;', text)


class TestEmpiricalEmailNoneGuards(unittest.TestCase):
    """Stress tests for Email notifier NoneType guards and encoding."""

    def setUp(self):
        self.notifier = EmailNotifier(
            smtp_host="smtp.example.com",
            smtp_port=587,
            username="alerts@example.com",
            password="secret_password",
            from_addr="alerts@example.com",
            to_addrs=["user@example.com"],
            use_tls=True,
        )

    @patch("smtplib.SMTP")
    def test_email_all_none_payload(self, mock_smtp_cls):
        """All alert attributes as None does not raise AttributeError or crash."""
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server

        alert = TicketAlert(
            event_name=None,
            section_name=None,
            status=None,
            event_url=None,
            price=None,
            session_name=None,
            session_date=None,
            direct_checkout_url=None,
        )

        success = self.notifier.send(alert)
        self.assertTrue(success)

        mock_server.send_message.assert_called_once()
        msg: EmailMessage = mock_server.send_message.call_args[0][0]

        # Verify subject and parts
        self.assertIn("Event", msg["Subject"])
        self.assertIn("Sector", msg["Subject"])

        text_part = next(p for p in msg.iter_parts() if p.get_content_type() == "text/plain").get_content()
        html_part = next(p for p in msg.iter_parts() if p.get_content_type() == "text/html").get_content()

        self.assertIn("Event:        Event", text_part)
        self.assertIn("Section:      Sector", text_part)
        self.assertIn("Event", html_part)
        self.assertIn("Sector", html_part)

    @patch("smtplib.SMTP")
    def test_email_adversarial_html_injection(self, mock_smtp_cls):
        """Hostile HTML markup in event_name, section_name is safely escaped."""
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server

        alert = TicketAlert(
            event_name='<b>Festival</b> <img src=x onerror=alert(1)> & "Rock"',
            section_name='<script>alert("XSS")</script>',
            status="AVAILABLE",
            event_url="https://allaccess.com.ar/event/foo",
        )

        success = self.notifier.send(alert)
        self.assertTrue(success)

        msg: EmailMessage = mock_server.send_message.call_args[0][0]
        html_part = next(p for p in msg.iter_parts() if p.get_content_type() == "text/html").get_content()

        self.assertNotIn("<script>", html_part)
        self.assertNotIn("<img src=x", html_part)
        self.assertIn("&lt;script&gt;", html_part)
        self.assertIn("&lt;b&gt;Festival&lt;/b&gt;", html_part)


if __name__ == "__main__":
    unittest.main()
