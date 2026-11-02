"""
Domain models for AllAccess ticket monitoring and alerting.
Strictly adheres to PROJECT.md contracts with frozen immutability and UTC timestamps.
"""
import math
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, Any, List, Tuple


def normalize_section_name(text: Optional[str]) -> str:
    """Normalize section names stripping accents, diacritics, and irregular whitespace."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(text))
    ascii_bytes = nfkd.encode("ASCII", "ignore")
    ascii_str = ascii_bytes.decode("utf-8")
    return " ".join(ascii_str.split()).lower()


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
class MonitoredTarget:
    """Represents a specific event and sector combination to monitor."""
    event_url: str
    target_section: str
    event_name: Optional[str] = None
    max_price: Optional[float] = None

    def __post_init__(self):
        if not self.event_url or not isinstance(self.event_url, str) or not self.event_url.strip():
            raise ValueError("MonitoredTarget 'event_url' cannot be empty.")
        if not self.target_section or not isinstance(self.target_section, str) or not self.target_section.strip():
            raise ValueError("MonitoredTarget 'target_section' cannot be empty.")
        if self.max_price is not None:
            try:
                price_val = float(self.max_price)
            except (ValueError, TypeError):
                raise ValueError(f"MonitoredTarget 'max_price' must be a valid number, got {self.max_price!r}.")
            if math.isnan(price_val) or math.isinf(price_val) or price_val <= 0:
                raise ValueError(f"MonitoredTarget 'max_price' must be positive, got {self.max_price}.")

    @property
    def target_id(self) -> Tuple[str, str]:
        """Unique compound identifier key (event_url, normalized_section)."""
        return (self.event_url.strip(), normalize_section_name(self.target_section))

    def matches_result(self, result: "CheckResult") -> bool:
        """Determines if a CheckResult corresponds to this monitored target."""
        if not result or not getattr(result, "event_url", None) or not getattr(result, "section_name", None):
            return False
        if not str(result.event_url).strip() or not str(result.section_name).strip():
            return False
        return (
            self.event_url.strip().rstrip("/").lower() == str(result.event_url).strip().rstrip("/").lower()
            and normalize_section_name(self.target_section) == normalize_section_name(result.section_name)
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serializes target to standard JSON-compatible dictionary."""
        data: Dict[str, Any] = {
            "event_url": self.event_url,
            "target_section": self.target_section,
        }
        if self.event_name is not None:
            data["event_name"] = self.event_name
        if self.max_price is not None:
            data["max_price"] = self.max_price
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MonitoredTarget":
        """Deserializes target from dictionary supporting alias keys."""
        if not isinstance(data, dict):
            raise TypeError(f"Expected dict for MonitoredTarget, got {type(data).__name__}")
        event_url = data.get("event_url") or data.get("url")
        target_section = data.get("target_section") or data.get("section")
        event_name = data.get("event_name") or data.get("name")
        max_price_raw = data.get("max_price")
        max_price = float(max_price_raw) if max_price_raw is not None else None
        return cls(
            event_url=str(event_url).strip() if event_url else "",
            target_section=str(target_section).strip() if target_section else "",
            event_name=str(event_name).strip() if event_name else None,
            max_price=max_price,
        )


@dataclass(frozen=True)
class SectionInfo:
    """Information regarding a specific venue section and its ticketing data."""
    name: str
    available: bool
    price: Optional[float] = None
    currency: str = "ARS"
    service_charge: Optional[float] = None
    raw_data: Optional[Dict[str, Any]] = None
    session_name: Optional[str] = None
    session_date: Optional[str] = None
    direct_checkout_url: Optional[str] = None

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

    @property
    def has_pricing(self) -> bool:
        """Returns True if pricing data is present."""
        return self.price is not None


@dataclass(frozen=True)
class CheckResult:
    """Outcome of a single ticket availability check against AllAccess."""
    status: AvailabilityStatus
    section_name: str = ""
    section_info: Optional[SectionInfo] = None
    event_name: str = "Foo Fighters"
    event_url: str = "https://www.allaccess.com.ar/event/foo-fighters"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error_message: Optional[str] = None
    status_code: Optional[int] = None
    session_name: Optional[str] = None
    session_date: Optional[str] = None
    direct_checkout_url: Optional[str] = None
    all_sections: List[SectionInfo] = field(default_factory=list)
    response_time_ms: float = 0.0
    section: Optional[SectionInfo] = None

    def __post_init__(self):
        # Bi-directional sync between section and section_info aliases
        if self.section is not None and self.section_info is None:
            object.__setattr__(self, "section_info", self.section)
        elif self.section_info is not None and self.section is None:
            object.__setattr__(self, "section", self.section_info)

        # Fallback section_name from section_info if empty
        if not self.section_name and self.section_info is not None:
            object.__setattr__(self, "section_name", self.section_info.name)

        # Automatic inheritance of session metadata from SectionInfo if omitted
        if self.section_info is not None:
            if self.session_name is None and self.section_info.session_name is not None:
                object.__setattr__(self, "session_name", self.section_info.session_name)
            if self.session_date is None and self.section_info.session_date is not None:
                object.__setattr__(self, "session_date", self.section_info.session_date)
            if self.direct_checkout_url is None and self.section_info.direct_checkout_url is not None:
                object.__setattr__(self, "direct_checkout_url", self.section_info.direct_checkout_url)

    @property
    def is_available(self) -> bool:
        """Returns True if the check detected available tickets."""
        return self.status == AvailabilityStatus.AVAILABLE

    def to_alert(self, is_reminder: bool = False) -> "TicketAlert":
        """Converts check result into a dispatchable TicketAlert domain event."""
        price = self.section_info.total_price if self.section_info else None
        currency = self.section_info.currency if self.section_info else "ARS"
        status_str = "REMINDER" if is_reminder else self.status.value
        session_name = self.session_name or (self.section_info.session_name if self.section_info else None)
        session_date = self.session_date or (self.section_info.session_date if self.section_info else None)
        direct_checkout_url = self.direct_checkout_url or (self.section_info.direct_checkout_url if self.section_info else None)
        return TicketAlert(
            event_name=self.event_name,
            section_name=self.section_name,
            status=status_str,
            event_url=self.event_url,
            price=price,
            currency=currency,
            timestamp=self.timestamp,
            is_reminder=is_reminder,
            session_name=session_name,
            session_date=session_date,
            direct_checkout_url=direct_checkout_url,
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
    session_name: Optional[str] = None
    session_date: Optional[str] = None
    direct_checkout_url: Optional[str] = None

    @property
    def formatted_price(self) -> str:
        """Formatted price representation for alerts."""
        if self.price is None:
            return "See AllAccess"
        return f"${self.price:,.0f} {self.currency}" if self.price.is_integer() else f"${self.price:,.2f} {self.currency}"
