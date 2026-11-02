"""
Email notification channel implementation.
Dispatches ticket alerts via SMTP supporting STARTTLS (port 587) and direct SSL (port 465).
"""
import html
import logging
import smtplib
import socket
import ssl
from email.message import EmailMessage
from typing import List, Optional, Union

from allaccess_monitor.models import TicketAlert
from allaccess_monitor.notifier.base import BaseNotifier

logger = logging.getLogger(__name__)


def _format_session_str(alert: TicketAlert) -> Optional[str]:
    """Format session and date information into a clean display string."""
    s_name = (alert.session_name or "").strip()
    s_date = (alert.session_date or "").strip()
    if s_name and s_date:
        if s_name == s_date or s_date in s_name:
            return s_name
        return f"{s_date} ({s_name})"
    elif s_name:
        return s_name
    elif s_date:
        return s_date
    return None


class EmailNotifier(BaseNotifier):
    """
    Notification delivery provider for SMTP email.
    Generates multipart plain text and HTML emails with clickable purchase buttons.
    """

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int = 587,
        username: str = "",
        password: str = "",
        from_addr: str = "",
        to_addrs: Optional[Union[List[str], str]] = None,
        use_tls: bool = True,
        use_ssl: bool = False,
        timeout: float = 15.0,
        timeout_seconds: Optional[float] = None,
    ):
        """
        Initialize Email notifier.

        Args:
            smtp_host: SMTP server hostname (e.g. 'smtp.gmail.com').
            smtp_port: SMTP port (default 587 for STARTTLS, 465 for SSL).
            username: SMTP authentication username / sender address.
            password: SMTP authentication password or App Password.
            from_addr: Envelope sender address (defaults to username if omitted).
            to_addrs: List or comma-separated string of recipient email addresses.
            use_tls: Whether to upgrade connection with STARTTLS (recommended for 587).
            use_ssl: Whether to establish direct SSL connection (recommended for 465).
            timeout: Socket timeout in seconds (default 15.0).
            timeout_seconds: Optional alias for timeout parameter.
        """
        self.smtp_host = str(smtp_host).strip()
        self.smtp_port = int(smtp_port)
        self.username = str(username).strip()
        self.password = str(password).strip()
        self.from_addr = str(from_addr).strip() or self.username
        self.use_tls = bool(use_tls)
        self.use_ssl = bool(use_ssl)
        self.timeout = float(timeout_seconds if timeout_seconds is not None else timeout)

        if isinstance(to_addrs, str):
            self.to_addrs = [item.strip() for item in to_addrs.split(",") if item.strip()]
        elif to_addrs is not None:
            self.to_addrs = [str(item).strip() for item in to_addrs if str(item).strip()]
        else:
            self.to_addrs = []

    @property
    def channel_name(self) -> str:
        """Channel identifier for logging and metrics."""
        return "email"

    def send(self, alert: TicketAlert) -> bool:
        """
        Dispatches multipart email alert via configured SMTP server.

        Args:
            alert: The TicketAlert instance to deliver.

        Returns:
            bool: True on successful SMTP delivery, False on any authentication or socket error.
        """
        if not self.smtp_host:
            logger.error("Email notifier cannot send alert: smtp_host is empty.")
            return False

        if not self.to_addrs:
            logger.error("Email notifier cannot send alert: to_addrs recipient list is empty.")
            return False

        event_name = str(alert.event_name or "Event")
        section_name = str(alert.section_name or "Sector")

        prefix = "[REMINDER]" if alert.is_reminder else "[ALERT]"
        subject = f"🚨 {prefix} {event_name} - {section_name} Tickets Available!"

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.from_addr or self.username
        msg["To"] = ", ".join(self.to_addrs)

        time_str = alert.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
        formatted_price = alert.formatted_price
        session_str = _format_session_str(alert)
        purchase_url = (alert.direct_checkout_url or alert.event_url or "").strip()
        event_url = (alert.event_url or "").strip()
        has_distinct_checkout = bool(alert.direct_checkout_url and alert.direct_checkout_url.strip() != event_url)

        # Plain Text Body
        session_line = f"Date/Session: {session_str}\n" if session_str else ""
        event_page_line = f"Event Page:   {event_url}\n" if has_distinct_checkout else ""
        status_label = "Available (Reminder)" if alert.is_reminder else "Available"

        text_body = (
            f"{subject}\n\n"
            f"Event:        {event_name}\n"
            f"{session_line}"
            f"Section:      {section_name}\n"
            f"Status:       {status_label}\n"
            f"Price:        {formatted_price}\n"
            f"Time:         {time_str}\n"
            f"Purchase:     {purchase_url}\n"
            f"{event_page_line}\n"
            f"Please visit the purchase link above immediately on AllAccess."
        )
        msg.set_content(text_body)

        # HTML Body styling
        theme_color = "#F39C12" if alert.is_reminder else "#2ECC71"
        header_title = "Tickets Still Available!" if alert.is_reminder else "Tickets Available Detected!"
        status_html = "🔔 Available (Reminder)" if alert.is_reminder else "🟢 Available"

        session_tr = f"""<tr>
          <td style="padding: 10px 14px; border-bottom: 1px solid #eef2f5; color: #7f8c8d; width: 35%;"><b>Date / Session:</b></td>
          <td style="padding: 10px 14px; border-bottom: 1px solid #eef2f5; color: #2c3e50; font-weight: 600;">{html.escape(session_str)}</td>
        </tr>""" if session_str else ""

        direct_tr = f"""<tr>
          <td style="padding: 10px 14px; border-bottom: 1px solid #eef2f5; color: #7f8c8d;"><b>Event Page:</b></td>
          <td style="padding: 10px 14px; border-bottom: 1px solid #eef2f5; color: #2c3e50;"><a href="{html.escape(event_url, quote=True)}" style="color: #3498db; text-decoration: underline;">Landing Page</a></td>
        </tr>""" if has_distinct_checkout else ""

        html_body = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{html.escape(subject)}</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f7f9fa; margin: 0; padding: 20px;">
  <div style="display:none;font-size:1px;color:#f7f9fa;line-height:1px;max-height:0px;max-width:0px;opacity:0;overflow:hidden;">
    Tickets detected for {html.escape(section_name)} ({html.escape(formatted_price)}) - {html.escape(event_name)}
  </div>
  <div style="max-width: 600px; margin: 0 auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.05); border: 1px solid #e1e8ed;">
    <div style="background-color: {theme_color}; padding: 24px; text-align: center; color: #ffffff;">
      <h1 style="margin: 0; font-size: 24px; font-weight: 700;">{html.escape(event_name)}</h1>
      <p style="margin: 6px 0 0 0; font-size: 16px; opacity: 0.95;">{header_title}</p>
    </div>
    <div style="padding: 28px;">
      <h2 style="color: #2c3e50; font-size: 18px; margin-top: 0;">{html.escape(subject)}</h2>
      <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
        <tr>
          <td style="padding: 10px 14px; border-bottom: 1px solid #eef2f5; color: #7f8c8d; width: 35%;"><b>Section:</b></td>
          <td style="padding: 10px 14px; border-bottom: 1px solid #eef2f5; color: #2c3e50; font-weight: 600;">{html.escape(section_name)}</td>
        </tr>
        {session_tr}
        <tr>
          <td style="padding: 10px 14px; border-bottom: 1px solid #eef2f5; color: #7f8c8d;"><b>Status:</b></td>
          <td style="padding: 10px 14px; border-bottom: 1px solid #eef2f5; color: #27ae60; font-weight: 600;">{status_html}</td>
        </tr>
        <tr>
          <td style="padding: 10px 14px; border-bottom: 1px solid #eef2f5; color: #7f8c8d;"><b>Price:</b></td>
          <td style="padding: 10px 14px; border-bottom: 1px solid #eef2f5; color: #2c3e50; font-weight: 600;">{html.escape(formatted_price)}</td>
        </tr>
        <tr>
          <td style="padding: 10px 14px; border-bottom: 1px solid #eef2f5; color: #7f8c8d;"><b>Detected:</b></td>
          <td style="padding: 10px 14px; border-bottom: 1px solid #eef2f5; color: #2c3e50;">{time_str}</td>
        </tr>
        {direct_tr}
      </table>
      <div style="text-align: center; margin: 32px 0;">
        <a href="{html.escape(purchase_url, quote=True)}" style="background-color: {theme_color}; color: #ffffff; padding: 14px 28px; text-decoration: none; border-radius: 6px; font-weight: 700; display: inline-block; font-size: 16px;">Buy Tickets on AllAccess</a>
      </div>
      <p style="font-size: 12px; color: #95a5a6; text-align: center; margin-top: 24px;">
        AllAccess Ticket Monitor Daemon
      </p>
    </div>
  </div>
</body>
</html>
"""
        msg.add_alternative(html_body, subtype="html")

        try:
            if self.use_ssl or self.smtp_port == 465:
                context = ssl.create_default_context()
                with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=context, timeout=self.timeout) as server:
                    if self.username and self.password:
                        server.login(self.username, self.password)
                    server.send_message(msg)
            else:
                with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout) as server:
                    if self.use_tls:
                        context = ssl.create_default_context()
                        server.starttls(context=context)
                    if self.username and self.password:
                        server.login(self.username, self.password)
                    server.send_message(msg)

            logger.info("Email alert delivered successfully to %s", self.to_addrs)
            return True

        except smtplib.SMTPAuthenticationError as exc:
            logger.error("SMTP authentication failure for user '%s': %s", self.username, exc)
            return False
        except smtplib.SMTPServerDisconnected as exc:
            logger.error("SMTP server disconnected unexpectedly: %s", exc)
            return False
        except (TimeoutError, socket.timeout) as exc:
            logger.error("SMTP connection timed out after %.1fs: %s", self.timeout, exc)
            return False
        except (smtplib.SMTPException, ssl.SSLError, OSError) as exc:
            logger.error("SMTP transmission error: %s", exc)
            return False
        except Exception as exc:
            logger.exception("Unexpected error in Email notifier: %s", exc)
            return False
