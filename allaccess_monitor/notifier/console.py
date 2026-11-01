"""
Console notification channel implementation.
Dispatches formatted alert summaries to stdout or a designated text stream for dry-run and local testing.
"""
import logging
import sys
from typing import Optional, TextIO

from allaccess_monitor.models import TicketAlert
from allaccess_monitor.notifier.base import BaseNotifier

logger = logging.getLogger(__name__)


class ConsoleNotifier(BaseNotifier):
    """
    Console and dry-run notification delivery provider.
    Outputs structured alert banners to terminal or stream without external network requests.
    """

    def __init__(self, stream: Optional[TextIO] = None):
        """
        Initialize Console notifier.

        Args:
            stream: Target text stream (defaults to dynamic sys.stdout).
        """
        self._stream = stream

    @property
    def stream(self) -> TextIO:
        """Target output stream."""
        return self._stream if self._stream is not None else sys.stdout

    @property
    def channel_name(self) -> str:
        """Channel identifier for logging and metrics."""
        return "console"

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

        banner = (
            f"\n{'=' * 78}\n"
            f"{prefix} TICKETS AVAILABLE: {alert.event_name}\n"
            f"{'=' * 78}\n"
            f"Section:    {alert.section_name}\n"
            f"Status:     AVAILABLE\n"
            f"Price:      {alert.formatted_price}\n"
            f"Time:       {time_str}\n"
            f"Purchase:   {alert.event_url}\n"
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
                safe_banner = banner.encode("ascii", errors="replace").decode("ascii")
                target_stream.write(safe_banner)
                target_stream.flush()
                return True
            except Exception as exc:
                logger.exception("Failed to write sanitized banner to console stream: %s", exc)
                return False
        except Exception as exc:
            logger.exception("Failed to write alert banner to console stream: %s", exc)
            return False
