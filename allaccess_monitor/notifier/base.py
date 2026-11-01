"""
Abstract base class definition for notification channels.
Enforces the Strategy pattern across messaging delivery providers.
"""
from abc import ABC, abstractmethod
from allaccess_monitor.models import TicketAlert


class BaseNotifier(ABC):
    """
    Abstract interface for all notification delivery channels.
    Implementations must be thread-safe and must encapsulate channel-specific error handling.
    """

    @property
    @abstractmethod
    def channel_name(self) -> str:
        """
        Identifier name of the notification channel (e.g. 'telegram', 'discord', 'email', 'console', 'composite').
        """
        pass

    @abstractmethod
    def send(self, alert: TicketAlert) -> bool:
        """
        Deliver a ticket alert to the underlying channel.

        Args:
            alert: The TicketAlert domain event containing ticket details.

        Returns:
            bool: True if the alert was successfully accepted/sent by the channel, False otherwise.
                  Must catch channel-specific exceptions internally to prevent daemon crashes.
        """
        pass
