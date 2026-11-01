"""
Discord notification channel implementation.
Dispatches ticket alerts via Discord Webhooks using rich embeds.
"""
import logging
from typing import Optional
import requests

from allaccess_monitor.models import TicketAlert
from allaccess_monitor.notifier.base import BaseNotifier

logger = logging.getLogger(__name__)

# Emerald Green hex #2ECC71 as decimal integer
EMBED_COLOR_AVAILABLE = 3066993
# Gold hex #F1C40F as decimal integer for reminders
EMBED_COLOR_REMINDER = 15844367


class DiscordNotifier(BaseNotifier):
    """
    Notification delivery provider for Discord webhooks.
    Generates rich embeds with structured fields for section, status, price, and event link.
    """

    def __init__(
        self,
        webhook_url: str,
        username: str = "AllAccess Ticket Monitor",
        timeout: float = 10.0,
        timeout_seconds: Optional[float] = None,
    ):
        """
        Initialize Discord notifier.

        Args:
            webhook_url: Target Discord webhook execution endpoint URL.
            username: Override username displayed for the webhook bot.
            timeout: HTTP request timeout in seconds (default 10.0).
            timeout_seconds: Optional alias for timeout parameter.
        """
        self.webhook_url = str(webhook_url).strip()
        self.username = username
        self.timeout = float(timeout_seconds if timeout_seconds is not None else timeout)

    @property
    def channel_name(self) -> str:
        """Channel identifier for logging and metrics."""
        return "discord"

    def send(self, alert: TicketAlert) -> bool:
        """
        Dispatches rich embed alert to configured Discord webhook.

        Args:
            alert: The TicketAlert instance to deliver.

        Returns:
            bool: True on HTTP 200/204 delivery, False on any HTTP or network error.
        """
        if not self.webhook_url:
            logger.error("Discord notifier cannot send alert: webhook_url is empty.")
            return False

        prefix = "🔔 Reminder:" if alert.is_reminder else "🚨 ALERT:"
        content = f"@everyone {prefix} Tickets detected for **{alert.section_name}**!"
        status_value = "🟢 Available" if not alert.is_reminder else "🔔 Reminder (Available)"
        embed_color = EMBED_COLOR_REMINDER if alert.is_reminder else EMBED_COLOR_AVAILABLE

        fields = [
            {"name": "Section", "value": alert.section_name, "inline": True},
            {"name": "Status", "value": status_value, "inline": True},
        ]

        if alert.price is not None:
            fields.append({"name": "Price", "value": alert.formatted_price, "inline": True})

        fields.append({
            "name": "Event Link",
            "value": f"[Click here to purchase on AllAccess]({alert.event_url})",
            "inline": False,
        })

        embed = {
            "title": f"{alert.event_name} — {alert.section_name} Available!",
            "url": alert.event_url,
            "description": f"Tickets in section **{alert.section_name}** are now available on AllAccess.",
            "color": embed_color,
            "fields": fields,
            "timestamp": alert.timestamp.isoformat(),
            "footer": {"text": "AllAccess Ticket Monitor"},
        }

        payload = {
            "username": self.username,
            "content": content,
            "embeds": [embed],
        }

        try:
            resp = requests.post(self.webhook_url, json=payload, timeout=self.timeout)

            # Discord returns 204 No Content on successful webhook execution
            if resp.status_code in (200, 204):
                logger.info("Discord alert delivered successfully to webhook.")
                return True

            if resp.status_code == 429:
                try:
                    retry_data = resp.json()
                    retry_after = retry_data.get("retry_after", resp.headers.get("Retry-After"))
                    logger.warning("Discord webhook rate-limited (HTTP 429). Retry after: %s", retry_after)
                except Exception:
                    logger.warning("Discord webhook rate-limited (HTTP 429).")
                return False

            if resp.status_code == 404:
                logger.error("Discord webhook not found (HTTP 404). Check webhook URL validity: %s", self.webhook_url)
                return False

            if resp.status_code == 400:
                logger.error("Discord webhook bad request (HTTP 400). Payload error: %s", resp.text)
                return False

            if resp.status_code >= 500:
                logger.error("Discord server error (HTTP %d): %s", resp.status_code, resp.text)
                return False

            logger.error("Discord webhook failed with HTTP %d: %s", resp.status_code, resp.text)
            return False

        except requests.exceptions.ConnectionError as exc:
            logger.error("Discord webhook connection error (DNS/network failure): %s", exc)
            return False
        except requests.exceptions.Timeout as exc:
            logger.error("Discord webhook timed out after %.1fs: %s", self.timeout, exc)
            return False
        except requests.exceptions.RequestException as exc:
            logger.error("Discord webhook request exception: %s", exc)
            return False
        except Exception as exc:
            logger.exception("Unexpected error in Discord notifier: %s", exc)
            return False
