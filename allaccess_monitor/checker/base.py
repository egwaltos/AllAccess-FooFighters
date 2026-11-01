"""
Abstract base class definition for ticket availability checkers.
"""
from abc import ABC, abstractmethod
from allaccess_monitor.models import CheckResult


class BaseTicketChecker(ABC):
    """
    Abstract contract for ticket checkers.
    
    Subclasses must implement check_availability to fetch and evaluate ticket status.
    Supports context manager protocol for clean resource and session management.
    """

    @abstractmethod
    def check_availability(self, section_name: str) -> CheckResult:
        """
        Fetch and evaluate ticket availability for the target section.

        Args:
            section_name: Name of the venue section to check (e.g. 'Sivori media').

        Returns:
            CheckResult containing the status, section information, and any error messages.
        """
        pass

    def close(self) -> None:
        """Release any underlying network connections, sockets, or sessions."""
        pass

    def __enter__(self) -> "BaseTicketChecker":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
