"""
Composite notifier multiplexer.
Dispatches ticket alerts across all enabled notification channels with fault isolation and detailed per-channel reporting.
"""
import logging
from typing import Dict, List, Optional

from allaccess_monitor.models import TicketAlert
from allaccess_monitor.notifier.base import BaseNotifier

logger = logging.getLogger(__name__)


class CompositeNotifier(BaseNotifier):
    """
    Multiplexing notification provider adhering to the Composite pattern.
    Delivers a single TicketAlert to multiple underlying channels sequentially,
    isolating exceptions so that a failure in one channel never stops delivery to other channels.
    """

    def __init__(self, notifiers: Optional[List[BaseNotifier]] = None):
        """
        Initialize Composite notifier.

        Args:
            notifiers: Optional initial list of child BaseNotifier implementations.
        """
        self.notifiers: List[BaseNotifier] = list(notifiers or [])
        self.last_delivery_report: Dict[str, bool] = {}

    @property
    def channel_name(self) -> str:
        """Channel identifier for logging and metrics."""
        return "composite"

    def add_notifier(self, notifier: BaseNotifier) -> None:
        """
        Appends a notification channel to the composite pipeline.

        Args:
            notifier: An instance of BaseNotifier to register.
        """
        if notifier not in self.notifiers:
            self.notifiers.append(notifier)

    def send(self, alert: TicketAlert) -> bool:
        """
        Multiplexes the ticket alert across all registered channels.

        Fault Isolation:
            Each channel delivery is wrapped in exception containment.
            A crash or timeout in one channel does not interrupt or delay subsequent channels.

        Args:
            alert: The TicketAlert instance to broadcast.

        Returns:
            bool: True if at least one channel delivered successfully, False if all channels failed or list is empty.
        """
        if not self.notifiers:
            logger.warning("No notification channels registered in CompositeNotifier.")
            self.last_delivery_report = {}
            return False

        report: Dict[str, bool] = {}

        for notifier in self.notifiers:
            name = getattr(notifier, "channel_name", notifier.__class__.__name__)
            try:
                success = bool(notifier.send(alert))
                report[name] = success
                if success:
                    logger.info("Channel '%s' successfully delivered alert.", name)
                else:
                    logger.warning("Channel '%s' failed to deliver alert.", name)
            except Exception as exc:
                logger.exception("Uncaught exception while dispatching alert via '%s': %s", name, exc)
                report[name] = False

        self.last_delivery_report = report
        logger.info("Composite delivery completed. Summary: %s", report)
        return any(report.values())

    def __len__(self) -> int:
        """Returns the number of registered channels."""
        return len(self.notifiers)
