"""
Console notification channel implementation.
Dispatches formatted alert summaries to stdout or a designated text stream for dry-run and local testing.
"""
import logging
import re
import sys
from typing import Optional, TextIO

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


class ConsoleNotifier(BaseNotifier):
    """
    Console and dry-run notification delivery provider.
    Outputs structured alert banners to terminal or stream without external network requests.
    Supports optional ANSI colors when running in interactive terminals.
    """

    def __init__(self, stream: Optional[TextIO] = None, use_color: Optional[bool] = None):
        """
        Initialize Console notifier.

        Args:
            stream: Target text stream (defaults to dynamic sys.stdout).
            use_color: Explicitly enable/disable ANSI colors. Defaults to auto-detecting TTY.
        """
        self._stream = stream
        self._use_color = use_color

    @property
    def stream(self) -> TextIO:
        """Target output stream."""
        return self._stream if self._stream is not None else sys.stdout

    @property
    def channel_name(self) -> str:
        """Channel identifier for logging and metrics."""
        return "console"

    def _should_use_color(self) -> bool:
        """Determines if ANSI colors should be emitted to target stream."""
        if self._use_color is not None:
            return bool(self._use_color)
        target = self.stream
        try:
            return hasattr(target, "isatty") and target.isatty()
        except Exception:
            return False

    def send(self, alert: TicketAlert) -> bool:
        """
        Writes structured alert notification to output stream.

        Args:
            alert: The TicketAlert instance to display.

        Returns:
            bool: Always True on successful stream write.
        """
        prefix = "[REMINDER]" if alert.is_reminder else "[ALERT]"
        time_str = alert.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
        session_str = _format_session_str(alert)
        purchase_url = (alert.direct_checkout_url or alert.event_url or "").strip()
        event_url = (alert.event_url or "").strip()
        has_distinct_checkout = bool(alert.direct_checkout_url and alert.direct_checkout_url.strip() != event_url)

        session_line = f"Date/Session: {session_str}\n" if session_str else ""
        event_page_line = f"Event Page:   {event_url}\n" if has_distinct_checkout else ""
        status_label = "AVAILABLE (REMINDER)" if alert.is_reminder else "AVAILABLE"

        color_active = self._should_use_color()

        if color_active:
            c_header = "\033[1;33m" if alert.is_reminder else "\033[1;32m"
            c_status = "\033[1;33m" if alert.is_reminder else "\033[1;32m"
            c_price = "\033[1;36m"
            c_link = "\033[4;34m"
            c_bold = "\033[1m"
            c_reset = "\033[0m"

            banner = (
                f"\n{c_header}{'=' * 78}{c_reset}\n"
                f"{c_header}{prefix} TICKETS AVAILABLE: {alert.event_name}{c_reset}\n"
                f"{c_header}{'=' * 78}{c_reset}\n"
                f"{f'{c_bold}Date/Session:{c_reset} {session_str}\n' if session_str else ''}"
                f"{c_bold}Section:{c_reset}      {alert.section_name}\n"
                f"{c_bold}Status:{c_reset}       {c_status}{status_label}{c_reset}\n"
                f"{c_bold}Price:{c_reset}        {c_price}{alert.formatted_price}{c_reset}\n"
                f"{c_bold}Time:{c_reset}         {time_str}\n"
                f"{c_bold}Purchase:{c_reset}     {c_link}{purchase_url}{c_reset}\n"
                f"{f'{c_bold}Event Page:{c_reset}   {event_url}\n' if has_distinct_checkout else ''}"
                f"{c_header}{'=' * 78}{c_reset}\n"
            )
        else:
            banner = (
                f"\n{'=' * 78}\n"
                f"{prefix} TICKETS AVAILABLE: {alert.event_name}\n"
                f"{'=' * 78}\n"
                f"{session_line}"
                f"Section:      {alert.section_name}\n"
                f"Status:       {status_label}\n"
                f"Price:        {alert.formatted_price}\n"
                f"Time:         {time_str}\n"
                f"Purchase:     {purchase_url}\n"
                f"{event_page_line}"
                f"{'=' * 78}\n"
            )

        target_stream = self.stream
        try:
            target_stream.write(banner)
            target_stream.flush()
            logger.info("Console alert banner emitted for section '%s'.", alert.section_name)
            return True
        except UnicodeEncodeError:
            try:
                # Strip ANSI codes if present and convert to safe ASCII
                clean_banner = re.sub(r'\033\[[0-9;]*m', '', banner)
                safe_banner = clean_banner.encode("ascii", errors="replace").decode("ascii")
                target_stream.write(safe_banner)
                target_stream.flush()
                return True
            except Exception as exc:
                logger.exception("Failed to write sanitized banner to console stream: %s", exc)
                return False
        except Exception as exc:
            logger.exception("Failed to write alert banner to console stream: %s", exc)
            return False
