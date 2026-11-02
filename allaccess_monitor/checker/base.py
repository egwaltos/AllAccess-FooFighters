"""
Abstract base class definition for ticket availability checkers.
"""
from abc import ABC, abstractmethod
from typing import Optional, Sequence, Dict, Any, List
from allaccess_monitor.models import CheckResult


class BaseTicketChecker(ABC):
    """
    Abstract contract for ticket checkers.
    
    Subclasses must implement check_availability to fetch and evaluate ticket status.
    Supports multi-target checks, batch evaluation, and context manager protocol.
    """

    @abstractmethod
    def check_availability(self, section_name: str, event_url: Optional[str] = None) -> CheckResult:
        """
        Fetch and evaluate ticket availability for the target section.

        Args:
            section_name: Name of the venue section to check (e.g. 'Sivori media').
            event_url: Optional event URL override.

        Returns:
            CheckResult containing the status, section information, and any error messages.
        """
        pass

    def check_multi_availability(
        self, section_names: Sequence[str], event_url: Optional[str] = None
    ) -> Dict[str, CheckResult]:
        """
        Evaluate availability for multiple sections.
        Default implementation delegates to check_availability for each section.
        """
        return {sec: self.check_availability(sec, event_url=event_url) for sec in section_names}

    def check_target(self, target: Any) -> CheckResult:
        """Evaluate availability for a single MonitoredTarget or target-like object."""
        sec = getattr(target, "target_section", str(target))
        url = getattr(target, "event_url", None)
        return self.check_availability(section_name=sec, event_url=url)

    def check_targets(self, targets: Sequence[Any]) -> List[CheckResult]:
        """
        Evaluate availability for multiple MonitoredTarget objects.
        Returns a list of CheckResults corresponding to the input targets.
        """
        return [self.check_target(t) for t in targets]

    def close(self) -> None:
        """Release any underlying network connections, sockets, or sessions."""
        pass

    def __enter__(self) -> "BaseTicketChecker":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
