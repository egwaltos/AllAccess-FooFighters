"""
Domain models for AllAccess ticket monitoring and alerting.
Strictly adheres to PROJECT.md contracts with frozen immutability and UTC timestamps.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, Any


class AvailabilityStatus(Enum):
    """Enumeration of ticket availability states."""
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    SECTION_NOT_FOUND = "SECTION_NOT_FOUND"
    ERROR = "ERROR"

    @property
    def is_available(self) -> bool:
        """Helper to quickly check if status denotes ticket availability."""
        return self == AvailabilityStatus.AVAILABLE


@dataclass(frozen=True)
class SectionInfo:
    """Information regarding a specific venue section and its ticketing data."""
    name: str
    available: bool
    price: Optional[float] = None
    currency: str = "ARS"
    service_charge: Optional[float] = None
    raw_data: Optional[Dict[str, Any]] = None

    @property
    def total_price(self) -> Optional[float]:
        """Calculates total purchase price including service charges if present."""
        if self.price is None:
            return None
        return self.price + (self.service_charge or 0.0)

    @property
    def formatted_price(self) -> str:
        """Returns human-readable formatted price string."""
        if self.price is None:
            return "N/A"
        p_str = f"${self.price:,.0f}" if self.price.is_integer() else f"${self.price:,.2f}"
        if self.service_charge:
            sc_str = f"${self.service_charge:,.0f}" if self.service_charge.is_integer() else f"${self.service_charge:,.2f}"
            tot = self.total_price
            tot_str = f"${tot:,.0f}" if tot and tot.is_integer() else f"${tot:,.2f}"
            return f"{tot_str} {self.currency} (Base: {p_str} + Srv: {sc_str})"
        return f"{p_str} {self.currency}"


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single ticket availability check against AllAccess."""
    status: AvailabilityStatus
    section_name: str
    section_info: Optional[SectionInfo] = None
    event_name: str = "Foo Fighters"
    event_url: str = "https://www.allaccess.com.ar/event/foo-fighters"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_message: Optional[str] = None
    status_code: Optional[int] = None

    @property
    def is_available(self) -> bool:
        """Returns True if the check detected available tickets."""
        return self.status == AvailabilityStatus.AVAILABLE

    def to_alert(self, is_reminder: bool = False) -> "TicketAlert":
        """Converts check result into a dispatchable TicketAlert domain event."""
        price = self.section_info.total_price if self.section_info else None
        currency = self.section_info.currency if self.section_info else "ARS"
        status_str = "REMINDER" if is_reminder else self.status.value
        return TicketAlert(
            event_name=self.event_name,
            section_name=self.section_name,
            status=status_str,
            event_url=self.event_url,
            price=price,
            currency=currency,
            timestamp=self.timestamp,
            is_reminder=is_reminder,
        )


@dataclass(frozen=True)
class TicketAlert:
    """Notification payload dispatched to messaging channels when tickets are available."""
    event_name: str
    section_name: str
    status: str                         # "AVAILABLE" | "REMINDER"
    event_url: str
    price: Optional[float] = None
    currency: str = "ARS"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_reminder: bool = False

    @property
    def formatted_price(self) -> str:
        """Formatted price representation for alerts."""
        if self.price is None:
            return "See AllAccess"
        return f"${self.price:,.0f} {self.currency}" if self.price.is_integer() else f"${self.price:,.2f} {self.currency}"
