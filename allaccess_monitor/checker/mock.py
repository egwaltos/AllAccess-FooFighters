"""
MockTicketChecker for offline simulation, unit testing, and CLI dry-run evaluation.
"""
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Optional, List, Union, Dict, Any
import logging

from allaccess_monitor.checker.base import BaseTicketChecker
from allaccess_monitor.checker.allaccess import parse_allaccess_html
from allaccess_monitor.models import AvailabilityStatus, CheckResult, SectionInfo

logger = logging.getLogger(__name__)


class MockTicketChecker(BaseTicketChecker):
    """
    Mock checker providing deterministic, synthetic responses for all 5 canonical states.
    Supports sequence playback across multiple check calls.
    """

    def __init__(
        self,
        status: Union[str, AvailabilityStatus] = "sold_out",
        section_name: str = "Sivori Media",
        price: float = 125000.0,
        service_charge: float = 18750.0,
        event_name: str = "Foo Fighters",
        event_url: str = "https://www.allaccess.com.ar/event/foo-fighters",
        sequence: Optional[List[Union[str, AvailabilityStatus]]] = None,
        initial_status: Optional[Union[str, AvailabilityStatus]] = None,
        latency_ms: float = 0.0,
    ):
        effective_status = initial_status if initial_status is not None else status
        self.current_status = self._normalize_status(effective_status)
        self.default_section_name = section_name
        self.price = price
        self.service_charge = service_charge
        self.event_name = event_name
        self.event_url = event_url
        self.sequence: List[AvailabilityStatus] = (
            [self._normalize_status(s) for s in sequence] if sequence else []
        )
        self.sequence_index = 0
        self.latency_ms = max(0.0, float(latency_ms or 0.0))

        # Telemetry & Call tracking
        self.call_count = 0
        self.last_checked_section: Optional[str] = None
        self.calls: List[Dict[str, Any]] = []

    def _normalize_status(self, status: Union[str, AvailabilityStatus]) -> AvailabilityStatus:
        if isinstance(status, AvailabilityStatus):
            return status
        norm = str(status).strip().lower()
        if norm in ("available", "avail"):
            return AvailabilityStatus.AVAILABLE
        elif norm in ("sold_out", "unavailable", "soldout"):
            return AvailabilityStatus.UNAVAILABLE
        elif norm in ("section_missing", "section_not_found", "not_found", "missing"):
            return AvailabilityStatus.SECTION_NOT_FOUND
        elif norm in ("network_error", "error", "malformed", "waf_challenge", "rate_limit"):
            return AvailabilityStatus.ERROR
        else:
            raise ValueError(f"Unknown mock status: {status}")

    def set_status(self, status: Union[str, AvailabilityStatus]) -> None:
        """Dynamically update the active mock status."""
        self.current_status = self._normalize_status(status)

    def set_sequence(self, sequence: List[Union[str, AvailabilityStatus]]) -> None:
        """Configure a sequence of statuses to return across consecutive checks."""
        self.sequence = [self._normalize_status(s) for s in sequence]
        self.sequence_index = 0

    def set_latency(self, latency_ms: float) -> None:
        """Dynamically update the simulated network latency in milliseconds."""
        self.latency_ms = max(0.0, float(latency_ms or 0.0))

    def reset(self) -> None:
        """Reset call count and sequence index."""
        self.call_count = 0
        self.sequence_index = 0
        self.calls.clear()
        self.last_checked_section = None

    def check_availability(self, section_name: str) -> CheckResult:
        """Generate synthetic CheckResult based on configured state or sequence."""
        if self.latency_ms > 0:
            time.sleep(self.latency_ms / 1000.0)

        self.call_count += 1
        self.last_checked_section = section_name
        now = datetime.now(timezone.utc)

        # Determine effective status
        if self.sequence and self.sequence_index < len(self.sequence):
            active_status = self.sequence[self.sequence_index]
            self.sequence_index += 1
        else:
            active_status = self.current_status

        # Build CheckResult
        if active_status == AvailabilityStatus.AVAILABLE:
            sec_info = SectionInfo(
                name=section_name or self.default_section_name,
                available=True,
                price=self.price,
                currency="ARS",
                service_charge=self.service_charge,
                raw_data={"mock": True, "state": "AVAILABLE"},
            )
            result = CheckResult(
                status=AvailabilityStatus.AVAILABLE,
                section_name=section_name,
                section_info=sec_info,
                event_name=self.event_name,
                event_url=self.event_url,
                timestamp=now,
                status_code=200,
            )

        elif active_status == AvailabilityStatus.UNAVAILABLE:
            sec_info = SectionInfo(
                name=section_name or self.default_section_name,
                available=False,
                price=self.price,
                currency="ARS",
                service_charge=self.service_charge,
                raw_data={"mock": True, "state": "SOLD_OUT"},
            )
            result = CheckResult(
                status=AvailabilityStatus.UNAVAILABLE,
                section_name=section_name,
                section_info=sec_info,
                event_name=self.event_name,
                event_url=self.event_url,
                timestamp=now,
                status_code=200,
            )

        elif active_status == AvailabilityStatus.SECTION_NOT_FOUND:
            result = CheckResult(
                status=AvailabilityStatus.SECTION_NOT_FOUND,
                section_name=section_name,
                section_info=None,
                event_name=self.event_name,
                event_url=self.event_url,
                timestamp=now,
                error_message=f"Section '{section_name}' not found in mock layout",
                status_code=200,
            )

        else:  # AvailabilityStatus.ERROR
            result = CheckResult(
                status=AvailabilityStatus.ERROR,
                section_name=section_name,
                section_info=None,
                event_name=self.event_name,
                event_url=self.event_url,
                timestamp=now,
                error_message="Simulated mock error (network or parsing failure)",
                status_code=503,
            )

        self.calls.append({"timestamp": now, "section_name": section_name, "result": result})
        return result

    @classmethod
    def from_fixture(
        cls, fixture_path: Union[str, Path], section_name: str = "Sivori media"
    ) -> CheckResult:
        """
        Convenience method to parse a real or mock HTML fixture file directly from disk.
        """
        p = Path(fixture_path)
        if not p.exists():
            raise FileNotFoundError(f"Fixture file not found: {p}")
        html_text = p.read_text(encoding="utf-8")
        return parse_allaccess_html(html_text, target_section=section_name)
