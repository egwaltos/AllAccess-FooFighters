"""
Telegram notification channel implementation.
Dispatches ticket alerts via the Telegram Bot API sendMessage endpoint using HTML formatting.
"""
import html
import logging
from typing import Optional, Union
import requests

from allaccess_monitor.models import TicketAlert
from allaccess_monitor.notifier.base import BaseNotifier

logger = logging.getLogger(__name__)


class TelegramNotifier(BaseNotifier):
    """
    Notification delivery provider for Telegram Bots.
    Uses HTML parse mode with XML entity escaping and inline hyperlinks.
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: Union[str, int],
        parse_mode: str = "HTML",
        timeout: float = 10.0,
        timeout_seconds: Optional[float] = None,
    ):
        """
        Initialize Telegram notifier.

        Args:
            bot_token: Secret bot authentication token issued by BotFather.
            chat_id: Target user or channel/group ID.
            parse_mode: Message formatting mode (defaults to 'HTML').
            timeout: HTTP request timeout in seconds (default 10.0).
            timeout_seconds: Optional alias for timeout parameter.
        """
        self.bot_token = str(bot_token).strip()
        self.chat_id = str(chat_id).strip()
        self.parse_mode = parse_mode
        self.timeout = float(timeout_seconds if timeout_seconds is not None else timeout)
        self.api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"

    @property
    def channel_name(self) -> str:
        """Channel identifier for logging and metrics."""
        return "telegram"

    def send(self, alert: TicketAlert) -> bool:
        """
        Dispatches HTML-formatted alert to configured Telegram chat.

        Args:
            alert: The TicketAlert instance to deliver.

        Returns:
            bool: True if delivered with HTTP 200 and ok=True, False on any error.
        """
        if not self.bot_token or not self.chat_id:
            logger.error("Telegram notifier cannot send alert: bot_token or chat_id is empty.")
            return False

        prefix = "🔔 [REMINDER]" if alert.is_reminder else "🚨 [ALERT]"
        event_name_esc = html.escape(str(alert.event_name or "Event"), quote=False)
        section_name_esc = html.escape(str(alert.section_name or "Sector"), quote=False)
        title = f"{prefix} {event_name_esc} - {section_name_esc} AVAILABLE!"
        status_label = "🟢 Available" if not alert.is_reminder else "🔔 Available (Reminder)"
        if alert.status and alert.status not in ("AVAILABLE", "REMINDER"):
            status_label = f"ℹ️ {html.escape(str(alert.status), quote=False)}"

        time_str = alert.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")

        session_row = ""
        session_info = alert.session_name or alert.session_date
        if session_info:
            s_name = str(alert.session_name).strip() if alert.session_name else ""
            s_date = str(alert.session_date).strip() if alert.session_date else ""
            if s_name and s_date:
                s_str = s_name if (s_name == s_date or s_date in s_name) else f"{s_date} ({s_name})"
            elif s_name:
                s_str = s_name
            else:
                s_str = s_date
            session_row = f"📅 <b>Date/Session:</b> {html.escape(s_str, quote=False)}\n"

        price_row = ""
        if alert.price is not None:
            price_row = f"💰 <b>Price:</b> {html.escape(alert.formatted_price, quote=False)}\n"

        event_url = str(alert.event_url or "").strip()
        direct_url = str(alert.direct_checkout_url or "").strip()
        if direct_url and event_url and direct_url != event_url:
            direct_url_esc = html.escape(direct_url, quote=True)
            event_url_esc = html.escape(event_url, quote=True)
            links_block = (
                f"🛒 <a href=\"{direct_url_esc}\">Direct Purchase / Checkout Link</a>\n"
                f"🔗 <a href=\"{event_url_esc}\">Event Page on AllAccess</a>"
            )
        elif direct_url and not event_url:
            direct_url_esc = html.escape(direct_url, quote=True)
            links_block = f"🛒 <a href=\"{direct_url_esc}\">Direct Purchase / Checkout Link</a>"
        elif event_url:
            event_url_esc = html.escape(event_url, quote=True)
            links_block = f"👉 <a href=\"{event_url_esc}\">Click here to purchase on AllAccess</a>"
        else:
            links_block = "👉 Visit AllAccess to purchase"

        text = (
            f"<b>{title}</b>\n\n"
            f"📍 <b>Section:</b> {section_name_esc}\n"
            f"{session_row}"
            f"🎟️ <b>Status:</b> {status_label}\n"
            f"{price_row}"
            f"🕒 <b>Time:</b> {time_str}\n\n"
            f"{links_block}"
        )

        payload = {
            "chat_id": str(self.chat_id),
            "text": text,
            "parse_mode": self.parse_mode,
            "disable_web_page_preview": False,
        }

        try:
            resp = requests.post(self.api_url, json=payload, timeout=self.timeout)

            if resp.status_code == 200:
                try:
                    data = resp.json()
                    if data.get("ok", True):
                        logger.info("Telegram alert delivered successfully to chat_id=%s.", self.chat_id)
                        return True
                    logger.error("Telegram API returned 200 but ok=False: %s", data)
                    return False
                except Exception:
                    logger.info("Telegram alert delivered successfully (HTTP 200).")
                    return True

            if resp.status_code == 429:
                try:
                    retry_after = resp.json().get("parameters", {}).get("retry_after")
                    logger.warning("Telegram rate limit encountered (HTTP 429). Retry after: %s seconds.", retry_after)
                except Exception:
                    logger.warning("Telegram rate limit encountered (HTTP 429).")
                return False

            if resp.status_code == 400:
                logger.error("Telegram Bad Request (HTTP 400). Verify chat_id and formatting: %s", resp.text)
                return False

            if resp.status_code == 401:
                logger.error("Telegram Unauthorized (HTTP 401). Invalid bot token.")
                return False

            logger.error("Telegram API request failed with HTTP %d: %s", resp.status_code, resp.text)
            return False

        except requests.exceptions.Timeout as exc:
            logger.error("Telegram request timed out after %.1fs: %s", self.timeout, exc)
            return False
        except requests.exceptions.RequestException as exc:
            logger.error("Telegram request connection error: %s", exc)
            return False
        except Exception as exc:
            logger.exception("Unexpected error in Telegram notifier: %s", exc)
            return False
