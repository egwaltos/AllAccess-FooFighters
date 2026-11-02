"""
MockTicketChecker for offline simulation, unit testing, and CLI dry-run evaluation.
"""
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Optional, List, Union, Dict, Any, Sequence
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
        session_name: Optional[str] = "25/02/2027 20:00",
        session_date: Optional[str] = "2027-02-25T23:00:00Z",
        direct_checkout_url: Optional[str] = None,
        section_statuses: Optional[Dict[str, Union[str, AvailabilityStatus]]] = None,
        section_prices: Optional[Dict[str, float]] = None,
        section_service_charges: Optional[Dict[str, float]] = None,
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
        self.session_name = session_name
        self.session_date = session_date
        self.direct_checkout_url = direct_checkout_url
        self.section_statuses: Dict[str, AvailabilityStatus] = {
            k: self._normalize_status(v) for k, v in (section_statuses or {}).items()
        }
        self.section_prices: Dict[str, float] = dict(section_prices or {})
        self.section_service_charges: Dict[str, float] = dict(section_service_charges or {})

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

    def set_section_status(
        self,
        section_name: str,
        status: Union[str, AvailabilityStatus],
        price: Optional[float] = None,
        service_charge: Optional[float] = None,
    ) -> None:
        """Dynamically configure availability status and pricing for a specific section."""
        self.section_statuses[section_name] = self._normalize_status(status)
        if price is not None:
            self.section_prices[section_name] = float(price)
        if service_charge is not None:
            self.section_service_charges[section_name] = float(service_charge)

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

    def check_availability(self, section_name: str, event_url: Optional[str] = None) -> CheckResult:
        """Generate synthetic CheckResult based on configured state or sequence."""
        if self.latency_ms > 0:
            time.sleep(self.latency_ms / 1000.0)

        self.call_count += 1
        self.last_checked_section = section_name
        now = datetime.now(timezone.utc)
        effective_url = event_url or self.event_url

        # Determine effective status
        if section_name in self.section_statuses:
            active_status = self.section_statuses[section_name]
        elif self.sequence and self.sequence_index < len(self.sequence):
            active_status = self.sequence[self.sequence_index]
            self.sequence_index += 1
        else:
            active_status = self.current_status

        price = self.section_prices.get(section_name, self.price)
        service_charge = self.section_service_charges.get(section_name, self.service_charge)
        checkout_url = self.direct_checkout_url or f"{effective_url.rstrip('/')}/239575"

        # Build CheckResult
        if active_status == AvailabilityStatus.AVAILABLE:
            sec_info = SectionInfo(
                name=section_name or self.default_section_name,
                available=True,
                price=price,
                currency="ARS",
                service_charge=service_charge,
                raw_data={"mock": True, "state": "AVAILABLE"},
                session_name=self.session_name,
                session_date=self.session_date,
                direct_checkout_url=checkout_url,
            )
            result = CheckResult(
                status=AvailabilityStatus.AVAILABLE,
                section_name=section_name,
                section_info=sec_info,
                event_name=self.event_name,
                event_url=effective_url,
                timestamp=now,
                status_code=200,
                session_name=self.session_name,
                session_date=self.session_date,
                direct_checkout_url=checkout_url,
            )

        elif active_status == AvailabilityStatus.UNAVAILABLE:
            sec_info = SectionInfo(
                name=section_name or self.default_section_name,
                available=False,
                price=price,
                currency="ARS",
                service_charge=service_charge,
                raw_data={"mock": True, "state": "SOLD_OUT"},
                session_name=self.session_name,
                session_date=self.session_date,
                direct_checkout_url=checkout_url,
            )
            result = CheckResult(
                status=AvailabilityStatus.UNAVAILABLE,
                section_name=section_name,
                section_info=sec_info,
                event_name=self.event_name,
                event_url=effective_url,
                timestamp=now,
                status_code=200,
                session_name=self.session_name,
                session_date=self.session_date,
                direct_checkout_url=checkout_url,
            )

        elif active_status == AvailabilityStatus.SECTION_NOT_FOUND:
            result = CheckResult(
                status=AvailabilityStatus.SECTION_NOT_FOUND,
                section_name=section_name,
                section_info=None,
                event_name=self.event_name,
                event_url=effective_url,
                timestamp=now,
                error_message=f"Section '{section_name}' not found in mock layout",
                status_code=200,
                session_name=self.session_name,
                session_date=self.session_date,
                direct_checkout_url=checkout_url,
            )

        else:  # AvailabilityStatus.ERROR
            result = CheckResult(
                status=AvailabilityStatus.ERROR,
                section_name=section_name,
                section_info=None,
                event_name=self.event_name,
                event_url=effective_url,
                timestamp=now,
                error_message="Simulated mock error (network or parsing failure)",
                status_code=503,
                session_name=self.session_name,
                session_date=self.session_date,
                direct_checkout_url=checkout_url,
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

    @classmethod
    def multi_from_fixture(
        cls,
        fixture_path: Union[str, Path],
        section_names: Sequence[str],
        event_url: str = "",
    ) -> Dict[str, CheckResult]:
        """Parse multiple sections directly from a real or mock HTML fixture file."""
        from allaccess_monitor.checker.allaccess import parse_allaccess_sections
        p = Path(fixture_path)
        if not p.exists():
            raise FileNotFoundError(f"Fixture file not found: {p}")
        html_text = p.read_text(encoding="utf-8")
        return parse_allaccess_sections(html_text, target_sections=section_names, event_url=event_url)
