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
        event_name = str(alert.event_name or "Event")
        section_name = str(alert.section_name or "Sector")
        content = f"@everyone {prefix} Tickets detected for **{section_name}**!"
        status_value = "🟢 Available" if not alert.is_reminder else "🔔 Reminder (Available)"
        if alert.status and alert.status not in ("AVAILABLE", "REMINDER"):
            status_value = f"ℹ️ {alert.status}"
        embed_color = EMBED_COLOR_REMINDER if alert.is_reminder else EMBED_COLOR_AVAILABLE

        fields = []

        # 1. Sector / Section field (contains 'section' for backward compatibility)
        fields.append({
            "name": "Sector / Section",
            "value": section_name[:1024],
            "inline": True,
        })

        # 2. Date / Session field (if available)
        session_info = alert.session_name or alert.session_date
        if session_info:
            s_name = str(alert.session_name).strip() if alert.session_name else ""
            s_date = str(alert.session_date).strip() if alert.session_date else ""
            if s_name and s_date:
                session_str = s_name if (s_name == s_date or s_date in s_name) else f"{s_date} ({s_name})"
            elif s_name:
                session_str = s_name
            else:
                session_str = s_date
            fields.append({
                "name": "Date / Session",
                "value": session_str[:1024],
                "inline": True,
            })

        # 3. Status field
        fields.append({
            "name": "Status",
            "value": status_value[:1024],
            "inline": True,
        })

        # 4. Price field (if available)
        if alert.price is not None:
            fields.append({
                "name": "Price",
                "value": alert.formatted_price[:1024],
                "inline": True,
            })

        # 5. Direct Purchase / Event Link fields
        event_url = str(alert.event_url or "").strip()
        direct_url = str(alert.direct_checkout_url or "").strip()
        if direct_url and direct_url != event_url:
            fields.append({
                "name": "Direct Checkout",
                "value": f"🛒 **[Click to Reserve Tickets]({direct_url})**"[:1024],
                "inline": False,
            })
            fields.append({
                "name": "Event Link",
                "value": f"[Click here to purchase on AllAccess]({event_url})"[:1024],
                "inline": False,
            })
        else:
            fields.append({
                "name": "Event Link",
                "value": f"[Click here to purchase on AllAccess]({event_url})"[:1024],
                "inline": False,
            })

        embed = {
            "title": f"{event_name} — {section_name} Available!"[:256],
            "url": alert.direct_checkout_url or alert.event_url,
            "description": f"Tickets in sector **{section_name}** are now available on AllAccess."[:4096],
            "color": embed_color,
            "fields": fields,
            "timestamp": alert.timestamp.isoformat(),
            "footer": {"text": "AllAccess Ticket Monitor"},
        }

        # Clamp total embed characters to Discord API hard limit of 6000.
        # Total = len(title) + len(description) + len(footer["text"]) + sum(len(f["name"]) + len(f["value"]) for f in fields) <= 6000
        total_chars = len(embed.get("title", "")) + len(embed.get("description", ""))
        total_chars += len(embed.get("footer", {}).get("text", ""))
        total_chars += sum(len(f.get("name", "")) + len(f.get("value", "")) for f in embed.get("fields", []))

        if total_chars > 6000:
            excess = total_chars - 6000
            # 1. Trim description first
            desc = embed.get("description", "")
            if len(desc) >= excess:
                embed["description"] = desc[:len(desc) - excess]
                excess = 0
            else:
                excess -= len(desc)
                embed["description"] = ""

            # 2. Trim field values if excess remains (preserving at least 1 character per field)
            if excess > 0:
                for f in reversed(embed.get("fields", [])):
                    val = f.get("value", "")
                    avail_trim = max(0, len(val) - 1)
                    if avail_trim >= excess:
                        f["value"] = val[:len(val) - excess]
                        excess = 0
                        break
                    else:
                        f["value"] = val[:1]
                        excess -= avail_trim

            # 3. Trim title if excess still remains (preserving at least 1 character)
            if excess > 0:
                title = embed.get("title", "")
                avail_trim = max(0, len(title) - 1)
                if avail_trim >= excess:
                    embed["title"] = title[:len(title) - excess]
                    excess = 0
                else:
                    embed["title"] = title[:1]
                    excess -= avail_trim

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
